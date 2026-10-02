# ============================================================
# GOLD SMART V5.4
# XAUUSD signal engine
# Data: Yahoo Finance / GC=F
# H4 -> H1 -> M15 -> M5
# CLOSED CANDLE ENGINE
# RISK = 1%
# AUTO TRADING = OFF
# ============================================================

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


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

WEBHOOK_URL = os.getenv(
    "WEBHOOK_URL",
    "https://gold-bot-q8la.onrender.com"
).strip()

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

YF_ERROR_COOLDOWN = 300


# ============================================================
# CACHE
# ============================================================

CACHE = {
    "5min": {
        "ttl": 60,
        "data": None,
        "time": 0
    },
    "15min": {
        "ttl": 180,
        "data": None,
        "time": 0
    },
    "1h": {
        "ttl": 600,
        "data": None,
        "time": 0
    },
    "4h": {
        "ttl": 1800,
        "data": None,
        "time": 0
    }
}


# ============================================================
# GLOBAL STATE
# ============================================================

last_yf_error = 0
last_yf_error_text = ""

last_signal_key = None
last_signal_time = 0

last_error_telegram_time = 0

ENGINE_STARTED = False

STATS = {
    "total": 0,
    "win": 0,
    "loss": 0,
    "be": 0,
    "open": 0
}


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# LOG
# ============================================================

def log(message):
    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S")

    print(
        f"[{timestamp} UTC] {message}",
        flush=True
    )


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(message):

    if not BOT_TOKEN or not CHAT_ID:
        log("Telegram credentials are missing")
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=15
        )

        if response.status_code != 200:

            log(
                "Telegram error: "
                f"{response.status_code} "
                f"{response.text[:500]}"
            )

            return False

        return True

    except Exception as error:

        log(
            "Telegram exception: "
            f"{type(error).__name__}: {error}"
        )

        return False


# ============================================================
# NORMALIZE YAHOO DATA
# ============================================================

def normalize_yahoo_dataframe(df):

    if df is None or df.empty:
        return None

    try:

        # ----------------------------------------------------
        # MultiIndex handling
        # ----------------------------------------------------

        if isinstance(
            df.columns,
            pd.MultiIndex
        ):

            new_columns = []

            for column in df.columns:

                found = None

                for part in column:

                    part_text = (
                        str(part)
                        .lower()
                        .strip()
                    )

                    if part_text in {
                        "open",
                        "high",
                        "low",
                        "close",
                        "adj close",
                        "volume"
                    }:

                        found = part_text
                        break

                if found is None:

                    found = str(
                        column[-1]
                    ).lower().strip()

                new_columns.append(found)

            df.columns = new_columns

        else:

            df.columns = [
                str(column)
                .lower()
                .strip()
                .replace(" ", "_")
                for column in df.columns
            ]

        # ----------------------------------------------------
        # Volume fallback
        # ----------------------------------------------------

        if "volume" not in df.columns:
            df["volume"] = 0

        required = [
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]

        for column in required:

            if column not in df.columns:

                log(
                    f"Yahoo missing column: {column}"
                )

                return None

        df = df[
            required
        ].copy()

        # ----------------------------------------------------
        # Numeric
        # ----------------------------------------------------

        for column in required:

            df[column] = pd.to_numeric(
                df[column],
                errors="coerce"
            )

        df = df.dropna(
            subset=[
                "open",
                "high",
                "low",
                "close"
            ]
        )

        if df.empty:
            return None

        # ----------------------------------------------------
        # Datetime UTC
        # ----------------------------------------------------

        df.index = pd.to_datetime(
            df.index,
            errors="coerce",
            utc=True
        )

        df = df[
            ~df.index.isna()
        ]

        df = df.sort_index()

        df = df[
            ~df.index.duplicated(
                keep="last"
            )
        ]

        if df.empty:
            return None

        return df

    except Exception as error:

        log(
            "Yahoo normalization error: "
            f"{type(error).__name__}: {error}"
        )

        traceback.print_exc()

        return None


# ============================================================
# DOWNLOAD YAHOO
# ============================================================

