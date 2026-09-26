import os, telebot, yfinance as yf, pandas as pd
from flask import Flask, request
from datetime import datetime
import pytz
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("BOT_")
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
AKTAU_TZ = pytz.timezone('Asia/Aqtau')
LOT = 0.03
STOP_DOLLAR = 7
TAKE_DOLLAR = 18

def get_gold_data():
    df = yf.download("GC=F", period="10d", interval="15m", progress=False, auto_adjust=True)
    if df is None or len(df) < 60:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df

def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta>0, 0).ewm(alpha=1/period).mean()
    loss = -delta.where(delta<0, 0).ewm(alpha=1/period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calc_adx(df, period=14):
    high, low, close = df['High'], df['Low'], df['Close']
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm[plus_dm<0]=0
    minus_dm[minus_dm<0]=0
    tr = pd.concat([high-low, (high-close.shift()).abs(), (low-close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period).mean() / atr)
    dx = 100 * abs(plus_di-minus_di) / (plus_di+minus_di)
    return dx.ewm(alpha=1/period).mean()

def make_chart(df, price, ema21, ema50, is_buy):
    plt.figure(figsize=(10,5))
    last60 = df.tail(80)
    plt.plot(last60['Close'], label='XAU Price', color='#C9A86A', linewidth=2.2)
    plt.plot(last60['EMA21'], label='EMA21', color='#1E90FF', linestyle='--', linewidth=1.5)
    plt.plot(last60['EMA50'], label='EMA50', color='#FF4500', linestyle='--', linewidth=1.5)
    entry = price
    sl = price - STOP_DOLLAR if is_buy else price + STOP_DOLLAR
    tp = price + TAKE_DOLLAR if is_buy else price - TAKE_DOLLAR
    plt.axhline(entry, color='black', linestyle=':', linewidth=1, label=f'Вход {entry:.1f}')
    plt.axhline(sl, color='red', linestyle='--', linewidth=1, label=f'SL {sl:.1f}')
    plt.axhline(tp, color='green', linestyle='--', linewidth=1, label=f'TP {tp:.1f}')
    plt.legend(fontsize=8)
    plt.title(f"GOLD XAU - {'BUY' if is_buy else 'SELL'} | EMA21 {ema21:.1f} > EMA50 {ema50:.1f}" if is_buy else f"GOLD XAU - SELL | EMA21 {ema21:.1f} < EMA50 {ema50:.1f}")
    plt.grid(alpha=0.3)
    plt.tight_layout()
    path = "/tmp/gold.png"
    plt.savefig(path, dpi=150)
    plt.close()
    return path, sl, tp

@bot.message_handler(commands=['gold','start'])
def gold_signal(message):
    now = datetime.now(AKTAU_TZ)
    hour = now.hour
    # 1. Азия блок
    if 2 <= hour < 12:
        df = get_gold_data()
        if df is not None:
            df['RSI'] = calc_rsi(df['Close'])
            rsi = float(df['RSI'].iloc[-1])
            price = float(df['Close'].iloc[-1])
            bot.send_message(message.chat.id, f"🌙 СЕССИЯ: ASIA 🌙\n🕐 Время Актау: {hour}:00\n📍 Азиатская сессия - флэт\n\n⛔ WAIT - ЖДЕМ ⏸️\n\n📍 Причина: {'Перепроданность' if rsi<30 else 'Флэт Азии' } RSI {rsi:.1f}\n💰 Цена: {price:.2f}$ | 📊 RSI: {rsi:.1f}\n\n⚠️ Сейчас ASIA 🌙 - торговать опасно!\n✅ Торгуем только:\n🇪🇺 Лондон 12:00-17:00\n🇺🇸 Нью-Йорк 17:00-02:00 по Актау\n\n⏰ Приходи в Лондон!")
            return
        else:
            bot.send_message(message.chat.id, f"🌙 ASIA {hour}:00 - не торгуем, ждем Лондон 12:00")
            return
    try:
        df = get_gold_data()
        if df is None:
            bot.send_message(message.chat.id, "❌ Данных нет")
            return
        df['EMA21'] = df['Close'].ewm(span=21).mean()
        df['EMA50'] = df['Close'].ewm(span=50).mean()
        df['RSI'] = calc_rsi(df['Close'])
        df['ADX'] = calc_adx(df)
        last = df.iloc[-1]
        price = float(last['Close']); ema21=float(last['EMA21']); ema50=float(last['EMA50'])
        rsi=float(last['RSI']); adx=float(last['ADX'])
        is_buy = ema21 > ema50
        
        # 2. RSI фильтр
        if rsi < 25 or rsi > 75:
            bot.send_message(message.chat.id, f"⛔ WAIT - RSI экстремум\n💰 {price:.2f}$ | RSI {rsi:.1f}\n⚠️ Перепроданность" if rsi<30 else f"⛔ WAIT - RSI экстремум\n💰 {price:.2f}$ | RSI {rsi:.1f}\n⚠️ Перекупленность")
            return
        # 3. ADX фильтр
        if adx < 22:
            bot.send_message(message.chat.id, f"🟡 ФЛЕТ\n💰 {price:.2f}$\n📉 ADX {adx:.1f} слабый тренд\n⏰ Актау {now.strftime('%H:%M')}")
            return
            
        session = "ЛОНДОН 🇬🇧" if 12 <= hour < 17 else "НЬЮ-ЙОРК 🇺🇸"
        direction = "BUY 🟢📈" if is_buy else "SELL 🔴📉"
        chart_path, sl, tp = make_chart(df, price, ema21, ema50, is_buy)
        
        text = f"✅ {direction} {session}\n━━━━━━━━━━━━━━\n💰 Цена: {price:.2f}$\n📈 EMA21 {ema21:.2f} | EMA50 {ema50:.2f}\n📊 RSI {rsi:.1f} | 💪 ADX {adx:.1f}\n━━━━━━━━━━━━━━\n🎯 Вход: {price:.2f}\n⛔ Стоп: {sl:.2f} (-{STOP_DOLLAR*LOT*100:.0f}$)\n🏆 Тейк: {tp:.2f} (+{TAKE_DOLLAR*LOT*100:.0f}$)\n📦 Лот: {LOT}\n⏰ Актау: {now.strftime('%H:%M')} ⏰"
        
        with open(chart_path, 'rb') as ph:
            bot.send_photo(message.chat.id, ph, caption=text)
            
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
    return "Бот запущен! 📈🌙", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))
