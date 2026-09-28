import os, requests, threading, telebot
from flask import Flask, request
from datetime import datetime
import pytz

TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)
API = f"https://api.telegram.org/bot{TOKEN}"
AKTAU = pytz.timezone("Asia/Aqtau")

def send(chat_id, text):
    requests.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=15)

def to_float(x):
    # фикс нового yfinance где приходит Series
    try:
        if hasattr(x, 'iloc'):
            x = x.iloc[0] if hasattr(x.iloc[0], 'iloc') == False and hasattr(x.iloc[0], '__float__') else x.iloc[0]
            if hasattr(x, 'iloc'):
                x = x.iloc[0]
        return float(x)
    except:
        return float(x.iloc[0]) if hasattr(x, 'iloc') else float(x)

def get_data():
    import yfinance as yf
    df = yf.download("GC=F", period="2d", interval="5m", progress=False, auto_adjust=False)
    last_price = to_float(df['Close'].iloc[-1])
    return last_price, df

def analyze():
    price, df = get_data()
    # Азия: последние 3 часа = 36 свечей по 5м (ночь)
    asia_high = to_float(df['High'].tail(36).max())
    asia_low = to_float(df['Low'].tail(36).min())
    # Вчерашний диапазон для контекста
    day_high = to_float(df['High'].tail(288).max())
    day_low = to_float(df['Low'].tail(288).min())

    # Логика
    signal = ""
    sl = tp1 = tp2 = 0

    if price > asia_high:
        signal = f"🟢 ЛОНГ - Пробой Азии {asia_high:.1f}"
        sl = asia_high - 8
        tp1 = price + 15
        tp2 = price + 30
    elif price < asia_low:
        signal = f"🔴 ШОРТ - Пробой Азии {asia_low:.1f}"
        sl = asia_low + 8
        tp1 = price - 15
        tp2 = price - 30
    else:
        # Внутри - ждем Лондон
        mid = (asia_high + asia_low)/2
        if price > mid:
            signal = f"🟡 ВНУТРИ Азии (ближе к верху), ждем лонг от {asia_high:.1f}"
        else:
            signal = f"🟡 ВНУТРИ Азии (ближе к низу), ждем шорт от {asia_low:.1f}"
        sl = asia_high + 5
        tp1 = asia_low

    return price, asia_low, asia_high, day_low, day_high, signal, sl, tp1, tp2

def do_gold(chat_id):
    try:
        price, alow, ahigh, dlow, dhigh, signal, sl, tp1, tp2 = analyze()
        now = datetime.now(AKTAU).strftime("%H:%M Актау")

        # Определяем сессию
        hour = datetime.now(AKTAU).hour
        if 5 <= hour < 11:
            sess = "АЗИЯ"
        elif 11 <= hour < 16:
            sess = "ЛОНДОН 🔥"
        else:
            sess = "НЬЮ-ЙОРК"

        msg = f"""🪙 *GOLD: {price:.2f} $* | {sess} {now}

📦 *Азия:* `{alow:.1f} - {ahigh:.1f}`
📅 *Сутки:* `{dlow:.1f} - {dhigh:.1f}`

{signal}
"""
        if "Пробой" in signal:
            msg += f"""
*SL:* `{sl:.1f}`
*TP1:* `{tp1:.1f}`
*TP2:* `{tp2:.1f}`

RR 1:2 соблюдай 1%
"""
        else:
            msg += f"\n⚠️ Нет пробоя - не лезь. Уровень для сделки: `{alow:.1f}` / `{ahigh:.1f}`"

        send(chat_id, msg)
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        send(chat_id, f"Ошибка: {e}")

@bot.message_handler(func=lambda m: True)
def handle(m):
    t = (m.text or "").lower()
    if "gold" in t or "start" in t:
        send(m.chat.id, "⏳ Считаю...")
        threading.Thread(target=do_gold, args=(m.chat.id,), daemon=True).start()
    else:
        send(m.chat.id, "Напиши /gold")

@app.route('/')
def home(): return "LIVE"

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    try:
        data = request.get_data().decode('utf-8')
        upd = telebot.types.Update.de_json(data)
        bot.process_new_updates([upd])
    except: pass
    return '', 200

@app.route('/setwebhook')
def sethook():
    bot.remove_webhook()
    r = bot.set_webhook(url=f'https://gold-bot-q8la.onrender.com/{TOKEN}')
    return f"SET {r}"
