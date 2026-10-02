import os
import json
import time
import threading
import requests
import numpy as np
import pandas as pd

from datetime import datetime, timezone
from flask import Flask, request


# =========================================================
# 🥇 GOLD SMART V5.9
# SMART SMC + SMART TREND ENGINE
#
# XAU/USD
# Telegram Webhook
# XAUS.COM DATA
#
# AUTO TRADING = OFF
# RISK = 1%
# CLOSED CANDLES = ON
# =========================================================


APP_NAME = "GOLD SMART V5.9"

# =========================================================
# API
# =========================================================

XAUS_SPOT_URL = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY_URL = "https://xaus.com/api/v1/intraday"

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


# =========================================================
# SETTINGS
# =========================================================

AUTO_TRADING = False

RISK_PERCENT = 1.0
DEFAULT_DEPOSIT = 800.0

MIN_SCORE = 70
EARLY_SCORE = 60

SL_DISTANCE = 5.0
TP_DISTANCE = 15.0

BE_TRIGGER = 6.0

POLL_SECONDS = 60
COOLDOWN_MIN = 15
CACHE_SECONDS = 45

CLOSED_CANDLES = True

STATS_FILE = "gold_stats.json"


# =========================================================
# APP
# =========================================================

app = Flask(__name__)


# =========================================================
# GLOBAL STATE
# =========================================================

price_cache = {
    "price": None,
    "time": 0
}

intraday_cache = {
    "df": None,
    "time": 0
}

virtual_trade = None

last_signal_time = 0


# =========================================================
# TELEGRAM
# =========================================================

def telegram_url(method):

    return (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )


def send_telegram(message):

    if not BOT_TOKEN or not CHAT_ID:

        print("Telegram credentials missing")

        return False

    try:

        response = requests.post(
            telegram_url("sendMessage"),
            json={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=15
        )

        if response.status_code != 200:

            print(
                "Telegram error:",
                response.text
            )

            return False

        return True

    except Exception as e:

        print(
            "Telegram exception:",
            repr(e)
        )

        return False


# =========================================================
# STATISTICS
# =========================================================

def default_stats():

    return {

        "total_signals": 0,

        "win": 0,
        "loss": 0,
        "be": 0,
        "open": 0,

        "full_signals": 0,
        "full_win": 0,
        "full_loss": 0,

        "early_signals": 0,
        "early_win": 0,
        "early_loss": 0,

        "trades": []
    }


def load_stats():

    if not os.path.exists(STATS_FILE):

        return default_stats()

    try:

        with open(
            STATS_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        base = default_stats()

        base.update(data)

        return base

    except Exception as e:

        print(
            "Stats load error:",
            repr(e)
        )

        return default_stats()


def save_stats(data):

    try:

        with open(
            STATS_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )

    except Exception as e:

        print(
            "Stats save error:",
            repr(e)
        )


stats = load_stats()


# =========================================================
# SPOT PRICE
# =========================================================

def fetch_spot(force=False):

    now = time.time()

    if (
        not force
        and price_cache["price"] is not None
        and now - price_cache["time"] < CACHE_SECONDS
    ):

        return price_cache["price"]

    try:

        response = requests.get(
            XAUS_SPOT_URL,
            timeout=15
        )

        response.raise_for_status()

        data = response.json()

        price = None

        if isinstance(data, dict):

            xau = data.get("xau")

            if isinstance(xau, dict):

                price = xau.get("price")

            if price is None:

                price = data.get(
                    "spot_usd_oz"
                )

        if price is None:

            raise ValueError(
                f"Unknown spot response: {data}"
            )

        price = float(price)

        price_cache["price"] = price
        price_cache["time"] = now

        return price

    except Exception as e:

        print(
            "Spot error:",
            repr(e)
        )

        if price_cache["price"] is not None:

            return price_cache["price"]

        return None


# =========================================================
# INTRADAY DATA
# =========================================================

def fetch_intraday(force=False):

    now = time.time()

    if (
        not force
        and intraday_cache["df"] is not None
        and now - intraday_cache["time"] < CACHE_SECONDS
    ):

        return intraday_cache["df"].copy()

    try:

        params = {
            "symbol": "xau",
            "hours": 48
        }

        response = requests.get(
            XAUS_INTRADAY_URL,
            params=params,
            timeout=20
        )

        response.raise_for_status()

        data = response.json()

        points = None

        if isinstance(data, dict):

            for key in [
                "points",
                "data",
                "series"
            ]:

                if isinstance(
                    data.get(key),
                    list
                ):

                    points = data[key]

                    break

        elif isinstance(data, list):

            points = data

        if not points:

            raise ValueError(
                "No intraday data"
            )

        rows = []

        for item in points:

            if not isinstance(item, dict):
                continue

            timestamp = (
                item.get("t")
                or item.get("timestamp")
                or item.get("time")
                or item.get("date")
            )

            price = (
                item.get("p")
                or item.get("price")
                or item.get("close")
            )

            if (
                timestamp is None
                or price is None
            ):

                continue

            try:

                if isinstance(
                    timestamp,
                    (int, float)
                ):

                    if timestamp > 100000000000:

                        dt = pd.to_datetime(
                            timestamp,
                            unit="ms",
                            utc=True
                        )

                    else:

                        dt = pd.to_datetime(
                            timestamp,
                            unit="s",
                            utc=True
                        )

                else:

                    dt = pd.to_datetime(
                        timestamp,
                        utc=True
                    )

                rows.append(
                    {
                        "time": dt,
                        "price": float(price)
                    }
                )

            except Exception:

                continue

        if len(rows) < 30:

            raise ValueError(
                f"Not enough data: {len(rows)}"
            )

        df = pd.DataFrame(rows)

        df = df.sort_values("time")

        df = df.drop_duplicates(
            "time"
        )

        df = df.set_index("time")

        ohlc = (
            df["price"]
            .resample("1min")
            .ohlc()
        )

        ohlc = ohlc.dropna()

        if (
            CLOSED_CANDLES
            and len(ohlc) > 2
        ):

            ohlc = ohlc.iloc[:-1]

        intraday_cache["df"] = ohlc.copy()

        intraday_cache["time"] = now

        return ohlc

    except Exception as e:

        print(
            "Intraday error:",
            repr(e)
        )

        if intraday_cache["df"] is not None:

            return intraday_cache["df"].copy()

        return None


# =========================================================
# RESAMPLE
# =========================================================

def resample_ohlc(df, timeframe):

    if df is None or len(df) < 10:

        return None

    rules = {

        "M5": "5min",
        "M15": "15min",
        "H1": "1h",
        "H4": "4h"
    }

    if timeframe not in rules:

        return None

    result = (
        df
        .resample(rules[timeframe])
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last"
            }
        )
        .dropna()
    )

    if (
        CLOSED_CANDLES
        and len(result) > 2
    ):

        result = result.iloc[:-1]

    return result


# =========================================================
# INDICATORS
# =========================================================

def add_indicators(df):

    if df is None or len(df) < 20:

        return None

    x = df.copy()

    x["ema20"] = (
        x["close"]
        .ewm(
            span=20,
            adjust=False
        )
        .mean()
    )

    x["ema50"] = (
        x["close"]
        .ewm(
            span=50,
            adjust=False
        )
        .mean()
    )

    x["ema200"] = (
        x["close"]
        .ewm(
            span=200,
            adjust=False
        )
        .mean()
    )

    delta = x["close"].diff()

    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )

    avg_gain = (
        gain
        .rolling(14)
        .mean()
    )

    avg_loss = (
        loss
        .rolling(14)
        .mean()
    )

    rs = (
        avg_gain
        / avg_loss.replace(
            0,
            np.nan
        )
    )

    x["rsi"] = (
        100
        - (
            100
            / (1 + rs)
        )
    )

    x["rsi"] = (
        x["rsi"]
        .fillna(50)
    )

    return x


# =========================================================
# MARKET STRUCTURE
# =========================================================

