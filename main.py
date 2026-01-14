"""
Aste Bergamo - Scraper "Vendite Giudiziarie" (Tribunale di Bergamo)

Obiettivo:
- Aprire https://www.tribunale.bergamo.it/vendite-giudiziarie_164.html
- Selezionare:
  - Beni Immobili
  - Ricerca Generale
  - Regione: Lombardia
  - Provincia: Bergamo
  - Comune: (lista target)
- Estrarre TUTTI gli annunci attivi e inviare una mail con:
  - Comune
  - Titolo annuncio
  - Testo annuncio (sintesi)
  - Link diretto (priorità PDF Avviso/Perizia)
  - (opzionale) altri link

Nota:
Il sito usa un banner cookie (Iubenda) che può bloccare i click.
Lo script lo gestisce in automatico.

Repo-ready per GitHub Actions.
"""

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

COMUNI_TARGET = [
    "Azzano San Paolo",
    "Stezzano",
    "Zanica",
    "Lallio",
    "Grassobio",
]

ASTA_PER_PAGINA = "50"  # 10 / 25 / 50
DEFAULT_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class Notice:
    comune: str
    header: str
    body: str
    direct_link: str
    links: Tuple[str, ...]
    sale_date: Optional[date]


# -----------------------------
# Utils
# -----------------------------
def log(msg: str) -> None:
    print(msg, flush=True)


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


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
    Decodifica i link 'urlsand.esvalabs.com' che includono la vera URL nel parametro 'u='.
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

        if "portalevenditepubbliche.giustizia.it" in candidate:
            return candidate

        return candidate
    except Exception:
        return u


