import os
import time
import threading
import requests
import pandas as pd
import numpy as np

from flask import Flask
import telebot


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TWELVE_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")

SYMBOL = "XAU/USD"
API_URL = "https://api.twelvedata.com"

# Telegram
bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode="HTML",
    threaded=True
)

app = Flask(__name__)


# =========================================================
# WEB
# =========================================================

@app.route("/")
def home():
    return "GOLD SMART V4 ONLINE", 200


@app.route("/health")
def health():
    return "OK", 200


# =========================================================
# TWELVE DATA
# =========================================================

def twelve_request(endpoint, params):
    params["apikey"] = TWELVE_API_KEY

    try:
        r = requests.get(
            f"{API_URL}/{endpoint}",
            params=params,
            timeout=15
        )

        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"

        data = r.json()

        if data.get("status") == "error":
            return None, data.get("message", "API error")

        return data, None

    except Exception as e:
        return None, str(e)


def get_price():
    data, error = twelve_request(
        "price",
        {
            "symbol": SYMBOL
        }
    )

    if error:
        return None, error

    try:
        price = float(data["price"])
        return price, None
    except Exception:
        return None, "Invalid price response"


def get_candles(interval, outputsize=250):

    data, error = twelve_request(
        "time_series",
        {
            "symbol": SYMBOL,
            "interval": interval,
            "outputsize": outputsize,
            "order": "asc",
            "timezone": "UTC"
        }
    )

    if error:
        return None, error

    if "values" not in data:
        return None, "No candle data"

    try:
        df = pd.DataFrame(data["values"])

        df["datetime"] = pd.to_datetime(df["datetime"])

        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(
            subset=["open", "high", "low", "close"]
        )

        df = df.sort_values("datetime").reset_index(drop=True)

        return df, None

    except Exception as e:
        return None, str(e)


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

    return 100 - (100 / (1 + rs))


