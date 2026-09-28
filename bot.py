import os, requests, telebot
from flask import Flask, request

TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)
API = f"https://api.telegram.org/bot{TOKEN}"

def send(chat_id, text):
    requests.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=15)

def do_gold(chat_id):
    try:
        import yfinance as yf
        d = yf.download("GC=F", period="1d", interval="5m", progress=False)
        price = float(d['Close'].iloc[-1])
        send(chat_id, f"GOLD: {price:.2f} $\nАзия 11:48 Актау - сработало!")
    except Exception as e:
        send(chat_id, f"Ошибка загрузки: {e}")

@bot.message_handler(func=lambda m: True)
def handle(m):
    txt = (m.text or "").lower()
    print(f"Got message: {txt} from {m.chat.id}")
    if "gold" in txt or "start" in txt:
        send(m.chat.id, "⏳ Считаю...")
        import threading
        threading.Thread(target=do_gold, args=(m.chat.id,), daemon=True).start()
    else:
        send(m.chat.id, "Напиши /gold")

@app.route('/')
def home(): return "LIVE"

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    try:
        data = request.get_data().decode('utf-8')
        print(f"WEBHOOK: {data[:300]}")
        update = telebot.types.Update.de_json(data)
        bot.process_new_updates([update])
    except Exception as e:
        print(f"ERR: {e}")
        import traceback; traceback.print_exc()
    return '', 200

@app.route('/setwebhook')
def sethook():
    bot.remove_webhook()
    r = bot.set_webhook(url=f'https://gold-bot-q8la.onrender.com/{TOKEN}')
    return f"Webhook SET: {r}"