def download_yahoo(interval):

    global last_yf_error
    global last_yf_error_text

    now = time.time()

    # --------------------------------------------------------
    # Error cooldown
    # --------------------------------------------------------

    if (
        now - last_yf_error
        < YF_ERROR_COOLDOWN
    ):

        log(
            "Yahoo request skipped: "
            "error cooldown active"
        )

        return None

    try:

        if interval == "5min":

            period = "5d"
            yf_interval = "5m"

        elif interval == "15min":

            period = "5d"
            yf_interval = "15m"

        elif interval == "1h":

            period = "1mo"
            yf_interval = "1h"

        elif interval == "4h":

            period = "3mo"
            yf_interval = "1h"

        else:

            raise ValueError(
                f"Unsupported interval: {interval}"
            )

        log(
            f"Yahoo request: "
            f"{YF_SYMBOL} "
            f"interval={yf_interval} "
            f"period={period}"
        )

        df = yf.download(
            tickers=YF_SYMBOL,
            period=period,
            interval=yf_interval,
            auto_adjust=False,
            progress=False,
            threads=False,
            prepost=False
        )

        if df is None or df.empty:

            raise RuntimeError(
                "Yahoo returned empty data"
            )

        df = normalize_yahoo_dataframe(df)

        if df is None or df.empty:

            raise RuntimeError(
                "Yahoo returned invalid dataframe"
            )

        # ----------------------------------------------------
        # Build 4H from 1H
        # ----------------------------------------------------

        if interval == "4h":

            df = (
                df
                .resample("4h")
                .agg({
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum"
                })
                .dropna()
            )

        if df.empty:

            raise RuntimeError(
                "Data became empty after processing"
            )

        log(
            f"Yahoo OK: {interval} | "
            f"rows={len(df)} | "
            f"last={df.index[-1]} | "
            f"close={df['close'].iloc[-1]:.2f}"
        )

        return df

    except Exception as error:

        last_yf_error = time.time()

        last_yf_error_text = (
            f"{type(error).__name__}: {error}"
        )

        log(
            f"Yahoo ERROR {interval}: "
            f"{last_yf_error_text}"
        )

        traceback.print_exc()

        return None


# ============================================================
# REMOVE INCOMPLETE CANDLE
# ============================================================

def remove_incomplete_last_candle(
    df,
    interval
):

    if df is None or df.empty:
        return df

    durations = {
        "5min": pd.Timedelta(minutes=5),
        "15min": pd.Timedelta(minutes=15),
        "1h": pd.Timedelta(hours=1),
        "4h": pd.Timedelta(hours=4)
    }

    try:

        duration = durations[interval]

        last_time = df.index[-1]

        if last_time.tzinfo is None:

            last_time = (
                last_time
                .tz_localize("UTC")
            )

        now = pd.Timestamp.now(
            tz="UTC"
        )

        candle_end = (
            last_time
            +
            duration
        )

        if candle_end > now:

            log(
                f"Removing incomplete "
                f"{interval} candle: "
                f"{last_time}"
            )

            df = df.iloc[:-1].copy()

        return df

    except Exception as error:

        log(
            "Closed candle error: "
            f"{type(error).__name__}: {error}"
        )

        if len(df) > 1:
            return df.iloc[:-1].copy()

        return df


# ============================================================
# GET CANDLES
# ============================================================

def candles(interval):

    if interval not in CACHE:
        return None

    cache_item = CACHE[interval]

    now = time.time()

    # --------------------------------------------------------
    # Cache
    # --------------------------------------------------------

    if (
        cache_item["data"] is not None
        and
        now - cache_item["time"]
        < cache_item["ttl"]
    ):

        return cache_item["data"].copy()

    # --------------------------------------------------------
    # Download
    # --------------------------------------------------------

    df = download_yahoo(interval)

    # --------------------------------------------------------
    # Fallback cache
    # --------------------------------------------------------

    if df is None or df.empty:

        if cache_item["data"] is not None:

            log(
                f"Using cached {interval} data"
            )

            return (
                cache_item["data"]
                .copy()
            )

        return None

    # --------------------------------------------------------
    # Closed candles only
    # --------------------------------------------------------

    df = remove_incomplete_last_candle(
        df,
        interval
    )

    if df is None or len(df) < 20:

        log(
            f"Not enough closed "
            f"{interval} candles: "
            f"{0 if df is None else len(df)}"
        )

        return None

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    cache_item["data"] = df.copy()
    cache_item["time"] = now

    return df.copy()


