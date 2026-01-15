import os
import re
import time
import smtplib
import requests
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional, Dict, Tuple
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup


# =========================
# CONFIG
# =========================

BASE_SEARCH_URL = "https://www.tribunale.bergamo.it/aste/cerca"

REGIONE = "Lombardia"
PROVINCIA = "Bergamo"

# Comuni target (NB: Grassobbio si scrive con doppia "b")
COMUNI = [
    "Azzano San Paolo",
    "Stezzano",
    "Zanica",
    "Lallio",
    "Grassobbio",
]

# Alias: accetta anche scritto male
COMUNE_ALIASES = {
    "GRASSOBIO": "Grassobbio",
    "GRASSOBBIO": "Grassobbio",
}

EMAIL_USER = os.environ.get("EMAIL_USER", "").strip()
EMAIL_PASS = os.environ.get("EMAIL_PASS", "").strip()
EMAIL_RECEIVER = os.environ.get("EMAIL_RECEIVER", "eglantinshaba@gmail.com").strip()

REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
SLEEP_BETWEEN_RETRIES = 2

# Regex estrazione info (robuste)
RE_DATE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}:\d{2})\b")
RE_PROC = re.compile(r"\b(\d{1,6}/\d{4})\b")
RE_TIPOLOGIA = re.compile(r"Tipologia\s+(.+?)\s+Quota", re.IGNORECASE)
RE_PREZZO_BASE = re.compile(r"Prezzo\s*base\s*€?\s*([0-9\.\,]+)", re.IGNORECASE)


@dataclass
class Notice:
    comune: str
    procedura: Optional[str]
    tipologia: Optional[str]
    data_vendita: Optional[str]
    prezzo_base: Optional[str]
    link_diretto: str
    link_ricerca: str


def _normalize_comune(label: str) -> str:
    up = label.strip().upper()
    if up in COMUNE_ALIASES:
        return COMUNE_ALIASES[up]
    return label.strip()


def _build_search_url(comune: str) -> str:
    """
    Link diretto risultati sul Tribunale (niente click, niente cookie banner).
    tipologia_lotto=1 = Beni Immobili (come nei link che ti funzionavano)
    """
    params = {
        "regione": REGIONE,
        "provincia": PROVINCIA,
        "comune": comune,
        "limit": "50",
        "tipologia": "",
        "tipo_procedura": "",
        "rge": "",
        "rge_anno": "",
        "prezzo_da": "",
        "prezzo_a": "",
        "orderby": "",
        "tipologia_lotto": "1",
    }
    return f"{BASE_SEARCH_URL}?{urlencode(params, doseq=True)}"


def _http_get(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AsteBergamoBot/2.0; +https://www.tribunale.bergamo.it/)",
        "Accept-Language": "it-IT,it;q=0.9,en;q=0.7",
    }

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(SLEEP_BETWEEN_RETRIES)

    raise RuntimeError(f"HTTP GET fallito su {url}: {last_err}")


def _find_result_container(a_tag) -> str:
    """
    Risale nel DOM fino a prendere il blocco dell'annuncio senza catturare tutta la pagina.
    Evita duplicazioni e testo infinito.
    """
    current = a_tag
    best = a_tag

    while True:
        parent = getattr(current, "parent", None)
        if parent is None:
            break

        if getattr(parent, "name", "") in ("body", "html"):
            break

        # Se nel parent ci sono più "Scheda dettagliata", è troppo alto
        schede = parent.find_all("a", string=re.compile(r"scheda\s+dettagliata", re.I))
        if len(schede) > 1:
            break

        txt = parent.get_text(" ", strip=True)

        # Soglia: sopra diventa troppo verbose
        if len(txt) > 1400:
            break

        best = parent
        current = parent

    return best.get_text(" ", strip=True)


