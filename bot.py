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

# Если получишь CoinGecko Demo API key,
# добавь его в Render → Environment Variables:
# COINGECKO_API_KEY = твой ключ
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY")

app = Flask(__name__)

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode="HTML"
)


# =========================================================
# COINGECKO GOLD PAGE / CONTROL PRICE
# =========================================================

COINGECKO_GOLD_URL = "https://www.coingecko.com/en/commodities/gold"


def get_coingecko_gold():

    """
    Получаем контрольную XAU/USD цену.
    Если API key есть — пробуем API.
    Если API недоступен — возвращаем None.

    ВАЖНО:
    Это benchmark XAU, НЕ точная котировка FxPro.
    """

    try:

        if not COINGECKO_API_KEY:
            print("CoinGecko API key not configured")
            return None

        # Пробуем официальный API endpoint.
        # Для commodity XAU доступность зависит от плана/API.
        url = "https://api.coingecko.com/api/v3/simple/price"

        headers = {
            "x-cg-demo-api-key": COINGECKO_API_KEY
        }

        params = {
            "ids": "gold",
            "vs_currencies": "usd"
        }

        r = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=10
        )

        if r.status_code != 200:
            print("CoinGecko status:", r.status_code)
            return None

        data = r.json()

        if "gold" in data:
            return float(data["gold"]["usd"])

    except Exception as e:

        print("CoinGecko error:", repr(e))

    return None


# =========================================================
# FALLBACK — EXTERNAL XAU
# =========================================================

def get_xau_fallback():

    """
    Вторичный источник.
    Используется только для отображения,
    если контрольный источник недоступен.
    """

    try:

        url = "https://xaus.com/api/v1/spot"

        r = requests.get(
            url,
            timeout=10
        )

        if r.status_code != 200:
            return None

        data = r.json()

        # Возможные форматы ответа
        candidates = []

        if isinstance(data, dict):

            for key in [
                "price",
                "spot",
                "xauusd",
                "XAUUSD",
                "value"
            ]:

                if key in data:
                    candidates.append(data[key])

            if isinstance(data.get("data"), dict):

                for key in [
                    "price",
                    "spot",
                    "xauusd",
                    "XAUUSD",
                    "value"
                ]:

                    if key in data["data"]:
                        candidates.append(
                            data["data"][key]
                        )

        for value in candidates:

            try:
                return float(value)
            except:
                pass

    except Exception as e:

        print("XAUS error:", repr(e))

    return None


# =========================================================
# HISTORICAL CANDLES
# =========================================================

def get_candles(interval):

    try:

        if interval == "15m":

            period = "60d"

        elif interval == "1h":

            period = "730d"

        else:

            period = "60d"

        df = yf.download(
            "GC=F",
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False
        )

        if df is None or df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):

            df.columns = df.columns.get_level_values(0)

        columns = [
            "Open",
            "High",
            "Low",
            "Close"
        ]

        df = df[columns].copy()

        df = df.dropna()

        return df

    except Exception as e:

        print(
            "Candle error:",
            interval,
            repr(e)
        )

        return None


# =========================================================
# INDICATORS
# =========================================================

def indicators(df):

    df = df.copy()

    df["EMA50"] = (
        df["Close"]
        .ewm(
            span=50,
            adjust=False
        )
        .mean()
    )

    df["EMA200"] = (
        df["Close"]
        .ewm(
            span=200,
            adjust=False
        )
        .mean()
    )

    return df


# =========================================================
# TREND
# =========================================================

def trend(df):

    if df is None or len(df) < 50:
        return "UNKNOWN"

    df = indicators(df)

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
# STRUCTURE
# =========================================================

def market_structure(df):

    if df is None or len(df) < 30:
        return "UNKNOWN"

    d = df.tail(30)

    last = d.iloc[-1]

    highs = d["High"].values
    lows = d["Low"].values

    previous_high = max(
        highs[:-3]
    )

    previous_low = min(
        lows[:-3]
    )

    if last["Close"] > previous_high:

        return "BOS UP"

    if last["Close"] < previous_low:

        return "BOS DOWN"

    # More conservative HH/HL
    h1 = highs[-1]
    h2 = highs[-5]

    l1 = lows[-1]
    l2 = lows[-5]

    if h1 > h2 and l1 > l2:

        return "HH/HL"

    if h1 < h2 and l1 < l2:

        return "LH/LL"

    return "RANGE"


# =========================================================
# LIQUIDITY
# =========================================================

def liquidity_sweep(df):

    if df is None or len(df) < 20:
        return "NONE"

    d = df.tail(20)

    last = d.iloc[-1]

    old_high = d["High"].iloc[:-1].max()
    old_low = d["Low"].iloc[:-1].min()

    # Sell-side liquidity swept
    if (
        last["Low"] < old_low
        and last["Close"] > old_low
    ):

        return "SELL-SIDE SWEEP"

    # Buy-side liquidity swept
    if (
        last["High"] > old_high
        and last["Close"] < old_high
    ):

        return "BUY-SIDE SWEEP"

    return "NONE"


