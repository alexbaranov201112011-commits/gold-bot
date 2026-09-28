import os, requests, threading, telebot
from flask import Flask, request

TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)
API = f"https://api.telegram.org/bot{TOKEN}"

def send(chat_id, text):
    requests.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=15)

def get_price():
    import yfinance as yf
    df = yf.download("GC=F", period="1d", interval="5m", progress=False, auto_adjust=False)
    close = df['Close'].iloc[-1]
    price = float(close.iloc[0] if hasattr(close, 'iloc') else close)
    return price, df

def do_gold(chat_id):
    try:
        price, df = get_price()
        high = float(df['High'].tail(36).max().iloc[0] if hasattr(df['High'].tail(36).max(), 'iloc') else df['High'].tail(36).max())
        low = float(df['Low'].tail(36).min().iloc[0] if hasattr(df['Low'].tail(36).min(), 'iloc') else df['Low'].tail(36).min())
        sig = "LONG" if price > high else "SHORT" if price < low else "ВНУТРИ"
        send(chat_id, f"GOLD: {price:.2f} $\nАзия {low:.1f}-{high:.1f}\nСигнал: {sig}")
    except Exception as e:
        send(chat_id, f"Ошибка: {e}")

@bot.message_handler(func=lambda m: True)
def handle(m):
    t = (m.text or "").lower()
    if "gold" in t or "start" in t:
        send(m.chat.id, "Считаю...")
        threading.Thread(target=do_gold, args=(m.chat.id,), daemon=True).start()

@app.route('/')
def home():
    return "LIVE"

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    try:
        data = request.get_data().decode('utf-8')
        upd = telebot.types.Update.de_json(data)
        bot.process_new_updates([upd])
    except Exception as e:
        print(e)
    return '', 200

@app.route('/setwebhook')
def sethook():
    bot.remove_webhook()
    r = bot.set_webhook(url=f'https://gold-bot-q8la.onrender.com/{TOKEN}')
    return f"SET {r}"
