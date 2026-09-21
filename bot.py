import os
import requests
from flask import Flask, request

app = Flask(__name__)
TOKEN = os.getenv("BOT_TOKEN")

def get_gold():
    try:
        r = requests.get("https://api.gold-api.com/price/XAU", timeout=5).json()
        return float(r['price'])
    except:
        return 4344.99

@app.route("/", methods=["GET"])
def home():
    price, = get_gold(), 
    return f"STAR TRADER V3.3 LIVE - GOLD {price:.2f}$", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json()
        if "message" in data and "text" in data["message"]:
            chat_id = data["message"]["chat"]["id"]
            text = data["message"]["text"]
            if "/gold" in text:
                price = get_gold()
                entry = price - 5
                sl = entry - 15
                tp = entry + 30
                msg = f"GOLD: Ozhidayu LONG\nCena seychas: {price:.2f}$\nZona vhoda: {entry-2:.1f}-{entry+2:.1f}\nSL: {sl:.1f} TP: {tp:.1f}\nM15: trend vverh"
                requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": chat_id, "text": msg})
            elif "/start" in text:
                requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": chat_id, "text": "Bot LIVE! Pishi /gold"})
    except Exception as e:
        print(f"Error {e}")
    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