def structure_direction(
    df,
    lookback=30
):

    if df is None:

        return "MIXED"

    if len(df) < 15:

        return "MIXED"

    x = df.tail(
        lookback
    )

    highs = []
    lows = []

    high_values = x["high"].values
    low_values = x["low"].values

    for i in range(
        2,
        len(x) - 2
    ):

        is_swing_high = (
            high_values[i]
            > high_values[i - 1]
            and
            high_values[i]
            > high_values[i - 2]
            and
            high_values[i]
            > high_values[i + 1]
            and
            high_values[i]
            > high_values[i + 2]
        )

        is_swing_low = (
            low_values[i]
            < low_values[i - 1]
            and
            low_values[i]
            < low_values[i - 2]
            and
            low_values[i]
            < low_values[i + 1]
            and
            low_values[i]
            < low_values[i + 2]
        )

        if is_swing_high:

            highs.append(
                high_values[i]
            )

        if is_swing_low:

            lows.append(
                low_values[i]
            )

    if (
        len(highs) < 2
        or len(lows) < 2
    ):

        return "MIXED"

    previous_high = highs[-2]
    latest_high = highs[-1]

    previous_low = lows[-2]
    latest_low = lows[-1]

    higher_high = (
        latest_high
        > previous_high
    )

    higher_low = (
        latest_low
        > previous_low
    )

    lower_high = (
        latest_high
        < previous_high
    )

    lower_low = (
        latest_low
        < previous_low
    )

    if (
        higher_high
        and higher_low
    ):

        return "BULLISH"

    if (
        lower_high
        and lower_low
    ):

        return "BEARISH"

    return "MIXED"


# =========================================================
# MOMENTUM
# =========================================================

def momentum_from_df(df):

    if df is None:

        return "MIXED"

    if len(df) < 6:

        return "MIXED"

    latest = float(
        df["close"].iloc[-1]
    )

    previous = float(
        df["close"].iloc[-5]
    )

    if latest > previous:

        return "BULLISH"

    if latest < previous:

        return "BEARISH"

    return "MIXED"


# =========================================================
# BOS
# =========================================================

def detect_bos(
    df,
    lookback=8
):

    if df is None:

        return "NONE"

    if len(df) < lookback + 2:

        return "NONE"

    previous = df.iloc[
        -lookback - 1:-1
    ]

    latest = df.iloc[-1]

    previous_high = (
        previous["high"].max()
    )

    previous_low = (
        previous["low"].min()
    )

    if (
        latest["close"]
        > previous_high
    ):

        return "BULLISH BOS"

    if (
        latest["close"]
        < previous_low
    ):

        return "BEARISH BOS"

    return "NONE"


# =========================================================
# LIQUIDITY SWEEP
# =========================================================

def detect_liquidity(
    df,
    lookback=12
):

    if df is None:

        return "NONE"

    if len(df) < lookback + 2:

        return "NONE"

    previous = df.iloc[
        -lookback - 1:-1
    ]

    latest = df.iloc[-1]

    previous_high = (
        previous["high"].max()
    )

    previous_low = (
        previous["low"].min()
    )

    # BSL sweep
    if (
        latest["high"]
        > previous_high
        and
        latest["close"]
        < previous_high
    ):

        return "BSL SWEEP"

    # SSL sweep
    if (
        latest["low"]
        < previous_low
        and
        latest["close"]
        > previous_low
    ):

        return "SSL SWEEP"

    return "NONE"


# =========================================================
# FVG
# =========================================================

def detect_fvg(df):

    if df is None:

        return "NONE"

    if len(df) < 5:

        return "NONE"

    first = df.iloc[-3]

    latest = df.iloc[-1]

    # Bullish FVG
    if (
        latest["low"]
        > first["high"]
    ):

        return "BULLISH FVG"

    # Bearish FVG
    if (
        latest["high"]
        < first["low"]
    ):

        return "BEARISH FVG"

    return "NONE"


# =========================================================
# DISPLACEMENT
# =========================================================

