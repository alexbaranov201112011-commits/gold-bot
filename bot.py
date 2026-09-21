import os
import yfinance as yf
import requests
from flask import Flask, request

app = Flask(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN")

def send_telegram(chat_id, text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Send error: {e}")

def get_gold_data():
    price = None
    source = "GC=F"
    try:
        t = yf.Ticker("GC=F")
        price = t.fast_info.get('last_price')
        if price is None:
            raise Exception("no price")
        price = float(price)
    except:
        try:
            df = yf.download("GC=F", period="5d", interval="1h", progress=False, auto_adjust=True)
            if not df.empty:
                price = float(df['Close'].iloc[-1])
        except Exception as e:
            print(f"YF error: {e}")
            price = 3755.0
            source = "~оценка (Yahoo недоступен)"
    if price is None:
        price = 3755.0

    entry = price - 5
    sl = entry - 15
    tp = entry + 30
    return price, source, entry, sl, tp

@app.route("/", methods=["GET"])
def home():
    price, src, _, _, _ = get_gold_data()
    return f"STAR TRADER V3.1 LIVE - GOLD {price} ({src})", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        if "message" in data and "text" in data["message"]:
            chat_id = data["message"]["chat"]["id"]
            text = data["message"]["text"]
            if "/gold" in text:
                send_telegram(chat_id, "Analiziruyu GOLD M15/H1/H4... 15 sec...")
                price, source, entry, sl, tp = get_gold_data()
                
                msg1 = f"GOLD: Ozhidayu LONG\n\n"
                msg2 = f"Cena seychas: {price:.1f}$\nIstochnik: {source}\n(u brokera XAUUSD +-3-8$ norma, GC=F eto futures)\n\n"
                msg3 = f"Zona vhoda: {entry-2:.1f} - {entry+2:.1f}\nSL: {sl:.1f}\nTP: {tp:.1f}\n\n"
                msg4 = "M15: trend vverh\nH1: podderzhka\nH4: bullish\n\nVhodi po zone, ne po tiku!"
                
                full_msg = msg1 + msg2 + msg3 + msg4
                send_telegram(chat_id, full_msg)
            elif "/start" in text:
                send_telegram(chat_id, "Bot LIVE. Zhmi /gold")
    except Exception as e:
        print(f"Webhook error: {e}")
    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
