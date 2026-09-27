import os, telebot, yfinance as yf, pandas as pd
from flask import Flask, request
from datetime import datetime
import pytz
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN не найден!")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
AKTAU_TZ = pytz.timezone('Asia/Aqtau')

@app.route("/", methods=["GET"])
def index():
    return "Bot is LIVE - OK", 200

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

@app.route("/setwebhook", methods=["GET"])
def set_webhook_route():
    try:
        bot.remove_webhook()
        bot.set_webhook(url="https://gold-bot-q8la.onrender.com/" + TOKEN)
        return "Webhook SET", 200
    except Exception as e:
        return f"Error {e}", 500

# ... остальной код gold оставляю как был, он уже работает ...

def get_gold_data():
    for ticker in ["XAUUSD=X", "GC=F"]:
        try:
            df = yf.download(ticker, period="10d", interval="15m", progress=False, auto_adjust=True)
            if df is None or len(df) < 60:
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
    high = df['High']; low = df['Low']; close = df['Close']
    plus_dm = high.diff(); minus_dm = -low.diff()
    plus_dm[plus_dm<0]=0; minus_dm[minus_dm<0]=0
    tr1 = high-low; tr2 = (high-close.shift()).abs(); tr3 = (low-close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period).mean() / atr)
    dx = 100 * abs(plus_di-minus_di) / (plus_di+minus_di)
    return dx.ewm(alpha=1/period).mean()

def make_chart(df, price, is_buy):
    import numpy as np
    plt.figure(figsize=(10,5))
    last60 = df.tail(80)
    plt.plot(last60['Close'], color='#C9A86A', linewidth=2.2)
    plt.plot(last60['Close'].ewm(span=21).mean(), label='EMA21', linestyle='--')
    plt.plot(last60['Close'].ewm(span=50).mean(), label='EMA50', linestyle='--')
    sl = price - 7 if is_buy else price + 7
    tp = price + 18 if is_buy else price - 18
    plt.axhline(price, color='black', linestyle=':', label=f'Entry {price:.1f}')
    plt.axhline(sl, color='red', linestyle='--', label=f'SL {sl:.1f}')
    plt.axhline(tp, color='green', linestyle='--', label=f'TP {tp:.1f}')
    plt.legend(fontsize=8); plt.grid(alpha=0.3); plt.tight_layout()
    path = "/tmp/gold.png"
    plt.savefig(path, dpi=150); plt.close()
    return path, sl, tp

@bot.message_handler(commands=['gold','start'])
def gold_signal(message):
    now = datetime.now(AKTAU_TZ)
    hour = now.hour
    df = get_gold_data()
    if df is None:
        bot.send_message(message.chat.id, "Данных нет, попробуй через минуту")
        return
    if 2 <= hour < 12:
        rsi = float(calc_rsi(df['Close']).iloc[-1])
        bot.send_message(message.chat.id, f"ASIA {hour}:00 WAIT RSI {rsi:.1f}")
        return
    try:
        df['EMA21'] = df['Close'].ewm(span=21).mean()
        df['EMA50'] = df['Close'].ewm(span=50).mean()
        df['RSI'] = calc_rsi(df['Close'])
        df['ADX'] = calc_adx(df)
        last = df.iloc[-1]
        price = float(last['Close']); ema21 = float(last['EMA21']); ema50 = float(last['EMA50'])
        rsi = float(last['RSI']); adx = float(last['ADX'])
        is_buy = ema21 > ema50
        if rsi < 25 or rsi > 75:
            bot.send_message(message.chat.id, f"WAIT RSI {rsi:.1f} Price {price:.2f}")
            return
        if adx < 22:
            bot.send_message(message.chat.id, f"FLAT ADX {adx:.1f} Price {price:.2f}")
            return
        session = "LONDON" if 12 <= hour < 17 else "NY"
        direction = "BUY" if is_buy else "SELL"
        chart_path, sl, tp = make_chart(df, price, is_buy)
        text = f"{direction} {session}\nPrice {price:.2f}\nEMA21 {ema21:.2f} EMA50 {ema50:.2f}\nRSI {rsi:.1f} ADX {adx:.1f}\nSL {sl:.2f} TP {tp:.2f}\nAktau {now.strftime('%H:%M')}"
        with open(chart_path, 'rb') as ph:
            bot.send_photo(message.chat.id, ph, caption=text)
    except Exception as e:
        bot.send_message(message.chat.id, f"Error: {e}")

@app.route('/' + TOKEN, methods=['POST'])
def webhook():
    bot.process_new_updates([telebot.types.Update.de_json(request.stream.read().decode("utf-8"))])
    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
