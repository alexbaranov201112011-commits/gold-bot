flask import Flask
from datetime import datetime
import pytz
import pandas as pd
import mplfinance as mpf

TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN, threaded=False)
AKTAU_TZ = pytz.timezone("Asia/Aqtau")

app = Flask(__name__)
@app.route('/')
def home():
    return "Bot V7.8 is running!"

def get_session():
    now = datetime.now(AKTAU_TZ)
    t = now.strftime("%H:%M")
    # NEWS FILTER
    if now.weekday() == 4 and 1 <= now.day <= 7 and "18:00" <= t <= "19:30":
        return "NEWS", "🔴 СЕГОДНЯ NFP 18:30 - Не торгуем!"
    if "18:20" <= t <= "19:10":
        return "NEWS", "🟡 Новости США - Ждем 19:10"
    hour = now.hour
    if 12 <= hour < 17:
        return "LONDON", "🇪🇺 ЛОНДОН - Торгуем!"
    elif hour >= 17 or hour < 2:
        return "NY", "🇺🇸 НЬЮ-ЙОРК - Торгуем!"
    else:
        return "ASIA", "🌙 АЗИЯ - Отдыхаем"

@bot.message_handler(commands=['start','gold'])
def handle(m):
    session, text = get_session()
    if session in ["ASIA","NEWS"]:
        bot.send_message(m.chat.id, f"{text}\n⛔ Сейчас {datetime.now(AKTAU_TZ).strftime('%H:%M')} по Актау - ждем")
        return
    try:
        data = yf.download("GC=F", period="2d", interval="5m", progress=False)
        data['EMA21'] = data['Close'].ewm(span=21).mean()
        data['EMA50'] = data['Close'].ewm(span=50).mean()
        delta = data['Close'].diff()
        gain = delta.where(delta>0,0).ewm(span=14).mean()
        loss = -delta.where(delta<0,0).ewm(span=14).mean()
        data['RSI'] = 100 - (100/(1+gain/loss))
        price = float(data['Close'].iloc[-1])
        rsi = float(data['RSI'].iloc[-1])
        ema21 = float(data['EMA21'].iloc[-1])
        ema50 = float(data['EMA50'].iloc[-1])
        
        if rsi < 30 or rsi > 70:
            bot.send_message(m.chat.id, f"{text}\nWAIT RSI {rsi:.1f}")
            return
            
        if ema21 > ema50:
            sig = f"🟢 BUY {text}\nЦена {price:.2f} RSI {rsi:.1f}"
        else:
            sig = f"🔴 SELL {text}\nЦена {price:.2f} RSI {rsi:.1f}"
        
        buf = io.BytesIO()
        mpf.plot(data.tail(60), type='candle', mav=(21,50), style='yahoo', savefig=dict(fname=buf, dpi=100))
        buf.seek(0)
        bot.send_photo(m.chat.id, buf, caption=sig)
    except Exception as e:
        bot.send_message(m.chat.id, f"Ошибка {e}")

def run_bot():
    bot.remove_webhook()
    import time; time.sleep(2)
    bot.infinity_polling(skip_pending=True)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
