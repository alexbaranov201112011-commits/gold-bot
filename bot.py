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

# Yahoo symbol
YF_SYMBOL = "GC=F"

SOURCE_NAME = "Yahoo Finance / GC=F"

# Trading
RISK_PERCENT = 1.0
DEFAULT_DEPOSIT = 800.0

MIN_LOT = 0.01
MAX_LOT = 0.02

MIN_SCORE = 70
EARLY_SCORE = 60

TARGET_RR = 3.0
MIN_RR = 2.0

# Engine
POLL_SECONDS = 60
COOLDOWN_MIN = 15

# Cache
CACHE = {
    "5min": {"ttl": 60, "data": None, "time": 0},
    "15min": {"ttl": 180, "data": None, "time": 0},
    "1h": {"ttl": 600, "data": None, "time": 0},
    "4h": {"ttl": 1800, "data": None, "time": 0},
}

# Yahoo error protection
YF_ERROR_COOLDOWN = 300
last_yf_error = 0
last_yf_error_text = ""

# Signal protection
last_signal_key = None
last_signal_time = 0

# Stats
STATS = {
    "total": 0,
    "win": 0,
    "loss": 0,
    "be": 0,
    "open": 0,
}

# Flask
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
        r = requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        if r.status_code != 200:
            log(f"Telegram error: {r.status_code} {r.text[:300]}")
            return False

        return True

    except Exception as e:
        log(f"Telegram exception: {e}")
        return False


# ============================================================
# YAHOO DATA
# ============================================================

def normalize_yahoo_dataframe(df):
    if df is None or df.empty:
        return None

    try:
        # yfinance can return MultiIndex columns
        if isinstance(df.columns, pd.MultiIndex):
            new_cols = []

            for col in df.columns:
                # Find OHLCV field
                found = None

                for part in col:
                    p = str(part).lower()

                    if p in {
                        "open",
                        "high",
                        "low",
                        "close",
                        "adj close",
                        "volume",
                    }:
                        found = p
                        break

                new_cols.append(found if found else str(col[-1]).lower())

            df.columns = new_cols

        else:
            df.columns = [
                str(c).lower().replace(" ", "_")
                for c in df.columns
            ]

        # Different possible naming
        rename = {
            "adj_close": "adj_close",
        }

        df = df.rename(columns=rename)

        required = [
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]

        for col in required:
            if col not in df.columns:
                if col == "volume":
                    df["volume"] = 0
                else:
                    return None

        df = df[required].copy()

        for col in required:
            df[col] = pd.to_numeric(
                df[col],
                errors="coerce"
            )

        df = df.dropna(
            subset=["open", "high", "low", "close"]
        )

        if df.empty:
            return None

        # Normalize datetime index
        idx = pd.to_datetime(
            df.index,
            errors="coerce",
            utc=True
        )

        df.index = idx
        df = df[~df.index.isna()]

        df = df.sort_index()
        df = df[~df.index.duplicated(keep="last")]

        return df

    except Exception as e:
        log(f"Yahoo normalize error: {e}")
        return None


