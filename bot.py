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
# 🥇 GOLD SMART V5.9.5
# XAU/USD SMART SMC SIGNAL ENGINE
#
# DATA SOURCE: XAUS.COM
# TIMEFRAMES: H4 → H1 → M15 → M5
#
# AUTO TRADING = OFF
# RISK = 1%
# CLOSED CANDLES = ON
#
# TELEGRAM:
# /start
# /status
# /signal
# /test
# /stats
# /help
# ============================================================


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BASE_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://gold-bot-q8la.onrender.com"
).strip().rstrip("/")

DEPOSIT = float(os.getenv("DEPOSIT", "800"))

RISK_PERCENT = 1.0

AUTO_TRADING = False

SESSION_START = 12
SESSION_END = 23

REQUEST_TIMEOUT = 20

# ------------------------------------------------------------
# SIGNAL THRESHOLDS
# ------------------------------------------------------------

MIN_FULL_SCORE = 70
MIN_EARLY_SCORE = 60

# ------------------------------------------------------------
# XAUS DATA
# ------------------------------------------------------------

XAUS_SPOT_URL = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY_URL = "https://xaus.com/api/v1/intraday"

MAX_DATA_AGE_MINUTES = 15

# ------------------------------------------------------------
# MINIMUM CLOSED CANDLES
#
# XAUS intraday = approximately 2-minute observations.
# 48h normally gives enough data for these requirements.
# ------------------------------------------------------------

MIN_M5 = 50
MIN_M15 = 20
MIN_H1 = 10
MIN_H4 = 6

# ------------------------------------------------------------
# STATE
# ------------------------------------------------------------

STATE_FILE = "gold_smart_state.json"

DEFAULT_STATE = {
    "signals": 0,
    "full_signals": 0,
    "early_signals": 0,
    "wins": 0,
    "losses": 0,
    "virtual_trades": [],
    "last_signal": None,
    "last_signal_time": None,
}


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# STATE
# ============================================================

state_lock = threading.Lock()


def load_state():
    if not os.path.exists(STATE_FILE):
        return dict(DEFAULT_STATE)

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        result = dict(DEFAULT_STATE)
        result.update(data)

        return result

    except Exception:
        return dict(DEFAULT_STATE)


STATE = load_state()


def save_state():
    try:
        with state_lock:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    STATE,
                    f,
                    ensure_ascii=False,
                    indent=2
                )
    except Exception as e:
        print("STATE SAVE ERROR:", e)


# ============================================================
# LOGGING
# ============================================================

def log(message):
    print(
        f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] "
        f"{message}",
        flush=True
    )


# ============================================================
# TELEGRAM
# ============================================================

def telegram_url(method):
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"


def send_telegram(text_message, chat_id=None):
    if not BOT_TOKEN:
        log("Telegram token missing")
        return False

    target = chat_id or CHAT_ID

    if not target:
        log("Telegram chat ID missing")
        return False

    try:
        response = requests.post(
            telegram_url("sendMessage"),
            json={
                "chat_id": target,
                "text": text_message,
                "disable_web_page_preview": True
            },
            timeout=REQUEST_TIMEOUT
        )

        if response.ok:
            return True

        log(
            f"Telegram error {response.status_code}: "
            f"{response.text[:500]}"
        )

        return False

    except Exception as e:
        log(f"Telegram exception: {e}")
        return False


# ============================================================
# XAUS SPOT
# ============================================================

def get_spot_price():
    try:

        params = {
            "compact": "1",
            "fresh": str(int(time.time()))
        }

        response = requests.get(
            XAUS_SPOT_URL,
            params=params,
            timeout=REQUEST_TIMEOUT
        )

        response.raise_for_status()

        data = response.json()

        # Primary official field
        price = data.get("spot_usd_oz")

        # Fallback
        if price is None:
            xau = data.get("xau", {})

            if isinstance(xau, dict):
                price = xau.get("price")

        if price is None:
            raise ValueError("XAUS price field not found")

        price = float(price)

        if price <= 0:
            raise ValueError("Invalid XAUS price")

        # ----------------------------------------------------
        # freshness
        # ----------------------------------------------------

        data_state = data.get("data_state", {})

        status = data_state.get("status", "unknown")
        age_seconds = data_state.get("age_seconds")

        if status == "unavailable":
            raise ValueError("XAUS price unavailable")

        if age_seconds is not None:

            try:
                age_seconds = float(age_seconds)

                if age_seconds > MAX_DATA_AGE_MINUTES * 60:
                    raise ValueError(
                        f"XAUS spot too old: {age_seconds:.0f}s"
                    )

            except ValueError:
                raise

        log(
            f"XAUS SPOT = {price:.2f} | "
            f"state={status} | age={age_seconds}"
        )

        return price

    except Exception as e:

        log(f"XAUS SPOT ERROR: {e}")

        raise