# =========================================================
# FVG
# =========================================================

def fvg(df):

    if df is None or len(df) < 5:
        return "NONE"

    a = df.iloc[-3]
    c = df.iloc[-1]

    # Bullish imbalance
    if a["High"] < c["Low"]:

        return "BULLISH FVG"

    # Bearish imbalance
    if a["Low"] > c["High"]:

        return "BEARISH FVG"

    return "NONE"


# =========================================================
# SIGNAL ENGINE
# =========================================================

def signal_engine(
    h4,
    h1,
    m15,
    structure,
    sweep,
    fvg_type
):

    buy = 0
    sell = 0

    # H4 = strongest
    if h4 == "BULLISH":
        buy += 3

    elif h4 == "BEARISH":
        sell += 3

    # H1
    if h1 == "BULLISH":
        buy += 2

    elif h1 == "BEARISH":
        sell += 2

    # M15
    if m15 == "BULLISH":
        buy += 1

    elif m15 == "BEARISH":
        sell += 1

    # BOS
    if structure == "BOS UP":
        buy += 3

    elif structure == "BOS DOWN":
        sell += 3

    # Liquidity
    if sweep == "SELL-SIDE SWEEP":
        buy += 2

    elif sweep == "BUY-SIDE SWEEP":
        sell += 2

    # FVG
    if fvg_type == "BULLISH FVG":
        buy += 1

    elif fvg_type == "BEARISH FVG":
        sell += 1

    # STRICT ENTRY FILTER
    if buy >= 8 and buy >= sell + 2:

        return "BUY", buy, sell

    if sell >= 8 and sell >= buy + 2:

        return "SELL", buy, sell

    return "WAIT", buy, sell


# =========================================================
# LEVELS
# =========================================================

def levels(df, signal):

    if (
        df is None
        or len(df) < 20
        or signal == "WAIT"
    ):

        return None

    d = df.tail(20)

    price = float(
        d["Close"].iloc[-1]
    )

    high = d["High"]
    low = d["Low"]
    close = d["Close"]

    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs()
        ],
        axis=1
    ).max(axis=1)

    atr = tr.rolling(14).mean().iloc[-1]

    if pd.isna(atr):

        return None

    # Conservative stop
    sl_distance = max(
        atr * 1.4,
        5.0
    )

    if signal == "BUY":

        entry = price
        sl = price - sl_distance
        tp1 = price + sl_distance * 1.5
        tp2 = price + sl_distance * 2.5

    else:

        entry = price
        sl = price + sl_distance
        tp1 = price - sl_distance * 1.5
        tp2 = price - sl_distance * 2.5

    return {
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2
    }


# =========================================================
# MAIN ANALYSIS
# =========================================================

def analyze():

    print("🥇 GOLD SMART V3.5 ANALYSIS")

    # Current control price
    control_price = get_coingecko_gold()

    # Backup
    fallback_price = get_xau_fallback()

    # Historical data
    h1_df = get_candles("1h")
    m15_df = get_candles("15m")

    if h1_df is None or m15_df is None:

        return {
            "error": "MARKET DATA UNAVAILABLE"
        }

    # H4 aggregation
    h4_df = (
        h1_df
        .resample("4h")
        .agg({
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last"
        })
        .dropna()
    )

    h4_trend = trend(h4_df)
    h1_trend = trend(h1_df)
    m15_trend = trend(m15_df)

    structure = market_structure(
        m15_df
    )

    sweep = liquidity_sweep(
        m15_df
    )

    fvg_type = fvg(
        m15_df
    )

    signal, buy_score, sell_score = signal_engine(
        h4_trend,
        h1_trend,
        m15_trend,
        structure,
        sweep,
        fvg_type
    )

    trade_levels = levels(
        m15_df,
        signal
    )

    market_price = float(
        m15_df["Close"].iloc[-1]
    )

    # =====================================================
    # PRICE VALIDATION
    # =====================================================

    if control_price is not None:

        difference = abs(
            market_price - control_price
        )

        difference_percent = (
            difference / control_price
        ) * 100

    else:

        difference = None
        difference_percent = None

    return {
        "control_price": control_price,
        "fallback_price": fallback_price,
        "market_price": market_price,
        "difference": difference,
        "difference_percent": difference_percent,

        "h4": h4_trend,
        "h1": h1_trend,
        "m15": m15_trend,

        "structure": structure,
        "sweep": sweep,
        "fvg": fvg_type,

        "signal": signal,
        "buy_score": buy_score,
        "sell_score": sell_score,

        "levels": trade_levels
    }


# =========================================================
# TELEGRAM FORMAT
# =========================================================

