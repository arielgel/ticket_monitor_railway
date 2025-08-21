import os
import re
import time
import threading
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

# ==============================
# Sniffer de disponibilidad del mapa
# ==============================
import json

# Palabras clave típicas en endpoints de mapa
SEATMAP_ENDPOINT_HINTS = ("seat", "seats", "zones", "zone", "sections", "section", "map", "availability", "inventory")
JSON_MIME_HINTS = ("application/json", "application/ld+json", "application/vnd.api+json")

def _is_seatmap_like(url: str) -> bool:
    u = url.lower()
    return any(k in u for k in SEATMAP_ENDPOINT_HINTS)

def _content_type_is_json(resp) -> bool:
    try:
        ct = resp.headers.get("content-type", "")
        return any(h in ct for h in JSON_MIME_HINTS)
    except Exception:
        return False

def _parse_availability_from_json(obj) -> list[tuple[str,int]]:
    """
    Heurística para distintas formas de JSON: buscamos estructuras con 'sector/zone/section' y 'available/remaining'.
    Devuelve lista de (sector, disponibles>0).
    """
    out = []
    def add(name, avail):
        try:
            avail = int(avail)
        except Exception:
            return
        name = f"{name}".strip()
        if name and avail > 0:
            out.append((name, avail))

    def walk(o):
        if isinstance(o, dict):
            # keys útiles
            keys = {k.lower(): k for k in o.keys()}
            # nombres
            name = o.get(keys.get("name","name")) or o.get(keys.get("zone","zone")) or o.get(keys.get("section","section")) or o.get(keys.get("sector","sector"))
            # disponibles
            avail = None
            for k in ["available","remaining","free","availability","stock","disponibles","cupos"]:
                if k in keys:
                    avail = o[keys[k]]
                    break
            # algunos payloads traen 'capacity' y 'sold', calculamos:
            if avail is None and "capacity" in keys and "sold" in keys:
                try:
                    avail = int(o[keys["capacity"]]) - int(o[keys["sold"]])
                except Exception:
                    pass
            if name is not None and avail is not None:
                add(name, avail)
            # bajar
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    try:
        walk(obj)
    except Exception:
        pass
    # dedupe y ordenar por disponibles desc
    ded = {}
    for n,a in out:
        ded[n] = max(ded.get(n,0), a)
    return sorted(ded.items(), key=lambda x: (-x[1], x[0].lower()))

def _sniff_seatmap_availability(page, wait_ms=4000) -> list[tuple[str,int]]:
    """
    Escucha respuestas durante wait_ms y devuelve sectores disponibles detectados por JSON.
    """
    found = []

    def on_response(resp):
        try:
            url = resp.url
            if not _is_seatmap_like(url):
                return
            if resp.status < 200 or resp.status >= 400:
                return
            if not _content_type_is_json(resp):
                return
            txt = resp.text()
            if not txt or len(txt) < 2:
                return
            obj = json.loads(txt)
            cand = _parse_availability_from_json(obj)
            if cand:
                found.extend(cand)
        except Exception:
            return

    page.on("response", on_response)
    try:
        page.wait_for_timeout(wait_ms)
    finally:
        try:
            page.off("response", on_response)
        except Exception:
            pass

    # dedupe final
    ded = {}
    for n,a in found:
        ded[n] = max(ded.get(n,0), a)
    return sorted(ded.items(), key=lambda x: (-x[1], x[0].lower()))

# ==============================
# Plan B DOM: leer sectores visibles en el mapa
# ==============================
SECTOR_HINT_SELECTORS = [
    "[class*='sector']",
    "[class*='zona']",
    "[class*='zone']",
    "[class*='section']",
    "[data-testid*='sector']",
    "[data-testid*='zone']",
    ".legend li", ".legend .item",
    ".map-legend li", ".map-legend .item",
    "[role='list'] [role='listitem']",
]

