"""
Aste Bergamo - Scraper "Vendite Giudiziarie" (Tribunale di Bergamo)

Fix principali (v2):
- Gestione robusta del banner cookie Iubenda che intercetta i click (iubenda-cs-banner)
- Operazioni (select/click) eseguite SOLO nel form "attivo" (quello che contiene "Mostra il risultato")
- Click su "Mostra il risultato" con fallback force=True
- Attese più robuste per caricamento Province/Comuni

ENV richieste per invio email (SMTP):
  SMTP_HOST     es. smtp.gmail.com
  SMTP_PORT     es. 587
  SMTP_USER     es. account@gmail.com
  SMTP_PASS     (Gmail: App Password)
  EMAIL_TO      destinatario (es. eglantinshaba@gmail.com)

ENV opzionali:
  HEADLESS=1/0  (default 1)
  DEBUG=1/0     (default 0)
"""

from __future__ import annotations

import os
import re
import sys
import ssl
import smtplib
from dataclasses import dataclass
from datetime import datetime, date
from email.message import EmailMessage
from typing import Iterable, List, Optional, Tuple

from bs4 import BeautifulSoup  # type: ignore
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError  # type: ignore


TRIBUNALE_URL = "https://www.tribunale.bergamo.it/vendite-giudiziarie_164.html"
REGIONE = "Lombardia"
PROVINCIA = "Bergamo"

COMUNI_TARGET = [
    "Stezzano",
    "Zanica",
    "Lallio",
    "Grassobio",
]

PER_PAGE = "50"


@dataclass(frozen=True)
class Notice:
    comune: str
    header: str
    body: str
    links: Tuple[str, ...]
    sale_date: Optional[date]


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


DEBUG = _env_bool("DEBUG", False)
HEADLESS = _env_bool("HEADLESS", True)


def log(msg: str) -> None:
    if DEBUG:
        print(f"[DEBUG] {msg}", file=sys.stderr)


def _normalize_whitespace(s: str) -> str:
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


_DATE_PATTERNS = [
    re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b"),
]


def _extract_dates(text: str) -> List[date]:
    found: List[date] = []
    for rx in _DATE_PATTERNS:
        for m in rx.finditer(text):
            d, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                found.append(date(y, mth, d))
            except ValueError:
                continue
    out: List[date] = []
    seen = set()
    for d in found:
        if d not in seen:
            out.append(d)
            seen.add(d)
    return out


def _pick_sale_date(block_text: str) -> Optional[date]:
    dates = _extract_dates(block_text)
    if not dates:
        return None
    today = date.today()
    future = [d for d in dates if d >= today]
    if future:
        return min(future)
    return max(dates)


def _extract_links_from_html(html: str) -> Tuple[str, ...]:
    soup = BeautifulSoup(html, "html.parser")
    hrefs: List[str] = []
    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        if href.startswith("/"):
            href = "https://www.tribunale.bergamo.it" + href
        hrefs.append(href)

    out: List[str] = []
    seen = set()
    for h in hrefs:
        if h not in seen:
            out.append(h)
            seen.add(h)
    return tuple(out)


def _split_notices_from_page_text(page_text: str) -> List[str]:
    text = _normalize_whitespace(page_text)
    start = text.find("TRIBUNALE")
    if start == -1:
        return []
    end = text.find("Il Tribunale", start)
    if end != -1:
        text = text[start:end].strip()
    else:
        text = text[start:].strip()

    blocks = re.split(r"\n(?=TRIBUNALE\s+DI\s+)", text)
    blocks = [b.strip() for b in blocks if b.strip().startswith("TRIBUNALE")]
    return blocks


