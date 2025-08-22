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

                        # Fechas visibles
                        _open_dropdown_if_any(page)
                        region = _find_functions_region(page)
                        fechas = _gather_dates_in_region(region)

                        lines = [f"🧭 <b>{title}</b> — Sectores disponibles:"]
                        if not fechas:
                            lines.append("(sin sectores)")
                        else:
                            for f in fechas:
                                # elegir fecha y abrir mapa
                                _choose_function_by_label(page, f)
                                _open_map_if_any(page)
                                # leer sectores coloreados
                                sectors = _get_colored_sectors_from_svg(page)
                                if sectors:
                                    # Mostramos hasta 12 para no hacer chorizo
                                    top = ", ".join(sectors[:12])
                                    lines.append(f"{f}: {top}")
                                else:
                                    lines.append(f"{f}: (sin sectores)")

                        try:
                            browser.close()
                        except Exception:
                            pass

                    tg_send("\n".join(lines) + f"\n{SIGN}", force=True)