def _parse_sector_text(txt: str) -> tuple[str,int|None,bool]:
    """
    Intenta extraer (nombre, disponibles?, agotado?).
    Ejs de textos: "Campo A — Disponible", "Platea Lateral (43)", "VIP - Agotado".
    """
    t = (txt or "").strip()
    tlow = t.lower()
    agot = any(k in tlow for k in ["agotado","sold out","sin disponibilidad","no disponible"])
    # buscar número entre paréntesis
    m = re.search(r"\((\d{1,4})\)", t)
    avail = int(m.group(1)) if m else None
    # limpiar nombre
    name = re.sub(r"\s*[-—–]\s*(agotado|sold out|sin disponibilidad).*", "", t, flags=re.I)
    name = re.sub(r"\(\d{1,4}\)", "", name).strip(" -—–\t")
    return name, avail, agot

def _read_sectors_from_dom(page) -> list[tuple[str,int]]:
    out = []
    try:
        nodes = page.locator(", ".join(SECTOR_HINT_SELECTORS))
        n = min(nodes.count(), 200)
        for i in range(n):
            it = nodes.nth(i)
            try:
                txt = (it.inner_text(timeout=250) or "").strip()
            except Exception:
                txt = ""
            if not txt:
                continue
            name, avail, agot = _parse_sector_text(txt)
            if not name or agot:
                continue
            out.append((name, avail if isinstance(avail,int) else 1))  # si no hay número pero no dice agotado, asumimos 1+
    except Exception:
        pass
    # dedupe y ordenar
    ded = {}
    for n,a in out:
        ded[n] = max(ded.get(n,0), a)
    return sorted(ded.items(), key=lambda x: (-x[1], x[0].lower()))

# ==============================
# Selección de fecha → mapa → extracción
# ==============================
def _open_map_for_date(dest_page) -> None:
    """
    Intenta abrir/mostrar el mapa (depende del flujo).
    A veces ya está visible; si no, click en 'Ver mapa', 'Seleccionar ubicación', etc.
    """
    triggers = [
        "button:has-text('Ver mapa')", "a:has-text('Ver mapa')",
        "button:has-text('Seleccionar ubicación')",
        "button:has-text('Seleccionar ubicaciones')",
        "button:has-text('Elegir ubicación')",
        "button:has-text('Elegir ubicaciones')",
        "[data-testid*='mapa']", "[data-testid*='seatmap']",
    ]
    for sel in triggers:
        try:
            btn = dest_page.locator(sel).first
            if btn and btn.count() > 0:
                btn.click(timeout=1500, force=True)
                dest_page.wait_for_timeout(400)
                break
        except Exception:
            continue
    # esperar algo típico de mapa
    try:
        dest_page.wait_for_selector(", ".join(SECTOR_HINT_SELECTORS), timeout=3000)
    except Exception:
        pass

def _choose_function_by_label(page, label: str) -> 'playwright.sync_api.Page':
    """
    Hace click en la fecha/función cuyo label contiene la fecha DD/MM/YYYY.
    Luego devuelve la page de destino (popup o inline) donde está el mapa/checkout.
    """
    # intentar abrir dropdown/menú por si hace falta
    _open_dropdown_if_any(page)

    # buscar item con esa fecha
    candidates = [
        "[role='listbox'] [role='option']",
        ".MuiMenuItem-root",
        "select option",
        "li", "button", "a", "div"
    ]
    found = None
    for sel in candidates:
        try:
            items = page.locator(sel)
            n = min(items.count(), 200)
            for i in range(n):
                it = items.nth(i)
                try:
                    txt = (it.inner_text(timeout=200) or "").strip()
                except Exception:
                    txt = ""
                if label in txt:
                    found = it; break
            if found: break
        except Exception:
            continue
    if not found:
        return page

    # click → popup o inline
    try:
        with page.expect_popup() as pinfo:
            found.click(timeout=1500, force=True)
        dest = pinfo.value
        try: dest.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception: pass
        return dest
    except Exception:
        # quizá es inline
        found.click(timeout=1500, force=True)
        try: page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception: pass
        return page

