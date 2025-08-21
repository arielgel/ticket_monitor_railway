import os
import re
import time
import json
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

URLS_RAW = _get_env_any("URLS", "MONITORED_URLS", "URL", default="")
_urls_norm = URLS_RAW.replace(";", ",")
URLS = [u.strip() for u in _urls_norm.split(",") if u.strip()]

CHECK_EVERY = int(_get_env_any("CHECK_EVERY_SECONDS", "CHECK_EVERY", default="300"))
TZ_NAME = _get_env_any("TIMEZONE", "TZ", "TZ_NAME", default="America/Argentina/Buenos_Aires")

def _parse_hour(hstr: str, default_hour: int) -> int:
    """Acepta '0', '9', '00:00', '09:00' y devuelve hora 0..23."""
    try:
        if not hstr:
            return default_hour
        hstr = str(hstr).strip()
        if ":" in hstr:
            hh = hstr.split(":", 1)[0]
            h = int(hh)
        else:
            h = int(hstr)
        if 0 <= h <= 23:
            return h
    except Exception:
        pass
    return default_hour

QUIET_START = _parse_hour(_get_env_any("QUIET_START", default="0"), default_hour=0)
QUIET_END   = _parse_hour(_get_env_any("QUIET_END",   default="9"), default_hour=9)
print(f"[QuietHours] QUIET_START={QUIET_START} QUIET_END={QUIET_END}")

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

def set_bot_commands():
    """Da de alta /shows, /status, /debug, /sectores como opciones sugeridas."""
    if not (BOT_TOKEN and CHAT_ID):
        return
    try:
        cmds = [
            {"command": "shows",     "description": "Listar shows configurados"},
            {"command": "status",    "description": "Ver estado (uso: /status N)"},
            {"command": "debug",     "description": "Debug de una URL (uso: /debug N)"},
            {"command": "sectores",  "description": "Listar sectores por fecha (uso: /sectores N)"},
        ]
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands",
            json={"commands": cmds},
            timeout=15
        ).raise_for_status()
    except Exception as e:
        print("⚠️ setMyCommands:", e)

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
# Región de funciones
# ==============================
def _nearest_block(locator, max_up=8):
    for lvl in range(1, max_up + 1):
        try:
            anc = locator.locator(f":scope >> xpath=ancestor::*[{lvl}]")
            if anc and anc.count() > 0:
                try:
                    anc.inner_text(timeout=250)  # asegura que es renderizable
                    return anc.first
                except Exception:
                    continue
        except Exception:
            continue
    return locator if locator and locator.count() > 0 else None

def _find_functions_region(page):
    for sel in ["text=Selecciona la función", "text=Seleccioná la función"]:
        try:
            node = page.locator(sel).first
            if node and node.count() > 0:
                return _nearest_block(node, max_up=8)
        except Exception:
            continue
    for sel in ["button:has-text('Ver entradas')", "a:has-text('Ver entradas')"]:
        try:
            node = page.locator(sel).first
            if node and node.count() > 0:
                return _nearest_block(node, max_up=8)
        except Exception:
            continue
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
        raw = (region.inner_text(timeout=600) or "").strip()
        for d in RE_DATE.findall(raw):
            if d not in seen:
                seen.add(d)
                fechas.append(d)
    except Exception:
        pass
    fechas = sorted(set(fechas), key=lambda s: (s[-4:], s[3:5], s[0:2]))
    return fechas

# ==============================
# Chequeo de URL (estado básico)
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
# Sectores: sniffer + DOM fallback
# ==============================
SEATMAP_ENDPOINT_HINTS = ("seat", "seats", "zones", "zone", "sections", "section", "map", "availability", "inventory")
JSON_MIME_HINTS = ("application/json", "application/ld+json", "application/vnd.api+json")
SECTOR_HINT_SELECTORS = [
    "[class*='sector']",
    "[class*='zona']",
    "[class*='zone']",
    "[class*='section']",
    "[data-testid*='sector']",
    "[data-testid*='zone']",
    ".legend li", ".legend .item",
    ".map-legend li", ".map-legend .item",
    "[role='list'] [role='listitem']",
]

def _is_seatmap_like(url: str) -> bool:
    u = url.lower()
    return any(k in u for k in SEATMAP_ENDPOINT_HINTS)

def _content_type_is_json(resp) -> bool:
    try:
        ct = resp.headers.get("content-type", "")
        return any(h in ct for h in JSON_MIME_HINTS)
    except Exception:
        return False