def detect_displacement(df):

    if df is None:

        return "NONE"

    if len(df) < 12:

        return "NONE"

    bodies = (
        df["close"]
        - df["open"]
    ).abs()

    average_body = (
        bodies
        .iloc[-11:-1]
        .mean()
    )

    latest_body = (
        bodies.iloc[-1]
    )

    if average_body <= 0:

        return "NONE"

    if (
        latest_body
        >= average_body * 1.8
    ):

        if (
            df["close"].iloc[-1]
            >
            df["open"].iloc[-1]
        ):

            return "BULLISH"

        if (
            df["close"].iloc[-1]
            <
            df["open"].iloc[-1]
        ):

            return "BEARISH"

    return "NONE"


# =========================================================
# PREMIUM / DISCOUNT
# =========================================================

def detect_zone(df):

    if df is None:

        return "MID"

    if len(df) < 20:

        return "MID"

    recent = df.tail(20)

    highest = (
        recent["high"].max()
    )

    lowest = (
        recent["low"].min()
    )

    midpoint = (
        highest + lowest
    ) / 2

    price = float(
        df["close"].iloc[-1]
    )

    if price < midpoint:

        return "DISCOUNT"

    if price > midpoint:

        return "PREMIUM"

    return "MID"


# =========================================================
# 🧠 SMART TREND ENGINE
# =========================================================

def trend_score(df):

    if df is None:

        return {
            "direction": "MIXED",
            "score": 50,
            "structure": "MIXED",
            "momentum": "MIXED",
            "bos": "NONE"
        }

    if len(df) < 25:

        return {
            "direction": "MIXED",
            "score": 50,
            "structure": "MIXED",
            "momentum": "MIXED",
            "bos": "NONE"
        }

    x = add_indicators(df)

    if x is None:

        return {
            "direction": "MIXED",
            "score": 50,
            "structure": "MIXED",
            "momentum": "MIXED",
            "bos": "NONE"
        }

    latest = x.iloc[-1]

    structure = structure_direction(x)

    momentum = momentum_from_df(x)

    bos = detect_bos(x)

    bullish_points = 0
    bearish_points = 0

    # =====================================================
    # STRUCTURE = 40
    # =====================================================

    if structure == "BULLISH":

        bullish_points += 40

    elif structure == "BEARISH":

        bearish_points += 40

    # =====================================================
    # EMA STRUCTURE = 25
    # =====================================================

    close = float(
        latest["close"]
    )

    ema20 = float(
        latest["ema20"]
    )

    ema50 = float(
        latest["ema50"]
    )

    ema200 = float(
        latest["ema200"]
    )

    # Full bullish alignment
    if (
        close
        > ema20
        > ema50
        > ema200
    ):

        bullish_points += 25

    # Full bearish alignment
    elif (
        close
        < ema20
        < ema50
        < ema200
    ):

        bearish_points += 25

    # Partial bullish alignment
    elif (
        close > ema50
        and ema20 > ema50
    ):

        bullish_points += 15

    # Partial bearish alignment
    elif (
        close < ema50
        and ema20 < ema50
    ):

        bearish_points += 15

    # =====================================================
    # MOMENTUM = 15
    # =====================================================

    if momentum == "BULLISH":

        bullish_points += 15

    elif momentum == "BEARISH":

        bearish_points += 15

    # =====================================================
    # BOS = 20
    # =====================================================

    if bos == "BULLISH BOS":

        bullish_points += 20

    elif bos == "BEARISH BOS":

        bearish_points += 20

    # =====================================================
    # FINAL TREND SCORE
    # =====================================================

    if (
        bullish_points == 0
        and bearish_points == 0
    ):

        return {
            "direction": "MIXED",
            "score": 50,
            "structure": structure,
            "momentum": momentum,
            "bos": bos
        }

    if (
        bullish_points
        > bearish_points
    ):

        score = (
            50
            +
            (
                bullish_points
                - bearish_points
            ) / 2
        )

        score = max(
            0,
            min(
                100,
                round(score)
            )
        )

        direction = (
            "BULLISH"
            if score >= 60
            else "MIXED"
        )

    elif (
        bearish_points
        > bullish_points
    ):

        score = (
            50
            +
            (
                bearish_points
                - bullish_points
            ) / 2
        )

        score = max(
            0,
            min(
                100,
                round(score)
            )
        )

        direction = (
            "BEARISH"
            if score >= 60
            else "MIXED"
        )

    else:

        score = 50
        direction = "MIXED"

    return {

        "direction": direction,

        "score": score,

        "structure": structure,

        "momentum": momentum,

        "bos": bos
    }