def _extract_sectors_for_date(dest) -> list[tuple[str,int]]:
    """
    Preferimos API (sniffer). Si no aparece nada, leemos DOM del mapa.
    """
    # dar unos ms para que dispare requests del mapa
    _open_map_for_date(dest)
    sectors = _sniff_seatmap_availability(dest, wait_ms=4000)
    if sectors:
        return sectors
    # plan B DOM
    return _read_sectors_from_dom(dest)

# ==============================
# Comando Telegram: /sectores N
# ==============================
def cmd_list_sectors_for_show_index(idx: int):
    url = URLS[idx - 1]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=15000)
            title = extract_title(page) or prettify_from_slug(url)

            # 1) Listar fechas válidas (reutilizamos tu pipeline)
            region = _find_functions_region(page)
            fechas = _gather_dates_in_region(region)  # dd/mm/yyyy ya filtradas/ordenadas

            if not fechas:
                tg_send(f"❓ Sin fechas visibles en {title}{SIGN}", force=True)
                return

            lines = [f"🧭 <b>{title}</b> — Sectores disponibles:"]
            for f in fechas:
                # 2) Selecciono fecha → destino
                dest = _choose_function_by_label(page, f)
                try:
                    dest.wait_for_load_state("networkidle", timeout=6000)
                except Exception:
                    pass

                # 3) Extraigo sectores
                sectors = _extract_sectors_for_date(dest)
                if sectors:
                    nice = ", ".join([f"{n} ({a})" for n,a in sectors])
                    lines.append(f"{f}: {nice}")
                else:
                    lines.append(f"{f}: —")

                # cerrar popup si es distinto
                if dest is not page:
                    try: dest.close()
                    except Exception: pass

            tg_send("\n".join(lines) + f"\n{SIGN}", force=True)

        except Exception as e:
            tg_send(f"💥 Error /sectores {idx}: {e}{SIGN}", force=True)
        finally:
            try: browser.close()
            except Exception: pass


# ==============================
# Config
# ==============================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
URLS = [u.strip() for u in os.getenv("MONITORED_URLS", "").split(",") if u.strip()]
CHECK_EVERY = int(os.getenv("CHECK_EVERY_SECONDS", "300"))
TZ_NAME = os.getenv("TIMEZONE", "America/Argentina/Buenos_Aires")
PREFERRED_MARKET = os.getenv("PREFERRED_MARKET", "Argentina")
MAX_FUTURE_MONTHS = int(os.getenv("MAX_FUTURE_MONTHS", "18"))

SIGN = " — Roberto"

# ==============================
# Estado
# ==============================
LAST_RESULTS = {u: {"status": "UNKNOWN", "detail": None, "ts": "", "title": None} for u in URLS}

# ==============================
# Utilitarios
# ==============================
def now_local():
    return datetime.now(ZoneInfo(TZ_NAME))

def within_quiet_hours():
    h = now_local().hour
    return 0 <= h < 9

def tg_send(text: str, force: bool = False):
    if not force and within_quiet_hours():
        print("⏸️ Silenciado:", text); return
    if BOT_TOKEN and CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=20,
            ).raise_for_status()
        except Exception as e:
            print("❌ Error Telegram:", e)
    print(text)

def page_text(page) -> str:
    try:
        return (page.evaluate("() => document.body.innerText") or "").lower()
    except Exception:
        return ""

def page_has_soldout(page) -> bool:
    t = page_text(page)
    return any(k in t for k in ["agotado", "sold out", "sin disponibilidad", "sem disponibilidade"])

def page_has_buy(page) -> bool:
    # Detector global (solo para debug). Para lógica de negocio usamos region_has_buy.
    t = page_text(page)
    return any(k in t for k in ["comprar", "comprar entradas", "buy tickets", "entradas"])

def extract_title(page):
    title = ""
    try:
        title = (page.title() or "").strip()
    except Exception:
        pass
    try:
        og = page.locator('meta[property="og:title"]').first
        if og.count() > 0:
            c = (og.get_attribute("content") or "").strip()
            if c: title = c
    except Exception:
        pass
    title = re.sub(r"\s+\|\s*All\s*Access.*$", "", title, flags=re.I)
    return title or None

