import os
import json
import time
import threading
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd
from flask import Flask, request


# ============================================================
# 🥇 GOLD SMART V5.9.4
# XAU/USD SMART SMC SIGNAL ENGINE
#
# DATA SOURCE: XAUS
# TIMEFRAMES: H4 → H1 → M15 → M5
#
# AUTO TRADING = OFF
# RISK = 1%
# CLOSED CANDLES = ON
#
# H4:
#   8+ candles  = FULL
#   2-7 candles  = LIMITED
#
# Full signal:
#   score >= 70
#
# Early signal:
#   score >= 60
#
# RR target ≈ 1:3
# ============================================================


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://gold-bot-q8la.onrender.com"
).strip()

XAUS_SPOT_URL = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY_URL = "https://xaus.com/api/v1/intraday"

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

PREFERRED_H4_CANDLES = 8

MIN_CANDLES = {
    "M5": 30,
    "M15": 16,
    "H1": 7,
    "H4": 2,
}


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# GLOBAL STATE
# ============================================================

state_lock = threading.Lock()

market_cache = {
    "timestamp": 0,
    "data": None,
}

last_signal = {
    "direction": None,
    "score": 0,
    "entry": None,
    "time": None,
}

virtual_trade = None

stats = {
    "signals": 0,
    "full_signals": 0,
    "early_signals": 0,
    "wins": 0,
    "losses": 0,
    "be": 0,
}

startup_lock = threading.Lock()
startup_done = False


# ============================================================
# BASIC HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def safe_float(value):
    try:
        if value is None:
            return None

        if isinstance(value, bool):
            return None

        number = float(value)

        if not np.isfinite(number):
            return None

        return number

    except Exception:
        return None


def normalize_timestamp(value):
    """
    Converts timestamp to pandas UTC Timestamp.
    """

    try:
        if value is None:
            return None

        if isinstance(value, (int, float)):
            # milliseconds
            if value > 10_000_000_000:
                return pd.to_datetime(value, unit="ms", utc=True)

            # seconds
            return pd.to_datetime(value, unit="s", utc=True)

        return pd.to_datetime(value, utc=True)

    except Exception:
        return None


def telegram_url(method):
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message, chat_id=None):
    if not BOT_TOKEN:
        print("Telegram error: BOT_TOKEN missing")
        return False

    target = chat_id or CHAT_ID

    if not target:
        print("Telegram error: TELEGRAM_CHAT_ID missing")
        return False

    try:
        response = requests.post(
            telegram_url("sendMessage"),
            json={
                "chat_id": target,
                "text": message,
            },
            timeout=15,
        )

        if response.status_code != 200:
            print(
                "Telegram send error:",
                response.status_code,
                response.text[:500],
            )
            return False

        return True

    except Exception as exc:
        print("Telegram exception:", repr(exc))
        return False


# ============================================================
# XAUS SPOT
# ============================================================

