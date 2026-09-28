import os
import time
import threading
import requests
import pandas as pd
import numpy as np

from flask import Flask, request, jsonify

# =========================================================
# GOLD SMART V4.1 — WEBHOOK
# XAU/USD SIGNAL BOT — NO AUTO TRADING
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")

RENDER_URL = "https://gold-bot-q8la.onrender.com"

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TWELVE_URL = "https://api.twelvedata.com"

app = Flask(__name__)

# ---------------------------------------------------------
# BASIC
# ---------------------------------------------------------

def telegram(method, payload=None):
    try:
        r = requests.post(
            f"{TELEGRAM_API}/{method}",
            json=payload or {},
            timeout=20
        )
        return r.json()
    except Exception as e:
        print("Telegram error:", e)
        return {"ok": False, "error": str(e)}


def send_message(chat_id, text):
    return telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
    )


# ---------------------------------------------------------
# TWELVE DATA
# ---------------------------------------------------------

def td_request(endpoint, params):
    params = dict(params)
    params["apikey"] = TWELVE_DATA_API_KEY

    try:
        r = requests.get(
            f"{TWELVE_URL}/{endpoint}",
            params=params,
            timeout=20
        )

        data = r.json()

        if "status" in data and data["status"] == "error":
            print("Twelve Data error:", data)
            return None

        return data

    except Exception as e:
        print("Twelve Data request error:", e)
        return None


def get_price():
    data = td_request(
        "price",
        {
            "symbol": "XAU/USD"
        }
    )

    if not data:
        return None

    try:
        return float(data["price"])
    except Exception:
        return None


def get_candles(interval, outputsize=250):
    data = td_request(
        "time_series",
        {
            "symbol": "XAU/USD",
            "interval": interval,
            "outputsize": outputsize,
            "timezone": "UTC"
        }
    )

    if not data or "values" not in data:
        return None

    try:
        df = pd.DataFrame(data["values"])

        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df["datetime"] = pd.to_datetime(df["datetime"], utc=True)

        df = df.sort_values("datetime").reset_index(drop=True)

        return df

    except Exception as e:
        print("Candle parsing error:", e)
        return None


# ---------------------------------------------------------
# INDICATORS
# ---------------------------------------------------------

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    return 100 - (100 / (1 + rs))


def atr(df, period=14):
    prev_close = df["close"].shift(1)

    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()

    tr = pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)

    return tr.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()


# ---------------------------------------------------------
# TREND
# ---------------------------------------------------------

def get_trend(df):
    if df is None or len(df) < 210:
        return "UNKNOWN"

    df = df.copy()

    df["ema50"] = ema(df["close"], 50)
    df["ema200"] = ema(df["close"], 200)

    last = df.iloc[-1]

    if last["close"] > last["ema50"] > last["ema200"]:
        return "BULLISH"

    if last["close"] < last["ema50"] < last["ema200"]:
        return "BEARISH"

    return "NEUTRAL"


# ---------------------------------------------------------
# STRUCTURE
# ---------------------------------------------------------

def get_structure(df, lookback=5):
    if df is None or len(df) < lookback * 3:
        return "UNKNOWN"

    highs = df["high"]
    lows = df["low"]

    recent_high = highs.iloc[-lookback:].max()
    previous_high = highs.iloc[-lookback * 2:-lookback].max()

    recent_low = lows.iloc[-lookback:].min()
    previous_low = lows.iloc[-lookback * 2:-lookback].min()

    if recent_high > previous_high and recent_low > previous_low:
        return "HH/HL"

    if recent_high < previous_high and recent_low < previous_low:
        return "LH/LL"

    return "MIXED"


# ---------------------------------------------------------
# LIQUIDITY SWEEP
# ---------------------------------------------------------

def liquidity_sweep(df):
    if df is None or len(df) < 30:
        return "NONE"

    prev = df.iloc[-2]
    last = df.iloc[-1]

    recent_high = df["high"].iloc[-22:-2].max()
    recent_low = df["low"].iloc[-22:-2].min()

    # Sell-side liquidity swept, then price recovered
    if (
        last["low"] < recent_low
        and last["close"] > recent_low
    ):
        return "SSL SWEEP"

    # Buy-side liquidity swept, then price rejected
    if (
        last["high"] > recent_high
        and last["close"] < recent_high
    ):
        return "BSL SWEEP"

    return "NONE"


# ---------------------------------------------------------
# FVG
# ---------------------------------------------------------

def detect_fvg(df):
    if df is None or len(df) < 5:
        return "NONE"

    a = df.iloc[-3]
    b = df.iloc[-2]
    c = df.iloc[-1]

    # Bullish FVG
    if a["high"] < c["low"]:
        return "BULLISH FVG"

    # Bearish FVG
    if a["low"] > c["high"]:
        return "BEARISH FVG"

    return "NONE"


# ---------------------------------------------------------
# MOMENTUM
# ---------------------------------------------------------

