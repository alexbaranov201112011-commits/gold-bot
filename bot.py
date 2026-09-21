import os, asyncio, telegram, pytz, datetime, requests
import numpy as np
from flask import Flask, request

TOKEN = os.environ.get("BOT_TOKEN")
bot = telegram.Bot(token=TOKEN)
app = Flask(__name__)
tz = pytz.timezone('Asia/Atyrau')

def get_candles():
    # Берем 100 часовых свечей PAXG (золото)
    url = "https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=1h&limit=100"
    r = requests.get(url, timeout=10).json()
    closes = [float(x[4]) for x in r]
    highs = [float(x[2]) for x in r]
    lows = [float(x[3]) for x in r]
    return closes, highs, lows

def calc_rsi(prices, period=14):
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0: return 70
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_ema(prices, period):
    return float(np.mean(prices[-period:]))

@app.route('/')
def home(): return "GOLD v6 ANALYZER LIVE"

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(force=True)
        update = telegram.Update.de_json(data, bot)
        if update.message:
            chat_id = update.message.chat.id
            txt = (update.message.text or "").lower()
            if "gold" in txt:
                now = datetime.datetime.now(tz)
                closes, highs, lows = get_candles()
                price = closes[-1]
                rsi = calc_rsi(closes)
                ema50 = calc_ema(closes, 50)
                ema200 = calc_ema(closes, 20)
                support = min(lows[-20:])
                resistance = max(highs[-20:])

                # Логика анализа
                if price > ema50 and rsi < 70:
                    trend = "LONG 📈 (цена выше EMA50, есть сила)"
                    action = "✅ BUY"
                    entry = f"{price-8:.1f} - {price:.1f}"
                    sl = f"{support:.1f} (поддержка)"
                    tp1 = f"{price+18:.1f}"
                    tp2 = f"{resistance:.1f} (сопротивление)"
                elif price < ema50 and rsi > 30:
                    trend = "SHORT 📉 (цена ниже EMA50)"
                    action = "❌ SELL"
                    entry = f"{price:.1f} - {price+8:.1f}"
                    sl = f"{resistance:.1f}"
                    tp1 = f"{price-18:.1f}"
                    tp2 = f"{support:.1f}"
                else:
                    trend = "Флэт / Перекуп" if rsi>70 else "Флэт"
                    action = "⏸ ЖДЕМ"
                    entry = "Вне рынка"
                    sl = "-"
                    tp1 = "-"
                    tp2 = "-"

                text = f"""🔱 GOLD YouTrade - АНАЛИЗ v6
⏰ {now.strftime('%d.%m %H:%M')} Атырау
💰 Цена: ${price:.2f}

📊 ТЕХАНАЛИЗ:
RSI (14): {rsi:.1f} {'🔥 перекуп' if rsi>70 else '🧊 перепродан' if rsi<30 else 'норм'}
EMA50: ${ema50:.1f} | EMA20: ${ema200:.1f}
Поддержка: ${support:.1f} | Сопротивление: ${resistance:.1f}
Тренд: {trend}

🎯 СИГНАЛ: {action}
Вход: {entry}
SL: {sl}
TP1: {tp1}
TP2: {tp2}

⚠️ Риск 1%. Анализ по 1H.
 /gold - обновить анализ"""

                asyncio.run(bot.send_message(chat_id=chat_id, text=text))
    except Exception as e:
        print("ERR:", e)
        try:
            asyncio.run(bot.send_message(chat_id=chat_id, text=f"Ошибка анализа: {e}"))
        except: pass
    return 'ok'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