# ============================================================
# XAUS INTRADAY
# ============================================================

def get_intraday_points():

    try:

        params = {
            "symbol": "xau",
            "hours": 48,
            "fresh": str(int(time.time()))
        }

        response = requests.get(
            XAUS_INTRADAY_URL,
            params=params,
            timeout=REQUEST_TIMEOUT
        )

        response.raise_for_status()

        data = response.json()

        points = data.get("points")

        if not isinstance(points, list):
            raise ValueError("XAUS points missing")

        if len(points) < 100:
            raise ValueError(
                f"Too few XAUS points: {len(points)}"
            )

        rows = []

        for point in points:

            if not isinstance(point, dict):
                continue

            timestamp = point.get("t")
            price = point.get("p")

            if timestamp is None or price is None:
                continue

            try:

                # ISO timestamp
                if isinstance(timestamp, str):

                    ts = pd.to_datetime(
                        timestamp,
                        utc=True,
                        errors="coerce"
                    )

                # Unix timestamp
                else:

                    ts = pd.to_datetime(
                        float(timestamp),
                        unit="s",
                        utc=True,
                        errors="coerce"
                    )

                price = float(price)

                if pd.isna(ts):
                    continue

                if price <= 0:
                    continue

                rows.append(
                    {
                        "timestamp": ts,
                        "price": price
                    }
                )

            except Exception:
                continue

        if len(rows) < 100:
            raise ValueError(
                f"Valid XAUS points too few: {len(rows)}"
            )

        df = pd.DataFrame(rows)

        df = df.drop_duplicates(
            subset=["timestamp"]
        )

        df = df.sort_values("timestamp")

        df = df.set_index("timestamp")

        df = df[["price"]]

        # ----------------------------------------------------
        # Freshness based on last observation
        # ----------------------------------------------------

        now = pd.Timestamp.now(tz="UTC")

        last_ts = df.index.max()

        age_minutes = (
            now - last_ts
        ).total_seconds() / 60

        if age_minutes > MAX_DATA_AGE_MINUTES:

            raise ValueError(
                f"XAUS intraday too old: "
                f"{age_minutes:.1f} min"
            )

        log(
            f"XAUS POINTS = {len(df)} | "
            f"last={last_ts} | "
            f"age={age_minutes:.1f}m"
        )

        return df

    except Exception as e:

        log(f"XAUS INTRADAY ERROR: {e}")

        raise


# ============================================================
# TIMEFRAME BUILDER
# ============================================================

def build_timeframe(df, rule):

    if df.empty:
        return pd.DataFrame()

    ohlc = df["price"].resample(rule).agg(
        [
            "first",
            "max",
            "min",
            "last"
        ]
    )

    ohlc.columns = [
        "open",
        "high",
        "low",
        "close"
    ]

    ohlc = ohlc.dropna()

    # --------------------------------------------------------
    # Remove unfinished current candle
    # --------------------------------------------------------

    now = pd.Timestamp.now(tz="UTC")

    current_bucket = now.floor(rule)

    ohlc = ohlc[
        ohlc.index < current_bucket
    ]

    return ohlc


def build_all_timeframes(df):

    h4 = build_timeframe(df, "4h")
    h1 = build_timeframe(df, "1h")
    m15 = build_timeframe(df, "15min")
    m5 = build_timeframe(df, "5min")

    log(
        f"TF COUNTS | "
        f"H4={len(h4)} | "
        f"H1={len(h1)} | "
        f"M15={len(m15)} | "
        f"M5={len(m5)}"
    )

    return {
        "H4": h4,
        "H1": h1,
        "M15": m15,
        "M5": m5
    }


