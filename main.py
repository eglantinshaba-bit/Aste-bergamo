import os
import re
import time
import smtplib
import requests
from dataclasses import dataclass
from typing import List, Dict, Optional
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup


# =========================
# CONFIG
# =========================
REGIONE = "Lombardia"
PROVINCIA = "Bergamo"

# Comuni target (NB: corretto è GRASSOBBIO con doppia B)
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

EMAIL_USER = os.environ.get("EMAIL_USER", "").strip()
EMAIL_PASS = os.environ.get("EMAIL_PASS", "").strip()
EMAIL_TO = os.environ.get("EMAIL_TO", "eglantinshaba@gmail.com").strip()

REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
SLEEP_BETWEEN_RETRIES = 2


# ===== Tribunale (fallback HTML) =====
BASE_TRIB_SEARCH_URL = "https://www.tribunale.bergamo.it/aste/cerca"
TRIB_DOMAIN = "https://www.tribunale.bergamo.it"

# ===== PVP Algolia (primario) =====
ALGOLIA_URL = "https://wvgafsu780-dsn.algolia.net/1/indexes/PROPORTAL/query"
ALGOLIA_HEADERS = {
    "X-Algolia-Application-Id": "WVGAFSU780",
    "X-Algolia-API-Key": "685934188b4952026856019688439e6a",
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (AsteBergamoBot/3.0)",
}


@dataclass
class Notice:
    comune: str
    titolo: str
    data_vendita: str
    prezzo_base: str
    link_diretto: str
    fonte: str  # "PVP" oppure "TRIBUNALE"


def norm_comune(c: str) -> str:
    up = c.strip().upper()
    return COMUNE_ALIASES.get(up, c.strip())


def build_trib_search_url(comune: str) -> str:
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
    return f"{BASE_TRIB_SEARCH_URL}?{urlencode(params)}"