def trend_from_df(df):

    return trend_score(
        df
    )["direction"]


# =========================================================
# BUILD MARKET
# =========================================================

def build_market_data():

    raw = fetch_intraday()

    if raw is None:

        return None

    market = {}

    for timeframe in [
        "M5",
        "M15",
        "H1",
        "H4"
    ]:

        dataframe = resample_ohlc(
            raw,
            timeframe
        )

        if (
            dataframe is None
            or len(dataframe) < 20
        ):

            return None

        dataframe = add_indicators(
            dataframe
        )

        if dataframe is None:

            return None

        market[timeframe] = dataframe

    return market


# =========================================================
# SIGNAL ENGINE
# =========================================================

def score_signal(market):

    h4 = market["H4"]
    h1 = market["H1"]
    m15 = market["M15"]
    m5 = market["M5"]

    trend_h4 = trend_score(h4)
    trend_h1 = trend_score(h1)
    trend_m15 = trend_score(m15)
    trend_m5 = trend_score(m5)

    trend_data = {

        "H4": trend_h4,
        "H1": trend_h1,
        "M15": trend_m15,
        "M5": trend_m5
    }

    buy_score = 0.0
    sell_score = 0.0

    # =====================================================
    # TREND WEIGHTS
    # =====================================================

    weights = {

        "H4": 20,
        "H1": 15,
        "M15": 15,
        "M5": 10
    }

    for timeframe, data in trend_data.items():

        contribution = (
            data["score"]
            / 100
        ) * weights[timeframe]

        if (
            data["direction"]
            == "BULLISH"
        ):

            buy_score += contribution

        elif (
            data["direction"]
            == "BEARISH"
        ):

            sell_score += contribution

    # =====================================================
    # M5 ENTRY ENGINE
    # =====================================================

    liquidity = detect_liquidity(m5)

    bos = detect_bos(m5)

    fvg = detect_fvg(m5)

    displacement = (
        detect_displacement(m5)
    )

    momentum = (
        momentum_from_df(m5)
    )

    zone = detect_zone(m5)

    rsi = float(
        m5["rsi"].iloc[-1]
    )

    # =====================================================
    # LIQUIDITY
    # =====================================================

    if liquidity == "SSL SWEEP":

        buy_score += 10

    elif liquidity == "BSL SWEEP":

        sell_score += 10

    # =====================================================
    # BOS
    # =====================================================

    if bos == "BULLISH BOS":

        buy_score += 15

    elif bos == "BEARISH BOS":

        sell_score += 15

    # =====================================================
    # FVG
    # =====================================================

    if fvg == "BULLISH FVG":

        buy_score += 5

    elif fvg == "BEARISH FVG":

        sell_score += 5

    # =====================================================
    # DISPLACEMENT
    # =====================================================

    if displacement == "BULLISH":

        buy_score += 10

    elif displacement == "BEARISH":

        sell_score += 10

    # =====================================================
    # MOMENTUM
    # =====================================================

    if momentum == "BULLISH":

        buy_score += 5

    elif momentum == "BEARISH":

        sell_score += 5

    # =====================================================
    # RSI
    # =====================================================

    if rsi < 35:

        buy_score += 5

    elif rsi > 65:

        sell_score += 5

    # =====================================================
    # PREMIUM / DISCOUNT
    # =====================================================

    if zone == "DISCOUNT":

        buy_score += 3

    elif zone == "PREMIUM":

        sell_score += 3

    # =====================================================
    # HIGHER TIMEFRAME BIAS
    # =====================================================

    if (
        trend_h4["direction"]
        == "BULLISH"
        and
        trend_h1["direction"]
        == "BULLISH"
    ):

        higher_bias = "BULLISH"

    elif (
        trend_h4["direction"]
        == "BEARISH"
        and
        trend_h1["direction"]
        == "BEARISH"
    ):

        higher_bias = "BEARISH"

    else:

        higher_bias = "MIXED"

    # =====================================================
    # DOMINANT SIDE
    # =====================================================

    if buy_score > sell_score:

        dominant = "BUY"

        dominant_score = buy_score

    elif sell_score > buy_score:

        dominant = "SELL"

        dominant_score = sell_score

    else:

        dominant = "NONE"

        dominant_score = 0

    # =====================================================
    # FULL SIGNAL
    # =====================================================

    signal = "WAIT"

    if (
        dominant == "BUY"
        and buy_score >= MIN_SCORE
        and higher_bias == "BULLISH"
        and (
            liquidity == "SSL SWEEP"
            or
            bos == "BULLISH BOS"
            or
            displacement == "BULLISH"
        )
    ):

        signal = "BUY"

    elif (
        dominant == "SELL"
        and sell_score >= MIN_SCORE
        and higher_bias == "BEARISH"
        and (
            liquidity == "BSL SWEEP"
            or
            bos == "BEARISH BOS"
            or
            displacement == "BEARISH"
        )
    ):

        signal = "SELL"

    # =====================================================
    # EARLY SIGNAL
    # =====================================================

    elif (
        dominant == "BUY"
        and buy_score >= EARLY_SCORE
        and higher_bias != "BEARISH"
        and (
            liquidity == "SSL SWEEP"
            or
            bos == "BULLISH BOS"
        )
    ):

        signal = "EARLY BUY"

    elif (
        dominant == "SELL"
        and sell_score >= EARLY_SCORE
        and higher_bias != "BULLISH"
        and (
            liquidity == "BSL SWEEP"
            or
            bos == "BEARISH BOS"
        )
    ):

        signal = "EARLY SELL"

    return {

        "signal": signal,

        "buy_score": round(
            buy_score,
            1
        ),

        "sell_score": round(
            sell_score,
            1
        ),

        "dominant": dominant,

        "dominant_score": round(
            dominant_score,
            1
        ),

        "higher_bias": higher_bias,

        "trend_data": trend_data,

        "liquidity": liquidity,

        "bos": bos,

        "fvg": fvg,

        "displacement": displacement,

        "momentum": momentum,

        "zone": zone,

        "rsi": round(
            rsi,
            1
        )
    }