def _parse_availability_from_json(obj) -> list[tuple[str,int]]:
    out = []
    def add(name, avail):
        try:
            avail = int(avail)
        except Exception:
            return
        name = f"{name}".strip()
        if name and avail > 0:
            out.append((name, avail))
    def walk(o):
        if isinstance(o, dict):
            keys = {k.lower(): k for k in o.keys()}
            name = o.get(keys.get("name","name")) or o.get(keys.get("zone","zone")) or o.get(keys.get("section","section")) or o.get(keys.get("sector","sector"))
            avail = None
            for k in ["available","remaining","free","availability","stock","disponibles","cupos"]:
                if k in keys:
                    avail = o[keys[k]]
                    break
            if avail is None and "capacity" in keys and "sold" in keys:
                try:
                    avail = int(o[keys["capacity"]]) - int(o[keys["sold"]])
                except Exception:
                    pass
            if name is not None and avail is not None:
                add(name, avail)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    try:
        walk(obj)
    except Exception:
        pass
    ded = {}
    for n,a in out:
        ded[n] = max(ded.get(n,0), a)
    return sorted(ded.items(), key=lambda x: (-x[1], x[0].lower()))

def _sniff_seatmap_availability(page, wait_ms=5000) -> list[tuple[str,int]]:
    found = []
    def on_response(resp):
        try:
            url = resp.url
            if not _is_seatmap_like(url):
                return
            if resp.status < 200 or resp.status >= 400:
                return
            if not _content_type_is_json(resp):
                return
            txt = resp.text()
            if not txt or len(txt) < 2:
                return
            obj = json.loads(txt)
            cand = _parse_availability_from_json(obj)
            if cand:
                found.extend(cand)
        except Exception:
            return
    page.on("response", on_response)
    try:
        page.wait_for_timeout(wait_ms)
    finally:
        try:
            page.off("response", on_response)
        except Exception:
            pass
    ded = {}
    for n,a in found:
        ded[n] = max(ded.get(n,0), a)
    return sorted(ded.items(), key=lambda x: (-x[1], x[0].lower()))

def _read_sectors_from_dom(page) -> list[tuple[str,int]]:
    out = []
    try:
        nodes = page.locator(", ".join(SECTOR_HINT_SELECTORS))
        n = min(nodes.count(), 200)
        for i in range(n):
            it = nodes.nth(i)
            try:
                txt = (it.inner_text(timeout=250) or "").strip()
            except Exception:
                txt = ""
            if not txt:
                continue
            t = txt.lower()
            if any(k in t for k in ["agotado", "sold out", "sin disponibilidad", "no disponible"]):
                continue
            m = re.search(r"\((\d{1,4})\)", txt)
            avail = int(m.group(1)) if m else 1
            name = re.sub(r"\(\d{1,4}\)", "", txt)
            name = re.sub(r"\s*[-—–]\s*(agotado|sold out|sin disponibilidad).*", "", name, flags=re.I).strip(" -—–\t")
            if name:
                out.append((name, avail))
    except Exception:
        pass
    ded = {}
    for n,a in out:
        ded[n] = max(ded.get(n,0), a)
    return sorted(ded.items(), key=lambda x: (-x[1], x[0].lower()))

def _open_map_for_date(dest_page) -> None:
    triggers = [
        "button:has-text('Ver mapa')", "a:has-text('Ver mapa')",
        "button:has-text('Seleccionar ubicación')",
        "button:has-text('Seleccionar ubicaciones')",
        "button:has-text('Elegir ubicación')",
        "button:has-text('Elegir ubicaciones')",
        "[data-testid*='mapa']", "[data-testid*='seatmap']",
        "button:has-text('Continuar')",
    ]
    for sel in triggers:
        try:
            btn = dest_page.locator(sel).first
            if btn and btn.count() > 0 and btn.is_visible():
                btn.click(timeout=1500, force=True)
                dest_page.wait_for_timeout(400)
        except Exception:
            continue
    try:
        dest_page.wait_for_selector(", ".join(SECTOR_HINT_SELECTORS), timeout=4000, state="visible")
    except Exception:
        pass