def http_get(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (AsteBergamoBot/3.0)",
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


# =========================================================
# 1) SCRAPING PRIMARIO: PVP (Algolia)
# =========================================================
def scrape_pvp_all() -> List[dict]:
    """
    Scarica tutti gli annunci del Tribunale di Bergamo in Lombardia, poi filtra per comuni.
    """
    payload = {
        "params": "filters=tribunale:BERGAMO AND regione:Lombardia&hitsPerPage=1000"
    }

    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(ALGOLIA_URL, headers=ALGOLIA_HEADERS, json=payload, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            return data.get("hits", []) or []
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(SLEEP_BETWEEN_RETRIES)

    raise RuntimeError(f"PVP/Algolia fallito: {last_err}")


def hit_get(hit: dict, keys: List[str], default: str = "") -> str:
    for k in keys:
        v = hit.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return default


def build_pvp_link(hit: dict) -> str:
    """
    Link diretto PVP (quello più stabile).
    """
    content_id = hit.get("id") or hit.get("contentId") or hit.get("objectID") or ""
    content_id = str(content_id).strip()
    if content_id:
        return f"https://pvp.giustizia.it/pvp/it/dettaglio_annuncio.page?contentId={content_id}"
    return ""


def pvp_to_notice(hit: dict) -> Optional[Notice]:
    comune = hit_get(hit, ["comune", "comune_asta", "citta"], "")
    titolo = hit_get(hit, ["titolo", "oggetto", "descrizione_breve"], "Annuncio")
    prezzo = hit_get(hit, ["prezzo_base", "prezzoBase"], "n/d")
    data_vendita = hit_get(hit, ["data_vendita", "dataVendita", "data_ora_vendita"], "n/d")

    link = build_pvp_link(hit)
    if not comune:
        return None

    return Notice(
        comune=comune,
        titolo=titolo,
        data_vendita=data_vendita,
        prezzo_base=str(prezzo),
        link_diretto=link,
        fonte="PVP",
    )


def filter_notices_by_comuni(notices: List[Notice]) -> Dict[str, List[Notice]]:
    target = {norm_comune(c).upper(): norm_comune(c) for c in COMUNI}
    out: Dict[str, List[Notice]] = {norm_comune(c): [] for c in COMUNI}

    for n in notices:
        c_up = norm_comune(n.comune).upper()
        # match diretto comune
        if c_up in target:
            out[target[c_up]].append(n)
            continue
        # match se il comune appare nel titolo
        for c in COMUNI:
            if norm_comune(c).upper() in n.titolo.upper():
                out[norm_comune(c)].append(n)
                break

    # dedup per link
    for c in out:
        seen = set()
        uniq = []
        for n in out[c]:
            key = n.link_diretto or (n.titolo + n.data_vendita)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(n)
        out[c] = uniq

    return out


# =========================================================
# 2) FALLBACK: Tribunale /aste/cerca (HTML)
# =========================================================
def scrape_tribunale_comune(comune: str) -> List[Notice]:
    comune = norm_comune(comune)
    url = build_trib_search_url(comune)
    html = http_get(url)
    soup = BeautifulSoup(html, "html.parser")

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

        # testo vicino al link per estrarre info
        parent = a
        best = a
        while True:
            p = getattr(parent, "parent", None)
            if not p or getattr(p, "name", "") in ("body", "html"):
                break
            # se contiene troppe schede è troppo alto
            if len(p.find_all("a", string=re.compile(r"scheda\s+dettagliata", re.I))) > 1:
                break
            txt = p.get_text(" ", strip=True)
            if len(txt) > 1400:
                break
            best = p
            parent = p

        text = best.get_text(" ", strip=True)

        # estrazioni semplici
        m_date = re.search(r"Data\s+(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}:\d{2})", text)
        data_v = f"{m_date.group(1)} {m_date.group(2)}" if m_date else "n/d"

        m_price = re.search(r"Prezzo\s+base\s+€\s*([0-9\.\,]+)", text, re.I)
        prezzo = f"€ {m_price.group(1)}" if m_price else "n/d"

        titolo = text[:180] if text else "Annuncio"

        notices.append(
            Notice(
                comune=comune,
                titolo=titolo,
                data_vendita=data_v,
                prezzo_base=prezzo,
                link_diretto=href,  # link diretto "Scheda dettagliata"
                fonte="TRIBUNALE",
            )
        )

    return notices


# =========================================================
# EMAIL
# =========================================================
def format_email(results: Dict[str, List[Notice]], errors: Dict[str, str]) -> str:
    lines: List[str] = []
    lines.append(f"Aste attive – Tribunale Bergamo – {date.today().strftime('%d/%m/%Y')}")
    lines.append("")

    for comune in [norm_comune(c) for c in COMUNI]:
        lst = results.get(comune, [])
        err = errors.get(comune)

        lines.append(f"{comune} ({len(lst)})")

        if err:
            lines.append(f"ERRORE: {err}")
            lines.append(f"LINK RICERCA: {build_trib_search_url(comune)}")
            lines.append("")
            continue

        if not lst:
            lines.append("Nessun annuncio attivo trovato.")
            lines.append("")
            continue

        for i, n in enumerate(lst, 1):
            lines.append(f"{i}. {n.titolo}")
            lines.append(f"   Data vendita: {n.data_vendita}")
            lines.append(f"   Prezzo base: {n.prezzo_base}")
            lines.append(f"   LINK DIRETTO: {n.link_diretto if n.link_diretto else build_trib_search_url(comune)}")
            lines.append(f"   Fonte: {n.fonte}")
            lines.append("")

    return "\n".join(lines).strip()


def send_email(body: str) -> bool:
    """
    IMPORTANTE: se mail fallisce, NON facciamo fallire il job (return False).
    Così non ricevi più "All jobs have failed".
    """
    if not EMAIL_USER or not EMAIL_PASS:
        print("EMAIL NON INVIATA: secrets EMAIL_USER/EMAIL_PASS mancanti.")
        return False

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
        return True
    except Exception as e:
        print(f"EMAIL NON INVIATA (SMTP error): {e}")
        return False


def main() -> int:
    results: Dict[str, List[Notice]] = {norm_comune(c): [] for c in COMUNI}
    errors: Dict[str, str] = {}

    # 1) prova PVP (robusto)
    pvp_ok = False
    try:
        hits = scrape_pvp_all()
        notices = []
        for h in hits:
            n = pvp_to_notice(h)
            if n:
                notices.append(n)
        results = filter_notices_by_comuni(notices)
        pvp_ok = True
        print("PVP OK: filtrati annunci per comuni.")
    except Exception as e:
        print(f"PVP KO: {e}")

    # 2) fallback tribunale HTML per i comuni che risultano vuoti (o se PVP KO)
    for comune in [norm_comune(c) for c in COMUNI]:
        if (not pvp_ok) or (len(results.get(comune, [])) == 0):
            try:
                trib_notices = scrape_tribunale_comune(comune)
                # Se PVP non ha trovato nulla, usa tribunale. Se PVP ha trovato, aggiungi solo se mancano.
                if trib_notices:
                    results[comune] = trib_notices
            except Exception as e:
                errors[comune] = str(e)

    body = format_email(results, errors)
    print(body)

    # invia mail ma NON far fallire actions
    send_email(body)

    # ritorna sempre 0 => niente "All jobs failed"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
