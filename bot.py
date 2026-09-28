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
            v = x.iloc[-1] if hasattr(x, '__len__') and len(x)>0 else x
            if hasattr(v, 'iloc'): v = v.iloc[0]
            return float(v)
        return float(x)
    except:
        return float(x.iloc[0]) if hasattr(x, 'iloc') else float(x)

def send(chat_id, text):
    requests.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=15)

def send_photo(chat_id, path, caption):
    with open(path, 'rb') as f:
        requests.post(f"{API}/sendPhoto", data={"chat_id": chat_id, "caption": caption, "parse_mode": "Markdown"}, files={"photo": f}, timeout=20)

def rsi_calc(series, period=14):
    delta = series.diff()
    gain = delta.where(delta>0,0).rolling(period).mean()
    loss = -delta.where(delta<0,0).rolling(period).mean()
    rs = gain/loss
    return 100 - (100/(1+rs))

def get_data():
    df = yf.download("GC=F", period="5d", interval="15m", progress=False, auto_adjust=False)
    return df

def analyze():
    df = get_data()
    close = df['Close']
    # последние цены
    price = to_float(close.iloc[-1])

    # RSI
    rsi_series = rsi_calc(close)
    rsi = to_float(rsi_series.iloc[-1])

    # EMA
    ema9 = close.ewm(span=9).mean().iloc[-1]
    ema9 = to_float(ema9)
    ema21 = close.ewm(span=21).mean().iloc[-1]
    ema21 = to_float(ema21)

    # Азия 02:00-12:00 Актау -> последние 40 свечей 15м = 10 часов
    asia_high = to_float(df['High'].tail(40).max())
    asia_low = to_float(df['Low'].tail(40).min())

    # ADR
    daily_range = (df['High'].tail(96).max() - df['Low'].tail(96).min())
    adr = to_float(daily_range) / 3

    # Сессия
    now_aktau = datetime.now(AKTAU)
    h = now_aktau.hour

    if 2 <= h < 12:
        session = "АЗИЯ 🌙 WAIT"
        signal_type = "WAIT"
    elif 12 <= h < 17:
        session = "ЛОНДОН 🇬🇧"
        signal_type = "ACTIVE"
    else:
        session = "НЬЮ-ЙОРК 🇺🇸"
        signal_type = "ACTIVE"

    trend = "ЛОНГ" if ema9 > ema21 else "ШОРТ"

    # Сигнал
    entry = sl = tp1 = tp2 = 0
    side = "WAIT"

    if signal_type == "WAIT":
        side = f"WAIT - Флэт Азии {asia_low:.1f}-{asia_high:.1f}"
    else:
        if price > asia_high and rsi > 55 and ema9 > ema21:
            side = "✅ BUY 🟢"
            entry = price
            sl = asia_low - 5
            tp1 = entry + adr*1.5
            tp2 = entry + adr*3
        elif price < asia_low and rsi < 45 and ema9 < ema21:
            side = "✅ SELL 🔴"
            entry = price
            sl = asia_high + 5
            tp1 = entry - adr*1.5
            tp2 = entry - adr*3
        else:
            side = f"ЖДЕМ пробой Азии {asia_low:.1f}/{asia_high:.1f} | RSI {rsi:.0f}"

    return {
        "price": price, "rsi": rsi, "ema9": ema9, "ema21": ema21,
        "asia_high": asia_high, "asia_low": asia_low, "adr": adr,
        "session": session, "trend": trend, "side": side,
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "df": df.tail(100)
    }

def make_chart(d, res, path="/tmp/gold.png"):
    df = d
    plt.figure(figsize=(10,5))
    plt.plot(df['Close'].values, label='GOLD')
    plt.axhline(res['asia_high'], color='green', linestyle='--', label=f"Asia H {res['asia_high']:.1f}")
    plt.axhline(res['asia_low'], color='red', linestyle='--', label=f"Asia L {res['asia_low']:.1f}")
    if res['entry']>0:
        plt.axhline(res['sl'], color='black', linestyle=':', label=f"SL {res['sl']:.1f}")
    plt.title(f"GOLD {res['price']:.2f} | {res['session']} | RSI {res['rsi']:.0f}")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig(path)
    plt.close()
    return path

def do_gold(chat_id):
    try:
        res = analyze()
        msg = f"""🟡 *GOLD: ${res['price']:.2f} | ADR: ${res['adr']:.1f}*
⏰ {res['session']} {datetime.now(AKTAU).strftime('%H:%M')} Актау
📈 Тренд: {res['trend']} | RSI: {res['rsi']:.0f} | EMA9 {res['ema9']:.1f} / EMA21 {res['ema21']:.1f}

🔹 Азия: `{res['asia_low']:.1f} - {res['asia_high']:.1f}`
🔹 Сигнал: *{res['side']}*

"""
        if "BUY" in res['side'] or "SELL" in res['side']:
            msg += f"""🔹 Вход: `${res['entry']:.2f}`
🎯 TP1: `${res['tp1']:.2f}`
🎯 TP2: `${res['tp2']:.2f}`
🛑 Стоп: `${res['sl']:.2f}`

Бот работает 24/7 ✅
"""
            chart = make_chart(res['df'], res)
            send_photo(chat_id, chart, msg)
        else:
            send(chat_id, msg)

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        send(chat_id, f"Ошибка: {e}")

@bot.message_handler(func=lambda m: True)
def handle(m):
    t = (m.text or "").lower()
    if "gold" in t or "start" in t:
        send(m.chat.id, "⏳ Считаю золото... (сильный анализ)")
        threading.Thread(target=do_gold, args=(m.chat.id,), daemon=True).start()

@app.route('/')
def home(): return "V-MAX LIVE"

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    try:
        upd = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([upd])
    except: pass
    return '',200

@app.route('/setwebhook')
def sethook():
    bot.remove_webhook()
    r = bot.set_webhook(url=f'https://gold-bot-q8la.onrender.com/{TOKEN}')
    return f"SET {r}"
