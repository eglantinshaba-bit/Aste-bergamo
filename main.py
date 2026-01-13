import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os

# Configurazione Mail
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
EMAIL_RECEIVER = "eglantinshaba@gmail.com"

# Comuni da monitorare (incluso Lalio/Lallio)
COMUNI_TARGET = ["AZZANO SAN PAOLO", "STEZZANO", "GRASSOBBIO", "LALLIO", "LALIO", "ZANICA", "TREVIOLO"]

def cerca_aste_ministero():
    print("Interrogazione database Ministero della Giustizia - Tribunale di Bergamo...")
    risultati = []
    
    # API ufficiale che alimenta i portali dei tribunali
    url = "https://wvgafsu780-dsn.algolia.net/1/indexes/PROPORTAL/query"
    params = {
        "x-algolia-application-id": "WVGAFSU780",
        "x-algolia-api-key": "685934188b4952026856019688439e6a"
    }
    
    # Filtriamo per Tribunale di Bergamo e Provincia BG
    # Cerchiamo fino a 500 annunci per non perdere nulla
    payload = {
        "params": "filters=tribunale:BERGAMO AND provincia:BG&hitsPerPage=500"
    }
    
    try:
        response = requests.post(url, params=params, json=payload, timeout=20)
        data = response.json()
        hits = data.get('hits', [])
        
        print(f"Analisi di {len(hits)} annunci totali del Tribunale di Bergamo...")

        for hit in hits:
            # Estraiamo i dati
            comune_asta = str(hit.get('comune', '')).upper()
            titolo = str(hit.get('titolo', '')).upper()
            prezzo = hit.get('prezzo_base', 0)
            id_asta = hit.get('id')
            link = f"https://pvp.giustizia.it/pvp/it/dettaglio_annuncio.page?contentId={id_asta}"

            # Controllo se il comune è nella tua lista
            if any(c in comune_asta for c in COMUNI_TARGET) or any(c in titolo for c in COMUNI_TARGET):
                # Formattazione per la tua regola (minuscolo, prima lettera maiuscola)
                testo_pulito = f"Comune: {comune_asta.capitalize()}\nOggetto: {titolo[:80].lower().capitalize()}...\nPrezzo: {prezzo}€\nLink: {link}"
                
                if testo_pulito not in risultati:
                    risultati.append(testo_pulito)
                    print(f"Trovata corrispondenza: {comune_asta}")

    except Exception as e:
        print(f"Errore API: {e}")
        return [f"errore tecnico database: {str(e)}"]

    return risultati

def invia_mail(lista):
    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = EMAIL_RECEIVER
    
    if lista and "errore" not in lista[0]:
        msg['Subject'] = f"Aggiornamento aste: {len(lista)} annunci"
        corpo = "Ecco le aste trovate per i tuoi comuni:\n\n" + "\n\n---\n\n".join(lista)
    elif lista and "errore" in lista[0]:
        msg['Subject'] = "Errore tecnico agente"
        corpo = f"L'agente ha avuto un problema: {lista[0]}"
    else:
        msg['Subject'] = "Nessun annuncio trovato"
        corpo = "Ho controllato il database del Tribunale di Bergamo, ma non ci sono nuove aste nei tuoi comuni oggi."

    msg.attach(MIMEText(corpo, 'plain'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)
        server.quit()
        print("Mail inviata correttamente.")
    except Exception as e:
        print(f"Errore invio mail: {e}")

if __name__ == "__main__":
    esito = cerca_aste_ministero()
    invia_mail(esito)