def download_yahoo(interval):
    global last_yf_error
    global last_yf_error_text

    now = time.time()

    # Don't hammer Yahoo after an error
    if now - last_yf_error < YF_ERROR_COOLDOWN:
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
            # Yahoo does not need to provide 4H directly.
            # We build 4H from 1H.
            period = "3mo"
            yf_interval = "1h"

        else:
            raise ValueError(f"Unsupported interval: {interval}")

        log(
            f"Yahoo request: {YF_SYMBOL} "
            f"{yf_interval} period={period}"
        )

        df = yf.download(
            tickers=YF_SYMBOL,
            period=period,
            interval=yf_interval,
            auto_adjust=False,
            progress=False,
            threads=False,
            prepost=False,
        )

        if df is None or df.empty:
            raise RuntimeError(
                f"Yahoo returned EMPTY data "
                f"for {YF_SYMBOL} {yf_interval}"
            )

        df = normalize_yahoo_dataframe(df)

        if df is None or df.empty:
            raise RuntimeError(
                f"Yahoo returned unusable dataframe "
                f"for {YF_SYMBOL} {yf_interval}"
            )

        # 4H from 1H
        if interval == "4h":

            df = (
                df.resample("4h")
                .agg({
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                })
                .dropna()
            )

        log(
            f"Yahoo OK: {interval} "
            f"rows={len(df)} "
            f"last={df.index[-1]} "
            f"close={df['close'].iloc[-1]:.2f}"
        )

        return df

    except Exception as e:

        last_yf_error = time.time()
        last_yf_error_text = str(e)

        log(
            f"❌ Yahoo error {interval}: "
            f"{type(e).__name__}: {e}"
        )

        traceback.print_exc()

        return None


# ============================================================
# CLOSED CANDLE
# ============================================================

def remove_incomplete_last_candle(df, interval):
    if df is None or df.empty:
        return df

    try:

        durations = {
            "5min": pd.Timedelta(minutes=5),
            "15min": pd.Timedelta(minutes=15),
            "1h": pd.Timedelta(hours=1),
            "4h": pd.Timedelta(hours=4),
        }

        duration = durations[interval]

        last_time = df.index[-1]

        if last_time.tzinfo is None:
            last_time = last_time.tz_localize("UTC")

        now = pd.Timestamp.now(tz="UTC")

        # If last candle has not fully closed — remove it.
        if last_time + duration > now:
            df = df.iloc[:-1].copy()

        return df

    except Exception as e:
        log(f"Closed candle error: {e}")

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

    if (
        item["data"] is not None
        and now - item["time"] < item["ttl"]
    ):
        return item["data"].copy()

    df = download_yahoo(interval)

    if df is None or df.empty:
        # Use previous cache if available
        if item["data"] is not None:
            log(
                f"Using cached {interval} "
                f"because Yahoo request failed"
            )
            return item["data"].copy()

        return None

    df = remove_incomplete_last_candle(
        df,
        interval
    )

    if df is None or len(df) < 20:
        log(
            f"Not enough closed candles: "
            f"{interval}"
        )
        return None

    item["data"] = df.copy()
    item["time"] = now

    return df.copy()


# ============================================================
# PRICE
# ============================================================

def current_price():
    df = candles("5min")

    if df is None or df.empty:
        return None

    try:
        return float(df["close"].iloc[-1])
    except Exception:
        return None


# ============================================================
# INDICATORS
# ============================================================

def ema(series, length):
    return series.ewm(
        span=length,
        adjust=False
    ).mean()


def rsi(series, length=14):
    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / length,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    result = 100 - (
        100 / (1 + rs)
    )

    return result.fillna(50)


# ============================================================
# MARKET STRUCTURE
# ============================================================

def detect_bos(df):
    if df is None or len(df) < 10:
        return "NONE"

    recent = df.tail(10)

    prev_high = recent["high"].iloc[:-2].max()
    prev_low = recent["low"].iloc[:-2].min()

    last_close = recent["close"].iloc[-1]

    if last_close > prev_high:
        return "BULLISH BOS"

    if last_close < prev_low:
        return "BEARISH BOS"

    return "NONE"


def detect_liquidity(df):
    if df is None or len(df) < 10:
        return "NONE"

    recent = df.tail(10)

    previous_high = recent["high"].iloc[:-2].max()
    previous_low = recent["low"].iloc[:-2].min()

    last = recent.iloc[-1]

    # BSL sweep:
    # price takes previous high but closes below it
    if (
        last["high"] > previous_high
        and last["close"] < previous_high
    ):
        return "BSL SWEEP"

    # SSL sweep
    if (
        last["low"] < previous_low
        and last["close"] > previous_low
    ):
        return "SSL SWEEP"

    return "NONE"


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


