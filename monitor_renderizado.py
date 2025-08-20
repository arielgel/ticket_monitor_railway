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
        print("⏸️ Silenciado:", text)
        return
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
        t = page.title() or ""
        title = t.strip()
    except Exception:
        pass
    try:
        og = page.locator('meta[property="og:title"]').first
        if og.count() > 0:
            c = (og.get_attribute("content") or "").strip()
            if c:
                title = c
    except Exception:
        pass
    # Limpieza típica
    title = re.sub(r"\s+\|\s*All\s*Access.*$", "", title, flags=re.I)
    return title or None

def prettify_from_slug(url: str) -> str:
    """Fallback de nombre a partir del slug /event/<slug>."""
    slug = url.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").title()

def fmt_status_entry(url: str, info: dict, include_url: bool = True) -> str:
    title = info.get("title") or ""
    st = info.get("status", "UNKNOWN")
    det = info.get("detail") or ""
    ts = info.get("ts", "")
    head = title if title else (url if include_url else "Show")
    if include_url and title:
        head = f"{title}\n{url}"
    if st == "AVAILABLE":
        line = f"✅ <b>Disponible</b> — {head}"
        if not include_url and title:
            line = f"✅ <b>Disponible</b> — {title}"
        if det: line += f"\nFunciones: {det}"
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
    """
    Chequeo rápido HTTP para /shows:
      True -> 2xx/3xx
      False -> 4xx/5xx o error de red
    """
    try:
        r = requests.get(url, timeout=6, allow_redirects=True, headers={"User-Agent":"Mozilla/5.0"})
        if 200 <= r.status_code < 400:
            return True, ""
        return False, f"ERROR HTTP {r.status_code}"
    except Exception as e:
        return False, f"ERROR {type(e).__name__}"

def fmt_shows_indexed() -> str:
    """Lista numerada sin URLs; marca errores obvios de URL."""
    lines = [f"🎯 Monitoreando (N={len(URLS)}){SIGN}"]
    if not URLS:
        lines.append("(no hay URLs configuradas)")
        return "\n".join(lines)
    for i, u in enumerate(URLS, start=1):
        title = (LAST_RESULTS.get(u) or {}).get("title")
        label = title or prettify_from_slug(u)
        ok, err = quick_url_check(u)
        if ok:
            lines.append(f"{i}) {label}")
        else:
            lines.append(f"{i}) {label}  ❗ {err}")
    return "\n".join(lines)

# ==============================
# Dropdown/listbox (abrir y enumerar funciones)
# ==============================
FUNC_TRIGGERS = [
    "button[aria-haspopup='listbox']",
    "[role='combobox']",
    "[data-testid*='select']",
    ".MuiSelect-select",       # Material UI
    ".aa-event-dates",         # clases comunes
    ".event-functions",        # genérico
]

LISTBOX_ROOTS = [
    "select",                  # <select>
    "[role='listbox']",
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
                page.wait_for_timeout(300)
                # no rompemos el loop: podemos tener varios triggers apilados
        except Exception:
            continue

def _list_functions_generic(page):
    """
    Devuelve lista de (label, element, via) para funciones:
      via='select' si viene de <select><option>
      via='list'   si viene de listbox/listas
    """
    # 1) <select><option>
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
                out.append((lbl, o, "select"))
            if out:
                return out
    except Exception:
        pass

    # 2) listbox/listas
    for root in LISTBOX_ROOTS[1:]:
        try:
            container = page.locator(root).first
            if not container or container.count() == 0:
                continue
            items = container.locator("[role='option'], li, .item, .option, a, button")
            if items.count() == 0:
                continue
            out = []
            for i in range(min(items.count(), 80)):
                it = items.nth(i)
                try:
                    txt = (it.inner_text(timeout=250) or "").strip()
                except Exception:
                    txt = ""
                if not txt:
                    continue
                out.append((txt, it, "list"))
            if out:
                return out
        except Exception:
            continue

    return []

# ==============================
# Chequeo de una URL
# ==============================
def check_url(url: str, page) -> tuple[list[str], str|None]:
    """
    Devuelve (lista_de_funciones_disponibles, titulo_del_show_o_None).
    Soporta:
      - <select><option>
      - listbox con role=option (requiere abrir antes)
      - show único (sin dropdown)
    """
    disponibles = []
    title = None

    try:
        page.goto(url, timeout=60000)
        page.wait_for_load_state("networkidle", timeout=15000)

        # Título
        title = extract_title(page)

        # Abrir dropdown si existe
        _open_dropdown_if_any(page)

        # Enumerar funciones
        funcs = _list_functions_generic(page)

        def is_soldout_label(s: str) -> bool:
            s = s.lower()
            return any(k in s for k in ["agotado", "sold out", "sin disponibilidad", "sem disponibilidade"])

        if funcs:
            for lbl, el, via in funcs:
                if not lbl or is_soldout_label(lbl):
                    continue
                # Normalizar etiqueta (quita sufijos tipo " — ...")
                lbl_clean = re.sub(r"\s+—\s+.*$", "", lbl).strip()
                if lbl_clean and lbl_clean not in disponibles:
                    disponibles.append(lbl_clean)
        else:
            # No hay dropdown: show único → buscar botón compra
            try:
                for btn in page.query_selector_all("button, a")[:100]:
                    t = (btn.inner_text() or "").lower()
                    if any(k in t for k in ["comprar", "entradas", "buy"]):
                        disponibles.append("Único show disponible")
                        break
            except Exception:
                pass

    except Exception as e:
        print(f"⚠️ Error al procesar {url}: {e}")

    return disponibles, title

# ==============================
# Telegram polling (/status [n], /shows)
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
                    shows, title = check_url(url, page)
                    ts = now_local().strftime("%Y-%m-%d %H:%M:%S")

                    prev_status = LAST_RESULTS.get(url, {}).get("status", "UNKNOWN")
                    if shows:
                        if prev_status != "AVAILABLE":
                            head = title or "Show"
                            det = ", ".join(shows)
                            tg_send(f"✅ ¡Entradas disponibles!\n{head}\nFunciones: {det}\n{SIGN}")
                        LAST_RESULTS[url] = {
                            "status": "AVAILABLE",
                            "detail": ", ".join(shows),
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
    # Hilo de polling de Telegram (comandos)
    t = threading.Thread(target=telegram_polling, daemon=True)
    t.start()

    # Monitor principal
    run_monitor()