# ============================================================
# VALIDATE DATA
# ============================================================

def validate_timeframes(tfs):

    requirements = {
        "M5": MIN_M5,
        "M15": MIN_M15,
        "H1": MIN_H1,
        "H4": MIN_H4
    }

    # Validate lower TF first
    for name in ["M5", "M15", "H1", "H4"]:

        count = len(tfs[name])
        minimum = requirements[name]

        if count < minimum:

            raise ValueError(
                f"Not enough {name} candles "
                f"({count}/{minimum})"
            )

    return True


# ============================================================
# EMA
# ============================================================

def add_indicators(df):

    df = df.copy()

    df["ema20"] = (
        df["close"]
        .ewm(span=20, adjust=False)
        .mean()
    )

    df["ema50"] = (
        df["close"]
        .ewm(span=50, adjust=False)
        .mean()
    )

    # EMA200 only when enough candles
    if len(df) >= 200:

        df["ema200"] = (
            df["close"]
            .ewm(span=200, adjust=False)
            .mean()
        )

    else:

        df["ema200"] = np.nan

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    delta = df["close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = (
        gain.rolling(14)
        .mean()
    )

    avg_loss = (
        loss.rolling(14)
        .mean()
    )

    rs = avg_gain / avg_loss.replace(
        0,
        np.nan
    )

    df["rsi"] = 100 - (
        100 / (1 + rs)
    )

    return df


# ============================================================
# TIMEFRAME ANALYSIS
# ============================================================

def timeframe_analysis(df):

    df = add_indicators(df)

    if len(df) < 2:
        return {
            "trend": "MIXED",
            "score": 50,
            "rsi": 50,
            "ema20": None,
            "ema50": None,
            "ema200": None
        }

    last = df.iloc[-1]
    previous = df.iloc[-2]

    close = float(last["close"])

    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])

    ema200 = (
        float(last["ema200"])
        if not pd.isna(last["ema200"])
        else None
    )

    rsi = (
        float(last["rsi"])
        if not pd.isna(last["rsi"])
        else 50.0
    )

    bullish_points = 0
    bearish_points = 0

    # --------------------------------------------------------
    # EMA20 / EMA50
    # --------------------------------------------------------

    if close > ema20:
        bullish_points += 1

    elif close < ema20:
        bearish_points += 1

    if ema20 > ema50:
        bullish_points += 2

    elif ema20 < ema50:
        bearish_points += 2

    # --------------------------------------------------------
    # EMA200
    # --------------------------------------------------------

    if ema200 is not None:

        if close > ema200:
            bullish_points += 2

        elif close < ema200:
            bearish_points += 2

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------

    if rsi >= 55:
        bullish_points += 1

    elif rsi <= 45:
        bearish_points += 1

    # --------------------------------------------------------
    # Price momentum
    # --------------------------------------------------------

    if close > float(previous["close"]):
        bullish_points += 1

    elif close < float(previous["close"]):
        bearish_points += 1

    total = bullish_points + bearish_points

    if bullish_points > bearish_points:

        trend = "BULLISH"

        if total > 0:
            score = int(
                50
                + 50
                * bullish_points
                / max(total, 1)
            )
        else:
            score = 50

    elif bearish_points > bullish_points:

        trend = "BEARISH"

        if total > 0:
            score = int(
                50
                + 50
                * bearish_points
                / max(total, 1)
            )
        else:
            score = 50

    else:

        trend = "MIXED"
        score = 50

    return {
        "trend": trend,
        "score": max(0, min(100, score)),
        "rsi": round(rsi, 1),
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "ema200": (
            round(ema200, 2)
            if ema200 is not None
            else None
        )
    }


# ============================================================
# SMC ENGINE
# ============================================================

