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

# comuni da controllare (formato per l'indirizzo del sito)
COMUNI = ["azzano-san-paolo", "stezzano", "grassobbio", "lallio", "zanica", "treviolo"]

def cerca_aste():
    # usiamo una sessione per mantenere i cookie e sembrare umani
    session = requests.Session()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7',
    }
    
    risultati = []
    
    for comune in COMUNI:
        url = f"https://www.astegiudiziarie.it/vendite-giudiziarie-immobiliari/bergamo/{comune}"
        print(f"controllo comune: {comune}")
        
        try:
            response = session.get(url, headers=headers, timeout=30)
            if response.status_code != 200:
                print(f"errore accesso {comune}: status {response.status_code}")
                continue
                
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # cerchiamo i link che portano alle schede delle aste
            # il sito usa spesso la classe 'listing-item' o link con 'scheda-asta'
            links = soup.find_all('a', href=True)
            
            for link in links:
                href = link['href']
                if '/scheda-asta/' in href:
                    link_completo = f"https://www.astegiudiziarie.it{href}"
                    
                    # cerchiamo il prezzo vicino al link (solitamente in un div superiore)
                    card = link.find_parent('div')
                    prezzo = "controlla sul sito"
                    if card:
                        prezzo_tag = card.find(string=lambda t: '€' in t)
                        if prezzo_tag:
                            prezzo = prezzo_tag.strip()

                    entry = f"paese: {comune.replace('-', ' ')}\nprezzo: {prezzo}\nlink: {link_completo}"
                    if entry not in risultati:
                        risultati.append(entry)
            
            # piccola pausa per non essere bloccati
            time.sleep(2)
            
        except Exception as e:
            print(f"errore tecnico su {comune}: {e}")

    return list(set(risultati))

def invia_mail(lista_aste):
    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = EMAIL_RECEIVER
    
    # applico la tua regola: solo la prima lettera maiuscola
    if lista_aste:
        msg['Subject'] = "nuovi annunci aste trovati"
        testo = "ecco i risultati trovati oggi:\n\n" + "\n\n---\n\n".join(lista_aste)
    else:
        msg['Subject'] = "nessun annuncio trovato"
        testo = "ho controllato ma oggi non ci sono nuove aste nei tuoi comuni."

    msg.attach(MIMEText(testo.lower().capitalize(), 'plain'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)
        server.quit()
        print("mail inviata correttamente")
    except Exception as e:
        print(f"errore invio mail: {e}")

if __name__ == "__main__":
    trovate = cerca_aste()
    invia_mail(trovate)