def fetch_spot():
    """
    XAUS current response may look like:

    {
        "xau": {
            "price": 4169.4,
            "currency": "USD",
            "unit": "troy_oz"
        },
        "spot_usd_oz": 4169.399902,
        ...
    }

    IMPORTANT:
    We explicitly support:
      payload["xau"]["price"]
      payload["spot_usd_oz"]
    """

    try:
        response = requests.get(
            XAUS_SPOT_URL,
            timeout=15,
        )

        response.raise_for_status()

        payload = response.json()

        price = None

        # ----------------------------------------------------
        # 1. CURRENT XAUS FORMAT
        # ----------------------------------------------------

        xau = payload.get("xau")

        if isinstance(xau, dict):
            price = safe_float(xau.get("price"))

        # ----------------------------------------------------
        # 2. spot_usd_oz
        # ----------------------------------------------------

        if price is None:
            price = safe_float(
                payload.get("spot_usd_oz")
            )

        # ----------------------------------------------------
        # 3. top-level price fields
        # ----------------------------------------------------

        if price is None:
            for key in (
                "price",
                "last",
                "close",
            ):
                price = safe_float(
                    payload.get(key)
                )

                if price is not None:
                    break

        # ----------------------------------------------------
        # 4. nested data
        # ----------------------------------------------------

        if price is None:
            data = payload.get("data")

            if isinstance(data, dict):

                nested_xau = data.get("xau")

                if isinstance(nested_xau, dict):
                    price = safe_float(
                        nested_xau.get("price")
                    )

                if price is None:
                    price = safe_float(
                        data.get("spot_usd_oz")
                    )

                if price is None:
                    for key in (
                        "price",
                        "last",
                        "close",
                    ):
                        price = safe_float(
                            data.get(key)
                        )

                        if price is not None:
                            break

        # ----------------------------------------------------
        # ERROR
        # ----------------------------------------------------

        if price is None:
            raise ValueError(
                f"XAUS spot price not found: {payload}"
            )

        print(
            f"XAUS spot loaded: {price:.2f}"
        )

        return price

    except Exception as exc:
        print(
            "XAUS spot error:",
            repr(exc),
        )

        return None


# ============================================================
# XAUS INTRADAY
# ============================================================

def fetch_intraday(hours=48):
    """
    XAUS intraday is used as the common source for
    M5 / M15 / H1 / H4.

    We never manufacture candles.
    """

    try:
        response = requests.get(
            XAUS_INTRADAY_URL,
            params={
                "hours": hours,
            },
            timeout=20,
        )

        response.raise_for_status()

        payload = response.json()

        raw_points = None

        # ----------------------------------------------------
        # possible response formats
        # ----------------------------------------------------

        if isinstance(payload, list):
            raw_points = payload

        elif isinstance(payload, dict):

            for key in (
                "data",
                "points",
                "series",
                "prices",
                "result",
            ):

                candidate = payload.get(key)

                if isinstance(candidate, list):
                    raw_points = candidate
                    break

                if isinstance(candidate, dict):

                    for nested_key in (
                        "data",
                        "points",
                        "series",
                        "prices",
                    ):

                        nested = candidate.get(
                            nested_key
                        )

                        if isinstance(
                            nested,
                            list
                        ):
                            raw_points = nested
                            break

                    if raw_points is not None:
                        break

        if not raw_points:
            raise ValueError(
                "XAUS intraday returned no data"
            )

        normalized = []

        for point in raw_points:

            if not isinstance(point, dict):
                continue

            timestamp = None
            price = None

            # ------------------------------------------------
            # timestamp
            # ------------------------------------------------

            for key in (
                "timestamp",
                "time",
                "datetime",
                "date",
                "t",
            ):

                if key in point:
                    timestamp = normalize_timestamp(
                        point.get(key)
                    )

                    if timestamp is not None:
                        break

            # ------------------------------------------------
            # price
            # ------------------------------------------------

            for key in (
                "price",
                "close",
                "value",
                "spot",
                "last",
            ):

                if key in point:
                    price = safe_float(
                        point.get(key)
                    )

                    if price is not None:
                        break

            if timestamp is None or price is None:
                continue

            normalized.append(
                {
                    "timestamp": timestamp,
                    "price": price,
                }
            )

        if len(normalized) < 10:
            raise ValueError(
                f"XAUS intraday too few valid points: "
                f"{len(normalized)}"
            )

        df = pd.DataFrame(normalized)

        df = df.drop_duplicates(
            subset=["timestamp"]
        )

        df = df.sort_values(
            "timestamp"
        )

        df = df.set_index(
            "timestamp"
        )

        df["price"] = pd.to_numeric(
            df["price"],
            errors="coerce",
        )

        df = df.dropna(
            subset=["price"]
        )

        print(
            "XAUS intraday loaded:",
            f"raw={len(raw_points)}",
            f"normalized={len(df)}",
            f"from={df.index.min()}",
            f"to={df.index.max()}",
        )

        return df

    except Exception as exc:
        print(
            "XAUS intraday error:",
            repr(exc),
        )

        return None