def _dismiss_iubenda(page, timeout_ms: int = 4000) -> None:
    """
    Se presente il banner cookie Iubenda (id=iubenda-cs-banner) lo chiude/clicca.
    Se non riesce, lo nasconde (fallback) così non intercetta i click.
    """
    try:
        banner = page.locator("#iubenda-cs-banner")
        banner.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        return

    try:
        banner = page.locator("#iubenda-cs-banner")
        if not banner.is_visible():
            return
    except Exception:
        return

    log("Iubenda banner visible -> try accept/reject/close")

    candidates = [
        "#iubenda-cs-accept-btn",
        ".iubenda-cs-accept-btn",
        "button:has-text('Accetta')",
        "a:has-text('Accetta')",
        "#iubenda-cs-reject-btn",
        ".iubenda-cs-reject-btn",
        "button:has-text('Rifiuta')",
        "a:has-text('Rifiuta')",
        ".iubenda-cs-close-btn",
        "button:has-text('Chiudi')",
        "button:has-text('Continua senza accettare')",
        "a:has-text('Continua senza accettare')",
        "button:has-text('OK')",
    ]

    for sel in candidates:
        try:
            btn = banner.locator(sel).first
            btn.wait_for(state="visible", timeout=1200)
            btn.click(timeout=1200)
            page.wait_for_timeout(300)
            if page.locator("#iubenda-cs-banner").count() == 0:
                return
            if not page.locator("#iubenda-cs-banner").is_visible():
                return
        except Exception:
            continue

    log("Iubenda banner not dismissable -> hide via JS fallback")
    try:
        page.evaluate(
            """() => {
                const b = document.querySelector('#iubenda-cs-banner');
                if (b) {
                  b.style.pointerEvents = 'none';
                  b.style.display = 'none';
                }
            }"""
        )
    except Exception:
        pass


def _get_visible_submit(page):
    subs = page.locator('input[type="submit"][value="Mostra il risultato"]')
    for i in range(subs.count()):
        s = subs.nth(i)
        try:
            if s.is_visible():
                return s
        except Exception:
            continue
    raise RuntimeError("Impossibile trovare il pulsante 'Mostra il risultato' visibile.")


def _get_active_form(page):
    submit = _get_visible_submit(page)
    form = submit.locator("xpath=ancestor::form[1]")
    if form.count() == 0:
        raise RuntimeError("Impossibile risalire al form dal pulsante 'Mostra il risultato'.")
    return form


def _find_visible_select_index_by_options(scope, must_contain: Iterable[str], timeout_ms: int = 8000) -> int:
    must = [m.lower() for m in must_contain]
    selects = scope.locator("select")
    count = selects.count()
    deadline = datetime.now().timestamp() + (timeout_ms / 1000)

    while datetime.now().timestamp() < deadline:
        for i in range(count):
            try:
                sel = selects.nth(i)
                if not sel.is_visible():
                    continue
                options_text: str = sel.evaluate(
                    """(el) => Array.from(el.options || []).map(o => (o.textContent||"").trim()).join("\\n")"""
                )
                ot = options_text.lower()
                if all(m in ot for m in must):
                    return i
            except Exception:
                continue
        scope.page.wait_for_timeout(250)

    raise RuntimeError(f"Impossibile trovare <select> visibile con opzioni {list(must_contain)}.")


def _select_option_by_label(scope, select_nth: int, label: str, timeout_ms: int = 10000) -> None:
    sel = scope.locator("select").nth(select_nth)
    deadline = datetime.now().timestamp() + (timeout_ms / 1000)
    last_err: Optional[str] = None
    while datetime.now().timestamp() < deadline:
        try:
            sel.select_option(label=label)
            return
        except Exception as e:
            last_err = str(e)
            scope.page.wait_for_timeout(250)
    raise RuntimeError(f"Impossibile selezionare '{label}' sul select #{select_nth}. Errore: {last_err}")


def _wait_select_has_option(scope, select_nth: int, label: str, timeout_ms: int = 12000) -> None:
    sel = scope.locator("select").nth(select_nth)
    deadline = datetime.now().timestamp() + (timeout_ms / 1000)
    while datetime.now().timestamp() < deadline:
        try:
            ok: bool = sel.evaluate(
                """(el, val) => Array.from(el.options || []).some(o => (o.textContent||"").trim() === val)""",
                label,
            )
            if ok:
                return
        except Exception:
            pass
        scope.page.wait_for_timeout(250)
    raise RuntimeError(f"Timeout: l'opzione '{label}' non è comparsa nel select #{select_nth}.")


