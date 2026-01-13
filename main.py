import requests
from bs4 import BeautifulSoup
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os
import time

# configurazione utente
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
EMAIL_RECEIVER = "eglantinshaba@gmail.com"

# comuni target (testo da cercare ovunque)
COMUNI = ["AZZANO SAN PAOLO", "STEZZANO", "GRASSOBBIO", "LALLIO", "LALIO", "ZANICA", "TREVIOLO"]

def cerca_aste():
    # usiamo una sessione avanzata che imita perfettamente un cellulare
    session = requests.Session()
    headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'it-it',
        'Referer': 'https://www.google.it/'
    }
    
    risultati = []
    
    # carichiamo la pagina generale di bergamo (più ricca di dati e meno protetta)
    url_base = "https://www.astegiudiziarie.it/vendite-giudiziarie-immobiliari/bergamo"
    
    try:
        print("avvio scansione intelligente...")
        response = session.get(url_base, headers=headers, timeout=30)
        
        if response.status_code != 200:
            return [f"errore di connessione al sito (codice {response.status_code})"]

        soup = BeautifulSoup(response.text, 'html.parser')
        
        # cerchiamo tutti i blocchi che contengono aste
        annunci = soup.find_all(['div', 'a'], class_=lambda x: x and ('annuncio' in x or 'listing' in x or 'item' in x))
        
        # se non troviamo blocchi, cerchiamo tutti i link che portano a una scheda-asta
        if not annunci:
            annunci = soup.find_all('a', href=True)

        for item in annunci:
            testo_asta = item.get_text(" ", strip=True).upper()
            
            # verifichiamo se l'asta contiene uno dei tuoi comuni
            for comune in COMUNI:
                if comune in testo_asta:
                    # estraiamo il link
                    href = item.get('href', '')
                    if not href.startswith('http'):
                        link_completo = f"https://www.astegiudiziarie.it{href}"
                    else:
                        link_completo = href
                    
                    if '/scheda-asta/' in link_completo:
                        # pulizia del titolo per la tua regola delle maiuscole
                        info = testo_asta[:100].lower().capitalize()
                        entry = f"comune: {comune.lower().capitalize()}\ninfo: {info}...\nlink: {link_completo}"
                        
                        if entry not in risultati:
                            risultati.append(entry)
                    break 

    except Exception as e:
        print(f"errore: {e}")
        return [f"errore tecnico: {str(e).lower()}"]

    return risultati

def invia_notifica(lista):
    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = EMAIL_RECEIVER
    
    # applicazione regola: solo una maiuscola iniziale
    if lista and "errore" not in lista[0]:
        msg['Subject'] = "aggiornamento aste bergamo"
        corpo = f"ciao, ecco cosa ho trovato oggi:\n\n" + "\n\n---\n\n".join(lista)
    elif lista and "errore" in lista[0]:
        msg['Subject'] = "errore tecnico agente"
        corpo = f"l'agente ha riscontrato un problema: {lista[0]}"
    else:
        msg['Subject'] = "nessun annuncio trovato"
        corpo = "ho controllato ma non ci sono nuove aste nei tuoi comuni oggi."

    msg.attach(MIMEText(corpo, 'plain'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)
        server.quit()
        print("notifica inviata")
    except Exception as e:
        print(f"errore mail: {e}")

if __name__ == "__main__":
    esito = cerca_aste()
    invia_notifica(esito)
