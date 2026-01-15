import os
import re
import time
import smtplib
import requests
from dataclasses import dataclass
from typing import List, Dict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup


# =========================
# CONFIG
# =========================
BASE_TRIB_SEARCH_URL = "https://www.tribunale.bergamo.it/aste/cerca"
TRIB_DOMAIN = "https://www.tribunale.bergamo.it"

REGIONE = "Lombardia"
PROVINCIA = "Bergamo"

# COMUNI target (Grassobbio corretto con doppia B)
COMUNI = [
    "Azzano San Paolo",
    "Stezzano",
    "Zanica",
    "Lallio",
    "Grassobbio",
]

COMUNE_ALIASES = {
    "GRASSOBIO": "Grassobbio",
    "GRASSOBBIO": "Grassobbio",
}

REQUEST_TIMEOUT = 25
MAX_RETRIES = 3
SLEEP_BETWEEN_RETRIES = 2

# EMAIL (Gmail App Password)
EMAIL_USER = os.environ.get("EMAIL_USER", "").strip()
EMAIL_PASS = os.environ.get("EMAIL_PASS", "").strip()
EMAIL_TO = os.environ.get("EMAIL_TO", "eglantinshaba@gmail.com").strip()


@dataclass
class Notice:
    comune: str
    titolo: str
    data_vendita: str
    prezzo_base: str
    link_diretto: str
    link_ricerca: str


def norm_comune(c: str) -> str:
    up = (c or "").strip().upper()
    return COMUNE_ALIASES.get(up, (c or "").strip())


def build_search_url(comune: str) -> str:
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
        "tipologia_lotto": "1",  # Beni Immobili
    }
    return f"{BASE_TRIB_SEARCH_URL}?{urlencode(params)}"


def http_get(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (AsteBergamoBot/FINAL)",
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
    raise RuntimeError(f"HTTP GET fallito: {url} -> {last_err}")


def extract_first(text: str, pattern: str, default: str = "n/d", flags=0) -> str:
    m = re.search(pattern, text or "", flags)
    if not m:
        return default
    if m.groups():
        return m.group(1).strip()
    return m.group(0).strip()


def climb_block(a_tag) -> str:
    """
    Prende solo il blocco dell'annuncio (non tutta la pagina).
    """
    current = a_tag
    best = a_tag

    while True:
        parent = getattr(current, "parent", None)
        if parent is None:
            break

        if getattr(parent, "name", "") in ("body", "html"):
            break

        schede = parent.find_all("a", string=re.compile(r"scheda\s+dettagliata", re.I))
        if len(schede) > 1:
            break

        txt = parent.get_text(" ", strip=True)
        if len(txt) > 1500:
            break

        best = parent
        current = parent

    return best.get_text(" ", strip=True)


def scrape_comune(comune_raw: str) -> List[Notice]:
    comune = norm_comune(comune_raw)
    url = build_search_url(comune)

    html = http_get(url)
    soup = BeautifulSoup(html, "lxml")

    # Link diretto annuncio = "Scheda dettagliata"
    schede = soup.find_all("a", string=re.compile(r"scheda\s+dettagliata", re.I))

    notices: List[Notice] = []
    seen = set()

    for a in schede:
        href = (a.get("href") or "").strip()
        if not href:
            continue

        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = urljoin(TRIB_DOMAIN, href)
        elif not href.lower().startswith("http"):
            href = urljoin(url, href)

        if href in seen:
            continue
        seen.add(href)

        block_text = climb_block(a)

        # Estraggo info principali (robuste)
        data_v = extract_first(block_text, r"Data\s+(\d{2}/\d{2}/\d{4}\s*-\s*\d{2}:\d{2})", "n/d", re.I)
        prezzo = extract_first(block_text, r"Prezzo\s+base\s+€\s*([0-9\.\,]+)", "n/d", re.I)
        if prezzo != "n/d":
            prezzo = f"€ {prezzo}"

        # Titolo compatto (procedura/lotto se presente)
        proc = extract_first(block_text, r"Procedura\s+([0-9]{1,6}/[0-9]{4})", "", re.I)
        lotto = extract_first(block_text, r"\bLotto\s+([0-9]+)\b", "", re.I)
        tipologia = extract_first(block_text, r"Tipologia\s+(.+?)\s+Quota", "", re.I)

        titolo_parts = []
        if proc:
            titolo_parts.append(f"Proc. {proc}")
        if lotto:
            titolo_parts.append(f"Lotto {lotto}")
        if tipologia:
            titolo_parts.append(tipologia)

        titolo = " - ".join(titolo_parts).strip()
        if not titolo:
            titolo = (block_text[:160] + "…") if len(block_text) > 160 else (block_text or "Annuncio")

        notices.append(
            Notice(
                comune=comune,
                titolo=titolo,
                data_vendita=data_v,
                prezzo_base=prezzo,
                link_diretto=href,         # ✅ LINK DIRETTO
                link_ricerca=url,          # link ricerca tribunale
            )
        )

    return notices


def format_email(results: Dict[str, List[Notice]], errors: Dict[str, str]) -> str:
    out: List[str] = []
    out.append(f"Aste attive – Tribunale di Bergamo – {time.strftime('%d/%m/%Y')}")
    out.append("")

    for comune in [norm_comune(c) for c in COMUNI]:
        lst = results.get(comune, [])
        err = errors.get(comune)

        out.append(f"{comune} ({len(lst)})")

        if err:
            out.append(f"ERRORE scraping per {comune}")
            out.append(f"Dettaglio: {err}")
            out.append(f"LINK RICERCA: {build_search_url(comune)}")
            out.append("")
            continue

        if not lst:
            out.append("Nessun annuncio attivo trovato.")
            out.append("")
            continue

        for i, n in enumerate(lst, 1):
            out.append(f"{i}. {n.titolo}")
            out.append(f"   Data vendita: {n.data_vendita}")
            out.append(f"   Prezzo base: {n.prezzo_base}")
            out.append(f"   LINK DIRETTO ANNUNCIO: {n.link_diretto}")
            out.append(f"   LINK RICERCA TRIBUNALE: {n.link_ricerca}")
            out.append("")

    return "\n".join(out).strip()


def send_email(body: str) -> None:
    """
    Se SMTP fallisce, NON deve far fallire il job.
    """
    if not EMAIL_USER or not EMAIL_PASS:
        print("EMAIL NON INVIATA: manca EMAIL_USER o EMAIL_PASS nei secrets.")
        return

    msg = MIMEMultipart()
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = "Aste attive - Tribunale di Bergamo (comuni selezionati)"
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
        print("Email inviata OK.")
    except Exception as e:
        print(f"EMAIL NON INVIATA (errore SMTP): {e}")


def main() -> int:
    results: Dict[str, List[Notice]] = {norm_comune(c): [] for c in COMUNI}
    errors: Dict[str, str] = {}

    for comune in COMUNI:
        c = norm_comune(comune)
        try:
            results[c] = scrape_comune(c)
        except Exception as e:
            errors[c] = str(e)

    body = format_email(results, errors)
    print(body)

    send_email(body)

    # ✅ mai fallire GitHub Actions
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
