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
#
# DATA: Yahoo Finance / GC=F
# TELEGRAM: WEBHOOK
# AUTO TRADING: OFF
# RISK: 1%
# CLOSED CANDLE ENGINE: ON
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
WEBHOOK_READY = False

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

    now = datetime.now(
        timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    print(
        f"[{now}] {message}",
        flush=True
    )


# =========================================================
# TELEGRAM SEND
# =========================================================

def send_telegram_message(
    message,
    chat_id=None
):

    if not BOT_TOKEN:

        log(
            "⚠️ BOT_TOKEN not configured"
        )

        return False

    target_chat = (
        chat_id or CHAT_ID
    )

    if not target_chat:

        log(
            "⚠️ TELEGRAM_CHAT_ID "
            "not configured"
        )

        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    try:

        response = requests.post(
            url,
            json={
                "chat_id": target_chat,
                "text": message
            },
            timeout=15
        )

        if (
            response.status_code == 200
            and response.json().get(
                "ok",
                False
            )
        ):

            return True

        log(
            f"❌ Telegram HTTP "
            f"{response.status_code}: "
            f"{response.text[:500]}"
        )

    except Exception as e:

        log(
            f"❌ Telegram error: {e}"
        )

    return False


# =========================================================
# TELEGRAM HELP
# =========================================================

def telegram_help():

    return (
        "🥇 GOLD SMART V5.4\n\n"

        "🟢 BOT: ONLINE\n"

        f"{'🟢' if ENGINE_STARTED else '🔴'} "
        f"ENGINE: "
        f"{'RUNNING' if ENGINE_STARTED else 'STOPPED'}\n"

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

        f"{'🟢' if BOT_TOKEN else '🔴'} "
        f"Telegram: "
        f"{'CONFIGURED' if BOT_TOKEN else 'NOT CONFIGURED'}\n"

        f"{'🟢' if WEBHOOK_READY else '🟡'} "
        f"Webhook: "
        f"{'OK' if WEBHOOK_READY else 'NOT CONFIRMED'}\n"

        f"{'🟢' if ENGINE_STARTED else '🔴'} "
        f"Engine: "
        f"{'RUNNING' if ENGINE_STARTED else 'STOPPED'}\n"

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

        f"📌 TOTAL CHECKS: "
        f"{_stats['signals']}\n\n"

        f"🟢 BUY: "
        f"{_stats['buy']}\n"

        f"🔴 SELL: "
        f"{_stats['sell']}\n"

        f"🟡 EARLY BUY: "
        f"{_stats['early_buy']}\n"

        f"🟠 EARLY SELL: "
        f"{_stats['early_sell']}\n"

        f"⚪ WAIT: "
        f"{_stats['wait']}\n"

        f"❌ ERRORS: "
        f"{_stats['errors']}\n\n"

        "━━━━━━━━━━━━━━━━━━\n"

        f"📡 SOURCE: "
        f"{SOURCE_NAME}\n"

        "🛡 RISK: 1%\n"

        "🤖 AUTO TRADING: OFF"
    )


# =========================================================
# DATA NORMALIZATION
# =========================================================

def normalize_dataframe(df):

    if df is None:
        return None

    if len(df) == 0:
        return None

    try:

        df = df.copy()

        if isinstance(
            df.columns,
            pd.MultiIndex
        ):

            df.columns = [
                column[0]
                if isinstance(
                    column,
                    tuple
                )
                else column
                for column in df.columns
            ]

        required = [
            "Open",
            "High",
            "Low",
            "Close",
        ]

        for column in required:

            if column not in df.columns:

                log(
                    f"❌ Missing column: "
                    f"{column}"
                )

                return None

        if "Volume" not in df.columns:

            df["Volume"] = 0

        for column in [
            "Open",
            "High",
            "Low",
            "Close",
            "Volume",
        ]:

            df[column] = pd.to_numeric(
                df[column],
                errors="coerce"
            )

        df = df.dropna(
            subset=required
        )

        if len(df) < 3:

            return None

        return df

    except Exception as e:

        log(
            f"❌ Normalize error: {e}"
        )

        return None


# =========================================================
# REMOVE INCOMPLETE CANDLE
# =========================================================

def remove_incomplete_candle(df):

    if df is None:
        return None

    if len(df) < 3:
        return None

    df = df.copy()

    try:

        df.index = pd.to_datetime(
            df.index
        )

        if df.index.tz is None:

            df.index = (
                df.index
                .tz_localize("UTC")
            )

        else:

            df.index = (
                df.index
                .tz_convert("UTC")
            )

    except Exception:

        pass

    # Только закрытые свечи.
    # Последняя свеча удаляется.

    df = df.iloc[:-1].copy()

    if len(df) < 2:

        return None

    return df


# =========================================================
# YAHOO DOWNLOAD
# =========================================================

def yahoo_download(
    interval,
    period
):

    global _yahoo_block_until

    if time.time() < _yahoo_block_until:

        remaining = int(
            _yahoo_block_until
            - time.time()
        )

        log(
            f"⏳ Yahoo cooldown "
            f"{remaining}s"
        )

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

        df = normalize_dataframe(
            df
        )

        if df is None:

            return None

        return df

    except Exception as e:

        error_text = str(e)

        if (
            "429" in error_text
            or "rate" in error_text.lower()
            or "too many"
            in error_text.lower()
        ):

            _yahoo_block_until = (
                time.time()
                + YAHOO_ERROR_COOLDOWN
            )

            log(
                f"⚠️ Yahoo rate limit. "
                f"Pause "
                f"{YAHOO_ERROR_COOLDOWN}s"
            )

        else:

            log(
                f"❌ Yahoo download "
                f"error: {error_text}"
            )

        return None


# =========================================================
# GET DATA WITH CACHE
# =========================================================

def get_data(interval):

    now = time.time()

    cached = _cache.get(
        interval
    )

    if cached:

        cached_time, cached_df = (
            cached
        )

        ttl = CACHE_TTL.get(
            interval,
            60
        )

        if (
            now - cached_time
            < ttl
        ):

            return cached_df.copy()

    periods = {
        "5m": "5d",
        "15m": "10d",
        "1h": "30d",
    }

    period = periods.get(
        interval,
        "10d"
    )

    df = yahoo_download(
        interval,
        period
    )

    if df is None:

        return None

    df = remove_incomplete_candle(
        df
    )

    if df is None:

        return None

    _cache[interval] = (
        now,
        df.copy()
    )

    return df


# =========================================================
# GET 4H DATA
# =========================================================

def get_4h_data():

    cached = _cache.get(
        "4h"
    )

    now = time.time()

    if cached:

        cached_time, cached_df = (
            cached
        )

        if (
            now - cached_time
            < CACHE_TTL["4h"]
        ):

            return cached_df.copy()

    df = get_data(
        "1h"
    )

    if df is None:

        return None

    try:

        h4 = df.resample(
            "4h"
        ).agg(
            {
                "Open": "first",
                "High": "max",
                "Low": "min",
                "Close": "last",
                "Volume": "sum",
            }
        )

        h4 = h4.dropna()

        if len(h4) < 3:

            return None

        h4 = h4.iloc[:-1].copy()

        if len(h4) < 2:

            return None

        _cache["4h"] = (
            now,
            h4.copy()
        )

        return h4

    except Exception as e:

        log(
            f"❌ 4H resample "
            f"error: {e}"
        )

        return None


# =========================================================
# EMA
# =========================================================

def ema(
    series,
    period
):

    return (
        series
        .ewm(
            span=period,
            adjust=False
        )
        .mean()
    )


# =========================================================
# RSI
# =========================================================

def calculate_rsi(
    series,
    period=14
):

    delta = series.diff()

    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )

    avg_gain = (
        gain
        .ewm(
            alpha=1 / period,
            adjust=False
        )
        .mean()
    )

    avg_loss = (
        loss
        .ewm(
            alpha=1 / period,
            adjust=False
        )
        .mean()
    )

    rs = (
        avg_gain
        /
        avg_loss.replace(
            0,
            np.nan
        )
    )

    return (
        100
        -
        (
            100
            /
            (1 + rs)
        )
    )


