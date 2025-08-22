import os, re, time, threading, traceback, requests, json
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

# ========= Config =========
def _get_env_any(*names, default=""):
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default

BOT_TOKEN = _get_env_any("TELEGRAM_BOT_TOKEN", "BOT_TOKEN", default="")
CHAT_ID   = _get_env_any("TELEGRAM_CHAT_ID", "CHAT_ID", default="")

URLS_RAW  = _get_env_any("URLS", "MONITORED_URLS", "URL", default="")
URLS      = [u.strip() for u in URLS_RAW.replace(";", ",").split(",") if u.strip()]

CHECK_EVERY = int(_get_env_any("CHECK_EVERY_SECONDS", "CHECK_EVERY", default="300"))
TZ_NAME     = _get_env_any("TIMEZONE", "TZ", "TZ_NAME", default="America/Argentina/Buenos_Aires")

def _parse_hour(hstr: str, default_hour: int) -> int:
    try:
        if not hstr: return default_hour
        hstr = str(hstr).strip()
        h = int(hstr.split(":",1)[0]) if ":" in hstr else int(hstr)
        return h if 0 <= h <= 23 else default_hour
    except Exception:
        return default_hour

QUIET_START = _parse_hour(_get_env_any("QUIET_START", default="0"), 0)
QUIET_END   = _parse_hour(_get_env_any("QUIET_END",   default="9"), 9)
print(f"[QuietHours] QUIET_START={QUIET_START} QUIET_END={QUIET_END}")

SIGN = " — Roberto"

# ========= Estado =========
LAST_RESULTS = {u: {"status":"UNKNOWN","detail":None,"ts":"","title":None} for u in URLS}

# ========= Utils =========
def now_local(): return datetime.now(ZoneInfo(TZ_NAME))
def within_quiet_hours():
    h = now_local().hour
    return (QUIET_START <= h < QUIET_END) if QUIET_START <= QUIET_END else (h >= QUIET_START or h < QUIET_END)

def tg_send(text: str, force: bool=False):
    if not force and within_quiet_hours():
        print("⏸️ Silenciado:", text); return
    if BOT_TOKEN and CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=20
            ).raise_for_status()
        except Exception as e:
            print("❌ Error Telegram:", e)
    print(text)

def prettify_from_slug(url: str) -> str:
    slug = url.rstrip("/").split("/")[-1]
    return slug.replace("-", " ").title()

def set_bot_commands():
    if not (BOT_TOKEN and CHAT_ID): return
    try:
        cmds = [
            {"command":"shows","description":"Listar shows"},
            {"command":"status","description":"Ver estado (/status N)"},
            {"command":"debug","description":"Debug (/debug N)"},
            {"command":"sectores","description":"(placeholder) Sectores (/sectores N)"},
        ]
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands", json={"commands":cmds}, timeout=15)
    except Exception as e:
        print("⚠️ setMyCommands:", e)

# ========= Selectores básicos =========
RE_DATE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")

def extract_title(page):
    title = ""
    try:
        og = page.locator('meta[property="og:title"]').first
        if og and og.count() > 0:
            c = (og.get_attribute("content") or "").strip()
            if c: title = c
    except: pass
    if not title:
        try: title = (page.title() or "").strip()
        except: pass
    if not title:
        for sel in ["h1",".event-title","[data-testid='event-title']","header h1"]:
            try:
                h = page.locator(sel).first
                if h and h.count()>0:
                    title = (h.inner_text() or "").strip()
                    if title: break
            except: continue
    return re.sub(r"\s*\|\s*All\s*Access.*$","",title,flags=re.I) or None

def _nearest_block(locator, max_up=8):
    for lvl in range(1,max_up+1):
        try:
            anc = locator.locator(f":scope >> xpath=ancestor::*[{lvl}]")
            if anc and anc.count() > 0:
                try:
                    anc.inner_text(timeout=250)
                    return anc.first
                except: continue
        except: continue
    return locator if locator and locator.count()>0 else None

