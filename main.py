from __future__ import annotations

import os
import re
import smtplib
import time
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
    Decodifica i link 'urlsand.esvalabs.com' che includono la vera URL nel parametro 'u='
    (evita 403, non fa richieste).
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


def _resolve_final_url(url: str, timeout: int = 10) -> str:
    """
    Restituisce URL "diretto":
    - se tracking esvalabs -> decodifica
    - altrimenti segue redirect HTTP
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
    # supporta 06.06.2024 e 06/06/2024
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text or "")
    if not m:
        return None
    dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(yyyy, mm, dd)
    except Exception:
        return None


# -----------------------------
# Playwright helpers
# -----------------------------
def _dismiss_cookie_banner(page) -> None:
    try:
        page.wait_for_timeout(400)

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
                    loc.click(timeout=1200)
                    page.wait_for_timeout(250)
                    return
                except Exception:
                    pass

        # fallback: rimuovi overlay
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


def _click_mostra_risultato(page) -> None:
    btn = page.locator('input[type="submit"][value="Mostra il risultato"]:visible').first
    btn.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    _dismiss_cookie_banner(page)
    btn.click(timeout=DEFAULT_TIMEOUT_MS, force=True)


def _get_active_form(page):
    # prende il form che contiene il bottone visibile "Mostra il risultato"
    btn = page.locator('input[type="submit"][value="Mostra il risultato"]:visible').first
    form = page.locator("form").filter(has=btn).first
    if form.count() == 0:
        raise RuntimeError("Form attivo non trovato (Mostra il risultato).")
    return form


def _select_option_in_active_form(page, value_label: str, max_wait_ms: int = 15_000) -> None:
    """
    Cerca tra i select del FORM attivo quello che contiene l'opzione value_label e la seleziona.
    (evita di selezionare dropdown di altre tab nascoste)
    """
    end = time.time() + max_wait_ms / 1000.0
    last_options_snapshot = ""

    while time.time() < end:
        form = _get_active_form(page)
        selects = form.locator("select:visible")
        for i in range(selects.count()):
            s = selects.nth(i)
            try:
                opts = [(_strip_spaces(x)).lower() for x in s.locator("option").all_text_contents()]
                if value_label.lower() in opts:
                    s.select_option(label=value_label, timeout=DEFAULT_TIMEOUT_MS)
                    return
                last_options_snapshot = ", ".join(opts[:12])
            except Exception:
                continue

        page.wait_for_timeout(250)

    raise RuntimeError(f"Impossibile selezionare '{value_label}' nel form attivo. Ultime opzioni viste: {last_options_snapshot}")


def _wait_results_tables(page) -> None:
    # aspetta almeno una tabella risultati oppure messaggio "nessun risultato"
    page.wait_for_timeout(500)
    try:
        page.wait_for_function(
            """
            () => {
              const t = document.querySelectorAll('table.table-blu.table-bordered');
              if (t && t.length > 0) return true;
              const txt = document.body ? document.body.innerText.toLowerCase() : '';
              return txt.includes('nessun') && txt.includes('risultat');
            }
            """,
            timeout=DEFAULT_TIMEOUT_MS,
        )
    except Exception:
        pass


def _extract_notices_from_tables(page, comune: str) -> List[Notice]:
    """
    Estrae gli annunci SOLO dalle tabelle risultati:
    <table class="table table-blu table-bordered"> ... </table>
    Dentro trovi sempre:
    - titolo in thead (p o th)
    - descrizione in tbody (prima riga)
    - link PDF in seconda riga (Avviso vendita / Perizia / Foto / Planimetria)
    """
    tables = page.locator("table.table-blu.table-bordered")
    n_tables = tables.count()

    if n_tables == 0:
        return []

    notices: List[Notice] = []

    for i in range(n_tables):
        t = tables.nth(i)

        # Header
        title_loc = t.locator("thead p, thead th").first
        header = ""
        try:
            if title_loc.count() > 0:
                header = _strip_spaces(title_loc.inner_text())
        except Exception:
            header = ""

        # evita footer/robe strane
        header_up = (header or "").upper()
        if header_up.startswith("TRIBUNALE DI BERGAMO - VIA BORFURO"):
            continue
        if header and not re.search(r"\b(LOTTO|N\.|N°|RG|R\.G)\b", header_up):
            # se non sembra un annuncio vero, skip
            continue

        if not header:
            header = "Annuncio"

        # Body
        body_text = ""
        try:
            first_row = t.locator("tbody tr").first
            body_text = _strip_spaces(first_row.inner_text())
        except Exception:
            body_text = ""

        # Links
        link_objs: List[dict] = []
        anchors = t.locator("a[href]")
        for k in range(anchors.count()):
            a = anchors.nth(k)
            try:
                href = a.get_attribute("href") or ""
                href = _normalize_url(href)
                if not href:
                    continue
                if href.startswith("mailto:") or "/cdn-cgi/l/email-protection" in href:
                    continue

                # label = testo della cella (utile per capire "Avviso vendita")
                label = ""
                try:
                    label = a.evaluate("el => (el.closest('td') && el.closest('td').innerText) ? el.closest('td').innerText : el.innerText")
                    label = _strip_spaces(label)
                except Exception:
                    label = ""

                link_objs.append({"href": href, "label": label})
            except Exception:
                continue

        # dedup
        seen = set()
        uniq_links: List[dict] = []
        for o in link_objs:
            h = o["href"]
            if h in seen:
                continue
            seen.add(h)
            uniq_links.append(o)

        # scegli link diretto:
        # 1) PDF che contiene "Avviso vendita" nella label
        direct = ""
        for o in uniq_links:
            if o["href"].lower().endswith(".pdf") and "avviso vendita" in (o["label"] or "").lower():
                direct = o["href"]
                break

        # 2) primo PDF
        if not direct:
            for o in uniq_links:
                if o["href"].lower().endswith(".pdf"):
                    direct = o["href"]
                    break

        # 3) link PVP o altro
        if not direct:
            for o in uniq_links:
                if "portalevenditepubbliche.giustizia.it" in o["href"].lower() or "esvalabs.com" in o["href"].lower():
                    direct = _resolve_final_url(o["href"])
                    break

        # 4) fallback pagina principale
        if not direct:
            direct = TRIBUNALE_URL

        # se tracking -> decodifica
        direct = _resolve_final_url(direct)

        # data vendita (se presente nel testo)
        sale_dt = _parse_sale_date(body_text + " " + header)

        # body limit per email
        if len(body_text) > 3200:
            body_text = body_text[:3200] + "…"

        notices.append(
            Notice(
                comune=comune,
                header=header,
                body=body_text,
                direct_link=direct,
                links=tuple([o["href"] for o in uniq_links][:10]),
                sale_date=sale_dt,
            )
        )

    return notices


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
            page.get_by_text("Beni Immobili", exact=False).first.click(timeout=DEFAULT_TIMEOUT_MS)
            page.wait_for_timeout(400)

            # SUBTAB: Ricerca Generale
            page.get_by_text("Ricerca Generale", exact=False).first.click(timeout=DEFAULT_TIMEOUT_MS)
            page.wait_for_timeout(400)

            _dismiss_cookie_banner(page)

            # Regione / Provincia / Comune (nel form attivo)
            _select_option_in_active_form(page, REGIONE, max_wait_ms=20_000)
            page.wait_for_timeout(500)

            _select_option_in_active_form(page, PROVINCIA, max_wait_ms=20_000)
            page.wait_for_timeout(1200)  # tempo per caricare Comuni

            _select_option_in_active_form(page, comune, max_wait_ms=25_000)

            # Aste per pagina
            try:
                _select_option_in_active_form(page, ASTA_PER_PAGINA, max_wait_ms=8_000)
            except Exception:
                pass

            _dismiss_cookie_banner(page)
            _click_mostra_risultato(page)

            _wait_results_tables(page)

            notices = _extract_notices_from_tables(page, comune)
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
            html.append("<p><i>Nessun annuncio trovato.</i></p>")
            continue

        html.append("<ul>")
        for n in lst:
            total += 1
            sale = n.sale_date.strftime("%d/%m/%Y") if n.sale_date else "n/d"
            html.append("<li style='margin-bottom:16px'>")
            html.append(f"<b>{n.header}</b> – <span>Data: {sale}</span><br>")

            # LINK diretto visibile + cliccabile
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

    html.append(f"<hr><p>Totale annunci: <b>{total}</b></p>")
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
            print(n.body[:500])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
