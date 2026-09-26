import os
import telebot
from flask import Flask, request
import yfinance as yf
import pandas as pd
from datetime import datetime
import pytz

TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    TOKEN = os.environ.get("BOT_")
    
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
AKTAU_TZ = pytz.timezone('Asia/Aqtau')
LOT = 0.03
STOP_DOLLAR = 7

def get_gold_data():
    data = yf.download("GC=F", period="5d", interval="15m", progress=False)
    if data is None or len(data) < 60:
        return None
    return data

def calculate_indicators(df):
    df['EMA21'] = df['Close'].ewm(span=21).mean()
    df['EMA50'] = df['Close'].ewm(span=50).mean()
    last = df.iloc[-1]
    ema21 = float(last['EMA21'])
    ema50 = float(last['EMA50'])
    price = float(last['Close'])
    adx = 32 if abs(ema21-ema50) > 5 else 19
    return price, ema21, ema50, adx

@bot.message_handler(commands=['gold', 'start'])
def gold_signal(message):
    now_aktau = datetime.now(AKTAU_TZ)
    hour = now_aktau.hour
    if 2 <= hour < 12:
        bot.send_message(message.chat.id, f"🚫 Азия - не торгуем 🌙\n⏰ Актау: {now_aktau.strftime('%H:%M')}\n💤 Ждем Лондон 12:00")
        return
    try:
        df = get_gold_data()
        if df is None:
            bot.send_message(message.chat.id, "❌ Данных нет")
            return
        price, ema21, ema50, adx = calculate_indicators(df)
        is_buy = ema21 > ema50
        trend = "БЫЧИЙ 📈" if is_buy else "МЕДВЕЖИЙ 📉"
        if adx < 25:
            bot.send_message(message.chat.id, f"🟡 ФЛЕТ\n💰 {price:.2f}$\n📉 ADX {adx} слабый")
            return
        session = "ЛОНДОН 🇬🇧" if 12 <= hour < 17 else "НЬЮ-ЙОРК 🇺🇸"
        direction = "BUY 🟢" if is_buy else "SELL 🔴"
        stop_price = price - STOP_DOLLAR if is_buy else price + STOP_DOLLAR
        take_price = price + 18 if is_buy else price - 18
        loss_money = STOP_DOLLAR * (LOT * 100)
        bot.send_message(message.chat.id, f"✅ {direction} {session}\n━━━━━━━━━━━━━━\n💰 {price:.2f}$\n📈 {trend}\n💪 ADX {adx}\n━━━━━━━━━━━━━━\n🎯 Вход: {price:.2f}\n⛔ Стоп: {stop_price:.2f} (-{loss_money:.0f}$)\n🏆 Тейк: {take_price:.2f} (+54$)\n📦 Лот: {LOT}\n⏰ Актау: {now_aktau.strftime('%H:%M')}")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")

@app.route('/' + TOKEN, methods=['POST'])
def webhook():
    bot.process_new_updates([telebot.types.Update.de_json(request.stream.read().decode("utf-8"))])
    return "ok", 200

@app.route("/")
def webhook_check():
    bot.remove_webhook()
    bot.set_webhook(url="https://gold-bot-q8la.onrender.com/" + TOKEN)
    return "Бот запущен!", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
