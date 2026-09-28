import os
import time
import threading

from flask import Flask
import telebot

BOT_TOKEN = os.environ.get("BOT_TOKEN")

app = Flask(__name__)

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode="HTML"
)


# =========================
# WEB
# =========================

@app.route("/")
def home():
    return "GOLD SMART BOT POLLING ONLINE", 200


@app.route("/health")
def health():
    return "OK", 200


# =========================
# TELEGRAM COMMANDS
# =========================

@bot.message_handler(commands=["start"])
def start(message):

    print("🔥 /start RECEIVED")

    bot.send_message(
        message.chat.id,
        "🟢 <b>GOLD SMART BOT</b>\n\n"
        "Telegram → Render → Bot работает!\n\n"
        "🥇 Напиши /gold"
    )


@bot.message_handler(commands=["gold"])
def gold(message):

    print("🥇 /gold RECEIVED")

    bot.send_message(
        message.chat.id,
        "🥇 <b>GOLD TEST</b>\n\n"
        "Связь работает отлично.\n\n"
        "Следующий этап — подключаем анализ XAUUSD."
    )


@bot.message_handler(
    func=lambda message: (
        message.text
        and message.text.lower().strip() in ["gold", "золото"]
    )
)
def gold_text(message):

    print("🥇 GOLD TEXT RECEIVED")

    bot.send_message(
        message.chat.id,
        "🥇 GOLD TEST OK"
    )


# =========================
# POLLING
# =========================

def polling_worker():

    print("================================")
    print("🟢 GOLD SMART BOT POLLING")
    print("================================")

    try:

        # Удаляем webhook
        bot.remove_webhook()

        time.sleep(2)

        print("✅ Webhook removed")
        print("🚀 Starting polling...")

        bot.infinity_polling(
            timeout=30,
            long_polling_timeout=30,
            skip_pending=False,
            allowed_updates=["message"]
        )

    except Exception as e:

        print("❌ POLLING ERROR:")
        print(repr(e))

        time.sleep(5)

        # Повторный запуск
        polling_worker()


# =========================
# START POLLING THREAD
# =========================

threading.Thread(
    target=polling_worker,
    daemon=True
).start()


# =========================
# LOCAL
# =========================

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )
