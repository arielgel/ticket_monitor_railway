import os
import re
import time
import threading
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ==============================
# Configuración
# ==============================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # string
URLS = [u.strip() for u in os.getenv("MONITORED_URLS", "").split(",") if u.strip()]
CHECK_EVERY = int(os.getenv("CHECK_EVERY_SECONDS", "300"))  # segundos
TZ_NAME = os.getenv("TIMEZONE", "America/Argentina/Buenos_Aires")
PREFERRED_MARKET = os.getenv("PREFERRED_MARKET", "Argentina")

SIGN = " — Roberto"

# ==============================
# Estado global (cache)
# ==============================
LAST_RESULTS = {u: {"status": "UNKNOWN", "detail": None, "ts": "", "title": None} for u in URLS}

# ==============================
# Helpers
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
# Fechas / países
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

def _looks_like_country(s: str) -> bool:
    t = s.strip().lower().replace("ó","o")
    return t in PAISES_COMUNES

# ==============================
# Selectores UI
# ==============================
# Triggers para abrir dropdown/menú de fechas
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

# Botones/links CTA de compra que suelen abrir el modal con fechas
CTA_BUY_SELECTORS = [
    "a:has-text('Comprar')",
    "a:has-text('Comprar entradas')",
    "button:has-text('Comprar')",
    "button:has-text('Comprar entradas')",
    "[data-testid*='comprar']",
    "[data-testid*='buy']",
    "[href*='comprar']",
]

# Donde MUI suelta las opciones (portales y DOM)
PORTAL_SELECTORS = [
    ".MuiPopover-root .MuiMenu-list[role='listbox'] li[role='option']",
    ".MuiPopover-root [role='listbox'] li[role='option']",
    ".MuiMenu-paper [role='listbox'] li[role='option']",
    ".MuiPaper-root [role='listbox'] li[role='option']",
    ".MuiPopover-root li.MuiMenuItem-root",
    ".MuiMenu-paper li.MuiMenuItem-root",
]
LISTBOX_SELECTORS = [
    "[role='listbox'] li[role='option']",
    "select option",
    ".MuiList-root li[role='option']",
    ".aa-event-dates [role='option']",
    ".event-functions [role='option']",
    ".MuiList-root li.MuiMenuItem-root",
]

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

def _click_cta_buy_if_present(page) -> bool:
    """Intenta clickear CTA de compra para que aparezca el modal de fechas. Devuelve True si clickeó algo."""
    clicked = False
    for sel in CTA_BUY_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn and btn.count() > 0:
                btn.click(timeout=1500, force=True)
                page.wait_for_timeout(350)
                clicked = True
                # esperar popover/modal
                try:
                    page.wait_for_selector(".MuiPopover-root, .MuiMenu-paper, [role='dialog'], [role='listbox']", timeout=2000)
                except Exception:
                    pass
                break
        except Exception:
            continue
    return clicked

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
            items = page.locator(sel)
            n = items.count()
            if n == 0: continue
            for i in range(min(n, 60)):
                it = items.nth(i)
                try:
                    txt = (it.inner_text(timeout=200) or "").strip().lower()
                except Exception:
                    txt = ""
                if not txt: continue
                if _looks_like_country(txt) and target in txt:
                    it.click(timeout=1500, force=True)
                    page.wait_for_load_state("networkidle"); page.wait_for_timeout(250)
                    return
        except Exception:
            continue

def _collect_options_from(page, selectors) -> list[str]:
    out = []
    try:
        items = page.locator(", ".join(selectors))
        if items and items.count() > 0:
            for i in range(min(items.count(), 120)):
                it = items.nth(i)
                try: txt = (it.inner_text(timeout=250) or "").strip()
                except Exception: txt = ""
                if txt: out.append(txt)
    except Exception:
        pass
    return out

def _list_functions_generic(page) -> list[tuple[str, str]]:
    """
    Devuelve lista de (label, via) intentando en:
      - Portales MUI
      - Listbox DOM
      - <select><option>
    """
    for selectors, via in ((PORTAL_SELECTORS, "portal"), (LISTBOX_SELECTORS, "list")):
        labels = _collect_options_from(page, selectors)
        if labels:
            return [(lbl, via) for lbl in labels]
    return []