def _resolve_final_url(url: str, timeout: int = 10) -> str:
    """
    Restituisce un link "diretto" e stabile.
    1) Se è tracking esvalabs -> decodifica e restituisce l'URL reale (senza fare richieste).
    2) Altrimenti segue redirect HTTP.
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


def _looks_like_pdf(url: str, text: str) -> bool:
    hl = (url or "").lower()
    tl = (text or "").lower()

    if ".pdf" in hl:
        return True
    if "pdf" in hl and any(k in hl for k in ["file=", "download", "doc", "alleg", "attach"]):
        return True
    if any(k in tl for k in ["avviso vendita", "avviso", "perizia", "autorizzazione gd"]):
        return True
    return False


def _score_link(href: str, text: str) -> int:
    h = (href or "").strip()
    t = (text or "").strip()
    if not h:
        return -1

    hl = h.lower()
    tl = t.lower()

    if hl.startswith("mailto:") or hl.startswith("tel:"):
        return -1
    if any(hl.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"]):
        return -1

    score = 0

    # 1) PDF (Avviso/Perizia) = top
    if _looks_like_pdf(h, t):
        score += 120
        if "avviso" in tl:
            score += 25
        if "perizia" in tl:
            score += 15
        if "autorizzazione" in tl:
            score += 10

    # 2) PVP
    if "portalevenditepubbliche.giustizia.it" in hl:
        score += 90

    # 3) tracking (lo decodifichiamo)
    if "esvalabs.com" in hl:
        score += 60

    # 4) dettagli / lotto
    if any(k in tl for k in ["dettaglio", "scheda", "procedura", "lotto"]):
        score += 20
    if any(k in hl for k in ["vendite", "asta", "lotto", "procedura"]):
        score += 15

    # 5) dominio tribunale
    if "tribunale.bergamo.it" in hl:
        score += 10

    return score


def _pick_direct_link(link_objs: List[Dict[str, str]]) -> str:
    best_href = ""
    best_score = -1
    for obj in link_objs:
        href = (obj.get("href") or "").strip()
        text = (obj.get("text") or "").strip()
        sc = _score_link(href, text)
        if sc > best_score:
            best_score = sc
            best_href = href
    return best_href or TRIBUNALE_URL


def _flatten_links(link_objs: List[Dict[str, str]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for o in link_objs:
        h = _normalize_url(o.get("href") or "")
        if not h:
            continue
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


# -----------------------------
# Playwright helpers
# -----------------------------
def _dismiss_cookie_banner(page) -> None:
    """
    Accetta/chiude Iubenda (cookie banner) + fallback JS remove overlay.
    """
    try:
        page.wait_for_timeout(600)

        candidates = [
            'button:has-text("Accetta")',
            'button:has-text("Accetto")',
            'button:has-text("Accetta tutto")',
            'button:has-text("Accetta tutti")',
            'button:has-text("Accept")',
            'button:has-text("Allow")',
            '[data-iubenda-cs="accept-btn"]',
            ".iubenda-cs-accept-btn",
            "#iubenda-cs-banner button",
        ]

        for sel in candidates:
            loc = page.locator(sel).first
            if loc.count() > 0:
                try:
                    loc.click(timeout=1500)
                    page.wait_for_timeout(350)
                    return
                except Exception:
                    pass

        page.evaluate(
            """
            () => {
              const el = document.querySelector('#iubenda-cs-banner');
              if (el) el.remove();
              const o = document.querySelector('.iubenda-cs-overlay');
              if (o) o.remove();
            }
            """
        )
    except Exception:
        pass


def _smart_select_option(page, label_text: str, value_text: str) -> None:
    """
    Seleziona value_text in un <select> associato a label_text.
    Fallback: cerca il select che contiene l’opzione richiesta.
    """
    value_text = value_text.strip()

    # 1) prova per label
    try:
        sel = page.get_by_label(label_text)
        if sel.count() > 0:
            sel.select_option(label=value_text, timeout=DEFAULT_TIMEOUT_MS)
            return
    except Exception:
        pass

    # 2) cerca tra tutti i select visibili
    selects = page.locator("select:visible")
    for i in range(selects.count()):
        s = selects.nth(i)
        try:
            options = s.locator("option").all_text_contents()
            if any(value_text.lower() == (o or "").strip().lower() for o in options):
                s.select_option(label=value_text, timeout=DEFAULT_TIMEOUT_MS)
                return
        except Exception:
            continue

    raise RuntimeError(f"Impossibile trovare <select> visibile con opzioni [{value_text}]")


def _click_mostra_risultato(page) -> None:
    btn = page.locator('input[type="submit"][value="Mostra il risultato"]').first
    btn.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    _dismiss_cookie_banner(page)
    btn.click(timeout=DEFAULT_TIMEOUT_MS, force=True)


def _wait_results_loaded(page) -> None:
    page.wait_for_timeout(500)
    page.wait_for_function(
        """
        () => {
          const txt = document.body && document.body.innerText ? document.body.innerText : '';
          return txt.toUpperCase().includes('TRIBUNALE DI ');
        }
        """,
        timeout=DEFAULT_TIMEOUT_MS,
    )


def _extract_blocks_from_page(page) -> List[Dict]:
    """
    Raggruppa blocchi da "TRIBUNALE DI ..." fino al successivo.
    Raccoglie:
    - <a href>
    - URL dentro onclick / data-url / data-href
    """
    js = r"""
    () => {
      const root = document.body;
      if (!root) return [];

      const norm = (s) => (s || '').replace(/\s+/g,' ').trim();

      const extractUrlsFromString = (s) => {
        const out = [];
        if (!s) return out;
        // URL complete e path relativi PDF
        const re = /((https?:\/\/)[^'"\s<>]+)|((\/)[^'"\s<>]+(\.pdf|\.PDF))/g;
        let m;
        while ((m = re.exec(s)) !== null) {
          const u = m[0];
          if (!u) continue;
          out.push(u);
        }
        if (s.includes('portalevenditepubbliche.giustizia.it')) {
          out.push('https://www.portalevenditepubbliche.giustizia.it');
        }
        return out;
      };

      const isTitle = (el) => {
        if (!el) return false;
        const t = norm(el.innerText);
        if (!t) return false;
        const tu = t.toUpperCase();
        if (!tu.startsWith('TRIBUNALE DI ')) return false;
        if (t.length > 140) return false;
        return true;
      };

      const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
      const titles = [];
      let node;
      while (node = walker.nextNode()) {
        if (isTitle(node)) titles.push(node);
      }
      if (!titles.length) return [];

      const titleSet = new Set(titles);

      const walker2 = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
      const blocks = [];
      let current = null;

      const pushCurrent = () => {
        if (!current) return;
        const body = norm(current.textParts.join('\n'));
        blocks.push({ header: current.header, body, links: current.links });
      };

      while (node = walker2.nextNode()) {

        if (titleSet.has(node)) {
          pushCurrent();
          current = { header: norm(node.innerText), textParts: [], links: [] };
          continue;
        }

        if (!current) continue;

        // <a href>
        if ((node.tagName || '').toUpperCase() === 'A') {
          const href = node.href || node.getAttribute('href') || '';
          if (href) {
            current.links.push({ href: href, text: norm(node.innerText) || norm(node.getAttribute('title')) });
          }
          const onclick = node.getAttribute('onclick') || '';
          extractUrlsFromString(onclick).forEach(u => current.links.push({ href: u, text: norm(node.innerText) || 'Allegato' }));
        }

        // onclick / data-url anche su altri elementi
        const onclick2 = node.getAttribute && node.getAttribute('onclick');
        if (onclick2) {
          extractUrlsFromString(onclick2).forEach(u => current.links.push({ href: u, text: 'Allegato' }));
        }
        const dataHref = node.getAttribute && (node.getAttribute('data-href') || node.getAttribute('data-url') || node.getAttribute('data-file'));
        if (dataHref) {
          current.links.push({ href: dataHref, text: 'Allegato' });
        }

        // testo utile
        const tag = (node.tagName || '').toUpperCase();
        if (['P','LI'].includes(tag)) {
          const t = norm(node.innerText);
          if (t && !t.toUpperCase().startsWith('TRIBUNALE DI ')) current.textParts.push(t);
        }
      }

      pushCurrent();

      // dedup links
      blocks.forEach(b => {
        const seen = new Set();
        b.links = (b.links || []).filter(o => {
          const h = (o.href || '').trim();
          if (!h) return false;
          if (seen.has(h)) return false;
          seen.add(h);
          return true;
        });
      });

      return blocks;
    }
    """
    blocks: List[Dict] = page.evaluate(js)
    return blocks or []


def _parse_sale_date(text: str) -> Optional[date]:
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text or "")
    if not m:
        return None
    dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(yyyy, mm, dd)
    except Exception:
        return None


def scrape_for_comune(comune: str) -> List[Notice]:
    headless = _env_bool("HEADLESS", True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)

        try:
            page.goto(TRIBUNALE_URL, wait_until="domcontentloaded")
            _dismiss_cookie_banner(page)

            # TAB: Beni Immobili
            try:
                page.get_by_role("link", name=re.compile(r"Beni\s+Immobili", re.I)).click(timeout=DEFAULT_TIMEOUT_MS)
            except Exception:
                page.get_by_text("Beni Immobili", exact=False).first.click(timeout=DEFAULT_TIMEOUT_MS)

            # SUBTAB: Ricerca Generale
            try:
                page.get_by_role("link", name=re.compile(r"Ricerca\s+Generale", re.I)).click(timeout=DEFAULT_TIMEOUT_MS)
            except Exception:
                page.get_by_text("Ricerca Generale", exact=False).first.click(timeout=DEFAULT_TIMEOUT_MS)

            _dismiss_cookie_banner(page)

            # Regione / Provincia / Comune
            _smart_select_option(page, "Regione", REGIONE)
            page.wait_for_timeout(500)
            _smart_select_option(page, "Provincia", PROVINCIA)
            page.wait_for_timeout(900)
            _smart_select_option(page, "Comune", comune)

            # Aste per pagina
            try:
                _smart_select_option(page, "Aste per pagina", ASTA_PER_PAGINA)
            except Exception:
                pass

            _dismiss_cookie_banner(page)
            _click_mostra_risultato(page)
            _wait_results_loaded(page)

            blocks = _extract_blocks_from_page(page)

            notices: List[Notice] = []
            for b in blocks:
                header = _strip_spaces(b.get("header") or "")
                body = (b.get("body") or "").strip()
                link_objs = b.get("links") or []
                flat_links = _flatten_links(link_objs)

                direct_raw = _pick_direct_link(link_objs)
                direct = _resolve_final_url(direct_raw)

                sale_dt = _parse_sale_date(body + " " + header)

                # SOLO attivi
                if sale_dt is not None and sale_dt < date.today():
                    continue

                if (not direct) or direct == TRIBUNALE_URL:
                    if flat_links:
                        direct = _resolve_final_url(flat_links[0])
                    else:
                        direct = TRIBUNALE_URL

                body_clean = _strip_spaces(body)
                if len(body_clean) > 3200:
                    body_clean = body_clean[:3200] + "…"

                notices.append(
                    Notice(
                        comune=comune,
                        header=header or "Annuncio",
                        body=body_clean,
                        direct_link=direct,
                        links=tuple(flat_links[:10]),
                        sale_date=sale_dt,
                    )
                )

            return notices

        finally:
            browser.close()


# -----------------------------
# Email
# -----------------------------
def build_email_html(all_notices: List[Notice]) -> Tuple[str, str]:
    today = date.today().strftime("%d/%m/%Y")
    subject = f"Aste giudiziarie attive (BG) - report {today}"

    by_comune: Dict[str, List[Notice]] = {}
    for n in all_notices:
        by_comune.setdefault(n.comune, []).append(n)

    for k in by_comune:
        by_comune[k].sort(key=lambda x: (x.sale_date or date.max, x.header))

    html = []
    html.append("<html><body style='font-family:Arial,Helvetica,sans-serif'>")
    html.append(f"<h2>{subject}</h2>")
    html.append("<p>Fonte: Tribunale di Bergamo – Vendite Giudiziarie</p>")

    total = 0
    for comune in COMUNI_TARGET:
        lst = by_comune.get(comune, [])
        html.append(f"<h3>{comune} ({len(lst)})</h3>")
        if not lst:
            html.append("<p><i>Nessun annuncio attivo trovato.</i></p>")
            continue

        html.append("<ul>")
        for n in lst:
            total += 1
            sale = n.sale_date.strftime("%d/%m/%Y") if n.sale_date else "n/d"
            html.append("<li style='margin-bottom:14px'>")
            html.append(f"<b>{n.header}</b> – <span>Data vendita: {sale}</span><br>")
            html.append(f"<a href='{n.direct_link}'>LINK DIRETTO ANNUNCIO</a><br>")
            if n.body:
                html.append(f"<div style='margin-top:6px;color:#222'>{n.body}</div>")
            if n.links:
                others = [u for u in n.links if u != n.direct_link][:3]
                if others:
                    html.append("<div style='margin-top:6px;font-size:12px'>Altri link: ")
                    html.append(" | ".join([f"<a href='{u}'>apri</a>" for u in others]))
                    html.append("</div>")
            html.append("</li>")
        html.append("</ul>")

    html.append(f"<hr><p>Totale annunci (attivi): <b>{total}</b></p>")
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
    all_notices: List[Notice] = []

    for comune in COMUNI_TARGET:
        try:
            notices = scrape_for_comune(comune)
            log(f"{comune}: {len(notices)} annunci")
            all_notices.extend(notices)
        except Exception as e:
            tb = traceback.format_exc()
            all_notices.append(
                Notice(
                    comune=comune,
                    header=f"ERRORE scraping per {comune}",
                    body=f"{e}\n\n{tb}",
                    direct_link=TRIBUNALE_URL,
                    links=(TRIBUNALE_URL,),
                    sale_date=None,
                )
            )

    subject, html_body = build_email_html(all_notices)

    if os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASS") and os.getenv("EMAIL_TO"):
        send_email(subject, html_body)
        log("Email inviata.")
    else:
        print(subject)
        print("=" * len(subject))
        for n in all_notices:
            print(f"\n[{n.comune}] {n.header} (data={n.sale_date})")
            print(f"LINK: {n.direct_link}")
            print(n.body[:600])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
