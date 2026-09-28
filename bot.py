import os
import time
import threading
import requests
import pandas as pd
import numpy as np
import yfinance as yf

from flask import Flask
import telebot


# =========================================================
# SETTINGS
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")

app = Flask(__name__)

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode="HTML"
)

GOLD_API = "https://gold-api.com/api/XAU/USD"


# =========================================================
# WEB
# =========================================================

@app.route("/")
def home():
    return "GOLD SMART V3.4 ONLINE", 200


@app.route("/health")
def health():
    return "OK", 200


# =========================================================
# GOLD PRICE
# =========================================================

def get_spot_price():

    try:

        r = requests.get(
            GOLD_API,
            timeout=10
        )

        data = r.json()

        # Поддерживаем разные варианты ответа API
        for key in ["price", "value", "ask", "close"]:

            if key in data:

                try:
                    return float(data[key])
                except:
                    pass

        # Иногда API возвращает nested data
        if isinstance(data.get("data"), dict):

            for key in ["price", "value", "ask", "close"]:

                if key in data["data"]:

                    try:
                        return float(data["data"][key])
                    except:
                        pass

    except Exception as e:

        print("SPOT ERROR:", repr(e))

    return None


# =========================================================
# MARKET DATA
# =========================================================

def get_candles(interval):

    try:

        period = "60d"

        if interval == "1h":
            period = "730d"

        df = yf.download(
            "GC=F",
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False
        )

        if df is None or df.empty:
            return None

        # Иногда Yahoo возвращает MultiIndex
        if isinstance(df.columns, pd.MultiIndex):

            df.columns = df.columns.get_level_values(0)

        needed = [
            "Open",
            "High",
            "Low",
            "Close"
        ]

        df = df[needed].copy()

        df = df.dropna()

        return df

    except Exception as e:

        print("CANDLE ERROR:", interval, repr(e))

        return None


# =========================================================
# INDICATORS
# =========================================================

def add_indicators(df):

    df = df.copy()

    df["EMA50"] = (
        df["Close"]
        .ewm(span=50, adjust=False)
        .mean()
    )

    df["EMA200"] = (
        df["Close"]
        .ewm(span=200, adjust=False)
        .mean()
    )

    delta = df["Close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    df["RSI"] = 100 - (
        100 / (1 + rs)
    )

    return df


# =========================================================
# TREND
# =========================================================

def get_trend(df):

    if df is None or len(df) < 210:
        return "UNKNOWN"

    df = add_indicators(df)

    last = df.iloc[-1]

    if (
        last["Close"] > last["EMA50"]
        and last["EMA50"] > last["EMA200"]
    ):
        return "BULLISH"

    if (
        last["Close"] < last["EMA50"]
        and last["EMA50"] < last["EMA200"]
    ):
        return "BEARISH"

    return "NEUTRAL"


# =========================================================
# MARKET STRUCTURE
# =========================================================

def structure(df):

    if df is None or len(df) < 20:
        return "UNKNOWN"

    recent = df.tail(20)

    last = recent.iloc[-1]

    previous_high = recent["High"].iloc[:-1].max()
    previous_low = recent["Low"].iloc[:-1].min()

    if last["Close"] > previous_high:
        return "BOS UP"

    if last["Close"] < previous_low:
        return "BOS DOWN"

    # Basic HH/HL and LH/LL context
    highs = recent["High"].tail(6).values
    lows = recent["Low"].tail(6).values

    if highs[-1] > highs[-3] and lows[-1] > lows[-3]:
        return "HH/HL"

    if highs[-1] < highs[-3] and lows[-1] < lows[-3]:
        return "LH/LL"

    return "RANGE"


# =========================================================
# LIQUIDITY SWEEP
# =========================================================