def _find_functions_region(page):
    for sel in ["text=Selecciona la función","text=Seleccioná la función"]:
        try:
            node = page.locator(sel).first
            if node and node.count()>0: return _nearest_block(node,8)
        except: continue
    for sel in ["button:has-text('Ver entradas')","a:has-text('Ver entradas')"]:
        try:
            node = page.locator(sel).first
            if node and node.count()>0: return _nearest_block(node,8)
        except: continue
    try:
        lb = page.locator("[role='listbox']").first
        if lb and lb.count()>0: return _nearest_block(lb,6)
    except: pass
    return None

def _open_dropdown_if_any(page):
    for sel in [
        "button[aria-haspopup='listbox']","[role='combobox']","[aria-controls*='menu']",
        "[data-testid*='select']", ".MuiSelect-select",
        "button:has-text('Selecciona la función')","button:has-text('Seleccioná la función')",
    ]:
        try:
            loc = page.locator(sel).first
            if loc and loc.count()>0:
                loc.click(timeout=1500, force=True)
                page.wait_for_timeout(200)
        except: continue
    try: page.wait_for_selector(".MuiPopover-root, .MuiMenu-paper, [role='listbox']", timeout=1200)
    except: pass

def _gather_dates_in_region(region):
    if not region: return []
    fechas, seen = [], set()
    try:
        raw = (region.inner_text(timeout=600) or "").strip()
        for d in RE_DATE.findall(raw):
            if d not in seen:
                seen.add(d); fechas.append(d)
    except: pass
    return sorted(set(fechas), key=lambda s:(s[-4:], s[3:5], s[0:2]))

def page_has_soldout(page) -> bool:
    try:
        t = (page.evaluate("() => document.body.innerText") or "").lower()
    except Exception:
        t = ""
    return any(k in t for k in ["agotado","sold out","sin disponibilidad","sem disponibilidade"])

# ========= Lógica de chequeo =========
def check_url(url: str, page):
    fechas, title, hint = [], None, "UNKNOWN"
    page.goto(url, timeout=60000)
    page.wait_for_load_state("networkidle", timeout=15000)
    title = extract_title(page)
    _open_dropdown_if_any(page)
    region = _find_functions_region(page)
    fechas = _gather_dates_in_region(region)
    if fechas:
        hint = "AVAILABLE_BY_DATES"
    elif page_has_soldout(page):
        hint = "SOLDOUT"
    else:
        hint = "UNKNOWN"
    return fechas, title, hint

# ========= Formateo =========
def fmt_status_entry(url: str, info: dict, include_url: bool=False) -> str:
    title = info.get("title") or prettify_from_slug(url)
    st    = info.get("status","UNKNOWN")
    det   = info.get("detail") or ""
    ts    = info.get("ts","")
    head = title if not include_url else f"{title}\n{url}"
    if st.startswith("AVAILABLE"):
        line = f"✅ <b>¡Entradas disponibles!</b>\n{head}"
        if det: line += f"\nFechas: {det}"
    elif st == "SOLDOUT":
        line = f"⛔ Agotado — {head}"
    else:
        line = f"❓ Indeterminado — {head}"
    if ts: line += f"\nÚltimo check: {ts}"
    return line

def fmt_shows_indexed():
    lines = ["🎯 Monitoreando:"]
    for i,u in enumerate(URLS, start=1):
        info = LAST_RESULTS.get(u) or {}
        title = info.get("title") or prettify_from_slug(u)
        lines.append(f"{i}. {title}")
    return "\n".join(lines) + f"\n{SIGN}"