def scrape_for_comune(comune: str) -> List[Notice]:
    log(f"Scrape comune={comune}")
    notices: List[Notice] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page = context.new_page()

        page.goto(TRIBUNALE_URL, wait_until="domcontentloaded", timeout=60000)

        _dismiss_iubenda(page)

        try:
            page.get_by_text("Beni Immobili", exact=False).first.click(timeout=1500)
        except Exception:
            pass
        try:
            page.get_by_text("Ricerca Generale", exact=False).first.click(timeout=1500)
        except Exception:
            pass

        form = _get_active_form(page)

        idx_regione = _find_visible_select_index_by_options(form, [REGIONE, "Veneto"], timeout_ms=8000)
        _select_option_by_label(form, idx_regione, REGIONE)

        idx_prov = _find_visible_select_index_by_options(form, [PROVINCIA], timeout_ms=12000)
        _wait_select_has_option(form, idx_prov, PROVINCIA, timeout_ms=12000)
        _select_option_by_label(form, idx_prov, PROVINCIA)

        comune_set = False
        try:
            idx_com = _find_visible_select_index_by_options(form, [comune], timeout_ms=15000)
            _wait_select_has_option(form, idx_com, comune, timeout_ms=15000)
            _select_option_by_label(form, idx_com, comune, timeout_ms=15000)
            comune_set = True
        except Exception as e:
            log(f"Comune via <select> non riuscito ({comune}): {e}")

        if not comune_set:
            try:
                inp = form.locator("input[id*='comun' i], input[name*='comun' i]").first
                if inp.count() and inp.is_visible():
                    inp.click(timeout=1000)
                    inp.fill(comune, timeout=2000)
                    inp.press("Enter", timeout=1500)
                    comune_set = True
            except Exception as e:
                raise RuntimeError(f"Impossibile impostare il Comune '{comune}' (né select né input). Dettaglio: {e}")

        try:
            idx_pp = _find_visible_select_index_by_options(form, ["10", "25", "50"], timeout_ms=2500)
            _select_option_by_label(form, idx_pp, PER_PAGE, timeout_ms=2500)
        except Exception:
            pass

        try:
            cb = form.get_by_label(re.compile(r"Includi\s+le\s+aste\s+passate", re.I))
            if cb.is_visible() and cb.is_checked():
                cb.uncheck()
        except Exception:
            pass

        _dismiss_iubenda(page)
        submit = form.locator('input[type="submit"][value="Mostra il risultato"]').first
        try:
            submit.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass

        try:
            submit.click(timeout=5000)
        except Exception:
            _dismiss_iubenda(page)
            submit.click(timeout=5000, force=True)

        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass

        try:
            page.get_by_text(re.compile(r"\bTRIBUNALE\b", re.I)).first.wait_for(timeout=15000)
        except PlaywrightTimeoutError:
            pass

        page_text = page.inner_text("body")
        blocks = _split_notices_from_page_text(page_text)

        if not blocks and "LOTTO" in page_text.upper():
            seg = _normalize_whitespace(page_text)
            blocks = re.split(r"\n(?=LOTTO\s+\w+)", seg)

        html = page.content()
        all_links = _extract_links_from_html(html)

        for b in blocks:
            b_norm = _normalize_whitespace(b)
            header = b_norm.splitlines()[0] if b_norm else "Annuncio"
            sale_dt = _pick_sale_date(b_norm)

            if sale_dt is not None and sale_dt < date.today():
                continue

            urls = re.findall(r"(https?://\S+|www\.\S+)", b_norm)
            cleaned: List[str] = []
            for u in urls:
                u = u.rstrip(").,;")
                if u.startswith("www."):
                    u = "https://" + u
                cleaned.append(u)

            pvp_links = [l for l in all_links if "portalevenditepubbliche" in l.lower()]
            linkset: List[str] = []
            for l in cleaned + pvp_links:
                if l not in linkset:
                    linkset.append(l)

            notices.append(
                Notice(
                    comune=comune,
                    header=header,
                    body=b_norm,
                    links=tuple(linkset),
                    sale_date=sale_dt,
                )
            )

        browser.close()

    return notices


def build_email_html(all_notices: List[Notice]) -> Tuple[str, str]:
    today = date.today().strftime("%d/%m/%Y")
    subject = f"Aste giudiziarie attive (BG) - report {today}"

    by_comune: dict[str, List[Notice]] = {}
    for n in all_notices:
        by_comune.setdefault(n.comune, []).append(n)

    for k in by_comune:
        by_comune[k] = sorted(by_comune[k], key=lambda x: (x.sale_date or date.max, x.header))

    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    parts: List[str] = []
    parts.append(f"<p><b>Fonte:</b> {esc(TRIBUNALE_URL)}</p>")
    parts.append(
        f"<p><b>Filtri:</b> Regione={esc(REGIONE)}; Provincia={esc(PROVINCIA)}; "
        f"Comuni={', '.join(map(esc, COMUNI_TARGET))}</p>"
    )

    total = sum(len(v) for v in by_comune.values())
    parts.append(f"<p><b>Totale annunci (attivi):</b> {total}</p>")

    for comune in COMUNI_TARGET:
        lst = by_comune.get(comune, [])
        parts.append(f"<h2>{esc(comune)} ({len(lst)})</h2>")
        if not lst:
            parts.append("<p>Nessun annuncio attivo trovato.</p>")
            continue

        for i, n in enumerate(lst, 1):
            sale = n.sale_date.strftime("%d/%m/%Y") if n.sale_date else "n/d"
            parts.append("<hr/>")
            parts.append(f"<p><b>{i}. {esc(n.header)}</b><br/>")
            parts.append(f"<b>Data rilevata:</b> {esc(sale)}</p>")

            body = esc(n.body)
            if len(body) > 3000:
                body = body[:3000] + "…"
            parts.append(
                "<pre style='white-space:pre-wrap;font-family:ui-monospace,Consolas,monospace'>"
                f"{body}</pre>"
            )

            if n.links:
                parts.append(
                    "<p><b>Link:</b><br/>"
                    + "<br/>".join(f"<a href='{esc(l)}'>{esc(l)}</a>" for l in n.links)
                    + "</p>"
                )

    html_body = "\n".join(parts)
    return subject, html_body


def send_email(subject: str, html_body: str) -> None:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587").strip() or "587")
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    email_to = os.getenv("EMAIL_TO", "").strip()

    missing = [
        k
        for k, v in {
            "SMTP_HOST": smtp_host,
            "SMTP_USER": smtp_user,
            "SMTP_PASS": smtp_pass,
            "EMAIL_TO": email_to,
        }.items()
        if not v
    ]
    if missing:
        raise RuntimeError(f"Variabili d'ambiente mancanti per invio email: {', '.join(missing)}")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = email_to
    msg.set_content("Il tuo client non supporta HTML. Apri con un client che supporta HTML.")
    msg.add_alternative(html_body, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.ehlo()
        server.starttls(context=context)
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)


def main() -> int:
    all_notices: List[Notice] = []

    for comune in COMUNI_TARGET:
        try:
            notices = scrape_for_comune(comune)
            log(f"{comune}: {len(notices)} annunci")
            all_notices.extend(notices)
        except Exception as e:
            all_notices.append(
                Notice(
                    comune=comune,
                    header=f"ERRORE scraping per {comune}",
                    body=str(e),
                    links=(TRIBUNALE_URL,),
                    sale_date=None,
                )
            )

    subject, html_body = build_email_html(all_notices)

    if os.getenv("SMTP_HOST") and os.getenv("SMTP_USER") and os.getenv("SMTP_PASS") and os.getenv("EMAIL_TO"):
        send_email(subject, html_body)
        print("Email inviata.")
    else:
        print(subject)
        print("=" * len(subject))
        for n in all_notices:
            print(f"\n[{n.comune}] {n.header} (data={n.sale_date})")
            print(n.body[:500])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