def get_momentum(df):
    if df is None or len(df) < 30:
        return "UNKNOWN", 50

    df = df.copy()

    df["rsi"] = rsi(df["close"], 14)

    last_rsi = float(df["rsi"].iloc[-1])

    if last_rsi >= 55:
        return "BULLISH", last_rsi

    if last_rsi <= 45:
        return "BEARISH", last_rsi

    return "NEUTRAL", last_rsi


# ---------------------------------------------------------
# ATR
# ---------------------------------------------------------

def get_atr(df):
    if df is None or len(df) < 30:
        return None

    values = atr(df, 14)

    value = values.iloc[-1]

    if pd.isna(value):
        return None

    return float(value)


# ---------------------------------------------------------
# SIGNAL ENGINE
# ---------------------------------------------------------

def analyze():

    price = get_price()

    if price is None:
        return {
            "status": "NO TRADE",
            "reason": "Не удалось получить текущую цену XAU/USD."
        }

    h4 = get_candles("4h")
    h1 = get_candles("1h")
    m15 = get_candles("15min")

    if h4 is None or h1 is None or m15 is None:
        return {
            "status": "NO TRADE",
            "price": price,
            "reason": "Недоступны свечи H4/H1/M15."
        }

    h4_trend = get_trend(h4)
    h1_trend = get_trend(h1)
    m15_trend = get_trend(m15)

    h1_structure = get_structure(h1)
    m15_structure = get_structure(m15)

    sweep = liquidity_sweep(m15)
    fvg = detect_fvg(m15)

    momentum, rsi_value = get_momentum(m15)

    atr_value = get_atr(m15)

    buy_score = 0
    sell_score = 0

    # -----------------------------------------------------
    # H4
    # -----------------------------------------------------

    if h4_trend == "BULLISH":
        buy_score += 2

    elif h4_trend == "BEARISH":
        sell_score += 2

    # -----------------------------------------------------
    # H1
    # -----------------------------------------------------

    if h1_trend == "BULLISH":
        buy_score += 2

    elif h1_trend == "BEARISH":
        sell_score += 2

    # -----------------------------------------------------
    # M15
    # -----------------------------------------------------

    if m15_trend == "BULLISH":
        buy_score += 2

    elif m15_trend == "BEARISH":
        sell_score += 2

    # -----------------------------------------------------
    # STRUCTURE
    # -----------------------------------------------------

    if h1_structure == "HH/HL":
        buy_score += 1

    elif h1_structure == "LH/LL":
        sell_score += 1

    if m15_structure == "HH/HL":
        buy_score += 1

    elif m15_structure == "LH/LL":
        sell_score += 1

    # -----------------------------------------------------
    # LIQUIDITY
    # -----------------------------------------------------

    if sweep == "SSL SWEEP":
        buy_score += 2

    elif sweep == "BSL SWEEP":
        sell_score += 2

    # -----------------------------------------------------
    # FVG
    # -----------------------------------------------------

    if fvg == "BULLISH FVG":
        buy_score += 1

    elif fvg == "BEARISH FVG":
        sell_score += 1

    # -----------------------------------------------------
    # MOMENTUM
    # -----------------------------------------------------

    if momentum == "BULLISH":
        buy_score += 1

    elif momentum == "BEARISH":
        sell_score += 1

    # -----------------------------------------------------
    # SIGNAL
    # -----------------------------------------------------

    margin = abs(buy_score - sell_score)

    signal = "WAIT"

    if buy_score >= 8 and margin >= 2:
        signal = "BUY"

    elif sell_score >= 8 and margin >= 2:
        signal = "SELL"

    # -----------------------------------------------------
    # SL / TP
    # -----------------------------------------------------

    entry = price
    sl = None
    tp1 = None
    tp2 = None

    if atr_value:

        # Conservative ATR distances
        sl_distance = max(atr_value * 1.2, 3.0)

        tp1_distance = sl_distance * 1.5
        tp2_distance = sl_distance * 2.2

        if signal == "BUY":
            sl = entry - sl_distance
            tp1 = entry + tp1_distance
            tp2 = entry + tp2_distance

        elif signal == "SELL":
            sl = entry + sl_distance
            tp1 = entry - tp1_distance
            tp2 = entry - tp2_distance

    return {
        "status": signal,
        "price": price,

        "h4": h4_trend,
        "h1": h1_trend,
        "m15": m15_trend,

        "h1_structure": h1_structure,
        "m15_structure": m15_structure,

        "sweep": sweep,
        "fvg": fvg,

        "momentum": momentum,
        "rsi": rsi_value,

        "buy_score": buy_score,
        "sell_score": sell_score,
        "margin": margin,

        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2
    }


# ---------------------------------------------------------
# FORMAT SIGNAL
# ---------------------------------------------------------

