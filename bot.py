import os
import time
import threading
import traceback
from datetime import datetime, timezone

import requests
import pandas as pd
import numpy as np
import yfinance as yf
from flask import Flask, request


# =========================================================
# 🥇 GOLD SMART V5.4
# XAUUSD / GOLD
# Yahoo Finance / GC=F
# CLOSED CANDLE ENGINE
# TELEGRAM COMMANDS
# AUTO TRADING = OFF
# RISK = 1%
# =========================================================


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

YF_SYMBOL = "GC=F"
SOURCE_NAME = "Yahoo Finance / GC=F"

RISK_PERCENT = 1.0
DEFAULT_DEPOSIT = 800.0

MIN_LOT = 0.01
MAX_LOT = 0.02

MIN_SCORE = 70
EARLY_SCORE = 60

TARGET_RR = 3.0

POLL_SECONDS = 60
COOLDOWN_MIN = 15

YAHOO_ERROR_COOLDOWN = 300

CACHE_TTL = {
    "5m": 60,
    "15m": 180,
    "1h": 600,
    "4h": 1800,
}


# =========================================================
# FLASK
# =========================================================

app = Flask(__name__)


# =========================================================
# GLOBAL STATE
# =========================================================

ENGINE_STARTED = False

_last_signal = None
_last_signal_time = 0

_yahoo_block_until = 0

_cache = {}

_stats = {
    "signals": 0,
    "buy": 0,
    "sell": 0,
    "early_buy": 0,
    "early_sell": 0,
    "wait": 0,
    "errors": 0,
}


# =========================================================
# LOG
# =========================================================

def log(message):
    now = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    print(f"[{now}] {message}", flush=True)


# =========================================================
# TELEGRAM SEND
# =========================================================

def send_telegram_message(message, chat_id=None):

    if not BOT_TOKEN:
        log("⚠️ BOT_TOKEN not configured")
        return False

    target_chat = chat_id or CHAT_ID

    if not target_chat:
        log("⚠️ TELEGRAM_CHAT_ID not configured")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": target_chat,
        "text": message,
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=15,
        )

        if response.status_code == 200:
            return True

        log(
            f"❌ Telegram HTTP "
            f"{response.status_code}: "
            f"{response.text[:500]}"
        )

    except Exception as e:

        log(f"❌ Telegram error: {e}")

    return False


# =========================================================
# TELEGRAM HELP
# =========================================================

def telegram_help():

    return (
        "🥇 GOLD SMART V5.4\n\n"

        "🟢 BOT: ONLINE\n"
        "🟢 ENGINE: RUNNING\n"
        "🕯 CLOSED CANDLE: ON\n"
        "🛡 RISK: 1%\n"
        "🤖 AUTO TRADING: OFF\n\n"

        "📋 КОМАНДЫ:\n\n"

        "/status — текущий рынок\n"
        "/signal — полный текущий сигнал\n"
        "/test — проверка системы\n"
        "/stats — статистика\n"
        "/help — список команд\n"
        "/start — главное меню\n"
    )


# =========================================================
# TELEGRAM TEST
# =========================================================

def telegram_test():

    return (
        "✅ GOLD SMART V5.4 TEST\n\n"

        "🟢 Telegram: OK\n"
        "🟢 Webhook: OK\n"
        "🟢 Engine: RUNNING\n"
        "🟢 Yahoo Finance: CONFIGURED\n"
        "🕯 Closed Candle: ON\n"
        "🛡 Risk: 1%\n"
        "🤖 Auto Trading: OFF"
    )


# =========================================================
# TELEGRAM STATS
# =========================================================

def telegram_stats():

    return (
        "📊 GOLD SMART V5.4\n"
        "STATISTICS\n\n"

        f"📌 TOTAL CHECKS: {_stats['signals']}\n\n"

        f"🟢 BUY: {_stats['buy']}\n"
        f"🔴 SELL: {_stats['sell']}\n"
        f"🟡 EARLY BUY: {_stats['early_buy']}\n"
        f"🟠 EARLY SELL: {_stats['early_sell']}\n"
        f"⚪ WAIT: {_stats['wait']}\n"
        f"❌ ERRORS: {_stats['errors']}\n\n"

        "━━━━━━━━━━━━━━━━━━\n"

        f"📡 SOURCE: {SOURCE_NAME}\n"
        "🛡 RISK: 1%\n"
        "🤖 AUTO TRADING: OFF"
    )


# =========================================================
# DATA NORMALIZATION
# =========================================================

