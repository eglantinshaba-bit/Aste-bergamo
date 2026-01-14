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

ASTA_PER_PAGINA = "50"
DEFAULT_TIMEOUT_MS = 35_000


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


def _parse_sale_date(text: str) -> Optional[date]:
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text or "")
    if not m:
        return None
    dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(yyyy, mm, dd)
    except Exception:
        return None


def _norm_place(s: str) -> str:
    s = (s or "").lower()
    s = s.replace(".", " ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # san -> s, ecc
    s = s.replace("s paolo", "san paolo")
    return s


COMUNI_REGEX = {
    "Azzano San Paolo": re.compile(r"\bazzano\s+(san|s)\s+paolo\b", re.I),
    "Stezzano": re.compile(r"\bstezzano\b", re.I),
    "Zanica": re.compile(r"\bzanica\b", re.I),
    "Lallio": re.compile(r"\blallio\b", re.I),
    "Grassobio": re.compile(r"\bgrassobio\b", re.I),
}


def _match_comune(text: str) -> Optional[str]:
    t = text or ""
    for comune, rx in COMUNI_REGEX.items():
        if rx.search(t):
            return comune
    return None


def _score_link(href: str, ctx: str) -> int:
    h = (href or "").lower().strip()
    c = (ctx or "").lower().strip()

    if not h:
        return -1
    if h.startswith("mailto:") or h.startswith("tel:"):
        return -1

    score = 0

    # PDF = link più "diretto" per annuncio
    if ".pdf" in h:
        score += 120
        if "avviso" in c:
            score += 30
        if "perizia" in c:
            score += 20
        if "ordinanza" in c:
            score += 10

    # PVP
    if "portalevenditepubbliche.giustizia.it" in h:
        score += 90

    # tracking (decodifico)
    if "esvalabs.com" in h:
        score += 60

    # dominio tribunale
    if "tribunale.bergamo.it" in h:
        score += 10

    return score


def _pick_direct_link(links: List[Dict[str, str]]) -> str:
    best = ""
    best_score = -1
    for o in links:
        href = o.get("href", "")
        ctx = o.get("ctx", "")
        sc = _score_link(href, ctx)
        if sc > best_score:
            best_score = sc
            best = href
    return best or TRIBUNALE_URL


# -----------------------------
# Playwright helpers
# -----------------------------
def _dismiss_cookie_banner(page) -> None:
    try:
        page.wait_for_timeout(350)
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
                    loc.click(timeout=1200)
                    page.wait_for_timeout(200)
                    return
                except Exception:
                    pass

        page.evaluate(
            """
            () => {
              const b = document.querySelector('#iubenda-cs-banner');
              if (b) b.remove();
              const o = document.querySelector('.iubenda-cs-overlay');
              if (o) o.remove();
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


def _select_after_label(form, label_text: str, desired: str) -> None:
    """
    Seleziona option in un <select> che sta subito dopo il testo label_text dentro il form.
    Fuzzy match su option text (per robustezza).
    """
    # trova il nodo che contiene "Regione/Provincia/Aste per pagina"
    lab = form.locator(
        f"xpath=.//*[contains(normalize-space(.), '{label_text}')]"
    ).first

    if lab.count() == 0:
        raise RuntimeError(f"Label '{label_text}' non trovata nel form.")

    sel = lab.locator("xpath=following::select[1]").first
    sel.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)

    # leggi opzioni
    opts = sel.locator("option").all_text_contents()
    opts_clean = [_strip_spaces(x) for x in opts if _strip_spaces(x)]

    desired_norm = _norm_place(desired)

    # match esatto
    for o in opts_clean:
        if o.lower() == desired.lower():
            sel.select_option(label=o)
            return

    # match "contains"
    best = None
    for o in opts_clean:
        if desired_norm in _norm_place(o):
            best = o
            break

    # fallback: token overlap
    if best is None:
        target_tokens = set(desired_norm.split())
        best_score = -1
        for o in opts_clean:
            ot = set(_norm_place(o).split())
            sc = len(target_tokens.intersection(ot))
            if sc > best_score:
                best_score = sc
                best = o

    if not best:
        raise RuntimeError(f"Impossibile selezionare '{desired}' per '{label_text}'. Opzioni viste: {opts_clean[:12]}")

    sel.select_option(label=best)


def _uncheck_include_past_if_present(page) -> None:
    """
    Se trova checkbox "Includi le aste passate", prova a disattivarla.
    """
    try:
        cb = page.get_by_label("Includi le aste passate")
        if cb.count() > 0:
            try:
                # se è checked, uncheck
                if cb.is_checked():
                    cb.uncheck()
            except Exception:
                # fallback JS
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


def _wait_results(page) -> None:
    page.wait_for_timeout(700)
    # aspetta che compaia almeno un "TRIBUNALE" dopo la ricerca
    page.wait_for_function(
        """
        () => {
          const t = document.body ? document.body.innerText.toUpperCase() : '';
          return t.includes('TRIBUNALE') && t.includes('LOTTO');
        }
        """,
        timeout=DEFAULT_TIMEOUT_MS,
    )


def _extract_blocks(page) -> List[Dict]:
    """
    Estrae annunci in blocchi:
    - header: "TRIBUNALE ... LOTTO ..."
    - body: righe descrizione
    - links: a[href] presenti nel blocco
    Ignora tutto quello dentro form/header/nav/footer.
    """
    js = r"""
    () => {
      const norm = (s) => (s || '').replace(/\s+/g,' ').trim();
      const isIgnored = (el) => {
        if (!el) return true;
        if (el.closest('form')) return true;
        if (el.closest('header')) return true;
        if (el.closest('nav')) return true;
        if (el.closest('footer')) return true;
        return false;
      };

      const isHeader = (el) => {
        if (!el) return false;
        if (isIgnored(el)) return false;
        const t = norm(el.innerText);
        if (!t) return false;
        const tu = t.toUpperCase();
        if (!tu.startsWith('TRIBUNALE')) return false;
        if (tu.length > 220) return false;
        // deve sembrare annuncio (lotto / n. / rg)
        if (!(tu.includes('LOTTO') || tu.includes('N.') || tu.includes('N°') || tu.includes('RG'))) return false;
        return true;
      };

      // trova tutti i possibili header
      const all = Array.from(document.querySelectorAll('body *'));
      const headers = all.filter(isHeader);
      if (!headers.length) return [];

      const headerSet = new Set(headers);

      // treewalker in ordine documento
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
      const blocks = [];
      let current = null;

      const push = () => {
        if (!current) return;
        // dedup links
        const seen = new Set();
        current.links = current.links.filter(o => {
          const h = (o.href || '').trim();
          if (!h) return false;
          if (seen.has(h)) return false;
          seen.add(h);
          return true;
        });
        // compatta testo
        current.body = norm(current.textParts.join('\n'));
        delete current.textParts;
        blocks.push(current);
      };

      let node;
      while (node = walker.nextNode()) {
        if (isIgnored(node)) continue;

        if (headerSet.has(node)) {
          push();
          current = { header: norm(node.innerText), textParts: [], links: [] };
          continue;
        }

        if (!current) continue;

        // testo descrizione
        const tag = (node.tagName || '').toUpperCase();
        if (['P','DIV','LI'].includes(tag)) {
          const txt = norm(node.innerText);
          if (txt && !txt.toUpperCase().startsWith('TRIBUNALE')) {
            // evita di copiare 100 righe di menu
            if (txt.length <= 2000) current.textParts.push(txt);
          }
        }

        // link
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
    blocks = page.evaluate(js)
    return blocks or []


def _setup_search(page) -> None:
    page.goto(TRIBUNALE_URL, wait_until="domcontentloaded")
    _dismiss_cookie_banner(page)

    # Beni Immobili + Ricerca Generale
    page.get_by_text("Beni Immobili", exact=False).first.click(timeout=DEFAULT_TIMEOUT_MS)
    page.wait_for_timeout(300)
    page.get_by_text("Ricerca Generale", exact=False).first.click(timeout=DEFAULT_TIMEOUT_MS)
    page.wait_for_timeout(400)

    _dismiss_cookie_banner(page)

    form = _active_form(page)

    # Regione / Provincia / per pagina
    _select_after_label(form, "Regione", REGIONE)
    page.wait_for_timeout(600)

    _select_after_label(form, "Provincia", PROVINCIA)
    page.wait_for_timeout(900)

    try:
        _select_after_label(form, "Aste per pagina", ASTA_PER_PAGINA)
    except Exception:
        pass

    _uncheck_include_past_if_present(page)

    _dismiss_cookie_banner(page)
    _click_mostra_risultato(page)
    _wait_results(page)


def scrape_all_for_province() -> List[Notice]:
    headless = _env_bool("HEADLESS", True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)

        try:
            _setup_search(page)

            blocks = _extract_blocks(page)
            notices: List[Notice] = []

            for b in blocks:
                header = _strip_spaces(b.get("header") or "")
                body = _strip_spaces(b.get("body") or "")

                # comune trovato in header+body
                comune = _match_comune(header + " " + body)
                if not comune:
                    continue

                links_raw = b.get("links") or []
                links: List[Dict[str, str]] = []
                for o in links_raw:
                    href = _normalize_url(o.get("href", ""))
                    if not href:
                        continue
                    ctx = _strip_spaces(o.get("ctx", ""))
                    links.append({"href": href, "ctx": ctx})

                direct_raw = _pick_direct_link(links)
                direct = _resolve_final_url(direct_raw)

                # data vendita (se presente)
                sale_dt = _parse_sale_date(header + " " + body)
                # escludi aste passate
                if sale_dt is not None and sale_dt < date.today():
                    continue

                # limita body per email
                if len(body) > 3200:
                    body = body[:3200] + "…"

                notices.append(
                    Notice(
                        comune=comune,
                        header=header or "Annuncio",
                        body=body,
                        direct_link=direct or TRIBUNALE_URL,
                        links=tuple([x["href"] for x in links][:10]),
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

    by_comune: Dict[str, List[Notice]] = {c: [] for c in COMUNI_TARGET}
    for n in all_notices:
        if n.comune in by_comune:
            by_comune[n.comune].append(n)

    for c in by_comune:
        by_comune[c].sort(key=lambda x: (x.sale_date or date.max, x.header))

    html = []
    html.append("<html><body style='font-family:Arial,Helvetica,sans-serif'>")
    html.append(f"<h2>{subject}</h2>")
    html.append("<p><b>Fonte:</b> Tribunale di Bergamo – Vendite Giudiziarie</p>")

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
            html.append("<li style='margin-bottom:16px'>")
            html.append(f"<b>{n.header}</b> – <span>Data vendita: {sale}</span><br>")

            html.append(
                f"<div style='margin-top:6px'>"
                f"<b>LINK DIRETTO:</b> "
                f"<a href='{n.direct_link}'>{n.direct_link}</a>"
                f"</div>"
            )

            if n.body:
                html.append(f"<div style='margin-top:8px;color:#222'>{n.body}</div>")

            html.append("</li>")
        html.append("</ul>")

    html.append(f"<hr><p>Totale annunci attivi trovati: <b>{total}</b></p>")
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
        notices = scrape_all_for_province()
        log(f"Totale annunci filtrati sui comuni target: {len(notices)}")
    except Exception as e:
        tb = traceback.format_exc()
        notices = [
            Notice(
                comune="ERRORE",
                header="ERRORE GENERALE SCRAPING",
                body=f"{e}\n\n{tb}",
                direct_link=TRIBUNALE_URL,
                links=(TRIBUNALE_URL,),
                sale_date=None,
            )
        ]

    subject, html_body = build_email_html(notices)

    if os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASS") and os.getenv("EMAIL_TO"):
        send_email(subject, html_body)
        log("Email inviata.")
    else:
        print(subject)
        print("=" * len(subject))
        for n in notices[:20]:
            print(f"\n[{n.comune}] {n.header} (data={n.sale_date})")
            print(f"LINK: {n.direct_link}")
            print(n.body[:500])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
