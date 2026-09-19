from flask import Flask, request
import telegram
import os
import datetime
import pytz
TOKEN="8964936412:AAEKIaDurlLDg-qskd5Rc0JC28uCwOnZ_Ek"
bot=telegram.Bot(token=TOKEN)
app=Flask(__name__)
@app.route('/')
def home():
    return "Bot is running OK! v2 - FIXED"
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data=request.get_json(force=True)
        update=telegram.Update.de_json(data, bot)
        if update.message:
            chat_id=update.message.chat.id
            txt=update.message.text or ""
            if "/gold" in txt or "/start" in txt:
                tz=pytz.timezone('Asia/Atyrau')
                now=datetime.datetime.now(tz)
                if 12 <= now.hour <= 22:
                    msg=f"🔱 АНАЛИЗ ЗОЛОТА v2\nВремя: {now.strftime('%H:%M')} Атырау\nBUY\nСигнал: ПОКУПКА"
                else:
                    msg=f"⏳ ОЖИДАНИЕ\nСейчас {now.strftime('%H:%M')} Атырау\nСессия 12:00-22:00"
                bot.send_message(chat_id=chat_id, text=msg)
    except Exception as e:
        print(e)
    return 'ok'
if __name__=='__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
