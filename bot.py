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
        return 4366.0

@app.route('/')
def home():
    return "GOLD v5 PRO FIXED"

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
                p = get_price()
                text = f"🔱 GOLD / XAUUSD PRO\n⏰ {now.strftime('%d.%m %H:%M')} Атырау\n💰 Цена: ${p:.2f}\n\n📈 Тренд: LONG выше 4340\n\n✅ BUY\nВход: {p-10:.1f} - {p:.1f}\nSL: {p-25:.1f}\nTP1: {p+15:.1f}\nTP2: {p+32:.1f}\n\n❌ SELL если ниже {p-20:.1f}\nSL: {p-7:.1f}\nTP: {p-40:.1f}\n\n/gold - обновить"
                asyncio.run(bot.send_message(chat_id=chat_id, text=text))
    except Exception as e:
        print(e)
    return 'ok'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