# ============================================================
# RESAMPLING
# ============================================================

def resample_price(df, timeframe):
    """
    Converts XAUS sampled price stream to OHLC candles.
    """

    if df is None or df.empty:
        return None

    rule_map = {
        "M5": "5min",
        "M15": "15min",
        "H1": "1h",
        "H4": "4h",
    }

    rule = rule_map.get(timeframe)

    if rule is None:
        return None

    try:

        candles = df["price"].resample(
            rule,
            label="right",
            closed="right",
        ).ohlc()

        candles = candles.dropna()

        if candles.empty:
            return None

        return candles

    except Exception as exc:
        print(
            f"Resample {timeframe} error:",
            repr(exc),
        )

        return None


# ============================================================
# REMOVE INCOMPLETE CANDLE
# ============================================================

def remove_incomplete_candle(df, timeframe):
    """
    Removes only the currently incomplete candle.

    We do NOT blindly remove the last candle.
    """

    if df is None or df.empty:
        return df

    duration_map = {
        "M5": pd.Timedelta(minutes=5),
        "M15": pd.Timedelta(minutes=15),
        "H1": pd.Timedelta(hours=1),
        "H4": pd.Timedelta(hours=4),
    }

    duration = duration_map.get(timeframe)

    if duration is None:
        return df

    try:

        current = pd.Timestamp.now(
            tz="UTC"
        )

        last_time = df.index[-1]

        if (
            current < last_time
            or current - last_time < duration
        ):
            return df.iloc[:-1]

        return df

    except Exception as exc:

        print(
            f"Incomplete candle check "
            f"{timeframe} error:",
            repr(exc),
        )

        return df


# ============================================================
# INDICATORS
# ============================================================

def add_indicators(df):

    if df is None or len(df) < 2:
        return df

    df = df.copy()

    close = df["close"]

    df["ema20"] = close.ewm(
        span=20,
        adjust=False,
    ).mean()

    df["ema50"] = close.ewm(
        span=50,
        adjust=False,
    ).mean()

    df["ema200"] = close.ewm(
        span=200,
        adjust=False,
    ).mean()

    delta = close.diff()

    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )

    avg_gain = gain.rolling(
        14
    ).mean()

    avg_loss = loss.rolling(
        14
    ).mean()

    rs = avg_gain / avg_loss.replace(
        0,
        np.nan,
    )

    df["rsi"] = (
        100
        - (
            100
            / (
                1 + rs
            )
        )
    )

    return df


# ============================================================
# TREND
# ============================================================

def get_trend(df):

    if df is None or len(df) < 7:
        return "INSUFFICIENT", 0

    df = add_indicators(df)

    last = df.iloc[-1]

    close = safe_float(
        last["close"]
    )

    ema20 = safe_float(
        last["ema20"]
    )

    ema50 = safe_float(
        last["ema50"]
    )

    ema200 = safe_float(
        last["ema200"]
    )

    rsi = safe_float(
        last["rsi"]
    )

    if (
        close is None
        or ema20 is None
        or ema50 is None
    ):
        return "MIXED", 50

    bull = 0
    bear = 0

    # --------------------------------------------------------
    # EMA structure
    # --------------------------------------------------------

    if close > ema20:
        bull += 1
    else:
        bear += 1

    if ema20 > ema50:
        bull += 1
    else:
        bear += 1

    if ema50 > ema200:
        bull += 1
    else:
        bear += 1

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    if rsi is not None:

        if rsi >= 55:
            bull += 1

        elif rsi <= 45:
            bear += 1

    # --------------------------------------------------------
    # result
    # --------------------------------------------------------

    if bull >= bear + 2:

        score = min(
            100,
            50 + bull * 12,
        )

        return "BULLISH", score

    if bear >= bull + 2:

        score = min(
            100,
            50 + bear * 12,
        )

        return "BEARISH", score

    return "MIXED", 50


# ============================================================
# MARKET STRUCTURE
# ============================================================