# =========================================================
# TREND DISPLAY
# =========================================================

def trend_line(
    timeframe,
    data
):

    return (
        f"📊 {timeframe}: "
        f"{data['direction']} "
        f"({data['score']}/100)"
    )


# =========================================================
# SIGNAL MESSAGE
# =========================================================

def build_signal_message(
    price,
    analysis
):

    trends = analysis[
        "trend_data"
    ]

    signal = analysis[
        "signal"
    ]

    if signal == "BUY":

        emoji = "🟢"

    elif signal == "SELL":

        emoji = "🔴"

    elif signal == "EARLY BUY":

        emoji = "🟡"

    elif signal == "EARLY SELL":

        emoji = "🟠"

    else:

        emoji = "⚪"

    return f"""
🥇 {APP_NAME}

{emoji} SIGNAL: {signal}

💰 XAUUSD: {price:.2f}

━━━━━━━━━━━━━━━━━━

{trend_line("H4", trends["H4"])}
{trend_line("H1", trends["H1"])}
{trend_line("M15", trends["M15"])}
{trend_line("M5", trends["M5"])}

🧭 HIGHER TF BIAS:
{analysis["higher_bias"]}

━━━━━━━━━━━━━━━━━━

💧 M5 Liquidity:
{analysis["liquidity"]}

🔨 M5 BOS:
{analysis["bos"]}

🧩 M5 FVG:
{analysis["fvg"]}

💥 M5 Displacement:
{analysis["displacement"]}

📈 M5 Momentum:
{analysis["momentum"]}

🎯 M5 Zone:
{analysis["zone"]}

📊 RSI:
{analysis["rsi"]}

━━━━━━━━━━━━━━━━━━

🟢 BUY SCORE:
{analysis["buy_score"]}

🔴 SELL SCORE:
{analysis["sell_score"]}

🎯 MIN SCORE:
{MIN_SCORE}

━━━━━━━━━━━━━━━━━━

📡 SOURCE:
XAUS

🕯 CLOSED CANDLES:
{"ON" if CLOSED_CANDLES else "OFF"}

🛡 AUTO TRADING:
{"ON" if AUTO_TRADING else "OFF"}

📉 RISK:
{RISK_PERCENT:.1f}%
""".strip()