def detect_displacement(df):
    if df is None or len(df) < 20:
        return "NONE"

    last = df.iloc[-1]

    body = abs(
        last["close"] - last["open"]
    )

    ranges = (
        df["high"] - df["low"]
    ).tail(20)

    avg_range = ranges.mean()

    if avg_range <= 0:
        return "NONE"

    if body >= avg_range * 1.5:

        if last["close"] > last["open"]:
            return "BULLISH"

        if last["close"] < last["open"]:
            return "BEARISH"

    return "NONE"


def detect_momentum(df):
    if df is None or len(df) < 20:
        return "MIXED"

    last_close = df["close"].iloc[-1]

    e20 = ema(
        df["close"],
        20
    ).iloc[-1]

    e50 = ema(
        df["close"],
        50
    ).iloc[-1]

    if (
        last_close > e20
        and e20 > e50
    ):
        return "BULLISH"

    if (
        last_close < e20
        and e20 < e50
    ):
        return "BEARISH"

    return "MIXED"


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
    ) / (high - low)

    if position >= 0.70:
        return "PREMIUM"

    if position <= 0.30:
        return "DISCOUNT"

    return "MID"


# ============================================================
# TREND
# ============================================================

def timeframe_trend(df):
    if df is None or len(df) < 50:
        return "MIXED"

    close = df["close"]

    e50 = ema(close, 50).iloc[-1]
    e200 = ema(close, 200).iloc[-1]

    price = close.iloc[-1]

    if price > e50 and e50 > e200:
        return "BULLISH"

    if price < e50 and e50 < e200:
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
        rsi(df["close"]).iloc[-1]
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
        "rsi": rsi_value,
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
        x is None or x.empty
        for x in [h4, h1, m15, m5]
    ):
        return None

    f4 = features(h4)
    f1 = features(h1)
    f15 = features(m15)
    f5 = features(m5)

    if not all(
        [f4, f1, f15, f5]
    ):
        return None

    # --------------------------------------------------------
    # SCORE
    # --------------------------------------------------------

    buy_score = 0
    sell_score = 0

    # H4
    if f4["trend"] == "BULLISH":
        buy_score += 20

    elif f4["trend"] == "BEARISH":
        sell_score += 20

    # H1
    if f1["trend"] == "BULLISH":
        buy_score += 15

    elif f1["trend"] == "BEARISH":
        sell_score += 15

    # M15
    if f15["trend"] == "BULLISH":
        buy_score += 15

    elif f15["trend"] == "BEARISH":
        sell_score += 15

    # M5 trend
    if f5["trend"] == "BULLISH":
        buy_score += 10

    elif f5["trend"] == "BEARISH":
        sell_score += 10

    # Liquidity
    if f5["liquidity"] == "SSL SWEEP":
        buy_score += 10

    elif f5["liquidity"] == "BSL SWEEP":
        sell_score += 10

    # BOS
    if f5["bos"] == "BULLISH BOS":
        buy_score += 15

    elif f5["bos"] == "BEARISH BOS":
        sell_score += 15

    # FVG
    if f5["fvg"] == "BULLISH FVG":
        buy_score += 5

    elif f5["fvg"] == "BEARISH FVG":
        sell_score += 5

    # Displacement
    if f5["displacement"] == "BULLISH":
        buy_score += 10

    elif f5["displacement"] == "BEARISH":
        sell_score += 10

    # Momentum
    if f5["momentum"] == "BULLISH":
        buy_score += 5

    elif f5["momentum"] == "BEARISH":
        sell_score += 5

    # RSI filter
    if f5["rsi"] < 30:
        buy_score += 5

    elif f5["rsi"] > 70:
        sell_score += 5

    # --------------------------------------------------------
    # DECISION
    # --------------------------------------------------------

    if buy_score >= MIN_SCORE and buy_score > sell_score:

        signal = "BUY"

    elif sell_score >= MIN_SCORE and sell_score > buy_score:

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

    # --------------------------------------------------------
    # DIRECTION %
    # --------------------------------------------------------

    total = buy_score + sell_score

    if total > 0:
        up_percent = round(
            buy_score / total * 100
        )

        down_percent = round(
            sell_score / total * 100
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
        "down_percent": down_percent,
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

    direction = (
        "BUY"
        if "BUY" in signal
        else "SELL"
    )

    # Conservative gold structure.
    # Approximate distances in dollars.
    sl_distance = 5.0

    if direction == "BUY":

        sl = price - sl_distance
        tp = price + (
            sl_distance * TARGET_RR
        )

    else:

        sl = price + sl_distance
        tp = price - (
            sl_distance * TARGET_RR
        )

    rr = TARGET_RR

    # Approximate lot calculation.
    risk_money = (
        DEFAULT_DEPOSIT
        * RISK_PERCENT
        / 100
    )

    # Approximate gold value:
    # $1 price movement on 0.01 lot ≈ $1
    estimated_lot = (
        risk_money
        / sl_distance
    )

    # Clamp
    lot = max(
        MIN_LOT,
        min(
            MAX_LOT,
            estimated_lot
        )
    )

    # Round to broker-style lot step
    lot = round(
        lot,
        2
    )

    return {
        "direction": direction,
        "entry": price,
        "sl": sl,
        "tp": tp,
        "rr": rr,
        "lot": lot,
    }


# ============================================================
# TELEGRAM FORMAT
# ============================================================

def format_signal(result):

    if result is None:
        return (
            "❌ <b>Не удалось получить данные XAU/USD.</b>\n\n"
            f"Источник: {SOURCE_NAME}\n"
            "Проверь журнал Render."
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

    elif "BUY" in signal:
        icon = "🟡"

    elif "SELL" in signal:
        icon = "🟠"

    else:
        icon = "⚪"

    text = (
        f"🥇 <b>GOLD SMART V5.4</b>\n\n"

        f"{icon} <b>SIGNAL: {signal}</b>\n\n"

        f"💰 XAUUSD: <b>{price:.2f}</b>\n\n"

        f"📊 H4: <b>{f4['trend']}</b>\n"
        f"📊 H1: <b>{f1['trend']}</b>\n"
        f"📊 M15: <b>{f15['trend']}</b>\n"
        f"📊 M5: <b>{f5['trend']}</b>\n\n"

        f"💧 M5 Liquidity: {f5['liquidity']}\n"
        f"🔨 M5 BOS: {f5['bos']}\n"
        f"🧩 M5 FVG: {f5['fvg']}\n"
        f"💥 M5 Displacement: {f5['displacement']}\n"
        f"📈 Momentum: {f5['momentum']}\n"
        f"📐 Zone: {f5['zone']}\n"
        f"📊 RSI: {f5['rsi']:.1f}\n\n"

        f"📈 BUY: {result['up_percent']}%\n"
        f"📉 SELL: {result['down_percent']}%\n\n"

        f"🎯 BUY SCORE: {result['buy_score']}\n"
        f"🎯 SELL SCORE: {result['sell_score']}\n"
    )

    if setup:

        text += (
            "\n━━━━━━━━━━━━━━━━━━\n"
            f"📍 ENTRY: {setup['entry']:.2f}\n"
            f"🛑 SL: {setup['sl']:.2f}\n"
            f"🎯 TP: {setup['tp']:.2f}\n"
            f"⚖️ RR: 1:{setup['rr']:.1f}\n"
            f"📦 LOT: {setup['lot']:.2f}\n"
            "🛡 RISK: 1%\n"
            "🤖 AUTO TRADING: OFF\n"
        )

    text += (
        "\n━━━━━━━━━━━━━━━━━━\n"
        f"📡 SOURCE: {SOURCE_NAME}\n"
        "🕯 CLOSED
