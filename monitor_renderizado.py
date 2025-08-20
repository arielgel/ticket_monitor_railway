import os
import time
import json
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ==================== Config por ENV ====================
# Si MONITORED_URLS está definido, se usan esas URLs (coma-separadas).
# Si no, se usa URL (una sola).
MONITORED_URLS = [u.strip() for u in os.getenv("MONITORED_URLS", "").split(",") if u.strip()]
URL_FALLBACK   = os.getenv("URL", "https://www.allaccess.com.ar/event/airbag")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CHECK_EVERY = int(os.getenv("CHECK_EVERY_SECONDS", "300"))       # 5 min
TIMEOUT_MS  = int(os.getenv("RENDER_TIMEOUT_MS", "60000"))        # 60 s

# Silencio nocturno (configurable)
TZ_NAME     = os.getenv("TIMEZONE", "America/Argentina/Buenos_Aires")
QUIET_START = os.getenv("QUIET_START", "00:00")  # inclusive
QUIET_END   = os.getenv("QUIET_END", "09:00")    # exclusivo

STATE_FILE = "estado_monitor.json"

# Palabras clave
AVAILABLE_KEYWORDS = ["comprar", "comprar entradas", "buy tickets"]
SOLDOUT_KEYWORDS   = ["agotado", "sold out"]

# Firma de mensajes
SIGNATURE = " — Roberto"

# ==================== Zona horaria ====================
def resolve_tz(tz_name: str):
    candidates = [tz_name, "America/Buenos_Aires", "Etc/GMT+3", "UTC"]
    for cand in candidates:
        try:
            return ZoneInfo(cand)
        except Exception:
            continue
    return None

_TZ_OBJ = resolve_tz(TZ_NAME)

def parse_hhmm(s: str):
    h, m = s.split(":")
    return int(h), int(m)

def now_local() -> datetime:
    return datetime.now(_TZ_OBJ) if _TZ_OBJ else datetime.now().astimezone()

def is_quiet_hours(moment: datetime) -> bool:
    sh, sm = parse_hhmm(QUIET_START)
    eh, em = parse_hhmm(QUIET_END)
    start = moment.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end   = moment.replace(hour=eh, minute=em, second=0, microsecond=0)
    if start <= end:
        return start <= moment < end
    else:
        return moment >= start or moment < end

def time_str(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")

# ==================== Estado persistente ====================
# Estructura:
# {
#   "last_status": {"url1": "SOLDOUT"/"AVAILABLE"/"UNKNOWN", ...},
#   "night_events": [{"ts": "...", "url": "...", "details": "..."}],
#   "last_summary_date": "YYYY-MM-DD" | null
# }
def default_state():
    return {"last_status": {}, "night_events": [], "last_summary_date": None}

def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # asegurar claves
        for k, v in default_state().items():
            data.setdefault(k, v)
        return data
    except Exception:
        return default_state()

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

def enqueue_night_event(state, moment: datetime, url: str, details: str):
    state["night_events"].append({
        "ts": time_str(moment),
        "url": url,
        "details": details
    })

def maybe_flush_morning_summary(state, moment: datetime):
    """A las QUIET_END, si hubo AVAILABLE de noche en cualquier URL, manda resumen."""
    if is_quiet_hours(moment):
        return state

    today_iso = moment.date().isoformat()
    if state.get("last_summary_date") == today_iso:
        return state

    eh, em = parse_hhmm(QUIET_END)
    cutoff = moment.replace(hour=eh, minute=em, second=0, microsecond=0)
    if moment < cutoff:
        return state

    events = state.get("night_events", [])
    if not events:
        state["last_summary_date"] = today_iso
        save_state(state)
        return state

    lines = [f"🗞️ Resumen nocturno ({QUIET_START}–{QUIET_END} {TZ_NAME}){SIGNATURE}", ""]
    for e in events[-80:]:
        lines.append(f"• {e['ts']} — {e['url']}\n  {e['details']}")
    send_telegram("\n".join(lines))

    state["night_events"] = []
    state["last_summary_date"] = today_iso
    save_state(state)
    return state

# ==================== Scrape/render ====================
def get_visible_status(page) -> tuple[str, str]:
    """Evalúa la página actual ya cargada en 'page'. Devuelve (status, details)."""
    # Esperas extra por si el sitio inyecta tarde
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_load_state("networkidle")
    page.wait_for_function("document.readyState === 'complete'", timeout=TIMEOUT_MS)
    # Scroll para lazy-load
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
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_function("document.readyState === 'complete'", timeout=TIMEOUT_MS)
        txt = page.evaluate("() => document.body.innerText").lower()

    if any(k in txt for k in AVAILABLE_KEYWORDS):
        return "AVAILABLE", "Detecté palabras de compra (p. ej. 'comprar')."
    if any(k in txt for k in SOLDOUT_KEYWORDS):
        return "SOLDOUT", "Detecté 'agotado'."
    return "UNKNOWN", "No encontré ni 'agotado' ni 'comprar'."

# ==================== Loop principal ====================
if __name__ == "__main__":
    URLS = MONITORED_URLS if MONITORED_URLS else [URL_FALLBACK]

    print(f"🔎 Monitor iniciado (N={len(URLS)}) cada {CHECK_EVERY}s | TZ={TZ_NAME} | silencio {QUIET_START}–{QUIET_END}")
    for u in URLS:
        print(f"• {u}")
    state = load_state()

    # Aviso de arranque (solo fuera de silencio)
    now = now_local()
    if not is_quiet_hours(now):
        send_telegram(f"🔎 Monitor iniciado (N={len(URLS)}){SIGNATURE}")

    while True:
        try:
            now = now_local()
            # Resumen si corresponde
            state = maybe_flush_morning_summary(state, now)

            # Abrimos Playwright una vez por ciclo y recorremos todas las URLs
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                          "--disable-dev-shm-usage"]
                )
                tz_id = TZ_NAME if _TZ_OBJ else "UTC"

                for url in URLS:
                    ctx = browser.new_context(
                        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/124.0.0.0 Safari/537.36"),
                        locale="es-AR",
                        timezone_id=tz_id,
                        viewport={"width": 1366, "height": 850},
                    )
                    page = ctx.new_page()
                    page.set_default_timeout(TIMEOUT_MS)

                    try:
                        page.goto(url, wait_until="networkidle", timeout=TIMEOUT_MS)
                        status, details = get_visible_status(page)
                    except PWTimeout:
                        status, details = "UNKNOWN", "Timeout al cargar/renderizar."
                    except Exception as e:
                        status, details = "UNKNOWN", f"Error: {e}"
                    finally:
                        ctx.close()

                    print(f"[{time_str(now)}] {status} | {url} | {details}")

                    last_map = state.get("last_status", {})
                    last_status = last_map.get(url)

                    # Solo notificamos si pasa a AVAILABLE
                    if status == "AVAILABLE" and status != last_status:
                        if is_quiet_hours(now):
                            enqueue_night_event(state, now, url, details)
                        else:
                            send_telegram(f"✅ ¡Entradas disponibles!\n{url}\n{details}{SIGNATURE}")

                    # Actualizamos estado
                    last_map[url] = status
                    state["last_status"] = last_map
                    save_state(state)

                browser.close()

        except Exception as e:
            print(f"[{time_str(now_local())}] 💥 Error general: {e}")

        time.sleep(CHECK_EVERY)
