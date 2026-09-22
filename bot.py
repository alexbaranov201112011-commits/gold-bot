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
    # Проверяем сильные новости по доллару сегодня
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        data = requests.get(url, timeout=8).json()
        now = datetime.now(timezone.utc)
        warnings = []
        for ev in data:
            if ev.get("country")!= "USD":
                continue
            if int(ev.get("impact",0)) < 3: # только красные новости
                continue
            # время новости
            try:
                dt = datetime.fromisoformat(ev["date"].replace("Z","+00:00"))
            except:
                continue
            diff_min = (dt - now).total_seconds()/60
            # если новость была 30 мин назад или будет через 60 мин
            if -30 < diff_min < 60:
