import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os

# CONFIGURAZIONI UTENTE
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
EMAIL_RECEIVER = "eglantinshaba@gmail.com"

# Elenco comuni (inclusa la variante per Lalio)
COMUNI_TARGET = ["AZZANO SAN PAOLO", "STEZZANO", "GRASSOBBIO", "LALLIO", "LALIO", "ZANICA", "TREVIOLO"]

def cerca_tutte_le_aste():
    print("🚀 Avvio scansione totale Provincia di Bergamo...")
    risultati_finali = []
    
    # API ufficiale del Portale Vendite Pubbliche
    url = "https://wvgafsu780-dsn.algolia.net/1/indexes/PROPORTAL/query"
    params = {
        "x-algolia-application-id": "WVGAFSU780",
        "x-algolia-api-key": "685934188b4952026856019688439e6a"
    }
    
    # Chiediamo TUTTE le aste in provincia di Bergamo (BG) senza filtri restrittivi
    # Aumentiamo i risultati a 200 per essere sicuri di prendere tutto
    payload = {
        "params": "filters=provincia:BG AND regione:Lombardia&hitsPerPage=200"
    }
    
    try:
        response = requests.post(url, params=params, json=payload, timeout=25)
        data = response.json()
        hits = data.get('hits', [])
        
        print(f"Scansione completata. Analisi di {len(hits)} annunci totali in provincia...")

        for hit in hits:
            comune_asta = str(hit.get('comune', '')).upper()
            titolo = str(hit.get('titolo', '')).upper()
            indirizzo = str(hit.get('indirizzo', '')).upper()
            
            # Controlliamo se uno dei tuoi comuni è presente nel testo o nel campo comune
            match = False
            for c in COMUNI_TARGET:
                if c in comune_asta or c in titolo or c in indirizzo:
                    match = True
                    comune_trovato = c
                    break
            
            if match:
                id_asta = hit.get('id')
                link = f"https://pvp.giustizia.it/pvp/it/dettaglio_annuncio.page?contentId={id_asta}"
                prezzo = hit.get('prezzo_base', 'N/A')
                
                entry = (f"📍 COMUNE: {comune_asta}\n"
                         f"🏠 OGGETTO: {titolo[:100]}...\n"
                         f"💰 PREZZO: {prezzo}€\n"
                         f"🔗 LINK: {link}")
                
                if entry not in risultati_finali:
                    risultati_finali.append(entry)
                    print(f"✅ TROVATA: {comune_asta}")

    except Exception as e:
        print(f"❌ Errore durante la scansione: {e}")

    return risultati_finali

def invia_mail(aste):
    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = EMAIL_RECEIVER
    
    if aste:
        msg['Subject'] = f"🔔 {len(aste)} ASTE TROVATE a Bergamo"
        corpo = "L'agente ha trovato gli annunci attivi per i tuoi comuni:\n\n" + "\n\n---\n\n".join(aste)
    else:
        msg['Subject'] = "🔍 Report Aste: Nessuna novità"
        corpo = "L'agente ha controllato tutto il database di Bergamo ma attualmente non ci sono aste attive per i comuni selezionati."

    msg.attach(MIMEText(corpo, 'plain'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)
        server.quit()
        print("📧 Mail inviata!")
    except Exception as e:
        print(f"❌ Errore mail: {e}")

if __name__ == "__main__":
    lista = cerca_tutte_le_aste()
    invia_mail(lista)
