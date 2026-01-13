import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os

# CONFIGURAZIONI UTENTE
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
EMAIL_RECEIVER = "eglantinshaba@gmail.com"

# Comuni da monitorare (incluso Lalio/Lallio)
COMUNI = ["AZZANO SAN PAOLO", "STEZZANO", "GRASSOBBIO", "LALLIO", "LALIO", "ZANICA", "TREVIOLO"]

def cerca_tutte_le_aste():
    print(f"Ricerca globale per: {COMUNI} in corso...")
    risultati_finali = []
    
    # API ufficiale del Portale Vendite Pubbliche (PVP)
    url = "https://wvgafsu780-dsn.algolia.net/1/indexes/PROPORTAL/query"
    params = {
        "x-algolia-application-id": "WVGAFSU780",
        "x-algolia-api-key": "685934188b4952026856019688439e6a"
    }
    
    for comune_ricerca in COMUNI:
        # Filtriamo per Regione Lombardia e Provincia Bergamo come richiesto
        payload = {
            "params": f"query={comune_ricerca}&filters=regione:Lombardia AND provincia:BG"
        }
        
        try:
            response = requests.post(url, params=params, json=payload, timeout=20)
            data = response.json()
            hits = data.get('hits', [])
            
            print(f"Comune {comune_ricerca}: trovati {len(hits)} annunci.")
            
            for hit in hits:
                id_asta = hit.get('id')
                # Costruiamo il link diretto all'annuncio
                link_diretto = f"https://pvp.giustizia.it/pvp/it/dettaglio_annuncio.page?contentId={id_asta}"
                titolo = hit.get('titolo', 'Senza Titolo').upper()
                prezzo = hit.get('prezzo_base', 'N/A')
                comune_effettivo = hit.get('comune', comune_ricerca).upper()
                
                # Creiamo la riga per l'email
                entry = (f"📍 LOCALITÀ: {comune_effettivo}\n"
                         f"🏠 DESCRIZIONE: {titolo[:100]}...\n"
                         f"💰 PREZZO: {prezzo}€\n"
                         f"🔗 LINK DIRETTO: {link_diretto}")
                
                if entry not in risultati_finali:
                    risultati_finali.append(entry)
                        
        except Exception as e:
            print(f"Errore tecnico su {comune_ricerca}: {e}")

    return risultati_finali

def invia_report_completo(aste):
    print(f"Preparazione invio email per {len(aste)} annunci...")
    
    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = EMAIL_RECEIVER
    msg['Subject'] = f"📊 REPORT ATTUALE ASTE: {len(aste)} Annunci trovati"
    
    intro = f"Ciao! Ecco l'elenco completo degli annunci attualmente disponibili per i tuoi comuni (Regione Lombardia - BG):\n\n"
    corpo = intro + "\n\n---\n\n".join(aste)
    
    msg.attach(MIMEText(corpo, 'plain'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)
        server.quit()
        print("✅ Email inviata con successo!")
    except Exception as e:
        print(f"❌ ERRORE INVIO MAIL: {e}")

if __name__ == "__main__":
    tutte_le_aste = cerca_tutte_le_aste()
    
    if tutte_le_aste:
        invia_report_completo(tutte_le_aste)
    else:
        # Se non trova nulla, invia comunque una mail di test per confermare che è vivo
        msg = MIMEMultipart()
        msg['From'] = EMAIL_USER
        msg['To'] = EMAIL_RECEIVER
        msg['Subject'] = "🔍 Test Agente: Nessun annuncio trovato"
        msg.attach(MIMEText("L'agente sta funzionando correttamente, ma attualmente non ci sono aste pubblicate per i comuni selezionati.", 'plain'))
        
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)
        server.quit()
        print("Mail di 'nessun risultato' inviata per test.")
