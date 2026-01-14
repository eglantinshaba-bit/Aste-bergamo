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
DEFAULT_TIMEOUT_MS = 45_000


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
    if "pdf-icon.png" in h:
        return -1

    score = 0
    if ".pdf" in h:
        score += 200
        if "avviso" in c:
            score += 60
        if "perizia" in c:
            score += 40
        if "ordinanza" in c:
            score += 20

    if "portalevenditepubbliche.giustizia.it" in h:
        score += 120

    if "esvalabs.com" in h:
        score += 80

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
        page.wait_for_timeout(300)

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


def _find_select_index_by_field_text(form, field_label: str) -> int:
    """
    Trova il <select> giusto in base al testo contenuto nel wrapper vicino.
    Serve per NON prendere "Ordina per" al posto di "Comune".
    """
    return form.evaluate(
        """
        (f, fieldLabel) => {
          const norm = (s) => (s || '').replace(/\\s+/g,' ').trim().toLowerCase();
          const label = norm(fieldLabel);

          const selects = Array.from(f.querySelectorAll('select')).filter(s => {
            const style = window.getComputedStyle(s);
            const visible = style && style.display !== 'none' && style.visibility !== 'hidden' && s.offsetParent !== null;
            return visible;
          });

          for (let i=0; i<selects.length; i++) {
            const s = selects[i];
            const wrap = s.closest('div') || s.closest('td') || s.parentElement;
            const txt = norm(wrap ? wrap.innerText : '');
            // deve contenere la label e NON essere "Ordina per"
            if (txt.includes(label)) return i;
          }
          return -1;
        }
        """,
        field_label,
    )


def _select_field_option(form, field_label: str, desired_value: str) -> None:
    idx = _find_select_index_by_field_text(form, field_label)
    if idx < 0:
        raise RuntimeError(f"Impossibile trovare il select del campo '{field_label}' nel form attivo.")

    sel = form.locator("select:visible").nth(idx)
    sel.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)

    # Leggi opzioni reali
    options = sel.locator("option").all_text_contents()
    options_clean = [_strip_spaces(x) for x in options if _strip_spaces(x)]

    desired_norm = _strip_spaces(desired_value).lower()

    # Match esatto
    for o in options_clean:
        if o.lower() == desired_norm:
            sel.select_option(label=o)
            return

    # Match "contiene"
    for o in options_clean:
        if desired_norm in o.lower():
            sel.select_option(label=o)
            return

    # Fallback token overlap
    target_tokens = set(desired_norm.split())
    best = None
    best_score = -1
    for o in options_clean:
        ot = set(o.lower().split())
        sc = len(target_tokens.intersection(ot))
        if sc > best_score:
            best_score = sc
            best = o

    if not best:
        raise RuntimeError(f"Impossibile selezionare '{desired_value}' su '{field_label}'. Opzioni viste: {options_clean[:15]}")

    sel.select_option(label=best)


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


def _setup_base_filters(page) -> None:
    page.goto(TRIBUNALE_URL, wait_until="domcontentloaded")
    _dismiss_cookie_banner(page)

    # Beni Immobili + Ricerca Generale
    page.get_by_text("Beni Immobili", exact=False).first.click(timeout=DEFAULT_TIMEOUT_MS)
    page.wait_for_timeout(300)
    page.get_by_text("Ricerca Generale", exact=False).first.click(timeout=DEFAULT_TIMEOUT_MS)
    page.wait_for_timeout(500)

    _dismiss_cookie_banner(page)

    form = _active_form(page)

    _select_field_option(form, "Regione", REGIONE)
    page.wait_for_timeout(600)

    _select_field_option(form, "Provincia", PROVINCIA)
    page.wait_for_timeout(900)

    # Comune verrà scelto per ogni run
    try:
        _select_field_option(form, "Aste per pagina", ASTA_PER_PAGINA)
        page.wait_for_timeout(300)
    except Exception:
        pass

    _uncheck_include_past_if_present(page)
    _dismiss_cookie_banner(page)


def _click_mostra_risultato(page) -> None:
    _dismiss_cookie_banner(page)
    btn = page.locator('input[type="submit"][value="Mostra il risultato"]:visible').first
    btn.click(timeout=DEFAULT_TIMEOUT_MS, force=True)


def _wait_results_or_empty(page) -> None:
    page.wait_for_timeout(800)
    # Aspetta o un annuncio (LOTTO) o messaggio "nessun"
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
        if (tu.length > 250) return false;
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
        // dedup link href
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
            if (txt.length <= 2500) current.textParts.push(txt);
          }
        }

        // link: prendi href + contesto
        const anchors = node.querySelectorAll ? Array.from(node.querySelectorAll('a[href]')) : [];
        anchors.forEach(a => {
          const href = a.href || a.getAttribute('href') || '';
          if (!href) return;
          const ctx = norm(a.innerText || a.title || a.getAttribute('aria-label') || (a.parentElement ? a.parentElement.innerText : ''));
          const onclick = a.getAttribute('onclick') || '';
          current.links.push({ href, ctx, onclick });
        });
      }

      push();
      return blocks;
    }
    """
    return page.evaluate(js) or []


def scrape_for_comune(comune: str) -> List[Notice]:
    headless = (os.getenv("HEADLESS", "1").strip().lower() not in {"0", "false", "no"})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)

        try:
            _setup_base_filters(page)
            form = _active_form(page)

            # Seleziona Comune (quello vero)
            _select_field_option(form, "Comune", comune)
            page.wait_for_timeout(500)

            _click_mostra_risultato(page)
            _wait_results_or_empty(page)

            page_text = (page.inner_text("body") or "").lower()
            if "nessun" in page_text and "lotto" not in page_text:
                return []

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

                    # se href è solo l’icona pdf, prova a tirar fuori url dal onclick
                    if "pdf-icon.png" in href.lower():
                        onclick = (o.get("onclick") or "")
                        m = re.search(r"(https?://[^'\" ]+)", onclick)
                        if m:
                            href = m.group(1).strip()
                        else:
                            m2 = re.search(r"(/[^'\" ]+\.pdf[^'\" ]*)", onclick)
                            if m2:
                                href = "https://www.tribunale.bergamo.it" + m2.group(1).strip()

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

            return notices

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
            html.append(f"<div style='margin-top:6px'><b>LINK DIRETTO:</b> <a href='{n.direct_link}'>{n.direct_link}</a></div>")
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
    results: Dict[str, List[Notice]] = {}

    for comune in COMUNI:
        try:
            log(f"Scraping comune: {comune}")
            results[comune] = scrape_for_comune(comune)
            log(f" -> trovati: {len(results[comune])}")
        except Exception as e:
            tb = traceback.format_exc()
            log(f"ERRORE scraping {comune}: {e}")
            results[comune] = [
                Notice(
                    comune=comune,
                    header=f"ERRORE scraping per {comune}",
                    body=f"{e}\n\n{tb}",
                    direct_link=TRIBUNALE_URL,
                    sale_date="n/d",
                )
            ]

    subject, html = build_email_html(results)

    # invio mail (se configurato)
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
