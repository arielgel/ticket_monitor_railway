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
    # Limpiezas típicas
    title = re.sub(r"\s+\|\s*All\s*Access.*$", "", title, flags=re.I)
    return title or None

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

def fmt_shows_indexed() -> str:
    """Lista numerada (sin URLs). Usa el orden de URLS."""
    lines = [f"🎯 Monitoreando (N={len(URLS)}){SIGN}"]
    for i, u in enumerate(URLS, start=1):
        title = (LAST_RESULTS.get(u) or {}).get("title")
        label = title or f"Show {i}"
        lines.append(f"{i}) {label}")
    return "\n".join(lines)

# ==============================
# Chequeo de una URL
# ==============================
def check_url(url: str, page) -> tuple[list[str], str|None]:
    """
    Devuelve (lista_de_funciones_disponibles, titulo_del_show_o_None).
    Heurística:
      - Si hay <select>, toma opciones que NO contengan 'Agotado'.
      - Si no hay <select>, busca botón con 'Comprar'/'Entradas'/'Buy'.
    """
    disponibles = []
    title = None
    try:
        page.goto(url, timeout=60000)
        page.wait_for_load_state("networkidle", timeout=15000)

        title = extract_title(page)

        # Intentar <select> (múltiples funciones)
        try:
            dropdown = page.wait_for_selector("select", timeout=3000)
            options = dropdown.query_selector_all("option")
            for opt in options:
                texto = (opt.inner_text() or "").strip()
                if texto and ("agotado" not in texto.lower()):
                    disponibles.append(texto)
        except PWTimeout:
            # Show único: buscar botón
            try:
                for btn in page.query_selector_all("button, a")[:80]:
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
                    # Intentar parsear índice: "/status 2"
                    m = re.match(r"^/status\s+(\d+)\s*$", tlow)
                    if m:
                        idx = int(m.group(1))
                        if 1 <= idx <= len(URLS):
                            url = URLS[idx - 1]
                            info = LAST_RESULTS.get(url, {"status": "UNKNOWN", "detail": None, "ts": "", "title": None})
                            # Mostrar sin URL acá (pediste limpio), pero podrías añadirla si querés.
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
