import requests
from bs4 import BeautifulSoup
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import os

# CONFIGURAZIONI
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASS")
EMAIL_RECEIVER = "eglantinshaba@gmail.com"
TARGET_URL = "https://www.tribunale.bergamo.it/aste/"
KEYWORDS = ["Stezzano", "Azzano", "Zanica", "Grassobbio", "Treviolo", "Colognola"]

def check_aste():
    print("Inizio scansione...")
    findings = []
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(TARGET_URL, headers=headers, timeout=20)
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # Prende tutto il testo della pagina
        text_content = soup.get_text()
        
        # Cerca le parole chiave
        for city in KEYWORDS:
            if city.lower() in text_content.lower():
                findings.append(city)
                
    except Exception as e:
        print(f"Errore: {e}")
        return []

    # Rimuove duplicati
    return list(set(findings))

def send_email(cities):
    msg = MIMEMultipart()
    msg['From'] = EMAIL_USER
    msg['To'] = EMAIL_RECEIVER
    msg['Subject'] = f"🔔 Aste trovate a: {', '.join(cities)}"
    
    body = f"Ciao, ho trovato le parole chiave {cities} sul sito {TARGET_URL}. Controlla subito!"
    msg.attach(MIMEText(body, 'plain'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(EMAIL_USER, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        print("Email inviata.")
    except Exception as e:
        print(f"Errore mail: {e}")

if __name__ == "__main__":
    cities_found = check_aste()
    if cities_found:
        print(f"Trovato: {cities_found}")
        send_email(cities_found)
    else:
        print("Nessuna città trovata oggi.")
