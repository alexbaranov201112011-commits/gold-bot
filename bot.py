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
# DATA SOURCE:
# Yahoo Finance / GC=F
#
# FEATURES:
# H4 → H1 → M15 → M5
# Liquidity Sweep
# BOS
# FVG
# Displacement
# Momentum
# Premium / Discount
# RSI
# Score Engine
# CLOSED CANDLE ONLY
# Telegram
# Flask
#
# AUTO TRADING = OFF
# RISK = 1%
# =========================================================


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

WEBHOOK_URL = "https://gold-bot-q8la.onrender.com"

YF_SYMBOL = "GC=F"
SOURCE_NAME = "Yahoo Finance / GC=F"

RISK_PERCENT = 1.0
DEFAULT_DEPOSIT = 800.0

MIN_LOT = 0.01
MAX_LOT = 0.02

MIN_SCORE = 70
EARLY_SCORE = 60

TARGET_RR = 3.0
MIN_RR = 2.0

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
# GLOBALS
# =========================================================

app = Flask(__name__)

ENGINE_STARTED = False

_last_signal = None
_last_signal_time = 0

_stats = {
    "signals": 0,
    "buy": 0,
    "sell": 0,
    "early_buy": 0,
    "early_sell": 0,
    "wait": 0,
    "errors": 0,
}

_cache = {}

_yahoo_block_until = 0


# =========================================================
# LOG
# =========================================================

def log(message):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {message}", flush=True)


# =========================================================
# TELEGRAM
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
            f"❌ Telegram HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )

    except Exception as e:
        log(f"❌ Telegram error: {e}")

    return False


# =========================================================
# TELEGRAM COMMANDS
# =========================================================

def telegram_help():
    return (
        "🥇 GOLD SMART V5.4\n\n"
        "📋 КОМАНДЫ:\n\n"
        "/start — запуск и список команд\n"
        "/status — текущее состояние рынка\n"
        "/test — проверка Telegram\n"
        "/stats — статистика сигналов\n"
        "/help — список команд\n\n"
        "📡 Source: Yahoo Finance / GC=F\n"
        "🕯 Closed Candle Engine: ON\n"
        "🛡 Risk: 1%\n"
        "🤖 Auto Trading: OFF"
    )


def telegram_test():
    return (
        "✅ GOLD SMART V5.4 — TEST\n\n"
        "🟢 Telegram: OK\n"
        "🟢 Engine: RUNNING\n"
        "🟢 Yahoo Finance: CONFIGURED\n"
        "🕯 Closed Candle Engine: ON\n"
        "🛡 Risk: 1%\n"
        "🤖 Auto Trading: OFF"
    )