def m5_smc(df):

    df = df.copy()

    if len(df) < 10:

        return {
            "liquidity": "NONE",
            "bos": "NONE",
            "fvg": "NONE",
            "displacement": "NONE",
            "momentum": "NONE",
            "premium_discount": "EQUILIBRIUM",
            "direction": "NONE",
            "score": 0
        }

    last = df.iloc[-1]
    prev = df.iloc[-2]

    high = float(last["high"])
    low = float(last["low"])
    close = float(last["close"])

    prev_high = float(prev["high"])
    prev_low = float(prev["low"])

    recent = df.iloc[-10:-1]

    recent_high = float(
        recent["high"].max()
    )

    recent_low = float(
        recent["low"].min()
    )

    # ========================================================
    # LIQUIDITY
    # ========================================================

    liquidity = "NONE"

    bullish_liquidity = False
    bearish_liquidity = False

    # Sweep sell-side liquidity
    if low < recent_low and close > recent_low:

        liquidity = "SSL SWEEP"
        bullish_liquidity = True

    # Sweep buy-side liquidity
    elif high > recent_high and close < recent_high:

        liquidity = "BSL SWEEP"
        bearish_liquidity = True

    # ========================================================
    # BOS
    # ========================================================

    bos = "NONE"

    bullish_bos = False
    bearish_bos = False

    if close > recent_high:

        bos = "BULLISH"
        bullish_bos = True

    elif close < recent_low:

        bos = "BEARISH"
        bearish_bos = True

    # ========================================================
    # FVG
    # ========================================================

    fvg = "NONE"

    bullish_fvg = False
    bearish_fvg = False

    if len(df) >= 3:

        c1 = df.iloc[-3]
        c3 = df.iloc[-1]

        c1_high = float(c1["high"])
        c1_low = float(c1["low"])

        c3_high = float(c3["high"])
        c3_low = float(c3["low"])

        if c3_low > c1_high:

            fvg = "BULLISH"
            bullish_fvg = True

        elif c3_high < c1_low:

            fvg = "BEARISH"
            bearish_fvg = True

    # ========================================================
    # DISPLACEMENT
    # ========================================================

    body = abs(
        float(last["close"])
        - float(last["open"])
    )

    ranges = (
        df["high"]
        - df["low"]
    )

    avg_range = float(
        ranges.iloc[-10:-1].mean()
    )

    displacement = "NONE"

    bullish_displacement = False
    bearish_displacement = False

    if avg_range > 0:

        if body > avg_range * 1.3:

            if close > float(last["open"]):

                displacement = "BULLISH"
                bullish_displacement = True

            else:

                displacement = "BEARISH"
                bearish_displacement = True

    # ========================================================
    # MOMENTUM
    # ========================================================

    momentum = "NONE"

    bullish_momentum = False
    bearish_momentum = False

    closes = df["close"].iloc[-4:].tolist()

    if all(
        closes[i] > closes[i - 1]
        for i in range(1, len(closes))
    ):

        momentum = "BULLISH"
        bullish_momentum = True

    elif all(
        closes[i] < closes[i - 1]
        for i in range(1, len(closes))
    ):

        momentum = "BEARISH"
        bearish_momentum = True

    # ========================================================
    # PREMIUM / DISCOUNT
    # ========================================================

    range_high = float(
        df["high"].iloc[-20:].max()
    )

    range_low = float(
        df["low"].iloc[-20:].min()
    )

    midpoint = (
        range_high + range_low
    ) / 2

    if close > midpoint:

        premium_discount = "PREMIUM"

    elif close < midpoint:

        premium_discount = "DISCOUNT"

    else:

        premium_discount = "EQUILIBRIUM"

    # ========================================================
    # SMC SCORE
    # ========================================================

    bull_score = 0
    bear_score = 0

    if bullish_liquidity:
        bull_score += 20

    if bearish_liquidity:
        bear_score += 20

    if bullish_bos:
        bull_score += 20

    if bearish_bos:
        bear_score += 20

    if bullish_fvg:
        bull_score += 10

    if bearish_fvg:
        bear_score += 10

    if bullish_displacement:
        bull_score += 15

    if bearish_displacement:
        bear_score += 15

    if bullish_momentum:
        bull_score += 10

    if bearish_momentum:
        bear_score += 10

    # Premium / discount contextual bonus
    if premium_discount == "DISCOUNT":
        bull_score += 5

    elif premium_discount == "PREMIUM":
        bear_score += 5

    if bull_score > bear_score:

        direction = "BUY"
        score = bull_score

    elif bear_score > bull_score:

        direction = "SELL"
        score = bear_score

    else:

        direction = "NONE"
        score = 0

    return {
        "liquidity": liquidity,
        "bos": bos,
        "fvg": fvg,
        "displacement": displacement,
        "momentum": momentum,
        "premium_discount": premium_discount,
        "direction": direction,
        "score": min(100, score)
    }