# ============================================================
# CURRENT PRICE
# ============================================================

def current_price():

    df = candles("5min")

    if df is None or df.empty:
        return None

    try:

        return float(
            df["close"].iloc[-1]
        )

    except Exception:

        return None


# ============================================================
# EMA
# ============================================================

def ema(
    series,
    length
):

    return series.ewm(
        span=length,
        adjust=False
    ).mean()


# ============================================================
# RSI
# ============================================================

def rsi(
    series,
    length=14
):

    delta = series.diff()

    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )

    avg_gain = gain.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()

    rs = (
        avg_gain
        /
        avg_loss.replace(
            0,
            np.nan
        )
    )

    result = (
        100
        -
        (
            100
            /
            (1 + rs)
        )
    )

    return result.fillna(50)


# ============================================================
# BOS
# ============================================================

def detect_bos(df):

    if df is None or len(df) < 12:
        return "NONE"

    recent = df.tail(12)

    previous_high = (
        recent["high"]
        .iloc[:-2]
        .max()
    )

    previous_low = (
        recent["low"]
        .iloc[:-2]
        .min()
    )

    last_close = (
        recent["close"]
        .iloc[-1]
    )

    if last_close > previous_high:
        return "BULLISH BOS"

    if last_close < previous_low:
        return "BEARISH BOS"

    return "NONE"


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def detect_liquidity(df):

    if df is None or len(df) < 12:
        return "NONE"

    recent = df.tail(12)

    previous_high = (
        recent["high"]
        .iloc[:-2]
        .max()
    )

    previous_low = (
        recent["low"]
        .iloc[:-2]
        .min()
    )

    last = recent.iloc[-1]

    # Buy-side liquidity sweep
    if (
        last["high"] > previous_high
        and
        last["close"] < previous_high
    ):

        return "BSL SWEEP"

    # Sell-side liquidity sweep
    if (
        last["low"] < previous_low
        and
        last["close"] > previous_low
    ):

        return "SSL SWEEP"

    return "NONE"


# ============================================================
# FVG
# ============================================================

def detect_fvg(df):

    if df is None or len(df) < 5:
        return "NONE"

    a = df.iloc[-3]
    c = df.iloc[-1]

    # Bullish FVG
    if c["low"] > a["high"]:
        return "BULLISH FVG"

    # Bearish FVG
    if c["high"] < a["low"]:
        return "BEARISH FVG"

    return "NONE"


# ============================================================
# DISPLACEMENT
# ============================================================

def detect_displacement(df):

    if df is None or len(df) < 21:
        return "NONE"

    last = df.iloc[-1]

    body = abs(
        last["close"]
        -
        last["open"]
    )

    ranges = (
        df["high"]
        -
        df["low"]
    ).tail(20)

    average_range = ranges.mean()

    if average_range <= 0:
        return "NONE"

    if body >= average_range * 1.5:

        if last["close"] > last["open"]:
            return "BULLISH"

        if last["close"] < last["open"]:
            return "BEARISH"

    return "NONE"


# ============================================================
# MOMENTUM
# ============================================================

def detect_momentum(df):

    if df is None or len(df) < 50:
        return "MIXED"

    close = df["close"]

    ema20_value = (
        ema(close, 20)
        .iloc[-1]
    )

    ema50_value = (
        ema(close, 50)
        .iloc[-1]
    )

    price = close.iloc[-1]

    if (
        price > ema20_value
        and
        ema20_value > ema50_value
    ):

        return "BULLISH"

    if (
        price < ema20_value
        and
        ema20_value < ema50_value
    ):

        return "BEARISH"

    return "MIXED"


# ============================================================
# PREMIUM / DISCOUNT
# ============================================================

def detect_zone(df):

    if df is None or len(df) < 20:
        return "MID"

    recent = df.tail(50)

    high = recent["high"].max()
    low = recent["low"].min()

    if high <= low:
        return "MID"

    price = df["close"].iloc[-1]

    position = (
        price - low
    ) / (
        high - low
    )

    if position >= 0.70:
        return "PREMIUM"

    if position <= 0.30:
        return "DISCOUNT"

    return "MID"


