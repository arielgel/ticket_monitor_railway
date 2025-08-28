# -*- coding: utf-8 -*-
# RadarEntradas — Detector de AGOTADO / DISPONIBLE con logs por ciclo

import os, re, sys, time, traceback
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
import requests

# ========= Config =========

def _get_env_any(key: str, default: str = "") -> str:
    v = os.getenv(key)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip()

BOT_TOKEN   = _get_env_any("TELEGRAM_BOT_TOKEN", "")
CHAT_ID     = _get_env_any("TELEGRAM_CHAT_ID", "")
URLS_RAW    = _get_env_any("URLS", _get_env_any("MONITORED_URLS", _get_env_any("URL", "")))
CHECK_EVERY = int(_get_env_any("CHECK_EVERY_SECONDS", "300"))   # 5 min por defecto
TZ_NAME     = _get_env_any("TIMEZONE", "America/Argentina/Buenos_Aires")

# No molestar (0–23, hora local)
QUIET_START = int(_get_env_any("QUIET_START", "1"))
QUIET_END   = int(_get_env_any("QUIET_END", "9"))

# Opcional: enviar resumen de disponibles en cada ciclo (por defecto OFF)
NOTIFY_AVAILABLE_EVERY_LOOP = _get_env_any("NOTIFY_AVAILABLE_EVERY_LOOP", "0") == "1"

SIGN = " — Roberto"

if not BOT_TOKEN or not CHAT_ID:
    print("⚠️ Faltan variables de entorno TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID.")
if not URLS_RAW:
    print("⚠️ Faltan URLs (URLS o MONITORED_URLS o URL).")

URLS = [u.strip() for u in URLS_RAW.split(",") if u.strip()]

# Timestamp del último ciclo (se muestra con /last)
LAST_LOOP_AT = None

# ========= Utilidades =========

def now_local():
    try:
        return datetime.now(ZoneInfo(TZ_NAME))
    except Exception:
        return datetime.now()

def in_quiet_hours(dt: datetime) -> bool:
    h = dt.hour
    if QUIET_START == QUIET_END:
        return False
    if QUIET_START < QUIET_END:
        return QUIET_START <= h < QUIET_END
    return h >= QUIET_START or h < QUIET_END

def tg_send(text: str, force: bool = False):
    """Manda mensaje a Telegram (respeta no molestar salvo force=True)."""
    if in_quiet_hours(now_local()) and not force:
        print(f"[quiet] {text[:90]}...", flush=True)
        return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
        requests.post(url, json=data, timeout=15)
    except Exception as e:
        print(f"⚠️ Telegram error: {e}", file=sys.stderr, flush=True)

def prettify_from_slug(url: str) -> str:
    try:
        slug = url.rstrip("/").split("/")[-1]
        return slug.replace("-", " ").upper()
    except Exception:
        return url

def extract_title(page):
    try:
        t = page.title() or ""
        t = re.sub(r"\s*\|.*$", "", t).strip()
        return t if t else None
    except Exception:
        return None

# ========= Perfiles por dominio (AllAccess + Deportick) =========

def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""

VENDOR_PROFILES = {
    # AllAccess
    "www.allaccess.com.ar": {
        "soldout_keywords": ["agotado", "sold out", "sin disponibilidad", "no disponible"],
        "soldout_selectors": [
            "text=/agotado/i", ".badge.soldout", "[data-status='soldout']",
        ],
        "buy_keywords": ["comprar", "comprar entradas", "ver entradas", "continuar", "buy", "tickets"],
        "buy_selectors": [
            "button:has-text('Comprar')", "a:has-text('Comprar')",
            "button:has-text('Comprar entradas')", "a:has-text('Comprar entradas')",
            "button:has-text('Ver entradas')", "a:has-text('Ver entradas')",
            "button:has-text('Continuar')", "a:has-text('Continuar')",
        ],
        "disable_global_date_fallback": False,
    },
    # Deportick (texto AGOTADO al pie, evitar fallback global de fechas)
    "deportick.com": {
        "soldout_keywords": ["agotado", "agotadas"],
        "soldout_selectors": ["text=/agotad/i", ".agotado", ".agotadas"],
        "buy_keywords": ["comprar", "comprar entradas", "quiero mis entradas"],
        "buy_selectors": [
            "button:has-text('Comprar')", "a:has-text('Comprar')",
            "button:has-text('Comprar entradas')", "a:has-text('Comprar entradas')",
        ],
        "disable_global_date_fallback": True,
    },
    "www.deportick.com": {
        "soldout_keywords": ["agotado", "agotadas"],
        "soldout_selectors": ["text=/agotad/i", ".agotado", ".agotadas"],
        "buy_keywords": ["comprar", "comprar entradas", "quiero mis entradas"],
        "buy_selectors": [
            "button:has-text('Comprar')", "a:has-text('Comprar')",
            "button:has-text('Comprar entradas')", "a:has-text('Comprar entradas')",
        ],
        "disable_global_date_fallback": True,
    },
}

# ========= Helpers de UI (fechas) =========

FUNC_TRIGGERS = [
    "button[aria-haspopup='listbox']",
    "[role='combobox']",
    "[data-testid*='select']",
    ".MuiSelect-select",
    ".aa-event-dates",
    ".event-functions",
]

def _open_dropdown_if_any(page):
    for trig in FUNC_TRIGGERS:
        try:
            loc = page.locator(trig).first
            if loc and loc.count() > 0 and loc.is_visible():
                loc.click(timeout=1500, force=True)
                page.wait_for_timeout(250)
        except Exception:
            continue

def _find_functions_region(page):
    for sel in ["select", "[role='listbox']", ".aa-event-dates", ".event-functions"]:
        try:
            r = page.locator(sel).first
            if r and r.count() > 0 and r.is_visible():
                return r
        except Exception:
            continue
    return page  # fallback

def _gather_dates_in_region(region):
    """Devuelve lista de fechas DD/MM/AAAA si aparecen en el bloque; si no, []."""
    dates = set()
    try:
        txt = ""
        try:
            txt = region.inner_text(timeout=500) or ""
        except Exception:
            txt = ""
        for m in re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", txt):
            dd, mm, yy = m
            dates.add(f"{int(dd):02d}/{int(mm):02d}/{yy}")
    except Exception:
        pass
    return sorted(dates)

# --- Filtro de fechas globales (evita “retiro/canje/pick up”) ---

_RETIRO_KEYS = ("retiro", "retirá", "retirar", "retíralo", "canje", "pick up", "punto de retiro", "retirás")

def _dates_from_text_filtered(body_text: str):
    """
    Extrae fechas evitando falsos positivos de secciones de retiro/canje.
    Se filtra por ventana de contexto +/- 80 caracteres alrededor del match.
    """
    dates = set()
    text = body_text or ""
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text):
        dd = int(m.group(1)); mm = int(m.group(2))
        yy = m.group(3)
        # Ventana de contexto
        i0 = max(0, m.start() - 80)
        i1 = min(len(text), m.end() + 80)
        ctx = text[i0:i1].lower()
        if any(k in ctx for k in _RETIRO_KEYS):
            continue
        if yy:
            dates.add(f"{dd:02d}/{mm:02d}/{yy if len(yy)==4 else ('20'+yy)}")
        else:
            dates.add(f"{dd:02d}/{mm:02d}")
    return sorted(dates)

def _gather_dates_anywhere(page):
    """
    Fallback: busca fechas DD/MM(/AAAA) en todo el body, con filtro anti-retiro.
    """
    try:
        body_text = (page.evaluate("() => document.body.innerText") or "")
    except Exception:
        body_text = ""
    return _dates_from_text_filtered(body_text)

# ========= Detección de compra / agotado =========

def _text_contains_any(text: str, keywords: list[str]) -> bool:
    t = (text or "").lower()
    return any(k.lower() in t for k in keywords)

def _detect_buy(page, profile: dict) -> bool:
    # 1) por selectores
    for sel in profile.get("buy_selectors", []):
        try:
            el = page.locator(sel).first
            if el and el.count() > 0 and el.is_visible():
                return True
        except Exception:
            continue
    # 2) por texto en botones/enlaces
    try:
        btns = page.query_selector_all("button, a")
        for b in btns[:500]:
            try:
                t = (b.inner_text() or "").strip().lower()
            except Exception:
                t = ""
            if t and _text_contains_any(t, profile.get("buy_keywords", [])):
                if b.is_visible():
                    return True
    except Exception:
        pass
    return False

def _detect_soldout(page, profile: dict) -> bool:
    # 1) selectores directos
    for sel in profile.get("soldout_selectors", []):
        try:
            loc = page.locator(sel).first
            if loc and loc.count() > 0 and loc.is_visible():
                return True
        except Exception:
            continue
    # 2) texto global
    try:
        body_text = (page.evaluate("() => document.body.innerText") or "").lower()
    except Exception:
        body_text = ""
    if _text_contains_any(body_text, profile.get("soldout_keywords", [])):
        # Evitar falsos positivos muy obvios
        noise = ["+54", "número de dni", "masculino", "femenino", "argentina", "brasil"]
        if not _text_contains_any(body_text, noise):
            return True
    return False

# ========= Núcleo: check_url =========

def check_url(url: str, page):
    """
    Devuelve (fechas, title, hint):
      - fechas: lista 'dd/mm/aaaa' (o dd/mm) si se detectó por UI válida
      - title: título del show
      - hint: 'AVAILABLE_BY_DATES' | 'AVAILABLE_BY_BUY' | 'SOLDOUT' | 'UNKNOWN'
    """
    fechas, title, hint = [], None, "UNKNOWN"

    page.goto(url, timeout=60000)
    page.wait_for_load_state("networkidle", timeout=15000)

    # micro-scroll para destrabar contenido lazy
    try:
        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(200)
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(100)
    except Exception:
        pass

    title = extract_title(page) or prettify_from_slug(url)
    prof = VENDOR_PROFILES.get(_host(url)) or VENDOR_PROFILES.get("www.allaccess.com.ar")

    # 1) fechas (preferimos la región de funciones)
    _open_dropdown_if_any(page)
    region = _find_functions_region(page)
    fechas = _gather_dates_in_region(region)

    # ⚠️ Para algunos vendors (Deportick) deshabilitamos el fallback global
    if not fechas and not prof.get("disable_global_date_fallback", False):
        alt = _gather_dates_anywhere(page)
        if alt:
            fechas = alt

    # 2) flags de compra / agotado
    buy = _detect_buy(page, prof)
    sold = _detect_soldout(page, prof)

    # 3) decisión — prioridad a SOLDOUT si no hay botón de compra
    #    (evita falsos "disponible" por fechas de retiro/canje)
    if sold and not buy:
        hint = "SOLDOUT"
    elif fechas:
        hint = "AVAILABLE_BY_DATES"
    elif buy:
        hint = "AVAILABLE_BY_BUY"
    else:
        hint = "UNKNOWN"

    return fechas, title, hint

# ========= Telegram =========

def list_shows() -> list[str]:
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        for i, url in enumerate(URLS, 1):
            try:
                page.goto(url, timeout=60000)
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                t = extract_title(page) or prettify_from_slug(url)
            except Exception:
                t = prettify_from_slug(url)
            out.append(f"{i}. {t}")
        browser.close()
    return out

def status_for(idx: int | None = None) -> list[str]:
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        items = enumerate(URLS, 1)
        if isinstance(idx, int):
            items = [(idx, URLS[idx-1])]

        for i, url in items:
            try:
                fechas, title, hint = check_url(url, page)
                if hint in ("AVAILABLE_BY_DATES", "AVAILABLE_BY_BUY"):
                    fechas_txt = ", ".join(sorted(fechas)) if fechas else "(sin fecha)"
                    msg = f"✅ **Disponible** — {title}\nFechas: {fechas_txt}\nÚltimo check: {now_local():%Y-%m-%d %H:%M:%S}{SIGN}"
                elif hint == "SOLDOUT":
                    msg = f"⛔ Agotado — {title}\nÚltimo check: {now_local():%Y-%m-%d %H:%M:%S}{SIGN}"
                else:
                    msg = f"❓ Indeterminado — {title}\nÚltimo check: {now_local():%Y-%m-%d %H:%M:%S}{SIGN}"
            except Exception as e:
                msg = f"💥 Error al chequear [{i}] {url}\n{e}{SIGN}"
            results.append(msg)

        browser.close()
    return results

