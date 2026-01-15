from __future__ import annotations

import os
import re
import smtplib
import traceback
from dataclasses import dataclass
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import requests
from playwright.sync_api import sync_playwright


TRIBUNALE_URL = "https://www.tribunale.bergamo.it/vendite-giudiziarie_164.html"

REGIONE = "Lombardia"
PROVINCIA = "Bergamo"

COMUNI = [
    "Azzano San Paolo",
    "Stezzano",
    "Zanica",
    "Lallio",
    "Grassobio",
]

ASTA_PER_PAGINA = "50"
DEFAULT_TIMEOUT_MS = 55_000


REGION_MARKERS = {"Lombardia", "Veneto", "Lazio", "Sicilia", "Piemonte", "Toscana", "Puglia"}
ORDINA_MARKERS = {"Convenienza", "Prezzo decrescente", "Prezzo crescente", "Data vendita decrescente"}


@dataclass(frozen=True)
class Notice:
    comune: str
    header: str
    body: str
    direct_link: str
    sale_date: str


# -----------------------------
# Utils
# -----------------------------
def log(msg: str) -> None:
    print(msg, flush=True)


def _strip_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _normalize_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return "https://www.tribunale.bergamo.it" + u
    if u.startswith("www."):
        return "https://" + u
    return u


def _try_extract_real_url_from_tracking(url: str) -> str:
    """
    Decodifica urlsand.esvalabs.com che contiene la URL vera in ?u=
    """
    u = (url or "").strip()
    if not u:
        return u

    try:
        p = urlparse(u)
        host = (p.netloc or "").lower()
        if "esvalabs.com" not in host:
            return u

        qs = parse_qs(p.query or "")
        if "u" not in qs or not qs["u"]:
            return u

        raw = qs["u"][0]
        decoded = raw
        for _ in range(4):
            decoded = unquote(decoded)

        m = re.search(r"(https?://[^ \n\r\t]+)", decoded)
        if not m:
            return u

        candidate = m.group(1).strip()

        # se contiene più "http://", prendi l’ultimo
        http_positions = [m.start() for m in re.finditer(r"https?://", candidate)]
        if len(http_positions) >= 2:
            candidate = candidate[http_positions[-1] :]

        return candidate
    except Exception:
        return u


def _resolve_final_url(url: str, timeout: int = 12) -> str:
    """
    Restituisce URL diretto stabile:
    - decodifica tracking esvalabs
    - segue redirect HTTP
    """
    u = _normalize_url(url)
    if not u:
        return ""

    decoded = _try_extract_real_url_from_tracking(u)
    if decoded and decoded != u:
        return decoded

    try:
        r = requests.get(
            u,
            allow_redirects=True,
            timeout=timeout,
            stream=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        final_url = str(r.url) or u
        r.close()
        return final_url
    except Exception:
        return u


def _extract_date_str(text: str) -> str:
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text or "")
    if not m:
        return "n/d"
    return f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{m.group(3)}"


def _score_link(href: str, ctx: str) -> int:
    h = (href or "").lower().strip()
    c = (ctx or "").lower().strip()

    if not h:
        return -1
    if h.startswith("mailto:") or h.startswith("tel:"):
        return -1
    if "cdn-cgi/l/email-protection" in h:
        return -1

    score = 0

    # PDF è il "link diretto" migliore
    if ".pdf" in h:
        score += 200
        if "avviso" in c:
            score += 80
        if "perizia" in c:
            score += 50
        if "foto" in c:
            score += 10
        if "planimetria" in c:
            score += 10

    # PVP
    if "portalevenditepubbliche.giustizia.it" in h:
        score += 120

    # tracking
    if "esvalabs.com" in h:
        score += 60

    # tribunale domain
    if "tribunale.bergamo.it" in h:
        score += 10

    return score


def _pick_best_direct_link(links: List[Dict[str, str]]) -> str:
    best = ""
    best_score = -1
    for o in links:
        href = o.get("href", "")
        ctx = o.get("ctx", "")
        sc = _score_link(href, ctx)
        if sc > best_score:
            best_score = sc
            best = href

    if not best:
        return TRIBUNALE_URL

    return _resolve_final_url(best)