def detect_bos(df):

    if df is None or len(df) < 6:
        return "NONE"

    recent = df.iloc[-6:]

    prev_high = recent["high"].iloc[:-1].max()
    prev_low = recent["low"].iloc[:-1].min()

    last_close = recent["close"].iloc[-1]

    if last_close > prev_high:
        return "BULLISH"

    if last_close < prev_low:
        return "BEARISH"

    return "NONE"


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def detect_liquidity(df):

    if df is None or len(df) < 6:
        return "NONE"

    recent = df.iloc[-6:]

    previous_high = recent["high"].iloc[:-1].max()
    previous_low = recent["low"].iloc[:-1].min()

    last = recent.iloc[-1]

    if (
        last["high"] > previous_high
        and last["close"] < previous_high
    ):
        return "SELL_SIDE_SWEEP"

    if (
        last["low"] < previous_low
        and last["close"] > previous_low
    ):
        return "BUY_SIDE_SWEEP"

    return "NONE"


# ============================================================
# FVG
# ============================================================

def detect_fvg(df):

    if df is None or len(df) < 3:
        return "NONE"

    a = df.iloc[-3]
    c = df.iloc[-1]

    # bullish FVG
    if c["low"] > a["high"]:
        return "BULLISH"

    # bearish FVG
    if c["high"] < a["low"]:
        return "BEARISH"

    return "NONE"


# ============================================================
# DISPLACEMENT
# ============================================================

def detect_displacement(df):

    if df is None or len(df) < 6:
        return "NONE"

    body = (
        df["close"]
        - df["open"]
    ).abs()

    recent_body = body.iloc[-1]

    average_body = body.iloc[-6:-1].mean()

    if average_body <= 0:
        return "NONE"

    if recent_body >= average_body * 1.8:

        if df["close"].iloc[-1] > df["open"].iloc[-1]:
            return "BULLISH"

        if df["close"].iloc[-1] < df["open"].iloc[-1]:
            return "BEARISH"

    return "NONE"


# ============================================================
# PREMIUM / DISCOUNT
# ============================================================

def get_zone(df):

    if df is None or len(df) < 10:
        return "NONE"

    recent = df.iloc[-10:]

    high = recent["high"].max()
    low = recent["low"].min()

    price = recent["close"].iloc[-1]

    if high <= low:
        return "NONE"

    midpoint = (
        high + low
    ) / 2

    if price > midpoint:
        return "PREMIUM"

    return "DISCOUNT"


# ============================================================
# STRUCTURE SUMMARY
# ============================================================

def analyze_tf(df):

    trend, trend_score = get_trend(df)

    return {
        "trend": trend,
        "trend_score": trend_score,
        "bos": detect_bos(df),
        "liquidity": detect_liquidity(df),
        "fvg": detect_fvg(df),
        "displacement": detect_displacement(df),
        "zone": get_zone(df),
    }


# ============================================================
# MARKET DATA
# ============================================================

def build_market_data():

    intraday = fetch_intraday(
        hours=48
    )

    if intraday is None:
        return None

    result = {}

    for tf in (
        "M5",
        "M15",
        "H1",
        "H4",
    ):

        candles = resample_price(
            intraday,
            tf,
        )

        if candles is None:
            print(
                f"Market data: "
                f"{tf}=0 candles"
            )

            return None

        candles = remove_incomplete_candle(
            candles,
            tf,
        )

        result[
            f"_{tf.lower()}_count"
        ] = len(candles)

        minimum = MIN_CANDLES[tf]

        print(
            f"Market data: "
            f"{tf}={len(candles)} "
            f"(minimum={minimum})"
        )

        if tf != "H4":

            if len(candles) < minimum:

                print(
                    f"Market data unavailable: "
                    f"{tf} has {len(candles)}, "
                    f"need {minimum}"
                )

                return None

        result[tf] = candles

    # --------------------------------------------------------
    # H4 status
    # --------------------------------------------------------

    h4_count = len(
        result["H4"]
    )

    if h4_count >= PREFERRED_H4_CANDLES:

        result["_h4_status"] = "FULL"

    elif h4_count >= MIN_CANDLES["H4"]:

        result["_h4_status"] = "LIMITED"

    else:

        print(
            f"Market data unavailable: "
            f"H4 has {h4_count}, "
            f"need {MIN_CANDLES['H4']}"
        )

        return None

    # --------------------------------------------------------
    # analysis
    # --------------------------------------------------------

    result["analysis"] = {}

    for tf in (
        "M5",
        "M15",
        "H1",
        "H4",
    ):

        result["analysis"][tf] = analyze_tf(
            result[tf]
        )

    return result


