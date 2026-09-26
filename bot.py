import os, threading, asyncio, io
from datetime import datetime
import pytz
import yfinance as yf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.getenv("BOT_TOKEN")
app = Flask(__name__)
@app.route('/')
def home(): return "V7.6 LONDON+NY READY"

# Время Актaу UTC+5
TZ_AKTAU = pytz.timezone('Asia/Aqtau')

def get_session():
    now = datetime.now(TZ_AKTAU)
    h = now.hour
    
    # Лондон 12:00-17:00 по Актaу (07:00-12:00 UTC)
    # Нью-Йорк 17:00-02:00 по Актaу (12:00-21:00 UTC)
    if 12 <= h < 17:
        return "LONDON 🇪🇺", True, "Лондонская сессия - высокая волатильность", h
    elif h >= 17 or h < 2:
        return "NEW-YORK 🇺🇸", True, "Нью-Йоркская сессия - самая сильная", h
    else:
        return "ASIA 🌙", False, "Азиатская сессия / межсессия - флэт и ложные сигналы", h

def get_data():
    data = yf.download("GC=F", period="1d", interval="5m", auto_adjust=True, progress=False)
    if data.empty or len(data) < 50:
        data = yf.download("XAUUSD=X", period="1d", interval="5m", auto_adjust=True, progress=False)
    return data

def get_analysis(data):
    close = data['Close']
    if len(close.shape) > 1: close = close.iloc[:,0]
    ema_fast = close.ewm(span=21).mean()
    ema_slow = close.ewm(span=50).mean()
    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rsi = 100 - (100 / (1 + gain/loss))
    
    price = float(close.iloc[-1])
    ef = float(ema_fast.iloc[-1])
    es = float(ema_slow.iloc[-1])
    rsi_now = float(rsi.iloc[-1])

    if rsi_now < 30:
        side = "WAIT"; emoji = "⛔"; reason = f"Перепроданность RSI {rsi_now:.1f}"
        sl = price+25; tp = price-33
    elif rsi_now > 70:
        side = "WAIT"; emoji = "⛔"; reason = f"Перекупленность RSI {rsi_now:.1f}"
        sl = price-25; tp = price+33
    elif ef > es and rsi_now > 50 and price > ef:
        side = "BUY"; emoji = "🟢"; reason = "Тренд вверх"
        sl = price-25; tp = price+33
    elif ef < es and rsi_now < 50 and price < ef:
        side = "SELL"; emoji = "🔴"; reason = "Тренд вниз"
        sl = price+25; tp = price-33
    else:
        side = "WAIT"; emoji = "🟡"; reason = "Флэт - нет тренда"
        sl = price+25; tp = price-33

    conf = 50 + abs(rsi_now-50)*0.5
    rr = abs(tp-price)/abs(price-sl) if price!=sl else 0
    return price, ef, es, rsi_now, side, emoji, reason, sl, tp, conf, rr, close, ema_fast, ema_slow

def make_chart(close, ema_f, ema_s, price, sl, tp, side, session_name):
    plt.figure(figsize=(8,4))
    plt.plot(close[-100:], label='Цена XAU', linewidth=1.5, color='gold')
    plt.plot(ema_f[-100:], label='EMA 21', linewidth=1, color='blue', alpha=0.7)
    plt.plot(ema_s[-100:], label='EMA 50', linewidth=1, color='red', alpha=0.7)
    plt.axhline(price, color='black', linestyle='--', label=f'Вход {price:.1f}')
    plt.axhline(sl, color='red', linestyle=':', label=f'SL {sl:.1f}')
    plt.axhline(tp, color='green', linestyle=':', label=f'TP {tp:.1f}')
    plt.title(f'ЗОЛОТО XAU - {side} | {session_name} 📈', fontsize=11)
    plt.legend(fontsize=8)
    plt.grid(alpha=0.3)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150)
    buf.seek(0)
    plt.close()
    return buf

async def gold_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Анализирую золото...")
    try:
        data = get_data()
        if data.empty:
            await update.message.reply_text("❌ Нет данных по золоту")
            return
            
        session_name, is_active, session_desc, hour = get_session()
        price, ef, es, rsi_now, side, emoji, reason, sl, tp, conf, rr, close, ema_f, ema_s = get_analysis(data)
        chart = make_chart(close, ema_f, ema_s, price, sl, tp, side, session_name)

        # ГЛАВНЫЙ ФИЛЬТР СЕССИЙ
        if not is_active:
            text = (f"🌙 <b>СЕССИЯ: {session_name}</b>\n"
                    f"🕐 Время Актaу: {hour}:00\n"
                    f"📍 {session_desc}\n\n"
                    f"{emoji} <b>{side} - ЖДЕМ ⏸️</b>\n\n"
                    f"📍 Причина: {reason}\n"
                    f"💰 Цена: {price:.2f}$ | 📊 RSI: {rsi_now:.1f}\n\n"
                    f"⚠️ Сейчас {session_name} - торговать опасно!\n"
                    f"✅ Торгуем только:\n"
                    f"🇪🇺 Лондон 12:00-17:00\n"
                    f"🇺🇸 Нью-Йорк 17:00-02:00 по Актaу\n\n"
                    f"⏰ Приходи в Лондон!")
            await update.message.reply_photo(photo=chart, caption=text, parse_mode='HTML')
            return

        if side == "WAIT":
            text = (f"🕐 <b>СЕССИЯ: {session_name}</b> - АКТИВНА ✅\n"
                    f"📍 {session_desc}\n\n"
                    f"{emoji} <b>{side} - ЖДЕМ</b> ⏸️\n\n"
                    f"📍 Причина: {reason}\n"
                    f"💰 Цена: {price:.2f}$\n"
                    f"📊 RSI: {rsi_now:.1f}\n"
                    f"📈 EMA 21/50: {ef:.1f} / {es:.1f}")
        else:
            text = (f"🕐 <b>СЕССИЯ: {session_name}</b> - АКТИВНА ✅\n"
                    f"📍 {session_desc}\n\n"
                    f"{emoji} <b>СИГНАЛ {side} XAU</b> {emoji}\n\n"
                    f"💰 Цена: {price:.2f}$\n"
                    f"🎯 Вход: {price:.2f}\n"
                    f"🛑 SL: {sl:.2f}\n"
                    f"✅ TP: {tp:.2f}\n"
                    f"📐 RR 1:{rr:.2f} | 🔮 {conf:.0f}%\n"
                    f"📊 RSI: {rsi_now:.1f}\n"
                    f"💡 {reason}")

        await update.message.reply_photo(photo=chart, caption=text, parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, is_active, _, _ = get_session()
    status = "🟢 Торгуем" if is_active else "🌙 Отдыхаем"
    await update.message.reply_text(f"👋 Бот V7.6 🇷🇺\n🕐 Сейчас: {status}\n\n🇪🇺 Лондон 12:00-17:00\n🇺🇸 Нью-Йорк 17:00-02:00 (Актaу)\n\n📈 /gold - сигнал\n🚀 /start - меню")

def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot_app = ApplicationBuilder().token(TOKEN).build()
    bot_app.add_handler(CommandHandler("gold", gold_cmd))
    bot_app.add_handler(CommandHandler("start", start_cmd))
    print("V7.6 LONDON+NY READY")
    bot_app.run_polling(stop_signals=None)

if __name__ == "__main__":
    def run_flask():
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
    threading.Thread(target=run_flask, daemon=True).start()
    run_bot()
