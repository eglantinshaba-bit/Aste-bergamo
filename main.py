import requests
from bs4 import BeautifulSoup
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os

# CONFIGURAZIONI UTENTE
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
EMAIL_RECEIVER = "eglantinshaba@gmail.com"

# Elenco comuni per Astegiudiziarie.it (formato URL)
# Nota: Usiamo i nomi separati da trattino per l'indirizzo web
COMUNI = ["azzano-san-paolo", "stezzano", "grassobbio", "lallio", "zanica", "treviolo"]

def cerca_aste_nuovo_sito():
    print("Avvio scansione su Astegiudiziarie.it...")
    risultati_finali = []
    
    # Headers per sembrare un utente reale da cellulare
    headers = {
        'User-Agent': 'Mozilla/5.0 (Linux; Android 10; SM-A556B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'
    }

    for comune in COMUNI:
        url_ricerca = f"https://www.astegiudiziarie.it/vendite-giudiziarie-immobiliari/bergamo/{comune}"
        print(f"Controllo: {comune}...")
        
        try:
            response = requests.get(url_ricerca, headers=headers, timeout=20)
            if response.status_code != 200:
                continue
                
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Cerchiamo i blocchi degli annunci (le "card")
            annunci = soup.find_all('div', class_='annuncio')
            
            for asta in annunci:
                # Estraiamo il titolo/descrizione
                titolo_tag = asta.find('h4') or asta.find('div', class_='titolo')
                titolo = titolo_tag.get_text(strip=True) if titolo_tag else "Immobile"
                
                # Estraiamo il prezzo
                prezzo_tag = asta.find('div', class_='prezzo') or asta.find('span', class_='prezzo')
                prezzo = prezzo_tag.get_text(strip=True) if prezzo_tag else "Vedi sito"
                
                # Estraiamo il link
                link_tag = asta.find('a', href=True)
                link_parziale = link_tag['href']
                link_completo = f"https://www.astegiudiziarie.it{link_parziale}" if not link_parziale.startswith('http') else link_parziale
                
                entry = (f"📍 LOCALITÀ: {comune.replace('-', ' ').upper()}\n"
                         f"🏠 INFO: {titolo[:100]}...\n"
                         f"💰 PREZZO: {prezzo}\n"
                         f"🔗 LINK: {link_completo}")
                
                if entry not in risultati_finali:
                    risultati_finali.append(entry)
                    
        except Exception as e:
            print(f"Errore su {comune}: {e}")

    return risultati_finali

def invia_mail(aste):
    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = EMAIL_RECEIVER
    
    # Titolo mail semplice come preferisci
    if aste:
        msg['Subject'] = f"aggiornamento aste: {len(aste)} annunci trovati"
        corpo = "Ecco gli annunci trovati su astegiudiziarie.it:\n\n" + "\n\n---\n\n".join(aste)
    else:
        msg['Subject'] = "nessun annuncio oggi"
        corpo = "Ho controllato astegiudiziarie.it ma non ci sono nuove aste per i tuoi comuni."

    msg.attach(MIMEText(corpo, 'plain'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASS)
        server.send_message(msg)
        server.quit()
        print("Mail inviata!")
    except Exception as e:
        print(f"Errore mail: {e}")

if __name__ == "__main__":
    lista = cerca_aste_nuovo_sito()
    invia_mail(lista)
