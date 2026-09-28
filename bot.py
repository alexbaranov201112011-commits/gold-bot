import os
import time

from flask import Flask, request
import telebot


# =====================================================
# SETTINGS
# =====================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv(
    "WEBHOOK_SECRET",
    "GoldSmartV33_2026"
)
RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL"
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")


# =====================================================
# FLASK + TELEGRAM
# =====================================================

app = Flask(__name__)

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode="HTML"
)


# =====================================================
# HOME
# =====================================================

@app.route("/", methods=["GET", "HEAD"])
def home():
    return "GOLD SMART BOT ONLINE", 200


@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


# =====================================================
# TELEGRAM WEBHOOK
# =====================================================

@app.route("/telegram", methods=["POST"])
def telegram_webhook():

    secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )

    if secret != WEBHOOK_SECRET:
        print("❌ Wrong webhook secret")
        return "Forbidden", 403

    try:

        data = request.get_data(
            as_text=True
        )

        print(
            "📩 TELEGRAM UPDATE:",
            data[:500]
        )

        update = telebot.types.Update.de_json(
            data
        )

        bot.process_new_updates(
            [update]
        )

        return "OK", 200

    except Exception as e:

        print(
            "❌ TELEGRAM ERROR:",
            repr(e)
        )

        return "ERROR", 500


# =====================================================
# /START
# =====================================================

@bot.message_handler(
    commands=["start"]
)
def start_command(message):

    print(
        "✅ /start received from:",
        message.chat.id
    )

    bot.send_message(

        message.chat.id,

        "🟡 <b>GOLD SMART BOT</b>\n\n"
        "🟢 Бот работает!\n\n"
        "Telegram → Render → Bot соединение "
        "успешно установлено ✅"
    )


# =====================================================
# GOLD TEST
# =====================================================

@bot.message_handler(
    commands=["gold"]
)
def gold_command(message):

    bot.send_message(

        message.chat.id,

        "🟡 <b>GOLD SMART</b>\n\n"
        "Связь с Telegram работает ✅\n\n"
        "Модуль анализа XAU/USD подключим "
        "следующим этапом."
    )


# =====================================================
# TEXT
# =====================================================

@bot.message_handler(
    func=lambda message:
        message.text is not None
        and message.text.lower().strip()
        in ["gold", "золото"]
)
def text_gold(message):

    gold_command(message)


# =====================================================
# WEBHOOK SETUP
# =====================================================

def setup_webhook():

    if not RENDER_EXTERNAL_URL:

        print(
            "❌ RENDER_EXTERNAL_URL is missing"
        )

        return

    webhook_url = (
        RENDER_EXTERNAL_URL.rstrip("/")
        + "/telegram"
    )

    print(
        "================================"
    )

    print(
        "🟡 GOLD SMART BOT STARTING"
    )

    print(
        "Webhook:",
        webhook_url
    )

    try:

        # Remove old webhook
        bot.remove_webhook()

        time.sleep(1)

        # Set new webhook
        result = bot.set_webhook(

            url=webhook_url,

            secret_token=WEBHOOK_SECRET,

            drop_pending_updates=True
        )

        print(
            "Webhook set:",
            result
        )

        # Telegram diagnostics
        me = bot.get_me()

        print(
            "BOT:",
            me.username
        )

        print(
            "BOT ID:",
            me.id
        )

        info = bot.get_webhook_info()

        print(
            "WEBHOOK URL:",
            info.url
        )

        print(
            "PENDING:",
            info.pending_update_count
        )

        print(
            "LAST ERROR:",
            info.last_error_message
        )

        print(
            "================================"
        )

    except Exception as e:

        print(
            "❌ WEBHOOK ERROR:",
            repr(e)
        )


# =====================================================
# IMPORTANT
# =====================================================

setup_webhook()


# =====================================================
# LOCAL
# =====================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