# -----------------------------
# Playwright helpers
# -----------------------------
def _dismiss_cookie_banner(page) -> None:
    """
    Chiude banner Iubenda che blocca i click (intercepts pointer events).
    """
    try:
        page.wait_for_timeout(250)

        candidates = [
            'button:has-text("Accetta")',
            'button:has-text("Accetto")',
            'button:has-text("Accetta tutto")',
            'button:has-text("Accetta tutti")',
            '[data-iubenda-cs="accept-btn"]',
            ".iubenda-cs-accept-btn",
        ]
        for sel in candidates:
            loc = page.locator(sel).first
            if loc.count() > 0:
                try:
                    loc.click(timeout=1500, force=True)
                    page.wait_for_timeout(250)
                    return
                except Exception:
                    pass

        # Fallback: rimuove overlay
        page.evaluate(
            """
            () => {
              const b = document.querySelector('#iubenda-cs-banner');
              if (b) b.remove();
              const overlays = document.querySelectorAll('.iubenda-cs-overlay');
              overlays.forEach(o => o.remove());
            }
            """
        )
    except Exception:
        pass


def _active_form(page):
    btn = page.locator('input[type="submit"][value="Mostra il risultato"]:visible').first
    btn.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    form = btn.locator("xpath=ancestor::form[1]")
    if form.count() == 0:
        raise RuntimeError("Form non trovato (Mostra il risultato).")
    return form


def _selects_snapshot(form) -> List[Dict]:
    """
    Torna lista di select nel form attivo con:
    - idx: posizione nel DOM (form.querySelectorAll('select'))
    - options: testi option normalizzati
    """
    snap = form.evaluate(
        """
        (f) => {
          const norm = (s) => (s || '').replace(/\\s+/g,' ').trim();
          const sels = Array.from(f.querySelectorAll('select'));
          return sels.map((s, i) => {
            const opts = Array.from(s.options || []).map(o => norm(o.textContent));
            return { idx: i, options: opts };
          });
        }
        """
    )
    return snap or []


def _find_select_idx_by_rule(snapshot: List[Dict], rule_name: str) -> int:
    """
    Identifica i select giusti SOLO guardando le opzioni.
    """
    for item in snapshot:
        opts = set([o for o in item.get("options", []) if o])
        if not opts:
            continue

        # Regione: contiene molte regioni note
        if rule_name == "regione":
            if len(REGION_MARKERS.intersection(opts)) >= 3:
                return int(item["idx"])

        # Aste per pagina: 10/25/50
        if rule_name == "per_pagina":
            if {"10", "25", "50"}.issubset(opts):
                return int(item["idx"])

        # Provincia: contiene Bergamo
        if rule_name == "provincia":
            if "Bergamo" in opts:
                # evita di prendere "Ordina per"
                if len(ORDINA_MARKERS.intersection(opts)) == 0:
                    return int(item["idx"])

        # Comune: contiene almeno uno dei comuni target
        if rule_name == "comune":
            if any(c in opts for c in COMUNI):
                # evita "Ordina per"
                if len(ORDINA_MARKERS.intersection(opts)) == 0:
                    return int(item["idx"])

    return -1


def _wait_until_select_contains(form, select_idx: int, option_text: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
    form.evaluate(
        """
        ([idx, opt, timeout]) => {
          const start = Date.now();
          const norm = (s) => (s || '').replace(/\\s+/g,' ').trim();
          const want = norm(opt).toLowerCase();
          function has() {
            const sel = document.querySelectorAll('select')[idx];
            if (!sel) return false;
            return Array.from(sel.options || []).some(o => norm(o.textContent).toLowerCase() === want);
          }
          return new Promise((resolve, reject) => {
            const tick = () => {
              if (has()) return resolve(true);
              if (Date.now() - start > timeout) return reject("timeout");
              setTimeout(tick, 200);
            };
            tick();
          });
        }
        """,
        [select_idx, option_text, timeout_ms],
    )


def _select_option_by_text(form, select_idx: int, desired_text: str) -> None:
    """
    Seleziona una option usando value reale (robusto contro spazi / NBSP).
    """
    sel = form.locator("select").nth(select_idx)
    sel.wait_for(state="attached", timeout=DEFAULT_TIMEOUT_MS)

    desired_norm = _strip_spaces(desired_text).lower()
    options = sel.locator("option")
    count = options.count()

    best_value = None
    best_label = None

    for i in range(count):
        t = _strip_spaces(options.nth(i).inner_text())
        if not t:
            continue
        if t.lower() == desired_norm:
            best_label = t
            best_value = options.nth(i).get_attribute("value")
            break

    # fallback "contains"
    if best_label is None:
        for i in range(count):
            t = _strip_spaces(options.nth(i).inner_text())
            if desired_norm in t.lower():
                best_label = t
                best_value = options.nth(i).get_attribute("value")
                break

    if best_label is None:
        raise RuntimeError(f"Option '{desired_text}' non trovata nel select idx={select_idx}")

    if best_value:
        sel.select_option(value=best_value)
    else:
        sel.select_option(label=best_label)


def _uncheck_include_past_if_present(page) -> None:
    try:
        cb = page.get_by_label("Includi le aste passate")
        if cb.count() > 0:
            try:
                if cb.is_checked():
                    cb.uncheck()
            except Exception:
                page.evaluate(
                    """
                    () => {
                      const inputs = Array.from(document.querySelectorAll('input[type=checkbox]'));
                      const el = inputs.find(i => (i.closest('label') && i.closest('label').innerText || '').toLowerCase().includes('aste passate'));
                      if (el) el.checked = false;
                    }
                    """
                )
    except Exception:
        pass


def _click_mostra_risultato(page) -> None:
    _dismiss_cookie_banner(page)
    btn = page.locator('input[type="submit"][value="Mostra il risultato"]:visible').first
    btn.click(timeout=DEFAULT_TIMEOUT_MS, force=True)


def _wait_results_or_empty(page) -> None:
    page.wait_for_timeout(800)
    page.wait_for_function(
        """
        () => {
          const t = (document.body && document.body.innerText || '').toLowerCase();
          return t.includes('lotto') || t.includes('nessun') || t.includes('nessuna');
        }
        """,
        timeout=DEFAULT_TIMEOUT_MS,
    )


def _extract_blocks(page) -> List[Dict]:
    """
    Estrae annunci come blocchi delimitati da header:
    "TRIBUNALE DI ... LOTTO ..."
    e raccoglie link presenti nel blocco.
    """
    js = r"""
    () => {
      const norm = (s) => (s || '').replace(/\s+/g,' ').trim();
      const U = (s) => norm(s).toUpperCase();

      const ignored = (el) => {
        if (!el) return true;
        if (el.closest('form')) return true;
        if (el.closest('header')) return true;
        if (el.closest('nav')) return true;
        if (el.closest('footer')) return true;
        return false;
      };

      const isHeader = (el) => {
        if (!el || ignored(el)) return false;
        const t = norm(el.innerText);
        if (!t) return false;
        const tu = U(t);
        if (!tu.startsWith('TRIBUNALE')) return false;
        if (!tu.includes('LOTTO')) return false;
        if (tu.length > 260) return false;
        return true;
      };

      const all = Array.from(document.querySelectorAll('body *'));
      const headers = all.filter(isHeader);
      if (!headers.length) return [];

      const headerSet = new Set(headers);
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);

      const blocks = [];
      let current = null;

      const push = () => {
        if (!current) return;
        const seen = new Set();
        current.links = current.links.filter(o => {
          const h = (o.href || '').trim();
          if (!h) return false;
          if (seen.has(h)) return false;
          seen.add(h);
          return true;
        });
        current.body = norm(current.textParts.join('\n'));
        delete current.textParts;
        blocks.push(current);
      };

      let node;
      while (node = walker.nextNode()) {
        if (ignored(node)) continue;

        if (headerSet.has(node)) {
          push();
          current = { header: norm(node.innerText), textParts: [], links: [] };
          continue;
        }
        if (!current) continue;

        const tag = (node.tagName || '').toUpperCase();
        if (['P','DIV','LI'].includes(tag)) {
          const txt = norm(node.innerText);
          if (txt && !U(txt).startsWith('TRIBUNALE')) {
            if (txt.length <= 3000) current.textParts.push(txt);
          }
        }

        const anchors = node.querySelectorAll ? Array.from(node.querySelectorAll('a[href]')) : [];
        anchors.forEach(a => {
          const href = a.href || a.getAttribute('href') || '';
          if (!href) return;

          let ctx = norm((a.closest('td') && a.closest('td').innerText) ? a.closest('td').innerText : (a.parentElement ? a.parentElement.innerText : a.innerText));
          if (!ctx) ctx = norm(a.innerText || '');

          current.links.push({ href, ctx });
        });
      }

      push();
      return blocks;
    }
    """
    return page.evaluate(js) or []


# -----------------------------
# Core scraper (1 browser, loop comuni)
# -----------------------------
def scrape_all_comuni() -> Dict[str, List[Notice]]:
    headless = (os.getenv("HEADLESS", "1").strip().lower() not in {"0", "false", "no"})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)

        try:
            page.goto(TRIBUNALE_URL, wait_until="domcontentloaded")
            _dismiss_cookie_banner(page)

            # Beni Immobili + Ricerca Generale
            page.get_by_text("Beni Immobili", exact=False).first.click(timeout=DEFAULT_TIMEOUT_MS)
            page.wait_for_timeout(400)
            page.get_by_text("Ricerca Generale", exact=False).first.click(timeout=DEFAULT_TIMEOUT_MS)
            page.wait_for_timeout(600)

            _dismiss_cookie_banner(page)

            form = _active_form(page)

            # Snapshot selects
            snap = _selects_snapshot(form)
            if not snap:
                raise RuntimeError("Nessun <select> trovato nel form attivo.")

            regione_idx = _find_select_idx_by_rule(snap, "regione")
            per_pagina_idx = _find_select_idx_by_rule(snap, "per_pagina")

            if regione_idx < 0:
                raise RuntimeError("Impossibile identificare il select REGIONE dal form attivo (opzioni non riconosciute).")

            # Set regione
            _select_option_by_text(form, regione_idx, REGIONE)
            page.wait_for_timeout(700)

            # Dopo regione, aggiorna snapshot per trovare provincia/comune
            snap2 = _selects_snapshot(form)
            provincia_idx = _find_select_idx_by_rule(snap2, "provincia")
            if provincia_idx < 0:
                # aspetta che appaia Bergamo
                # trova il primo select che inizia a popolarsi con province
                for item in snap2:
                    if item.get("options") and len(item["options"]) > 3:
                        if any(o.endswith("(BG)") or o == "Bergamo" for o in item["options"]):
                            provincia_idx = int(item["idx"])
                            break
            if provincia_idx < 0:
                # retry forte (AJAX)
                page.wait_for_timeout(1500)
                snap2 = _selects_snapshot(form)
                provincia_idx = _find_select_idx_by_rule(snap2, "provincia")

            if provincia_idx < 0:
                raise RuntimeError("Impossibile identificare il select PROVINCIA (Bergamo non disponibile).")

            _select_option_by_text(form, provincia_idx, PROVINCIA)
            page.wait_for_timeout(900)

            # comune idx (dopo provincia)
            snap3 = _selects_snapshot(form)
            comune_idx = _find_select_idx_by_rule(snap3, "comune")
            if comune_idx < 0:
                page.wait_for_timeout(1200)
                snap3 = _selects_snapshot(form)
                comune_idx = _find_select_idx_by_rule(snap3, "comune")

            if comune_idx < 0:
                raise RuntimeError("Impossibile identificare il select COMUNE (opzioni comuni non disponibili).")

            # Aste per pagina
            if per_pagina_idx >= 0:
                try:
                    _select_option_by_text(form, per_pagina_idx, ASTA_PER_PAGINA)
                    page.wait_for_timeout(300)
                except Exception:
                    pass

            _uncheck_include_past_if_present(page)
            _dismiss_cookie_banner(page)

            results: Dict[str, List[Notice]] = {}

            for comune in COMUNI:
                try:
                    log(f"Scraping comune: {comune}")

                    # seleziona comune (aspetta che esista l'opzione)
                    _select_option_by_text(form, comune_idx, comune)
                    page.wait_for_timeout(450)

                    _click_mostra_risultato(page)
                    _wait_results_or_empty(page)

                    page_text = (page.inner_text("body") or "").lower()
                    if ("nessun" in page_text or "nessuna" in page_text) and ("lotto" not in page_text):
                        results[comune] = []
                        log(f" -> trovati: 0 (nessun annuncio)")
                        continue

                    blocks = _extract_blocks(page)
                    notices: List[Notice] = []

                    for b in blocks:
                        header = _strip_spaces(b.get("header") or "")
                        body = _strip_spaces(b.get("body") or "")
                        sale_date = _extract_date_str(header + " " + body)

                        raw_links = b.get("links") or []
                        links: List[Dict[str, str]] = []
                        for o in raw_links:
                            href = _normalize_url(o.get("href", ""))
                            if not href:
                                continue
                            ctx = _strip_spaces(o.get("ctx", ""))
                            links.append({"href": href, "ctx": ctx})

                        direct = _pick_best_direct_link(links)

                        notices.append(
                            Notice(
                                comune=comune,
                                header=header or "Annuncio",
                                body=body[:3500] + ("…" if len(body) > 3500 else ""),
                                direct_link=direct or TRIBUNALE_URL,
                                sale_date=sale_date,
                            )
                        )

                    results[comune] = notices
                    log(f" -> trovati: {len(notices)}")

                except Exception as e:
                    tb = traceback.format_exc()
                    results[comune] = [
                        Notice(
                            comune=comune,
                            header=f"ERRORE scraping per {comune}",
                            body=f"{e}\n\n{tb}",
                            direct_link=TRIBUNALE_URL,
                            sale_date="n/d",
                        )
                    ]
                    log(f" -> ERRORE {comune}: {e}")

            return results

        finally:
            browser.close()


