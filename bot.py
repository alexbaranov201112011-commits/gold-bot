import requests
import yfinance as yf
import pandas as pd
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from flask import Flask
import threading
import os

# --- VERSION ---
VERSION = "V7.4 NEWS FILTER - READY"
TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН_СЮДА")

# Flask для Render
web = Flask(__name__)
@web.route('/')
def home():
    return VERSION

# --- NEWS FILTER V7.4 ---
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

def check_news():
    try:
        data = requests.get(CALENDAR_URL, timeout=5).json()
        for ev in data:
            if ev.get('impact') != 'High': 
                continue
            if ev.get('currency') != 'USD': 
                continue
            
            ev_time = datetime.fromisoformat(ev['date'].replace('Z',''))
            diff_min = (ev_time - datetime.utcnow()).total_seconds() / 60

            # Блок: 45 мин до и 30 мин после HIGH USD
            if -30 < diff_min < 45:
                return f"⛔ БЛОК: {ev['title']} через {int(diff_min)} мин (HIGH USD)\nДо {ev_time.strftime('%H:%M')} UTC сигналов нет."
        return None
    except Exception as e:
        print(f"News error: {e}")
        return None

# --- GOLD LOGIC ---
def get_gold_signal():
    df = yf.download("GC=F", period="5d", interval="15m", progress=False)
    if len(df) < 200:
        return None
    
    close = df['Close']
    high = df['High']
    low = df['Low']
    
    price = float(close.iloc[-1])
    ema50 = float(close.ewm(span=50).mean().iloc[-1])
    ema200 = float(close.ewm(span=200).mean().iloc[-1])
    
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = -delta.where(delta < 0, 0).rolling(14).mean()
    rs = gain / loss
    rsi = float(100 - (100 / (1 + rs.iloc[-1])))

    atr = float((high - low).rolling(14).mean().iloc[-1])
    
    if price > ema50 > ema200 and rsi < 70 and rsi > 45:
        side = "BUY 🟢"
        sl = price - atr * 1.5
        tp = price + atr * 3.0
    elif price < ema50 < ema200 and rsi > 30 and rsi < 55:
        side = "SELL 🔴"
        sl = price + atr * 1.5
        tp = price - atr * 3.0
    else:
        side = "WAIT"
        sl = tp = 0

    return price, side, sl, tp, rsi, atr, ema50, ema200

# --- TELEGRAM HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"{VERSION}\n\nКоманды:\n/gold - сигнал по золоту с фильтром новостей")

async def gold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 1. Проверка новостей
    news = check_news()
    if news:
        await update.message.reply_text(
            f"{news}\n\n"
            f"📰 Торговля на HIGH новости = слив.\n"
            f"Подожди 1 час."
        )
        return

    # 2. Техника
    data = get_gold_signal()
    if not data:
        await update.message.reply_text("Ошибка данных, попробуй через минуту")
        return
        
    price, side, sl, tp, rsi, atr, ema50, ema200 = data
    
    if side == "WAIT":
        await update.message.reply_text(
            f"✅ Новостей нет\n\n"
            f"🟡 GOLD WAIT\n"
            f"Цена: {price:.2f}\n"
            f"RSI: {rsi:.1f} | EMA50: {ema50:.2f}\n"
            f"Нет четкого тренда - ждем."
        )
        return

    rr = abs(tp-price) / abs(price-sl) if sl != 0 else 0
    winrate = 0.58
    ev = winrate*rr - (1-winrate)
    
    text = f"""✅ Новостей нет - торгуем

🟡 GOLD {side}
Цена: {price:.2f}

Вход: {price:.2f}
SL: {sl:.2f}
TP: {tp:.2f}

RR: 1:{rr:.1f} | RSI: {rsi:.1f}
EV: {ev:.2f}R | ATR: {atr:.2f}

Kelly: 12% от депа
{VERSION}
"""
    await update.message.reply_text(text)

# --- RUN ---
def run_bot():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gold", gold))
    print(f"Starting {VERSION}")
    app.run_polling()

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    web.run(host="0.0.0.0", port=port)
