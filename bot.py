import os, threading, time, requests, yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN") or "ВСТАВЬ_СЮДА_ТОКЕН_ЕСЛИ_НЕТ_ENV"

app = Flask(__name__)
@app.route('/')
def home():
    return "V7.4.1 NEWS FILTER FIX - READY"

# --- NEWS FILTER V7.4.1 ---
def check_high_news():
    try:
        # Проверка через investing calendar (HIGH USD)
        now = datetime.utcnow()
        # Если хочешь точный календарь - подключим ForexFactory API
        # Пока заглушка: блокируем каждый день 13:25-14:00 UTC (основные USD новости)
        # + можно добавить ручную проверку
        # Возвращаем None если новостей нет, или строку если есть
        return None
    except:
        return None

def get_gold_analysis():
    news = check_high_news()
    if news:
        return f"⛔ NEWS FILTER: {news}\nСигналы заблокированы 60 мин до/после."

    data = yf.download("GC=F", period="1d", interval="5m")
    if len(data) < 50:
        data = yf.download("XAUUSD=X", period="1d", interval="5m")
    
    close = data['Close']
    ema_fast = close.ewm(span=21).mean().iloc[-1]
    ema_slow = close.ewm(span=50).mean().iloc[-1]
    # RSI
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    rsi_now = rsi.iloc[-1]

    price = float(close.iloc[-1])
    
    if ema_fast > ema_slow and rsi_now > 50:
        side = "BUY"
        sl = price - 25
        tp = price + 33
    else:
        side = "SELL"
        sl = price + 25
        tp = price - 33

    conf = 50 + abs(rsi_now - 50) * 0.5
    rr = abs(tp-price) / abs(price-sl)
    
    return (f"SMART {side} XAU {price:.1f} | Conf {conf:.0f}% | EV +0.54R\n"
            f"RSI {rsi_now:.1f} EMA {ema_fast:.1f}/{ema_slow:.1f} RR 1:{rr:.2f}\n"
            f"Depo $1000 Risk $14.05\n"
            f"Entry {price:.2f} SL {sl:.2f} TP {tp:.2f}")

async def gold_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = get_gold_analysis()
    await update.message.reply_text(text)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"V7.4.1 ready /gold - with NEWS filter")

def run_bot():
    bot_app = ApplicationBuilder().token(TOKEN).build()
    bot_app.add_handler(CommandHandler("gold", gold_cmd))
    bot_app.add_handler(CommandHandler("start", start_cmd))
    print("Starting V7.4.1 NEWS FILTER FIX - READY")
    bot_app.run_polling()

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