def _choose_function_by_label(page, label: str):
    _open_dropdown_if_any(page)
    page.wait_for_timeout(150)
    def _click_visible(locator):
        locator.scroll_into_view_if_needed(timeout=2000)
        locator.wait_for(state="visible", timeout=2500)
        try:
            with page.expect_popup() as pinfo:
                locator.click(timeout=2000)
            popup = pinfo.value
            try: popup.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception: pass
            return popup
        except Exception:
            locator.click(timeout=2000)
            try: page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception: pass
            return page
    # 1) Portal/listbox ARIA
    for sel in [
        ".MuiPopover-root [role='listbox'] [role='option']",
        ".MuiMenu-paper [role='listbox'] [role='option']",
        "[role='listbox'] [role='option']",
        ".MuiPopover-root li.MuiMenuItem-root",
        ".MuiMenu-paper li.MuiMenuItem-root",
    ]:
        try:
            cand = page.locator(sel).filter(has_text=label)
            n = min(cand.count(), 40)
            for i in range(n):
                it = cand.nth(i)
                if it.is_visible():
                    return _click_visible(it)
        except Exception:
            continue
    # 2) Región
    region = None
    try: region = _find_functions_region(page)
    except Exception: pass
    if region:
        cand = region.locator("[role='option'], li, .MuiMenuItem-root, button, a, div").filter(has_text=label)
        n = min(cand.count(), 60)
        for i in range(n):
            it = cand.nth(i)
            if it.is_visible():
                return _click_visible(it)
    # 3) <select> directo
    try:
        if page.locator("select").count() > 0:
            page.select_option("select", label=label)
            page.wait_for_timeout(200)
            for trig in ["button:has-text('Ver entradas')", "a:has-text('Ver entradas')",
                         "button:has-text('Comprar')", "a:has-text('Comprar')"]:
                btn = page.locator(trig).first
                if btn and btn.count() > 0 and btn.is_visible():
                    return _click_visible(btn)
            return page
    except Exception:
        pass
    # 4) Fallback: texto exacto visible
    try:
        anynode = page.get_by_text(label, exact=True)
        n = min(anynode.count(), 20)
        for i in range(n):
            it = anynode.nth(i)
            if it.is_visible():
                return _click_visible(it)
    except Exception:
        pass
    return page

def _extract_sectors_for_date(dest) -> list[tuple[str,int]]:
    _open_map_for_date(dest)
    sectors = _sniff_seatmap_availability(dest, wait_ms=6000)
    if sectors:
        return sectors
    return _read_sectors_from_dom(dest)

def cmd_list_sectors_for_show_index(idx: int):
    url = URLS[idx - 1]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, timeout=60000)
            page.wait_for_load_state("networkidle", timeout=15000)
            title = extract_title(page) or prettify_from_slug(url)
            region = _find_functions_region(page)
            fechas = _gather_dates_in_region(region)
            if not fechas:
                tg_send(f"❓ Sin fechas visibles en {title}{SIGN}", force=True); return
            lines = [f"🧭 <b>{title}</b> — Sectores disponibles:"]
            for f in fechas:
                dest = _choose_function_by_label(page, f)
                try:
                    dest.wait_for_load_state("domcontentloaded", timeout=6000)
                except Exception:
                    pass
                dest.wait_for_timeout(400)
                sectors = _extract_sectors_for_date(dest)
                if sectors:
                    nice = ", ".join([f"{n} ({a})" for n,a in sectors])
                    lines.append(f"{f}: {nice}")
                else:
                    lines.append(f"{f}: —")
                if dest is not page:
                    try: dest.close()
                    except Exception: pass
            tg_send("\n".join(lines) + f"\n{SIGN}", force=True)
        finally:
            try:
                browser.close()
            except Exception:
                pass

# ==============================
# Telegram Polling
# ==============================
def telegram_polling():
    if not (BOT_TOKEN and CHAT_ID):
        print("ℹ️ Telegram polling desactivado (faltan credenciales).")
        return

    # Sugerencias de comandos
    set_bot_commands()

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
                            info = LAST_RESULTS.get(url, {"status":"UNKNOWN","detail":None,"ts":"","title":None})
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

                elif tlow.startswith("/sectores"):
                    m = re.match(r"^/sectores\s+(\d+)\s*$", tlow)
                    if m:
                        idx = int(m.group(1))
                        if 1 <= idx <= len(URLS):
                            tg_send(f"⏳ Buscando sectores por fecha del show #{idx}…{SIGN}", force=True)
                            threading.Thread(
                                target=cmd_list_sectors_for_show_index,
                                args=(idx,),
                                daemon=True
                            ).start()
                        else:
                            tg_send(f"Índice fuera de rango (1–{len(URLS)}).{SIGN}", force=True)
                    else:
                        tg_send(f"Usá: /sectores N (ej: /sectores 2){SIGN}", force=True)

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
                                else:
                                    LAST_RESULTS[url] = {"status": "UNKNOWN", "detail": None, "ts": ts, "title": title}
                                print(f"{title or url} — {LAST_RESULTS[url]['status']} — {ts}")
                finally:
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
        print("⚠️ No hay URLs configuradas.")
    if BOT_TOKEN and CHAT_ID and URLS:
        t = threading.Thread(target=telegram_polling, daemon=True)
        t.start()
        run_monitor()
    else:
        print("⚠️ Faltan variables de entorno TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID o URLs.")