# ============================================================
# TIMEFRAME TREND
# ============================================================

def timeframe_trend(df):

    if df is None or len(df) < 200:
        return "MIXED"

    close = df["close"]

    ema50_value = (
        ema(close, 50)
        .iloc[-1]
    )

    ema200_value = (
        ema(close, 200)
        .iloc[-1]
    )

    price = close.iloc[-1]

    if (
        price > ema50_value
        and
        ema50_value > ema200_value
    ):

        return "BULLISH"

    if (
        price < ema50_value
        and
        ema50_value < ema200_value
    ):

        return "BEARISH"

    return "MIXED"


# ============================================================
# FEATURES
# ============================================================

def features(df):

    if df is None or len(df) < 30:
        return None

    price = float(
        df["close"].iloc[-1]
    )

    rsi_value = float(
        rsi(
            df["close"]
        ).iloc[-1]
    )

    return {
        "price": price,
        "trend": timeframe_trend(df),
        "liquidity": detect_liquidity(df),
        "bos": detect_bos(df),
        "fvg": detect_fvg(df),
        "displacement": detect_displacement(df),
        "momentum": detect_momentum(df),
        "zone": detect_zone(df),
        "rsi": rsi_value
    }


# ============================================================
# ANALYSIS
# ============================================================

def analyze():

    h4 = candles("4h")
    h1 = candles("1h")
    m15 = candles("15min")
    m5 = candles("5min")

    if any(
        item is None or item.empty
        for item in [
            h4,
            h1,
            m15,
            m5
        ]
    ):

        log(
            "ANALYZE: incomplete market data"
        )

        return None

    f4 = features(h4)
    f1 = features(h1)
    f15 = features(m15)
    f5 = features(m5)

    if not all(
        [
            f4,
            f1,
            f15,
            f5
        ]
    ):

        log(
            "ANALYZE: features unavailable"
        )

        return None

    buy_score = 0
    sell_score = 0

    # --------------------------------------------------------
    # H4 = 20
    # --------------------------------------------------------

    if f4["trend"] == "BULLISH":
        buy_score += 20

    elif f4["trend"] == "BEARISH":
        sell_score += 20

    # --------------------------------------------------------
    # H1 = 15
    # --------------------------------------------------------

    if f1["trend"] == "BULLISH":
        buy_score += 15

    elif f1["trend"] == "BEARISH":
        sell_score += 15

    # --------------------------------------------------------
    # M15 = 15
    # --------------------------------------------------------

    if f15["trend"] == "BULLISH":
        buy_score += 15

    elif f15["trend"] == "BEARISH":
        sell_score += 15

    # --------------------------------------------------------
    # M5 = 10
    # --------------------------------------------------------

    if f5["trend"] == "BULLISH":
        buy_score += 10

    elif f5["trend"] == "BEARISH":
        sell_score += 10

    # --------------------------------------------------------
    # Liquidity = 10
    # --------------------------------------------------------

    if f5["liquidity"] == "SSL SWEEP":
        buy_score += 10

    elif f5["liquidity"] == "BSL SWEEP":
        sell_score += 10

    # --------------------------------------------------------
    # BOS = 15
    # --------------------------------------------------------

    if f5["bos"] == "BULLISH BOS":
        buy_score += 15

    elif f5["bos"] == "BEARISH BOS":
        sell_score += 15

    # --------------------------------------------------------
    # FVG = 5
    # --------------------------------------------------------

    if f5["fvg"] == "BULLISH FVG":
        buy_score += 5

    elif f5["fvg"] == "BEARISH FVG":
        sell_score += 5

    # --------------------------------------------------------
    # Displacement = 10
    # --------------------------------------------------------

    if f5["displacement"] == "BULLISH":
        buy_score += 10

    elif f5["displacement"] == "BEARISH":
        sell_score += 10

    # --------------------------------------------------------
    # Momentum = 5
    # --------------------------------------------------------

    if f5["momentum"] == "BULLISH":
        buy_score += 5

    elif f5["momentum"] == "BEARISH":
        sell_score += 5

    # --------------------------------------------------------
    # RSI = 5
    # --------------------------------------------------------

    if f5["rsi"] < 30:
        buy_score += 5

    elif f5["rsi"] > 70:
        sell_score += 5

    # ========================================================
    # SIGNAL
    # ========================================================

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

    # ========================================================
    # PERCENT
    # ========================================================

    total_score = (
        buy_score
        +
        sell_score
    )

    if total_score > 0:

        up_percent = round(
            buy_score
            /
            total_score
            *
            100
        )

        down_percent = round(
            sell_score
            /
            total_score
            *
            100
        )

    else:

        up_percent = 50
        down_percent = 50

    return {
        "signal": signal,
        "price": f5["price"],
        "h4": f4,
        "h1": f1,
        "m15": f15,
        "m5": f5,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "up_percent": up_percent,
        "down_percent": down_percent
    }


