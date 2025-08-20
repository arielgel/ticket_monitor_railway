import os
import time
import json
import requests
from datetime import datetime, date
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ==================== Config ====================
URL = os.getenv("URL", "https://www.allaccess.com.ar/event/airbag")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_EVERY = int(os.getenv("CHECK_EVERY_SECONDS", "300"))
TIMEOUT_MS = int(os.getenv("RENDER_TIMEOUT_MS", "60000"))

# Silencio nocturno (configurable por env)
TZ_NAME     = os.getenv("TIMEZONE", "America/Argentina/Buenos_Aires")
QUIET_START = os.getenv("QUIET_START", "00:00")  # inclusive
QUIET_END   = os.getenv("QUIET_END", "09:00")    # exclusivo

STATE_FILE = "estado_monitor.json"

AVAILABLE_KEYWORDS = ["comprar", "comprar entradas", "buy tickets"]
SOLDOUT_KEYWORDS   = ["agotado", "sold out"]


# ==================== Utiles horario ====================
def parse_hhmm(s: str):
    h, m = s.split(":")
    return int(h), int(m)

def now_local():
    return datetime.now(ZoneInfo(TZ_NAME))

def is_quiet_hours(moment: datetime) -> bool:
    sh, sm = parse_hhmm(QUIET_START)
    eh, em = parse_hhmm(QUIET_END)
    local = moment
    start = local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end   = local.replace(hour=eh, minute=em, second=0, microsecond=0)
    if start <= end:
        return start <= local < end
    else:
        # rango que cruza medianoche (ej 22:00–07:00)
        return local >= start or local < end

def time_str(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


# ==================== Estado persistente ====================
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_status": None, "night_events": [], "last_summary_date": None}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_status": None, "night_events": [], "last_summary_date": None}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ==================== Notificaciones ====================
def send_telegram(text: str):
    if not TOKEN or not CHAT_ID:
        print("⚠️ Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=20,
        )
        r.raise_for_status()
    except Exception as e:
        print("❌ Error Telegram:", e)


def enqueue_night_event(state, moment: datetime, status: str, details: str):
    state["night_events"].append({
        "ts": time_str(moment),
        "status": status,
        "details": details
    })


def maybe_flush_morning_summary(state, moment: datetime):
    """Envía resumen a las 09:00 (o la hora QUIET_END) si hay eventos nocturnos y
    aún no se envió resumen hoy."""
    # Solo si YA no es horario silencioso
    if is_quiet_hours(moment):
        return state

    today_iso = moment.date().isoformat()
    if state.get("last_summary_date") == today_iso:
        return state

    # Chequear si hoy ya pasamos QUIET_END
    eh, em = parse_hhmm(QUIET_END)
    cutoff = moment.replace(hour=eh, minute=em, second=0, microsecond=0)
    if moment < cutoff:
        return state  # todavía no llegamos al final del silencio

    events = state.get("night_events", [])
    if not events:
        state["last_summary_date"] = today_iso
        save_state(state)
        return state

    # Construir resumen
    lines = [f"🗞️ Resumen nocturno ({QUIET_START}–{QUIET_END} {TZ_NAME})", f"URL: {URL}", ""]
    for e in events[-30:]:  # límite defensivo
        lines.append(f"• {e['ts']}: {e['status']} — {e['details']}")
    send_telegram("\n".join(lines))

    # Vaciar cola y marcar día
    state["night_events"] = []
    state["last_summary_date"] = today_iso
    save_state(state)
    return state


# ==================== Scrape/render ====================
def get_visible_status():
    """Devuelve (status, detalles) tras renderizado real."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # import local

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            locale="es-AR",
            timezone_id=TZ_NAME,
            viewport={"width": 1366, "height": 850},
        )
        page = ctx.new_page()
        page.set_default_timeout(TIMEOUT_MS)

        try:
            page.goto(URL, wait_until="networkidle", timeout=TIMEOUT_MS)
            # Esperas extra por si hay JS perezoso
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_load_state("networkidle")
            page.wait_for_function("document.readyState === 'complete'", timeout=TIMEOUT_MS)
            page.evaluate("""() => new Promise(res => {
                let y = 0;
                const i = setInterval(() => {
                    window.scrollTo(0, y += 500);
                    if (y > document.body.scrollHeight + 800) { clearInterval(i); res(); }
                }, 120);
            })""")
            page.wait_for_timeout(800)

            txt = page.evaluate("() => document.body.innerText").lower()

            # Intersticial anti-bot
            if "aguarde un instante" in txt or "actividad sospechosa" in txt:
                page.wait_for_timeout(2500)
                page.reload(wait_until="networkidle")
                txt = page.evaluate("() => document.body.innerText").lower()

            # Evaluación por keywords
            if any(k in txt for k in AVAILABLE_KEYWORDS):
                return "AVAILABLE", "Detecté palabras de compra (p. ej. 'comprar')."
            if any(k in txt for k in SOLDOUT_KEYWORDS):
                return "SOLDOUT", "Sigue figurando 'agotado'."
            return "UNKNOWN", "No encontré ni 'agotado' ni 'comprar'."

        except PWTimeout:
            return "UNKNOWN", "Timeout al cargar/renderizar."
        except Exception as e:
            return "UNKNOWN", f"Error: {e}"
        finally:
            ctx.close()
            browser.close()


# ==================== Loop principal ====================
if __name__ == "__main__":
    print(f"🔎 Monitor iniciado: {URL} (cada {CHECK_EVERY}s) | TZ={TZ_NAME} | silencio {QUIET_START}–{QUIET_END}")
    state = load_state()
    # Primer ping informativo (fuera de silencio)
    now = now_local()
    if not is_quiet_hours(now):
        send_telegram(f"🔎 Monitor iniciado: {URL} (cada {CHECK_EVERY}s)")

    while True:
        try:
            now = now_local()
            # Si corresponde, manda resumen de lo que pasó en la noche
            state = maybe_flush_morning_summary(state, now)

            status, details = get_visible_status()
            logline = f"[{time_str(now)}] status={status} | {details}"
            print(logline)

            last_status = state.get("last_status")

            # Disparos según horario
            if status != last_status and status != "UNKNOWN":
                if is_quiet_hours(now):
                    # Guardar para resumen
                    enqueue_night_event(state, now, status, details)
                else:
                    # Aviso inmediato
                    if status == "AVAILABLE":
                        send_telegram(f"✅ ¡Entradas disponibles!\n{URL}\n{details}")
                    elif status == "SOLDOUT":
                        send_telegram(f"⛔ Aún figura AGOTADO.\n{URL}\n{details}")

                state["last_status"] = status
                save_state(state)

        except Exception as e:
            print(f"[{time_str(now_local())}] 💥 Error general: {e}")

        time.sleep(CHECK_EVERY)