# ============================================================
# SIGNAL ENGINE
# ============================================================

def generate_signal(tfs):

    h4 = timeframe_analysis(tfs["H4"])
    h1 = timeframe_analysis(tfs["H1"])
    m15 = timeframe_analysis(tfs["M15"])
    m5 = timeframe_analysis(tfs["M5"])

    smc = m5_smc(tfs["M5"])

    # ========================================================
    # TREND SCORE
    # ========================================================

    buy_score = 0
    sell_score = 0

    # H4 = 20
    if h4["trend"] == "BULLISH":
        buy_score += 20

    elif h4["trend"] == "BEARISH":
        sell_score += 20

    # H1 = 20
    if h1["trend"] == "BULLISH":
        buy_score += 20

    elif h1["trend"] == "BEARISH":
        sell_score += 20

    # M15 = 15
    if m15["trend"] == "BULLISH":
        buy_score += 15

    elif m15["trend"] == "BEARISH":
        sell_score += 15

    # M5 = 15
    if m5["trend"] == "BULLISH":
        buy_score += 15

    elif m5["trend"] == "BEARISH":
        sell_score += 15

    # ========================================================
    # SMC
    # ========================================================

    if smc["direction"] == "BUY":

        buy_score += smc["score"]

    elif smc["direction"] == "SELL":

        sell_score += smc["score"]

    # ========================================================
    # CONTRADICTION FILTER
    # ========================================================

    if (
        h4["trend"] == "BULLISH"
        and h1["trend"] == "BEARISH"
    ):

        buy_score = min(buy_score, 59)

    elif (
        h4["trend"] == "BEARISH"
        and h1["trend"] == "BULLISH"
    ):

        sell_score = min(sell_score, 59)

    # ========================================================
    # FINAL
    # ========================================================

    if buy_score > sell_score:

        direction = "BUY"
        score = buy_score

    elif sell_score > buy_score:

        direction = "SELL"
        score = sell_score

    else:

        direction = "WAIT"
        score = 50

    # Cap
    score = min(100, int(score))

    # ========================================================
    # SIGNAL CLASS
    # ========================================================

    if direction != "WAIT" and score >= MIN_FULL_SCORE:

        signal_type = "FULL"

    elif direction != "WAIT" and score >= MIN_EARLY_SCORE:

        signal_type = "EARLY"

    else:

        direction = "WAIT"
        signal_type = "WAIT"

    return {
        "direction": direction,
        "score": score,
        "type": signal_type,
        "h4": h4,
        "h1": h1,
        "m15": m15,
        "m5": m5,
        "smc": smc
    }


# ============================================================
# LEVELS
# ============================================================

def calculate_levels(price, direction):

    # Simple robust gold structure
    # Can be replaced later by ATR/SMC structure.

    sl_distance = 4.0

    if direction == "BUY":

        entry = price

        sl = entry - sl_distance

        risk = entry - sl

        tp1 = entry + risk * 1.5

        tp2 = entry + risk * 3.0

    elif direction == "SELL":

        entry = price

        sl = entry + sl_distance

        risk = sl - entry

        tp1 = entry - risk * 1.5

        tp2 = entry - risk * 3.0

    else:

        return None

    # ========================================================
    # Approximate lot calculation
    #
    # Gold:
    # 1 lot ≈ 100 oz
    #
    # Risk = deposit * 1%
    # ========================================================

    risk_money = (
        DEPOSIT
        * RISK_PERCENT
        / 100
    )

    # Approximate USD risk:
    # 1 lot * $1 move ≈ $100
    #
    # lot = risk / (SL * 100)

    lot = risk_money / (
        sl_distance * 100
    )

    # User safety limits
    lot = max(0.01, min(0.02, lot))

    return {
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "tp1": round(tp1, 2),
        "tp2": round(tp2, 2),
        "rr": "1:3",
        "lot": round(lot, 2)
    }


# ============================================================
# CONFIDENCE
# ============================================================

