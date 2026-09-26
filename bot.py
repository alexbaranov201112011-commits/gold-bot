import threading
from flask import Flask
import telebot
import yfinance as yf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import io
import time

TOKEN = "8964936412:AAGfpB9OIHTkayN2a86aI-JkixZLiMC4S6s"
bot = telebot.TeleBot(TOKEN)
bot.delete_webhook(drop_pending_updates=True)
time.sleep(2)

DEPO = 1000
RISK_PCT = 0.02
WINRATE = 0.66

app = Flask(__name__)
@app.route('/')
def home(): 
    return "V7 SMART LIVE FIXED", 200

def run_flask():
    app.run(host='0.0.0.0', port=10000)

threading.Thread(target=run_flask, daemon=True).start()

def get_math():
    df = yf.download("GC=F", period="1mo", interval="1h", auto_adjust=True).dropna()
    if len(df) < 60:
        df = yf.download("GC=F", period="3mo", interval="1h", auto_adjust=True).dropna()
    close = df['Close']
    price = float(close.iloc[-1])
    ema20 = float(close.ewm(20).mean().iloc[-1])
    ema50 = float(close.ewm(50).mean().iloc[-1])
    delta = close.diff()
    gain = delta.where(delta>0,0).rolling(14).mean()
    loss = -delta.where(delta<0,0).rolling(14).mean()
    rsi = 100 - (100/(1+gain/loss))
    rsi_val = float(rsi.iloc[-1])
    atr = float((df['High']-df['Low']).rolling(14).mean().iloc[-1])
    entry = price
    if price < ema20:
        sl = price + atr*1.5
        tp = price - atr*2.
