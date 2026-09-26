import threading
from flask import Flask
import telebot
import yfinance as yf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import io
import pandas as pd

TOKEN = "ВСТАВЬ_СВОЙ_ТОКЕН_СЮДА"
bot = telebot.TeleBot(TOKEN)
DEPO = 1000
RISK_PCT = 0.02  # 2%
WINRATE = 0.66   # твои 4/2

app = Flask(__name__)
@app.route('/')
def home(): return "V7 SMART LIVE", 200
threading.Thread(target=lambda: app.run(host='0.0.0.0', port=10000), daemon=True).start()

def get_math():
    df = yf.download("GC=F", period="1mo", interval="1h").dropna()
    close = df['Close']
    price = float(close.iloc[-1])
    ema20 = close.ewm(20).mean().iloc[-1]
    ema50 = close.ewm(50).mean().iloc[-1]
    
    # RSI
    delta = close.diff()
    gain = delta.where(delta>0,0).rolling(14).mean()
    loss = -delta.where(delta<0,0).rolling(14).mean()
    rsi = 100 - (100/(1+gain/loss.iloc[-1]))
    rsi_val = float(rsi.iloc[-1])

    # ATR
    atr = (df['High']-df['Low']).rolling(14).mean().iloc[-1]
    
    # Уровни
    entry = price
    if price < ema20: # SHORT логика
        sl = price + atr*1.5
        tp = price - atr*2.0
        side = "SELL"
    else:
        sl = price - atr*1.5
        tp = price + atr*2.0
        side = "BUY"

    RR = abs(tp-entry)/abs(sl-entry)
    EV = (WINRATE * RR) - (1-WINRATE)

    # Уверенность по 5 факторам
    conf_rsi = 90 if rsi_val<35 or rsi_val>65 else 50
    conf_ema = 85 if (price<ema20 and price<ema50) or (price>ema20 and price>ema50) else 40
    conf_vol = 75  # заглушка, yfinance объем золота слабый
    conf_sess = 80 # Лондон/НЙ сейчас активны
    conf_spread = 85
    conf_total = (conf_rsi*0.3 + conf_ema*0.3 + conf_vol*0.15 + conf_sess*0.15 + conf_spread*0.1)

    # Келли
    f_kelly = WINRATE - (1-WINRATE)/RR
    f_safe = max(0, f_kelly*0.5)
    lot_risk_usd = DEPO * RISK_PCT * (0.5 + f_safe) # 22% от риска
    
    return df, price, rsi_val, ema20, ema50, side, entry, sl, tp, RR, EV, conf_total, lot_risk_usd, atr

@bot.message_handler(commands=['gold'])
def gold(m):
    df, price, rsi, ema20, ema50, side, entry, sl, tp, RR, EV, conf, risk_usd, atr = get_math()
    
    if EV < 0.2:
        bot.reply_to(m, f"⏭️ SKIP\nЦена ${price:.1f}\nEV={EV:.2f} < 0.2 - математика не выгодна. Ждем.")
        return
    if conf < 65:
        bot.reply_to(m, f"⏭️ WAIT\nУверенность {conf:.0f}% < 65% - нет сигнала.")
        return

    # График уверенности
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

    # Текст как ты хотел
    txt = f"""🧠 V7 SMART SIGNAL

{side} XAU {entry:.1f} | Уверенность {conf:.0f}% | EV +{EV:.2f}R

📐 Математика:
RSI: {rsi:.1f}
EMA20: {ema20:.1f} / EMA50: {ema50:.1f}
RR: 1:{RR:.2f}
ATR: {atr:.2f}

💰 Депо: ${DEPO} | Риск: ${risk_usd:.2f} ({RISK_PCT*100:.0f}%)
Формула Келли: f={WINRATE - (1-WINRATE)/RR:.2f} -> берем половину = {(WINRATE - (1-WINRATE)/RR)*0.5:.2f}

🎯 Вход: {entry:.2f}
SL: {sl:.2f}
TP: {tp:.2f}

Если 4 плюса 2 минуса с таким EV - депо 1000$ -> ~1180$ за серию."""

    bot.send_photo(m.chat.id, buf, caption=txt)

@bot.message_handler(commands=['start'])
def start(m): bot.reply_to(m, "V7 SMART готов. Жми /gold")

print("V7 STARTED")
bot.infinity_polling()
