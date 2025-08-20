import os
import re
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.sync_api import sync_playwright

# ==========================
# CONFIG
# ==========================
URLS = [
    "https://www.allaccess.com.ar/event/airbag",
    "https://www.allaccess.com.ar/event/bad-bunny",
    # agregá más eventos manualmente aquí
]

CHECK_EVERY_SECONDS = int(os.getenv("CHECK_EVERY_SECONDS", "300"))  # 5 min por defecto
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TZ_NAME = "America/Argentina/Buenos_Aires"

# Horario de silencio: no molestar entre 00:00 y 09:00
MUTE_HOURS = range(0, 9)

# ==========================
# HELPERS
# ==========================
def now_local():
    return datetime.now(ZoneInfo(TZ_NAME))

def notify(msg: str):
    """Envia mensaje a Telegram respetando el horario de silencio."""
    now = now_local()
    if now.hour in MUTE_HOURS:
        print(f"[{now}] ⏸️ Silenciado (no enviar): {msg}")
        return
    if BOT_TOKEN and CHAT_ID:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"})
    print(f"[{now}] {msg}")

# ==========================
# MULTIFUNCIÓN: detectar funciones y su disponibilidad
# ==========================
FUNC_SELECTORS = [
    "select",
    "[role='listbox']",
    ".MuiList-root",
    ".aa-event-dates",
    ".event-functions",
]

def _is_soldout_text(t: str) -> bool:
    t = t.lower()
    return any(x in t for x in ["agotado", "sold out", "sem disponibilidade", "sin disponibilidad"])

def _is_disabled_attr(el_handle) -> bool:
    try:
        return bool(el_handle.get_attribute("disabled")) or \
               (el_handle.get_attribute("aria-disabled") in ("true", "1"))
    except Exception:
        return False

def list_functions(page):
    """
    Devuelve lista de funciones: [{"label": str, "element": locator, "soldout": bool}, ...]
    Intenta soportar <select><option>, listas <li>, divs con role=option.
    Siempre intenta abrir el dropdown antes de leer.
    """
    # Intentar abrir el desplegable
    try:
        for root_sel in FUNC_SELECTORS:
            root = page.locator(root_sel)
            if root.count() > 0:
                root.first.click(timeout=1500, force=True)
                page.wait_for_timeout(400)
                break
    except Exception:
        pass

    # 1) <select><option>
    try:
        sel = page.locator("select")
        if sel.count() > 0:
            opt = sel.locator("option")
            out = []
            for i in range(opt.count()):
                o = opt.nth(i)
                lbl = (o.inner_text(timeout=200) or o.get_attribute("label") or "").strip()
                sold = _is_soldout_text(lbl) or _is_disabled_attr(o.element_handle())
                out.append({"label": lbl, "element": o, "soldout": sold, "via": "select"})
            if out:
                return out, sel
    except Exception:
        pass

    # 2) listbox / li / items
    for root_sel in FUNC_SELECTORS[1:]:
        try:
            root = page.locator(root_sel)
            if root.count() == 0:
                continue
            items = root.locator("[role='option'], li, .item, .option")
            if items.count() == 0:
                continue
            out = []
            for i in range(items.count()):
                it = items.nth(i)
                txt = (it.inner_text(timeout=200) or "").strip()
                sold = _is_soldout_text(txt) or _is_disabled_attr(it.element_handle())
                out.append({"label": txt, "element": it, "soldout": sold, "via": "list"})
            if out:
                return out, root
        except Exception:
            continue

    return [], None

def select_function_and_verify(page, item, via):
    """
    Selecciona una función y verifica si realmente hay botón de compra.
    Devuelve True si disponible.
    """
    try:
        if via == "select":
            val = item["element"].get_attribute("value")
            item["element"].evaluate("(o) => o.parentElement.value = o.value")
            item["element"].evaluate("(o) => o.parentElement.dispatchEvent(new Event('change', {bubbles: true}))")
        else:
            item["element"].click(timeout=2000, force=True)

        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(800)

        txt = page.evaluate("() => document.body.innerText").lower()
        has_buy = any(k in txt for k in ["comprar", "comprar entradas", "buy tickets"])
        has_sold = _is_soldout_text(txt)
        return has_buy and not has_sold
    except Exception:
        return False

# ==========================
# CORE: chequeo de estado
# ==========================
def get_visible_status(page):
    txt = page.evaluate("() => document.body.innerText").lower()
    if "agotado" in txt and "comprar" not in txt:
        return "SOLDOUT", None

    # Check multifunción
    funcs, root = list_functions(page)
    disponibles = []

    if funcs:
        for f in funcs:
            label = f["label"] or "(sin etiqueta)"
            if f["soldout"]:
                continue
            ok = select_function_and_verify(page, f, f["via"])
            if ok:
                lbl_clean = re.sub(r"\s+-\s+.*$", "", label).strip()
                disponibles.append(lbl_clean)

        if disponibles:
            unicos = []
            for d in disponibles:
                if d not in unicos:
                    unicos.append(d)
            return "AVAILABLE", ", ".join(unicos)

    # Fallback: buscar botón global
    has_buy = any(k in txt for k in ["comprar", "comprar entradas", "buy tickets"])
    if has_buy:
        return "AVAILABLE", "Disponible (sin fechas específicas)"
    return "SOLDOUT", None

# ==========================
# LOOP PRINCIPAL
# ==========================
def run_monitor():
    last_state = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        while True:
            for url in URLS:
                try:
                    page.goto(url, timeout=60000)
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(1200)

                    state, detail = get_visible_status(page)
                    if last_state.get(url) != state and state == "AVAILABLE":
                        msg = f"✅ ¡Entradas disponibles!\n{url}"
                        if detail:
                            msg += f"\nFechas: {detail}"
                        notify(msg)
                    last_state[url] = state

                except Exception as e:
                    print(f"Error en {url}: {e}")

            time.sleep(CHECK_EVERY_SECONDS)

if __name__ == "__main__":
    run_monitor()
