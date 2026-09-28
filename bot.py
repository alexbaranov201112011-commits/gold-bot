import os
import json
import requests

from flask import Flask, request
import telebot

BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
RENDER_EXTERNAL_URL = os.environ.get(
    "RENDER_EXTERNAL_URL",
    "https://gold-bot-q8la.onrender.com"
).rstrip("/")

WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}/telegram"

app = Flask(__name__)

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode="HTML"
)


# =========================
# BASIC ROUTES
# =========================

@app.route("/")
def home():
    return "GOLD SMART BOT ONLINE", 200


@app.route("/health")
def health():
    return "OK", 200


# =========================
# TELEGRAM WEBHOOK
# =========================

@app.route("/telegram", methods=["POST"])
def telegram_webhook():

    print("")
    print("================================")
    print("📩 TELEGRAM REQUEST RECEIVED")
    print("================================")

    # Показываем, пришёл ли запрос вообще
    print("METHOD:", request.method)
    print("CONTENT TYPE:", request.content_type)
    print("SECRET HEADER:",
          request.headers.get("X-Telegram-Bot-Api-Secret-Token"))

    # Проверяем secret, если он установлен
    if WEBHOOK_SECRET:
        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token"
        )

        if received_secret != WEBHOOK_SECRET:
            print("❌ WRONG WEBHOOK SECRET")
            print("================================")
            return "FORBIDDEN", 403

    try:
        data = request.get_json(force=True)

        print("📦 UPDATE:")
        print(json.dumps(data, ensure_ascii=False))

        update = telebot.types.Update.de_json(
            json.dumps(data)
        )

        bot.process_new_updates([update])

        print("✅ UPDATE PROCESSED")
        print("================================")

        return "OK", 200

    except Exception as e:

        print("❌ WEBHOOK ERROR:")
        print(repr(e))
        print("================================")

        return "ERROR", 500


# =========================
# TELEGRAM COMMANDS
# =========================

@bot.message_handler(commands=["start"])
def start(message):

    print("🔥 /start RECEIVED")
    print("CHAT ID:", message.chat.id)
    print("USER:", message.from_user.username)

    bot.send_message(
        message.chat.id,
        "🟢 <b>GOLD SMART BOT</b>\n\n"
        "Telegram → Render → Bot работает.\n\n"
        "Команда /gold доступна для теста."
    )


@bot.message_handler(commands=["gold"])
def gold(message):

    print("🥇 /gold RECEIVED")

    bot.send_message(
        message.chat.id,
        "🥇 <b>GOLD TEST</b>\n\n"
        "Связь с Telegram работает.\n"
        "Следующим этапом подключим анализ XAUUSD."
    )


@bot.message_handler(
    func=lambda message: message.text
    and message.text.lower().strip() in ["gold", "золото"]
)
def gold_text(message):

    print("🥇 GOLD TEXT RECEIVED")

    bot.send_message(
        message.chat.id,
        "🥇 GOLD TEST OK\n\n"
        "Напиши /gold для проверки."
    )


# =========================
# WEBHOOK SETUP
# =========================

def setup_webhook():

    print("")
    print("================================")
    print("🟡 GOLD SMART BOT DIAGNOSTIC")
    print("================================")

    try:

        # Удаляем старый webhook
        delete_url = (
            f"https://api.telegram.org/bot"
            f"{BOT_TOKEN}/deleteWebhook"
        )

        delete_response = requests.post(
            delete_url,
            timeout=20
        )

        print("DELETE WEBHOOK:")
        print(delete_response.text)

        # Устанавливаем новый
        set_url = (
            f"https://api.telegram.org/bot"
            f"{BOT_TOKEN}/setWebhook"
        )

        payload = {
            "url": WEBHOOK_URL,
            "drop_pending_updates": False
        }

        if WEBHOOK_SECRET:
            payload["secret_token"] = WEBHOOK_SECRET

        set_response = requests.post(
            set_url,
            json=payload,
            timeout=20
        )

        print("SET WEBHOOK:")
        print(set_response.text)

        # Проверяем состояние
        info_url = (
            f"https://api.telegram.org/bot"
            f"{BOT_TOKEN}/getWebhookInfo"
        )

        info_response = requests.get(
            info_url,
            timeout=20
        )

        print("WEBHOOK INFO:")
        print(info_response.text)

        # Проверяем самого бота
        me_url = (
            f"https://api.telegram.org/bot"
            f"{BOT_TOKEN}/getMe"
        )

        me_response = requests.get(
            me_url,
            timeout=20
        )

        print("BOT INFO:")
        print(me_response.text)

        print("================================")
        print("WEBHOOK:", WEBHOOK_URL)
        print("================================")

    except Exception as e:

        print("❌ SETUP ERROR:")
        print(repr(e))


# ВАЖНО:
# выполняется при запуске Gunicorn
setup_webhook()


# =========================
# LOCAL RUN
# =========================

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )
