import threading
from flask import Flask
import telebot
import yfinance as yf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import io
import time

TOKEN = "ВСТАВЬ_СЮДА_СВОЙ_ТОКЕН_ОТ_BOTFATHER"
bot = telebot.TeleBot(TOKEN)

# --- FIX 409 CONFLICT ---
bot.delete_webhook(drop_pending_updates=True)
time.sleep(2)
# ------------------------

DEPO = 1000
RISK_PCT = 0.02
WINRATE = 0.66

app = Flask(__name__)
@app.route('/')
def home(): return "V7 SMART LIVE FIXED", 200

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
    rs = gain / loss
    rsi = 100 - (100/(1+rs))
    rsi_val = float(rsi.iloc[-1])

    atr = float((df['High']-df['Low']).rolling(14).mean().iloc[-1])
    
    entry = price
    if price < ema20:
        sl = price + atr*1.5
        tp = price - atr*2.0
        side = "SELL"
    else:
        sl = price - atr*1.5
        tp = price + atr*2.0
        side = "BUY"

    RR = abs(tp-entry)/abs(sl-entry) if abs(sl-entry)>0 else 1.5
    EV = (WINRATE * RR) - (1-WINRATE)

    conf_rsi = 85 if rsi_val<38 or rsi_val>62 else 50
    conf_ema = 85 if (price<ema20 and price<ema50) or (price>ema20 and price>ema50) else 40
    conf_vol = 70
    conf_sess = 40
    conf_spread = 85
    conf_total = (conf_rsi*0.3 + conf_ema*0.3 + conf_vol*0.15 + conf_sess*0.15 + conf_spread*0.1)

    f_kelly = WINRATE - (1-WINRATE)/RR
    f_safe = max(0, f_kelly*0.5)
    risk_usd = DEPO * RISK_PCT * (0.5 + f_safe)
    
    return df, price, rsi_val, ema20, ema50, side, entry, sl, tp, RR, EV, conf_total, risk_usd, atr

@bot.message_handler(commands=['gold'])
def gold(m):
    try:
        df, price, rsi, ema20, ema50, side, entry, sl, tp, RR, EV, conf, risk_usd, atr = get_math()
        
        if EV < 0.2:
            bot.reply_to(m, f"⏭️ SKIP\nPrice ${price:.1f}\nEV={EV:.2f} < 0.2 - not profitable")
            return
        if conf < 65:
            bot.reply_to(m, f"⏭️ WAIT\nConfidence {conf:.0f}% < 65%")
            return

        plt.figure(figsize=(6,3))
        plt.plot(df['Close'].tail(100).values, label='Gold')
        plt.axhline(entry, color='yellow', linestyle='--', label=f'Entry {entry:.1f}')
        plt.axhline(sl, color='red', linestyle='--', label=f'SL {sl:.1f}')
        plt.axhline(tp, color='green', linestyle='--', label=f'TP {tp:.1f}')
        plt.title(f"{side} | Conf {conf:.0f}% | EV {EV:.2f}R")
        plt.legend(fontsize=6)
        plt.grid(alpha=0.3)
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=150)
        buf.seek(0)
        plt.close()

        txt = f"""🧠 V7 SMART SIGNAL

{side} XAU {entry:.1f} | Confidence {conf:.0f}% | EV +{EV:.2f}R

Math:
RSI: {rsi:.1f}
EMA20: {ema20:.1f} / EMA50: {ema50:.1f}
RR: 1:{RR:.2f}
ATR: {atr:.2f}

Depo: ${DEPO} | Risk: ${risk_usd:.2f} ({RISK_PCT*100:.0f}%)
Kelly: f={WINRATE - (1-WINRATE)/RR:.2f} -> safe 0.5 = {(WINRATE - (1-WINRATE)/RR)*0.5:.2f}

Entry: {entry:.2f}
SL: {sl:.2f}
TP: {tp:.2f}"""

        bot.send_photo(m.chat.id, buf, caption=txt)
    except Exception as e:
        bot.reply_to(m, f"Error: {e}")

@bot.message_handler(commands=['start'])
def start(m): 
    bot.reply_to(m, "V7 SMART FIXED ready. Type /gold")

print("V7 STARTED - polling...")
bot.infinity_polling(skip_pending=True, allowed_updates=[])