# ============================================================
# TRADE SETUP
# ============================================================

def make_setup(result):

    if result is None:
        return None

    signal = result["signal"]
    price = result["price"]

    if signal not in {
        "BUY",
        "SELL",
        "EARLY BUY",
        "EARLY SELL"
    }:

        return None

    if "BUY" in signal:
        direction = "BUY"
    else:
        direction = "SELL"

    # --------------------------------------------------------
    # Conservative gold setup
    # --------------------------------------------------------

    sl_distance = 5.0

    if direction == "BUY":

        entry = price
        sl = price - sl_distance
        tp = price + (
            sl_distance
            *
            TARGET_RR
        )

    else:

        entry = price
        sl = price + sl_distance
        tp = price - (
            sl_distance
            *
            TARGET_RR
        )

    # --------------------------------------------------------
    # Approximate lot calculation
    # --------------------------------------------------------

    risk_money = (
        DEFAULT_DEPOSIT
        *
        RISK_PERCENT
        /
        100
    )

    estimated_lot = (
        risk_money
        /
        sl_distance
    )

    lot = max(
        MIN_LOT,
        min(
            MAX_LOT,
            estimated_lot
        )
    )

    lot = round(
        lot,
        2
    )

    return {
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rr": TARGET_RR,
        "lot": lot
    }


# ============================================================
# FORMAT SIGNAL
# ============================================================

def format_signal(result):

    if result is None:

        return (
            "❌ <b>GOLD SMART V5.4</b>\n\n"
            "Не удалось получить данные XAUUSD.\n\n"
            f"📡 SOURCE: {SOURCE_NAME}\n"
            "⏳ Следующая проверка автоматически."
        )

    signal = result["signal"]
    price = result["price"]

    f4 = result["h4"]
    f1 = result["h1"]
    f15 = result["m15"]
    f5 = result["m5"]

    setup = make_setup(result)

    if signal == "BUY":

        icon = "🟢"

    elif signal == "SELL":

        icon = "🔴"

    elif signal == "EARLY BUY":

        icon = "🟡"

    elif signal == "EARLY SELL":

        icon = "🟠"

    else:

        icon = "⚪"

    lines = []

    lines.append(
        "🥇 <b>GOLD SMART V5.4</b>"
    )

    lines.append("")

    lines.append(
        f"{icon} <b>SIGNAL: {signal}</b>"
    )

    lines.append("")

    lines.append(
        f"💰 XAUUSD: <b>{price:.2f}</b>"
    )

    lines.append("")

    lines.append(
        f"📊 H4: <b>{f4['trend']}</b>"
    )

    lines.append(
        f"📊 H1: <b>{f1['trend']}</b>"
    )

    lines.append(
        f"📊 M15: <b>{f15['trend']}</b>"
    )

    lines.append(
        f"📊 M5: <b>{f5['trend']}</b>"
    )

    lines.append("")

    lines.append(
        f"💧 M5 Liquidity: "
        f"{f5['liquidity']}"
    )

    lines.append(
        f"🔨 M5 BOS: "
        f"{f5['bos']}"
    )

    lines.append(
        f"🧩 M5 FVG: "
        f"{f5['fvg']}"
    )

    lines.append(
        f"💥 M5 Displacement: "
        f"{f5['displacement']}"
    )

    lines.append(
        f"📈 Momentum: "
        f"{f5['momentum']}"
    )

    lines.append(
        f"📐 Zone: "
        f"{f5['zone']}"
    )

    lines.append(
        f"📊 RSI: "
        f"{f5['rsi']:.1f}"
    )

    lines.append("")

    lines.append(
        f"📈 BUY: "
        f"{result['up_percent']}%"
    )

    lines.append(
        f"📉 SELL: "
        f"{result['down_percent']}%"
    )

    lines.append("")

    lines.append(
        f"🎯 BUY SCORE: "
        f"{result['buy_score']}"
    )

    lines.append(
        f"🎯 SELL SCORE: "
        f"{result['sell_score']}"
    )

    if setup:

        lines.append("")
        lines.append(
            "━━━━━━━━━━━━━━━━━━"
        )

        lines.append(
            f"📍 ENTRY: "
            f"{setup['entry']:.2f}"
        )

        lines.append(
            f"🛑 SL: "
            f"{setup['sl']:.2f}"
        )

        lines.append(
            f"🎯 TP: "
            f"{setup['tp']:.2f}"
        )

        lines.append(
            f"⚖️ RR: "
            f"1:{setup['rr']:.1f}"
        )

        lines.append(
            f"📦 LOT: "
            f"{setup['lot']:.2f}"
        )

        lines.append(
            "🛡 RISK: 1%"
        )

        lines.append(
            "🤖 AUTO TRADING: OFF"
        )

    lines.append("")
    lines.append(
        "━━━━━━━━━━━━━━━━━━"
    )

    lines.append(
        f"📡 SOURCE: {SOURCE_NAME}"
    )

    lines.append(
        "🕯 CLOSED CANDLE ENGINE"
    )

    return "\n".join(lines)