# ============================================================
# MARKET CACHE
# ============================================================

def get_market_data(force=False):

    current_time = time.time()

    with state_lock:

        if (
            not force
            and market_cache["data"] is not None
            and current_time
            - market_cache["timestamp"]
            < CACHE_SECONDS
        ):

            return market_cache["data"]

    data = build_market_data()

    if data is None:
        return None

    with state_lock:

        market_cache["data"] = data
        market_cache["timestamp"] = current_time

    return data


# ============================================================
# HIGHER TF BIAS
# ============================================================

def get_higher_bias(market):

    h4 = market["analysis"]["H4"]
    h1 = market["analysis"]["H1"]
    m15 = market["analysis"]["M15"]

    h4_status = market[
        "_h4_status"
    ]

    # --------------------------------------------------------
    # FULL H4
    # --------------------------------------------------------

    if h4_status == "FULL":

        h4_trend = h4["trend"]
        h1_trend = h1["trend"]

        if (
            h4_trend == "BULLISH"
            and h1_trend == "BULLISH"
        ):
            return "BULLISH"

        if (
            h4_trend == "BEARISH"
            and h1_trend == "BEARISH"
        ):
            return "BEARISH"

        return "MIXED"

    # --------------------------------------------------------
    # LIMITED H4
    #
    # H1 + M15 become primary bias.
    # --------------------------------------------------------

    if (
        h1["trend"] == "BULLISH"
        and m15["trend"] == "BULLISH"
    ):
        return "BULLISH"

    if (
        h1["trend"] == "BEARISH"
        and m15["trend"] == "BEARISH"
    ):
        return "BEARISH"

    return "MIXED"


# ============================================================
# SIGNAL SCORING
# ============================================================

