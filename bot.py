from flask import Flask
import threading, os
import telebot
import yfinance as yf
import matplotlib.pyplot as plt
from datetime import datetime

app = Flask(__name__)
@app.route('/')
def home(): return "Bot is alive!"
def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
threading.Thread(target=run_flask, daemon=True).start()

TOKEN = os.environ.get("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=['start'])
def start(m):
    bot.send_message(m.chat.id, "Бот запущен! Напиши /gold - цена золота, /chart - график")

@bot.message_handler(commands=['gold'])
def gold(m):
    try:
        data = yf.Ticker("GC=F").history(period="1d")
        price = data['Close'].iloc[-1]
        bot.send_message(m.chat.id, f"🪙 Золото сейчас: ${price:.2f} за унцию\n{datetime.now().strftime('%d.%m.%Y %H:%M')}")
    except Exception as e:
        bot.send_message(m.chat.id, f"Ошибка: {e}")

@bot.message_handler(commands=['chart'])
def chart(m):
    try:
        data = yf.Ticker("GC=F").history(period="5d")
        plt.figure()
        plt.plot(data['Close'])
        plt.title("Gold 5 days")
        plt.savefig("chart.png")
        plt.close()
        with open("chart.png", "rb") as f:
            bot.send_photo(m.chat.id, f)
    except Exception as e:
        bot.send_message(m.chat.id, f"Ошибка графика: {e}")

print("Bot polling started...")
bot.infinity_polling()