def _extract_fields(text: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    procedura = None
    tipologia = None
    data_vendita = None
    prezzo_base = None

    m_proc = RE_PROC.search(text)
    if m_proc:
        procedura = m_proc.group(1)

    m_tip = RE_TIPOLOGIA.search(text)
    if m_tip:
        tipologia = m_tip.group(1).strip()

    m_date = RE_DATE.search(text)
    if m_date:
        data_vendita = f"{m_date.group(1)} {m_date.group(2)}"

    m_prezzo = RE_PREZZO_BASE.search(text)
    if m_prezzo:
        prezzo_base = f"€ {m_prezzo.group(1)}"

    return procedura, tipologia, data_vendita, prezzo_base


def scrape_for_comune(comune_input: str) -> List[Notice]:
    comune = _normalize_comune(comune_input)
    search_url = _build_search_url(comune)

    html = _http_get(search_url)
    soup = BeautifulSoup(html, "html.parser")

    # LINK DIRETTO ANNUNCIO = "Scheda dettagliata"
    scheda_links = soup.find_all("a", string=re.compile(r"scheda\s+dettagliata", re.I))

    notices: List[Notice] = []
    seen: set[str] = set()

    for a in scheda_links:
        href = (a.get("href") or "").strip()
        if not href:
            continue

        # Normalizza link assoluto
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin("https://www.tribunale.bergamo.it", href)
        elif not href.lower().startswith("http"):
            href = urljoin(search_url, href)

        if href in seen:
            continue
        seen.add(href)

        container_text = _find_result_container(a)
        procedura, tipologia, data_vendita, prezzo_base = _extract_fields(container_text)

        notices.append(
            Notice(
                comune=comune,
                procedura=procedura,
                tipologia=tipologia,
                data_vendita=data_vendita,
                prezzo_base=prezzo_base,
                link_diretto=href,
                link_ricerca=search_url,
            )
        )

    return notices


def _format_email(results: Dict[str, List[Notice]], errors: Dict[str, str]) -> str:
    lines: List[str] = []

    for comune in COMUNI:
        key = _normalize_comune(comune)
        notices = results.get(key, [])
        err = errors.get(key)

        lines.append(f"{key} ({len(notices)})")

        if err:
            lines.append(f"ERRORE scraping per {key}")
            lines.append(f"Dettaglio: {err}")
            lines.append(f"LINK RICERCA: {_build_search_url(key)}")
            lines.append("")
            continue

        if not notices:
            lines.append("Nessun annuncio attivo trovato.")
            lines.append("")
            continue

        for i, n in enumerate(notices, start=1):
            title_parts = []
            if n.procedura:
                title_parts.append(f"Proc. {n.procedura}")
            if n.tipologia:
                title_parts.append(n.tipologia)
            title = " - ".join(title_parts) if title_parts else "Annuncio"

            lines.append(f"{i}. {title}")
            if n.data_vendita:
                lines.append(f"   Data vendita: {n.data_vendita}")
            if n.prezzo_base:
                lines.append(f"   Prezzo base: {n.prezzo_base}")
            lines.append(f"   LINK DIRETTO ANNUNCIO: {n.link_diretto}")
            lines.append(f"   LINK RICERCA TRIBUNALE: {n.link_ricerca}")
            lines.append("")

    return "\n".join(lines).strip()


def send_email(body: str) -> None:
    if not EMAIL_USER or not EMAIL_PASS:
        raise RuntimeError("EMAIL_USER / EMAIL_PASS non impostati nei secrets del repository.")

    msg = MIMEMultipart()
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_RECEIVER
    msg["Subject"] = "Aste attive - Tribunale di Bergamo (comuni selezionati)"
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)


def main() -> int:
    results: Dict[str, List[Notice]] = {}
    errors: Dict[str, str] = {}

    for comune in COMUNI:
        c = _normalize_comune(comune)
        try:
            results[c] = scrape_for_comune(c)
        except Exception as e:
            errors[c] = str(e)

    email_body = _format_email(results, errors)
    print(email_body)

    send_email(email_body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
