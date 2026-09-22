from flask import Flask, request
import requests
import os

app = Flask(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN")

BUY_LOW = 4341.0
BUY_HIGH = 4345.0
SL_BUY = 4331.0
SL_SELL = 4355.0
TP1 = 4363.0
TP2 = 4378.0

def get_gold_price():
    return 4330.1

def format_msg(price, is_buy):
    sig = "LONG (BUY)" if is_buy else "SHORT (SELL)"
    trend = "vverh" if is_buy else "vniz"
    sl = SL_BUY if is_buy else SL_SELL
    return (
        f"STAR TRADER | GOLD (XAUUSD)\n\n"
        f"Cena: {price:.2f}$\n"
        f"SIGNAL: {sig}\n\n"
        f"Vhod: {BUY_LOW:.1f} - {BUY_HIGH:.1f}\n"
        f"SL: {sl:.1f}\n"
        f"TP1: {TP1:.1f}\n"
        f"TP2: {TP2:.1f}\n\n"
        f"M15: trend {trend}\n"
        f"Vhodi po zone!\n\n"
        f"STAR TRADER V3.4 AUTO LIVE"
    )

def send_tg(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)

@app.route("/")
def home():
    return "STAR TRADER V3.4 AUTO LIVE", 200

@app.route("/check")
def check():
    return f"checked {get_gold_price()}", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if data and "message" in data:
        chat_id = data["message"]["chat"]["id"]
        txt = data["message"].get("text","")
        if txt in ["/gold","/check","/start"]:
            price = get_gold_price()
            is_buy = price >= BUY_LOW
            send_tg(chat_id, format_msg(price, is_buy))
    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
