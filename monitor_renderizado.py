import os
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
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # numérico en string
URLS = [u.strip() for u in os.getenv("MONITORED_URLS", "").split(",") if u.strip()]
CHECK_EVERY = int(os.getenv("CHECK_EVERY_SECONDS", "300"))  # segundos
TZ_NAME = os.getenv("TIMEZONE", "America/Argentina/Buenos_Aires")

# Firma
SIGN = " — Roberto"

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

def fmt_status_snapshot(snap):
    """Formatea el snapshot para /status."""
    lines = [f"📊 Estado actual (N={len(snap)}){SIGN}"]
    for url, info in snap.items():
        st = info.get("status", "UNKNOWN")
        det = info.get("detail") or ""
        ts = info.get("ts", "")
        if st == "AVAILABLE":
            line = f"• ✅ Disponible — {url}"
            if det: line += f"\n  Fechas: {det}"
        elif st == "SOLDOUT":
            line = f"• ⛔ Agotado — {url}"
        else:
            line = f"• ❓ Indeterminado — {url}"
            if det: line += f"\n  Nota: {det}"
        if ts: line += f"\n  Último check: {ts}"
        lines.append(line)
    return "\n".join(lines)

# Estado cacheado para /status
LAST_RESULTS = {u: {"status": "UNKNOWN", "detail": None, "ts": ""} for u in URLS}

# ==============================
# Chequeo de una URL
# ==============================
def check_url(url: str, page) -> list[str]:
    """
    Devuelve lista de funciones con entradas disponibles.
    Estrategia simple: si hay <select>, toma opciones que no contengan 'Agotado'.
    Si no hay select, intenta ver si hay botón de compra.
    """
    disponibles = []
    try:
        page.goto(url, timeout=60000)
        page.wait_for_load_state("networkidle", timeout=15000)

        # Intentar <select> (múltiples funciones)
        try:
            dropdown = page.wait_for_selector("select", timeout=5000)
            options = dropdown.query_selector_all("option")
            for opt in options:
                texto = (opt.inner_text() or "").strip()
                if texto and ("agotado" not in texto.lower()):
                    disponibles.append(texto)
        except PWTimeout:
            # Show único (sin dropdown): buscar botón de compra/entradas
            try:
                all_btns = page.query_selector_all("button, a")
                for btn in all_btns[:50]:
                    t = (btn.inner_text() or "").lower()
                    if any(k in t for k in ["comprar", "entradas", "buy"]):
                        disponibles.append("Único show disponible")
                        break
            except Exception:
                pass

    except Exception as e:
        print(f"⚠️ Error al procesar {url}: {e}")
    return disponibles

# ==============================
# Hilo de comandos Telegram (/status)
# ==============================
def telegram_polling():
    """
    Long polling liviano para comandos entrantes.
    Responde a:
      - /status   → devuelve el estado cacheado (siempre, ignorando silencio)
    """
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

                # Solo respondemos al chat autorizado
                if not text or chat_id != str(CHAT_ID):
                    continue

                if text.lower().startswith("/status"):
                    # snapshot atómico
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
                    shows = check_url(url, page)
                    ts = now_local().strftime("%Y-%m-%d %H:%M:%S")

                    if shows:
                        # Notificar si pasa de NO disponible a disponible
                        prev = LAST_RESULTS.get(url, {}).get("status", "UNKNOWN")
                        if prev != "AVAILABLE":
                            tg_send(f"✅ ¡Entradas disponibles!\n{url}\nFunciones: {', '.join(shows)}{SIGN}")
                        LAST_RESULTS[url] = {"status": "AVAILABLE", "detail": ", ".join(shows), "ts": ts}
                    else:
                        LAST_RESULTS[url] = {"status": "SOLDOUT", "detail": None, "ts": ts}
                        print(f"❌ Nada en {url} — {ts}")

                except Exception as e:
                    ts = now_local().strftime("%Y-%m-%d %H:%M:%S")
                    LAST_RESULTS[url] = {"status": "UNKNOWN", "detail": str(e), "ts": ts}
                    print(f"💥 Error en {url}: {e}")

            time.sleep(CHECK_EVERY)

# ==============================
# Arranque
# ==============================
if __name__ == "__main__":
    # Iniciar hilo de polling de Telegram
    t = threading.Thread(target=telegram_polling, daemon=True)
    t.start()

    # Correr monitor principal
    run_monitor()
