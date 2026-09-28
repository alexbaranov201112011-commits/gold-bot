import os
import time
import threading
from datetime import datetime, timedelta, timezone

import requests
import pandas as pd
import numpy as np
from flask import Flask, request, jsonify

import telebot


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "GoldSmartV33_2026")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)


# =========================================================
# BASIC WEB ROUTES
# =========================================================

@app.route("/", methods=["GET", "HEAD"])
def home():
    return "GOLD SMART V3.3 ONLINE", 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "bot": "GOLD SMART V3.3",
        "time": datetime.now(timezone.utc).isoformat()
    }), 200


# =========================================================
# WEBHOOK
# =========================================================

WEBHOOK_READY = False


def setup_webhook():
    global WEBHOOK_READY

    if not RENDER_EXTERNAL_URL:
        print("ERROR: RENDER_EXTERNAL_URL is missing")
        return

    webhook_url = (
        RENDER_EXTERNAL_URL.rstrip("/") +
        "/telegram"
    )

    try:
        print("Removing old webhook...")

        bot.remove_webhook()

        time.sleep(1)

        print("Setting webhook:", webhook_url)

        result = bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True
        )

        print("Webhook result:", result)

        WEBHOOK_READY = bool(result)

        print("WEBHOOK_READY:", WEBHOOK_READY)

    except Exception as e:
        print("Webhook setup error:", repr(e))


@app.route("/telegram", methods=["POST"])
def telegram_webhook():

    # Check Telegram secret header
    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    )

    if WEBHOOK_SECRET and received_secret != WEBHOOK_SECRET:
        print("Invalid webhook secret")
        return "Forbidden", 403

    try:
        json_string = request.get_data().decode("utf-8")

        update = telebot.types.Update.de_json(json_string)

        bot.process_new_updates([update])

        return "OK", 200

    except Exception as e:
        print("Telegram webhook error:", repr(e))
        return "ERROR", 500


# =========================================================
# GOLD DATA
# =========================================================

SPOT_URL = "https://xaus.com/api/v1/spot"
INTRADAY_URL = "https://xaus.com/api/v1/intraday"


def get_spot_price():
    try:
        r = requests.get(
            SPOT_URL,
            timeout=15
        )

        r.raise_for_status()

        data = r.json()

        # Try common response formats
        if isinstance(data, dict):

            for key in [
                "price",
                "spot",
                "xauusd",
                "XAUUSD",
                "last"
            ]:
                if key in data:
                    return float(data[key])

            if "data" in data and isinstance(data["data"], dict):
                for key in [
                    "price",
                    "spot",
                    "xauusd",
                    "XAUUSD",
                    "last"
                ]:
                    if key in data["data"]:
                        return float(data["data"][key])

        return None

    except Exception as e:
        print("Spot error:", repr(e))
        return None


def get_intraday():

    try:

        r = requests.get(
            INTRADAY_URL,
            timeout=20
        )

        r.raise_for_status()

        data = r.json()

        if isinstance(data, dict):

            if "data" in data:
                data = data["data"]

            elif "prices" in data:
                data = data["prices"]

            elif "result" in data:
                data = data["result"]

        if not isinstance(data, list):
            print("Unexpected intraday format")
            return pd.DataFrame()

        rows = []

        for item in data:

            if not isinstance(item, dict):
                continue

            timestamp = (
                item.get("timestamp")
                or item.get("time")
                or item.get("date")
            )

            price = (
                item.get("price")
                or item.get("close")
                or item.get("last")
            )

            if timestamp is None or price is None:
                continue

            try:

                ts = pd.to_datetime(
                    timestamp,
                    utc=True
                )

                rows.append({
                    "time": ts,
                    "close": float(price)
                })

            except Exception:
                continue

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)

        df = (
            df
            .drop_duplicates("time")
            .sort_values("time")
            .set_index("time")
        )

        return df

    except Exception as e:

        print("Intraday error:", repr(e))

        return pd.DataFrame()


