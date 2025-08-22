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

def _open_map_if_any(page):
    for sel in [
        "button:has-text('Ver mapa')", "a:has-text('Ver mapa')",
        "button:has-text('Seleccionar ubicación')",
        "button:has-text('Elegir ubicación')",
        "[data-testid*='mapa']", "[data-testid*='seatmap']",
        "button:has-text('Continuar')",
    ]:
        try:
            btn = page.locator(sel).first
            if btn and btn.count() > 0 and btn.is_visible():
                btn.click(timeout=1800, force=True)
                page.wait_for_timeout(500)
        except Exception:
            continue
    # Esperas suaves: primero popover/modal, luego svg/canvas/legend
    try:
        page.wait_for_timeout(500)
        page.wait_for_selector("svg, canvas, [class*='legend'], [data-testid*='legend']", timeout=4000)
    except Exception:
        pass

def _get_map_frame(page):
    """
    Devuelve (frame, where) donde buscar el mapa. Si no hay iframe, devuelve (page, "page").
    """
    try:
        # Heurística: iframes con mapa/seat/zone
        frames = [f for f in page.frames if f != page.main_frame]
        for fr in frames:
            url = (fr.url or "").lower()
            if any(k in url for k in ["seat", "map", "zone", "inventory", "ticket"]):
                return fr, "iframe-url"
        # Si no matchea por URL, probamos contenido
        for fr in frames:
            try:
                if fr.query_selector("svg") or fr.query_selector("canvas") or fr.query_selector("[class*='legend']"):
                    return fr, "iframe-dom"
            except Exception:
                continue
    except Exception:
        pass
    return page, "page"

def _list_frames_info(page):
    """Devuelve lista con info corta de frames: idx, url recortada."""
    out = []
    try:
        for i, fr in enumerate(page.frames):
            u = (fr.url or "").strip()
            if len(u) > 140:
                u = u[:140] + "…"
            out.append(f"[{i}] {u}")
    except Exception:
        pass
    return out

NET_HITS_HINTS = ("seat", "seats", "zone", "zones", "map", "inventory", "section", "sections")

def sniff_network_for_map(page, wait_ms=5000, max_show=2):
    #"""Escucha respuestas que parezcan de mapa/sectores y devuelve resumenes."""
    hits = []
    def on_response(resp):
        try:
            url = (resp.url or "")
            low = url.lower()
            if not any(k in low for k in NET_HITS_HINTS):
                return
            ct = (resp.headers.get("content-type") or "").lower()
            size = resp.headers.get("content-length") or "?"
            hits.append((url, ct, size, resp.status))
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
    # Devolvemos los primeros max_show para no hacer ladrillo
    out = []
    for url, ct, size, status in hits[:max_show]:
        short = url if len(url) <= 200 else (url[:200] + "…")
        out.append(f"{status} {short}  [{ct or 'n/a'}]  {size} bytes")
    return out

def _choose_function_by_label(page, label: str):
    #"""Intenta clickear la fecha/función con cierto texto."""
    # abrir el dropdown si corresponde
    _open_dropdown_if_any(page)
    page.wait_for_timeout(150)

    # opciones típicas (listbox/menu)
    for sel in [
        ".MuiPopover-root [role='listbox'] [role='option']",
        ".MuiMenu-paper [role='listbox'] [role='option']",
        "[role='listbox'] [role='option']",
        ".MuiPopover-root li.MuiMenuItem-root",
        ".MuiMenu-paper li.MuiMenuItem-root",
        "[role='option']", "li.MuiMenuItem-root", "li",
    ]:
        try:
            cand = page.locator(sel).filter(has_text=label)
            n = min(cand.count(), 40)
            for i in range(n):
                it = cand.nth(i)
                if it.is_visible():
                    it.scroll_into_view_if_needed(timeout=1500)
                    it.click(timeout=2000, force=True)
                    page.wait_for_timeout(300)
                    return True
        except Exception:
            continue

    # región de funciones como fallback
    try:
        region = _find_functions_region(page)
        if region:
            cand = region.locator("[role='option'], li, .MuiMenuItem-root, button, a, div").filter(has_text=label)
            n = min(cand.count(), 60)
            for i in range(n):
                it = cand.nth(i)
                if it.is_visible():
                    it.scroll_into_view_if_needed(timeout=1500)
                    it.click(timeout=2000, force=True)
                    page.wait_for_timeout(300)
                    return True
    except Exception:
        pass

    # último recurso: texto en la página
    try:
        anynode = page.get_by_text(label, exact=True)
        n = min(anynode.count(), 20)
        for i in range(n):
            it = anynode.nth(i)
            if it.is_visible():
                it.scroll_into_view_if_needed(timeout=1500)
                it.click(timeout=2000, force=True)
                page.wait_for_timeout(300)
                return True
    except Exception:
        pass
    return False

def _open_purchase_for_date(page, label: str):
    """
    Selecciona la fecha 'label', y luego intenta abrir la pantalla de compra.
    Devuelve (dest, opened_new) donde dest es page o popup, y opened_new indica si abrió nueva pestaña.
    """
    _choose_function_by_label(page, label)
    page.wait_for_timeout(250)

    # Botón "Ver entradas" o similar: a veces navega en la misma pestaña, a veces abre popup.
    triggers = [
        "button:has-text('Ver entradas')", "a:has-text('Ver entradas')",
        "button:has-text('Comprar')", "a:has-text('Comprar')",
        "button:has-text('Continuar')", "a:has-text('Continuar')",
    ]

    for sel in triggers:
        try:
            btn = page.locator(sel).first
            if btn and btn.count() > 0 and btn.is_visible():
                # Intentar capturar popup si lo hay
                try:
                    with page.expect_popup() as pinfo:
                        btn.click(timeout=2000, force=True)
                    popup = pinfo.value
                    # darle tiempo a que cargue
                    try: popup.wait_for_load_state("domcontentloaded", timeout=6000)
                    except Exception: pass
                    return popup, True
                except Exception:
                    # si no hubo popup, asumimos navegación en la misma pestaña
                    btn.click(timeout=2000, force=True)
                    try: page.wait_for_load_state("domcontentloaded", timeout=6000)
                    except Exception: pass
                    return page, False
        except Exception:
            continue

    # Fallback: no encontramos botón; nos quedamos en la misma página
    return page, False

def _get_colored_sectors_from_svg(ctx) -> list[str]:
    # ... dentro, reemplazá page.evaluate(...) por ctx.evaluate(...)
    try:
        labels = page.evaluate("""
        () => {
          const parseRGB = (s) => {
            if (!s) return null;
            if (s.startsWith('#')) {
              let c = s.slice(1);
              if (c.length === 3) c = c.split('').map(x=>x+x).join('');
              const r = parseInt(c.slice(0,2),16),
                    g = parseInt(c.slice(2,4),16),
                    b = parseInt(c.slice(4,6),16);
              return [r,g,b];
            }
            const m = s.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/i);
            return m ? [parseInt(m[1]),parseInt(m[2]),parseInt(m[3])] : null;
          };
          const isGrayish = (rgb) => {
            if (!rgb) return true;
            const [r,g,b] = rgb;
            const d1 = Math.abs(r-g), d2 = Math.abs(r-b), d3 = Math.abs(g-b);
            const near = (d1<12 && d2<12 && d3<12); // componentes parecidas
            // gris claro/medio: alto brillo; descartamos blanco puro y transparente
            const bright = (r+g+b)/3;
            return near && bright > 120;
          };

          const svg = document.querySelector('svg');
          if (!svg) return [];
          const out = new Set();

          Array.from(svg.querySelectorAll('*')).forEach(el => {
            const cs = getComputedStyle(el);
            const fill = cs.fill;
            const op   = parseFloat(cs.fillOpacity || '1');
            if (!fill || fill === 'none' || op === 0) return;
            const rgb = parseRGB(fill);
            if (isGrayish(rgb)) return; // descartamos grises

            // capturar algún nombre
            let label = el.getAttribute('aria-label')
                      || el.getAttribute('data-name')
                      || el.getAttribute('title')
                      || '';
            if (!label) {
              const withAria = el.closest('[aria-label]');
              if (withAria) label = withAria.getAttribute('aria-label') || '';
            }
            if (!label) {
              const g = el.closest('g');
              const txt = g && g.querySelector && g.querySelector('text');
              if (txt && txt.textContent) label = txt.textContent.trim();
            }
            if (!label) {
              label = el.id || (el.className && el.className.baseVal) || '';
            }
            if (label) out.add(label.trim());
          });

          return Array.from(out).slice(0, 80);
        }
        """)
        # limpieza mínima de texto
        cleaned = []
        for s in labels or []:
            s = re.sub(r"\\s+", " ", s).strip(" -—–\t")
            if s:
                cleaned.append(s)
        return cleaned
    except Exception:
        return []

