import os, telebot, yfinance as yf, pandas as pd
from flask import Flask, request
from datetime import datetime
import pytz
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    TOKEN = os.environ.get("BOT_")
if not TOKEN:
    raise ValueError("BOT_TOKEN не найден!")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
AKTAU_TZ = pytz.timezone('Asia/Aqtau')
LOT = 0.03
STOP_DOLLAR = 7
TAKE_DOLLAR = 18

def get_gold_data():
    for ticker in ["XAUUSD=X", "GC=F"]:
        try:
            df = yf.download(ticker, period="10d", interval="15m", progress=False, auto_adjust=True)
            if df is None:
                continue
            if len(df) < 60:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            price = float(df['Close'].iloc[-1])
            if 4000 < price < 5000:
                return df
        except:
            continue
    return None

def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta>0, 0).ewm(alpha=1/period).mean()
    loss = -delta.where(delta<0, 0).ewm(alpha=1/period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calc_adx(df, period=14):
    high = df['High']
    low = df['Low']
    close = df['Close']
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm<0]=0
    minus_dm[minus_dm<0]=0
    tr1 = high-low
    tr2 = (high-close.shift()).abs()
    tr3 = (low-close.shift()).abs()
   