def normalize_dataframe(df):

    if df is None or df.empty:
        return None

    try:

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df.copy()

        required = [
            "Open",
            "High",
            "Low",
            "Close",
            "Volume",
        ]

        for column in required:

            if column not in df.columns:

                if column == "Volume":
                    df[column] = 0

                else:
                    return None

        df = df[required].copy()

        for column in required:

            df[column] = pd.to_numeric(
                df[column],
                errors="coerce",
            )

        df = df.dropna(
            subset=[
                "Open",
                "High",
                "Low",
                "Close",
            ]
        )

        return df

    except Exception as e:

        log(
            f"❌ Data normalization error: {e}"
        )

        return None


# =========================================================
# REMOVE CURRENT INCOMPLETE CANDLE
# =========================================================

def remove_incomplete_candle(df):

    if df is None or len(df) < 3:
        return None

    df = df.copy()

    try:

        df.index = pd.to_datetime(df.index)

        if df.index.tz is None:

            df.index = df.index.tz_localize("UTC")

        else:

            df.index = df.index.tz_convert("UTC")

    except Exception:
        pass

    # Remove latest candle.
    # We only analyse CLOSED candles.

    df = df.iloc[:-1].copy()

    if len(df) < 2:
        return None

    return df


# =========================================================
# YAHOO DOWNLOAD
# =========================================================

def yahoo_download(interval, period):

    global _yahoo_block_until

    if time.time() < _yahoo_block_until:
        return None

    try:

        df = yf.download(
            YF_SYMBOL,
            period=period,
            interval=interval,
            auto_adjust=False,
            progress=False,
            threads=False,
        )

        df = normalize_dataframe(df)

        if df is None:
            return None

        return df

    except Exception as e:

        error_text = str(e)

        if (
            "429" in error_text
            or "rate" in error_text.lower()
        ):

            _yahoo_block_until = (
                time.time()
                + YAHOO_ERROR_COOLDOWN
            )

            log(
                "⚠️ Yahoo rate limit. "
                f"Pause {YAHOO_ERROR_COOLDOWN}s"
            )

        else:

            log(
                f"❌ Yahoo download error: "
                f"{error_text}"
            )

        return None


# =========================================================
# GET DATA
# =========================================================

def get_data(interval):

    now = time.time()

    cached = _cache.get(interval)

    if cached:

        timestamp, data = cached

        ttl = CACHE_TTL.get(
            interval,
            60,
        )

        if now - timestamp < ttl:
            return data

    periods = {
        "5m": "5d",
        "15m": "10d",
        "1h": "30d",
    }

    period = periods.get(
        interval,
        "10d",
    )

    df = yahoo_download(
        interval,
        period,
    )

    if df is None:

        if cached:
            return cached[1]

        return None

    df = remove_incomplete_candle(df)

    if df is None:
        return None

    _cache[interval] = (
        now,
        df,
    )

    return df


# =========================================================
# BUILD 4H FROM 1H
# =========================================================

def get_4h_data():

    now = time.time()

    cached = _cache.get("4h")

    if cached:

        timestamp, data = cached

        if (
            now - timestamp
            < CACHE_TTL["4h"]
        ):
            return data

    df_1h = get_data("1h")

    if df_1h is None or df_1h.empty:
        return None

    try:

        df = df_1h.copy()

        df = df.resample("4h").agg(
            {
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
            }
        )

        df = df.dropna()

        if len(df) < 3:
            return None

        # Remove latest potentially incomplete 4H candle.

        df = df.iloc[:-1].copy()

        _cache["4h"] = (
            now,
            df,
        )

        return df

    except Exception as e:

        log(
            f"❌ 4H resample error: {e}"
        )

        return None


# =========================================================
# EMA
# =========================================================

def ema(series, period):

    return series.ewm(
        span=period,
        adjust=False,
    ).mean()


# =========================================================
# RSI
# =========================================================

def calculate_rsi(
    series,
    period=14,
):

    delta = series.diff()

    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )

    avg_gain = gain.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False,
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False,
    ).mean()

    rs = (
        avg_gain
        / avg_loss.replace(
            0,
            np.nan,
        )
    )

    return 100 - (
        100 / (1 + rs)
    )


# =========================================================
# TREND
# =========================================================

def get_trend(df):

    if df is None or len(df) < 60:
        return "MIXED"

    close = df["Close"]

    ema50 = ema(
        close,
        50,
    )

    ema200 = ema(
        close,
        200,
    )

    price = float(
        close.iloc[-1]
    )

    e50 = float(
        ema50.iloc[-1]
    )

    e200 = float(
        ema200.iloc[-1]
    )

    if (
        price > e50
        and e50 > e200
    ):

        return "BULLISH"

    if (
        price < e50
        and e50 < e200
    ):

        return "BEARISH"

    return "MIXED"