def telegram_stats():
    total = _stats["signals"]

    return (
        "📊 GOLD SMART V5.4 — STATISTICS\n\n"
        f"📌 TOTAL SIGNALS: {total}\n\n"
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
# YAHOO DATA
# =========================================================

def normalize_dataframe(df):
    if df is None or df.empty:
        return None

    try:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df.copy()

        needed = [
            "Open",
            "High",
            "Low",
            "Close",
            "Volume",
        ]

        for column in needed:
            if column not in df.columns:
                if column == "Volume":
                    df[column] = 0
                else:
                    return None

        df = df[needed].copy()

        for column in needed:
            df[column] = pd.to_numeric(
                df[column],
                errors="coerce"
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
        log(f"❌ Data normalization error: {e}")
        return None


def remove_incomplete_candle(df):
    """
    IMPORTANT:
    Always remove the latest potentially incomplete candle.
    """

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
    # This guarantees closed-candle analysis.
    df = df.iloc[:-1].copy()

    if len(df) < 2:
        return None

    return df


def yahoo_download(interval, period):
    global _yahoo_block_until

    now = time.time()

    if now < _yahoo_block_until:
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

        if df is None or df.empty:
            return None

        return df

    except Exception as e:
        error_text = str(e)

        if "429" in error_text or "rate" in error_text.lower():
            _yahoo_block_until = time.time() + YAHOO_ERROR_COOLDOWN
            log(
                f"⚠️ Yahoo rate limit. "
                f"Pause {YAHOO_ERROR_COOLDOWN}s"
            )
        else:
            log(f"❌ Yahoo download error: {error_text}")

        return None


def get_data(interval):
    now = time.time()

    cached = _cache.get(interval)

    if cached:
        timestamp, data = cached

        if now - timestamp < CACHE_TTL.get(
            interval,
            60
        ):
            return data

    periods = {
        "5m": "5d",
        "15m": "10d",
        "1h": "30d",
    }

    period = periods.get(interval, "10d")

    df = yahoo_download(
        interval,
        period
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


def get_4h_data():
    now = time.time()

    cached = _cache.get("4h")

    if cached:
        timestamp, data = cached

        if now - timestamp < CACHE_TTL["4h"]:
            return data

    df_1h = get_data("1h")

    if df_1h is None or df_1h.empty:
        return None

    try:
        df = df_1h.copy()

        df = df.resample("4h").agg({
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        })

        df = df.dropna()

        if len(df) < 3:
            return None

        # The 1H source has already removed its current
        # incomplete candle. We also remove the latest
        # resampled 4H candle for extra safety.
        df = df.iloc[:-1].copy()

        _cache["4h"] = (
            now,
            df,
        )

        return df

    except Exception as e:
        log(f"❌ 4H resample error: {e}")
        return None


# =========================================================
# INDICATORS
# =========================================================

def ema(series, period):
    return series.ewm(
        span=period,
        adjust=False
    ).mean()


def calculate_rsi(series, period=14):
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

    rs = avg_gain / avg_loss.replace(
        0,
        np.nan
    )

    rsi = 100 - (
        100 / (1 + rs)
    )

    return rsi


# =========================================================
# TREND
# =========================================================

def get_trend(df):
    if df is None or len(df) < 60:
        return "MIXED"

    close = df["Close"]

    ema50 = ema(close, 50)
    ema200 = ema(close, 200)

    price = float(close.iloc[-1])
    e50 = float(ema50.iloc[-1])
    e200 = float(ema200.iloc[-1])

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

def detect_bos(df, lookback=12):
    if df is None or len(df) < lookback + 3:
        return "NONE"

    previous = df.iloc[
        -(lookback + 1):-1
    ]

    current = df.iloc[-1]

    previous_high = previous["High"].max()
    previous_low = previous["Low"].min()

    if current["Close"] > previous_high:
        return "BULLISH BOS"

    if current["Close"] < previous_low:
        return "BEARISH BOS"

    return "NONE"


# =========================================================
# LIQUIDITY SWEEP
# =========================================================

def detect_liquidity(df, lookback=10):
    if df is None or len(df) < lookback + 2:
        return "NONE"

    previous = df.iloc[
        -(lookback + 1):-1
    ]

    current = df.iloc[-1]

    previous_high = previous["High"].max()
    previous_low = previous["Low"].min()

    # Buy-side liquidity sweep:
    # price trades above previous highs
    # but closes back below them.
    if (
        current["High"] > previous_high
        and current["Close"] < previous_high
    ):
        return "BSL SWEEP"

    # Sell-side liquidity sweep:
    # price trades below previous lows
    # but closes back above them.
    if (
        current["Low"] < previous_low
        and current["Close"] > previous_low
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

    # Bullish FVG:
    # current low > first candle high
    if c3["Low"] > c1["High"]:
        return "BULLISH FVG"

    # Bearish FVG:
    # current high < first candle low
    if c3["High"] < c1["Low"]:
        return "BEARISH FVG"

    return "NONE"


# =========================================================
# DISPLACEMENT
# =========================================================

def detect_displacement(df):
    if df is None or len(df) < 25:
        return "NONE"

    recent = df.iloc[-21:-1].copy()

    recent["range"] = (
        recent["High"]
        - recent["Low"]
    )

    avg_range = recent["range"].mean()

    current = df.iloc[-1]

    current_range = (
        current["High"]
        - current["Low"]
    )

    body = abs(
        current["Close"]
        - current["Open"]
    )

    if avg_range <= 0:
        return "NONE"

    if (
        body >= avg_range * 1.5
        and current["Close"]
        > current["Open"]
    ):
        return "BULLISH"

    if (
        body >= avg_range * 1.5
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

    ema20 = ema(close, 20)
    ema50 = ema(close, 50)

    price = float(close.iloc[-1])
    e20 = float(ema20.iloc[-1])
    e50 = float(ema50.iloc[-1])

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

def get_zone(df, lookback=50):
    if df is None or len(df) < lookback:
        return "EQUILIBRIUM"

    recent = df.iloc[-lookback:]

    high = float(recent["High"].max())
    low = float(recent["Low"].min())

    if high <= low:
        return "EQUILIBRIUM"

    price = float(df["Close"].iloc[-1])

    midpoint = (
        high + low
    ) / 2

    if price > midpoint:
        return "PREMIUM"

    if price < midpoint:
        return "DISCOUNT"

    return "EQUILIBRIUM"


# =========================================================
# RSI
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
# TIMEFRAME ANALYSIS
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

    if any(
        x is None or x.empty
        for x in [
            df_4h,
            df_1h,
            df_15m,
            df_5m,
        ]
    ):
        raise RuntimeError(
            "Market data unavailable"
        )

    h4 = analyze_timeframe(df_4h)
    h1 = analyze_timeframe(df_1h)
    m15 = analyze_timeframe(df_15m)
    m5 = analyze_timeframe(df_5m)

    price = float(
        df_5m["Close"].iloc[-1]
    )

    buy_score = 0
    sell_score = 0

    # -----------------------------------------------------
    # H4 TREND — 20
    # -----------------------------------------------------

    if h4["trend"] == "BULLISH":
        buy_score += 20

    elif h4["trend"] == "BEARISH":
        sell_score += 20

    # -----------------------------------------------------
    # H1 TREND — 15
    # -----------------------------------------------------

    if h1["trend"] == "BULLISH":
        buy_score += 15

    elif h1["trend"] == "BEARISH":
        sell_score += 15

    # -----------------------------------------------------
    # M15 TREND — 15
    # -----------------------------------------------------

    if m15["trend"] == "BULLISH":
        buy_score += 15

    elif m15["trend"] == "BEARISH":
        sell_score += 15

    # -----------------------------------------------------
    # M5 TREND — 10
    # -----------------------------------------------------

    if m5["trend"] == "BULLISH":
        buy_score += 10

    elif m5["trend"] == "BEARISH":
        sell_score += 10

    # -----------------------------------------------------
    # LIQUIDITY — 10
    # -----------------------------------------------------

    if m5["liquidity"] == "SSL SWEEP":
        buy_score += 10

    elif m5["liquidity"] == "BSL SWEEP":
        sell_score += 10

    # -----------------------------------------------------
    # BOS — 15
    # -----------------------------------------------------

    if m5["bos"] == "BULLISH BOS":
        buy_score += 15

    elif m5["bos"] == "BEARISH BOS":
        sell_score += 15

    # -----------------------------------------------------
    # FVG — 5
    # -----------------------------------------------------

    if m5["fvg"] == "BULLISH FVG":
        buy_score += 5

    elif m5["fvg"] == "BEARISH FVG":
        sell_score += 5

    # -----------------------------------------------------
    # DISPLACEMENT — 10
    # -----------------------------------------------------

    if m5["displacement"] == "BULLISH":
        buy_score += 10

    elif m5["displacement"] == "BEARISH":
        sell_score += 10

    # -----------------------------------------------------
    # MOMENTUM — 5
    # -----------------------------------------------------

    if m5["momentum"] == "BULLISH":
        buy_score += 5

    elif m5["momentum"] == "BEARISH":
        sell_score += 5

    # -----------------------------------------------------
    # RSI — 5
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

    total_score = (
        buy_score + sell_score
    )

    if total_score > 0:
        up_percent = round(
            buy_score
            / total_score
            * 100
        )

        down_percent = 100 - up_percent

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

    # Conservative fixed distance.
    # IMPORTANT:
    # This is an approximate setup for signal display,
    # NOT broker-specific execution sizing.
    sl_distance = 5.0

    if signal in [
        "BUY",
        "EARLY BUY",
    ]:
        direction = "BUY"

        entry = price
        sl = entry - sl_distance
        tp = entry + (
            sl_distance * TARGET_RR
        )

    else:
        direction = "SELL"

        entry = price
        sl = entry + sl_distance
        tp = entry - (
            sl_distance * TARGET_RR
        )

    risk_money = (
        DEFAULT_DEPOSIT
        * RISK_PERCENT
        / 100
    )

    # Approximate lot.
    lot = risk_money / (
        sl_distance * 100
    )

    lot = max(
        MIN_LOT,
        min(MAX_LOT, lot)
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
    }.get(signal, "⚪")

    m5 = analysis["m5"]

    lines = []

    lines.append(
        "🥇 GOLD SMART V5.4"
    )
    lines.append("")
    lines.append(
        f"{emoji} SIGNAL: {signal}"
    )
    lines.append("")
    lines.append(
        f"💰 XAUUSD: {analysis['price']:.2f}"
    )
    lines.append("")
    lines.append(
        f"📊 H4: {analysis['h4_trend']}"
    )
    lines.append(
        f"📊 H1: {analysis['h1_trend']}"
    )
    lines.append(
        f"📊 M15: {analysis['m15_trend']}"
    )
    lines.append(
        f"📊 M5: {analysis['m5_trend']}"
    )
    lines.append("")
    lines.append(
        f"💧 M5 Liquidity: {m5['liquidity']}"
    )
    lines.append(
        f"🔨 M5 BOS: {m5['bos']}"
    )
    lines.append(
        f"🧩 M5 FVG: {m5['fvg']}"
    )
    lines.append(
        f"💥 M5 Displacement: "
        f"{m5['displacement']}"
    )
    lines.append(
        f"📈 Momentum: {m5['momentum']}"
    )
    lines.append(
        f"📐 Zone: {m5['zone']}"
    )
    lines.append(
        f"RSI: {m5['rsi']:.1f}"
    )
    lines.append("")
    lines.append(
        f"📈 UP: {analysis['up_percent']}%"
    )
    lines.append(
        f"📉 DOWN: {analysis['down_percent']}%"
    )
    lines.append(
        f"🎯 BUY SCORE: {analysis['buy_score']}"
    )
    lines.append(
        f"🎯 SELL SCORE: {analysis['sell_score']}"
    )

    setup = make_setup(
        analysis
    )

    if setup:
        lines.append("")
        lines.append(
            "━━━━━━━━━━━━━━━━━━"
        )
        lines.append(
            f"📍 {setup['direction']}"
        )
        lines.append(
            f"ENTRY: {setup['entry']:.2f}"
        )
        lines.append(
            f"SL: {setup['sl']:.2f}"
        )
        lines.append(
            f"TP: {setup['tp']:.2f}"
        )
        lines.append(
            f"LOT: {setup['lot']:.2f}"
        )
        lines.append(
            f"RR: 1:{setup['rr']:.0f}"
        )

    lines.append("")
    lines.append(
        "🤖 AUTO TRADING: OFF"
    )
    lines.append(
        f"📡 SOURCE: {SOURCE_NAME}"
    )
    lines.append(
        "🕯 CLOSED CANDLE ENGINE"
    )

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

    # WAIT is logged but not repeatedly sent to Telegram.
    if signal == "WAIT":
        log(
            f"📊 WAIT | "
            f"XAUUSD {analysis['price']:.2f} | "
            f"BUY {analysis['buy_score']} | "
            f"SELL {analysis['sell_score']}"
        )
        return

    now = time.time()

    # Same signal cooldown.
    if (
        signal == _last_signal
        and (
            now - _last_signal_time
            < COOLDOWN_MIN * 60
        )
    ):
        log(
            f"⏳ Cooldown | {signal}"
        )
        return

    _last_signal = signal
    _last_signal_time = now

    message = format_signal(
        analysis
    )

    log(
        f"🚨 {signal} | "
        f"XAUUSD {analysis['price']:.2f} | "
        f"BUY {analysis['buy_score']} | "
        f"SELL {analysis['sell_score']}"
    )

    send_telegram_message(
        message
    )


# =========================================================
# ENGINE
# =========================================================

def engine_loop():
    log(
        "🥇 GOLD SMART V5.4 ENGINE STARTED"
    )
    log(
        f"📡 SOURCE: {SOURCE_NAME}"
    )
    log(
        f"🛡 RISK: {RISK_PERCENT:g}%"
    )
    log(
        "🤖 AUTO TRADING: OFF"
    )
    log(
        "🕯 CLOSED CANDLE ENGINE"
    )

    while True:
        try:
            analysis = analyze_market()

            process_signal(
                analysis
            )

        except Exception as e:
            _stats["errors"] += 1

            log(
                f"❌ Engine error: {e}"
            )

            traceback.print_exc()

        time.sleep(
            POLL_SECONDS
        )


def start_engine():
    global ENGINE_STARTED

    if ENGINE_STARTED:
        return

    ENGINE_STARTED = True

    thread = threading.Thread(
        target=engine_loop,
        daemon=True,
        name="gold-smart-engine",
    )

    thread.start()

    log(
        "🟢 Engine thread launched"
    )


# =========================================================
# FLASK ROUTES
# =========================================================

@app.route("/", methods=["GET", "HEAD"])
def home():
    return "GOLD SMART V5.4 OK", 200


@app.route("/health", methods=["GET"])
def health():
    return {
        "status": "ok",
        "engine": ENGINE_STARTED,
        "source": SOURCE_NAME,
        "auto_trading": False,
        "risk_percent": RISK_PERCENT,
    }, 200


@app.route("/test", methods=["GET"])
def test():
    return {
        "status": "ok",
        "message": "GOLD SMART V5.4 TEST OK",
    }, 200


@app.route("/stats", methods=["GET"])
def stats():
    return _stats, 200


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

@app.route("/telegram", methods=["POST"])
def telegram_webhook():

    try:
        update = request.get_json(
            silent=True
        ) or {}

        message = update.get(
            "message",
            {}
        )

        text_msg = message.get(
            "text",
            ""
        )

        chat = message.get(
            "chat",
            {}
        )

        chat_id = chat.get(
            "id"
        )

        if not text_msg or not chat_id:
            return {
                "ok": True
            }, 200

        command = (
            text_msg
            .strip()
            .split()[0]
            .lower()
            .split("@")[0]
        )

        log(
            f"📩 Telegram command: "
            f"{command}"
        )

        # -------------------------------------------------
        # START
        # -------------------------------------------------

        if command == "/start":
            reply = telegram_help()

        # -------------------------------------------------
        # HELP
        # -------------------------------------------------

        elif command == "/help":
            reply = telegram_help()

        # -------------------------------------------------
        # TEST
        # -------------------------------------------------

        elif command == "/test":
            reply = telegram_test()

        # -------------------------------------------------
        # STATS
        # -------------------------------------------------

        elif command == "/stats":
            reply = telegram_stats()

        # -------------------------------------------------
        # STATUS
        # -------------------------------------------------

        elif command == "/status":

            try:
                analysis = (
                    analyze_market()
                )

                m5 = analysis["m5"]

                reply = (
                    "🥇 GOLD SMART V5.4\n\n"
                    f"💰 XAUUSD: "
                    f"{analysis['price']:.2f}\n\n"
                    f"📊 H4: "
                    f"{analysis['h4_trend']}\n"
                    f"📊 H1: "
                    f"{analysis['h1_trend']}\n"
                    f"📊 M15: "
                    f"{analysis['m15_trend']}\n"
                    f"📊 M5: "
                    f"{analysis['m5_trend']}\n\n"
                    f"💧 Liquidity: "
                    f"{m5['liquidity']}\n"
                    f"🔨 BOS: "
                    f"{m5['bos']}\n"
                    f"🧩 FVG: "
                    f"{m5['fvg']}\n"
                    f"💥 Displacement: "
                    f"{m5['displacement']}\n"
                    f"📈 Momentum: "
                    f"{m5['momentum']}\n"
                    f"📐 Zone: "
                    f"{m5['zone']}\n"
                    f"RSI: "
                    f"{m5['rsi']:.1f}\n\n"
                    f"📈 BUY: "
                    f"{analysis['buy_score']}\n"
                    f"📉 SELL: "
                    f"{analysis['sell_score']}\n\n"
                    f"🎯 SIGNAL: "
                    f"{analysis['signal']}\n\n"
                    "🛡 RISK: 1%\n"
                    "🤖 AUTO TRADING: OFF"
                )

            except Exception as e:

                _stats["errors"] += 1

                reply = (
                    "❌ Не удалось получить "
                    "данные рынка.\n\n"
                    f"Ошибка: {str(e)}"
                )

        # -------------------------------------------------
        # UNKNOWN
        # -------------------------------------------------

        else:
            reply = (
                "❓ Неизвестная команда.\n\n"
                "Используй:\n"
                "/start\n"
                "/status\n"
                "/test\n"
                "/stats\n"
                "/help"
            )

        send_telegram_message(
            reply,
            chat_id
        )

        return {
            "ok": True
        }, 200

    except Exception as e:

        _stats["errors"] += 1

        log(
            f"❌ Telegram webhook error: "
            f"{e}"
        )

        traceback.print_exc()

        # Telegram should receive HTTP 200
        # so it doesn't endlessly retry.
        return {
            "ok": False
        }, 200


# =========================================================
# OPTIONAL WEBHOOK ROUTE
# =========================================================

@app.route("/webhook", methods=["POST"])
def generic_webhook():
    return {
        "ok": True,
        "message": "Webhook received",
    }, 200


# =========================================================
# START ENGINE
# =========================================================
#
# IMPORTANT:
# Gunicorn starts the module as:
#
# gunicorn bot:app
#
# Therefore __main__ is NOT executed.
# The engine must start during module import.
# =========================================================

start_engine()


# =========================================================
# LOCAL RUN
# =========================================================

if __name__ == "__main__":
    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
