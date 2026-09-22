from flask import Flask, request
import requests
import os
from datetime import datetime, timezone

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
            data = requests.get(url, timeout=8, headers={"User-Agent":"Mozilla"}).json()
            if isinstance(data, list) and len(data) > 50:
                return [float(c[4]) for c in data]
        except:
            continue
    return None

def get_news():
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        data = requests.get(url, timeout=8).json()
        now = datetime.now(timezone.utc)
        warnings = []
        for ev in data:
            if ev.get("country")!= "USD":
                continue
            if int(ev.get("impact", 0)) < 3:
                continue
            try:
                dt = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
            except:
                continue
            diff_min = (dt - now).total_seconds() / 60
            if -30 < diff_min < 60:
                warnings.append(f"{ev['title']} v {dt.strftime('%H:%M')} UTC")
        if warnings:
            return " | ".join(warnings[:2])
        return None
    except Exception as e:
        print(f"news error {e}")
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
        gains = 0
        losses = 0
        for i in range(1,15):
            diff = closes[-i] - closes[-i-1]
            if diff > 0:
                gains += diff
            else:
                losses += abs(diff)
        avg_g = gains/14 if gains else 0.5
        avg_l = losses/14 if losses else 0.5
        rsi = 100 - (100/(1+avg_g/avg_l)) if avg_l!= 0 else 50
        return price, ema50, rsi, True, news
    else:
        return price, price-5, 50, False, news

def analyze(price, ema50, rsi, has_data, news):
    if news:
        return None, f"NOVOSTI! {news} - ne torguem 60 min do i 30 posle."
    distance = abs(price - ema50)
    if distance < 3.0:
        return None, f"Bokovik! Cena {price:.1f} u EMA50 {ema50:.1f}. ZHDI."
    if 48 < rsi < 52 and distance < 7:
        return None, f"Net impulsa. RSI {rsi:.0f}. ZHDI."
    if price > ema50 and rsi >= 52:
        return True, f"Cena vyshe EMA50 {ema50:.1f}, RSI {rsi:.0f} - byki"
    elif price < ema50 and rsi <= 53:
        return False, f"Cena nizhe EMA50 {ema50:.1f}, RSI {rsi:.0f} - medvedi"
    else:
        return None, f"Nechistiy rynok. RSI {rsi:.0f}. ZHDI."

def format_msg(price, ema50, rsi, is_buy, reason, has_data, news):
    status = "REAL M15" if has_data else "ZONE MODE"
    news_line = f"\nNEWS: {news}\n" if news else "\nNEWS: Net silnyh novostey\n"
    if
