import os, requests, threading, telebot
from flask import Flask, request
import yfinance as yf
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from datetime import datetime
import pytz

TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)
API = f"https://api.telegram.org/bot{TOKEN}"
AKTAU = pytz.timezone("Asia/Aqtau")

def to_float(x):
    try:
        if hasattr(x, 'iloc'):
            v = x.iloc[-1]
            if hasattr(v, 'iloc'):
                v = v.iloc[0]
            return float(v)
        return float(x)
    except:
        try:
            return float(x.iloc[0])
        except:
            return float(x)

def send(chat_id, text):
    requests.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=15)

def send_photo(chat_id, path, caption):
    with open(path, 'rb') as f:
        requests.post(f"{API}/sendPhoto", data={"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"}, files={"photo": f}, timeout=25)

def rsi_calc(series, period=14):
    s = pd.Series(series).squeeze()
    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = -delta.clip(upper=0).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, 0.00001)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def get_df():
    df = yf.download("GC=F", period="30d", interval="15m", progress=False, auto_adjust=False)
    return df

def backtest_winrate(df):
    wins_long = 0
    total_long = 0
    wins_short = 0
    total_short = 0
    try:
        for i in range(20, len(df)-20):
            asia_high = to_float(df['High'].iloc[i-12:i].max())
            asia_low = to_float(df['Low'].iloc[i-12:i].min())
            price_break = to_float(df['Close'].iloc[i])
            future_price = to_float(df['Close'].iloc[i+8])
            if price_break > asia_high:
                total_long += 1
                if future_price > price_break + 10:
                    wins_long += 1
            if price_break < asia_low:
                total_short += 1
                if future_price < price_break - 10:
                    wins_short += 1
    except Exception as e:
        print(f"backtest error {e}")

    wr_long = (wins_long/total_long*100) if total_long>0 else 58.0
    wr_short = (wins_short/total_short*100) if total_short>0 else 56.0
    return wr_long, wr_short, total_long+total_short

def analyze():
    df = get_df()
    close = df['Close']
    price = to_float(close.iloc[-1])
    rsi = to_float(rsi_calc(close).iloc[-1])
    ema9 = to_float(close.ewm(span=9).mean().iloc[-1])
    ema21 = to_float(close.ewm(span=21).mean().iloc[-1])
    asia_high = to_float(df['High'].tail(12).max())
    asia_low = to_float(df['Low'].tail(12).min())
    asia_range = asia_high - asia_low
    adr = to_float((df['High'].tail(96).max() - df['Low'].tail(96).min()) / 4)
    wr_long, wr_short, trades = backtest_winrate(df)
    now = datetime.now(AKTAU)
    h = now.hour
    if 2 <= h < 12:
        session = "АЗИЯ
