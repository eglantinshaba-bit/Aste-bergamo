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
DEFAULT_TIMEOUT_MS = 60_000

REGION_MARKERS = {
    "Lombardia", "Veneto", "Lazio", "Sicilia", "Piemonte", "Toscana", "Puglia",
    "Emilia-Romagna", "Campania", "Liguria", "Marche", "Calabria", "Sardegna"
}

ORDINA_MARKERS = {
    "Convenienza", "Prezzo decrescente", "Prezzo crescente",
    "Data vendita decrescente", "Data vendita crescente", "Data pubblicazione"
}


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


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = s.replace(".", " ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


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
        http_positions = [m.start() for m in re.finditer(r"https?://", candidate)]
        if len(http_positions) >= 2:
            candidate = candidate[http_positions[-1] :]

        return candidate
    except Exception:
        return u


def _resolve_final_url(url: str, timeout: int = 12) -> str:
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
    # PDF = migliore "link diretto"
    if ".pdf" in h:
        score += 200
        if "avviso" in c:
            score += 80
        if "perizia" in c:
            score += 50
        if "ordinanza" in c:
            score += 20

    # PVP
    if "portalevenditepubbliche.giustizia.it" in h:
        score += 120

    # tracking
    if "esvalabs.com" in h:
        score += 60

    # tribunale
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


def _snapshot_selects(form) -> List[Dict]:
    """
    Snapshot di tutti i select nel form attivo (visible o no) con opzioni.
    """
    return form.evaluate(
        """
        (f) => {
          const norm = (s) => (s || '').replace(/\\s+/g,' ').trim();
          const sels = Array.from(f.querySelectorAll('select'));
          return sels.map((s, idx) => {
            const style = window.getComputedStyle(s);
            const visible = style.display !== 'none' && style.visibility !== 'hidden' && s.offsetParent !== null;
            const opts = Array.from(s.options || []).map(o => norm(o.textContent));
            return { idx, visible, opts };
          });
        }
        """
    ) or []


def _find_select_idx_region(snapshot: List[Dict]) -> int:
    for it in snapshot:
        opts = set([o for o in it.get("opts", []) if o])
        if len(REGION_MARKERS.intersection(opts)) >= 4:
            return int(it["idx"])
    return -1


def _find_select_idx_provincia(snapshot: List[Dict]) -> int:
    for it in snapshot:
        opts = set([o for o in it.get("opts", []) if o])
        if "Bergamo" in opts and len(ORDINA_MARKERS.intersection(opts)) == 0:
            return int(it["idx"])
    return -1


def _find_select_idx_comune(snapshot: List[Dict]) -> int:
    for it in snapshot:
        opts = set([o for o in it.get("opts", []) if o])
        if any(c in opts for c in COMUNI) and len(ORDINA_MARKERS.intersection(opts)) == 0:
            return int(it["idx"])
    return -1


def _find_select_idx_per_pagina(snapshot: List[Dict]) -> int:
    for it in snapshot:
        opts = set([o for o in it.get("opts", []) if o])
        if {"10", "25", "50"}.issubset(opts):
            return int(it["idx"])
    return -1


def _choose_option_value(select_locator, desired: str) -> Tuple[str, str]:
    """
    Ritorna (value,label) migliore per una option che matcha desired anche se il testo è tipo "Stezzano (BG)".
    """
    desired_n = _norm(desired)

    opts = select_locator.locator("option")
    n = opts.count()

    best_val = ""
    best_label = ""
    best_score = -1

    for i in range(n):
        lab = _strip_spaces(opts.nth(i).inner_text())
        if not lab:
            continue
        val = opts.nth(i).get_attribute("value") or ""

        lab_n = _norm(lab)

        # punteggio: match esatto / contains / overlap token
        score = 0
        if lab.lower() == desired.lower():
            score = 1000
        elif desired_n == lab_n:
            score = 900
        elif desired_n in lab_n:
            score = 700
        else:
            target_tokens = set(desired_n.split())
            lab_tokens = set(lab_n.split())
            score = 100 + len(target_tokens.intersection(lab_tokens)) * 10

        # preferisci label più corta se score uguale
        if score > best_score or (score == best_score and best_label and len(lab) < len(best_label)):
            best_score = score
            best_val = val
            best_label = lab

    if best_score < 150:
        raise RuntimeError(f"Option '{desired}' non trovata in questo select.")

    return best_val, best_label


def _select_value(form, select_idx: int, desired_text: str) -> str:
    sel = form.locator("select").nth(select_idx)
    sel.wait_for(state="attached", timeout=DEFAULT_TIMEOUT_MS)
    value, label = _choose_option_value(sel, desired_text)
    if value:
        sel.select_option(value=value)
    else:
        sel.select_option(label=label)
    return label


def _click_mostra_risultato(page) -> None:
    _dismiss_cookie_banner(page)
    btn = page.locator('input[type="submit"][value="Mostra il risultato"]:visible').first
    btn.click(timeout=DEFAULT_TIMEOUT_MS, force=True)


def _wait_results_or_empty(page) -> None:
    page.wait_for_timeout(700)
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
    Blocco annuncio = header che contiene 'TRIBUNALE' e 'LOTTO'
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
        if (tu.length > 280) return false;
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
            if (txt.length <= 4000) current.textParts.push(txt);
          }
        }

        const anchors = node.querySelectorAll ? Array.from(node.querySelectorAll('a[href]')) : [];
        anchors.forEach(a => {
          const href = a.href || a.getAttribute('href') || '';
          if (!href) return;
          const ctx = norm((a.closest('td') && a.closest('td').innerText) ? a.closest('td').innerText : (a.parentElement ? a.parentElement.innerText : a.innerText));
          current.links.push({ href, ctx });
        });
      }

      push();
      return blocks;
    }
    """
    return page.evaluate(js) or []


# -----------------------------
# Scrape 1 comune (pagina fresca)
# -----------------------------
def scrape_single_comune(page, comune: str) -> List[Notice]:
    page.goto(TRIBUNALE_URL, wait_until="domcontentloaded")
    _dismiss_cookie_banner(page)

    # Beni Immobili + Ricerca Generale
    page.get_by_text("Beni Immobili", exact=False).first.click(timeout=DEFAULT_TIMEOUT_MS)
    page.wait_for_timeout(400)
    page.get_by_text("Ricerca Generale", exact=False).first.click(timeout=DEFAULT_TIMEOUT_MS)
    page.wait_for_timeout(700)
    _dismiss_cookie_banner(page)

    form = _active_form(page)

    # Trova indici select dal contenuto opzioni
    snap = _snapshot_selects(form)

    regione_idx = _find_select_idx_region(snap)
    if regione_idx < 0:
        raise RuntimeError("Select REGIONE non identificato (opzioni regioni non trovate).")

    # Seleziona regione
    selected_regione = _select_value(form, regione_idx, REGIONE)
    page.wait_for_timeout(800)

    snap2 = _snapshot_selects(form)
    provincia_idx = _find_select_idx_provincia(snap2)
    if provincia_idx < 0:
        page.wait_for_timeout(1400)
        snap2 = _snapshot_selects(form)
        provincia_idx = _find_select_idx_provincia(snap2)

    if provincia_idx < 0:
        raise RuntimeError("Select PROVINCIA non identificato (Bergamo non trovato).")

    selected_prov = _select_value(form, provincia_idx, PROVINCIA)
    page.wait_for_timeout(1200)

    snap3 = _snapshot_selects(form)
    comune_idx = _find_select_idx_comune(snap3)
    if comune_idx < 0:
        page.wait_for_timeout(1500)
        snap3 = _snapshot_selects(form)
        comune_idx = _find_select_idx_comune(snap3)

    if comune_idx < 0:
        raise RuntimeError("Select COMUNE non identificato (lista comuni non caricata).")

    # per pagina
    per_pag_idx = _find_select_idx_per_pagina(snap3)
    if per_pag_idx >= 0:
        try:
            _select_value(form, per_pag_idx, ASTA_PER_PAGINA)
        except Exception:
            pass

    # seleziona comune
    selected_comune = _select_value(form, comune_idx, comune)
    log(f"[OK] Comune richiesto='{comune}' selezionato='{selected_comune}' | Regione='{selected_regione}' Provincia='{selected_prov}'")
    page.wait_for_timeout(500)

    _click_mostra_risultato(page)
    _wait_results_or_empty(page)

    txt = (page.inner_text("body") or "").lower()
    if ("nessun" in txt or "nessuna" in txt) and ("lotto" not in txt):
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
    results: Dict[str, List[Notice]] = {}

    headless = (os.getenv("HEADLESS", "1").strip().lower() not in {"0", "false", "no"})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)

        try:
            for comune in COMUNI:
                page = browser.new_page()
                page.set_default_timeout(DEFAULT_TIMEOUT_MS)

                try:
                    log(f"=== COMUNE: {comune} ===")
                    results[comune] = scrape_single_comune(page, comune)
                    log(f" -> trovati: {len(results[comune])}")
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
                finally:
                    page.close()

        finally:
            browser.close()

    subject, html = build_email_html(results)

    if os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASS") and os.getenv("EMAIL_TO"):
        send_email(subject, html)
        log("Email inviata.")
    else:
        log("SMTP non configurato: stampo risultati a console.")
        print(subject)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
