import os
import re
import json
import time
import smtplib
import requests
from dataclasses import dataclass, asdict
from typing import List, Dict, Tuple
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

# Stato (per invio solo se novità)
STATE_PATH = os.environ.get("STATE_PATH", ".state/state.json").strip()

# Se vuoi testare l’invio anche senza novità (solo per debug)
FORCE_EMAIL = os.environ.get("FORCE_EMAIL", "0").strip() == "1"


@dataclass
class Notice:
    comune: str
    titolo: str
    data_vendita: str
    prezzo_base: str
    link_diretto: str
    link_ricerca: str

    def fingerprint(self) -> str:
        """
        Identificatore univoco annuncio:
        - prima scelta: link diretto
        - fallback: titolo + data + prezzo
        """
        if self.link_diretto and self.link_diretto.startswith("http"):
            return self.link_diretto.strip()
        return f"{self.titolo}|{self.data_vendita}|{self.prezzo_base}".strip()


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
        "User-Agent": "Mozilla/5.0 (AsteBergamoBot/UPDATES)",
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

        data_v = extract_first(block_text, r"Data\s+(\d{2}/\d{2}/\d{4}\s*-\s*\d{2}:\d{2})", "n/d", re.I)
        prezzo = extract_first(block_text, r"Prezzo\s+base\s+€\s*([0-9\.\,]+)", "n/d", re.I)
        if prezzo != "n/d":
            prezzo = f"€ {prezzo}"

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
                link_diretto=href,  # ✅ LINK DIRETTO ANNUNCIO
                link_ricerca=url,
            )
        )

    return notices


# =========================
# STATE (solo se aggiornamenti)
# =========================
def load_state(path: str) -> Dict[str, List[str]]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): list(v) for k, v in data.items()}
        return {}
    except Exception:
        return {}


def save_state(path: str, state: Dict[str, List[str]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def diff_new(results: Dict[str, List[Notice]], prev_state: Dict[str, List[str]]) -> Dict[str, List[Notice]]:
    """
    ritorna solo i nuovi annunci (non visti prima).
    """
    new_map: Dict[str, List[Notice]] = {}
    for comune, notices in results.items():
        prev = set(prev_state.get(comune, []))
        new_items = [n for n in notices if n.fingerprint() not in prev]
        new_map[comune] = new_items
    return new_map


def build_next_state(results: Dict[str, List[Notice]]) -> Dict[str, List[str]]:
    return {comune: [n.fingerprint() for n in notices] for comune, notices in results.items()}


# =========================
# EMAIL
# =========================
def format_email_only_updates(new_items: Dict[str, List[Notice]]) -> str:
    out: List[str] = []
    out.append(f"NUOVI ANNUNCI – Tribunale di Bergamo – {time.strftime('%d/%m/%Y %H:%M')}")
    out.append("")

    total = 0
    for comune in [norm_comune(c) for c in COMUNI]:
        lst = new_items.get(comune, [])
        if not lst:
            continue

        out.append(f"{comune} ({len(lst)})")
        for i, n in enumerate(lst, 1):
            total += 1
            out.append(f"{i}. {n.titolo}")
            out.append(f"   Data vendita: {n.data_vendita}")
            out.append(f"   Prezzo base: {n.prezzo_base}")
            out.append(f"   LINK DIRETTO: {n.link_diretto}")
            out.append(f"   LINK RICERCA: {n.link_ricerca}")
            out.append("")
        out.append("")

    out.append(f"Totale nuovi annunci: {total}")
    return "\n".join(out).strip()


def send_email(subject: str, body: str) -> None:
    """
    Se SMTP fallisce, NON deve far fallire il job.
    """
    if not EMAIL_USER or not EMAIL_PASS:
        print("EMAIL NON INVIATA: manca EMAIL_USER o EMAIL_PASS nei secrets.")
        return

    msg = MIMEMultipart()
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
        print("Email inviata OK.")
    except Exception as e:
        print(f"EMAIL NON INVIATA (errore SMTP): {e}")


def main() -> int:
    # 1) scrape
    results: Dict[str, List[Notice]] = {norm_comune(c): [] for c in COMUNI}
    for comune in COMUNI:
        c = norm_comune(comune)
        try:
            results[c] = scrape_comune(c)
        except Exception as e:
            # se un comune fallisce, non blocchiamo
            print(f"[ERRORE] {c}: {e}")
            results[c] = []

    # 2) carica stato precedente
    prev = load_state(STATE_PATH)

    # 3) calcola nuovi annunci
    new_items = diff_new(results, prev)

    any_new = any(len(v) > 0 for v in new_items.values())
    total_new = sum(len(v) for v in new_items.values())

    # 4) salva nuovo stato sempre
    next_state = build_next_state(results)
    save_state(STATE_PATH, next_state)
    print(f"Stato salvato in {STATE_PATH}")

    # 5) invia mail solo se ci sono aggiornamenti
    if any_new or FORCE_EMAIL:
        subject = f"Nuovi annunci aste BG ({total_new})"
        body = format_email_only_updates(new_items) if any_new else "FORCE_EMAIL attivo: nessuna novità reale."
        send_email(subject, body)
    else:
        print("Nessun annuncio nuovo: nessuna email inviata.")

    # ✅ job sempre SUCCESS
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
