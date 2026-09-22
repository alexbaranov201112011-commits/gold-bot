from flask import Flask, request
import requests
import os

app = Flask(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN")

def get_market_data():
    try:
        # Берем свечи золота PAXG = XAU с Binance
        url = "https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=15m&limit=100"
        data = requests.get(url, timeout=10).json()
        closes = [float(c[4]) for c in data]
        price = closes[-1]

        # EMA 50 и EMA 200
        def ema(arr, period):
            k = 2 / (period + 1)
            e = arr[0]
            for p in arr[1:]:
                e = p * k + e * (1 - k)
            return e

        ema50 = ema(closes[-60:], 50)
        ema200 = ema(closes, 200) if len(closes) >= 200 else ema(closes, 50)

        # RSI 14
        gains = []
        losses = []
        for i in range(1, 15):
            diff = closes[-i] - closes[-i-1]
            if diff > 0:
                gains.append(diff)
            else:
                losses.append(abs(diff))
        avg_gain = sum(gains)/14 if gains else 0.1
        avg_loss = sum(losses)/14 if losses else 0.1
        rs = avg_gain / avg_loss if avg_loss!= 0 else 10
        rsi = 100 - (100 / (1 + rs))

        return price, ema50, ema200, rsi
    except Exception as e:
        print(f"market error: {e}")
        # fallback
        try:
            p = requests.get("https://api.gold-api.com/price/XAU", timeout=5).json()["price"]
            return float(p), 0, 0, 50
        except:
            return 4330.0, 0, 0, 50

def analyze(price, ema50, ema200, rsi):
    # Умная логика
    if price > ema50 and rsi > 45 and rsi < 75:
        return True, f"EMA50 выше, RSI {rsi:.0f} - быки"
    elif price < ema50 and rsi < 55:
        return False, f"EMA50 ниже, RSI {rsi:.0f} - медведи"
    else:
        # если запутался, смотрим зону 4341
        return price >= 4341, f"Боковик, RSI {rsi:.0f}, смотрю зону 4341"

def format_msg(price, ema50, rsi, is_buy, reason):
    signal = "LONG (BUY) 🟢" if is_buy else "SHORT (SELL) 🔴"
    trend = "vverh" if is_buy else "vniz"

    if is_buy:
        sl = price - 20
        tp1 = price + 25
        tp2 = price + 45
    else:
        sl = price + 20
        tp1 = price - 25
        tp2 = price - 50

    return f"""📊 STAR TRADER V4 SMART | GOLD

💰 Cena: {price:.2f}$
📈 EMA50: {ema50:.2f}$
📉 RSI: {rsi:.1f}

📈 SIGNAL: {signal}
💡 Analiz: {reason}

🔷 Vhod: {price-2:.1f} - {price+2:.1f}
🔻 SL: {sl:.1f}
🎯 TP1: {tp1:.1f}
🎯 TP2: {tp2:.1f}

⏱️ M15: trend {trend}
🤖 Umniy analiz M15

STAR TRADER V4 SMART"""

def send_tg(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except:
        pass

@app.route("/")
def home():
    p, e50, e200, rsi = get_market_data()
    return f"SMART BOT OK | Price: {p} EMA: {e50:.2f} RSI: {rsi:.1f}", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if data and "message" in data:
        chat_id = data["message"]["chat"]["id"]
        txt = data["message"].get("text", "")
        if txt in ["/gold", "/check", "/start"]:
            price, ema50, ema200, rsi = get_market_data()
            is_buy, reason = analyze(price, ema50, ema200, rsi)
            send_tg(chat_id, format_msg(price, ema50, rsi, is_buy, reason))
    return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