def confidence_from_score(score):

    if score >= 85:
        return "VERY HIGH"

    if score >= 75:
        return "HIGH"

    if score >= 70:
        return "GOOD"

    if score >= 60:
        return "MEDIUM"

    return "LOW"


# ============================================================
# FORMAT SIGNAL
# ============================================================

def format_signal(price, result):

    direction = result["direction"]
    score = result["score"]
    signal_type = result["type"]

    h4 = result["h4"]
    h1 = result["h1"]
    m15 = result["m15"]
    m5 = result["m5"]
    smc = result["smc"]

    confidence = confidence_from_score(score)

    if direction == "BUY":

        emoji = "🟢"

    elif direction == "SELL":

        emoji = "🔴"

    else:

        emoji = "⚪"

    lines = []

    lines.append(
        "🥇 GOLD SMART V5.9.5"
    )

    lines.append("")

    lines.append(
        f"{emoji} SIGNAL: {direction}"
    )

    lines.append(
        f"💰 XAUUSD: {price:.2f}"
    )

    lines.append("")

    lines.append(
        f"📊 H4: {h4['trend']} "
        f"({h4['score']}/100)"
    )

    lines.append(
        f"📊 H1: {h1['trend']} "
        f"({h1['score']}/100)"
    )

    lines.append(
        f"📊 M15: {m15['trend']} "
        f"({m15['score']}/100)"
    )

    lines.append(
        f"📊 M5: {m5['trend']} "
        f"({m5['score']}/100)"
    )

    lines.append("")

    lines.append(
        f"💧 Liquidity: "
        f"{smc['liquidity']}"
    )

    lines.append(
        f"🔨 BOS: {smc['bos']}"
    )

    lines.append(
        f"🧩 FVG: {smc['fvg']}"
    )

    lines.append(
        f"💥 Displacement: "
        f"{smc['displacement']}"
    )

    lines.append(
        f"📈 Momentum: "
        f"{smc['momentum']}"
    )

    lines.append(
        f"⚖️ Zone: "
        f"{smc['premium_discount']}"
    )

    lines.append("")

    lines.append(
        f"🎯 SCORE: {score}/100"
    )

    lines.append(
        f"🔥 TYPE: {signal_type}"
    )

    lines.append(
        f"🧠 CONFIDENCE: {confidence}"
    )

    # --------------------------------------------------------
    # Levels
    # --------------------------------------------------------

    if direction in ("BUY", "SELL"):

        levels = calculate_levels(
            price,
            direction
        )

        lines.append("")

        lines.append(
            f"📍 ENTRY: {levels['entry']:.2f}"
        )

        lines.append(
            f"🛑 SL: {levels['sl']:.2f}"
        )

        lines.append(
            f"🎯 TP1: {levels['tp1']:.2f}"
        )

        lines.append(
            f"🎯 TP2: {levels['tp2']:.2f}"
        )

        lines.append(
            f"📐 RR: {levels['rr']}"
        )

        lines.append(
            f"💼 LOT: {levels['lot']:.2f}"
        )

    else:

        lines.append("")

        lines.append(
            "⏳ ACTION: WAIT"
        )

        lines.append(
            "Не входить до появления "
            "качественного подтверждения."
        )

    lines.append("")

    lines.append(
        "🛡 AUTO TRADING: OFF"
    )

    lines.append(
        f"📉 Risk: {RISK_PERCENT:.1f}%"
    )

    lines.append(
        "🕯 Closed candles: ON"
    )

    return "\n".join(lines)


# ============================================================
# STATS
# ============================================================

def format_stats():

    total = int(
        STATE.get("signals", 0)
    )

    full = int(
        STATE.get("full_signals", 0)
    )

    early = int(
        STATE.get("early_signals", 0)
    )

    wins = int(
        STATE.get("wins", 0)
    )

    losses = int(
        STATE.get("losses", 0)
    )

    closed = wins + losses

    if closed > 0:

        winrate = (
            wins / closed
        ) * 100

    else:

        winrate = 0

    return (
        "📊 GOLD SMART V5.9.5\n"
        "\n"
        f"🔢 Signals: {total}\n"
        f"🟢 Full: {full}\n"
        f"🟡 Early: {early}\n"
        f"✅ Wins: {wins}\n"
        f"❌ Losses: {losses}\n"
        f"🎯 Winrate: {winrate:.1f}%\n"
        "\n"
        "🤖 Auto trading: OFF"
    )