# ============================================================
# SIGNAL KEY
# ============================================================

def signal_key(result):

    if result is None:
        return None

    return (
        result["signal"],
        round(
            result["price"],
            2
        ),
        result["m5"]["bos"],
        result["m5"]["liquidity"],
        result["m5"]["fvg"]
    )


# ============================================================
# PROCESS SIGNAL
# ============================================================

def process_signal():

    global last_signal_key
    global last_signal_time
    global last_error_telegram_time

    result = analyze()

    # --------------------------------------------------------
    # No data
    # --------------------------------------------------------

    if result is None:

        log(
            "No complete market data"
        )

        # Avoid Telegram error spam.
        now = time.time()

        if (
            now - last_error_telegram_time
            > 900
        ):

            telegram_send(
                format_signal(None)
            )

            last_error_telegram_time = now

        return

    # --------------------------------------------------------
    # WAIT
    # --------------------------------------------------------

    if result["signal"] == "WAIT":

        log(
            f"WAIT | "
            f"price={result['price']:.2f} | "
            f"BUY={result['buy_score']} | "
            f"SELL={result['sell_score']} | "
            f"H4={result['h4']['trend']} | "
            f"H1={result['h1']['trend']} | "
            f"M15={result['m15']['trend']} | "
            f"M5={result['m5']['trend']}"
        )

        return

    # --------------------------------------------------------
    # Duplicate protection
    # --------------------------------------------------------

    key = signal_key(result)
    now = time.time()

    if (
        key == last_signal_key
        and
        now - last_signal_time
        <
        COOLDOWN_MIN * 60
    ):

        log(
            "Duplicate signal skipped"
        )

        return

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    last_signal_key = key
    last_signal_time = now

    STATS["total"] += 1
    STATS["open"] += 1

    # --------------------------------------------------------
    # Telegram
    # --------------------------------------------------------

    message = format_signal(
        result
    )

    telegram_send(
        message
    )

    # --------------------------------------------------------
    # Log
    # --------------------------------------------------------

    log(
        f"SIGNAL {result['signal']} | "
        f"price={result['price']:.2f} | "
        f"BUY={result['buy_score']} | "
        f"SELL={result['sell_score']}"
    )


# ============================================================
# ENGINE
# ============================================================

def engine_loop():

    log(
        "🥇 GOLD SMART V5.4 ENGINE STARTED"
    )

    log(
f"📡 SOURCE: {SOURCE_NAME}"
