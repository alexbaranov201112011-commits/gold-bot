from flask import Flask, request
import requests
import os
from datetime import datetime, timezone

app = Flask(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN")

def get_price():
    urls = [
        "https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT",
        "https://data-api.binance.vision/api/v3/ticker/price?symbol=PAXGUSDT",
        "https://api.gold-api.com/price/XAU"
    ]
    for url in urls:
        try:
            r = requests.get(url, timeout=5).json()
            if "price" in r:
                return float(r["price"])
        except:
            continue
    return 4324.0

def get_klines():
    urls = [
        "https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=15m&limit=100",
        "https://data-api.binance.vision/api/v3/klines?symbol=PAXGUSDT&interval=15m&limit=100"
    ]
    for url in urls:
        try:
            d = requests.get(url, timeout=8, headers={"User-Agent":"Mozilla"}).json()
            if isinstance(d, list) and len(d) > 50:
                return [float(c[4]) for c in d]
        except:
            continue
    return None

def get_news():
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        data = requests.get(url, timeout=8).json()
        now = datetime.now(timezone.utc)
        warn = []
        for ev in data:
            if ev.get("country")!= "USD":
                continue
            if int(ev.get("impact", 0)) < 3:
                continue
            try:
                dt = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
            except:
                continue
            diff = (dt - now).total_seconds() / 60
            if -30 < diff < 60:
                warn.append(ev.get("title","News") + " at " + dt.strftime("%H:%M"))
        if warn:
            return " | ".join(warn[:2])
        return None
    except:
        return None

def get_market_data():
    price = get_price()
    closes = get_klines()
    news = get_news()
    if closes:
        def ema(arr, p):
            k = 2/(p+1)
            e = arr[0]
            for v in arr[1:]:
                e = v*k + e*(1-k)
            return e
        ema50 = ema(closes[-70:], 50)
        g = 0
        l = 0
        for i in range(1,15):
            d = closes[-i] - closes[-i-1]
            if d > 0:
                g += d
            else:
                l += abs(d)
        ag = g/14 if g else 0.5
        al = l/14 if l else 0.5
        rsi = 100 - (100/(1+ag/al)) if al!= 0 else 50
        return price, ema50, rsi, True, news
    else:
        return price, price-5, 50, False, news

def analyze(price, ema50, rsi, has_data, news):
    if news:
        return None, "NOVOSTI! " + news
    dist = abs(price - ema50)
    if dist < 3.0:
        return None, "Bokovik! Cena %.1f u EMA50" % price
    if 48 < rsi < 52 and dist < 7:
        return None, "Net impulsa RSI %.0f" % rsi
    if price > ema50 and rsi >= 52:
        return True, "Cena vyshe EMA50 %.1f RSI %.0f byki" % (ema50, rsi)
    if price < ema50 and rsi <= 53:
        return False, "Cena nizhe EMA50 %.1f RSI %.0f medvedi" % (ema50, rsi)
    return None, "Nechistiy rynok RSI %.0f" % rsi

def format_msg(price, ema50, rsi, is_buy, reason, has_data, news):
    status = "REAL M15" if has_data else "ZONE"
    nline = news if news else "Net silnyh novostey"
    if is_buy is None:
        return "STAR V5.2 GOLD\n\nCena: %.2f$\nEMA: %.2f$ [%s]\nRSI: %.1f\nNEWS: %s\n\nSIGNAL: NET VHODA\n%s\n" % (price, ema50, status, rsi, nline, reason)
    sig = "LONG BUY" if is_buy else "SHORT SELL"
    sl = price - 20 if is_buy else price + 20
    tp1 = price + 25 if is_buy else price - 25
    tp2 = price + 50 if is_buy else price - 50
    return "STAR V5.2 GOLD\n\nCena: %.2f$\nEMA: %.2f$ [%s]\nRSI: %.1f\nNEWS: %s\n\nSIGNAL: %s\n%s\nVhod: %.1f-%.1f\nSL: %.1f\nTP1: %.1f\nTP2: %.1f\n" % (price, ema50, status, rsi, nline, sig, reason, price-2, price+2, sl, tp1, tp2)

def send_tg(chat_id, text):
    try:
        requests.post("https://api.telegram.org/bot%s/sendMessage" % BOT_TOKEN, json={"chat_id": chat_id, "text": text}, timeout=10)
    except:
        pass

@app.route("/")
def home():
    p,e,r,h,n = get_market_data()
    return "V5.2 OK | %s" % p, 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if data and "message" in data:
        cid = data["message"]["chat"]["id"]
        txt = data["message"].get("text","")
        if txt in ["/gold","/check","/start"]:
            p,e,r,h,n = get_market_data()
            ib, rs = analyze(p,e,r,h,n)
            send_tg(cid, format_msg(p,e,r,ib,rs,h,n))
    return "ok",200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))
