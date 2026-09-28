import os
import threading
import telebot
from flask import Flask, request

TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

def do_gold(chat_id):
    try:
        import yfinance as yf
        d = yf.download("GC=F", period="1d", interval="5m", progress=False)
        price = float(d['Close'].iloc[-1])
        bot.send_message(chat_id, f"GOLD: {price:.2f} $")
    except Exception as e:
        bot.send_message(chat_id, f"Ошибка: {e}")

@bot.message_handler(commands=['gold','start'])
def handle(m):
    bot.send_message(m.chat.id, "⏳ Считаю...")
    threading.Thread(target=do_gold, args=(m.chat.id,)).start()

@app.route('/')
def home():
    return "LIVE"

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    return '', 403

@app.route('/setwebhook')
def sethook():
    bot.remove_webhook()
    r = bot.set_webhook(url=f'https://gold-bot-q8la.onrender.com/{TOKEN}')
    return f"Webhook SET: {r}"