# ============================================================
# STATUS
# ============================================================

def status_text():

    return (
        "🥇 GOLD SMART V5.9.5\n"
        "\n"
        "🟢 Engine: READY\n"
        "🟢 Telegram: READY\n"
        "🟢 XAUS API: READY\n"
        "\n"
        "💰 XAU/USD\n"
        "🛡 AUTO TRADING: OFF\n"
        f"📉 Risk: {RISK_PERCENT:.1f}%\n"
        "\n"
        "🕯 Closed candles: ON\n"
        "🧠 H4 → H1 → M15 → M5\n"
        "\n"
        "📡 Data: XAUS.COM"
    )


# ============================================================
# HELP
# ============================================================

def help_text():

    return (
        "🥇 GOLD SMART V5.9.5\n"
        "\n"
        "📋 COMMANDS\n"
        "\n"
        "/start — запуск\n"
        "/status — состояние бота\n"
        "/signal — анализ XAU/USD\n"
        "/test — тест Telegram\n"
        "/stats — статистика\n"
        "/help — помощь\n"
        "\n"
        "🧠 Анализ:\n"
        "H4 → H1 → M15 → M5\n"
        "\n"
        "📊 SMC:\n"
        "Liquidity\n"
        "BOS\n"
        "FVG\n"
        "Displacement\n"
        "Momentum\n"
        "Premium / Discount\n"
        "\n"
        "🛡 Auto trading: OFF"
    )


# ============================================================
# TEST
# ============================================================

def test_text():

    return (
        "🟢 TEST OK\n"
        "\n"
        "Telegram → Render → Bot\n"
        "Webhook работает.\n"
        "\n"
        "🥇 GOLD SMART V5.9.5"
    )


# ============================================================
# PROCESS SIGNAL
# ============================================================

def process_signal(chat_id):

    send_telegram(
        "🔎 GOLD SMART V5.9.5\n\n"
        "Анализирую XAU/USD...\n"
        "H4 → H1 → M15 → M5",
        chat_id
    )

    try:

        # ----------------------------------------------------
        # SPOT
        # ----------------------------------------------------

        price = get_spot_price()

        # ----------------------------------------------------
        # INTRADAY
        # ----------------------------------------------------

        df = get_intraday_points()

        # ----------------------------------------------------
        # TIMEFRAMES
        # ----------------------------------------------------

        tfs = build_all_timeframes(df)

        validate_timeframes(tfs)

        # ----------------------------------------------------
        # ANALYSIS
        # ----------------------------------------------------

        result = generate_signal(tfs)

        # ----------------------------------------------------
        # STATE
        # ----------------------------------------------------

        with state_lock:

            STATE["signals"] = (
                int(STATE.get("signals", 0))
                + 1
            )

            if result["type"] == "FULL":

                STATE["full_signals"] = (
                    int(
                        STATE.get(
                            "full_signals",
                            0
                        )
                    ) + 1
                )

            elif result["type"] == "EARLY":

                STATE["early_signals"] = (
                    int(
                        STATE.get(
                            "early_signals",
                            0
                        )
                    ) + 1
                )

            STATE["last_signal"] = (
                result["direction"]
            )

            STATE["last_signal_time"] = (
                datetime.now(
                    timezone.utc
                ).isoformat()
            )

        save_state()

        # ----------------------------------------------------
        # SEND RESULT
        # ----------------------------------------------------

        message = format_signal(
            price,
            result
        )

        send_telegram(
            message,
            chat_id
        )

        log(
            f"SIGNAL | "
            f"{result['direction']} | "
            f"{result['score']}/100"
        )

    except Exception as e:

        log(
            f"SIGNAL ERROR: {e}"
        )

        send_telegram(
            "🥇 GOLD SMART V5.9.5\n\n"
            "🔴 ERROR\n\n"
            f"❌ {str(e)}",
            chat_id
        )


# ============================================================
# TELEGRAM UPDATE PROCESSOR
# ============================================================

