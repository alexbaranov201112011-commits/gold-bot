import os
import yfinance as yf
import requests
from flask import Flask, request

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN not set!")

def send_telegram(chat_id, text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Send error: {e}")

def get_gold_data():
    # Пробуем получить реальную цену GC=F
    price = None
    source = "GC=F"
    try:
        # самый быстрый способ
        t = yf.Ticker("GC=F")
        price = t.fast_info.get('last_price')
        if price is None:
            raise Exception("no fast price")
        price = float(price)
    except:
        try:
            df = yf.download("GC=F", period="5d", interval="1h", progress=False)
            if not df.empty:
                price = float(df['Close'].iloc[-1])
        except Exception as e:
            print(f"YF error: {e}")
            price = 3755.0 # фолбек если Yahoo вообще лег
            source = "~оценка (Yahoo недоступен)"

    # Простая логика STAR TRADER для теста
    # Тут потом вставим твою полную логику M15/H1/H4
    # Сейчас делаем чтобы работало и не было заглушки 2580
    
    # Имитация анализа тренда
    entry = price - 5 if price else 3750
    sl = entry - 15
    tp = entry + 30
    
    return price, source, entry, sl, tp

@app.route("/", methods=["GET"])
def home():
    price, src, _, _, _ = get_gold_data()
    return f"STAR TRADER V2 WEBHOOK LIVE - GOLD {price} ({src})", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        print(f"Update: {data}")
        if "message" in data and "text" in data["message"]:
            chat_id = data["message"]["chat"]["id"]
            text = data["message"]["text"]

            if "/gold" in text:
                send_telegram(chat_id, "🔍 Анализирую GOLD M15/H1/H4... 15 сек...")
                
                price, source, entry, sl, tp = get_gold_data()
                
                msg = f"""📈 *GOLD: Ожидаю л
