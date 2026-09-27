import os
import threading
import telebot
from flask import Flask, request
import yfinance as yf
import pandas as pd

TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

def analyze_and_send(chat_id):
    try:
        # ... сюда вставь свою логику золота, как была ...
        # для теста пока просто:
        data = yf.download("GC=F", period="5d", interval="15m", progress=False)
        price = float(data['Close'].iloc[-1])
        bot.send_message(chat_id, f"GOLD: {price:.2f}\nASIA WAIT 03:51 Aktau")
    except Exception as e:
        bot.send_message(chat_id, f"Ошибка: {e}")

@bot.message_handler(commands=['gold','start'])
def gold_signal(message):
    bot.send_message(message.chat.id, "⏳ Считаю золото...")
    threading.Thread(target=analyze_and_send, args=(message.chat.id,)).start()

@app.route('/')
def home(): return "Bot is LIVE - OK"

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    bot.process_new_updates([telebot.types.Update.de_json(request.stream.read().decode("utf-8"))])
    return "ok", 200

@app.route('/setwebhook')
def set_webhook():
    bot.remove_webhook()
    bot.set_webhook(url=f'https://gold-bot-q8la.onrender.com/{TOKEN}')
    return "Webhook SET"

if __name__ == "__main__":
    app.run()
