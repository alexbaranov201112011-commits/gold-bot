# ============================================================
# 🥇 GOLD SMART V5.4
# Yahoo Finance / GC=F
# H4 → H1 → M15 → M5
# CLOSED CANDLE ENGINE
# Risk 1%
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

# ------------------------------------------------------------
# TRADING
# ------------------------------------------------------------

RISK_PERCENT = 1.0
DEFAULT_DEPOSIT = 800.0

MIN_LOT = 0.01
MAX_LOT = 0.02

MIN_SCORE = 70
EARLY_SCORE = 60

TARGET_RR = 3.0
MIN_RR = 2.0

# ------------------------------------------------------------
# ENGINE
# ------------------------------------------------------------

POLL_SECONDS = 60
COOLDOWN_MIN = 15

# ------------------------------------------------------------
# CACHE
# ------------------------------------------------------------

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
    },
}

# ------------------------------------------------------------
# YAHOO PROTECTION
# ------------------------------------------------------------

YF_ERROR_COOLDOWN = 300

last_yf_error = 0
last_yf_error_text = ""

# ------------------------------------------------------------
# SIGNAL PROTECTION
# ------------------------------------------------------------

last_signal_key = None
last_signal_time = 0

# ------------------------------------------------------------
# STATISTICS
# ------------------------------------------------------------

STATS = {
    "total": 0,
    "win": 0,
    "loss": 0,
    "be": 0,
    "open": 0,
}

# ------------------------------------------------------------
# FLASK
# ------------------------------------------------------------

app = Flask(__name__)

ENGINE_STARTED = False


# ============================================================
# LOG
# ============================================================

def log(msg):
    print(
        f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC] "
        f"{msg}",
        flush=True
    )


# ============================================================
# TELEGRAM
# ============================================================

def telegram_send(text):
    if not BOT_TOKEN or not CHAT_ID:
        log("Telegram credentials missing")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    try:
        response = requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        if response.status_code != 200:
            log(
                f"Telegram error: "
                f"{response.status_code} "
                f"{response.text[:500]}"
            )
            return False

        return True

    except Exception as e:
        log(
            f"Telegram exception: "
            f"{type(e).__name__}: {e}"
        )
        return False


# ============================================================
# YAHOO DATA NORMALIZATION
# ============================================================

def normalize_yahoo_dataframe(df):

    if df is None or df.empty:
        return None

    try:

        # ----------------------------------------------------
        # yfinance can return MultiIndex columns.
        # Normalize them to OHLCV.
        # ----------------------------------------------------

        if isinstance(df.columns, pd.MultiIndex):

            normalized = []

            for column in df.columns:

                found = None

                for part in column:

                    part_lower = str(part).lower().strip()

                    if part_lower in {
                        "open",
                        "high",
                        "low",
                        "close",
                        "adj close",
                        "volume",
                    }:
                        found = part_lower
                        break

                if found:
                    normalized.append(found)
                else:
                    normalized.append(
                        str(column[-1]).lower()
                    )

            df.columns = normalized

        else:

            df.columns = [
                str(column)
                .lower()
                .replace(" ", "_")
                for column in df.columns
            ]

        # ----------------------------------------------------
        # Required columns
        # ----------------------------------------------------

        required = [
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]

        # Some Yahoo responses may not contain volume.
        if "volume" not in df.columns:
            df["volume"] = 0

        for column in required:

            if column not in df.columns:

                log(
                    f"Yahoo missing column: "
                    f"{column}"
                )

                return None

        df = df[required].copy()

        # ----------------------------------------------------
        # Numeric conversion
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
                "close",
            ]
        )

        if df.empty:
            return None

        # ----------------------------------------------------
        # Datetime
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

    except Exception as e:

        log(
            f"Yahoo normalization error: "
            f"{type(e).__name__}: {e}"
        )

        traceback.print_exc()

        return None


# ============================================================
# YAHOO DOWNLOAD
# ============================================================

