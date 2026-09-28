import os, threading, requests
import telebot
from flask import Flask, request

TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
API = f"https://api.telegram.org/bot{TOKEN}"

def send(chat_id, text):
    try:
        requests.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=10)
    except Exception as e:
        print(f"SEND ERROR: {e}")

def do_gold(chat_id):
    try:
        import yfinance as yf
        d = yf.download("GC=F", period="1d", interval="5m", progress=False)
        price = float(d['Close'].iloc[-1])
        send(chat_id, f"GOLD: {price:.2f} $\nРаботает! Азия 11:48 Актау")
    except Exception as e:
        print(f"GOLD ERROR: {e}")
        send(chat_id, f"Ошибка: {e}")

@bot.message_handler(commands=['gold','start'])
def handle(m):
    print(f"Got /gold from {m.chat.id}")
    send(m.chat.id, "⏳ Считаю...")
    threading.Thread(target=do_gold, args=(m.chat.id,)).start()

@app.route('/')
def home(): return "LIVE"

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    try:
        json_str = request.get_data().decode('utf-8')
        print(f"WEBHOOK JSON: {json_str[:200]}")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        print(f"WEBHOOK ERROR: {e}")
    return '', 200

@app.route('/setwebhook')
def sethook():
    bot.remove_webhook()
    r = bot.set_webhook(url=f'https://gold-bot-q8la.onrender.com/{TOKEN}')
    return f"Webhook SET: {r}"
