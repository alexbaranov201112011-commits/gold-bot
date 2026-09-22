from flask import Flask, request
import requests
import os

app = Flask(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN")

def get_price():
    for url in [
        "https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT",
        "https://data-api.binance.vision/api/v3/ticker/price?symbol=PAXGUSDT",
        "https://api.gold-api.com/price/XAU"
    ]:
        try:
            r = requests.get(url, timeout=5).json()
            if "price" in r:
                return float(r["price"])
            if "symbol" in str(r):
                # binance format might be list?
                pass
        except:
            continue
    return 4321.8

def get_klines():
    urls = [
        "https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=15m&limit=100",
        "https://data-api.binance.vision/api/v3/klines?symbol=PAXGUSDT&interval=15m&limit=100"
    ]
    for url in urls:
        try:
            data = requests.get(url, timeout=8, headers={"User-Agent":"Mozilla"}).json()
            if isinstance(data, list) and len(data) > 50:
                closes = [float(c[4]) for c in data]
                return closes
        except:
            continue
    return None

def get_market_data():
    price = get_price()
    closes = get_klines()
    if closes:
        # EMA50
        def ema(arr, p):
            k = 2/(p+1)
            e = arr[0]
            for v in arr[1:]:
                e = v*k + e*(1-k)
            return e
        ema50 = ema(closes[-70:], 50)
        # RSI
        gains=0; losses=0
        for i in range(1,15):
            diff = closes[-i] - closes[-i-1]
            if diff>0: gains+=diff
            else: losses+=abs(diff)
        avg_g = gains/14 if gains else 0.5
        avg_l = losses/14 if losses else 0.5
        rsi = 100 - (100/(1+avg_g/avg_l)) if avg_l!=0 else 55
        return price, ema50, rsi, True
    else:
        # нет свечей, считаем тренд по цене vs зоны
        return price, price-5, 50, False

def analyze(price, ema50, rsi, has_data):
    if has_data:
        if price > ema50 and rsi > 48:
            return True, f"Цена выше EMA50 {ema50:.1f}, RSI {rsi:.0f} - быки"
        else:
            return False, f"Цена ниже EMA50 {ema50:.1f}, RSI {rsi:.0f} - медведи"
    else:
        # фолбэк как в V3
        return price >= 4341, f"Нет свечей, смотрю зону 4341 (RSI {rsi:.0f})"

def format_msg(price, ema50, rsi, is_buy, reason, has_data):
    signal = "LONG (BUY) 🟢" if is_buy else "SHORT (SELL) 🔴"
    status = "REAL M15" if has_data else "ZONE MODE"
    sl = price - 20 if is_buy else price + 20
    tp1 = price + 25 if is_buy else price - 25
    tp2 = price + 50 if is_buy else price - 50
    return f"""📊 STAR TRADER V4.1 SMART | GOLD

💰 Cena: {price:.2f}$
📈 EMA50: {ema50:.2f}$ [{status}]
📉 RSI: {rsi:.1f}

📈 SIGNAL: {signal}
💡 Analiz: {reason}

🔷 Vhod: {price-2:.1f} - {price+2:.1f}
🔻 SL: {sl:.1f}
🎯 TP1: {tp1:.1f}
🎯 TP2: {tp2:.1f}

⏱️ M15: {'vverh' if is_buy else 'vniz'}
🤖 Umniy analiz

STAR TRADER V4.1 SMART"""

def send_tg(chat_id, text):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=10)
    except: pass

@app.route("/")
def home():
    p,e,r,h = get_market_data()
    return f"V4.1 OK | {p} EMA:{e:.1f} RSI:{r:.1f} has_data:{h}",200

@app.route("/webhook", methods=["POST"])
def webhook():
    data=request.get_json()
    if data and "message" in data:
        chat_id=data["message"]["chat"]["id"]
        if data["message"].get("text","") in ["/gold","/check","/start"]:
            p,e,r,h = get_market_data()
            is_buy, reason = analyze(p,e,r,h)
            send_tg(chat_id, format_msg(p,e,r,is_buy,reason,h))
    return "ok",200

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))
