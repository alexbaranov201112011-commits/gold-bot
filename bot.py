import os, io, telebot
import matplotlib.pyplot as plt
import random
from datetime import datetime

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = telebot.TeleBot(BOT_TOKEN)

def calc_confidence(rsi, ema, volume, session):
    # rsi 0-100, ema 0/1, volume 0-100, session London/NY = 1, Asia = 0.3
    score = (rsi*0.3 + ema*25 + volume*0.2 + session*25)
    return min(95, max(40, int(score)))

def get_risk(conf):
    if conf < 65: return 0
    if conf < 80: return 1.0
    return 2.0

@bot.message_handler(commands=['gold','start'])
def gold_signal(message):
    # ЗАГЛУШКА - сюда подключишь свои реальные индикаторы
    # Сейчас рандом для теста математики
    rsi_power = random.randint(60,90)
    ema_trend = 1 # 1 = тренд есть
    volume = random.randint(60,95)
    session = 1 if 8 <= datetime.utcnow().hour <= 19 else 0.4

    conf = calc_confidence(rsi_power, ema_trend, volume, session)
    risk = get_risk(conf)

    if risk == 0:
        bot.send_message(message.chat.id, f"⚠️ Слабый сигнал (уверенность {conf}%). Пропускаем по математике.")
        return

    # Сигнал
    direction = random.choice(["BUY","SELL"])
    entry = 2635.50 + random.uniform(-5,5)
    tp = entry + 12 if direction=="BUY" else entry - 12
    sl = entry - 7 if direction=="BUY" else entry + 7
    
    depo = 1000
    profit_usd = depo * (risk/100) * 1.8
    
    # График
    fig, ax = plt.subplots(figsize=(5,3))
    ax.plot([entry-10, entry, tp], [0,1,2], marker='o')
    ax.set_title(f'XAU {direction} | Conf {conf}% | Risk {risk}%')
    ax.grid(alpha=0.3)
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=150)
    buf.seek(0)
    plt.close()

    text = f"""📊 GOLD V2 - УМНЫЙ СИГНАЛ

Направление: {direction} XAUUSD
Вход: {entry:.2f}
TP: {tp:.2f} | SL: {sl:.2f}

🧠 Уверенность: {conf}%
📈 Риск: {risk}% от депо
💰 Потенциал: +${profit_usd:.2f} | RR 1:1.8

Математика: EV = +{(conf/100*1.8 - (1-conf/100)):.2f}R - ПОЛОЖИТЕЛЬНО
"""
    bot.send_photo(message.chat.id, buf, caption=text)

bot.infinity_polling()