def liquidity_sweep(df):

    if df is None or len(df) < 15:
        return "NONE"

    recent = df.tail(15)

    last = recent.iloc[-1]

    previous = recent.iloc[:-1]

    previous_high = previous["High"].max()
    previous_low = previous["Low"].min()

    # Sweep sell-side liquidity
    if (
        last["Low"] < previous_low
        and last["Close"] > previous_low
    ):
        return "SELL-SIDE SWEEP"

    # Sweep buy-side liquidity
    if (
        last["High"] > previous_high
        and last["Close"] < previous_high
    ):
        return "BUY-SIDE SWEEP"

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
    if a["High"] < c["Low"]:
        return "BULLISH FVG"

    # Bearish FVG
    if a["Low"] > c["High"]:
        return "BEARISH FVG"

    return "NONE"


# =========================================================
# SCORE
# =========================================================

def calculate_signal(h4, h1, m15):

    score_buy = 0
    score_sell = 0

    # H4
    if h4 == "BULLISH":
        score_buy += 2

    elif h4 == "BEARISH":
        score_sell += 2

    # H1
    if h1 == "BULLISH":
        score_buy += 2

    elif h1 == "BEARISH":
        score_sell += 2

    # M15
    if m15["trend"] == "BULLISH":
        score_buy += 1

    elif m15["trend"] == "BEARISH":
        score_sell += 1

    # Structure
    if m15["structure"] == "BOS UP":
        score_buy += 2

    elif m15["structure"] == "BOS DOWN":
        score_sell += 2

    # Liquidity
    if m15["sweep"] == "SELL-SIDE SWEEP":
        score_buy += 1

    elif m15["sweep"] == "BUY-SIDE SWEEP":
        score_sell += 1

    # FVG
    if m15["fvg"] == "BULLISH FVG":
        score_buy += 1

    elif m15["fvg"] == "BEARISH FVG":
        score_sell += 1

    if score_buy >= 7 and score_buy > score_sell:

        return "BUY", score_buy, score_sell

    if score_sell >= 7 and score_sell > score_buy:

        return "SELL", score_buy, score_sell

    return "WAIT", score_buy, score_sell


# =========================================================
# TRADE LEVELS
# =========================================================

def calculate_levels(df, signal):

    if df is None or len(df) < 20:
        return None

    recent = df.tail(20)

    price = float(recent["Close"].iloc[-1])

    atr = (
        recent["High"] - recent["Low"]
    ).rolling(14).mean().iloc[-1]

    if pd.isna(atr) or atr <= 0:
        return None

    # Conservative ATR-based levels
    sl_distance = max(
        atr * 1.5,
        price * 0.0015
    )

    if signal == "BUY":

        entry = price
        sl = price - sl_distance
        tp1 = price + sl_distance * 1.0
        tp2 = price + sl_distance * 1.8

    elif signal == "SELL":

        entry = price
        sl = price + sl_distance
        tp1 = price - sl_distance * 1.0
        tp2 = price - sl_distance * 1.8

    else:

        return {
            "entry": price,
            "sl": None,
            "tp1": None,
            "tp2": None
        }

    return {
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2
    }


# =========================================================
# ANALYSIS
# =========================================================

def analyze_gold():

    print("🥇 Starting GOLD analysis...")

    h4_df = get_candles("1h")
    h1_df = get_candles("1h")
    m15_df = get_candles("15m")

    # H4 approximation from 1H data
    if h4_df is not None and len(h4_df) >= 4:

        h4 = (
            h4_df
            .resample("4h")
            .agg({
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last"
            })
            .dropna()
        )

    else:

        h4 = None

    h4_trend = get_trend(h4)
    h1_trend = get_trend(h1_df)

    m15_trend = get_trend(m15_df)

    m15_structure = structure(m15_df)
    m15_sweep = liquidity_sweep(m15_df)
    m15_fvg = detect_fvg(m15_df)

    m15_data = {
        "trend": m15_trend,
        "structure": m15_structure,
        "sweep": m15_sweep,
        "fvg": m15_fvg
    }

    signal, buy_score, sell_score = calculate_signal(
        h4_trend,
        h1_trend,
        m15_data
    )

    levels = calculate_levels(
        m15_df,
        signal
    )

    spot = get_spot_price()

    if spot is None and levels:
        spot = levels["entry"]

    return {
        "price": spot,
        "h4": h4_trend,
        "h1": h1_trend,
        "m15": m15_trend,
        "structure": m15_structure,
        "sweep": m15_sweep,
        "fvg": m15_fvg,
        "signal": signal,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "levels": levels
    }


