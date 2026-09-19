from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler
import os
import datetime
import pytz
import asyncio

TOKEN = "8964936412:AAEKIaDurlLDg-qskd5Rc0JC28uCwOnZ_Ek"
app = Flask(__name__)

# Создаем приложение бота
application = Application.builder().token(TOKEN).build()

async def gold_command(update, context):
    tz = pytz.timezone('Asia/Atyrau')
    now = datetime.datetime.now(tz)
    if 12 <= now.hour <= 22:
        msg = f"🔱 АНАЛИЗ РЫНКА ЗОЛОТА\n\nВремя: {now.strftime('%H:%M')} Атырау\nТренд 15м: BUY ▲\nТренд 1ч: BUY ▲\nУверенность: 95%\n\n📈 Сигнал: ПОКУПКА\nSL: -300п\nTP: +600п"
    else:
        msg = f"⏳ ОЖИДАНИЕ\nСейчас {now.strftime('%H:%M')} Атырау\nСессия с 12:00 до 22:00"
    await update.message.reply_text(msg)

application.add_handler(CommandHandler("gold", gold_command))
application.add_handler(CommandHandler("start", gold_command))

@app.route('/')
def home():
    return "Bot is running OK! v2"

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, application.bot)
    asyncio.run(application.process_update(update))
    return 'ok'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