def _get_sectors_from_legend(ctx) -> list[str]:
    """
    Cuando no hay SVG, algunas integraciones muestran una leyenda/lista con sectores activos.
    """
    try:
        items = ctx.query_selector_all("[class*='legend'] li, .legend li, [data-testid*='legend'] li, [role='list'] [role='listitem']")
        out = []
        for it in items[:200]:
            try:
                txt = (it.inner_text() or "").strip()
            except Exception:
                txt = ""
            if not txt:
                continue
            low = txt.lower()
            if any(k in low for k in ["agotado", "sold out", "sin disponibilidad", "no disponible"]):
                continue
            # limpiamos contadores "(123)"
            txt = re.sub(r"\(\d{1,4}\)", "", txt).strip(" -—–\t")
            if txt:
                out.append(txt)
        # únicos, orden alfabético
        return sorted(set(out), key=lambda s: s.lower())[:80]
    except Exception:
        return []

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
                        page.goto(url, timeout=60000)
                        page.wait_for_load_state("networkidle", timeout=15000)
                        title = extract_title(page) or prettify_from_slug(url)

                        # Fechas en la landing
                        _open_dropdown_if_any(page)
                        region = _find_functions_region(page)
                        fechas = _gather_dates_in_region(region)

                        # Intentar flujo de compra con la primera fecha (si existe)
                        map_ctx_where = "landing"
                        has_svg = has_canvas = has_legend = False
                        cross_block = False
                        net_hits = []
                        used_date = fechas[0] if fechas else None

                        if used_date:
                            dest, opened_new = _open_purchase_for_date(page, used_date)
                            # Dar chance a que se cargue el sitio de compra real
                            dest.wait_for_timeout(1000)
                            # Oler red mientras se arma
                            net_hits = sniff_network_for_map(dest, wait_ms=2500, max_show=3)
                            # Ver si hay iframe con el mapa dentro de 'dest'
                            ctx, map_ctx_where = _get_map_frame(dest)
                            try:
                                has_svg    = bool(ctx.query_selector("svg"))
                                has_canvas = bool(ctx.query_selector("canvas"))
                                has_legend = bool(ctx.query_selector("[class*='legend'], [data-testid*='legend']"))
                            except Exception:
                                cross_block = True
                            # No cerramos 'dest' si es popup; lo cierra browser.close()

                        soldout = page_has_soldout(page)
                        decision = "AVAILABLE_BY_DATES" if fechas else ("SOLDOUT" if soldout else "UNKNOWN")

                        browser.close()

                    tg_send(
                        "🧪 DEBUG — {title}\n"
                        "URL idx {idx}\n"
                        "decision_hint={decision}\n"
                        "fechas: {fechas}\n"
                        "probe_date: {probe}\n"
                        "map_ctx: {where}  cross_origin_blocked={block}\n"
                        "has_svg={svg}, has_canvas={canv}, has_legend={leg}\n"
                        "net_hits:\n{hits}\n"
                        "{sign}".format(
                            title=title,
                            idx=idx,
                            decision=decision,
                            fechas=", ".join(fechas) if fechas else "-",
                            probe=used_date or "-",
                            where=map_ctx_where,
                            block=str(cross_block).lower(),
                            svg=str(has_svg).lower(),
                            canv=str(has_canvas).lower(),
                            leg=str(has_legend).lower(),
                            hits=("\n".join("• "+h for h in net_hits) if net_hits else "(sin coincidencias)"),
                            sign=SIGN
                        ),
                        force=True
                    )

                elif tlow.startswith("/sectores"):
                    m = re.match(r"^/sectores\s+(\d+)\s*$", tlow)
                    if not m:
                        tg_send(f"Usá: /sectores N (ej: /sectores 2){SIGN}", force=True)
                        continue

                    idx = int(m.group(1))
                    if not (1 <= idx <= len(URLS)):
                        tg_send(f"Índice fuera de rango (1–{len(URLS)}).{SIGN}", force=True)
                        continue

                    url = URLS[idx-1]
                    with sync_playwright() as p:
                        browser = p.chromium.launch(headless=True)
                        page = browser.new_page()
                        page.goto(url, timeout=60000)
                        page.wait_for_load_state("networkidle", timeout=15000)
                        title = extract_title(page) or prettify_from_slug(url)

                        _open_dropdown_if_any(page)
                        region = _find_functions_region(page)
                        fechas = _gather_dates_in_region(region)

                        lines = [f"🧭 <b>{title}</b> — Sectores disponibles:"]
                        if not fechas:
                            lines.append("(sin sectores)")
                        else:
                            for f in fechas:
                                _choose_function_by_label(page, f)
                                _open_map_if_any(page)

                                # ¿mapa en iframe?
                                ctx, where = _get_map_frame(page)

                                # primero intentamos SVG coloreado
                                sectors = _get_colored_sectors_from_svg(ctx)
                                if not sectors:
                                    # probamos leyenda/lista
                                    sectors = _get_sectors_from_legend(ctx)

                                if sectors:
                                    top = ", ".join(sectors[:12])
                                    lines.append(f"{f}: {top}")
                                else:
                                    lines.append(f"{f}: (sin sectores)")

                        try: browser.close()
                        except Exception: pass

                    tg_send("\n".join(lines) + f"\n{SIGN}", force=True)

						
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
