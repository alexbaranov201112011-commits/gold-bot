import telebot
import yfinance as yf
import pandas as pd
import mplfinance as mpf
import io
from datetime import datetime, timedelta
import pytz
import os

TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(TOKEN)

AKTAU_TZ = pytz.timezone("Asia/Aqtau")

def is_first_friday(date):
    return date.weekday() == 4 and 1 <= date.day <= 7

def get_news_status():
    now = datetime.now(AKTAU_TZ)
    time_str = now.strftime("%H:%M")
    day = now.weekday() # 0=Пн
    
    # 🔴 NFP - Первая пятница месяца 18:00-19:30
    if is_first_friday(now) and "18:00" <= time_str <= "19:30":
        return True, "🔴 СЕГОДНЯ NFP (Non-Farm) 18:30 - Рынок бешеный! Не торгуем!"
    
    # 🔴 FOMC - Среда 23:00-01:00 (примерно, ставки)
    # Блокируем среду ночь + четверг 00-01
    if day == 2 and time_str >= "23:00": # Среда
        return True, "🔴 FOMC / СТАВКА ФРС сегодня ночью - Не торгуем до утра!"
    if day == 3 and time_str <= "01:00": # Четверг ночь
        return True, "🔴 После FOMC - волатильность! Ждем до Лондона"
    
    # 🟡 Ежедневные новости США 18:20-19:10 (CPI, PPI и тд)
    if "18:20" <= time_str <= "19:10":
        return True, "🟡 Сейчас новости США (CPI и др) - Ждем 19:10"
    
    return False, ""

def get_session():
    now = datetime.now(AKTAU_TZ)
    t = now.strftime("%H:%M")
    hour = now.hour
    
    is_news, news_text = get_news_status()
    if is_news:
        return "NEWS", news_text
    
    # Актау: Лондон 12:00-17:00, NY 17:00-02:00, Азия 02:00-12:00
    if 12 <= hour < 17:
        return "LONDON", "🇪🇺 СЕССИЯ: ЛОНДОН - Торгуем!"
    elif hour >= 17 or hour < 2:
        return "NY", "🇺🇸 СЕССИЯ: НЬЮ-ЙОРК - Торгуем!"
    else:
        return "ASIA", "🌙 СЕССИЯ: АЗИЯ - Отдыхаем, флэт"

@bot.message_handler(commands=['start'])
def start(m):
    session, text = get_session()
    now = datetime.now(AKTAU_TZ).strftime("%H:%M")
    bot.send_message(m.chat.id, f"Бот V7.7 GOLD+NEWS 📈\nСейчас {now} по Актау\n{text}\n\nЖми /gold для анализа", parse_mode="Markdown")

@bot.message_handler(commands=['gold'])
def gold(m):
    try:
        session, session_text = get_session()
        
        # Проверка сессии и новостей
        if session == "ASIA":
            bot.send_message(m.chat.id, f"{session_text}\n\n⛔ Сигналов нет до 12:00")
            return
        if session == "NEWS":
            bot.send_message(m.chat.id, f"{session_text}\n\n⛔ {session_text}\nЖми /gold после новостей")
            return

        # Скачиваем золото
        data = yf.download("GC=F", period="2d", interval="5m", progress=False)
        if len(data) < 60:
            bot.send_message(m.chat.id, "Данных мало, подожди")
            return
        
        # EMA и RSI
        data['EMA21'] = data['Close'].ewm(span=21).mean()
        data['EMA50'] = data['Close'].ewm(span=50).mean()
        
        delta = data['Close'].diff()
        gain = delta.where(delta > 0, 0).ewm(span=14).mean()
        loss = -delta.where(delta < 0, 0).ewm(span=14).mean()
        rs = gain / loss
        data['RSI'] = 100 - (100 / (1 + rs))
        
        price = float(data['Close'].iloc[-1])
        ema21 = float(data['EMA21'].iloc[-1])
        ema50 = float(data['EMA50'].iloc[-1])
        rsi = float(data['RSI'].iloc[-1])
        
        # Фильтр RSI
        if rsi < 30:
            bot.send_message(m.chat.id, f"{session_text}\n🕐 Время Актау: {datetime.now(AKTAU_TZ).strftime('%H:%M')}\n\n⛔ **WAIT - ЖДЕМ**\nПричина: Перепроданность RSI {rsi:.1f}\nЦена: {price:.2f}$ | RSI: {rsi:.1f}")
            return
        if rsi > 70:
            bot.send_message(m.chat.id, f"{session_text}\n🕐 Время Актау: {datetime.now(AKTAU_TZ).strftime('%H:%M')}\n\n⛔ **WAIT - ЖДЕМ**\nПричина: Перекупленность RSI {rsi:.1f}\nЦена: {price:.2f}$ | RSI: {rsi:.1f}")
            return
        
        # Сигнал
        if ema21 > ema50 and rsi > 50:
            signal = f"🟢 **SMART BUY**\n{session_text}\nЦена: {price:.2f}$ | RSI: {rsi:.1f} | Уверенность 75%"
            sl = price - 25
            tp = price + 33
        elif ema21 < ema50 and rsi < 50:
            signal = f"🔴 **SMART SELL**\n{session_text}\nЦена: {price:.2f}$ | RSI: {rsi:.1f} | Уверенность 75%"
            sl = price + 25
            tp = price - 33
        else:
            signal = f"🟡 **WAIT**\n{session_text}\nЦена: {price:.2f}$ | RSI: {rsi:.1f}\nБоковик, ждем тренд"
            sl = tp = None
        
        # График
        df = data.tail(60)
        buf = io.BytesIO()
        mpf.plot(df, type='candle', mav=(21,50), volume=False, style='yahoo', savefig=dict(fname=buf, dpi=100))
        buf.seek(0)
        
        msg = f"{signal}\nДепо $1000 | SL {sl:.2f}$ | TP {tp:.2f}$" if sl else signal
        bot.send_photo(m.chat.id, buf, caption=msg, parse_mode="Markdown")
        
    except Exception as e:
        bot.send_message(m.chat.id, f"Ошибка: {e}")

bot.infinity_polling()