# =========================================================
# TREND
# =========================================================

def get_trend(df):

    if df is None:

        return "MIXED"

    if len(df) < 60:

        return "MIXED"

    close = df["Close"]

    ema50 = ema(
        close,
        50
    )

    ema200 = ema(
        close,
        200
    )

    price = close.iloc[-1]

    e50 = ema50.iloc[-1]

    e200 = ema200.iloc[-1]

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
    lookback=12
):

    if df is None:

        return "NONE"

    if len(df) < lookback + 2:

        return "NONE"

    current_close = (
        df["Close"].iloc[-1]
    )

    previous = df.iloc[
        -(lookback + 1):-1
    ]

    previous_high = (
        previous["High"].max()
    )

    previous_low = (
        previous["Low"].min()
    )

    if (
        current_close
        > previous_high
    ):

        return "BULLISH BOS"

    if (
        current_close
        < previous_low
    ):

        return "BEARISH BOS"

    return "NONE"


# =========================================================
# LIQUIDITY SWEEP
# =========================================================

def detect_liquidity_sweep(
    df,
    lookback=10
):

    if df is None:

        return "NONE"

    if len(df) < lookback + 2:

        return "NONE"

    current = df.iloc[-1]

    previous = df.iloc[
        -(lookback + 1):-1
    ]

    previous_high = (
        previous["High"].max()
    )

    previous_low = (
        previous["Low"].min()
    )

    if (
        current["High"]
        > previous_high
        and
        current["Close"]
        < previous_high
    ):

        return "BSL SWEEP"

    if (
        current["Low"]
        < previous_low
        and
        current["Close"]
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

    if df is None:

        return "NONE"

    if len(df) < 25:

        return "NONE"

    current = df.iloc[-1]

    body = abs(
        current["Close"]
        -
        current["Open"]
    )

    previous = df.iloc[
        -21:-1
    ]

    ranges = (
        previous["High"]
        -
        previous["Low"]
    )

    mean_range = (
        ranges.mean()
    )

    if (
        mean_range <= 0
        or
        np.isnan(mean_range)
    ):

        return "NONE"

    if body >= (
        mean_range * 1.5
    ):

        if (
            current["Close"]
            >
            current["Open"]
        ):

            return "BULLISH"

        if (
            current["Close"]
            <
            current["Open"]
        ):

            return "BEARISH"

    return "NONE"


# =========================================================
# MOMENTUM
# =========================================================

def get_momentum(df):

    if df is None:

        return "MIXED"

    if len(df) < 50:

        return "MIXED"

    e20 = ema(
        df["Close"],
        20
    )

    e50 = ema(
        df["Close"],
        50
    )

    if (
        e20.iloc[-1]
        >
        e50.iloc[-1]
    ):

        return "BULLISH"

    if (
        e20.iloc[-1]
        <
        e50.iloc[-1]
    ):

        return "BEARISH"

    return "MIXED"


# =========================================================
# MARKET ZONE
# =========================================================

def get_zone(df):

    if df is None:

        return "EQUILIBRIUM"

    if len(df) < 50:

        return "EQUILIBRIUM"

    recent = df.iloc[-50:]

    high = recent[
        "High"
    ].max()

    low = recent[
        "Low"
    ].min()

    midpoint = (
        high + low
    ) / 2

    price = (
        df["Close"].iloc[-1]
    )

    if price > midpoint:

        return "PREMIUM"

    if price < midpoint:

        return "DISCOUNT"

    return "EQUILIBRIUM"


# =========================================================
# ANALYZE TIMEFRAME
# =========================================================

def analyze_timeframe(df):

    if df is None:

        return {
            "trend": "MIXED",
            "bos": "NONE",
            "liquidity": "NONE",
            "fvg": "NONE",
            "displacement": "NONE",
            "momentum": "MIXED",
            "zone": "EQUILIBRIUM",
            "rsi": 50.0,
        }

    rsi_series = calculate_rsi(
        df["Close"]
    )

    rsi_value = (
        rsi_series.iloc[-1]
    )

    if pd.isna(
        rsi_value
    ):

        rsi_value = 50.0

    return {
        "trend":
            get_trend(df),

        "bos":
            detect_bos(df),

        "liquidity":
            detect_liquidity_sweep(
                df
            ),

        "fvg":
            detect_fvg(df),

        "displacement":
            detect_displacement(
                df
            ),

        "momentum":
            get_momentum(df),

        "zone":
            get_zone(df),

        "rsi":
            float(rsi_value),
    }


# =========================================================
# MARKET ANALYSIS
# =========================================================

def analyze_market():

    h4_df = get_4h_data()

    h1_df = get_data(
        "1h"
    )

    m15_df = get_data(
        "15m"
    )

    m5_df = get_data(
        "5m"
    )

    if (
        h4_df is None
        or h1_df is None
        or m15_df is None
        or m5_df is None
    ):

        raise RuntimeError(
            "Market data unavailable"
        )

    h4 = analyze_timeframe(
        h4_df
    )

    h1 = analyze_timeframe(
        h1_df
    )

    m15 = analyze_timeframe(
        m15_df
    )

    m5 = analyze_timeframe(
        m5_df
    )

    buy_score = 0

    sell_score = 0


    # =====================================================
    # H4
    # =====================================================

    if h4["trend"] == "BULLISH":

        buy_score += 20

    elif h4["trend"] == "BEARISH":

        sell_score += 20


    # =====================================================
    # H1
    # =====================================================

    if h1["trend"] == "BULLISH":

        buy_score += 15

    elif h1["trend"] == "BEARISH":

        sell_score += 15


    # =====================================================
    # M15
    # =====================================================

    if m15["trend"] == "BULLISH":

        buy_score += 15

    elif m15["trend"] == "BEARISH":

        sell_score += 15


    # =====================================================
    # M5
    # =====================================================

    if m5["trend"] == "BULLISH":

        buy_score += 10

    elif m5["trend"] == "BEARISH":

        sell_score += 10


    # =====================================================
    # LIQUIDITY
    # =====================================================

    if (
        m5["liquidity"]
        == "SSL SWEEP"
    ):

        buy_score += 10

    elif (
        m5["liquidity"]
        == "BSL SWEEP"
    ):

        sell_score += 10


    # =====================================================
    # BOS
    # =====================================================

    if (
        m5["bos"]
        == "BULLISH BOS"
    ):

        buy_score += 15

    elif (
        m5["bos"]
        == "BEARISH BOS"
    ):

        sell_score += 15


    # =====================================================
    # FVG
    # =====================================================

    if (
        m5["fvg"]
        == "BULLISH FVG"
    ):

        buy_score += 5

    elif (
        m5["fvg"]
        == "BEARISH FVG"
    ):

        sell_score += 5


    # =====================================================
    # DISPLACEMENT
    # =====================================================

    if (
        m5["displacement"]
        == "BULLISH"
    ):

        buy_score += 10

    elif (
        m5["displacement"]
        == "BEARISH"
    ):

        sell_score += 10


    # =====================================================
    # MOMENTUM
    # =====================================================

    if (
        m5["momentum"]
        == "BULLISH"
    ):

        buy_score += 5

    elif (
        m5["momentum"]
        == "BEARISH"
    ):

        sell_score += 5


    # =====================================================
    # RSI
    # =====================================================

    rsi = m5["rsi"]

    if (
        50 <= rsi <= 68
    ):

        buy_score += 5

    elif (
        32 <= rsi < 50
    ):

        sell_score += 5


    # =====================================================
    # SIGNAL
    # =====================================================

    if (
        buy_score >= MIN_SCORE
        and
        buy_score > sell_score
    ):

        signal = "BUY"

    elif (
        sell_score >= MIN_SCORE
        and
        sell_score > buy_score
    ):

        signal = "SELL"

    elif (
        buy_score >= EARLY_SCORE
        and
        buy_score > sell_score
    ):

        signal = "EARLY BUY"

    elif (
        sell_score >= EARLY_SCORE
        and
        sell_score > buy_score
    ):

        signal = "EARLY SELL"

    else:

        signal = "WAIT"


    # =====================================================
    # UP / DOWN
    # =====================================================

    total_score = (
        buy_score
        +
        sell_score
    )

    if total_score > 0:

        up = (
            buy_score
            /
            total_score
            *
            100
        )

        down = (
            100
            -
            up
        )

    else:

        up = 50.0
        down = 50.0


    price = float(
        m5_df["Close"].iloc[-1]
    )

    return {

        "price":
            price,

        "signal":
            signal,

        "buy_score":
            buy_score,

        "sell_score":
            sell_score,

        "up":
            up,

        "down":
            down,

        "h4":
            h4,

        "h1":
            h1,

        "m15":
            m15,

        "m5":
            m5,
    }


# =========================================================
# TRADE SETUP
# =========================================================

def make_setup(
    analysis
):

    signal = analysis[
        "signal"
    ]

    if signal not in (
        "BUY",
        "SELL",
        "EARLY BUY",
        "EARLY SELL",
    ):

        return None

    entry = float(
        analysis["price"]
    )

    sl_distance = 5.0

    if signal in (
        "BUY",
        "EARLY BUY",
    ):

        sl = (
            entry
            -
            sl_distance
        )

        tp = (
            entry
            +
            sl_distance
            *
            TARGET_RR
        )

    else:

        sl = (
            entry
            +
            sl_distance
        )

        tp = (
            entry
            -
            sl_distance
            *
            TARGET_RR
        )

    risk_money = (
        DEFAULT_DEPOSIT
        *
        RISK_PERCENT
        /
        100
    )

    lot = (
        risk_money
        /
        (
            sl_distance
            *
            100
        )
    )

    lot = max(
        MIN_LOT,
        min(
            MAX_LOT,
            lot
        )
    )

    return {

        "entry":
            entry,

        "sl":
            sl,

        "tp":
            tp,

        "lot":
            lot,

        "rr":
            TARGET_RR,

        "risk_money":
            risk_money,

        "sl_distance":
            sl_distance,
    }


# =========================================================
# FORMAT SIGNAL
# =========================================================

def format_signal(
    analysis
):

    signal = analysis[
        "signal"
    ]

    emoji = {

        "BUY":
            "🟢",

        "SELL":
            "🔴",

        "EARLY BUY":
            "🟡",

        "EARLY SELL":
            "🟠",

        "WAIT":
            "⚪",

    }.get(
        signal,
        "⚪"
    )

    message = (

        "🥇 GOLD SMART V5.4\n\n"

        f"{emoji} SIGNAL: "
        f"{signal}\n\n"

        f"💰 XAUUSD: "
        f"{analysis['price']:.2f}\n\n"

        f"📊 H4: "
        f"{analysis['h4']['trend']}\n"

        f"📊 H1: "
        f"{analysis['h1']['trend']}\n"

        f"📊 M15: "
        f"{analysis['m15']['trend']}\n"

        f"📊 M5: "
        f"{analysis['m5']['trend']}\n\n"

        f"💧 M5 Liquidity: "
        f"{analysis['m5']['liquidity']}\n"

        f"🔨 M5 BOS: "
        f"{analysis['m5']['bos']}\n"

        f"🧩 M5 FVG: "
        f"{analysis['m5']['fvg']}\n"

        f"💥 M5 Displacement: "
        f"{analysis['m5']['displacement']}\n"

        f"📈 Momentum: "
        f"{analysis['m5']['momentum']}\n"

        f"📐 Zone: "
        f"{analysis['m5']['zone']}\n"

        f"RSI: "
        f"{analysis['m5']['rsi']:.1f}\n\n"

        f"📈 UP: "
        f"{analysis['up']:.1f}%\n"

        f"📉 DOWN: "
        f"{analysis['down']:.1f}%\n\n"

        f"🟢 BUY SCORE: "
        f"{analysis['buy_score']}\n"

        f"🔴 SELL SCORE: "
        f"{analysis['sell_score']}\n"
    )

    setup = make_setup(
        analysis
    )

    if setup:

        message += (

            "\n━━━━━━━━━━━━━━━━━━\n"

            "🎯 TRADE SETUP\n\n"

            f"📍 Entry: "
            f"{setup['entry']:.2f}\n"

            f"🛑 SL: "
            f"{setup['sl']:.2f}\n"

            f"🎯 TP: "
            f"{setup['tp']:.2f}\n"

            f"📦 Lot: "
            f"{setup['lot']:.2f}\n"

            f"⚖️ RR: "
            f"1:{setup['rr']:.0f}\n"

            f"💰 Risk: "
            f"{RISK_PERCENT:.1f}%\n"
        )

    message += (

        "\n━━━━━━━━━━━━━━━━━━\n"

        "🕯 CLOSED CANDLE: ON\n"

        f"📡 SOURCE: "
        f"{SOURCE_NAME}\n"

        "🤖 AUTO TRADING: OFF\n"

        "⚠️ DEMO / INFORMATIONAL"
    )

    return message


# =========================================================
# PROCESS SIGNAL
# =========================================================

def process_signal(
    analysis
):

    global _last_signal
    global _last_signal_time

    signal = analysis[
        "signal"
    ]

    _stats[
        "signals"
    ] += 1

    if signal == "BUY":

        _stats[
            "buy"
        ] += 1

    elif signal == "SELL":

        _stats[
            "sell"
        ] += 1

    elif signal == "EARLY BUY":

        _stats[
            "early_buy"
        ] += 1

    elif signal == "EARLY SELL":

        _stats[
            "early_sell"
        ] += 1

    elif signal == "WAIT":

        _stats[
            "wait"
        ] += 1


    # =====================================================
    # WAIT
    # =====================================================

    if signal == "WAIT":

        log(
            f"📊 WAIT | "
            f"XAUUSD "
            f"{analysis['price']:.2f} | "
            f"BUY "
            f"{analysis['buy_score']} | "
            f"SELL "
            f"{analysis['sell_score']}"
        )

        return


    # =====================================================
    # COOLDOWN
    # =====================================================

    now = time.time()

    if (
        _last_signal == signal
        and
        (
            now
            -
            _last_signal_time
        )
        <
        COOLDOWN_MIN * 60
    ):

        log(
            f"⏳ COOLDOWN | "
            f"{signal}"
        )

        return


    # =====================================================
    # NEW SIGNAL
    # =====================================================

    _last_signal = signal

    _last_signal_time = now

    message = format_signal(
        analysis
    )

    log(
        f"🚨 NEW SIGNAL | "
        f"{signal} | "
        f"XAUUSD "
        f"{analysis['price']:.2f}"
    )

    send_telegram_message(
        message
    )


# =========================================================
# TELEGRAM UPDATE HANDLER
# =========================================================

def handle_telegram_update(
    update
):

    try:

        message = update.get(
            "message"
        )

        if not message:

            return

        chat = message.get(
            "chat",
            {}
        )

        chat_id = chat.get(
            "id"
        )

        text = (
            message
            .get(
                "text",
                ""
            )
            .strip()
            .lower()
        )

        if not chat_id:

            return

        log(
            f"📩 Telegram command: "
            f"{text}"
        )


        # =================================================
        # START
        # =================================================

        if text.startswith(
            "/start"
        ):

            send_telegram_message(
                telegram_help(),
                chat_id
            )

            return


        # =================================================
        # HELP
        # =================================================

        if text.startswith(
            "/help"
        ):

            send_telegram_message(
                telegram_help(),
                chat_id
            )

            return


        # =================================================
        # TEST
        # =================================================

        if text.startswith(
            "/test"
        ):

            send_telegram_message(
                telegram_test(),
                chat_id
            )

            return


        # =================================================
        # STATS
        # =================================================

        if text.startswith(
            "/stats"
        ):

            send_telegram_message(
                telegram_stats(),
                chat_id
            )

            return


        # =================================================
        # STATUS
        # =================================================

        if text.startswith(
            "/status"
        ):

            try:

                analysis = (
                    analyze_market()
                )

                message = (
                    format_signal(
                        analysis
                    )
                )

                send_telegram_message(
                    message,
                    chat_id
                )

            except Exception as e:

                _stats[
                    "errors"
                ] += 1

                log(
                    f"❌ /status error: "
                    f"{e}"
                )

                send_telegram_message(
                    "⚠️ GOLD SMART V5.4\n\n"
                    "❌ Market data "
                    "temporarily unavailable.\n"
                    f"Reason: {str(e)[:300]}",
                    chat_id
                )

            return


        # =================================================
        # SIGNAL
        # =================================================

        if text.startswith(
            "/signal"
        ):

            try:

                analysis = (
                    analyze_market()
                )

                message = (
                    format_signal(
                        analysis
                    )
                )

                send_telegram_message(
                    message,
                    chat_id
                )

            except Exception as e:

                _stats[
                    "errors"
                ] += 1

                log(
                    f"❌ /signal error: "
                    f"{e}"
                )

                send_telegram_message(
                    "⚠️ SIGNAL ERROR\n\n"
                    "❌ Market data "
                    "temporarily unavailable.\n"
                    f"Reason: {str(e)[:300]}",
                    chat_id
                )

            return

    except Exception as e:

        _stats[
            "errors"
        ] += 1

        log(
            f"❌ Telegram handler "
            f"error: {e}"
        )


# =========================================================
# ROOT
# =========================================================

@app.route(
    "/",
    methods=[
        "GET",
        "HEAD"
    ]
)
def home():

    return (
        "🥇 GOLD SMART V5.4 ONLINE",
        200
    )


# =========================================================
# HEALTH
