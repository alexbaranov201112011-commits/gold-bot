import os
import threading
import time
import requests
import pandas as pd
import numpy as np

from flask import Flask, request

# =========================================================
# GOLD SMART V4.2
# Telegram Webhook + Twelve Data
# Manual signals only — NO AUTO TRADING
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")

PORT = int(os.environ.get("PORT", "10000"))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TWELVE_URL = "https://api.twelvedata.com"

app = Flask(__name__)


# =========================================================
# TELEGRAM
# =========================================================

def telegram_send(chat_id, text):
    if not BOT_TOKEN:
        return

    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text
            },
            timeout=15
        )
    except Exception as e:
        print("Telegram error:", e)


# =========================================================
# TWELVE DATA
# =========================================================

def td_request(endpoint, params):
    if not TWELVE_DATA_API_KEY:
        print("Twelve Data API key missing")
        return None

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

        if "code" in data and data.get("code") != 200:
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
            "format": "JSON"
        }
    )

    if not data or "values" not in data:
        return None

    try:
        df = pd.DataFrame(data["values"])

        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df["datetime"] = pd.to_datetime(df["datetime"])

        df = df.sort_values("datetime").reset_index(drop=True)

        return df

    except Exception as e:
        print("Candle parsing error:", e)
        return None


# =========================================================
# INDICATORS
# =========================================================