def process_update(update):

    try:

        message = update.get("message")

        if not message:
            return

        chat = message.get("chat", {})

        chat_id = str(
            chat.get("id", "")
        )

        text_message = (
            message.get("text", "")
            .strip()
        )

        if not text_message:
            return

        # ----------------------------------------------------
        # Commands
        # ----------------------------------------------------

        command = (
            text_message
            .split()[0]
            .lower()
            .split("@")[0]
        )

        log(
            f"Telegram command: "
            f"{command} from {chat_id}"
        )

        if command == "/start":

            send_telegram(
                "🥇 GOLD SMART V5.9.5\n\n"
                "🟢 Бот запущен.\n\n"
                "Используй /help",
                chat_id
            )

        elif command == "/help":

            send_telegram(
                help_text(),
                chat_id
            )

        elif command == "/test":

            send_telegram(
                test_text(),
                chat_id
            )

        elif command == "/status":

            send_telegram(
                status_text(),
                chat_id
            )

        elif command == "/stats":

            send_telegram(
                format_stats(),
                chat_id
            )

        elif command == "/signal":

            # Run analysis in background
            thread = threading.Thread(
                target=process_signal,
                args=(chat_id,),
                daemon=True
            )

            thread.start()

        else:

            send_telegram(
                "❓ Неизвестная команда.\n\n"
                "Используй /help",
                chat_id
            )

    except Exception as e:

        log(
            f"UPDATE ERROR: {e}"
        )


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

def set_webhook():

    if not BOT_TOKEN:
        log(
            "Webhook not configured: "
            "BOT_TOKEN missing"
        )
        return

    webhook_url = (
        f"{BASE_URL}/telegram"
    )

    try:

        response = requests.post(
            telegram_url("setWebhook"),
            json={
                "url": webhook_url,
                "drop_pending_updates": True
            },
            timeout=REQUEST_TIMEOUT
        )

        log(
            f"SET WEBHOOK: "
            f"{response.status_code} "
            f"{response.text[:500]}"
        )

    except Exception as e:

        log(
            f"WEBHOOK ERROR: {e}"
        )


# ============================================================
# FLASK ROUTES
# ============================================================

@app.route("/", methods=["GET"])
def home():

    return {
        "app": "GOLD SMART V5.9.5",
        "status": "READY",
        "auto_trading": False,
        "risk_percent": RISK_PERCENT,
        "data_source": "XAUS.COM"
    }


@app.route("/health", methods=["GET"])
def health():

    return {
        "status": "ok",
        "app": "GOLD SMART V5.9.5"
    }


@app.route("/webhook-info", methods=["GET"])
def webhook_info():

    if not BOT_TOKEN:

        return {
            "error": "BOT_TOKEN missing"
        }, 500

    try:

        response = requests.get(
            telegram_url("getWebhookInfo"),
            timeout=REQUEST_TIMEOUT
        )

        return response.json()

    except Exception as e:

        return {
            "error": str(e)
        }, 500


@app.route("/telegram", methods=["POST"])
def telegram_webhook():

    try:

        update = request.get_json(
            silent=True
        )

        if update:

            # Fast response to Telegram
            thread = threading.Thread(
                target=process_update,
                args=(update,),
                daemon=True
            )

            thread.start()

        return "OK", 200

    except Exception as e:

        log(
            f"WEBHOOK ERROR: {e}"
        )

        return "OK", 200


# ============================================================
# STARTUP
# ============================================================

def startup():

    time.sleep(2)

    log(
        "======================================"
    )

    log(
        "🥇 GOLD SMART V5.9.5 STARTING"
    )

    log(
        f"AUTO TRADING = {AUTO_TRADING}"
    )

    log(
        f"RISK = {RISK_PERCENT}%"
    )

    log(
        "DATA SOURCE = XAUS.COM"
    )

    log(
        "TIMEFRAMES = H4 → H1 → M15 → M5"
    )

    log(
        f"MIN CANDLES = "
        f"M5:{MIN_M5} "
        f"M15:{MIN_M15} "
        f"H1:{MIN_H1} "
        f"H4:{MIN_H4}"
    )

    log(
        "======================================"
    )

    set_webhook()


# ============================================================
# START BACKGROUND THREAD
# ============================================================

threading.Thread(
    target=startup,
    daemon=True
).start()


# ============================================================
# LOCAL RUN
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv("PORT", "5000")
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