def prettify_from_slug(url: str) -> str:
    slug = url.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").title()

def fmt_status_entry(url: str, info: dict, include_url: bool = True) -> str:
    title = info.get("title") or ""
    st = info.get("status", "UNKNOWN")
    det = info.get("detail") or ""
    ts = info.get("ts", "")
    head = title if title else (url if include_url else "Show")
    if include_url and title: head = f"{title}\n{url}"
    if st == "AVAILABLE":
        line = f"✅ <b>Disponible</b> — {head}"
        if not include_url and title: line = f"✅ <b>Disponible</b> — {title}"
        if det: line += f"\nFechas: {det}"
    elif st == "SOLDOUT":
        line = f"⛔ Agotado — {head}" if include_url else f"⛔ Agotado — {title or 'Show'}"
    else:
        line = f"❓ Indeterminado — {head}" if include_url else f"❓ Indeterminado — {title or 'Show'}"
        if det: line += f"\nNota: {det}"
    if ts: line += f"\nÚltimo check: {ts}"
    return line

def fmt_status_snapshot(snap: dict) -> str:
    lines = [f"📊 Estado actual (N={len(snap)}){SIGN}"]
    for url in URLS:
        info = snap.get(url, {"status": "UNKNOWN", "detail": None, "ts": "", "title": None})
        lines.append("• " + fmt_status_entry(url, info, include_url=True))
    return "\n".join(lines)

def quick_url_check(url: str) -> tuple[bool, str]:
    try:
        r = requests.get(url, timeout=6, allow_redirects=True, headers={"User-Agent":"Mozilla/5.0"})
        if 200 <= r.status_code < 400: return True, ""
        return False, f"ERROR HTTP {r.status_code}"
    except Exception as e:
        return False, f"ERROR {type(e).__name__}"

def fmt_shows_indexed() -> str:
    lines = [f"🎯 Monitoreando (N={len(URLS)}){SIGN}"]
    if not URLS:
        lines.append("(no hay URLs configuradas)"); return "\n".join(lines)
    for i, u in enumerate(URLS, start=1):
        title = (LAST_RESULTS.get(u) or {}).get("title")
        label = title or prettify_from_slug(u)
        ok, err = quick_url_check(u)
        lines.append(f"{i}) {label}" + ("" if ok else f"  ❗ {err}"))
    return "\n".join(lines)

# ==============================
# Fechas
# ==============================
PAISES_COMUNES = {"argentina","brasil","colombia","chile","uruguay","perú","peru","paraguay","bolivia","mexico","méxico","portugal","españa","otros","other","latam"}

RE_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[-/](\d{1,2})(?:[-/](\d{2,4}))?\b")
RE_MONTH_NAME   = re.compile(r"\b(\d{1,2})\s+(?:de\s+)?(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)(?:\s+de)?\s+(\d{4})\b", re.IGNORECASE)
MESES_ES = ["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"]
MESES_ABBR_ES = ["ene","feb","mar","abr","may","jun","jul","ago","sep","oct","nov","dic"]
RE_MONTH_ABBR  = re.compile(r"\b(\d{1,2})\s*[-/ ]\s*(ene|feb|mar|abr|may|jun|jul|ago|sep|oct|nov|dic)\s*[-/ ]\s*(\d{4})\b", re.IGNORECASE)

def _month_to_num(m: str) -> int:
    m = m.strip().lower()
    for i, name in enumerate(MESES_ES, start=1):
        if name == m: return i
    return 0