def add_indicators(df):
    df = df.copy()

    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

    delta = df["close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    df["rsi"] = 100 - (100 / (1 + rs))

    high_low = df["high"] - df["low"]
    high_close = abs(df["high"] - df["close"].shift())
    low_close = abs(df["low"] - df["close"].shift())

    tr = pd.concat(
        [high_low, high_close, low_close],
        axis=1
    ).max(axis=1)

    df["atr"] = tr.rolling(14).mean()

    return df


# =========================================================
# TREND
# =========================================================

def get_trend(df):
    if df is None or len(df) < 210:
        return "UNKNOWN"

    last = df.iloc[-1]

    if last["close"] > last["ema50"] > last["ema200"]:
        return "BULLISH"

    if last["close"] < last["ema50"] < last["ema200"]:
        return "BEARISH"

    return "MIXED"


# =========================================================
# STRUCTURE
# =========================================================

def get_structure(df, lookback=5):
    if df is None or len(df) < lookback * 3:
        return "UNKNOWN"

    recent = df.tail(lookback * 3)

    highs = recent["high"].rolling(
        lookback,
        center=True
    ).max()

    lows = recent["low"].rolling(
        lookback,
        center=True
    ).min()

    swing_highs = recent[
        recent["high"] == highs
    ]["high"]

    swing_lows = recent[
        recent["low"] == lows
    ]["low"]

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return "MIXED"

    h1, h2 = swing_highs.iloc[-2], swing_highs.iloc[-1]
    l1, l2 = swing_lows.iloc[-2], swing_lows.iloc[-1]

    if h2 > h1 and l2 > l1:
        return "HH/HL"

    if h2 < h1 and l2 < l1:
        return "LH/LL"

    return "MIXED"


# =========================================================
# LIQUIDITY SWEEP
# =========================================================

def detect_liquidity(df):
    if df is None or len(df) < 10:
        return "NONE"

    prev = df.iloc[-2]
    last = df.iloc[-1]

    recent_high = df["high"].iloc[-7:-2].max()
    recent_low = df["low"].iloc[-7:-2].min()

    # Bearish liquidity sweep:
    # price takes previous high but closes below it
    if last["high"] > recent_high and last["close"] < recent_high:
        return "BSL SWEEP"

    # Bullish liquidity sweep:
    # price takes previous low but closes above it
    if last["low"] < recent_low and last["close"] > recent_low:
        return "SSL SWEEP"

    return "NONE"


# =========================================================
# FVG
# =========================================================

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


# =========================================================
# BOS / CHoCH
# =========================================================

def detect_structure_break(df):
    if df is None or len(df) < 15:
        return "NONE"

    last = df.iloc[-1]

    previous_high = df["high"].iloc[-10:-2].max()
    previous_low = df["low"].iloc[-10:-2].min()

    if last["close"] > previous_high:
        return "BULLISH BOS"

    if last["close"] < previous_low:
        return "BEARISH BOS"

    return "NONE"


# =========================================================
# MOMENTUM
# =========================================================

def get_momentum(df):
    if df is None or len(df) < 10:
        return "UNKNOWN"

    last = df.iloc[-1]

    if last["close"] > last["close"].shift if False else False:
        pass

    c1 = df["close"].iloc[-1]
    c5 = df["close"].iloc[-6]

    if c1 > c5:
        return "BULLISH"

    if c1 < c5:
        return "BEARISH"

    return "NEUTRAL"


# =========================================================
# SIGNAL ENGINE V4.2
# =========================================================

def analyze():

    price = get_price()

    if price is None:
        return {
            "signal": "NO TRADE",
            "reason": "Не удалось получить текущую цену XAU/USD."
        }

    h4 = get_candles("4h")
    h1 = get_candles("1h")
    m15 = get_candles("15min")

    if h4 is None or h1 is None or m15 is None:
        return {
            "signal": "NO TRADE",
            "reason": "Недостаточно рыночных данных."
        }

    h4 = add_indicators(h4)
    h1 = add_indicators(h1)
    m15 = add_indicators(m15)

    if (
        len(h4) < 210
        or len(h1) < 210
        or len(m15) < 210
    ):
        return {
            "signal": "NO TRADE",
            "reason": "Недостаточно свечей для EMA200."
        }

    h4_trend = get_trend(h4)
    h1_trend = get_trend(h1)
    m15_trend = get_trend(m15)

    h1_structure = get_structure(h1)
    m15_structure = get_structure(m15)

    liquidity = detect_liquidity(m15)
    fvg = detect_fvg(m15)
    bos = detect_structure_break(m15)

    momentum = get_momentum(m15)

    rsi = float(m15["rsi"].iloc[-1])
    atr = float(m15["atr"].iloc[-1])

    if not np.isfinite(atr) or atr <= 0:
        return {
            "signal": "NO TRADE",
            "reason": "ATR недоступен."
        }

    buy_score = 0
    sell_score = 0

    # -----------------------------------------------------
    # TREND
    # -----------------------------------------------------

    if h4_trend == "BULLISH":
        buy_score += 2

    if h4_trend == "BEARISH":
        sell_score += 2

    if h1_trend == "BULLISH":
        buy_score += 2

    if h1_trend == "BEARISH":
        sell_score += 2

    if m15_trend == "BULLISH":
        buy_score += 2

    if m15_trend == "BEARISH":
        sell_score += 2

    # -----------------------------------------------------
    # STRUCTURE
    # -----------------------------------------------------

    if h1_structure == "HH/HL":
        buy_score += 1

    if h1_structure == "LH/LL":
        sell_score += 1

    if m15_structure == "HH/HL":
        buy_score += 1

    if m15_structure == "LH/LL":
        sell_score += 1

    # -----------------------------------------------------
    # MOMENTUM
    # -----------------------------------------------------

    if momentum == "BULLISH":
        buy_score += 1

    if momentum == "BEARISH":
        sell_score += 1

    # -----------------------------------------------------
    # SMC CONFIRMATION
    # -----------------------------------------------------

    smc_buy = 0
    smc_sell = 0

    if liquidity == "SSL SWEEP":
        smc_buy += 2

    if liquidity == "BSL SWEEP":
        smc_sell += 2

    if fvg == "BULLISH FVG":
        smc_buy += 2

    if fvg == "BEARISH FVG":
        smc_sell += 2

    if bos == "BULLISH BOS":
        smc_buy += 2

    if bos == "BEARISH BOS":
        smc_sell += 2

    buy_score += smc_buy
    sell_score += smc_sell

    # -----------------------------------------------------
    # RSI EXTENSION FILTER
    # -----------------------------------------------------

    buy_extended = rsi >= 68
    sell_extended = rsi <= 32

    # -----------------------------------------------------
    # STRICT SIGNAL RULE
    # -----------------------------------------------------

    trend_buy = (
        h4_trend == "BULLISH"
        and h1_trend == "BULLISH"
        and m15_trend == "BULLISH"
    )

    trend_sell = (
        h4_trend == "BEARISH"
        and h1_trend == "BEARISH"
        and m15_trend == "BEARISH"
    )

    buy_smc_confirmed = (
        smc_buy >= 2
    )

    sell_smc_confirmed = (
        smc_sell >= 2
    )

    signal = "WAIT"
    reason = ""

    # BUY
    if (
        trend_buy
        and buy_smc_confirmed
        and buy_score >= 9
        and not buy_extended
    ):
        signal = "BUY"

    # SELL
    elif (
        trend_sell
        and sell_smc_confirmed
        and sell_score >= 9
        and not sell_extended
    ):
        signal = "SELL"

    else:
        reasons = []

        if not trend_buy and not trend_sell:
            reasons.append("нет полного согласования H4/H1/M15")

        if not buy_smc_confirmed and not sell_smc_confirmed:
            reasons.append("нет SMC-подтверждения")

        if sell_extended:
            reasons.append("SELL слишком растянут по RSI")

        if buy_extended:
            reasons.append("BUY слишком растянут по RSI")

        if not reasons:
            reasons.append("условия входа ещё не подтверждены")

        reason = "; ".join(reasons)

    # -----------------------------------------------------
    # TRADE LEVELS
    # -----------------------------------------------------

    sl = None
    tp1 = None
    tp2 = None
    rr = None

    if signal == "BUY":

        recent_low = m15["low"].iloc[-12:].min()

        sl = min(
            recent_low - atr * 0.20,
            price - atr * 1.2
        )

        risk = price - sl

        tp1 = price + risk * 2.0
        tp2 = price + risk * 3.0

        rr = 2.0

    elif signal == "SELL":

        recent_high = m15["high"].iloc[-12:].max()

        sl = max(
            recent_high + atr * 0.20,
            price + atr * 1.2
        )

        risk = sl - price

        tp1 = price - risk * 2.0
        tp2 = price - risk * 3.0

        rr = 2.0

    return {
        "signal": signal,
        "price": price,

        "h4": h4_trend,
        "h1": h1_trend,
        "m15": m15_trend,

        "h1_structure": h1_structure,
        "m15_structure": m15_structure,

        "liquidity": liquidity,
        "fvg": fvg,
        "bos": bos,

        "momentum": momentum,
        "rsi": rsi,

        "buy_score": buy_score,
        "sell_score": sell_score,

        "smc_buy": smc_buy,
        "smc_sell": smc_sell,

        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "rr": rr,

        "reason": reason
    }


# =========================================================
# FORMAT MESSAGE
# =========================================================

def format_signal(a):

    if a["signal"] == "NO TRADE":

        return (
            "🥇 GOLD SMART V4.2\n\n"
            "⛔ NO TRADE\n\n"
            f"Причина: {a['reason']}\n\n"
            "⚠️ Автоторговля отключена."
        )

    signal = a["signal"]

    if signal == "BUY":
        signal_text = "🟢 SIGNAL: BUY"
    elif signal == "SELL":
        signal_text = "🔴 SIGNAL: SELL"
    else:
        signal_text = "⚪ SIGNAL: WAIT"

    text = (
        "🥇 GOLD SMART V4.2\n\n"
        f"💰 Цена: {a['price']:.2f}\n\n"

        f"📊 H4: {a['h4']}\n"
        f"📊 H1: {a['h1']}\n"
        f"📊 M15: {a['m15']}\n\n"

        f"🏗 H1 Structure: {a['h1_structure']}\n"
        f"🏗 M15 Structure: {a['m15_structure']}\n\n"

        f"💧 Liquidity: {a['liquidity']}\n"
        f"🧩 FVG: {a['fvg']}\n"
        f"🔨 BOS/CHoCH: {a['bos']}\n"
        f"📈 Momentum: {a['momentum']}\n"
        f"RSI: {a['rsi']:.1f}\n\n"

        f"🟢 BUY score: {a['buy_score']}\n"
        f"🔴 SELL score: {a['sell_score']}\n"
        f"🧠 SMC BUY: {a['smc_buy']}\n"
        f"🧠 SMC SELL: {a['smc_sell']}\n\n"

        f"{signal_text}\n"
    )

    if signal in ["BUY", "SELL"]:

        text += (
            "\n"
            f"🎯 Entry: {a['price']:.2f}\n"
            f"🛡 SL: {a['sl']:.2f}\n"
            f"🎯 TP1: {a['tp1']:.2f}\n"
            f"🎯 TP2: {a['tp2']:.2f}\n"
            f"⚖️ RR: 1:{a['rr']:.1f}\n"
        )

    else:

        text += (
            "\n"
            f"⏳ Причина: {a['reason']}\n"
        )

    text += (
        "\n"
        "🛡 Режим: MANUAL\n"
        "🚫 Автоторговля отключена.\n"
        "⚠️ Цена — ориентир Twelve Data, "
        "она может немного отличаться от FxPro."
    )

    return text


# =========================================================
# COMMANDS
# =========================================================

def process_message(message):

    chat = message.get("chat", {})
    chat_id = chat.get("id")

    if not chat_id:
        return

    text = message.get("text", "").strip().lower()

    if text in ["/start", "start"]:

        telegram_send(
            chat_id,
            "🥇 GOLD SMART V4.2\n\n"
            "Бот запущен.\n\n"
            "Команда:\n"
            "🟡 /gold — анализ XAU/USD\n\n"
            "Режим: ручные сигналы.\n"
            "Автоторговля отключена."
        )

        return

    if text in ["/gold", "gold", "золото"]:

        telegram_send(
            chat_id,
            "⏳ Анализ XAU/USD..."
        )

        try:
            analysis = analyze()
            result = format_signal(analysis)

            telegram_send(
                chat_id,
                result
            )

        except Exception as e:

            print("Analysis error:", e)

            telegram_send(
                chat_id,
                "⛔ Ошибка анализа.\n\n"
                "Попробуй ещё раз через несколько секунд."
            )

        return

    telegram_send(
        chat_id,
        "Используй команду /gold для анализа золота."
    )


# =========================================================
# WEBHOOK
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return "Gold Smart V4.2 is running."


@app.route("/health", methods=["GET"])
def health():
    return "OK"


@app.route("/telegram", methods=["POST"])
def telegram_webhook():

    try:
        update = request.get_json(silent=True)

        if update and "message" in update:
            process_message(update["message"])

    except Exception as e:
        print("Webhook error:", e)

    return "OK", 200


# =========================================================
# SET WEBHOOK
# =========================================================

def setup_webhook():

    time.sleep(5)

    render_url = os.environ.get(
        "RENDER_EXTERNAL_URL"
    )

    if not render_url:
        print("RENDER_EXTERNAL_URL not found")
        return

    webhook_url = f"{render_url}/telegram"

    try:

        response = requests.post(
            f"{TELEGRAM_API}/setWebhook",
            json={
                "url": webhook_url,
                "allowed_updates": ["message"]
            },
            timeout=15
        )

        print("Webhook setup:", response.text)

    except Exception as e:
        print("Webhook setup error:", e)


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    threading.Thread(
        target=setup_webhook,
        daemon=True
    ).start()

    app.run(
        host="0.0.0.0",
        port=PORT
    )