def score_signal(market):

    a = market["analysis"]

    h4 = a["H4"]
    h1 = a["H1"]
    m15 = a["M15"]
    m5 = a["M5"]

    higher_bias = get_higher_bias(
        market
    )

    buy_score = 0
    sell_score = 0

    # ========================================================
    # TREND WEIGHTS
    # ========================================================

    trend_weights = {
        "H4": 20,
        "H1": 15,
        "M15": 15,
        "M5": 10,
    }

    for tf, weight in trend_weights.items():

        trend = a[tf]["trend"]

        if trend == "BULLISH":
            buy_score += weight

        elif trend == "BEARISH":
            sell_score += weight

    # ========================================================
    # HIGHER BIAS BONUS
    # ========================================================

    if higher_bias == "BULLISH":
        buy_score += 10

    elif higher_bias == "BEARISH":
        sell_score += 10

    # ========================================================
    # LIQUIDITY
    # ========================================================

    if m5["liquidity"] == "BUY_SIDE_SWEEP":
        buy_score += 15

    elif m5["liquidity"] == "SELL_SIDE_SWEEP":
        sell_score += 15

    # ========================================================
    # BOS
    # ========================================================

    if m5["bos"] == "BULLISH":
        buy_score += 15

    elif m5["bos"] == "BEARISH":
        sell_score += 15

    # ========================================================
    # FVG
    # ========================================================

    if m5["fvg"] == "BULLISH":
        buy_score += 8

    elif m5["fvg"] == "BEARISH":
        sell_score += 8

    # ========================================================
    # DISPLACEMENT
    # ========================================================

    if m5["displacement"] == "BULLISH":
        buy_score += 12

    elif m5["displacement"] == "BEARISH":
        sell_score += 12

    # ========================================================
    # ZONE
    # ========================================================

    if m5["zone"] == "DISCOUNT":
        buy_score += 5

    elif m5["zone"] == "PREMIUM":
        sell_score += 5

    # ========================================================
    # DETERMINE DIRECTION
    # ========================================================

    if buy_score > sell_score:

        direction = "BUY"
        score = buy_score

    elif sell_score > buy_score:

        direction = "SELL"
        score = sell_score

    else:

        direction = "WAIT"
        score = max(
            buy_score,
            sell_score,
        )

    # ========================================================
    # M5 CONFIRMATION
    # ========================================================

    m5_confirmation = False

    if direction == "BUY":

        if (
            m5["bos"] == "BULLISH"
            or m5["liquidity"]
            == "BUY_SIDE_SWEEP"
            or m5["fvg"] == "BULLISH"
            or m5["displacement"]
            == "BULLISH"
        ):
            m5_confirmation = True

    elif direction == "SELL":

        if (
            m5["bos"] == "BEARISH"
            or m5["liquidity"]
            == "SELL_SIDE_SWEEP"
            or m5["fvg"] == "BEARISH"
            or m5["displacement"]
            == "BEARISH"
        ):
            m5_confirmation = True

    # ========================================================
    # FULL
    # ========================================================

    full_signal = (
        direction in ("BUY", "SELL")
        and score >= MIN_SCORE
        and m5_confirmation
        and (
            higher_bias == direction
        )
    )

    # ========================================================
    # EARLY
    # ========================================================

    early_signal = (
        direction in ("BUY", "SELL")
        and score >= EARLY_SCORE
        and m5_confirmation
        and (
            higher_bias
            in (
                direction,
                "MIXED",
            )
        )
        and not full_signal
    )

    if full_signal:

        signal_type = "FULL"

    elif early_signal:

        signal_type = "EARLY"

    else:

        signal_type = "WAIT"

    return {
        "direction": direction,
        "score": int(
            min(100, score)
        ),
        "higher_bias": higher_bias,
        "signal_type": signal_type,
        "m5_confirmation": m5_confirmation,
        "buy_score": int(
            min(100, buy_score)
        ),
        "sell_score": int(
            min(100, sell_score)
        ),
    }


# ============================================================
# TRADE LEVELS
# ============================================================

def make_trade(direction, price):

    if direction == "BUY":

        entry = price

        sl = price - SL_DISTANCE

        tp1 = price + TP_DISTANCE * 0.50

        tp2 = price + TP_DISTANCE

    else:

        entry = price

        sl = price + SL_DISTANCE

        tp1 = price - TP_DISTANCE * 0.50

        tp2 = price - TP_DISTANCE

    return {
        "direction": direction,
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2, 2),
        "rr": "1:3",
        "risk": RISK_PERCENT,
    }


# ============================================================
# FORMAT SIGNAL
# ============================================================

def format_signal_message(
    market,
    signal,
    price,
):

    a = market["analysis"]

    direction = signal["direction"]
    score = signal["score"]
    signal_type = signal[
        "signal_type"
    ]

    h4_status = market[
        "_h4_status"
    ]

    if signal_type == "FULL":

        icon = (
            "🟢"
            if direction == "BUY"
            else "🔴"
        )

        title = "SIGNAL"

    elif signal_type == "EARLY":

        icon = "🟡"
        title = "EARLY SIGNAL"

    else:

        icon = "⚪"
        title = "SIGNAL"

    message = (
        "🥇 GOLD SMART V5.9.4\n\n"
        f"{icon} {title}: "
        f"{direction}\n\n"
        f"💰 XAUUSD: {price:.2f}\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 H4: "
        f"{a['H4']['trend']} "
        f"({a['H4']['trend_score']}/100)\n"
        f"📊 H1: "
        f"{a['H1']['trend']} "
        f"({a['H1']['trend_score']}/100)\n"
        f"📊 M15: "
        f"{a['M15']['trend']} "
        f"({a['M15']['trend_score']}/100)\n"
        f"📊 M5: "
        f"{a['M5']['trend']} "
        f"({a['M5']['trend_score']}/100)\n\n"
        f"🧭 HIGHER TF BIAS: "
        f"{signal['higher_bias']}\n\n"
        f"🎯 SCORE: "
        f"{score}/100\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"💧 M5 Liquidity: "
        f"{a['M5']['liquidity']}\n"
        f"🔨 M5 BOS: "
        f"{a['M5']['bos']}\n"
        f"🧩 M5 FVG: "
        f"{a['M5']['fvg']}\n"
        f"💥 M5 Displacement: "
        f"{a['M5']['displacement']}\n"
        f"📍 M5 Zone: "
        f"{a['M5']['zone']}\n\n"
        f"🕯 H4 DATA: "
        f"{h4_status} "
        f"({market['_h4_count']})\n"
    )

    # --------------------------------------------------------
    # TRADE LEVELS
    # --------------------------------------------------------

    if signal_type in (
        "FULL",
        "EARLY",
    ):

        trade = make_trade(
            direction,
            price,
        )

        message += (
            "\n━━━━━━━━━━━━━━━━━━\n\n"
            f"🎯 ENTRY: "
            f"{trade['entry']:.2f}\n"
            f"🛑 SL: "
            f"{trade['sl']:.2f}\n"
            f"🎯 TP1: "
            f"{trade['tp1']:.2f}\n"
            f"🎯 TP2: "
            f"{trade['tp2']:.2f}\n\n"
            f"📐 RR: "
            f"{trade['rr']}\n"
            f"📉 Risk: "
            f"{trade['risk']:.1f}%\n"
            f"💰 Deposit: "
            f"${DEFAULT_DEPOSIT:.0f}\n\n"
            f"🛡 AUTO TRADING: "
            f"{'ON' if AUTO_TRADING else 'OFF'}\n"
        )

        if signal_type == "FULL":

            message += (
                "\n🟢 FULL SIGNAL\n"
                "SMC confirmation detected."
            )

        else:

            message += (
                "\n🟡 EARLY SIGNAL\n"
                "Wait for stronger confirmation "
                "before aggressive entry."
            )

    else:

        message += (
            "\n━━━━━━━━━━━━━━━━━━\n\n"
            "⏳ WAIT\n"
            "Нет достаточного подтверждения "
            "для качественного входа."
        )

    return message


# ============================================================
# WAIT MESSAGE FOR DATA FAILURE
# ============================================================

def data_wait_message():

    return (
        "🥇 GOLD SMART V5.9.4\n\n"
        "⚠️ SIGNAL: WAIT\n\n"
        "❌ Не удалось получить достаточно "
        "данных XAU/USD.\n\n"
        "Проверь Render Logs.\n"
        "Бот НЕ создаёт искусственные свечи.\n\n"
        "🛡 AUTO TRADING: OFF"
    )


# ============================================================
# PROCESS SIGNAL
# ============================================================

