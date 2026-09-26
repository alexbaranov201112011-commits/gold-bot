import os
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
time.sleep(1)

DEPO = 1000
RISK_PCT = 0.02
WINRATE = 0.66

app = Flask(__name__)
@app.route('/')
def home(): return "V7 SMART LIVE FIXED - READY", 200

def get_math():
    df = yf.download("GC=F", period="1mo", interval="1h", auto_adjust=True).dropna()
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
        sl = price + atr*1.5; tp = price - atr*2.0; side = "SELL"
    else:
        sl = price - atr*1.5; tp = price + atr*2.0; side = "BUY"
    RR = abs(tp-entry)/abs(sl-entry) if sl!=entry else 1.5
    EV = (WINRATE * RR) - (1-WINRATE)
    conf = 85 if rsi_val<38 or rsi_val>62 else 50
    f_kelly = WINRATE - (1-WINRATE)/RR
    risk_usd = DEPO * RISK_PCT * (0.5 + max(0,f_kelly*0.5))
    return df, price, rsi_val, ema20, ema50, side, entry, sl, tp, RR, EV, conf, risk_usd, atr

@bot.message_handler(commands=['gold'])
def gold(m):
    try:
        df, price, rsi, ema20, ema50, side, entry, sl, tp, RR, EV, conf, risk_usd, atr = get_math()
        plt.figure(figsize=(6,3))
        plt.plot(df['Close'].tail(100).values)
        plt.axhline(entry, color='yellow', linestyle='--'); plt.axhline(sl, color='red', linestyle='--'); plt.axhline(tp, color='green', linestyle='--')
        plt.title(f"{side} Conf {conf:.0f}% EV {EV:.2f}R"); plt.grid(alpha=0.3)
        buf = io.BytesIO(); plt.savefig(buf, format='png', dpi=150); buf.seek(0); plt.close()
        txt = f"SMART {side} XAU {entry:.1f} | Conf {conf:.0f}% | EV +{EV:.2f}R\nRSI {rsi:.1f} RR 1:{RR:.2f} Depo ${DEPO} Risk ${risk_usd:.2f}\nSL {sl:.2f} TP {tp:.2f}"
        bot.send_photo(m.chat.id, buf, caption=txt)
    except Exception as e:
        bot.reply_to(m, f"Error: {e}")

@bot.message_handler(commands=['start'])
def start(m): bot.reply_to(m, "V7 ready /gold")

def run_bot():
    print("BOT POLLING STARTED")
    bot.infinity_polling(skip_pending=True)

threading.Thread(target=run_bot, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"FLASK START ON PORT {port}")
    app.run(host='0.0.0.0', port=port)