def extract_dates_only(text: str) -> list[str]:
    out, seen = [], set()
    for d, m, y in RE_NUMERIC_DATE.findall(text):
        dd, mm = int(d), int(m)
        if not (1 <= dd <= 31 and 1 <= mm <= 12): continue
        s = f"{dd:02d}/{mm:02d}/{int(y):04d}" if y else f"{dd:02d}/{mm:02d}"
        if s not in seen: seen.add(s); out.append(s)
    for d, mes, y in RE_MONTH_NAME.findall(text):
        dd, mm, yy = int(d), _month_to_num(mes), int(y)
        if not (1 <= dd <= 31 and 1 <= mm <= 12): continue
        s = f"{dd:02d}/{mm:02d}/{yy:04d}"
        if s not in seen: seen.add(s); out.append(s)
    for d, mes_abbr, y in RE_MONTH_ABBR.findall(text):
        dd = int(d); mes_abbr = mes_abbr.lower()
        try: mm = MESES_ABBR_ES.index(mes_abbr) + 1
        except ValueError: mm = 0
        if not (1 <= dd <= 31 and 1 <= mm <= 12): continue
        yy = int(y)
        s = f"{dd:02d}/{mm:02d}/{yy:04d}"
        if s not in seen: seen.add(s); out.append(s)
    return out

def _parse_date_str(s: str):
    parts = s.split("/")
    if len(parts) == 3:
        try:
            dd, mm, yy = int(parts[0]), int(parts[1]), int(parts[2])
            return datetime(yy, mm, dd, tzinfo=ZoneInfo(TZ_NAME))
        except Exception:
            return None
    return None

def filter_and_sort_dates(fecha_strs: list[str]) -> list[str]:
    """Solo fechas futuras y dentro de MAX_FUTURE_MONTHS. Orden ascendente."""
    out = []
    now = now_local()
    horizon = now + timedelta(days=MAX_FUTURE_MONTHS*30)
    seen = set()
    for s in fecha_strs:
        dt = _parse_date_str(s)
        if not dt: 
            continue
        if not (now < dt <= horizon):
            continue
        key = dt.strftime("%Y-%m-%d")
        if key in seen: 
            continue
        seen.add(key)
        out.append(dt)
    out.sort()
    return [d.strftime("%d/%m/%Y") for d in out]

# ==============================
# Selectores / UI
# ==============================
FUNC_TRIGGERS = [
    "button[aria-haspopup='listbox']",
    "[role='combobox']",
    "[aria-controls*='menu']",
    "[data-testid*='select']",
    "div.MuiSelect-select",
    ".MuiSelect-select",
    "button:has-text('Seleccionar')",
    "button:has-text('Seleccioná')",
    "button:has-text('Selecciona')",
    "button:has-text('Elegí')",
    "button:has-text('Elegi')",
    "button:has-text('Fecha')",
    "button:has-text('Función')",
    "[role='button']:has-text('Fecha')",
    "[role='button']:has-text('Función')",
]
CTA_BUY_SELECTORS = [
    "a:has-text('Comprar')",
    "a:has-text('Comprar entradas')",
    "button:has-text('Comprar')",
    "button:has-text('Comprar entradas')",
    "[data-testid*='comprar']",
    "[data-testid*='buy']",
    "[href*='comprar']",
]

# ==============================
# Región de funciones (recorte)
# ==============================
def _nearest_block(locator, max_up=6):
    """Sube por ancestros y devuelve el bloque contenedor real."""
    for lvl in range(1, max_up + 1):
        try:
            anc = locator.locator(f":scope >> xpath=ancestor::*[{lvl}]")
            if anc and anc.count() > 0:
                try:
                    bb = anc.bounding_box()
                    txt = (anc.inner_text(timeout=250) or "").strip()
                except Exception:
                    bb, txt = None, ""
                if bb and len(txt) > 10:
                    return anc.first
        except Exception:
            continue
    return locator if locator and locator.count() > 0 else None

def _find_functions_region(page):
    """
    Devuelve SOLO la región del selector de funciones.
    Prioridad:
      1) bloque del texto 'Selecciona la función'
      2) bloque del botón 'Ver entradas'
      3) primer [role=listbox] visible
    """
    try:
        lab = page.locator("text=Selecciona la función").first
        if lab and lab.count() > 0:
            return _nearest_block(lab)
    except Exception:
        pass
    try:
        btn = page.locator("button:has-text('Ver entradas'), a:has-text('Ver entradas')").first
        if btn and btn.count() > 0:
            return _nearest_block(btn)
    except Exception:
        pass
    try:
        lb = page.locator("[role='listbox']").first
        if lb and lb.count() > 0:
            return _nearest_block(lb)
    except Exception:
        pass
    return None

