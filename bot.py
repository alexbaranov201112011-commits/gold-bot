import os, asyncio, telegram, pytz, datetime, requests
from flask import Flask, request

TOKEN = os.environ.get("BOT_TOKEN")
bot = telegram.Bot(token=TOKEN)
app = Flask(__name__)
tz = pytz.timezone('Asia/Atyrau')

def get_price():
    try:
        r = requests.get("https://api.gold-api.com/price/XAU", timeout=5).json()
        return float(r.get('price', 0))
    except:
        return 0

@app.route('/')
def home(): return "Bot GOLD FINAL v4 OK"

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(force=True)
        update = telegram.Update.de_json(data, bot)
        if update.message:
            chat_id = update.message.chat.id
            txt = (update.message.text or "").lower()
            if "gold" in txt or "start" in txt:
                now = datetime.datetime.now(tz)
                price = get_price()
                
                if 12 <= now.hour <= 22:
                    if price > 2660:
                        signal = "🟢 BUY / ЛОНГ\nЦена пробила сопротивление. Ищем вход на откате."
                    elif price < 2640:
                        signal = "🔴 SELL / ШОРТ\nЦена под давлением. Продажи приоритет."
                    else:
                        signal = "🟡 БОКОВИК / ЖДЕМ\nЦена в диапазоне 2640-2660. Без входа, ждем пробой."
                    
                    msg = f"""🔱 АНАЛИЗ ЗОЛОТА v4
⏰ {now.strftime('%d.%m %H:%M')} Атырау
💰 Цена: ${price:.2f}

{signal}

🎯 Уровни:
Buy: выше 2660
Sell: ниже 2640
Стоп: 15$
Тейк: 30$ / 60$

_Только по команде /gold_"""
                else:
                    msg = f"⌛ Вне сессии\nСейчас {now.strftime('%H:%M')}. Торгуем 12:00-22:00 по Атырау. Цена сейчас ${price:.2f}"
                
                asyncio.run(bot.send_message(chat_id=chat_id, text=msg))
    except Exception as e:
        print(f"ERROR: {e}")
    return 'ok'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