# =========================================================
# OPEN VIRTUAL TRADE
# =========================================================

def open_virtual_trade(
    direction,
    price,
    signal_type
):

    global virtual_trade

    if virtual_trade is not None:

        return

    if direction not in [
        "BUY",
        "SELL"
    ]:

        return

    if signal_type not in [
        "BUY",
        "SELL"
    ]:

        return

    if direction == "BUY":

        sl = (
            price
            - SL_DISTANCE
        )

        tp = (
            price
            + TP_DISTANCE
        )

    else:

        sl = (
            price
            + SL_DISTANCE
        )

        tp = (
            price
            - TP_DISTANCE
        )

    virtual_trade = {

        "direction": direction,

        "signal_type": signal_type,

        "entry": round(
            price,
            2
        ),

        "sl": round(
            sl,
            2
        ),

        "tp": round(
            tp,
            2
        ),

        "original_sl": round(
            sl,
            2
        ),

        "be": False,

        "opened_at":
            datetime.now(
                timezone.utc
            ).isoformat()
    }

    stats["open"] = 1

    save_stats(stats)


# =========================================================
# CLOSE VIRTUAL TRADE
# =========================================================

def close_virtual_trade(
    result
):

    global virtual_trade

    if virtual_trade is None:

        return

    signal_type = (
        virtual_trade[
            "signal_type"
        ]
    )

    stats["open"] = 0

    if result == "WIN":

        stats["win"] += 1

    elif result == "LOSS":

        stats["loss"] += 1

    elif result == "BE":

        stats["be"] += 1

    if signal_type in [
        "BUY",
        "SELL"
    ]:

        if result == "WIN":

            stats["full_win"] += 1

        elif result == "LOSS":

            stats["full_loss"] += 1

    trade_record = {

        "direction":
            virtual_trade["direction"],

        "signal_type":
            signal_type,

        "entry":
            virtual_trade["entry"],

        "sl":
            virtual_trade["sl"],

        "tp":
            virtual_trade["tp"],

        "result":
            result,

        "closed_at":
            datetime.now(
                timezone.utc
            ).isoformat()
    }

    stats[
        "trades"
    ].append(
        trade_record
    )

    virtual_trade = None

    save_stats(stats)


# =========================================================
# UPDATE VIRTUAL TRADE
# =========================================================

def update_virtual_trade(
    price
):

    global virtual_trade

    if virtual_trade is None:

        return None

    direction = (
        virtual_trade[
            "direction"
        ]
    )

    entry = (
        virtual_trade[
            "entry"
        ]
    )

    tp = (
        virtual_trade[
            "tp"
        ]
    )

    # =====================================================
    # BUY
    # =====================================================

    if direction == "BUY":

        profit_move = (
            price - entry
        )

        if (
            not virtual_trade["be"]
            and
            profit_move >= BE_TRIGGER
        ):

            virtual_trade["sl"] = entry

            virtual_trade["be"] = True

            save_stats(stats)

            send_telegram(
                f"""
🛡 GOLD SMART V5.9

🔒 BREAK EVEN

🟢 BUY

💰 Entry:
{entry:.2f}

📍 Current:
{price:.2
