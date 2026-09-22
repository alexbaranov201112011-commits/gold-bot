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
                closes = [float(c[4]) for c in data]
                return closes
        except:
            continue
    return None

def get_market_data():
    price = get_price()
    closes = get_klines()
    if closes:
        def ema(arr, p):
            k = 2/(p+1)
            e = arr[0]
            for v in arr[1:]:
                e = v*k + e*(1-k)
            return e
        ema50 = ema(closes[-70:], 50)
        gains=0; losses=0
        for i in range(1,15):
            diff = closes[-i] - closes[-i-1]
            if diff>0: gains+=diff
            else: losses+=abs(diff)
        avg_g = gains/14 if gains else 0.5
        avg_l = losses/14 if losses else 0.5
        rsi = 100 - (100/(1+avg_g/avg_l)) if avg_l!=0 else 50
        return price, ema50, rsi, True
    else:
        return price, price-5, 50, False

def analyze(price, ema50, rsi, has_data):
    distance = abs(price - ema50)

    # ФИЛЬТР: не давать сигнал когда не нужно
    if distance < 3.0:
        return None, f"Боковик! Цена {price:.1f} у EMA50 {ema50:.1f} (дист. {distance:.1f}$). ЖДИ."

    if 48 < rsi < 52 and distance < 7:
        return None, f"Нет импульса. RSI {rsi:.0f} нейтральный. ЖДИ."

    if price > ema50 and rsi >= 52:
        return True, f"Цена выше EMA50 {ema50:.1f}, RSI {rsi:.0f} - быки"
    elif price < ema50 and rsi <= 53:
        return False, f"Цена ниже EMA50 {ema50:.1f}, RSI {rsi:.0f} - медведи"
    else:
        return None, f"Нечистый рынок. RSI {rsi:.0f}, дист. {distance:.1f}$. ЖДИ."

def format_msg(price, ema50, rsi, is_buy, reason, has_data):
    status = "REAL M15" if has_data else "ZONE MODE"

    if is_buy is None:
        return f"""📊 STAR TRADER V4.