# =========================================================
# BOS
# =========================================================

def detect_bos(
    df,
    lookback=12,
):

    if (
        df is None
        or len(df) < lookback + 3
    ):
        return "NONE"

    previous = df.iloc[
        -(lookback + 1):-1
    ]

    current = df.iloc[-1]

    previous_high = previous[
        "High"
    ].max()

    previous_low = previous[
        "Low"
    ].min()

    if (
        current["Close"]
        > previous_high
    ):

        return "BULLISH BOS"

    if (
        current["Close"]
        < previous_low
    ):

        return "BEARISH BOS"

    return "NONE"


# =========================================================
# LIQUIDITY SWEEP
# =========================================================

def detect_liquidity(
    df,
    lookback=10,
):

    if (
        df is None
        or len(df) < lookback + 2
    ):
        return "NONE"

    previous = df.iloc[
        -(lookback + 1):-1
    ]

    current = df.iloc[-1]

    previous_high = previous[
        "High"
    ].max()

    previous_low = previous[
        "Low"
    ].min()

    # Buy-side liquidity sweep
    if (
        current["High"]
        > previous_high
        and current["Close"]
        < previous_high
    ):

        return "BSL SWEEP"

    # Sell-side liquidity sweep
    if (
        current["Low"]
        < previous_low
        and current["Close"]
        > previous_low
    ):

        return "SSL SWEEP"

    return "NONE"


# =========================================================
# FVG
# =========================================================

def detect_fvg(df):

    if df is None or len(df) < 4:
        return "NONE"

    c1 = df.iloc[-3]
    c3 = df.iloc[-1]

    if (
        c3["Low"]
        > c1["High"]
    ):

        return "BULLISH FVG"

    if (
        c3["High"]
        < c1["Low"]
    ):

        return "BEARISH FVG"

    return "NONE"


# =========================================================
# DISPLACEMENT
# =========================================================

def detect_displacement(df):

    if df is None or len(df) < 25:
        return "NONE"

    recent = df.iloc[
        -21:-1
    ].copy()

    recent["range"] = (
        recent["High"]
        - recent["Low"]
    )

    avg_range = recent[
        "range"
    ].mean()

    current = df.iloc[-1]

    body = abs(
        current["Close"]
        - current["Open"]
    )

    if avg_range <= 0:
        return "NONE"

    if (
        body
        >= avg_range * 1.5
        and current["Close"]
        > current["Open"]
    ):

        return "BULLISH"

    if (
        body
        >= avg_range * 1.5
        and current["Close"]
        < current["Open"]
    ):

        return "BEARISH"

    return "NONE"


# =========================================================
# MOMENTUM
# =========================================================

def detect_momentum(df):

    if df is None or len(df) < 60:
        return "MIXED"

    close = df["Close"]

    ema20 = ema(
        close,
        20,
    )

    ema50 = ema(
        close,
        50,
    )

    price = float(
        close.iloc[-1]
    )

    e20 = float(
        ema20.iloc[-1]
    )

    e50 = float(
        ema50.iloc[-1]
    )

    if (
        price > e20
        and e20 > e50
    ):

        return "BULLISH"

    if (
        price < e20
        and e20 < e50
    ):

        return "BEARISH"

    return "MIXED"


# =========================================================
# PREMIUM / DISCOUNT
# =========================================================

def get_zone(
    df,
    lookback=50,
):

    if (
        df is None
        or len(df) < lookback
    ):

        return "EQUILIBRIUM"

    recent = df.iloc[
        -lookback:
    ]

    high = float(
        recent["High"].max()
    )

    low = float(
        recent["Low"].min()
    )

    if high <= low:
        return "EQUILIBRIUM"

    price = float(
        df["Close"].iloc[-1]
    )

    midpoint = (
        high + low
    ) / 2

    if price > midpoint:
        return "PREMIUM"

    if price < midpoint:
        return "DISCOUNT"

    return "EQUILIBRIUM"


# =========================================================
# RSI VALUE
# =========================================================

def get_rsi_value(df):

    if df is None or len(df) < 20:
        return 50.0

    rsi = calculate_rsi(
        df["Close"]
    )

    value = rsi.iloc[-1]

    if pd.isna(value):
        return 50.0

    return float(value)


# =========================================================
# ANALYZE TIMEFRAME
# =========================================================

def analyze_timeframe(df):

    return {
        "trend": get_trend(df),
        "bos": detect_bos(df),
        "liquidity": detect_liquidity(df),
        "fvg": detect_fvg(df),
        "displacement": detect_displacement(df),
        "momentum": detect_momentum(df),
        "zone": get_zone(df),
        "rsi": get_rsi_value(df),
    }


# =========================================================
# MARKET ANALYSIS
# =========================================================

def analyze_market():

    df_4h = get_4h_data()
    df_1h = get_data("1h")
    df_15m = get_data("15m")
    df_5m = get_data("5m")

    datasets = [
        df_4h,
        df_1h,
        df_15m,
        df_5m,
    ]

    if any(
        x is None or x.empty
        for x in datasets
    ):

        raise RuntimeError(
            "Market data unavailable"
        )

    h4 = analyze_timeframe(
        df_4h
    )

    h1 = analyze_timeframe(
        df_1h
    )

    m15 = analyze_timeframe(
        df_15m
    )

    m5 = analyze_timeframe(
        df_5m
    )

    price = float(
        df_5m["Close"].iloc[-1]
    )

    buy_score = 0
    sell_score = 0


    # -----------------------------------------------------
    # H4 TREND
    # -----------------------------------------------------

    if h4["trend"] == "BULLISH":
        buy_score += 20

    elif h4["trend"] == "BEARISH":
        sell_score += 20


    # -----------------------------------------------------
    # H1 TREND
    # -----------------------------------------------------

    if h1["trend"] == "BULLISH":
        buy_score += 15

    elif h1["trend"] == "BEARISH":
        sell_score += 15


    # -----------------------------------------------------
    # M15 TREND
    # -----------------------------------------------------

    if m15["trend"] == "BULLISH":
        buy_score += 15

    elif m15["trend"] == "BEARISH":
        sell_score += 15


    # -----------------------------------------------------
    # M5 TREND
    # -----------------------------------------------------

    if m5["trend"] == "BULLISH":
        buy_score += 10

    elif m5["trend"] == "BEARISH":
        sell_score += 10


    # -----------------------------------------------------
    # LIQUIDITY
    # -----------------------------------------------------

    if m5["liquidity"] == "SSL SWEEP":
        buy_score += 10

    elif m5["liquidity"] == "BSL SWEEP":
        sell_score += 10


    # -----------------------------------------------------
    # BOS
    # -----------------------------------------------------

    if m5["bos"] == "BULLISH BOS":
        buy_score += 15

    elif m5["bos"] == "BEARISH BOS":
        sell_score += 15


    # -----------------------------------------------------
    # FVG
    # -----------------------------------------------------

    if m5["fvg"] == "BULLISH FVG":
        buy_score += 5

    elif m5["fvg"] == "BEARISH FVG":
        sell_score += 5


    # -----------------------------------------------------
    # DISPLACEMENT
    # -----------------------------------------------------

    if m5["displacement"] == "BULLISH":
        buy_score += 10

    elif m5["displacement"] == "BEARISH":
        sell_score += 10


    # -----------------------------------------------------
    # MOMENTUM
    # -----------------------------------------------------

    if m5["momentum"] == "BULLISH":
        buy_score += 5

    elif m5["momentum"] == "BEARISH":
        sell_score += 5


    # -----------------------------------------------------
    # RSI
    # -----------------------------------------------------

    rsi = m5["rsi"]

    if 50 <= rsi <= 68:

        buy_score += 5

    elif 32 <= rsi < 50:

        sell_score += 5


    # -----------------------------------------------------
    # SIGNAL
    # -----------------------------------------------------

    if (
        buy_score >= MIN_SCORE
        and buy_score > sell_score
    ):

        signal = "BUY"

    elif (
        sell_score >= MIN_SCORE
        and sell_score > buy_score
    ):

        signal = "SELL"

    elif (
        buy_score >= EARLY_SCORE
        and buy_score > sell_score
    ):

        signal = "EARLY BUY"

    elif (
        sell_score >= EARLY_SCORE
        and sell_score > buy_score
    ):

        signal = "EARLY SELL"

    else:

        signal = "WAIT"


    # -----------------------------------------------------
    # SCORE PERCENT
    # -----------------------------------------------------

    total_score = (
        buy_score
        + sell_score
    )

    if total_score > 0:

        up_percent = round(
            buy_score
            / total_score
            * 100
        )

        down_percent = (
            100 - up_percent
        )

    else:

        up_percent = 50
        down_percent = 50


    return {

        "price": price,

        "h4": h4,
        "h1": h1,
        "m15": m15,
        "m5": m5,

        "h4_trend": h4["trend"],
        "h1_trend": h1["trend"],
        "m15_trend": m15["trend"],
        "m5_trend": m5["trend"],

        "buy_score": buy_score,
        "sell_score": sell_score,

        "up_percent": up_percent,
        "down_percent": down_percent,

        "signal": signal,
    }