def download_yahoo(interval):

    global last_yf_error
    global last_yf_error_text

    now = time.time()

    # --------------------------------------------------------
    # Error cooldown
    # --------------------------------------------------------

    if now - last_yf_error < YF_ERROR_COOLDOWN:

        log(
            "Yahoo request skipped because "
            "error cooldown is active"
        )

        return None

    try:

        # ----------------------------------------------------
        # Yahoo intervals
        # ----------------------------------------------------

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

            # Yahoo does not need direct 4H data.
            # We download 1H and aggregate it.
            period = "3mo"
            yf_interval = "1h"

        else:

            raise ValueError(
                f"Unsupported interval: {interval}"
            )

        log(
            f"📡 Yahoo request: "
            f"{YF_SYMBOL} "
            f"interval={yf_interval} "
            f"period={period}"
        )

        # ----------------------------------------------------
        # Download
        # ----------------------------------------------------

        df = yf.download(
            tickers=YF_SYMBOL,
            period=period,
            interval=yf_interval,
            auto_adjust=False,
            progress=False,
            threads=False,
            prepost=False,
        )

        # ----------------------------------------------------
        # Empty
        # ----------------------------------------------------

        if df is None or df.empty:

            raise RuntimeError(
                f"Yahoo returned EMPTY data "
                f"for {YF_SYMBOL} "
                f"{yf_interval}"
            )

        # ----------------------------------------------------
        # Normalize
        # ----------------------------------------------------

        df = normalize_yahoo_dataframe(df)

        if df is None or df.empty:

            raise RuntimeError(
                f"Yahoo returned invalid dataframe "
                f"for {YF_SYMBOL} "
                f"{yf_interval}"
            )

        # ----------------------------------------------------
        # 4H aggregation
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
                    "volume": "sum",
                })
                .dropna()
            )

        # ----------------------------------------------------
        # Final validation
        # ----------------------------------------------------

        if df.empty:

            raise RuntimeError(
                f"Yahoo dataframe became empty "
                f"after processing {interval}"
            )

        log(
            f"✅ Yahoo OK: "
            f"{interval} | "
            f"rows={len(df)} | "
            f"last={df.index[-1]} | "
            f"close={df['close'].iloc[-1]:.2f}"
        )

        return df

    except Exception as e:

        last_yf_error = time.time()

        last_yf_error_text = (
            f"{type(e).__name__}: {e}"
        )

        log(
            f"❌ Yahoo ERROR "
            f"{interval}: "
            f"{last_yf_error_text}"
        )

        traceback.print_exc()

        return None


# ============================================================
# CLOSED CANDLE
# ============================================================

def remove_incomplete_last_candle(
    df,
    interval
):

    if df is None or df.empty:
        return df

    try:

        durations = {

            "5min":
                pd.Timedelta(minutes=5),

            "15min":
                pd.Timedelta(minutes=15),

            "1h":
                pd.Timedelta(hours=1),

            "4h":
                pd.Timedelta(hours=4),
        }

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

        # ----------------------------------------------------
        # Current candle is not closed
        # ----------------------------------------------------

        if last_time + duration > now:

            log(
                f"🕯 Removing incomplete "
                f"{interval} candle: "
                f"{last_time}"
            )

            df = df.iloc[:-1].copy()

        return df

    except Exception as e:

        log(
            f"Closed candle error: "
            f"{type(e).__name__}: {e}"
        )

        # Conservative fallback
        if len(df) > 1:
            return df.iloc[:-1].copy()

        return df


# ============================================================
# CACHE
# ============================================================

def candles(interval):

    if interval not in CACHE:
        return None

    item = CACHE[interval]

    now = time.time()

    # --------------------------------------------------------
    # Cached data
    # --------------------------------------------------------

    if (
        item["data"] is not None
        and
        now - item["time"] < item["ttl"]
    ):

        return item["data"].copy()

    # --------------------------------------------------------
    # New Yahoo request
    # --------------------------------------------------------

    df = download_yahoo(interval)

    # --------------------------------------------------------
    # Failed request
    # --------------------------------------------------------

    if df is None or df.empty:

        if item["data"] is not None:

            log(
                f"⚠️ Using cached "
                f"{interval} data"
            )

            return item["data"].copy()

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
            f"⚠️ Not enough closed "
            f"{interval} candles: "
            f"{0 if df is None else len(df)}"
        )

        return None

    # --------------------------------------------------------
    # Save cache
    # --------------------------------------------------------

    item["data"] = df.copy()
    item["time"] = now

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

    if df is None or len(df) < 10:
        return "NONE"

    recent = df.tail(10)

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
# LIQUIDITY
# ============================================================

