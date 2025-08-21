import os, re, time, requests, traceback
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

# --- Config ---
BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID", "")
URLS        = os.getenv("URLS", "").split(";") if os.getenv("URLS") else []
TZ_NAME     = "America/Argentina/Buenos_Aires"
CHECK_EVERY = int(os.getenv("CHECK_EVERY_SECONDS", "300"))

# --- Utilidades ---
def now_local():
    return datetime.now(ZoneInfo(TZ_NAME))

def send_telegram(msg: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("⚠️ Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"})
    except Exception as e:
        print(f"❌ Error Telegram: {e}")

def extract_title(page):
    try:
        h1 = page.query_selector("h1")
        if h1:
            return (h1.inner_text() or "").strip()
    except: pass
    return "¿Sin título?"

def page_has_buy(page):
    try:
        for btn in page.query_selector_all("button, a")[:100]:
            t = (btn.inner_text() or "").lower()
            if any(k in t for k in ["comprar", "entradas", "buy"]):
                return True
    except: pass
    return False

def page_has_soldout(page):
    txt = page.inner_text("body").lower()
    return any(k in txt for k in ["agotado", "sold out", "sin disponibilidad"])

def scan_dates(page):
    """Busca patrones de fecha dd/mm/yyyy en todo el body"""
    txt = page.inner_text("body")
    found = re.findall(r"\b\d{2}/\d{2}/\d{4}\b", txt)
    return list(dict.fromkeys(found))  # únicas, en orden

# --- Core ---
def check_url(url: str, page):
    fechas = []
    title = None
    status_hint = "UNKNOWN"

    try:
        page.goto(url, timeout=60000)
        page.wait_for_load_state("networkidle", timeout=15000)
        title = extract_title(page)

        has_buy = page_has_buy(page)
        has_sold = page_has_soldout(page)

        # Buscar fechas
        fechas = scan_dates(page)

        # Nueva lógica de hint
        if fechas:
            status_hint = "AVAILABLE_BY_DATES"
        elif has_buy and not fechas:
            status_hint = "AVAILABLE_NO_DATES"
        elif has_sold:
            status_hint = "SOLDOUT"
        else:
            status_hint = "UNKNOWN"

    except Exception as e:
        print(f"⚠️ Error en check_url {url}: {e}")

    return fechas, title, status_hint

# --- Telegram Polling ---
def telegram_polling():
    last_update = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={last_update+1}&timeout=20"
            r = requests.get(url, timeout=30).json()
            if "result" not in r: 
                continue
            for upd in r["result"]:
                last_update = upd["update_id"]
                if "message" not in upd: 
                    continue
                msg = upd["message"]
                txt = msg.get("text","").strip()
                if not txt: continue

                tlow = txt.lower()
                if tlow.startswith("/status"):
                    parts = txt.split()
                    if len(parts) > 1 and parts[1].isdigit():
                        idx = int(parts[1])
                        if 1 <= idx <= len(URLS):
                            with sync_playwright() as pw:
                                browser = pw.chromium.launch(headless=True)
                                page = browser.new_page()
                                fechas, title, hint = check_url(URLS[idx-1], page)
                                browser.close()
                            if hint.startswith("AVAILABLE"):
                                fstr = ", ".join(fechas) if fechas else "(sin fecha)"
                                send_telegram(f"✅ **Disponible** — {title}\nFechas: {fstr}\nÚltimo check: {now_local():%Y-%m-%d %H:%M:%S}\n — Roberto")
                            elif hint == "SOLDOUT":
                                send_telegram(f"⛔ Agotado — {title}\nÚltimo check: {now_local():%Y-%m-%d %H:%M:%S}\n — Roberto")
                            else:
                                send_telegram(f"❓ Indeterminado — {title}\nÚltimo check: {now_local():%Y-%m-%d %H:%M:%S}\n — Roberto")
                        else:
                            send_telegram("⚠️ Índice fuera de rango.")
                    else:
                        out = []
                        for i,u in enumerate(URLS, start=1):
                            with sync_playwright() as pw:
                                browser = pw.chromium.launch(headless=True)
                                page = browser.new_page()
                                title = extract_title(page) if page else u
                                browser.close()
                            out.append(f"{i}. {title}")
                        send_telegram("🎯 Monitoreando:\n" + "\n".join(out) + "\n — Roberto")

                elif tlow.startswith("/debug"):
                    parts = txt.split()
                    if len(parts) > 1 and parts[1].isdigit():
                        idx = int(parts[1])
                        if 1 <= idx <= len(URLS):
                            with sync_playwright() as pw:
                                browser = pw.chromium.launch(headless=True)
                                page = browser.new_page()
                                fechas, title, hint = check_url(URLS[idx-1], page)
                                browser.close()
                            send_telegram(
                                f"🧪 DEBUG — {title}\n"
                                f"URL idx {idx}\n"
                                f"status_hint={hint}\n"
                                f"pre/post: {', '.join(fechas) if fechas else '-'}\n — Roberto"
                            )

        except Exception as e:
            print(f"❌ Error polling: {e}")
            time.sleep(5)

# --- Loop principal ---
def monitor_loop():
    while True:
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page()
                for i,url in enumerate(URLS, start=1):
                    fechas, title, hint = check_url(url, page)
                    if hint.startswith("AVAILABLE"):
                        fstr = ", ".join(fechas) if fechas else "(sin fecha)"
                        send_telegram(f"✅ **Disponible** — {title}\nFechas: {fstr}\nÚltimo check: {now_local():%Y-%m-%d %H:%M:%S}\n — Roberto")
                    elif hint == "SOLDOUT":
                        send_telegram(f"⛔ Agotado — {title}\nÚltimo check: {now_local():%Y-%m-%d %H:%M:%S}\n — Roberto")
                    else:
                        print(f"❓ Indeterminado — {title}")
                browser.close()
            time.sleep(CHECK_EVERY)
        except Exception as e:
            print(f"💥 Error monitor_loop: {traceback.format_exc()}")
            time.sleep(30)

# --- Main ---
if __name__ == "__main__":
    if BOT_TOKEN and CHAT_ID and URLS:
        import threading
        t = threading.Thread(target=telegram_polling, daemon=True)
        t.start()
        monitor_loop()
    else:
        print("⚠️ Faltan variables de entorno BOT_TOKEN, CHAT_ID o URLS.")