# ==============================
# Chequeo de una URL
# ==============================
def check_url(url: str, page) -> tuple[list[str], str|None, str]:
    fechas, title = [], None
    status_hint = "UNKNOWN"
    try:
        page.goto(url, timeout=60000)
        page.wait_for_load_state("networkidle", timeout=15000)

        title = extract_title(page)

        # pistas globales antes de abrir menú
        if page_has_buy(page):
            status_hint = "AVAILABLE"
        elif page_has_soldout(page):
            status_hint = "SOLDOUT"

        _select_preferred_market_if_present(page)

        # 1) Intentar abrir dropdown normal
        _open_dropdown_if_any(page)
        # 2) Buscar opciones
        funcs = _list_functions_generic(page)

        # 3) Si no hay, probar con CTA de compra -> abre modal con fechas
        if not funcs:
            if _click_cta_buy_if_present(page):
                funcs = _list_functions_generic(page)

        # Scroll suavecitos para lazy render
        try:
            page.mouse.wheel(0, 400); page.wait_for_timeout(120)
            page.mouse.wheel(0, -400); page.wait_for_timeout(120)
        except Exception:
            pass

        def is_soldout_label(s: str) -> bool:
            s = s.lower()
            return any(k in s for k in ["agotado", "sold out", "sin disponibilidad", "sem disponibilidade"])

        if funcs:
            for lbl, via in funcs:
                if not lbl: continue
                if _looks_like_country(lbl):  continue
                if is_soldout_label(lbl):     continue
                for f in extract_dates_only(lbl):
                    if f not in fechas:
                        fechas.append(f)
        else:
            # Fallback: si hay botón de compra pero no fechas visibles, marcamos sin fecha
            if page_has_buy(page):
                fechas.append("(sin fecha)")
                status_hint = "AVAILABLE"

    except Exception as e:
        print(f"⚠️ Error al procesar {url}: {e}")

    return fechas, title, status_hint

# ==============================
# DEBUG helpers
# ==============================
def _collect_options_debug(page):
    buckets = {}
    buckets["portal_listbox"] = _collect_options_from(page, PORTAL_SELECTORS)[:15]
    buckets["dom_listbox"]    = _collect_options_from(page, ["[role='listbox'] li[role='option']"])[:15]
    buckets["select_option"]  = _collect_options_from(page, ["select option"])[:15]
    return buckets

def _collect_options_debug_after_cta(page):
    buckets = {}
    _click_cta_buy_if_present(page)
    time.sleep(0.2)
    buckets["afterCTA_portal"] = _collect_options_from(page, PORTAL_SELECTORS)[:15]
    buckets["afterCTA_dom"]    = _collect_options_from(page, ["[role='listbox'] li[role='option']"])[:15]
    buckets["afterCTA_select"] = _collect_options_from(page, ["select option"])[:15]
    return buckets

def debug_show_by_index(idx: int):
    url = URLS[idx - 1]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=15000)
            title = extract_title(page) or prettify_from_slug(url)
            has_buy = page_has_buy(page)
            has_sold = page_has_soldout(page)

            _select_preferred_market_if_present(page)
            _open_dropdown_if_any(page)
            try:
                page.mouse.wheel(0, 500); page.wait_for_timeout(150)
                page.mouse.wheel(0, -500); page.wait_for_timeout(150)
            except Exception:
                pass

            buckets1 = _collect_options_debug(page)
            buckets2 = _collect_options_debug_after_cta(page)

            def _flat(name, vals):
                if vals:
                    return f"{name}: " + "; ".join([v.replace("\n"," ").strip() for v in vals])
                return None

            parts = [
                f"🧪 DEBUG — {title}",
                f"URL idx {idx}",
                f"buy_detected={has_buy}, soldout_detected={has_sold}",
            ]
            for k,v in buckets1.items():
                s = _flat(k, v)
                if s: parts.append(s)
            for k,v in buckets2.items():
                s = _flat(k, v)
                if s: parts.append(s)
            if len(parts) == 3:
                parts.append("no options found in known selectors (antes/after CTA)")

            tg_send("\n".join(parts) + f"\n{SIGN}", force=True)
        except Exception as e:
            tg_send(f"🧪 DEBUG ERROR idx {idx}: {e}\n{SIGN}", force=True)
        finally:
            try: browser.close()
            except Exception: pass