def detect_liquidity(df):

    if df is None or len(df) < 10:
        return "NONE"

    recent = df.tail(10)

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

    # --------------------------------------------------------
    # Buy-side liquidity sweep
    # --------------------------------------------------------

    if (
        last["high"] > previous_high
        and
        last["close"] < previous_high
    ):

        return "BSL SWEEP"

    # --------------------------------------------------------
    # Sell-side liquidity sweep
    # --------------------------------------------------------

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
    b = df.iloc[-2]
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

    if df is None or len(df) < 20:
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

    if df is None or len(df) < 20:
        return "MIXED"

    close = df["close"]

    ema20 = ema(
        close,
        20
    ).iloc[-1]

    ema50 = ema(
        close,
        50
    ).iloc[-1]

    price = close.iloc[-1]

    if (
        price > ema20
        and
        ema20 > ema50
    ):

        return "BULLISH"

    if (
        price < ema20
        and
        ema20 < ema50
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

    if df is None or len(df) < 50:
        return "MIXED"

    close = df["close"]

    ema50 = ema(
        close,
        50
    ).iloc[-1]

    ema200 = ema(
        close,
        200
    ).iloc[-1]

    price = close.iloc[-1]

    if (
        price > ema50
        and
        ema50 > ema200
    ):

        return "BULLISH"

    if (
        price < ema50
        and
        ema50 < ema200
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

        "trend":
            timeframe_trend(df),

        "liquidity":
            detect_liquidity(df),

        "bos":
            detect_bos(df),

        "fvg":
            detect_fvg(df),

        "displacement":
            detect_displacement(df),

        "momentum":
            detect_momentum(df),

        "zone":
            detect_zone(df),

        "rsi":
            rsi_value,
    }


# ============================================================
# ANALYZE
# ============================================================

def analyze():

    h4 = candles("4h")
    h1 = candles("1h")
    m15 = candles("15min")
    m5 = candles("5min")

    # --------------------------------------------------------
    # Data validation
    # --------------------------------------------------------

    if any(
        x is None or x.empty
        for x in [
            h4,
            h1,
            m15,
            m5
        ]
    ):

        log(
            "❌ ANALYZE: "
            "one or more timeframes "
            "have no data"
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
            "❌ ANALYZE: "
            "features unavailable"
        )

        return None

    # ========================================================
    # SCORE
    # ========================================================

    buy_score = 0
    sell_score = 0

    # --------------------------------------------------------
    # H4
    # --------------------------------------------------------

    if f4["trend"] == "BULLISH":

        buy_score += 20

    elif f4["trend"] == "BEARISH":

        sell_score += 20

    # --------------------------------------------------------
    # H1
    # --------------------------------------------------------

    if f1["trend"] == "BULLISH":

        buy_score += 15

    elif f1["trend"] == "BEARISH":

        sell_score += 15

    # --------------------------------------------------------
    # M15
    # --------------------------------------------------------

    if f15["trend"] == "BULLISH":

        buy_score += 15

    elif f15["trend"] == "BEARISH":

        sell_score += 15

    # --------------------------------------------------------
    # M5
    # --------------------------------------------------------

    if f5["trend"] == "BULLISH":

        buy_score += 10

    elif f5["trend"] == "BEARISH":

        sell_score += 10

    # --------------------------------------------------------
    # Liquidity
    # --------------------------------------------------------

    if f5["liquidity"] == "SSL SWEEP":

        buy_score += 10

    elif f5["liquidity"] == "BSL SWEEP":

        sell_score += 10

    # --------------------------------------------------------
    # BOS
    # --------------------------------------------------------

    if f5["bos"] == "BULLISH BOS":

        buy_score += 15

    elif f5["bos"] == "BEARISH BOS":

        sell_score += 15

    # --------------------------------------------------------
    # FVG
    # --------------------------------------------------------

    if f5["fvg"] == "BULLISH FVG":

        buy_score += 5

    elif f5["fvg"] == "BEARISH FVG":

        sell_score += 5

    # --------------------------------------------------------
    # Displacement
    # --------------------------------------------------------

    if f5["displacement"] == "BULLISH":

        buy_score += 10

    elif f5["displacement"] == "BEARISH":

        sell_score += 10

    # --------------------------------------------------------
    # Momentum
    # --------------------------------------------------------

    if f5["momentum"] == "BULLISH":

        buy_score += 5

    elif f5["momentum"] == "BEARISH":

        sell_score += 5

    # --------------------------------------------------------
    # RSI
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
    # PERCENTAGES
    # ========================================================

    total = (
        buy_score
        +
        sell_score
    )

    if total > 0:

        up_percent = round(
            buy_score
            /
            total
            *
            100
        )

        down_percent = round(
            sell_score
            /
            total
            *
            100
        )

    else:

        up_percent = 50
        down_percent = 50

    return {

        "signal":
            signal,

        "price":
            f5["price"],

        "h4":
            f4,

        "h1":
            f1,

        "m15":
            f15,

        "m5":
            f5,

        "buy_score":
            buy_score,

        "sell_score":
            sell_score,

        "up_percent":
            up_percent,

        "down_percent":
            down_percent,
    }


# ============================================================
# SETUP
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
        "EARLY SELL",
    }:

        return None

    if "BUY" in signal:

        direction = "BUY"

    else:

        direction = "SELL"

    # --------------------------------------------------------
    # Conservative SL
    # --------------------------------------------------------

    sl_distance = 5.0

    if direction == "BUY":

        sl = (
            price
            -
            sl_distance
        )

        tp = (
            price
            +
            sl_distance
            *
            TARGET_RR
        )

    else:

        sl = (
            price
            +
            sl_distance
        )

        tp = (
            price
            -
            sl_distance
            *
            TARGET_RR
        )

    rr = TARGET_RR

    # --------------------------------------------------------
    # Approximate risk calculation
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

    # --------------------------------------------------------
    # Lot clamp
    # --------------------------------------------------------

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

        "direction":
            direction,

        "entry":
            price,

        "sl":
            sl,

        "tp":
            tp,

        "rr":
            rr,

        "lot":
            lot,
    }