def process_signal(
    send_message=True,
    force=False,
):

    price = fetch_spot()

    if price is None:

        if send_message:
            send_telegram(
                data_wait_message()
            )

        return None

    market = get_market_data(
        force=force
    )

    if market is None:

        if send_message:
            send_telegram(
                data_wait_message()
            )

        return None

    signal = score_signal(
        market
    )

    message = format_signal_message(
        market,
        signal,
        price,
    )

    signal_type = signal[
        "signal_type"
    ]

    # --------------------------------------------------------
    # statistics
    # --------------------------------------------------------

    if signal_type != "WAIT":

        with state_lock:

            stats["signals"] += 1

            if signal_type == "FULL":
                stats["full_signals"] += 1

            elif signal_type == "EARLY":
                stats["early_signals"] += 1

    # --------------------------------------------------------
    # virtual trade only for FULL
    # --------------------------------------------------------

    global virtual_trade

    if (
        signal_type == "FULL"
        and not AUTO_TRADING
    ):

        trade = make_trade(
            signal["direction"],
            price,
        )

        with state_lock:

            virtual_trade = {
                **trade,
                "opened_at": now_utc().isoformat(),
                "status": "OPEN",
            }

    # --------------------------------------------------------
    # signal tracking
    # --------------------------------------------------------

    if signal_type != "WAIT":

        with state_lock:

            last_signal["direction"] = (
                signal["direction"]
            )

            last_signal["score"] = (
                signal["score"]
            )

            last_signal["entry"] = price

            last_signal["time"] = (
                now_utc().isoformat()
            )

    # --------------------------------------------------------
    # telegram
    # --------------------------------------------------------

    if send_message:

        send_telegram(
            message
        )

    return {
        "price": price,
        "market": market,
        "signal": signal,
        "message": message,
    }


# ============================================================
# VIRTUAL TRADE MANAGEMENT
# ============================================================

def check_virtual_trade(price):

    global virtual_trade

    with state_lock:

        trade = virtual_trade

        if not trade:
            return

        if trade.get("status") != "OPEN":
            return

        direction = trade[
            "direction"
        ]

        entry = trade[
            "entry"
        ]

        sl = trade[
            "sl"
        ]

        tp2 = trade[
            "tp2"
        ]

        # ----------------------------------------------------
        # BUY
        # ----------------------------------------------------

        if direction == "BUY":

            # BE
            if (
                price >= entry + BE_TRIGGER
                and sl < entry
            ):

                trade["sl"] = entry

                sl = entry

                print(
                    "Virtual BUY: "
                    "SL moved to BE"
                )

            # TP
            if price >= tp2:

                trade["status"] = "TP"

                stats["wins"] += 1

                print(
                    "Virtual BUY: TP"
                )

                return

            # SL
            if price <= sl:

                trade["status"] = (
                    "BE"
                    if sl == entry
                    else "SL"
                )

                if sl == entry:
                    stats["be"] += 1
                else:
                    stats["losses"] += 1

                print(
                    "Virtual BUY:",
                    trade["status"],
                )

                return

        # ----------------------------------------------------
        # SELL
        # ----------------------------------------------------

        if direction == "SELL":

            # BE
            if (
                price <= entry - BE_TRIGGER
                and sl > entry
            ):

                trade["sl"] = entry

                sl = entry

                print(
                    "Virtual SELL: "
                    "SL moved to BE"
                )

            # TP
            if price <= tp2:

                trade["status"] = "TP"

                stats["wins"] += 1

                print(
                    "Virtual SELL: TP"
                )

                return

            # SL
            if price >= sl:

                trade["status"] = (
                    "BE"
                    if sl == entry
                    else "SL"
                )

                if sl == entry:
                    stats["be"] += 1
                else:
                    stats["losses"] += 1

                print(
                    "Virtual SELL:",
                    trade["status"],
                )

                return


# ============================================================
# STATUS
# ============================================================

def build_status():

    price = fetch_spot()

    market = market_cache.get(
        "data"
    )

    if market:

        h4_count = market[
            "_h4_count"
        ]

        h4_status = market[
            "_h4_status"
        ]

        data_status = (
            f"🕯 H4: "
            f"{h4_status} "
            f"({h4_count})"
        )

    else:

        data_status = (
            "🕯 Market data: "
            "NOT LOADED"
        )

    with state_lock:

        trade = virtual_trade

    if trade:

        virtual_status = (
            f"📌 Virtual trade: "
            f"{trade['direction']} "
            f"{trade['entry']:.2f}\n"
            f"🛑 SL: "
            f"{trade['sl']:.2f}\n"
            f"🎯 TP2: "
            f"{trade['tp2']:.2f}\n"
            f"📊 Status: "
            f