# ==============================
# Telegram polling
# ==============================
def telegram_polling():
    if not (BOT_TOKEN and CHAT_ID):
        print("ℹ️ Telegram polling desactivado (faltan credenciales)."); return

    offset = None
    api = f"https://api.telegram.org/bot{BOT_TOKEN}"
    print("🛰️ Telegram polling iniciado.")
    while True:
        try:
            params = {"timeout": 50}
            if offset is not None: params["offset"] = offset
            r = requests.get(f"{api}/getUpdates", params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                time.sleep(3); continue

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat = msg.get("chat", {})
                text = (msg.get("text") or "").strip()
                chat_id = str(chat.get("id") or "")
                if not text or chat_id != str(CHAT_ID): continue

                tlow = text.lower()
                if tlow.startswith("/shows"):
                    tg_send(fmt_shows_indexed(), force=True)

                elif tlow.startswith("/status"):
                    m = re.match(r"^/status\s+(\d+)\s*$", tlow)
                    if m:
                        idx = int(m.group(1))
                        if 1 <= idx <= len(URLS):
                            url = URLS[idx - 1]
                            info = LAST_RESULTS.get(url, {"status": "UNKNOWN", "detail": None, "ts": "", "title": None})
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

        except Exception as e:
            print("⚠️ Polling error:", e)
            time.sleep(5)

# ==============================
# Main loop
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
                        status = "SOLDOUT" if (status_hint == "SOLDOUT" and not page_has_buy(page)) \
                                 else ("AVAILABLE" if status_hint == "AVAILABLE" else "UNKNOWN")
                        detail = "(sin fecha)" if status == "AVAILABLE" else None
                        LAST_RESULTS[url] = {"status": status, "detail": detail, "ts": ts, "title": title}
                        print(f"{'⛔' if status=='SOLDOUT' else ('✅' if status=='AVAILABLE' else '❓')} {title or url} — {ts}")
                        if status == "AVAILABLE" and prev_status != "AVAILABLE":
                            head = title or "Show"
                            tg_send(f"✅ ¡Entradas disponibles!\n{head}\nFechas: {detail or '(sin fecha)'}\n{SIGN}")

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
import os
import re
import time
import threading
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ==============================
# Configuración
# ==============================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # string
URLS = [u.strip() for u in os.getenv("MONITORED_URLS", "").split(",") if u.strip()]
CHECK_EVERY = int(os.getenv("CHECK_EVERY_SECONDS", "300"))  # segundos
TZ_NAME = os.getenv("TIMEZONE", "America/Argentina/Buenos_Aires")
PREFERRED_MARKET = os.getenv("PREFERRED_MARKET", "Argentina")

SIGN = " — Roberto"

# ==============================
# Estado global (cache)
# ==============================
LAST_RESULTS = {u: {"status": "UNKNOWN", "detail": None, "ts": "", "title": None} for u in URLS}

# ==============================
# Helpers
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
# Fechas / países
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

def _looks_like_country(s: str) -> bool:
    t = s.strip().lower().replace("ó","o")
    return t in PAISES_COMUNES

# ==============================
# Selectores UI
# ==============================
# Triggers para abrir dropdown/menú de fechas
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

# Botones/links CTA de compra que suelen abrir el modal con fechas
CTA_BUY_SELECTORS = [
    "a:has-text('Comprar')",
    "a:has-text('Comprar entradas')",
    "button:has-text('Comprar')",
    "button:has-text('Comprar entradas')",
    "[data-testid*='comprar']",
    "[data-testid*='buy']",
    "[href*='comprar']",
]

# Donde MUI suelta las opciones (portales y DOM)
PORTAL_SELECTORS = [
    ".MuiPopover-root .MuiMenu-list[role='listbox'] li[role='option']",
    ".MuiPopover-root [role='listbox'] li[role='option']",
    ".MuiMenu-paper [role='listbox'] li[role='option']",
    ".MuiPaper-root [role='listbox'] li[role='option']",
    ".MuiPopover-root li.MuiMenuItem-root",
    ".MuiMenu-paper li.MuiMenuItem-root",
]
LISTBOX_SELECTORS = [
    "[role='listbox'] li[role='option']",
    "select option",
    ".MuiList-root li[role='option']",
    ".aa-event-dates [role='option']",
    ".event-functions [role='option']",
    ".MuiList-root li.MuiMenuItem-root",
]

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

def _click_cta_buy_if_present(page) -> bool:
    """Intenta clickear CTA de compra para que aparezca el modal de fechas. Devuelve True si clickeó algo."""
    clicked = False
    for sel in CTA_BUY_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn and btn.count() > 0:
                btn.click(timeout=1500, force=True)
                page.wait_for_timeout(350)
                clicked = True
                # esperar popover/modal
                try:
                    page.wait_for_selector(".MuiPopover-root, .MuiMenu-paper, [role='dialog'], [role='listbox']", timeout=2000)
                except Exception:
                    pass
                break
        except Exception:
            continue
    return clicked

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
            items = page.locator(sel)
            n = items.count()
            if n == 0: continue
            for i in range(min(n, 60)):
                it = items.nth(i)
                try:
                    txt = (it.inner_text(timeout=200) or "").strip().lower()
                except Exception:
                    txt = ""
                if not txt: continue
                if _looks_like_country(txt) and target in txt:
                    it.click(timeout=1500, force=True)
                    page.wait_for_load_state("networkidle"); page.wait_for_timeout(250)
                    return
        except Exception:
            continue

def _collect_options_from(page, selectors) -> list[str]:
    out = []
    try:
        items = page.locator(", ".join(selectors))
        if items and items.count() > 0:
            for i in range(min(items.count(), 120)):
                it = items.nth(i)
                try: txt = (it.inner_text(timeout=250) or "").strip()
                except Exception: txt = ""
                if txt: out.append(txt)
    except Exception:
        pass
    return out

def _list_functions_generic(page) -> list[tuple[str, str]]:
    """
    Devuelve lista de (label, via) intentando en:
      - Portales MUI
      - Listbox DOM
      - <select><option>
    """
    for selectors, via in ((PORTAL_SELECTORS, "portal"), (LISTBOX_SELECTORS, "list")):
        labels = _collect_options_from(page, selectors)
        if labels:
            return [(lbl, via) for lbl in labels]
    return []

# ==============================
# Chequeo de una URL
# ==============================
def check_url(url: str, page) -> tuple[list[str], str|None, str]:
    fechas, title = [], None
    status_hint = "UNKNOWN"
    try:
        page.goto(url, timeout=60000)
        page.wait_for_load_state("networkidle", timeout=15000)

        title = extract_title(page)

        # pistas globales antes de abrir menú
        if page_has_buy(page):
            status_hint = "AVAILABLE"
        elif page_has_soldout(page):
            status_hint = "SOLDOUT"

        _select_preferred_market_if_present(page)

        # 1) Intentar abrir dropdown normal
        _open_dropdown_if_any(page)
        # 2) Buscar opciones
        funcs = _list_functions_generic(page)

        # 3) Si no hay, probar con CTA de compra -> abre modal con fechas
        if not funcs:
            if _click_cta_buy_if_present(page):
                funcs = _list_functions_generic(page)

        # Scroll suavecitos para lazy render
        try:
            page.mouse.wheel(0, 400); page.wait_for_timeout(120)
            page.mouse.wheel(0, -400); page.wait_for_timeout(120)
        except Exception:
            pass

        def is_soldout_label(s: str) -> bool:
            s = s.lower()
            return any(k in s for k in ["agotado", "sold out", "sin disponibilidad", "sem disponibilidade"])

        if funcs:
            for lbl, via in funcs:
                if not lbl: continue
                if _looks_like_country(lbl):  continue
                if is_soldout_label(lbl):     continue
                for f in extract_dates_only(lbl):
                    if f not in fechas:
                        fechas.append(f)
        else:
            # Fallback: si hay botón de compra pero no fechas visibles, marcamos sin fecha
            if page_has_buy(page):
                fechas.append("(sin fecha)")
                status_hint = "AVAILABLE"

    except Exception as e:
        print(f"⚠️ Error al procesar {url}: {e}")

    return fechas, title, status_hint

# ==============================
# DEBUG helpers
# ==============================
def _collect_options_debug(page):
    buckets = {}
    buckets["portal_listbox"] = _collect_options_from(page, PORTAL_SELECTORS)[:15]
    buckets["dom_listbox"]    = _collect_options_from(page, ["[role='listbox'] li[role='option']"])[:15]
    buckets["select_option"]  = _collect_options_from(page, ["select option"])[:15]
    return buckets

def _collect_options_debug_after_cta(page):
    buckets = {}
    _click_cta_buy_if_present(page)
    time.sleep(0.2)
    buckets["afterCTA_portal"] = _collect_options_from(page, PORTAL_SELECTORS)[:15]
    buckets["afterCTA_dom"]    = _collect_options_from(page, ["[role='listbox'] li[role='option']"])[:15]
    buckets["afterCTA_select"] = _collect_options_from(page, ["select option"])[:15]
    return buckets

def debug_show_by_index(idx: int):
    url = URLS[idx - 1]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=15000)
            title = extract_title(page) or prettify_from_slug(url)
            has_buy = page_has_buy(page)
            has_sold = page_has_soldout(page)

            _select_preferred_market_if_present(page)
            _open_dropdown_if_any(page)
            try:
                page.mouse.wheel(0, 500); page.wait_for_timeout(150)
                page.mouse.wheel(0, -500); page.wait_for_timeout(150)
            except Exception:
                pass

            buckets1 = _collect_options_debug(page)
            buckets2 = _collect_options_debug_after_cta(page)

            def _flat(name, vals):
                if vals:
                    return f"{name}: " + "; ".join([v.replace("\n"," ").strip() for v in vals])
                return None

            parts = [
                f"🧪 DEBUG — {title}",
                f"URL idx {idx}",
                f"buy_detected={has_buy}, soldout_detected={has_sold}",
            ]
            for k,v in buckets1.items():
                s = _flat(k, v)
                if s: parts.append(s)
            for k,v in buckets2.items():
                s = _flat(k, v)
                if s: parts.append(s)
            if len(parts) == 3:
                parts.append("no options found in known selectors (antes/after CTA)")

            tg_send("\n".join(parts) + f"\n{SIGN}", force=True)
        except Exception as e:
            tg_send(f"🧪 DEBUG ERROR idx {idx}: {e}\n{SIGN}", force=True)
        finally:
            try: browser.close()
            except Exception: pass

