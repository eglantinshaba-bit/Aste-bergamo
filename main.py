import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
import time

# configurazione mail
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
EMAIL_RECEIVER = "eglantinshaba@gmail.com"

# comuni target
COMUNI = ["AZZANO SAN PAOLO", "STEZZANO", "GRASSOBBIO", "LALLIO", "ZANICA", "TREVIOLO"]

def cerca_aste_totale():
    print("avvio ricerca ad alto potenziale...")
    risultati = []
    
    # usiamo l'api ufficiale del ministero (pvp)
    url = "https://wvgafsu780-dsn.algolia.net/1/indexes/PROPORTAL/query"
    params = {
        "x-algolia-application-id": "WVGAFSU780",
        "x-algolia-api-key": "685934188b4952026856019688439e6a"
    }
    
    # cerchiamo direttamente per tribunale di bergamo
    payload = {
        "params": "filters=tribunale:BERGAMO AND regione:Lombardia&hitsPerPage=1000"
    }
    
    # tentativo con 3 riprore in caso di errore di rete
    for i in range(3):
        try:
            response = requests.post(url, params=params, json=payload, timeout=30)
            if response.status_code == 200:
                data = response.json()
                hits = data.get('hits', [])
                
                for hit in hits:
                    comune_asta = str(hit.get('comune', '')).upper()
                    titolo = str(hit.get('titolo', '')).upper()
                    
                    if any(c in comune_asta for c in COMUNI) or any(c in titolo for c in COMUNI):
                        id_asta = hit.get('id')
                        link = f"https://pvp.giustizia.it/pvp/it/dettaglio_annuncio.page?contentId={id_asta}"
                        prezzo = hit.get('prezzo_base', 'n/a')
                        
                        entry = f"paese: {comune_asta.capitalize()}\nprezzo: {prezzo}€\nlink: {link}"
                        if entry not in risultati:
                            risultati.append(entry)
                break # se ha successo esce dal ciclo di riprova
        except Exception as e:
            print(f"tentativo {i+1} fallito: {e}")
            time.sleep(5) # aspetta 5 secondi prima di riprovare

    return risultati

def invia_mail(lista):
    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = EMAIL_RECEIVER
    
    # applicazione della tua regola: solo la prima lettera maiuscola
    if lista:
        msg['Subject'] = "nuovi annunci aste bergamo"
        testo = "ecco le aste trovate per i tuoi comuni:\n\n" + "\n\n---\n\n".join(lista)
    else:
        msg['Subject'] = "nessun annuncio trovato oggi"
        testo = "ho controllato il database ufficiale, ma non ci sono nuove aste per i tuoi comuni oggi."

    msg.attach(MIMEText(testo.lower().capitalize(), 'plain'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)
        server.quit()
        print("notifica inviata con successo")
    except Exception as e:
        print(f"errore mail: {e}")

if __name__ == "__main__":
    esito = cerca_aste_totale()
    invia_mail(esito)