# ========= Telegram polling =========
def telegram_polling():
    if not (BOT_TOKEN and CHAT_ID):
        print("ℹ️ Telegram polling desactivado."); return
    set_bot_commands()
    offset = None
    api = f"https://api.telegram.org/bot{BOT_TOKEN}"
    print("🛰️ Telegram polling iniciado.")
    while True:
        try:
            params = {"timeout":50}
            if offset is not None: params["offset"] = offset
            r = requests.get(f"{api}/getUpdates", params=params, timeout=60)
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"): time.sleep(3); continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat = msg.get("chat", {}) ; chat_id = str(chat.get("id") or "")
                if not text or chat_id != str(CHAT_ID): continue
                tlow = text.lower()

                if tlow.startswith("/shows"):
                    tg_send(fmt_shows_indexed(), force=True)

                elif tlow.startswith("/status"):
                    m = re.match(r"^/status\s+(\d+)\s*$", tlow)
                    if m:
                        idx = int(m.group(1))
                        if 1 <= idx <= len(URLS):
                            url = URLS[idx-1]
                            info = LAST_RESULTS.get(url, {"status":"UNKNOWN","detail":None,"ts":"","title":None})
                            tg_send(fmt_status_entry(url, info, include_url=False) + f"\n{SIGN}", force=True)
                        else:
                            tg_send(f"Índice fuera de rango (1–{len(URLS)}).{SIGN}", force=True)
                    else:
                        snap = LAST_RESULTS.copy()
                        lines = [f"📊 Estado actual (N={len(snap)}){SIGN}"]
                        for url,info in snap.items():
                            lines.append("• " + fmt_status_entry(url, info, include_url=False))
                        tg_send("\n".join(lines), force=True)

                elif tlow.startswith("/debug"):
                    m = re.match(r"^/debug\s+(\d+)\s*$", tlow)
                    if not m:
                        tg_send(f"Usá: /debug N (ej: /debug 2){SIGN}", force=True); continue
                    idx = int(m.group(1))
                    if not (1 <= idx <= len(URLS)):
                        tg_send(f"Índice fuera de rango (1–{len(URLS)}).{SIGN}", force=True); continue
                    url = URLS[idx-1]
                    with sync_playwright() as p:
                        browser = p.chromium.launch(headless=True)
                        page = browser.new_page()
                        page.goto(url, timeout=60000)
                        page.wait_for_load_state("networkidle", timeout=15000)
                        title = extract_title(page) or prettify_from_slug(url)
                        _open_dropdown_if_any(page)
                        region = _find_functions_region(page)
                        pre = _gather_dates_in_region(region)
                        post = pre[:]
                        soldout = page_has_soldout(page)
                        decision = "AVAILABLE_BY_DATES" if (pre or post) else ("SOLDOUT" if soldout else "UNKNOWN")
                        browser.close()
                    tg_send(
                        f"🧪 DEBUG — {title}\nURL idx {idx}\n"
                        f"decision_hint={decision}\npre: {', '.join(pre) if pre else '-'}\n"
                        f"post: {', '.join(post) if post else '-'}\n{SIGN}",
                        force=True
                    )

                elif tlow.startswith("/sectores"):
                    tg_send("🧭 /sectores aún no activado en esta build mínima. (Lo sumamos cuando estabilice)."+SIGN, force=True)

        except Exception:
            print("⚠️ Polling error:", traceback.format_exc()); time.sleep(5)

# ========= Loop principal =========
def run_monitor():
    tg_send(f"🔎 Radar levantado (URLs: {len(URLS)}){SIGN}", force=True)
    while True:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                for url in URLS:
                    fechas, title, hint = check_url(url, page)
                    ts = now_local().strftime("%Y-%m-%d %H:%M:%S")
                    if fechas:
                        det = ", ".join(fechas)
                        prev = LAST_RESULTS.get(url, {}).get("status")
                        if prev != "AVAILABLE":
                            tg_send(f"✅ <b>¡Entradas disponibles!</b>\n{title or 'Show'}\nFechas: {det}\n{SIGN}")
                        LAST_RESULTS[url] = {"status":"AVAILABLE","detail":det,"ts":ts,"title":title}
                    else:
                        LAST_RESULTS[url] = {
                            "status":"SOLDOUT" if hint=="SOLDOUT" else "UNKNOWN",
                            "detail":None,"ts":ts,"title":title
                        }
                        print(f"{title or url} — {LAST_RESULTS[url]['status']} — {ts}")
                browser.close()
            time.sleep(CHECK_EVERY)
        except Exception:
            print("💥 Error monitor:", traceback.format_exc()); time.sleep(30)

# ========= Main =========
if __name__ == "__main__":
    if not URLS:
        print("⚠️ No hay URLs configuradas.")
    if BOT_TOKEN and CHAT_ID and URLS:
        t = threading.Thread(target=telegram_polling, daemon=True)
        t.start()
        run_monitor()
    else:
        print("⚠️ Faltan variables de entorno TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID o URLs.")
