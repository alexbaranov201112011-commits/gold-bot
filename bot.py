from flask import Flask, request
import requests
import os

app = Flask(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN")

def get_gold_price():
    return 4330.10

def format_msg(price, is_buy):
    if is_buy:
        signal = "LONG (BUY)"
        trend = "vverh"
        sl = 4331.0
        tp1 = 4363.0
        tp2 = 4378.0
    else:
        signal = "SHORT (SELL)"
        trend = "vniz"
        sl = 4355.0
        tp1 = 4300.0
        tp2 = 4250.0

    return f"""📊 STAR TRADER | GOLD (XAUUSD)

💰 Cena: {price:.2f}$ 

📈 SIGNAL: {signal}

🔷 Vhod: 4341.0 - 4345.0
🔻 SL: {sl:.1f}
🎯 TP1: {tp1:.1f}
🎯 TP2: {tp2:.1f}

⏱️ M15: trend {trend}
⚠️ Vhodi po zone!

STAR TRADER V3.4 AUTO LIVE"""

def send_tg(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except:
        pass

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
        txt = data["message"].get("text", "")
        if txt in ["/gold", "/check", "/start"]:
            price = get_gold_price()
            is_buy = price >= 4341.0
            send_tg(chat_id, format_msg(price, is_buy))
    return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