# -----------------------------
# Email
# -----------------------------
def build_email_html(results: Dict[str, List[Notice]]) -> Tuple[str, str]:
    today = date.today().strftime("%d/%m/%Y")
    subject = f"Aste Tribunale Bergamo – Annunci attivi ({today})"

    html = []
    html.append("<html><body style='font-family:Arial,Helvetica,sans-serif'>")
    html.append(f"<h2>{subject}</h2>")
    html.append("<p><b>Fonte:</b> tribunale.bergamo.it → Vendite Giudiziarie</p>")

    total = 0
    for comune in COMUNI:
        lst = results.get(comune, [])
        html.append(f"<h3>{comune} ({len(lst)})</h3>")

        if not lst:
            html.append("<p><i>Nessun annuncio attivo trovato.</i></p>")
            continue

        html.append("<ul>")
        for n in lst:
            total += 1
            html.append("<li style='margin-bottom:18px'>")
            html.append(f"<b>{n.header}</b><br>")
            html.append(f"<span>Data vendita: <b>{n.sale_date}</b></span><br>")
            html.append(
                f"<div style='margin-top:6px'><b>LINK DIRETTO:</b> "
                f"<a href='{n.direct_link}'>{n.direct_link}</a></div>"
            )
            if n.body:
                html.append(f"<div style='margin-top:8px;color:#222'>{n.body}</div>")
            html.append("</li>")
        html.append("</ul>")

    html.append(f"<hr><p>Totale annunci trovati: <b>{total}</b></p>")
    html.append("</body></html>")

    return subject, "".join(html)


def send_email(subject: str, html_body: str) -> None:
    host = os.getenv("SMTP_HOST", "")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER", "")
    pwd = os.getenv("SMTP_PASS", "")
    to_addr = os.getenv("EMAIL_TO", "")

    if not (host and user and pwd and to_addr):
        raise RuntimeError("Credenziali SMTP mancanti (SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/EMAIL_TO).")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(host, port) as server:
        server.starttls()
        server.login(user, pwd)
        server.sendmail(user, [to_addr], msg.as_string())


# -----------------------------
# Main
# -----------------------------
def main() -> int:
    try:
        results = scrape_all_comuni()
    except Exception as e:
        tb = traceback.format_exc()
        results = {
            "ERRORE": [
                Notice(
                    comune="ERRORE",
                    header="ERRORE GENERALE SCRAPING",
                    body=f"{e}\n\n{tb}",
                    direct_link=TRIBUNALE_URL,
                    sale_date="n/d",
                )
            ]
        }

    subject, html = build_email_html(results)

    if os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASS") and os.getenv("EMAIL_TO"):
        send_email(subject, html)
        log("Email inviata.")
    else:
        log("SMTP non configurato: stampo risultati a console.")
        print(subject)
        for c, lst in results.items():
            print(f"\n{c} ({len(lst)})")
            for n in lst[:3]:
                print(" -", n.header)
                print("   LINK:", n.direct_link)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