def _gather_dates_in_region(region):
    """
    Extrae fechas SOLO dentro de 'region'.
    No mira el resto de la página (evita basura de banners/footer).
    """
    if not region:
        return []
    fechas, seen = [], set()
    # 1) items tipo opción dentro de region
    try:
        items = region.locator("[role='option'], option, .MuiMenuItem-root, li, a, button, div")
        n = min(items.count(), 200)
        for i in range(n):
            it = items.nth(i)
            try:
                txt = (it.inner_text(timeout=250) or "").strip()
            except Exception:
                txt = ""
            low = txt.lower()
            if not txt or "agotado" in low or "sold out" in low or "sin disponibilidad" in low:
                continue
            for f in extract_dates_only(txt):
                if f not in seen:
                    seen.add(f); fechas.append(f)
    except Exception:
        pass
    # 2) respaldo: texto bruto del bloque
    try:
        raw = (region.inner_text(timeout=400) or "").strip()
        for f in extract_dates_only(raw):
            if f not in seen:
                seen.add(f); fechas.append(f)
    except Exception:
        pass
    return filter_and_sort_dates(fechas)

# ==============================
# Interacciones
# ==============================
def _open_dropdown_if_any(page):
    for trig in FUNC_TRIGGERS:
        try:
            loc = page.locator(trig).first
            if loc and loc.count() > 0:
                loc.click(timeout=1500, force=True)
                page.wait_for_timeout(250)
        except Exception:
            continue
    try:
        page.wait_for_selector(".MuiPopover-root, .MuiMenu-paper, [role='listbox']", timeout=1200)
    except Exception:
        pass

def _select_preferred_market_if_present(page):
    target = (PREFERRED_MARKET or "").strip().lower()
    if not target: return
    candidates = [
        "[role='tablist'] [role='tab']",
        ".tabs .tab",
        ".country-tabs *",
        "[data-testid*='country'] *",
        ".MuiTabs-root button, .MuiTab-root",
        ".filter-countries *"
    ]
    for sel in candidates:
        try:
            items = page.locator(sel); n = items.count()
            if n == 0: continue
            for i in range(min(n, 60)):
                it = items.nth(i)
                try: txt = (it.inner_text(timeout=200) or "").strip().lower()
                except Exception: txt = ""
                if not txt: continue
                if txt in PAISES_COMUNES and target in txt:
                    it.click(timeout=1500, force=True)
                    page.wait_for_load_state("networkidle"); page.wait_for_timeout(250)
                    return
        except Exception:
            continue

def _click_cta_buy_get_page(page):
    # si abre popup, seguimos ahí; si no, misma page
    for sel in CTA_BUY_SELECTORS:
        try:
            el = page.locator(sel).first
            if el and el.count() > 0:
                try:
                    with page.expect_popup() as pinfo:
                        el.click(timeout=1500, force=True)
                    popup = pinfo.value
                    try: popup.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception: pass
                    return popup
                except Exception:
                    el.click(timeout=1500, force=True)
                    try: page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except Exception: pass
                    return page
        except Exception:
            continue
    return page

# --- NUEVO: buy solo dentro de la región ---
def region_has_buy(region) -> bool:
    """Detecta CTA de compra SOLO dentro de la región de funciones."""
    if not region:
        return False
    try:
        sel = (
            "a:has-text('Comprar'), a:has-text('Comprar entradas'), "
            "button:has-text('Comprar'), button:has-text('Comprar entradas'), "
            "[data-testid*='comprar'], [data-testid*='buy'], [href*='comprar']"
        )
        btns = region.locator(sel)
        return btns.count() > 0
    except Exception:
        return False

