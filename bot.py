from flask import Flask, request
import requests
import os

app = Flask(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN")

def get_gold_price():
    # Пробуем 3 бесплатных источника
    try:
        # Источник 1: gold-api.com
        r = requests.get("https://api.gold-api.com/price/XAU", timeout=5).json()
        if "price" in r:
            return float(r["price"])
    except:
        pass
    try:
        # Источник 2: metals.live
        r = requests.get("https://api.metals.live/v1/spot/gold", timeout=5).json()
        return float(r[0])
    except:
        pass
    try:
        # Источник 3: через Binance PAXG ~ золото
        r = requests.get("https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT", timeout=5).json()
        return float(r["price"])
    except:
        return 4330.10 # fallback если инет упал

def format_msg(price, is_buy):
    if is_buy:
        signal = "LONG (BUY)"
        trend = "vverh"
        sl = price - 10