# ============================================================
# FORMAT SIGNAL
# ============================================================

def format_signal(result):

    if result is None:

        return (
            "❌ <b>Не удалось получить данные XAU/USD.</b>\n\n"
            f"📡 Источник: {SOURCE_NAME}\n"
            "⏳ Бот продолжит проверку автоматически."
        )

    signal = result["signal"]

    price = result["price"]

    f4 = result["h4"]
    f1 = result["h1"]
    f15 = result["m15"]
    f5 = result["m5"]

    setup = make_setup(result)

    # --------------------------------------------------------
    # Icon
    # --------------------------------------------------------

    if signal == "BUY":

        icon = "🟢"

    elif signal == "SELL":

        icon = "🔴"

    elif "BUY" in signal:

        icon = "🟡"

    elif "SELL" in signal:

        icon = "🟠"

    else:

        icon = "⚪"

    # ========================================================
    # MESSAGE
    # ========================================================

    text = (
        f"🥇 <b>GOLD SMART V5.4</b>\n\n"

        f"{icon} <b>SIGNAL: {signal}</b>\n\n"

        f"💰 XAUUSD: <b>{price:.2f}</b>\n\n"

        f"📊 H4: <b>{f4['trend']}</b>\n"
        f"📊 H1: <b>{f1['trend']}</b>\n"
        f"📊 M
