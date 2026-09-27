import os, telebot, yfinance as yf, pandas as pd
from flask import Flask, request
from datetime import datetime
import pytz
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("BOT_")
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
AKTAU_TZ = pytz.timezone('Asia/Aqtau')
LOT = 0.03
STOP_DOLLAR = 7
TAKE_DOLLAR = 18

def get_gold_data():
    # ПРОБУЕМ СПОТ ЦЕНУ КАК У БРОКЕРА
    tickers = ["XAUUSD=X", "XAU/USD", "GC=F"]  # сначала спот, потом фьючерс как запасной
    for ticker in tickers:
        try:
            df = yf.download(ticker, period="10d", interval="15m", progress=False, auto_adjust=True)
            if df is None or len(df) < 60:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            # Проверка что цена похожа на брокера (4200-4400)
            last_price = float(df['Close'].iloc[-1])
            if 4000 < last_price < 4500:
                print(f"Используем тикер {ticker}: {last_price}")
                return df
        except Exception as e:
            print(f"Ошибка тикера {ticker}: {e}")
            continue
    return None

def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta>0, 0).ewm(alpha=1/period).mean()
    loss = -delta.where(delta<0, 0).ewm(alpha=1/period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calc_adx(df, period=14):
    high, low, close = df['High'], df['Low'], df['Close']
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm<0]=0
    minus_dm[minus_dm<0]=0
    tr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period).mean() / atr)
    dx = 100 * abs(plus_di-minus_di) / (plus_di+minus_di)
    return dx.ewm(alpha=1/period).mean()

def make_chart(df, price, ema21, ema50, is_buy):
    plt.figure(figsize=(10,5))
    last60 = df.tail(80)
    plt.plot(last60['Close'], label='XAU Price', color='#C9A86A', linewidth=2.2)
    plt.plot(last60['EMA21'], label='EMA21', color='#1E90FF', linestyle='--', linewidth=1.5)
    plt.plot(last60['EMA50'], label='EMA50', color='#FF4500', linestyle='--', linewidth=1.5)
    entry = price
    sl = price - STOP_DOLLAR if is_buy else price + STOP_DOLLAR
    tp = price + TAKE_DOLLAR if is_buy else price - TAKE_DOLLAR
    plt.axhline(entry, color='black', linestyle=':', linewidth=1, label=f'Вход {entry:.1f}')
    plt.axhline(sl, color='red', linestyle='--', linewidth=1, label=f'SL {sl:.1f}')
    plt.axhline(tp, color='green', linestyle='--', linewidth=1, label=f'TP {tp:.1f}')
    plt.legend(fontsize=8)
   
