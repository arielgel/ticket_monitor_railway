import os
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ==============================
# Configuración
# ==============================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
URLS = [u.strip() for u in os.getenv("MONITORED_URLS", "").split(",") if u.strip()]
CHECK_EVERY = int(os.getenv("CHECK_EVERY_SECONDS", "300"))
TZ_NAME = "America/Argentina/Buenos_Aires"

# ==============================
# Helpers
# ==============================
def now_local():
    return datetime.now(ZoneInfo(TZ_NAME))

def within_quiet_hours():
    h = now_local().hour
    return 0 <= h < 9  # silencio entre medianoche y 9am

# ==============================
# Notificación
# ==============================
def notify(msg: str):
    """Notificación que respeta silencio nocturno"""
    if within_quiet_hours():
        print("⏸️ Silenciado:", msg)
        return
    if BOT_TOKEN and CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
                timeout=20,
            ).raise_for_status()
        except Exception as e:
            print("❌ Error Telegram:", e)
    print(msg)

def notify_force(msg: str):
    """Notificación que IGNORA silencio nocturno (para pruebas/ping)."""
    if BOT_TOKEN and CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
                timeout=20,
            ).raise_for_status()
        except Exception as e:
            print("❌ Error Telegram (force):", e)
    print(msg)

# ==============================
# Check disponibilidad
# ==============================
def check_url(url: str, page) -> list[str]:
    """
    Devuelve lista de funciones con entradas disponibles.
    Si está agotado -> devuelve lista vacía.
    """
    disponibles = []
    try:
        page.goto(url, timeout=60000)
        page.wait_for_load_state("networkidle", timeout=15000)

        # desplegar dropdown si existe
        try:
            dropdown = page.wait_for_selector("select", timeout=5000)
            options = dropdown.query_selector_all("option")
            for opt in options:
                texto = opt.inner_text().strip()
                if "Agotado" not in texto and texto != "":
                    disponibles.append(texto)
        except PWTimeout:
            # puede ser single show (sin dropdown)
            try:
                btn = page.query_selector("button, a")
                if btn and ("Comprar" in btn.inner_text() or "entradas" in btn.inner_text()):
                    disponibles.append("Único show disponible")
            except Exception:
                pass

    except Exception as e:
        print(f"⚠️ Error al procesar {url}: {e}")
    return disponibles

# ==============================
# Main loop
# ==============================
if __name__ == "__main__":
    notify_force(f"🔎 Radar levantado (URLs: {len(URLS)}) — Roberto")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        while True:
            for url in URLS:
                shows = check_url(url, page)
                if shows:
                    notify(f"✅ Entradas disponibles en {url}\nFunciones: {', '.join(shows)}")
                else:
                    print(f"❌ Nada en {url} — {now_local()}")
            time.sleep(CHECK_EVERY)