def atr(df, period=14):

    high_low = df["high"] - df["low"]

    high_close = (
        df["high"] - df["close"].shift()
    ).abs()

    low_close = (
        df["low"] - df["close"].shift()
    ).abs()

    tr = pd.concat(
        [high_low, high_close, low_close],
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

    df = df.copy()

    df["ema50"] = ema(df["close"], 50)
    df["ema200"] = ema(df["close"], 200)

    last = df.iloc[-1]

    if last["ema50"] > last["ema200"]:
        if last["close"] > last["ema50"]:
            return "BULLISH"

        return "BULLISH_WEAK"

    if last["ema50"] < last["ema200"]:
        if last["close"] < last["ema50"]:
            return "BEARISH"

        return "BEARISH_WEAK"

    return "NEUTRAL"


# =========================================================
# MARKET STRUCTURE
# =========================================================

def structure(df, lookback=20):

    if len(df) < lookback + 5:
        return "NEUTRAL"

    recent = df.tail(lookback)

    highs = recent["high"].values
    lows = recent["low"].values

    hh = highs[-1] > highs[:-1].max()
    ll = lows[-1] < lows[:-1].min()

    if hh:
        return "BULLISH BOS"

    if ll:
        return "BEARISH BOS"

    last_half = recent.iloc[:lookback // 2]
    second_half = recent.iloc[lookback // 2:]

    if second_half["high"].mean() > last_half["high"].mean():
        return "HH/HL"

    if second_half["low"].mean() < last_half["low"].mean():
        return "LH/LL"

    return "RANGE"


# =========================================================
# LIQUIDITY SWEEP
# =========================================================

def liquidity_sweep(df, lookback=20):

    if len(df) < lookback + 2:
        return "NONE"

    prev = df.iloc[-2]
    last = df.iloc[-1]

    reference = df.iloc[-lookback-1:-1]

    previous_high = reference["high"].max()
    previous_low = reference["low"].min()

    # Sell-side liquidity swept and price closed back above
    if (
        last["low"] < previous_low
        and last["close"] > previous_low
    ):
        return "SSL SWEEP → BUY"

    # Buy-side liquidity swept and price closed back below
    if (
        last["high"] > previous_high
        and last["close"] < previous_high
    ):
        return "BSL SWEEP → SELL"

    return "NONE"


# =========================================================
# FVG
# =========================================================

def detect_fvg(df):

    if len(df) < 4:
        return "NONE"

    a = df.iloc[-3]
    b = df.iloc[-2]
    c = df.iloc[-1]

    # Bullish FVG
    if c["low"] > a["high"]:
        return "BULLISH FVG"

    # Bearish FVG
    if c["high"] < a["low"]:
        return "BEARISH FVG"

    return "NONE"


# =========================================================
# MOMENTUM
# =========================================================

def momentum(df):

    df = df.copy()

    df["rsi"] = rsi(df["close"])

    value = df["rsi"].iloc[-1]

    if value >= 55:
        return "BULLISH", value

    if value <= 45:
        return "BEARISH", value

    return "NEUTRAL", value


# =========================================================
# ANALYSIS
# =========================================================

def analyze():

    price, price_error = get_price()

    if price is None:
        return {
            "signal": "NO TRADE",
            "error": f"PRICE ERROR: {price_error}"
        }

    h4, e4 = get_candles("4h", 250)
    h1, e1 = get_candles("1h", 250)
    m15, e15 = get_candles("15min", 250)

    if h4 is None or h1 is None or m15 is None:

        errors = []

        if e4:
            errors.append(f"H4: {e4}")

        if e1:
            errors.append(f"H1: {e1}")

        if e15:
            errors.append(f"M15: {e15}")

        return {
            "signal": "NO TRADE",
            "price": price,
            "error": " | ".join(errors)
        }

    # Trends
    trend_h4 = get_trend(h4)
    trend_h1 = get_trend(h1)
    trend_m15 = get_trend(m15)

    # Structure
    structure_h1 = structure(h1)
    structure_m15 = structure(m15)

    # Liquidity
    sweep = liquidity_sweep(m15)

    # FVG
    fvg = detect_fvg(m15)

    # Momentum
    mom_h1, rsi_h1 = momentum(h1)
    mom_m15, rsi_m15 = momentum(m15)

    # ATR
    m15 = m15.copy()
    m15["atr"] = atr(m15)

    atr_value = float(m15["atr"].iloc[-1])

    # =====================================================
    # SCORING
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

    # Structure
    if structure_h1 in ["HH/HL", "BULLISH BOS"]:
        buy_score += 1
        reasons_buy.append(structure_h1)

    elif structure_h1 in ["LH/LL", "BEARISH BOS"]:
        sell_score += 1
        reasons_sell.append(structure_h1)

    if structure_m15 == "BULLISH BOS":
        buy_score += 1
        reasons_buy.append("M15 BOS")

    elif structure_m15 == "BEARISH BOS":
        sell_score += 1
        reasons_sell.append("M15 BOS")

    # Liquidity
    if "BUY" in sweep:
        buy_score += 2
        reasons_buy.append(sweep)

    elif "SELL" in sweep:
        sell_score += 2
        reasons_sell.append(sweep)

    # FVG
    if fvg == "BULLISH FVG":
        buy_score += 1
        reasons_buy.append("Bullish FVG")

    elif fvg == "BEARISH FVG":
        sell_score += 1
        reasons_sell.append("Bearish FVG")

    # Momentum
    if mom_h1 == "BULLISH":
        buy_score += 1
        reasons_buy.append(f"H1 RSI {rsi_h1:.1f}")

    elif mom_h1 == "BEARISH":
        sell_score += 1
        reasons_sell.append(f"H1 RSI {rsi_h1:.1f}")

    # =====================================================
    # SIGNAL FILTER
    # =====================================================

    signal = "WAIT"

    if (
        buy_score >= 8
        and buy_score >= sell_score + 2
    ):
        signal = "BUY"

    elif (
        sell_score >= 8
        and sell_score >= buy_score + 2
    ):
        signal = "SELL"

    # =====================================================
    # LEVELS
    # =====================================================

    entry = price

    # Conservative ATR-based levels
    if atr_value <= 0:
        atr_value = 5.0

    if signal == "BUY":

        sl = entry - atr_value * 1.5

        tp1 = entry + atr_value * 1.5
        tp2 = entry + atr_value * 2.5

    elif signal == "SELL":

        sl = entry + atr_value * 1.5

        tp1 = entry - atr_value * 1.5
        tp2 = entry - atr_value * 2.5

    else:

        sl = None
        tp1 = None
        tp2 = None

    return {
        "signal": signal,
        "price": price,

        "h4": trend_h4,
        "h1": trend_h1,
        "m15": trend_m15,

        "structure_h1": structure_h1,
        "structure_m15": structure_m15,

        "sweep": sweep,
        "fvg": fvg,

        "rsi_h1": rsi_h1,
        "rsi_m15": rsi_m15,

        "buy_score": buy_score,
        "sell_score": sell_score,

        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,

        "atr": atr_value,

        "buy_reasons": reasons_buy,
        "sell_reasons": reasons_sell
    }


# =========================================================
# TELEGRAM FORMAT
# =========================================================

def format_analysis(result):

    if result["signal"] == "NO TRADE":

        return (
            "🥇 <b>GOLD SMART V4</b>\n\n"
            "🛑 <b>NO TRADE</b>\n\n"
            f"💰 Цена: {result.get('price', 'N/A')}\n"
            f"⚠️ {result.get('error', 'Unknown error')}\n\n"
            "Данные не подтверждены."
        )

    signal = result["signal"]

    if signal == "BUY":
        emoji = "🟢"
        reasons = result["buy_reasons"]

    elif signal == "SELL":
        emoji = "🔴"
        reasons = result["sell_reasons"]

    else:
        emoji = "⚪"
        reasons = []

    text = (
        "🥇 <b>GOLD SMART V4</b>\n\n"
        f"💰 Цена: <b>{result['price']:.2f}</b>\n\n"

        f"📊 H4: <b>{result['h4']}</b>\n"
        f"📊 H1: <b>{result['h1']}</b>\n"
        f"📊 M15: <b>{result['m15']}</b>\n\n"

        f"🏗 H1 Structure: {result['structure_h1']}\n"
        f"🏗 M15 Structure: {result['structure_m15']}\n"
        f"💧 Liquidity: {result['sweep']}\n"
        f"🟨 FVG: {result['fvg']}\n\n"

        f"RSI H1: {result['rsi_h1']:.1f}\n"
        f"RSI M15: {result['rsi_m15']:.1f}\n\n"

        f"🟢 BUY score: {result['buy_score']}/12\n"
        f"🔴 SELL score: {result['sell_score']}/12\n\n"

        f"{emoji} <b>SIGNAL: {signal}</b>\n"
    )

    if signal in ["BUY", "SELL"]:

        text += (
            "\n"
            f"🎯 Entry: <b>{result['entry']:.2f}</b>\n"
            f"🛡 SL: <b>{result['sl']:.2f}</b>\n"
            f"🎯 TP1: <b>{result['tp1']:.2f}</b>\n"
            f"🎯 TP2: <b>{result['tp2']:.2f}</b>\n"
            "\n"
            "<b>Подтверждения:</b>\n"
        )

        for reason in reasons:
            text += f"• {reason}\n"

    else:

        text += (
            "\n"
            "⏳ Нет достаточного подтверждения.\n"
            "Лучше ждать следующую структуру."
        )

    text += (
        "\n\n"
        "⚠️ Только аналитический сигнал. "
        "Автоторговля отключена."
    )

    return text


# =========================================================
# COMMANDS
# =========================================================

@bot.message_handler(commands=["start"])
def start(message):

    bot.send_message(
        message.chat.id,
        "🥇 <b>GOLD SMART V4</b>\n\n"
        "Бот онлайн.\n\n"
        "/gold — анализ XAU/USD\n"
        "/start — проверка связи"
    )


@bot.message_handler(commands=["gold"])
def gold_command(message):

    bot.send_message(
        message.chat.id,
        "🔎 Анализирую XAU/USD...\n"
        "H4 → H1 → M15"
    )

    result = analyze()

    bot.send_message(
        message.chat.id,
        format_analysis(result)
    )


@bot.message_handler(
    func=lambda message: message.text
    and message.text.lower() in ["gold", "золото"]
)
def gold_text(message):

    result = analyze()

    bot.send_message(
        message.chat.id,
        format_analysis(result)
    )


# =========================================================
# POLLING
# =========================================================

def run_bot():

    print("==============================")
    print("🟢 GOLD SMART V4 POLLING")
    print("==============================")

    time.sleep(3)

    try:
        bot.remove_webhook()

        bot.infinity_polling(
            timeout=30,
            long_polling_timeout=30,
            skip_pending=True
        )

    except Exception as e:

        print("Polling error:", e)


threading.Thread(
    target=run_bot,
    daemon=True
).start()


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )
