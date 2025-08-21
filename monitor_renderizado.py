import os
import re
import time
import threading
import traceback
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

# ==============================
# Config retro-compatible (Railway)
# ==============================
def _get_env_any(*names, default=""):
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default

BOT_TOKEN = _get_env_any("TELEGRAM_BOT_TOKEN", "BOT_TOKEN", default="")
CHAT_ID = _get_env_any("TELEGRAM_CHAT_ID", "CHAT_ID", default="")

# URLs: acepta URLS, MONITORED_URLS o URL. Soporta ; o , como separador.
URLS_RAW = _get_env_any("URLS", "MONITORED_URLS", "URL", default="")
_urls_norm = URLS_RAW.replace(";", ",")
URLS = [u.strip() for u in _urls_norm.split(",") if u.strip()]

CHECK_EVERY = int(_get_env_any("CHECK_EVERY_SECONDS", "CHECK_EVERY", default="300"))
TZ_NAME = _get_env_any("TIMEZONE", "TZ", "TZ_NAME", default="America/Argentina/Buenos_Aires")
QUIET_START = int(_get_env_any("QUIET_START", default="0"))
QUIET_END = int(_get_env_any("QUIET_END", default="9"))

SIGN = " — Roberto"

# ==============================
# Estado en memoria
# ==============================
LAST_RESULTS = {u: {"status": "UNKNOWN", "detail": None, "ts": "", "title": None} for u in URLS}

# ==============================
# Utilitarios
# ==============================
def now_local() -> datetime:
    return datetime.now(ZoneInfo(TZ_NAME))

def within_quiet_hours() -> bool:
    h = now_local().hour
    if QUIET_START <= QUIET_END:
        return QUIET_START <= h < QUIET_END
    return h >= QUIET_START or h < QUIET_END

def tg_send(text: str, force: bool = False):
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

def prettify_from_slug(url: str) -> str:
    slug = url.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").title()

# ==============================
# Título y detectores
# ==============================
def extract_title(page):
    title = ""
    try:
        og = page.locator('meta[property="og:title"]').first
        if og and og.count() > 0:
            c = (og.get_attribute("content") or "").strip()
            if c:
                title = c
    except Exception:
        pass
    if not title:
        try:
            title = (page.title() or "").strip()
        except Exception:
            pass
    if not title:
        for sel in ["h1", ".event-title", "[data-testid='event-title']", "header h1"]:
            try:
                h = page.locator(sel).first
                if h and h.count() > 0:
                    title = (h.inner_text() or "").strip()
                    if title:
                        break
            except Exception:
                continue
    title = re.sub(r"\s*\|\s*All\s*Access.*$", "", title, flags=re.I)
    return title or None

def page_text(page) -> str:
    try:
        return (page.evaluate("() => document.body.innerText") or "").lower()
    except Exception:
        return ""

def page_has_soldout(page) -> bool:
    t = page_text(page)
    return any(k in t for k in ["agotado", "sold out", "sin disponibilidad", "sem disponibilidade"])

# ==============================
# Región de funciones (AllAccess)
# ==============================
def _nearest_block(locator, max_up=8):
    for lvl in range(1, max_up + 1):
        try:
            anc = locator.locator(f":scope >> xpath=ancestor::*[{lvl}]")
            if anc and anc.count() > 0:
                try:
                    bb = anc.bounding_box()
                    txt = (anc.inner_text(timeout=250) or "").strip()
                except Exception:
                    bb, txt = None, ""
                if bb and len(txt) > 10:
                    return anc.first
        except Exception:
            continue
    return locator if locator and locator.count() > 0 else None

def _find_functions_region(page):
    # 1) label
    for sel in ["text=Selecciona la función", "text=Seleccioná la función"]:
        try:
            node = page.locator(sel).first
            if node and node.count() > 0:
                return _nearest_block(node, max_up=8)
        except Exception:
            continue
    # 2) botón “Ver entradas”
    for sel in ["button:has-text('Ver entradas')", "a:has-text('Ver entradas')"]:
        try:
            node = page.locator(sel).first
            if node and node.count() > 0:
                return _nearest_block(node, max_up=8)
        except Exception:
            continue
    # 3) listbox visible
    try:
        lb = page.locator("[role='listbox']").first
        if lb and lb.count() > 0:
            return _nearest_block(lb, max_up=6)
    except Exception:
        pass
    return None

def _open_dropdown_if_any(page):
    triggers = [
        "button[aria-haspopup='listbox']",
        "[role='combobox']",
        "[aria-controls*='menu']",
        "[data-testid*='select']",
        ".MuiSelect-select",
        "button:has-text('Selecciona la función')",
        "button:has-text('Seleccioná la función')",
        "button:has-text('Seleccionar')",
        "button:has-text('Seleccioná')",
    ]
    for sel in triggers:
        try:
            loc = page.locator(sel).first
            if loc and loc.count() > 0:
                loc.click(timeout=1500, force=True)
                page.wait_for_timeout(250)
        except Exception:
            continue
    try:
        page.wait_for_selector(".MuiPopover-root, .MuiMenu-paper, [role='listbox']", timeout=1200)
    except Exception:
        pass

RE_DATE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")

def _gather_dates_in_region(region):
    if not region:
        return []
    fechas, seen = [], set()

    try:
        items = region.locator("[role='option'], .MuiMenuItem-root, li, option, button, a, div")
        n = min(items.count(), 200)
        for i in range(n):
            it = items.nth(i)
            try:
                txt = (it.inner_text(timeout=250) or "").strip()
            except Exception:
                txt = ""
            low = txt.lower()
            if not txt or "agotado" in low or "sold out" in low or "sin disponibilidad" in low:
                continue
            for d in RE_DATE.findall(txt):
                if d not in seen:
                    seen.add(d)
                    fechas.append(d)
    except Exception:
        pass

    try:
        raw = (region.inner_text(timeout=400) or "").strip()
        for d in RE_DATE.findall(raw):
            if d not in seen:
                seen.add(d)
                fechas.append(d)
    except Exception:
        pass

    fechas = sorted(set(fechas), key=lambda s: (s[-4:], s[3:5], s[0:2]))
    return fechas

# ==============================
# Chequeo de una URL
# ==============================
def check_url(url: str, page):
    fechas, title = [], None
    status_hint = "UNKNOWN"
    try:
        page.goto(url, timeout=60000)
        page.wait_for_load_state("networkidle", timeout=15000)

        title = extract_title(page)

        _open_dropdown_if_any(page)
        region = _find_functions_region(page)
        fechas = _gather_dates_in_region(region)

        if fechas:
            status_hint = "AVAILABLE_BY_DATES"
        else:
            if page_has_soldout(page):
                status_hint = "SOLDOUT"
            elif region and region.locator("a:has-text('Comprar'), button:has-text('Comprar')").count() > 0:
                status_hint = "AVAILABLE_NO_DATES"
            else:
                status_hint = "UNKNOWN"
    except Exception as e:
        print(f"⚠️ Error en check_url {url}: {e}")

    return fechas, title, status_hint

# ==============================
# Formateo de salida
# ==============================
def fmt_status_entry(url: str, info: dict, include_url: bool = False) -> str:
    title = info.get("title") or prettify_from_slug(url)
    st = info.get("status", "UNKNOWN")
    det = info.get("detail") or ""
    ts = info.get("ts", "")
    head = title if not include_url else f"{title}\n{url}"

    if st.startswith("AVAILABLE"):
        line = f"✅ <b>¡Entradas disponibles!</b>\n{head}"
        if det:
            line += f"\nFechas: {det}"
    elif st == "SOLDOUT":
        line = f"⛔ Agotado — {head}"
    else:
        line = f"❓ Indeterminado — {head}"
        if det:
            line += f"\nNota: {det}"
    if ts:
        line += f"\nÚltimo check: {ts}"
    return line

def fmt_shows_indexed() -> str:
    lines = ["🎯 Monitoreando:"]
    for i, u in enumerate(URLS, start=1):
        info = LAST_RESULTS.get(u) or {}
        title = info.get("title") or prettify_from_slug(u)
        lines.append(f"{i}. {title}")
    return "\n".join(lines) + f"\n{SIGN}"