# ==============================
# Core: chequear una URL
# ==============================
def check_url(url: str, page) -> tuple[list[str], str|None, str]:
    fechas, title = [], None
    status_hint = "UNKNOWN"
    try:
        page.goto(url, timeout=60000)
        page.wait_for_load_state("networkidle", timeout=15000)

        title = extract_title(page)

        _select_preferred_market_if_present(page)
        _open_dropdown_if_any(page)

        # 1) Región de funciones en la página original
        region = _find_functions_region(page)
        fechas = _gather_dates_in_region(region)

        soldout = page_has_soldout(page)
        buy_in_region = region_has_buy(region)

        # 2) Si no hay fechas:
        if not fechas:
            if soldout:
                status_hint = "SOLDOUT"   # prioridad al agotado
            elif buy_in_region:
                # Intentamos CTA SOLO si hay botón en la región
                dest = _click_cta_buy_get_page(page)
                try: dest.wait_for_load_state("networkidle", timeout=6000)
                except Exception: pass
                try:
                    dest.mouse.wheel(0, 400); dest.wait_for_timeout(120)
                    dest.mouse.wheel(0, -400); dest.wait_for_timeout(120)
                except Exception:
                    pass
                region2 = _find_functions_region(dest)
                fechas = _gather_dates_in_region(region2)
                if not fechas:
                    status_hint = "AVAILABLE"  # disponible pero sin fechas visibles
            else:
                status_hint = "UNKNOWN"

    except Exception as e:
        print(f"⚠️ Error al procesar {url}: {e}")

    return fechas, title, status_hint

# ==============================
# DEBUG (con hints)
# ==============================
def debug_show_by_index(idx: int):
    url = URLS[idx - 1]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=15000)
            title = extract_title(page) or prettify_from_slug(url)
            has_buy_global = page_has_buy(page)
            has_sold_global = page_has_soldout(page)

            _select_preferred_market_if_present(page)
            _open_dropdown_if_any(page)

            region = _find_functions_region(page)
            pre = _gather_dates_in_region(region)
            buy_in_region = region_has_buy(region)

            dest = _click_cta_buy_get_page(page)
            try: dest.wait_for_load_state("networkidle", timeout=6000)
            except Exception: pass
            region2 = _find_functions_region(dest)
            post = _gather_dates_in_region(region2)

            decision_hint = ("SOLDOUT" if (has_sold_global and not pre)
                             else ("AVAILABLE" if buy_in_region else "UNKNOWN"))

            parts = [
                f"🧪 DEBUG — {title}",
                f"URL idx {idx}",
                f"buy_detected_global={has_buy_global}, soldout_detected_global={has_sold_global}",
                f"buy_in_region={'yes' if buy_in_region else 'no'}",
                f"popup_opened={'yes' if dest is not page else 'no'}",
                f"decision_hint={decision_hint}",
                "pre: " + (", ".join(pre) if pre else "-"),
                "post: " + (", ".join(post) if post else "-"),
            ]
            tg_send("\n".join(parts) + f"\n{SIGN}", force=True)
        except Exception as e:
            tg_send(f"🧪 DEBUG ERROR idx {idx}: {e}\n{SIGN}", force=True)
        finally:
            try: browser.close()
            except Exception: pass

