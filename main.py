import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os

# CONFIGURAZIONI UTENTE
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
EMAIL_RECEIVER = "eglantinshaba@gmail.com"

# Comuni richiesti (escluso Colognola)
COMUNI = ["AZZANO SAN PAOLO", "STEZZANO", "GRASSOBBIO", "LALLIO", "LALIO", "ZANICA", "TREVIOLO"]

def cerca_aste_bergamo():
    print("Ricerca in corso: Regione Lombardia -> Provincia Bergamo...")
    risultati_finali = []
    
    # API ufficiale per interrogare i dati del Portale Vendite Pubbliche
    url = "https://wvgafsu780-dsn.algolia.net/1/indexes/PROPORTAL/query"
    params = {
        "x-algolia-agent": "Algolia for JavaScript (3.33.0)",
        "x-algolia-application-id": "WVGAFSU780",
        "x-algolia-api-key": "685934188b4952026856019688439e6a"
    }
    
    for comune_ricerca in COMUNI:
        # Filtro per Regione: Lombardia e Provincia: BG
        payload = {
            "params": f"query={comune_ricerca}&filters=regione:Lombardia AND provincia:BG"
        }
        
        try:
            response = requests.post(url, params=params, json=payload, timeout=20)
            hits = response.json().get('hits', [])
            
            for hit in hits:
                titolo = hit.get('titolo', '').upper()
                comune_asta = hit.get('comune', '').upper()
                id_asta = hit.get('id')
                
                # Link diretto all'annuncio identificato
                link_diretto = f"https://pvp.giustizia.it/pvp/it/dettaglio_annuncio.page?contentId={id_asta}"
                
                # Verifica che il comune sia nell'elenco desiderato
                if any(c in comune_asta for c in COMUNI):
                    entry = (f"📍 LOCALITÀ: {comune_asta}\n"
                             f"🏠 OGGETTO: {titolo[:100]}...\n"
                             f"💰 PREZZO BASE: {hit.get('prezzo_base', 'N/A')}€\n"
                             f"🔗 LINK DIRETTO: {link_diretto}")
                    
                    if entry not in risultati_finali:
                        risultati_finali.append(entry)
                        
        except Exception as e:
            print(f"Errore durante la ricerca di {comune_ricerca}: {e}")

    return risultati_finali

def invia_notifica(aste):
    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = EMAIL_RECEIVER
    msg['Subject'] = f"✅ AGGIORNAMENTO ASTE: {len(aste)} nuovi annunci"
    
    corpo = "L'agente ha filtrato le nuove aste in Lombardia (Bergamo) per i comuni selezionati:\n\n"
    corpo += "\n\n---\n\n".join(aste)
    msg.attach(MIMEText(corpo, 'plain'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)
        server.quit()
        print("Email inviata!")
    except Exception as e:
        print(f"Errore invio: {e}")

if __name__ == "__main__":
    lista = cerca_aste_bergamo()
    if lista:
        invia_notifica(lista)
    else:
        print("Nessun nuovo annuncio trovato per i comuni selezionati.")

