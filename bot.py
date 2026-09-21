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
         