# ==============================
# Telegram
# ==============================
def telegram_polling():
    if not (BOT_TOKEN and CHAT_ID):
        print("ℹ️ Telegram polling desactivado (faltan credenciales).")
        return

    offset = None
    api = f"https://api.telegram.org/bot{BOT_TOKEN}"
    print("🛰️ Telegram polling iniciado.")

    while True:
        try:
            params = {"timeout": 50}
            if offset is not None:
                params["offset"] = offset

            r = requests.get(f"{api}/getUpdates", params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                time.sleep(3)
                continue

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1

                msg = upd.get("message") or {}
                chat = msg.get("chat", {})
                text = (msg.get("text") or "").strip()
                chat_id = str(chat.get("id") or "")
                if not text or chat_id != str(CHAT_ID):
                    continue

                tlow = text.lower()

                if tlow.startswith("/shows"):
                    tg_send(fmt_shows_indexed(), force=True)

                elif tlow.startswith("/status"):
                    m = re.match(r"^/status\s+(\d+)\s*$", tlow)
                    if m:
                        idx = int(m.group(1))
                        if 1 <= idx <= len(URLS):
                            url = URLS[idx - 1]
                            info = LAST_RESULTS.get(
                                url,
                                {"status": "UNKNOWN", "detail": None, "ts": "", "title": None},
                            )
                            tg_send(fmt_status_entry(url, info, include_url=False) + f"\n{SIGN}", force=True)
                        else:
                            tg_send(f"Índice fuera de rango (1–{len(URLS)}).{SIGN}", force=True)
                    else:
                        snap = LAST_RESULTS.copy()
                        tg_send(fmt_status_snapshot(snap), force=True)

                elif tlow.startswith("/debug"):
                    m = re.match(r"^/debug\s+(\d+)\s*$", tlow)
                    if m:
                        idx = int(m.group(1))
                        if 1 <= idx <= len(URLS):
                            tg_send(f"⏳ Corriendo debug del show #{idx}…{SIGN}", force=True)
                            threading.Thread(target=debug_show_by_index, args=(idx,), daemon=True).start()
                        else:
                            tg_send(f"Índice fuera de rango (1–{len(URLS)}).{SIGN}", force=True)
                    else:
                        tg_send(f"Usá: /debug N (ej: /debug 2){SIGN}", force=True)

                elif tlow.startswith("/sectores"):
                    m = re.match(r"^/sectores\s+(\d+)\s*$", tlow)
                    if m:
                        idx = int(m.group(1))
                        if 1 <= idx <= len(URLS):
                            tg_send(f"⏳ Buscando sectores por fecha del show #{idx}…{SIGN}", force=True)
                            threading.Thread(
                                target=cmd_list_sectors_for_show_index,
                                args=(idx,),
                                daemon=True
                            ).start()
                        else:
                            tg_send(f"Índice fuera de rango (1–{len(URLS)}).{SIGN}", force=True)
                    else:
                        tg_send(f"Usá: /sectores N (ej: /sectores 2){SIGN}", force=True)

        except Exception as e:
            print("⚠️ Polling error:", e)
            time.sleep(5)

# ==============================
# Loop principal
# ==============================
def run_monitor():
    tg_send(f"🔎 Radar levantado (URLs: {len(URLS)}){SIGN}", force=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        while True:
            for url in URLS:
                try:
                    fechas, title, status_hint = check_url(url, page)
                    ts = now_local().strftime("%Y-%m-%d %H:%M:%S")
                    prev_status = LAST_RESULTS.get(url, {}).get("status", "UNKNOWN")

                    if fechas:
                        det = ", ".join(fechas)
                        if prev_status != "AVAILABLE":
                            head = title or "Show"
                            tg_send(f"✅ ¡Entradas disponibles!\n{head}\nFechas: {det}\n{SIGN}")
                        LAST_RESULTS[url] = {"status": "AVAILABLE", "detail": det, "ts": ts, "title": title}
                    else:
                        status = "SOLDOUT" if status_hint == "SOLDOUT" \
                                 else ("AVAILABLE" if status_hint == "AVAILABLE" else "UNKNOWN")
                        detail = None
                        LAST_RESULTS[url] = {"status": status, "detail": detail, "ts": ts, "title": title}
                        print(f"{'⛔' if status=='SOLDOUT' else ('✅' if status=='AVAILABLE' else '❓')} {title or url} — {ts}")
                        if status == "AVAILABLE" and prev_status != "AVAILABLE":
                            head = title or "Show"
                            tg_send(f"✅ ¡Entradas disponibles!\n{head}\nFechas: (sin fecha visible)\n{SIGN}")
                except Exception as e:
                    ts = now_local().strftime("%Y-%m-%d %H:%M:%S")
                    LAST_RESULTS[url] = {"status": "UNKNOWN", "detail": str(e), "ts": ts, "title": LAST_RESULTS.get(url, {}).get("title")}
                    print(f"💥 Error en {url}: {e}")
            time.sleep(CHECK_EVERY)

# ==============================
# Arranque
# ==============================
if __name__ == "__main__":
    t = threading.Thread(target=telegram_polling, daemon=True)
    t.start()
    run_monitor()
