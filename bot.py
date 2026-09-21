import os, asyncio, telegram, pytz, datetime, requests
from flask import Flask, request

TOKEN = os.environ.get("BOT_TOKEN")
bot = telegram.Bot(token=TOKEN)
app = Flask(__name__)
tz = pytz.timezone('Asia/Atyrau')

def get_candles():
    url = "https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=1h&limit=100"
    r = requests.get(url, timeout=10).json()
    closes = [float(x[4]) for x in r]
    highs = [float(x[2]) for x in r]
    lows = [float(x[3]) for x in r]
    return closes, highs, lows

def calc_rsi(prices, period=14):
    gains, losses = 0, 0
    for i in range(-period, 0):
        diff = prices[i] - prices[i-1]
        if diff > 0: gains += diff
        else: losses -= diff
    if losses == 0: return 70.0
    rs = (gains/period) / (losses/period)
    return 100 - (100 / (1 + rs))

def calc_ema(prices, period):
    return sum(prices[-period:]) / period

@app.route('/')
def home():
    return "GOLD v6.1 FIXED NO NUMPY"

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(force=True)
        update = telegram.Update.de_json(data, bot)
        if update.message and "gold" in (update.message.text or "").lower():
            now = datetime.datetime.now(tz)
            closes, highs, lows = get_candles()
            price = closes[-1]
            rsi = calc_rsi(closes)
            ema50 = calc_ema(closes, 50)
            support = min(lows[-20:])
            resistance = max(highs[-20:])

            if price > ema50 and rsi < 68:
                trend = f"LONG 📈 выше EMA50 {ema50:.1f}"
                action = "✅ BUY"
                entry = f"{price-8:.1f} - {price:.1f}"
                sl = f"{support:.1f}"
                tp1 = f"{price+15:.1f}"
                tp2 = f"{resistance:.1f}"
            elif price < ema50 and rsi > 32:
                trend = f"SHORT 📉 ниже EMA50 {ema50:.1f}"
                action = "❌ SELL"
                entry = f"{price:.1f} - {price+8:.1f}"
                sl = f"{resistance:.1f}"
                tp1 = f"{price-15:.1f}"
                tp2 = f"{support:.1f}"
            else:
                trend = "Флэт / Перекуп" if rsi>70 else "Флэт"
                action = "⏸ ЖДЕМ"
                entry = "Вне рынка"
                sl = "-"
                tp1 = "-"
                tp2 = "-"

            text = f"🔱 GOLD АНАЛИЗ v6.1\n⏰ {now.strftime('%d.%m %H:%M')} Атырау\n💰 ${price:.2f}\n\n📊 RSI: {rsi:.1f}\nEMA50: ${ema50:.1f}\nПоддержка: ${support:.1f} | Сопр: ${resistance:.1f}\nТренд: {trend}\n\n🎯 {action}\nВход: {entry}\nSL: {sl}\nTP1: {tp1}\nTP2: {tp2}\n\n/gold - обновить"
            asyncio.run(bot.send_message(chat_id=update.message.chat.id, text=text))
    except Exception as e:
        print("ERR:", e)
    return 'ok'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