# =========================================================
# OHLC
# =========================================================

def make_ohlc(df, timeframe):

    if df.empty:
        return pd.DataFrame()

    ohlc = df["close"].resample(timeframe).ohlc()

    ohlc = ohlc.dropna()

    return ohlc


# =========================================================
# INDICATORS
# =========================================================

def ema(series, period):

    return series.ewm(
        span=period,
        adjust=False
    ).mean()


def rsi(series, period=14):

    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    result = 100 - (100 / (1 + rs))

    return result.fillna(50)


def atr(df, period=14):

    high = df["high"]
    low = df["low"]
    close = df["close"]

    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ],
        axis=1
    ).max(axis=1)

    return tr.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()


# =========================================================
# TREND
# =========================================================

def get_trend(df):

    if df.empty or len(df) < 30:
        return "NEUTRAL"

    close = df["close"]

    fast_period = min(50, max(10, len(df) // 3))
    slow_period = min(150, max(20, len(df) // 2))

    fast = ema(close, fast_period)
    slow = ema(close, slow_period)

    last = close.iloc[-1]

    if last > fast.iloc[-1] and fast.iloc[-1] > slow.iloc[-1]:
        return "BULLISH"

    if last < fast.iloc[-1] and fast.iloc[-1] < slow.iloc[-1]:
        return "BEARISH"

    return "NEUTRAL"


# =========================================================
# MARKET STRUCTURE
# =========================================================

def detect_bos(df):

    if len(df) < 10:
        return "NONE"

    recent = df.tail(10)

    previous_high = recent["high"].iloc[:-2].max()
    previous_low = recent["low"].iloc[:-2].min()

    last_close = recent["close"].iloc[-1]

    if last_close > previous_high:
        return "BULLISH BOS"

    if last_close < previous_low:
        return "BEARISH BOS"

    return "NONE"


def detect_liquidity_sweep(df):

    if len(df) < 12:
        return "NONE"

    recent = df.tail(12)

    previous_high = recent["high"].iloc[:-2].max()
    previous_low = recent["low"].iloc[:-2].min()

    last = recent.iloc[-1]

    # Sweep above liquidity then close back below
    if (
        last["high"] > previous_high
        and last["close"] < previous_high
    ):
        return "SELL-SIDE REVERSAL"

    # Sweep below liquidity then close back above
    if (
        last["low"] < previous_low
        and last["close"] > previous_low
    ):
        return "BUY-SIDE REVERSAL"

    return "NONE"


def detect_fvg(df):

    if len(df) < 5:
        return "NONE"

    a = df.iloc[-3]
    c = df.iloc[-1]

    # Bullish FVG
    if c["low"] > a["high"]:
        return "BULLISH FVG"

    # Bearish FVG
    if c["high"] < a["low"]:
        return "BEARISH FVG"

    return "NONE"


# =========================================================
# ASIAN RANGE
# =========================================================

def get_asian_range(df):

    if df.empty:
        return None, None

    local = df.copy()

    local["local"] = local.index.tz_convert(
        "Asia/Aqtau"
    )

    # FIXED:
    # .dt.hour instead of .hour
    local["hour"] = local["local"].dt.hour

    # Asian session: 00:00 - 08:00 Aktau
    asia = local[
        (local["hour"] >= 0) &
        (local["hour"] < 8)
    ]

    if asia.empty:
        return None, None

    return (
        float(asia["high"].max()),
        float(asia["low"].min())
    )


# =========================================================
# PREVIOUS DAY LEVELS
# =========================================================

def get_previous_day_levels(df):

    if df.empty:
        return None, None

    local = df.copy()

    local["local"] = local.index.tz_convert(
        "Asia/Aqtau"
    )

    local["date"] = local["local"].dt.date

    today = local["date"].iloc[-1]

    previous_days = local[
        local["date"] < today
    ]

    if previous_days.empty:
        return None, None

    previous_date = previous_days["date"].iloc[-1]

    day = previous_days[
        previous_days["date"] == previous_date
    ]

    return (
        float(day["high"].max()),
        float(day["low"].min())
    )


# =========================================================
# SIGNAL ENGINE
# =========================================================

def build_signal():

    raw = get_intraday()

    if raw.empty:
        return {
            "status": "ERROR",
            "message": "Не удалось получить данные XAU/USD."
        }

    spot = get_spot_price()

    if spot is None:
        spot = float(raw["close"].iloc[-1])

    m15 = make_ohlc(raw, "15min")
    h1 = make_ohlc(raw, "1h")
    h4 = make_ohlc(raw, "4h")

    if m15.empty:
        return {
            "status": "ERROR",
            "message": "Недостаточно данных M15."
        }

    # Indicators
    m15_rsi = rsi(m15["close"]).iloc[-1]
    m15_atr = atr(m15).iloc[-1]

    trend_h4 = get_trend(h4)
    trend_h1 = get_trend(h1)
    trend_m15 = get_trend(m15)

    bos = detect_bos(m15)
    sweep = detect_liquidity_sweep(m15)
    fvg = detect_fvg(m15)

    asia_high, asia_low = get_asian_range(raw)

    pdh, pdl = get_previous_day_levels(raw)

    # =====================================================
    # SCORE
    # =====================================================

    buy_score = 0
    sell_score = 0

    reasons_buy = []
    reasons_sell = []

    # H4
    if trend_h4 == "BULLISH":
        buy_score += 2
        reasons_buy.append("H4 bullish")

    elif trend_h4 == "BEARISH":
        sell_score += 2
        reasons_sell.append("H4 bearish")

    # H1
    if trend_h1 == "BULLISH":
        buy_score += 2
        reasons_buy.append("H1 bullish")

    elif trend_h1 == "BEARISH":
        sell_score += 2
        reasons_sell.append("H1 bearish")

    # M15
    if trend_m15 == "BULLISH":
        buy_score += 1
        reasons_buy.append("M15 bullish")

    elif trend_m15 == "BEARISH":
        sell_score += 1
        reasons_sell.append("M15 bearish")

    # BOS
    if bos == "BULLISH BOS":
        buy_score += 2
        reasons_buy.append("Bullish BOS")

    elif bos == "BEARISH BOS":
        sell_score += 2
        reasons_sell.append("Bearish BOS")

    # Liquidity
    if sweep == "BUY-SIDE REVERSAL":
        buy_score += 2
        reasons_buy.append("Liquidity sweep")

    elif sweep == "SELL-SIDE REVERSAL":
        sell_score += 2
        reasons_sell.append("Liquidity sweep")

    # FVG
    if fvg == "BULLISH FVG":
        buy_score += 1
        reasons_buy.append("Bullish FVG")

    elif fvg == "BEARISH FVG":
        sell_score += 1
        reasons_sell.append("Bearish FVG")

    # RSI filter
    if m15_rsi < 35:
        buy_score += 1
        reasons_buy.append("RSI oversold")

    if m15_rsi > 65:
        sell_score += 1
        reasons_sell.append("RSI overbought")

    # =====================================================
    # DECISION
    # =====================================================

    difference = abs(
        buy_score - sell_score
    )

    if (
        buy_score >= 6
        and buy_score > sell_score
        and difference >= 2
    ):
        direction = "BUY"

    elif (
        sell_score >= 6
        and sell_score > buy_score
        and difference >= 2
    ):
        direction = "SELL"

    else:
        direction = "WAIT"

    # =====================================================
    # LEVELS
    # =====================================================

    atr_value = float(m15_atr)

    # Conservative gold stop
    stop_distance = max(
        atr_value * 1.2,
        3.0
    )

    if direction == "BUY":

        entry = spot

        sl = entry - stop_distance

        tp1 = entry + stop_distance * 1.5
        tp2 = entry + stop_distance * 2.0
        tp3 = entry + stop_distance * 3.0

    elif direction == "SELL":

        entry = spot

        sl = entry + stop_distance

        tp1 = entry - stop_distance * 1.5
        tp2 = entry - stop_distance * 2.0
        tp3 = entry - stop_distance * 3.0

    else:

        entry = spot
        sl = None
        tp1 = None
        tp2 = None
        tp3 = None

    return {
        "status": "OK",
        "price": spot,
        "direction": direction,

        "buy_score": buy_score,
        "sell_score": sell_score,

        "h4": trend_h4,
        "h1": trend_h1,
        "m15": trend_m15,

        "bos": bos,
        "sweep": sweep,
        "fvg": fvg,

        "rsi": float(m15_rsi),
        "atr": atr_value,

        "asia_high": asia_high,
        "asia_low": asia_low,

        "pdh": pdh,
        "pdl": pdl,

        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,

        "reasons_buy": reasons_buy,
        "reasons_sell": reasons_sell
    }


# =========================================================
# TELEGRAM FORMAT
# =========================================================

def fmt(value):

    if value is None:
        return "—"

    return f"{value:.2f}"


def make_message():

    result = build_signal()

    if result["status"] != "OK":

        return (
            "🟡 <b>GOLD SMART V3.3</b>\n\n"
            f"❌ {result['message']}"
        )

    direction = result["direction"]

    if direction == "BUY":
        header = "🟢 <b>BUY</b>"

    elif direction == "SELL":
        header = "🔴 <b>SELL</b>"

    else:
        header = "🟡 <b>WAIT / NO TRADE</b>"

    text = (
        "🟡 <b>GOLD SMART V3.3</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{header}\n\n"

        f"💰 XAU/USD: <b>{fmt(result['price'])}</b>\n\n"

        "📊 <b>TREND</b>\n"
        f"H4: {result['h4']}\n"
        f"H1: {result['h1']}\n"
        f"M15: {result['m15']}\n\n"

        "🧠 <b>STRUCTURE</b>\n"
        f"BOS: {result['bos']}\n"
        f"Liquidity: {result['sweep']}\n"
        f"FVG: {result['fvg']}\n\n"

        f"RSI M15: {result['rsi']:.1f}\n"
        f"ATR M15: {result['atr']:.2f}\n\n"

        "🎯 <b>LEVELS</b>\n"
        f"Entry: <b>{fmt(result['entry'])}</b>\n"
        f"SL: <b>{fmt(result['sl'])}</b>\n"
        f"TP1: <b>{fmt(result['tp1'])}</b>\n"
        f"TP2: <b>{fmt(result['tp2'])}</b>\n"
        f"TP3: <b>{fmt(result['tp3'])}</b>\n\n"

        "🏦 <b>LIQUIDITY LEVELS</b>\n"
        f"Asian High: {fmt(result['asia_high'])}\n"
        f"Asian Low: {fmt(result['asia_low'])}\n"
        f"PDH: {fmt(result['pdh'])}\n"
        f"PDL: {fmt(result['pdl'])}\n\n"

        "📈 <b>SCORE</b>\n"
        f"BUY: {result['buy_score']}/12\n"
        f"SELL: {result['sell_score']}/12\n\n"
    )

    if direction == "BUY":

        text += (
            "✅ <b>BUY подтверждён системой.</b>\n"
            "Ждать подтверждение цены перед входом.\n"
        )

    elif direction == "SELL":

        text += (
            "✅ <b>SELL подтверждён системой.</b>\n"
            "Ждать подтверждение цены перед входом.\n"
        )

    else:

        text += (
            "⏸ <b>Сделку сейчас не открывать.</b>\n"
            "Система не видит достаточного преимущества.\n"
        )

    text += (
        "\n⚠️ Данные являются ориентиром XAU/USD и могут "
        "отличаться от котировки FxPro.\n"
        "Бот не открывает сделки автоматически."
    )

    return text


# =========================================================
# COMMANDS
# =========================================================

@bot.message_handler(commands=["start"])
def start_command(message):

    bot.reply_to(
        message,
        "🟡 <b>GOLD SMART V3.3</b>\n\n"
        "Бот онлайн ✅\n\n"
        "Команды:\n"
        "/gold — полный анализ золота\n"
        "/quick — быстрый сигнал\n"
        "/levels — уровни\n"
        "/status — статус бота\n\n"
        "Также можно написать: <b>gold</b> или <b>золото</b>"
    )


@bot.message_handler(commands=["gold"])
def gold_command(message):

    bot.send_chat_action(
        message.chat.id,
        "typing"
    )

    try:

        text = make_message()

        bot.send_message(
            message.chat.id,
            text
        )

    except Exception as e:

        print("Gold command error:", repr(e))

        bot.send_message(
            message.chat.id,
            "❌ Ошибка анализа. Попробуй ещё раз через минуту."
        )


@bot.message_handler(commands=["quick"])
def quick_command(message):

    try:

        result = build_signal()

        if result["status"] != "OK":
            bot.reply_to(
                message,
                "❌ Нет данных XAU/USD."
            )
            return

        direction = result["direction"]

        emoji = {
            "BUY": "🟢",
            "SELL": "🔴",
            "WAIT": "🟡"
        }[direction]

        bot.reply_to(
            message,
            (
                f"{emoji} <b>{direction}</b>\n\n"
                f"XAU/USD: {fmt(result['price'])}\n"
                f"BUY score: {result['buy_score']}/12\n"
                f"SELL score: {result['sell_score']}/12"
            )
        )

    except Exception as e:

        print("Quick error:", repr(e))

        bot.reply_to(
            message,
            "❌ Ошибка."
        )


@bot.message_handler(commands=["levels"])
def levels_command(message):

    try:

        result = build_signal()

        if result["status"] != "OK":
            bot.reply_to(
                message,
                "❌ Нет данных."
            )
            return

        bot.reply_to(
            message,
            (
                "🎯 <b>GOLD LEVELS</b>\n\n"
                f"Price: {fmt(result['price'])}\n"
                f"Asian High: {fmt(result['asia_high'])}\n"
                f"Asian Low: {fmt(result['asia_low'])}\n"
                f"PDH: {fmt(result['pdh'])}\n"
                f"PDL: {fmt(result['pdl'])}\n"
            )
        )

    except Exception as e:

        print("Levels error:", repr(e))

        bot.reply_to(
            message,
            "❌ Ошибка."
        )


@bot.message_handler(commands=["status"])
def status_command(message):

    bot.reply_to(
        message,
        (
            "🟢 <b>GOLD SMART V3.3</b>\n\n"
            "Server: ONLINE\n"
            f"Webhook: {'OK' if WEBHOOK_READY else 'CHECK'}\n"
            "Mode: SIGNALS ONLY\n"
            "Auto trading: OFF"
        )
    )


# =========================================================
# TEXT COMMANDS
# =========================================================

@bot.message_handler(
    func=lambda message: (
        message.text is not None
        and message.text.lower().strip()
        in [
            "gold",
            "золото",
            "голд",
            "золото 🟡"
        ]
    )
)
def text_gold(message):

    gold_command(message)


# =========================================================
# START WEBHOOK WHEN GUNICORN IMPORTS bot.py
# =========================================================

# IMPORTANT:
# This is intentionally OUTSIDE:
# if __name__ == "__main__":
#
# Gunicorn imports bot.py and therefore executes this line.

try:
    setup_webhook()
except Exception as e:
    print("Startup webhook error:", repr(e))


# =========================================================
# LOCAL RUN
# =========================================================

if __name__ == "__main__":

    port = int(
        os.getenv("PORT", "10000")
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
