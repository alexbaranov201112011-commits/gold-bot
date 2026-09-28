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
        data = yf.download("GC=F", period="1d", interval="5m", progress=False)
        price = float(data['Close'].iloc[-1])
        bot.send_message(chat_id, f"GOLD: {price:.2f} $\nАзия ждем 11:48 Актау")
    except Exception as e:
        bot.send_message(chat_id, f"Ошибка золота: {e}")

@bot.message_handler(commands=['gold','start'])
def handle(message):
    bot.send_message(message.chat.id, "⏳ Считаю...")
    threading.Thread(target=do_gold, args=(message.chat.id,)).start()

@app.route('/')
def home():
    return "Bot LIVE"

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    bot.process_new_updates([telebot.types.Update.de_json(request.stream.read().decode("utf-8"))])
    return "ok", 200

@app.route('/setwebhook')
def sethook():
    bot.remove_webhook()
    bot.set_webhook(url=f'https://gold-bot-q8la.onrender.com/{TOKEN}')
    return "Webhook SET"
