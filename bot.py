from flask import Flask, request
import requests
import os

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
# Твои уровни - можешь поменять
BUY_ZONE_LOW = 4341
BUY_ZONE_HIGH = 4345
SL_BUY = 4331
SL_SELL = 4355
TP1 = 4363
TP2 = 4378

def get_gold_price():
    # Тут твоя логика получения цены, пока ставлю заглушку
    # Замени на свой источник
    try:
        # пример: берем с твоего текущего /check
        return 4330.1
    except:
        return 4330.1

def format_beautiful(price, is_buy_zone):
    signal = "LONG (BUY)" if is_buy_zone else "SHORT (SELL)"
    trend = "vverh" if is_buy_zone else "vniz"
    
    return f"""📊 STAR TRADER | GOLD (XAUUSD)

💰 Cena: {price:.2f}$ 

📈 SIGNAL: {signal}

🔷 Vhod: {BUY_ZONE_LOW:.1f} - {BUY_ZONE_HIGH:.1f}
🔻 SL: {SL_BUY if is_buy_zone else SL_SELL:.1f}
🎯 TP1: {TP1:.1f}
🎯 TP2: {TP2:.1f}

⏱️ M15: trend {trend}
⚠️ Vhodi po zone!

STAR TRADER V3.4 AUTO LIVE
"""

def send_telegram(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text})

@app.route("/")
def home():
    # Это для UptimeRobot - только показывает что жив
    return "STAR TRADER V3.4 AUTO LIVE", 200

@app.route("/check")
def check_web():
    # Для браузера
    price = get_gold_price()
    return f"checked {price}"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")
        
        price = get_gold_price()
        # Логика: если цена выше зоны - BUY, ниже - SELL
        is_buy = price >= BUY_ZONE_LOW
        
        # Обе команды теперь дают КРАСИВЫЙ формат
        if text in ["/gold", "/check", "/start"]:
            msg = format_beautiful(price, is_buy)
            send_telegram(chat_id