def format_result(data):

    if "error" in data:

        return (
            "⚠️ <b>GOLD SMART V3.5</b>\n\n"
            "🚫 MARKET DATA UNAVAILABLE\n\n"
            "Сигнал заблокирован."
        )

    control = data["control_price"]

    market = data["market_price"]

    fallback = data["fallback_price"]

    # =====================================================
    # PRICE PROTECTION
    # =====================================================

    if control is not None:

        difference = data["difference"]

        # 10 USD maximum discrepancy
        if difference > 10:

            return (
                "🚨 <b>DATA MISMATCH</b>\n\n"

                f"Контроль XAU: "
                f"<b>{control:.2f}</b>\n"

                f"Market data: "
                f"<b>{market:.2f}</b>\n\n"

                f"Разница: "
                f"<b>{difference:.2f}</b>\n\n"

                "🚫 <b>NO TRADE</b>\n\n"

                "Источник цены слишком сильно "
                "расходится."
            )

    signal = data["signal"]

    if signal == "BUY":

        emoji = "🟢"

    elif signal == "SELL":

        emoji = "🔴"

    else:

        emoji = "🟡"

    text = (
        "🥇 <b>GOLD SMART V3.5</b>\n\n"

        f"💰 XAU benchmark: "
        f"<b>{control:.2f}</b>\n"

        f"📊 Market data: "
        f"<b>{market:.2f}</b>\n"
    )

    if fallback:

        text += (
            f"🔎 Secondary: "
            f"<b>{fallback:.2f}</b>\n"
        )

    text += (
        "\n"

        f"📈 H4: <b>{data['h4']}</b>\n"
        f"📈 H1: <b>{data['h1']}</b>\n"
        f"📊 M15: <b>{data['m15']}</b>\n\n"

        f"💧 Liquidity: "
        f"<b>{data['sweep']}</b>\n"

        f"🔀 Structure: "
        f"<b>{data['structure']}</b>\n"

        f"📦 FVG: "
        f"<b>{data['fvg']}</b>\n\n"

        f"{emoji} <b>SIGNAL: {signal}</b>\n\n"

        f"BUY score: "
        f"<b>{data['buy_score']}</b>\n"

        f"SELL score: "
        f"<b>{data['sell_score']}</b>\n"
    )

    lv = data["levels"]

    if signal in ["BUY", "SELL"] and lv:

        text += (
            "\n🎯 <b>TRADE PLAN</b>\n"

            f"ENTRY: "
            f"<b>{lv['entry']:.2f}</b>\n"

            f"SL: "
            f"<b>{lv['sl']:.2f}</b>\n"

            f"TP1: "
            f"<b>{lv['tp1']:.2f}</b>\n"

            f"TP2: "
            f"<b>{lv['tp2']:.2f}</b>\n"
        )

    else:

        text += (
            "\n⏸ <b>WAIT</b>\n"
            "Нет достаточного подтверждения."
        )

    text += (
        "\n\n"
        "⚠️ Signal only — "
        "автоматической торговли нет."
    )

    return text


# =========================================================
# TELEGRAM
# =========================================================

@bot.message_handler(commands=["start"])
def start(message):

    bot.send_message(
        message.chat.id,
        "🟢 <b>GOLD SMART V3.5</b>\n\n"
        "Бот онлайн.\n\n"
        "🥇 /gold — анализ золота\n"
        "📊 /status — статус"
    )


@bot.message_handler(commands=["status"])
def status(message):

    bot.send_message(
        message.chat.id,
        "🟢 <b>GOLD SMART V3.5 ONLINE</b>\n\n"
        "Telegram: OK\n"
        "Render: OK\n"
        "Polling: OK\n"
        "Analysis: OK"
    )


@bot.message_handler(commands=["gold"])
def gold(message):

    bot.send_message(
        message.chat.id,
        "⏳ <b>Анализ XAUUSD...</b>\n\n"
        "Проверяю цену → H4 → H1 → M15..."
    )

    try:

        data = analyze()

        result = format_result(data)

        bot.send_message(
            message.chat.id,
            result
        )

    except Exception as e:

        print(
            "ANALYSIS ERROR:",
            repr(e)
        )

        bot.send_message(
            message.chat.id,
            "⚠️ Ошибка анализа.\n\n"
            "Попробуй ещё раз."
        )


@bot.message_handler(
    func=lambda message:
        message.text
        and message.text.lower().strip()
        in ["gold", "золото"]
)
def gold_text(message):

    gold(message)


# =========================================================
# POLLING
# =========================================================

def polling_worker():

    print("==============================")
    print("🟢 GOLD SMART V3.5 POLLING")
    print("==============================")

    while True:

        try:

            bot.remove_webhook()

            time.sleep(2)

            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=False,
                allowed_updates=["message"]
            )

        except Exception as e:

            print(
                "POLLING ERROR:",
                repr(e)
            )

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
