import os, time, json, hashlib, requests, sys
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

URL = os.getenv("URL", "https://www.allaccess.com.ar/event/airbag")
CHECK_EVERY_SECONDS = int(os.getenv("CHECK_EVERY_SECONDS", "300"))  # 5 min
RENDER_TIMEOUT_MS = int(os.getenv("RENDER_TIMEOUT_MS", "60000"))    # 60s
STATE_FILE = "estado_render.json"

AVAILABLE_KEYWORDS = (os.getenv("AVAILABLE_KEYWORDS") or "comprar,comprar entradas,buy tickets").lower().split(",")
SOLDOUT_KEYWORDS   = (os.getenv("SOLDOUT_KEYWORDS")   or "agotado,sold out").lower().split(",")

TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(text: str):
    if not TOKEN or not CHAT_ID:
        print("⚠️ Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print("❌ Error Telegram:", e)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        return json.load(open(STATE_FILE, "r", encoding="utf-8"))
    except Exception:
        return {}

def save_state(d):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)

def visible_text(page):
    return page.evaluate("() => document.body.innerText").lower()

def status_from_text(txt: str) -> str:
    if any(k.strip() and k.strip() in txt for k in AVAILABLE_KEYWORDS):
        return "AVAILABLE"
    if any(k.strip() and k.strip() in txt for k in SOLDOUT_KEYWORDS):
        return "SOLDOUT"
    return "UNKNOWN"

def fingerprint(txt: str) -> str:
    return hashlib.sha256(txt.encode("utf-8")).hexdigest()

def wait_full_render(page):
    page.wait_for_load_state("domcontentloaded", timeout=RENDER_TIMEOUT_MS)
    page.wait_for_load_state("networkidle", timeout=RENDER_TIMEOUT_MS)
    page.wait_for_function("document.readyState === 'complete'", timeout=RENDER_TIMEOUT_MS)
    page.evaluate("""() => new Promise(res => {
        let y = 0;
        const i = setInterval(() => {
            window.scrollTo(0, y += 500);
            if (y > document.body.scrollHeight + 800) { clearInterval(i); res(); }
        }, 120);
    })""")
    page.wait_for_timeout(1200)

def handle_interstitial(page):
    txt = visible_text(page)
    if "aguarde un instante" in txt or "actividad sospechosa" in txt:
        page.wait_for_timeout(2500)
        page.reload(wait_until="networkidle")
        wait_full_render(page)

def check_once(pw):
    browser = pw.chromium.launch(headless=True, args=[
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
    ])
    ctx = browser.new_context(
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"),
        locale="es-AR",
        timezone_id="America/Buenos_Aires",
        viewport={"width": 1366, "height": 850},
        has_touch=False,
    )
    page = ctx.new_page()
    page.set_default_timeout(RENDER_TIMEOUT_MS)

    try:
        page.goto(URL, wait_until="networkidle", timeout=RENDER_TIMEOUT_MS)
        wait_full_render(page)
        handle_interstitial(page)

        try:
            page.wait_for_selector("select, [role=combobox], button, a", timeout=2000)
            page.wait_for_timeout(500)
        except PWTimeout:
            pass

        txt = visible_text(page)
        st  = status_from_text(txt)
        fp  = fingerprint(txt)
        return st, fp
    finally:
        ctx.close()
        browser.close()

def main_loop():
    state = load_state()
    last_status = state.get("last_status")
    last_fp = state.get("last_fp")

    send_telegram(f"🔎 Monitor iniciado: {URL} (cada {CHECK_EVERY_SECONDS}s)")
    print("🚀 Monitor corriendo. URL:", URL)

    with sync_playwright() as pw:
        while True:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            try:
                status, fp = check_once(pw)
                print(f"[{ts}] status={status} fp={fp[:10]}...")
                if status != last_status and status != "UNKNOWN":
                    if status == "AVAILABLE":
                        send_telegram(f"✅ ¡Detecté botón de compra/keywords en:\n{URL}")
                    elif status == "SOLDOUT":
                        send_telegram(f"⛔ Aún figura AGOTADO en:\n{URL}")
                    last_status = status

                if fp != last_fp:
                    last_fp = fp
                save_state({"last_status": last_status, "last_fp": last_fp})

            except Exception as e:
                print(f"[{ts}] 💥 Error:", e)

            time.sleep(CHECK_EVERY_SECONDS)

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        print("🛑 Monitor detenido por el usuario")
        sys.exit(0)
