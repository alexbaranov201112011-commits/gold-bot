import os, requests, threading, telebot
from flask import Flask, request

TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)
API = f"https://api.telegram.org/bot{TOKEN}"

def send(chat_id, text):
    try:
        requests.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=15)
    except Exception as e:
        print(f"SEND ERR: {e}")

def get_gold_price():
    import yfinance as yf
    d = yf.download("GC=F", period="1d", interval="5m", progress=False, auto_adjust=False)
    close = d['Close']
    val = close.iloc[-1]
    # Фикс для новых версий yfinance - возвращает Series
    price = float(val.iloc[0] if hasattr(val, 'iloc') else val)
    return price, d

def analyze():
    price, df = get_gold_price()
    # Логика Азия 11:48 Актау
    # Берем последние 3 часа для Азии
    asia_high = float(df['High'].tail(36).max().iloc[0] if hasattr(df['High'].tail(36).max(), 'iloc') else df['High'].tail(36).max())
    asia_low = float(df['Low'].tail(36).min().iloc[0] if hasattr(df['Low'].tail(36).min(), 'iloc') else df['Low'].tail(36).min())

    signal = "ЖДЁМ"
    if price > asia_high:
        signal = f"🟢 ЛОНГ (пробой Азии {asia_high:.1f})"
    elif price < asia_low:
        signal = f"🔴 ШОРТ (пробой Азии {asia_low:.1f})"
    else:
        signal = f"🟡 Внутри Азии {asia_low:.1f} - {asia_high:.1f}"

    return price, asia_high, asia_low, signal

def do_gold(chat_id):
    try:
        price, high, low, signal = analyze()
        send(chat_id, f"🪙 GOLD: {price:.2f} $\n\n📊 Азия: {low:.1f} - {high:.1f}\n{signal}\n\n⏰ 11:48 Актау | 08:48 МСК")
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        send(chat_id, f"Ошибка: {e}")

@bot.message_handler(func=lambda m: True)
def handle(m):
    txt = (m.text or "").lower()
    print(f"MSG: {txt}")
    if "gold" in txt or "start" in txt:
        send(m.chat.id, "⏳ Считаю...")
        threading.Thread(target=do_gold, args=(m.chat.id,), daemon=True).start()
    else:
        send(m.chat.id, "Напиши /gold для сигнала")

@app.route('/')
def home():
    return "LIVE"

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    try:
        data = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(data)
        bot.process_new_updates([update])
    except Exception as e:
        print(f"WEBHOOK ERR: {e}")
    return '', 200

@app.route('/setwebhook')
def sethook():
    bot.remove_webhook()
    r = bot.set_webhook(url=f'https://gold-bot-q8la.onrender.com/{TOKEN}')
    return f"Webhook SET: {r}"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