def format_signal(data):

    if data.get("status") == "NO TRADE":

        return (
            "🥇 <b>GOLD SMART V4.1</b>\n\n"
            f"💰 Цена: <b>{data.get('price', 'N/A')}</b>\n\n"
            "⛔ <b>NO TRADE</b>\n"
            f"Причина: {data.get('reason', 'unknown')}\n\n"
            "ℹ️ Автоторговля отключена."
        )

    signal = data["status"]

    if signal == "BUY":
        emoji = "🟢"
    elif signal == "SELL":
        emoji = "🔴"
    else:
        emoji = "🟡"

    text = (
        f"🥇 <b>GOLD SMART V4.1</b>\n\n"
        f"💰 Цена: <b>{data['price']:.2f}</b>\n\n"

        f"📊 H4: <b>{data['h4']}</b>\n"
        f"📊 H1: <b>{data['h1']}</b>\n"
        f"📊 M15: <b>{data['m15']}</b>\n\n"

        f"🏗 H1 Structure: {data['h1_structure']}\n"
        f"🏗 M15 Structure: {data['m15_structure']}\n"
        f"💧 Liquidity: {data['sweep']}\n"
        f"🧩 FVG: {data['fvg']}\n"
        f"📈 Momentum: {data['momentum']}\n"
        f"RSI: {data['rsi']:.1f}\n\n"

        f"🟢 BUY score: {data['buy_score']}\n"
        f"🔴 SELL score: {data['sell_score']}\n"
        f"⚖️ Margin: {data['margin']}\n\n"

        f"{emoji} <b>SIGNAL: {signal}</b>\n"
    )

    if signal in ["BUY", "SELL"]:

        text += (
            "\n"
            f"🎯 Entry: <b>{data['entry']:.2f}</b>\n"
            f"🛡 SL: <b>{data['sl']:.2f}</b>\n"
            f"🎯 TP1: <b>{data['tp1']:.2f}</b>\n"
            f"🎯 TP2: <b>{data['tp2']:.2f}</b>\n\n"
            "⚠️ Ручная торговля. Автоторговля отключена."
        )

    else:

        text += (
            "\n"
            "⏳ <b>WAIT</b>\n"
            "Сигнал недостаточно подтверждён.\n\n"
            "⚠️ Не входить в сделку только по этому сообщению."
        )

    return text


# ---------------------------------------------------------
# TELEGRAM COMMAND PROCESSOR
# ---------------------------------------------------------

def process_update(update):

    try:

        message = update.get("message")

        if not message:
            return

        chat = message.get("chat")

        if not chat:
            return

        chat_id = chat["id"]

        text = message.get("text", "").strip().lower()

        print(
            f"Telegram message: chat={chat_id}, text={text}"
        )

        if text == "/start":

            send_message(
                chat_id,
                (
                    "🥇 <b>GOLD SMART V4.1</b>\n\n"
                    "Бот успешно подключён.\n\n"
                    "Команды:\n"
                    "🔹 /gold — анализ золота XAU/USD\n"
                    "🔹 /start — информация\n\n"
                    "📊 Анализ H4 → H1 → M15\n"
                    "💧 Liquidity\n"
                    "🧩 FVG\n"
                    "📈 Momentum\n"
                    "🛡 Risk filter\n\n"
                    "Автоторговля отключена."
                )
            )

            return

        if text in ["/gold", "gold", "золото"]:

            send_message(
                chat_id,
                "⏳ <b>Анализирую XAU/USD...</b>"
            )

            result = analyze()

            send_message(
                chat_id,
                format_signal(result)
            )

            return

    except Exception as e:

        print("Update processing error:", e)


# ---------------------------------------------------------
# TELEGRAM WEBHOOK
# ---------------------------------------------------------

@app.route("/telegram", methods=["POST"])
def telegram_webhook():

    try:

        update = request.get_json(silent=True)

        if update:
            process_update(update)

        return jsonify({
            "ok": True
        })

    except Exception as e:

        print("Webhook error:", e)

        return jsonify({
            "ok": False,
            "error": str(e)
        }), 500


# ---------------------------------------------------------
# HEALTH
# ---------------------------------------------------------

@app.route("/", methods=["GET", "HEAD"])
def home():

    return "GOLD SMART V4.1 ONLINE", 200


@app.route("/health", methods=["GET"])
def health():

    return jsonify({
        "status": "online",
        "bot": "GOLD SMART V4.1",
        "mode": "webhook",
        "trading": "disabled"
    })


# ---------------------------------------------------------
# SET WEBHOOK
# ---------------------------------------------------------

def setup_webhook():

    time.sleep(3)

    if not BOT_TOKEN:

        print("❌ BOT_TOKEN is missing")
        return

    webhook_url = f"{RENDER_URL}/telegram"

    print("Setting Telegram webhook:")
    print(webhook_url)

    result = telegram(
        "setWebhook",
        {
            "url": webhook_url,
            "drop_pending_updates": True
        }
    )

    print("Webhook result:", result)

    info = telegram("getWebhookInfo")

    print("Webhook info:", info)


# Start webhook setup once
threading.Thread(
    target=setup_webhook,
    daemon=True
).start()


# ---------------------------------------------------------
# LOCAL
# ---------------------------------------------------------

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port
    )
