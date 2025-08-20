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
PREFERRED_MARKET = os.getenv("PREFERRED_MARKET", "Argentina")  # para tabs/menús de país

SIGN = " — Roberto"

# ==============================
# Estado global (cache)
# ==============================
# LAST_RESULTS[url] = {"status": "AVAILABLE"/"SOLDOUT"/"UNKNOWN", "detail": str|None, "ts": "...", "title": str|None}
LAST_RESULTS = {u: {"status": "UNKNOWN", "detail": None, "ts": "", "title": None} for u in URLS}

# ==============================
# Helpers
# ==============================
def now_local():
    return datetime.now(ZoneInfo(TZ_NAME))

def within_quiet_hours():
    h = now_local().hour
    return 0 <= h < 9  # silencio 00:00–09:00

def tg_send(text: str, force: bool = False):
    """Envía mensaje por Telegram. force=True ignora el silencio."""
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

def extract_title(page):
    """Obtiene un título amigable (title o og:title)."""
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
# Heurísticas fecha/hora vs. países
# ==============================
MESES_ES = ["enero","febrero","marzo","abril","mayo","junio","julio","agosto","septiembre","octubre","noviembre","diciembre"]
DIAS_ES  = ["lunes","martes","miércoles","miercoles","jueves","viernes","sábado","sabado","domingo"]
PAISES_COMUNES = {"argentina","brasil","colombia","chile","uruguay","perú","peru","paraguay","bolivia","mexico","méxico","portugal","españa","otros","other","latam"}

# dd-mm(-yyyy) o dd/mm(/yyyy)
RE_NUMERIC_DATE = re.compile(r"\b(\d{1,2})[-/](\d{1,2})(?:[-/](\d{2,4}))?\b")
# dd de <mes> (de) yyyy | dd <mes> yyyy
RE_MONTH_NAME   = re.compile(
    r"\b(\d{1,2})\s+(?:de\s+)?(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)(?:\s+de)?\s+(\d{4})\b",
    re.IGNORECASE
)

def _month_to_num(m: str) -> int:
    m = m.strip().lower()
    for i, name in enumerate(MESES_ES, start=1):
        if name == m: return i
    return 0

def extract_dates_only(text: str) -> list[str]:
    """
    Extrae SOLO fechas (sin horas) del texto y las normaliza como dd/mm/yyyy o dd/mm.
    Mantiene orden de aparición y deduplica.
    """
    out = []
    seen = set()

    # 1) Fechas numéricas
    for d, m, y in RE_NUMERIC_DATE.findall(text):
        dd = int(d); mm = int(m)
        if not (1 <= dd <= 31 and 1 <= mm <= 12): continue
        if y:
            yy = int(y); yy = (2000 + yy) if len(y) == 2 else yy
            s = f"{dd:02d}/{mm:02d}/{yy:04d}"
        else:
            s = f"{dd:02d}/{mm:02d}"
        if s not in seen:
            seen.add(s); out.append(s)

    # 2) Fechas con nombre de mes
    for d, mes, y in RE_MONTH_NAME.findall(text):
        dd = int(d); mm = _month_to_num(mes)
        if not (1 <= dd <= 31 and 1 <= mm <= 12): continue
        yy = int(y)
        s = f"{dd:02d}/{mm:02d}/{yy:04d}"
        if s not in seen:
            seen.add(s); out.append(s)

    return out

def _looks_like_country(s: str) -> bool:
    t = s.strip().lower().replace("ó","o")
    return t in PAISES_COMUNES

# ==============================
# Dropdown/listbox (abrir y enumerar funciones)
# ==============================
FUNC_TRIGGERS = [
    "button[aria-haspopup='listbox']",
    "[role='combobox']",
    "[data-testid*='select']",
    ".MuiSelect-select",
    ".aa-event-dates",
    ".event-functions",
]

LISTBOX_ROOTS = [
    "[role='listbox']",       # PRIORIDAD: listbox Material UI
    "select",                 # fallback
    ".MuiList-root",
    ".aa-event-dates",
    ".event-functions",
]

def _open_dropdown_if_any(page):
    """Intenta abrir el dropdown para que aparezcan opciones."""
    for trig in FUNC_TRIGGERS:
        try:
            loc = page.locator(trig).first
            if loc and loc.count() > 0:
                loc.click(timeout=1500, force=True)
                page.wait_for_timeout(250)
        except Exception:
            continue

def _select_preferred_market_if_present(page):
    """Si hay tabs/lista de países, clickea el preferido (ej. Argentina) antes de enumerar funciones."""
    target = (PREFERRED_MARKET or "").strip().lower()
    if not target: return
    candidates = [
        "[role='tablist'] [role='tab']",
        ".tabs .tab",
        ".country-tabs *",
        "[data-testid*='country'] *",
        "[role='listbox'] [role='option']",
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

def _list_functions_generic(page):
    """
    Devuelve lista de (label, element, via) para funciones:
      via='list'   si viene de listbox/listas (PRIORIDAD)
      via='select' si viene de <select><option> (fallback)
    """
    # 1) Listbox Material UI
    try:
        container = page.locator("[role='listbox']").first
        if container and container.count() > 0:
            items = container.locator("li[role='option']")
            out = []
            for i in range(min(items.count(), 120)):
                it = items.nth(i)
                try:
                    txt = (it.inner_text(timeout=250) or "").strip()
                except Exception:
                    txt = ""
                if not txt: continue
                out.append((txt, it, "list"))
            if out: return out
    except Exception:
        pass

    # 2) <select><option>
    try:
        sel = page.locator("select").first
        if sel and sel.count() > 0:
            opts = sel.locator("option")
            out = []
            for i in range(opts.count()):
                o = opts.nth(i)
                try:
                    lbl = (o.inner_text(timeout=200) or o.get_attribute("label") or "").strip()
                except Exception:
                    lbl = (o.get_attribute("label") or "").strip()
                if lbl:
                    out.append((lbl, o, "select"))
            if out: return out
    except Exception:
        pass

    # 3) Otros posibles contenedores (fallbacks suaves)
    for root in (".MuiList-root", ".aa-event-dates", ".event-functions"):
        try:
            container = page.locator(root).first
            if not container or container.count() == 0: continue
            items = container.locator("[role='option'], li, .item, .option, a, button")
            if items.count() == 0: continue
            out = []
            for i in range(min(items.count(), 120)):
                it = items.nth(i)
                try:
                    txt = (it.inner_text(timeout=250) or "").strip()
                except Exception:
                    txt = ""
                if not txt: continue
                out.append((txt, it, "list"))
            if out: return out
        except Exception:
            continue

    return []

# ==============================
# Chequeo de una URL
# ==============================
def check_url(url: str, page) -> tuple[list[str], str|None]:
    """
    Devuelve (lista_de_FECHAS_disponibles, titulo_del_show_o_None).
    Soporta:
      - listbox con <li role="option"> (prioridad)
      - <select><option>
      - show único (sin dropdown) -> intenta detectar compra (sin fecha)
    """
    fechas = []
    title = None

    try:
        page.goto(url, timeout=60000)
        page.wait_for_load_state("networkidle", timeout=15000)

        # Título
        title = extract_title(page)

        # Preferir mercado (ej. Argentina) si hubiera selector de países
        _select_preferred_market_if_present(page)

        # Abrir dropdown y enumerar opciones
        _open_dropdown_if_any(page)
        funcs = _list_functions_generic(page)

        def is_soldout_label(s: str) -> bool:
            s = s.lower()
            return any(k in s for k in ["agotado", "sold out", "sin disponibilidad", "sem disponibilidade"])

        if funcs:
            for lbl, el, via in funcs:
                if not lbl: continue
                if _looks_like_country(lbl):  # evitar países
                    continue
                if is_soldout_label(lbl):
                    continue
                # EXTRAER SOLO FECHAS
                found = extract_dates_only(lbl)
                for f in found:
                    if f not in fechas:
                        fechas.append(f)
        else:
            # No hay dropdown: show único → buscar botón compra (sin fecha)
            try:
                for btn in page.query_selector_all("button, a")[:120]:
                    t = (btn.inner_text() or "").lower()
                    if any(k in t for k in ["comprar", "entradas", "buy"]):
                        if "(sin fecha)" not in fechas:
                            fechas.append("(sin fecha)")
                        break
            except Exception:
                pass

    except Exception as e:
        print(f"⚠️ Error al procesar {url}: {e}")

    return fechas, title

# ==============================
# Telegram polling (/status [n], /shows)
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

        except Exception as e:
            print("⚠️ Polling error:", e)
            time.sleep(5)

# ==============================
# Main loop del monitor
# ==============================
def run_monitor():
    tg_send(f"🔎 Radar levantado (URLs: {len(URLS)}){SIGN}", force=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        while True:
            for url in URLS:
                try:
                    fechas, title = check_url(url, page)
                    ts = now_local().strftime("%Y-%m-%d %H:%M:%S")

                    prev_status = LAST_RESULTS.get(url, {}).get("status", "UNKNOWN")
                    if fechas:
                        # Armar string de fechas (sin horas)
                        det = ", ".join(fechas)
                        if prev_status != "AVAILABLE":
                            head = title or "Show"
                            tg_send(f"✅ ¡Entradas disponibles!\n{head}\nFechas: {det}\n{SIGN}")
                        LAST_RESULTS[url] = {
                            "status": "AVAILABLE",
                            "detail": det,
                            "ts": ts,
                            "title": title
                        }
                    else:
                        LAST_RESULTS[url] = {
                            "status": "SOLDOUT",
                            "detail": None,
                            "ts": ts,
                            "title": title
                        }
                        print(f"❌ Nada en {title or url} — {ts}")

                except Exception as e:
                    ts = now_local().strftime("%Y-%m-%d %H:%M:%S")
                    LAST_RESULTS[url] = {
                        "status": "UNKNOWN",
                        "detail": str(e),
                        "ts": ts,
                        "title": LAST_RESULTS.get(url, {}).get("title")
                    }
                    print(f"💥 Error en {url}: {e}")

            time.sleep(CHECK_EVERY)

# ==============================
# Arranque
# ==============================
if __name__ == "__main__":
    t = threading.Thread(target=telegram_polling, daemon=True)
    t.start()
    run_monitor()