def telegram_polling():
    global LAST_LOOP_AT
    last_update_id = None
    base = f"https://api.telegram.org/bot{BOT_TOKEN}"

    def get_updates(offset=None):
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            return requests.get(f"{base}/getUpdates", params=params, timeout=30).json()
        except Exception:
            return {}

    while True:
        data = get_updates(last_update_id + 1 if last_update_id else None)
        ok = data.get("ok", False) if isinstance(data, dict) else False
        if not ok:
            time.sleep(1)
            continue

        for upd in data.get("result", []):
            last_update_id = upd["update_id"]
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            tlow = text.lower()

            if tlow.startswith("/shows"):
                names = list_shows()
                if names:
                    tg_send("🎯 Monitoreando:\n" + "\n".join(names) + f"\n{SIGN}", force=True)
                else:
                    tg_send("No hay URLs configuradas." + SIGN, force=True)

            elif tlow.startswith("/status"):
                m = re.match(r"^/status\s+(\d+)\s*$", tlow)
                if m:
                    idx = int(m.group(1))
                    if 1 <= idx <= len(URLS):
                        for s in status_for(idx):
                            tg_send(s, force=True)
                    else:
                        tg_send(f"Índice fuera de rango (1–{len(URLS)}).{SIGN}", force=True)
                else:
                    for s in status_for(None):
                        tg_send(s, force=True)

            elif tlow.startswith("/debug"):
                m = re.match(r"^/debug\s+(\d+)\s*$", tlow)
                if not m:
                    tg_send(f"Usá: /debug N (ej: /debug 2){SIGN}", force=True)
                    continue
                idx = int(m.group(1))
                if not (1 <= idx <= len(URLS)):
                    tg_send(f"Índice fuera de rango (1–{len(URLS)}).{SIGN}", force=True)
                    continue
                url = URLS[idx-1]
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    page = browser.new_page()
                    try:
                        fechas, title, hint = check_url(url, page)
                        tg_send(
                            "🧪 DEBUG — {title}\n"
                            "URL idx {idx}\n"
                            "decision_hint={hint}\n"
                            "fechas: {fechas}\n"
                            "{sign}".format(
                                title=title, idx=idx, hint=hint,
                                fechas=", ".join(fechas) if fechas else "-",
                                sign=SIGN
                            ),
                            force=True
                        )
                    except Exception as e:
                        tg_send(f"💥 Error debug: {e}{SIGN}", force=True)
                    finally:
                        browser.close()

            elif tlow.startswith("/sectores"):
                m = re.match(r"^/sectores\s+(\d+)\s*$", tlow)
                if not m:
                    tg_send(f"Usá: /sectores N (ej: /sectores 2){SIGN}", force=True)
                    continue
                idx = int(m.group(1))
                names = list_shows()
                name = names[idx-1].split(". ", 1)[-1] if 1 <= idx <= len(URLS) else f"#{idx}"
                tg_send(f"🧭 {name} — Sectores disponibles:\n(sin sectores)\n{SIGN}", force=True)

            elif tlow.startswith("/last") or tlow.startswith("/ping"):
                ts = LAST_LOOP_AT
                if ts is None:
                    tg_send(f"Aún no hay un ciclo registrado. Esperá el primer loop…{SIGN}", force=True)
                else:
                    tg_send(f"⏱️ Último ciclo: {ts:%Y-%m-%d %H:%M:%S} ({TZ_NAME}){SIGN}", force=True)

        time.sleep(0.4)

# ========= Loop de monitoreo =========

def monitor_loop():
    global LAST_LOOP_AT
    last_snapshot = {}  # url -> 'SOLDOUT'|'AVAILABLE'|'UNKNOWN'
    while True:
        try:
            print(f"[loop] start {now_local():%Y-%m-%d %H:%M:%S} urls={len(URLS)}", flush=True)
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()

                available_summary = []

                for url in URLS:
                    try:
                        fechas, title, hint = check_url(url, page)
                        state = "SOLDOUT" if hint == "SOLDOUT" else ("AVAILABLE" if hint.startswith("AVAILABLE") else "UNKNOWN")
                        prev = last_snapshot.get(url)

                        # Log por URL (para ver que pasó por acá)
                        fechas_txt = ", ".join(fechas) if fechas else "(sin fecha)"
                        print(f"[loop-check] {title} → {state} ({fechas_txt})", flush=True)

                        # Notificación de transición a DISPONIBLE
                        if prev in (None, "SOLDOUT", "UNKNOWN") and state == "AVAILABLE":
                            tg_send(f"✅ ¡Entradas disponibles!\n{title}\nFechas: {fechas_txt}\n{SIGN}", force=True)

                        # Notificación de transición a AGOTADO (suave)
                        if prev == "AVAILABLE" and state == "SOLDOUT":
                            tg_send(f"⛔ Se agotó — {title}{SIGN}", force=False)

                        if state == "AVAILABLE":
                            available_summary.append(f"- {title} — {fechas_txt}")

                        last_snapshot[url] = state

                    except Exception as e:
                        print(f"⚠️ Error check {url}: {e}", flush=True)
                        traceback.print_exc()

                # Resumen opcional por ciclo
                if NOTIFY_AVAILABLE_EVERY_LOOP and available_summary:
                    tg_send(
                        "✅ Disponibles ahora (" + str(len(available_summary)) + "):\n"
                        + "\n".join(available_summary)
                        + f"\nÚltimo check: {now_local():%Y-%m-%d %H:%M:%S}{SIGN}",
                        force=True
                    )

                browser.close()

        except Exception as e:
            print(f"💥 Loop error: {e}", flush=True)
        finally:
            LAST_LOOP_AT = now_local()
            print(f"[loop] done  {LAST_LOOP_AT:%Y-%m-%d %H:%M:%S} — sleeping {CHECK_EVERY}s", flush=True)
            time.sleep(max(30, CHECK_EVERY))

# ========= Arranque =========

if __name__ == "__main__":
    mode = _get_env_any("MODE", "both").lower()   # both | bot | monitor
    print(f"[RadarEntradas] mode={mode} urls={len(URLS)} tz={TZ_NAME} quiet={QUIET_START}-{QUIET_END}", flush=True)

    if mode in ("bot", "both"):
        import threading
        th = threading.Thread(target=telegram_polling, daemon=True)
        th.start()

    if mode in ("monitor", "both"):
        monitor_loop()