# ==============================
# Telegram polling
# ==============================
def telegram_polling():
    if not (BOT_TOKEN and CHAT_ID):
        print("ℹ️ Telegram polling desactivado (faltan credenciales)."); return

    offset = None
    api = f"https://api.telegram.org/bot{BOT_TOKEN}"
    print("🛰️ Telegram polling iniciado.")
    while True:
        try:
            params = {"timeout": 50}
            if offset is not None: params["offset"] = offset
            r = requests.get(f"{api}/getUpdates", params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                time.sleep(3); continue

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat = msg.get("chat", {})
                text = (msg.get("text") or "").strip()
                chat_id = str(chat.get("id") or "")
                if not text or chat_id != str(CHAT_ID): continue

                tlow = text.lower()
                if tlow.startswith("/shows"):
                    tg_send(fmt_shows_indexed(), force=True)

                elif tlow.startswith("/status"):
                    m = re.match(r"^/status\s+(\d+)\s*$", tlow)
                    if m:
                        idx = int(m.group(1))
                        if 1 <= idx <= len(URLS):
                            url = URLS[idx - 1]
                            info = LAST_RESULTS.get(url, {"status": "UNKNOWN", "detail": None, "ts": "", "title": None})
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

        except Exception as e:
            print("⚠️ Polling error:", e)
            time.sleep(5)

# ==============================
# Main loop
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
                        status = "SOLDOUT" if (status_hint == "SOLDOUT" and not page_has_buy(page)) \
                                 else ("AVAILABLE" if status_hint == "AVAILABLE" else "UNKNOWN")
                        detail = "(sin fecha)" if status == "AVAILABLE" else None
                        LAST_RESULTS[url] = {"status": status, "detail": detail, "ts": ts, "title": title}
                        print(f"{'⛔' if status=='SOLDOUT' else ('✅' if status=='AVAILABLE' else '❓')} {title or url} — {ts}")
                        if status == "AVAILABLE" and prev_status != "AVAILABLE":
                            head = title or "Show"
                            tg_send(f"✅ ¡Entradas disponibles!\n{head}\nFechas: {detail or '(sin fecha)'}\n{SIGN}")

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