# ==============================
# Telegram (comandos básicos)
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
                        lines = [f"📊 Estado actual (N={len(snap)}){SIGN}"]
                        for url, info in snap.items():
                            lines.append("• " + fmt_status_entry(url, info, include_url=False))
                        tg_send("\n".join(lines), force=True)

                elif tlow.startswith("/debug"):
                    m = re.match(r"^/debug\s+(\d+)\s*$", tlow)
                    if not m:
                        tg_send(f"Usá: /debug N (ej: /debug 2){SIGN}", force=True)
                        continue

                    idx = int(m.group(1))
                    if not (1 <= idx <= len(URLS)):
                        tg_send(f"Índice fuera de rango (1–{len(URLS)}).{SIGN}", force=True)
                        continue

                    url = URLS[idx - 1]
                    # Abrimos Playwright en un bloque separado y lo cerramos limpio
                    with sync_playwright() as p:
                        browser = p.chromium.launch(headless=True)
                        try:
                            page = browser.new_page()
                            page.goto(url, timeout=60000)
                            page.wait_for_load_state("networkidle", timeout=15000)
                            title = extract_title(page) or prettify_from_slug(url)

                            _open_dropdown_if_any(page)
                            region = _find_functions_region(page)
                            pre = _gather_dates_in_region(region)
                            post = pre[:]  # AllAccess suele ser inline

                            soldout = page_has_soldout(page)
                            if pre or post:
                                decision = "AVAILABLE_BY_DATES"
                            elif soldout:
                                decision = "SOLDOUT"
                            else:
                                decision = "UNKNOWN"
                        finally:
                            # Cierre del browser SIEMPRE dentro del with
                            try:
                                browser.close()
                            except Exception:
                                pass

                    tg_send(
                        f"🧪 DEBUG — {title}\n"
                        f"URL idx {idx}\n"
                        f"decision_hint={decision}\n"
                        f"pre: {', '.join(pre) if pre else '-'}\n"
                        f"post: {', '.join(post) if post else '-'}\n{SIGN}",
                        force=True
                    )

        except Exception:
            print("⚠️ Polling error:", traceback.format_exc())
            time.sleep(5)

# ==============================
# Loop principal
# ==============================
def run_monitor():
    tg_send(f"🔎 Radar levantado (URLs: {len(URLS)}){SIGN}", force=True)
    while True:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    for url in URLS:
                        try:
                            fechas, title, hint = check_url(url, page)
                            ts = now_local().strftime("%Y-%m-%d %H:%M:%S")
                            if fechas:
                                det = ", ".join(fechas)
                                prev = LAST_RESULTS.get(url, {}).get("status")
                                if prev != "AVAILABLE":
                                    tg_send(
                                        f"✅ <b>¡Entradas disponibles!</b>\n{title or 'Show'}\nFechas: {det}\n{SIGN}"
                                    )
                                LAST_RESULTS[url] = {"status": "AVAILABLE", "detail": det, "ts": ts, "title": title}
                            else:
                                if hint == "SOLDOUT":
                                    LAST_RESULTS[url] = {"status": "SOLDOUT", "detail": None, "ts": ts, "title": title}
                                elif hint.startswith("AVAILABLE_NO_DATES"):
                                    LAST_RESULTS[url] = {"status": "AVAILABLE", "detail": None, "ts": ts, "title": title}
                                else:
                                    LAST_RESULTS[url] = {"status": "UNKNOWN", "detail": None, "ts": ts, "title": title}
                                print(f"{title or url} — {LAST_RESULTS[url]['status']} — {ts}")
                finally:
                    # Cerrar el browser SIEMPRE antes de salir del with
                    try:
                        browser.close()
                    except Exception:
                        pass
            time.sleep(CHECK_EVERY)
        except Exception:
            print("💥 Error monitor:", traceback.format_exc())
            time.sleep(30)

# ==============================
# Main
# ==============================
if __name__ == "__main__":
    if not URLS:
        print("⚠️ No hay URLs (variables URLS / MONITORED_URLS / URL vacías).")
    if BOT_TOKEN and CHAT_ID and URLS:
        t = threading.Thread(target=telegram_polling, daemon=True)
        t.start()
        run_monitor()
    else:
        print("⚠️ Faltan variables de entorno TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID o URLs.")
