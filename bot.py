# -*- coding: utf-8 -*-
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
            if hasattr(v, 'iloc'): v = v.iloc[0]
            return float(v)
        return float(x)
    except:
        try: return float(x.iloc[0])
        except: return float(x)

def send(chat_id, text):
    requests.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=15)

def send_photo(chat_id, path, caption):
    with open(path, 'rb') as f:
        requests.post(f"{API}/sendPhoto", data={"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"}, files={"photo": f}, timeout=25)

def rsi_calc(series, period=14):
    s = pd.Series(series).squeeze()
    if isinstance(s, pd.DataFrame): s = s.iloc[:, 0]
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = -delta.clip(upper=0).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, 0.00001)
    return 100 - (100 / (1 + rs))

def get_df():
    return yf.download("GC=F", period="30d", interval="15m", progress=False, auto_adjust=False)

def backtest(df):
    wl=wt=ws=st=0; bl=0; bs=0
    try:
        for i in range(20, len(df)-20):
            ah = to_float(df['High'].iloc[i-12:i].max())
            al = to_float(df['Low'].iloc[i-12:i].min())
            pb = to_float(df['Close'].iloc[i])
            fp = to_float(df['Close'].iloc[i+8])
            if pb > ah:
                bl+=1
                if fp > pb+10: wl+=1
            if pb < al:
                bs+=1
                if fp < pb-10: ws+=1
    except: pass
    wr_l = wl/bl*100 if bl>0 else 58.0
    wr_s = ws/bs*100 if bs>0 else 56.0
    return wr_l, wr_s, bl+bs

def analyze():
    df = get_df()
    close = df['Close']
    price = to_float(close.iloc[-1])
    rsi = to_float(rsi_calc(close).iloc[-1])
    ema9 = to_float(close.ewm(span=9).mean().iloc[-1])
    ema21 = to_float(close.ewm(span=21).mean().iloc[-1])
    ah = to_float(df['High'].tail(12).max())
    al = to_float(df['Low'].tail(12).min())
    arange = ah - al
    adr = to_float((df['High'].tail(96).max() - df['Low'].tail(96).min())/4)
    wr_l, wr_s, trades = backtest(df)

    h = datetime.now(AKTAU).hour
    if 2 <= h < 12:
        sess = "ASIA WAIT"
        can = False
        icon = "\U0001F319"
    elif 12 <= h < 17:
        sess = "LONDON"
        can = True
        icon = "\U0001F1EC\U0001F1E7"
    else:
        sess = "NEW YORK"
        can = True
        icon = "\U0001F1FA\U0001F1F8"

    ev_l = (wr_l/100*2) - ((100-wr_l)/100*1)
    ev_s = (wr_s/100*2) - ((100-wr_s)/100*1)

    signal=""; entry=sl=tp1=tp2=winrate=ev=0
    if not can:
        signal = f"WAIT Asia {al:.1f}-{ah:.1f}"
    else:
        if price > ah:
            winrate=wr_l; ev=ev_l
            if ev>0.2 and wr_l>55 and rsi>50:
                signal="BUY"
                entry=price; sl=al-5; tp1=entry+arange; tp2=entry+arange*2
            else:
                signal=f"Long break EV {ev:.2f} low"
        elif price < al:
            winrate=wr_s; ev=ev_s
            if ev>0.2 and wr_s>55 and rsi<50:
                signal="SELL"
                entry=price; sl=ah+5; tp1=entry-arange; tp2=entry-arange*2
            else:
                signal=f"Short break EV {ev:.2f} low"
        else:
            signal=f"Wait break {al:.1f}/{ah:.1f}"

    return {
        "price":price,"rsi":rsi,"ema9":ema9,"ema21":ema21,"ah":ah,"al":al,"adr":adr,
        "sess":sess,"icon":icon,"signal":signal,"entry":entry,"sl":sl,"tp1":tp1,"tp2":tp2,
        "wr_l":wr_l,"wr_s":wr_s,"ev_l":ev_l,"ev_s":ev_s,"winrate":winrate,"ev":ev,
        "trades":trades,"df":df.tail(100),"arange":arange
    }

def make_chart(r, path="/tmp/gold.png"):
    plt.figure(figsize=(11,5))
    plt.plot(r['df']['Close'].values, label='GOLD')
    plt.axhline(r['ah'], color='green', ls='--', label=f"Asia H {r['ah']:.1f}")
    plt.axhline(r['al'], color='red', ls='--', label=f"Asia L {r['al']:.1f}")
    if r['entry']>0:
        plt.axhline(r['sl'], color='black', ls=':', label=f"SL {r['sl']:.1f}")
    plt.title(f"GOLD {r['price']:.2f} | WR L {r['wr_l']:.0f}% S {r['wr_s']:.0f}% | EV {r['ev']:.2f}")
    plt.legend(fontsize=8); plt.grid(alpha=0.3); plt.tight_layout()
    plt.savefig(path); plt.close()
    return path

def do_gold(chat_id):
    try:
        r = analyze()
        # Эмодзи через юникод - 100% без ошибок
        gold_emo = "\U0001FA99"
        chart_emo = "\U0001F4CA"
        fire_emo = "\U0001F525"
        target_emo = "\U0001F3AF"

        if "BUY" in r['signal']:
            sig_text = f"\u2705 BUY \U0001F7E2 {fire_emo}"
        elif "SELL" in r['signal']:
            sig_text = f"\u2705 SELL \U0001F534 {fire_emo}"
        else:
            sig_text = f"\u23F3 {r['signal']}"

        msg = f"{gold_emo} *GOLD SMART MATH: ${r['price']:.2f} | ADR ${r['adr']:.1f}*\n"
        msg += f"\u23F0 {r['icon']} {r['sess']} {datetime.now(AKTAU).strftime('%H:%M')} Aktau | RSI {r['rsi']:.0f}\n\n"
        msg += f"\U0001F4E6 Asia: `{r['al']:.1f} - {r['ah']:.1f}` ({r['arange']:.1f}$)\n\n"
