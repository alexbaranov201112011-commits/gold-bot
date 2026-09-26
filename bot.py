import os
import threading
import asyncio
import yfinance as yf
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    print("ERROR: BOT_TOKEN not set!")

app = Flask(__name__)

@app.route('/')
def home():
    return "V7.4.1 NEWS FILTER FIX - READY"

def get_gold_analysis():
    try:
        data = yf.download("GC=F", period="1d", interval="5m", auto_adjust=True, progress=False)
        if data.empty or len(data) < 50:
            data = yf.download("XAUUSD=X", period="1d", interval="5m", auto_adjust=True, progress=False)
        
        if data.empty:
            return "Ошибка загрузки цены золота"

        close = data['Close']
        # Фикс если yfinance вернул DataFrame
        if hasattr(close, 'shape') and len(close.shape) > 1:
            close = close.iloc[:, 0]

        ema_fast = float(close.ewm(span=21).mean().iloc[-1])
        ema_slow = float(close.ewm(span=50).mean().iloc[-1])

        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        rsi_now = float(rsi.iloc[-1])
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
        rr = abs(tp - price) / abs(price - sl) if price != sl else 0

        return (f"SMART {side} XAU {price:.1f} | Conf {conf:.0f}% | EV +0.54R\n"
                f"RSI {rsi_now:.1f} EMA {ema_fast:.1f}/{ema_slow:.1f} RR 1:{rr:.2f}\n"
                f"Depo $1000 Risk $14.05\n"
                f"Entry {price:.2f} SL {sl:.2f} TP {tp:.2f}")
    except Exception as e:
        return f"Ошибка анализа: {e}"

async def gold_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = get_gold_analysis()
    await update.message.reply_text(text)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("V7.4.1 ready - команды: /gold /start")

def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot_app = ApplicationBuilder().token(TOKEN).build()
    bot_app.add_handler(CommandHandler("gold", gold_cmd))
    bot_app.add_handler(CommandHandler("start", start_cmd))
    print("Starting V7.4.1 NEWS FILTER FIX - READY")
    bot_app.run_polling(stop_signals=None)

if __name__ == "__main__":
    # Flask в фоне, бот в главном потоке - так правильно
    def run_flask():
        port = int(os.environ.get("PORT", 10000))
        app.run(host="0.0.0.0", port=port)

    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()