# =========================================================
# SETUP
# =========================================================

def make_setup(analysis):

    price = analysis["price"]

    signal = analysis["signal"]

    if signal not in [
        "BUY",
        "SELL",
        "EARLY BUY",
        "EARLY SELL",
    ]:

        return None


    # Conservative fixed gold distance.
    # This is for demo / informational signals.
    # Broker contract specifications may differ.

    sl_distance = 5.0


    if signal in [
        "BUY",
        "EARLY BUY",
    ]:

        direction = "BUY"

        entry = price

        sl = (
            entry
            - sl_distance
        )

        tp = (
            entry
            + sl_distance
            * TARGET_RR
        )

    else:

        direction = "SELL"

        entry = price

        sl = (
            entry
            + sl_distance
        )

        tp = (
            entry
            - sl_distance
            * TARGET_RR
        )


    risk_money = (
        DEFAULT_DEPOSIT
        * RISK_PERCENT
        / 100
    )


    # Approximate lot calculation.
    # Do not use as live execution sizing
    # without broker contract verification.

    lot = (
        risk_money
        / (sl_distance * 100)
    )

    lot = max(
        MIN_LOT,
        min(
            MAX_LOT,
            lot,
        ),
    )


    return {

        "direction": direction,

        "entry": entry,

        "sl": sl,

        "tp": tp,

        "lot": lot,

        "rr": TARGET_RR,
    }


# =========================================================
# FORMAT SIGNAL
# =========================================================

def format_signal(analysis):

    signal = analysis["signal"]

    emoji = {
        "BUY": "🟢",
        "SELL": "🔴",
        "EARLY BUY": "🟡",
        "EARLY SELL": "🟠",
        "WAIT": "⚪",
    }.get(
        signal,
        "⚪",
    )

    m5 = analysis["m5"]

    lines = [

        "🥇 GOLD SMART V5.4",

        "",

        f"{emoji} SIGNAL: {signal}",

        "",

        f"💰 XAUUSD: "
        f"{analysis['price']:.2f}",

        "",

        f"📊 H4: "
        f"{analysis['h4_trend']}",

        f"📊 H1: "
        f"{analysis['h1_trend']}",

        f"📊 M15: "
        f"{analysis['m15_trend']}",

        f"📊 M5: "
        f"{analysis['m5_trend']}",

        "",

        f"💧 M5 Liquidity: "
        f"{m5['liquidity']}",

        f"🔨 M5 BOS: "
        f"{m5['bos']}",

        f"🧩 M5 FVG: "
        f"{m5['fvg']}",

        f"💥 M5 Displacement: "
        f"{m5['displacement']}",

        f"📈 Momentum: "
        f"{m5['momentum']}",

        f"📐 Zone: "
        f"{m5['zone']}",

        f"RSI: "
        f"{m5['rsi']:.1f}",

        "",

        f"📈 UP: "
        f"{analysis['up_percent']}%",

        f"📉 DOWN: "
        f"{analysis['down_percent']}%",

        f"🎯 BUY SCORE: "
        f"{analysis['buy_score']}",

        f"🎯 SELL SCORE: "
        f"{analysis['sell_score']}",
    ]


    setup = make_setup(
        analysis
    )


    if setup:

        lines += [

            "",

            "━━━━━━━━━━━━━━━━━━",

            f"📍 "
            f"{setup['direction']}",

            f"ENTRY: "
            f"{setup['entry']:.2f}",

            f"SL: "
            f"{setup['sl']:.2f}",

            f"TP: "
            f"{setup['tp']:.2f}",

            f"LOT: "
            f"{setup['lot']:.2f}",

            f"RR: 1:"
            f"{setup['rr']:.0f}",
        ]


    lines += [

        "",

        "🤖 AUTO TRADING: OFF",

        f"📡 SOURCE: "
        f"{SOURCE_NAME}",

        "🕯 CLOSED CANDLE ENGINE",
    ]


    return "\n".join(lines)


# =========================================================
# PROCESS SIGNAL
# =========================================================

def process_signal(analysis):

    global _last_signal
    global _last_signal_time

    signal = analysis["signal"]


    _stats["signals"] += 1


    if signal == "BUY":

        _stats["buy"] += 1

    elif signal == "SELL":

        _stats["sell"] += 1

    elif signal == "EARLY BUY":

        _stats["early_buy"] += 1

    elif signal == "EARLY SELL":

        _stats["early_sell"] += 1

    elif signal == "WAIT":

        _stats["wait"] += 1


    # -----------------------------------------------------
    # WAIT
    # -----------------------------------------------------

    if signal == "WAIT":

        log