# =========================================================
# TELEGRAM MESSAGE
# =========================================================

def format_analysis(data):

    signal = data["signal"]

    if signal == "BUY":

        icon = "🟢"
        title = "BUY"

    elif signal == "SELL":

        icon = "🔴"
        title = "SELL"

    else:

        icon = "🟡"
        title = "WAIT"

    price = data["price"]

    levels = data["levels"]

    text = (
        "🥇 <b>GOLD SMART V3.4</b>\n\n"
        f"💰 Цена: <b>{price:.2f}</b>\n\n"
        f"📊 H4: <b>{data['h4']}</b>\n"
        f"📊 H1: <b>{data['h1']}</b>\n"
        f"📊 M15: <b>{data['m15']}</b>\n\n"
        f"💧 Liquidity: <b>{data['sweep']}</b>\n"
        f"🔀 Structure: <b>{data['structure']}</b>\n"
        f"📦 FVG: <b>{data['fvg']}</b>\n\n"
        f"{icon} <b>SIGNAL: {title}</b>\n"
        f"⭐ BUY score: {data['buy_score']}/10+\n"
        f"⭐ SELL score: {data['sell_score']}/10+\n"
    )

    if signal in ["BUY", "SELL"] and levels:

        text += (
            "\n🎯 <b>TRADE PLAN</b>\n"
            f"ENTRY: <b>{levels['entry']:.2f}</b>\n"
            f"SL: <b>{levels['sl']:.2f}</b>\n"
            f"TP1: <b>{levels['tp1']:.2f}</b>\n"
            f"TP2: <b>{levels['tp2']:.2f}</b>\n"
        )

    else:

        text += (
            "\n⏸ <b>Нет достаточного подтверждения.</b>\n"
            "Лучше ждать следующего движения."
        )

    text += (
        "\n\n⚠️ <b>Важно:</b> это аналитический сигнал, "
        "не автоматическая торговля."
    )

    return text


# =========================================================
# TELEGRAM COMMANDS
# =========================================================

@bot.message_handler(commands=["start"])
def start(message):

    bot.send_message(
        message.chat.id,
        "🟢 <b>GOLD SMART V3.4</b>\n\n"
        "Бот работает.\n\n"
        "🥇 /gold — анализ XAUUSD\n"
        "📊 /status — проверка системы"
    )


@bot.message_handler(commands=["status"])
def status(message):

    bot.send_message(
        message.chat.id,
        "🟢 <b>GOLD SMART ONLINE</b>\n\n"
        "Telegram: OK\n"
        "Render: OK\n"
        "Polling: OK\n"
        "Analysis Engine: V3.4"
    )


@bot.message_handler(commands=["gold"])
def gold(message):

    bot.send_message(
        message.chat.id,
        "⏳ <b>Анализ XAUUSD...</b>\n\n"
        "Проверяю H4 → H1 → M15..."
    )

    try:

        data = analyze_gold()

        result = format_analysis(data)

        bot.send_message(
            message.chat.id,
            result
        )

    except Exception as e:

        print("ANALYSIS ERROR:", repr(e))

        bot.send_message(
            message.chat.id,
            "⚠️ Не удалось получить данные золота.\n\n"
            "Попробуй ещё раз через несколько секунд."
        )


@bot.message_handler(
    func=lambda message:
        message.text
        and message.text.lower().strip() in [
            "gold",
            "золото"
        ]
)
def gold_text(message):

    gold(message)


# =========================================================
# POLLING
# =========================================================

def polling_worker():

    print("================================")
    print("🟢 GOLD SMART V3.4 POLLING")
    print("================================")

    while True:

        try:

            bot.remove_webhook()

            time.sleep(2)

            print("🚀 Polling started")

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


threading.Thread(
    target=polling_worker,
    daemon=True
).start()


# =========================================================
# LOCAL
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
