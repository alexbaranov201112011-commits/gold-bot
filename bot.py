import os
import json
import time
import threading
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd

from flask import Flask, request, jsonify


# ============================================================
# 🥇 GOLD SMART V5.9.4
# XAU/USD SMART SMC SIGNAL ENGINE
#
# DATA SOURCE: XAUS.COM
# TIMEFRAMES: H4 → H1 → M15 → M5
#
# AUTO TRADING = OFF
# RISK = 1%
#
# FIX:
# - XAUS intraday = ~2 minute points
# - 48h history
# - correct UTC conversion
# - robust resampling
# - closed candles only
# - realistic TF minimums
# - H4 does NOT require 50 candles
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

MIN_FULL_SCORE = 70
MIN_EARLY_SCORE = 60

SESSION_START = 12
SESSION_END = 23

XAUS_SPOT_URL = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY_URL = "https://xaus.com/api/v1/intraday"

STATE_FILE = "gold_smart_state.json"

REQUEST_TIMEOUT = 20

# Minimum closed candles required
MIN_M5 = 50
MIN_M15 = 30
MIN_H1 = 20
MIN_H4 = 8

# Maximum age of data
MAX_DATA_AGE_MINUTES = 15


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# STATE
# ============================================================

DEFAULT_STATE = {
    "signal_total": 0,
    "full_signals": 0,
    "early_signals": 0,
    "wins": 0,
    "losses": 0,
    "last_signal": "NONE",
    "last_signal_time": "",
    "last_signal_price": 0.0,
    "last_error": "",
}


def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            state = DEFAULT_STATE.copy()
            state.update(data)
            return state

    except Exception as e:
        print(f"STATE LOAD ERROR: {e}")

    return DEFAULT_STATE.copy()


STATE = load_state()


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(STATE, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"STATE SAVE ERROR: {e}")


# ============================================================
# TELEGRAM
# ============================================================

def telegram_call(method, payload=None):
    if not BOT_TOKEN:
        print("TELEGRAM ERROR: BOT_TOKEN missing")
        return None

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    try:
        r = requests.post(
            url,
            json=payload or {},
            timeout=REQUEST_TIMEOUT
        )

        print(
            f"TELEGRAM {method}: "
            f"{r.status_code} {r.text[:300]}"
        )

        if r.ok:
            return r.json()

    except Exception as e:
        print(f"TELEGRAM REQUEST ERROR: {e}")

    return None


def send_message(text_message, chat_id=None):
    target = str(chat_id or CHAT_ID).strip()

    if not target:
        print("TELEGRAM ERROR: CHAT_ID missing")
        return False

    result = telegram_call(
        "sendMessage",
        {
            "chat_id": target,
            "text": text_message,
        }
    )

    return bool(result and result.get("ok"))


# ============================================================
# WEBHOOK
# ============================================================

def webhook_url():
    return f"{BASE_URL}/telegram"


def set_webhook():
    if not BOT_TOKEN:
        print("WEBHOOK ERROR: BOT_TOKEN missing")
        return

    result = telegram_call(
        "setWebhook",
        {
            "url": webhook_url(),
            "drop_pending_updates": False,
            "allowed_updates": ["message"],
        }
    )

    if result and result.get("ok"):
        print("TELEGRAM WEBHOOK READY")
        print(f"WEBHOOK URL: {webhook_url()}")
    else:
        print("TELEGRAM WEBHOOK ERROR")


def get_webhook_info():
    return telegram_call("getWebhookInfo")


# ============================================================
# XAUS SPOT
# ============================================================

def get_spot_price():
    try:
        url = f"{XAUS_SPOT_URL}?compact=1&fresh={int(time.time())}"

        r = requests.get(
            url,
            timeout=REQUEST_TIMEOUT
        )

        r.raise_for_status()

        data = r.json()

        price = data.get("spot_usd_oz")

        if price is None:
            xau = data.get("xau", {})
            price = xau.get("price")

        if price is None:
            raise ValueError("spot_usd_oz / xau.price not found")

        price = float(price)

        if price <= 0:
            raise ValueError("Invalid spot price")

        return price

    except Exception as e:
        print(f"XAUS SPOT ERROR: {e}")
        raise


# ============================================================
# XAUS INTRADAY
# ============================================================

def get_intraday_points():
    """
    XAUS intraday:
    - symbol=xau
    - hours=48
    - approximately 2-minute points
    """

    try:
        params = {
            "symbol": "xau",
            "hours": 48,
            "fresh": int(time.time()),
        }

        r = requests.get(
            XAUS_INTRADAY_URL,
            params=params,
            timeout=REQUEST_TIMEOUT
        )

        print(
            f"XAUS INTRADAY HTTP {r.status_code}"
        )

        r.raise_for_status()

        data = r.json()

        points = data.get("points", [])

        if not isinstance(points, list):
            raise ValueError("XAUS points is not a list")

        print(
            f"XAUS POINTS RECEIVED: {len(points)}"
        )

        if len(points) < 100:
            raise ValueError(
                f"Too few XAUS points: {len(points)}"
            )

        rows = []

        for p in points:

            if not isinstance(p, dict):
                continue

            ts = p.get("t")
            price = p.get("p")

            if ts is None or price is None:
                continue

            try:
                price = float(price)

                if price <= 0:
                    continue

                # XAUS timestamps are expected to be UTC.
                # Support ISO strings and unix timestamps.

                if isinstance(ts, (int, float)):
                    dt = pd.to_datetime(
                        ts,
                        unit="s",
                        utc=True,
                        errors="coerce"
                    )
                else:
                    dt = pd.to_datetime(
                        ts,
                        utc=True,
                        errors="coerce"
                    )

                if pd.isna(dt):
                    continue

                rows.append(
                    {
                        "time": dt,
                        "price": price,
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
            subset=["time"]
        )

        df = df.sort_values("time")

        df = df.set_index("time")

        df = df[~df.index.duplicated(
            keep="last"
        )]

        # Remove impossible values
        df = df[
            np.isfinite(df["price"])
        ]

        if len(df) < 100:
            raise ValueError(
                f"Clean XAUS points too few: {len(df)}"
            )

        # ====================================================
        # DATA FRESHNESS
        # ====================================================

        latest = df.index[-1]

        now = pd.Timestamp.now(tz="UTC")

        age_minutes = (
            now - latest
        ).total_seconds() / 60.0

        print(
            f"XAUS LATEST: {latest.isoformat()}"
        )

        print(
            f"XAUS DATA AGE: {age_minutes:.1f} min"
        )

        if age_minutes > MAX_DATA_AGE_MINUTES:
            raise ValueError(
                f"XAUS data too old: "
                f"{age_minutes:.1f} min"
            )

        return df

    except Exception as e:
        print(f"XAUS INTRADAY ERROR: {e}")
        raise


# ============================================================
# RESAMPLING
# ============================================================

def build_timeframe(df, rule):
    """
    Build OHLC from XAUS price points.

    XAUS supplies price observations, not native candles.
    Therefore:
        open  = first
        high  = max
        low   = min
        close = last
    """

    ohlc = df["price"].resample(
        rule,
        label="right",
        closed="right"
    ).agg(
        [
            "first",
            "max",
            "min",
            "last",
        ]
    )

    ohlc.columns = [
        "open",
        "high",
        "low",
        "close",
    ]

    ohlc = ohlc.dropna()

    # ========================================================
    # REMOVE CURRENT UNFINISHED CANDLE
    # ========================================================

    now = pd.Timestamp.now(tz="UTC")

    if rule == "5min":
        current_start = now.floor("5min")

    elif rule == "15min":
        current_start = now.floor("15min")

    elif rule == "1h":
        current_start = now.floor("1h")

    elif rule == "4h":
        current_start = now.floor("4h")

    else:
        current_start = None

    if current_start is not None:
        ohlc = ohlc[
            ohlc.index < current_start
        ]

    return ohlc


def build_all_timeframes(df):

    m5 = build_timeframe(df, "5min")
    m15 = build_timeframe(df, "15min")
    h1 = build_timeframe(df, "1h")
    h4 = build_timeframe(df, "4h")

    print(
        f"TF CANDLES | "
        f"M5={len(m5)} "
        f"M15={len(m15)} "
        f"H1={len(h1)} "
        f"H4={len(h4)}"
    )

    return {
        "M5": m5,
        "M15": m15,
        "H1": h1,
        "H4": h4,
    }


# ============================================================
# DATA VALIDATION
# ============================================================

def validate_timeframes(tfs):

    required = {
        "M5": MIN_M5,
        "M15": MIN_M15,
        "H1": MIN_H1,
        "H4": MIN_H4,
    }

    for name, minimum in required.items():

        count = len(tfs[name])

        print(
            f"{name}: "
            f"{count}/{minimum}"
        )

        if count < minimum:
            raise ValueError(
                f"Not enough {name} candles "
                f"({count}/{minimum})"
            )

    return True


# ============================================================
# INDICATORS
# ============================================================

def ema(series, period):
    return series.ewm(
        span=period,
        adjust=False
    ).mean()


def rsi(series, period=14):

    delta = series.diff()

    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )

    avg_gain = gain.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(
        0,
        np.nan
    )

    result = 100 - (
        100 / (1 + rs)
    )

    return result.fillna(50)


def add_indicators(df):

    df = df.copy()

    df["ema20"] = ema(
        df["close"],
        20
    )

    df["ema50"] = ema(
        df["close"],
        50
    )

    df["ema200"] = ema(
        df["close"],
        200
    )

    df["rsi"] = rsi(
        df["close"],
        14
    )

    return df


# ============================================================
# TREND
# ============================================================

def timeframe_analysis(df, name):

    df = add_indicators(df)

    last = df.iloc[-1]

    close = float(last["close"])
    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])
    rsi_value = float(last["rsi"])

    # --------------------------------------------------------
    # TREND
    # --------------------------------------------------------

    bullish_points = 0
    bearish_points = 0

    if close > ema20:
        bullish_points += 1
    else:
        bearish_points += 1

    if close > ema50:
        bullish_points += 1
    else:
        bearish_points += 1

    # EMA 200 only if enough history exists
    if len(df) >= 200:

        ema200 = float(last["ema200"])

        if close > ema200:
            bullish_points += 1
        else:
            bearish_points += 1

    # RSI
    if rsi_value >= 55:
        bullish_points += 1

    elif rsi_value <= 45:
        bearish_points += 1

    # --------------------------------------------------------
    # STRUCTURE
    # --------------------------------------------------------

    recent = df.tail(
        min(10, len(df))
    )

    previous_high = float(
        recent["high"].iloc[:-1].max()
    ) if len(recent) > 1 else float(
        recent["high"].max()
    )

    previous_low = float(
        recent["low"].iloc[:-1].min()
    ) if len(recent) > 1 else float(
        recent["low"].min()
    )

    bos = "NONE"

    if close > previous_high:
        bos = "BULLISH"
        bullish_points += 1

    elif close < previous_low:
        bos = "BEARISH"
        bearish_points += 1

    # --------------------------------------------------------
    # SCORE
    # --------------------------------------------------------

    if bullish_points > bearish_points:
        trend = "BULLISH"

    elif bearish_points > bullish_points:
        trend = "BEARISH"

    else:
        trend = "MIXED"

    score = int(
        50 +
        (
            abs(
                bullish_points -
                bearish_points
            ) * 10
        )
    )

    score = max(
        0,
        min(100, score)
    )

    return {
        "name": name,
        "trend": trend,
        "score": score,
        "rsi": rsi_value,
        "bos": bos,
        "close": close,
    }


# ============================================================
# M5 SMC
# ============================================================

def m5_smc(df):

    df = add_indicators(df)

    if len(df) < 10:
        return {
            "liquidity": "NONE",
            "bos": "NONE",
            "fvg": "NONE",
            "displacement": "NONE",
            "momentum": "NONE",
            "premium_discount": "EQUILIBRIUM",
        }

    last = df.iloc[-1]
    prev = df.iloc[-2]
    prev2 = df.iloc[-3]

    recent = df.tail(10)

    recent_high = float(
        recent["high"].iloc[:-1].max()
    )

    recent_low = float(
        recent["low"].iloc[:-1].min()
    )

    close = float(last["close"])

    high = float(last["high"])
    low = float(last["low"])

    prev_high = float(prev["high"])
    prev_low = float(prev["low"])

    liquidity = "NONE"

    # --------------------------------------------------------
    # SELL-SIDE LIQUIDITY SWEEP
    # --------------------------------------------------------

    if (
        low < recent_low
        and close > recent_low
    ):
        liquidity = "SELL-SIDE SWEPT"

    # --------------------------------------------------------
    # BUY-SIDE LIQUIDITY SWEEP
    # --------------------------------------------------------

    elif (
        high > recent_high
        and close < recent_high
    ):
        liquidity = "BUY-SIDE SWEPT"

    # --------------------------------------------------------
    # BOS
    # --------------------------------------------------------

    bos = "NONE"

    if close > recent_high:
        bos = "BULLISH"

    elif close < recent_low:
        bos = "BEARISH"

    # --------------------------------------------------------
    # FVG
    # --------------------------------------------------------

    fvg = "NONE"

    high_2 = float(prev2["high"])
    low_2 = float(prev2["low"])

    if low > high_2:
        fvg = "BULLISH"

    elif high < low_2:
        fvg = "BEARISH"

    # --------------------------------------------------------
    # DISPLACEMENT
    # --------------------------------------------------------

    body = abs(
        close -
        float(last["open"])
    )

    candle_range = (
        high - low
    )

    avg_range = (
        df["high"] -
        df["low"]
    ).tail(10).mean()

    displacement = "NONE"

    if (
        candle_range > 0
        and body > avg_range * 0.8
    ):
        if close > float(last["open"]):
            displacement = "BULLISH"
        else:
            displacement = "BEARISH"

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    momentum = "NONE"

    ema20 = float(
        last["ema20"]
    )

    if close > ema20:
        momentum = "BULLISH"

    elif close < ema20:
        momentum = "BEARISH"

    # --------------------------------------------------------
    # PREMIUM / DISCOUNT
    # --------------------------------------------------------

    range_high = float(
        recent["high"].max()
    )

    range_low = float(
        recent["low"].min()
    )

    midpoint = (
        range_high +
        range_low
    ) / 2

    if close > midpoint:
        premium_discount = "PREMIUM"

    elif close < midpoint:
        premium_discount = "DISCOUNT"

    else:
        premium_discount = "EQUILIBRIUM"

    return {
        "liquidity": liquidity,
        "bos": bos,
        "fvg": fvg,
        "displacement": displacement,
        "momentum": momentum,
        "premium_discount": premium_discount,
    }


# ============================================================
# SIGNAL ENGINE
# ============================================================

def generate_signal(tfs):

    h4 = timeframe_analysis(
        tfs["H4"],
        "H4"
    )

    h1 = timeframe_analysis(
        tfs["H1"],
        "H1"
    )

    m15 = timeframe_analysis(
        tfs["M15"],
        "M15"
    )

    m5 = timeframe_analysis(
        tfs["M5"],
        "M5"
    )

    smc = m5_smc(
        tfs["M5"]
    )

    buy_score = 0
    sell_score = 0

    # ========================================================
    # HIGHER TIMEFRAME
    # ========================================================

    weights = {
        "H4": 20,
        "H1": 20,
        "M15": 15,
        "M5": 15,
    }

    analyses = {
        "H4": h4,
        "H1": h1,
        "M15": m15,
        "M5": m5,
    }

    for name, analysis in analyses.items():

        weight = weights[name]

        if analysis["trend"] == "BULLISH":
            buy_score += weight

        elif analysis["trend"] == "BEARISH":
            sell_score += weight

    # ========================================================
    # SMC
    # ========================================================

    if smc["liquidity"] == "SELL-SIDE SWEPT":
        buy_score += 10

    elif smc["liquidity"] == "BUY-SIDE SWEPT":
        sell_score += 10

    if smc["bos"] == "BULLISH":
        buy_score += 10

    elif smc["bos"] == "BEARISH":
        sell_score += 10

    if smc["fvg"] == "BULLISH":
        buy_score += 5

    elif smc["fvg"] == "BEARISH":
        sell_score += 5

    if smc["displacement"] == "BULLISH":
        buy_score += 5

    elif smc["displacement"] == "BEARISH":
        sell_score += 5

    if smc["momentum"] == "BULLISH":
        buy_score += 5

    elif smc["momentum"] == "BEARISH":
        sell_score += 5

    # ========================================================
    # HIGHER TF CONTRADICTION FILTER
    # ========================================================

    direction = "WAIT"
    score = max(
        buy_score,
        sell_score
    )

    if (
        h4["trend"] == "BULLISH"
        and h1["trend"] == "BULLISH"
        and sell_score > buy_score
    ):
        score = min(
            score,
            MIN_EARLY_SCORE - 1
        )

    elif (
        h4["trend"] == "BEARISH"
        and h1["trend"] == "BEARISH"
        and buy_score > sell_score
    ):
        score = min(
            score,
            MIN_EARLY_SCORE - 1
        )

    if buy_score > sell_score:
        direction = "BUY"

    elif sell_score > buy_score:
        direction = "SELL"

    else:
        direction = "WAIT"

    # ========================================================
    # SIGNAL TYPE
    # ========================================================

    if score >= MIN_FULL_SCORE:
        signal_type = "FULL"

    elif score >= MIN_EARLY_SCORE:
        signal_type = "EARLY"

    else:
        signal_type = "WAIT"

    if signal_type == "WAIT":
        direction = "WAIT"

    # ========================================================
    # CONFIDENCE
    # ========================================================

    confidence = "LOW"

    if score >= 80:
        confidence = "HIGH"

    elif score >= 70:
        confidence = "MEDIUM-HIGH"

    elif score >= 60:
        confidence = "MEDIUM"

    # ========================================================
    # PRICE
    # ========================================================

    price = float(
        tfs["M5"]["close"].iloc[-1]
    )

    return {
        "direction": direction,
        "score": int(score),
        "buy_score": int(buy_score),
        "sell_score": int(sell_score),
        "type": signal_type,
        "confidence": confidence,
        "price": price,
        "h4": h4,
        "h1": h1,
        "m15": m15,
        "m5": m5,
        "smc": smc,
    }


# ============================================================
# LEVELS
# ============================================================

def calculate_levels(signal):

    price = float(
        signal["price"]
    )

    direction = signal["direction"]

    # Simple volatility-aware SL
    m5 = signal["m5"]

    recent_range = (
        m5["close"]
    )

    if direction == "BUY":

        sl = price - 4.0

        risk_distance = (
            price - sl
        )

        tp1 = price + (
            risk_distance * 1.5
        )

        tp2 = price + (
            risk_distance * 3.0
        )

    elif direction == "SELL":

        sl = price + 4.0

        risk_distance = (
            sl - price
        )

        tp1 = price - (
            risk_distance * 1.5
        )

        tp2 = price - (
            risk_distance * 3.0
        )

    else:
        return None

    # ========================================================
    # LOT CALCULATION
    #
    # Approximation for XAUUSD:
    # 1 lot ≈ $100 per $1 move
    # ========================================================

    risk_money = (
        DEPOSIT *
        RISK_PERCENT /
        100
    )

    if risk_distance > 0:

        raw_lot = (
            risk_money /
            (risk_distance * 100)
        )

    else:
        raw_lot = 0.01

    lot = max(
        0.01,
        min(
            0.02,
            raw_lot
        )
    )

    return {
        "entry": price,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "rr": 3.0,
        "lot": lot,
    }


# ============================================================
# FORMAT SIGNAL
# ============================================================

def format_signal(signal):

    direction = signal["direction"]
    score = signal["score"]

    if direction == "BUY":
        icon = "🟢"

    elif direction == "SELL":
        icon = "🔴"

    else:
        icon = "⚪"

    smc = signal["smc"]

    text = (
        "🥇 GOLD SMART V5.9.4\n\n"
        f"{icon} SIGNAL: {direction}\n"
        f"💰 XAUUSD: {signal['price']:.2f}\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 H4: {signal['h4']['trend']} "
        f"({signal['h4']['score']}/100)\n"
        f"📊 H1: {signal['h1']['trend']} "
        f"({signal['h1']['score']}/100)\n"
        f"📊 M15: {signal['m15']['trend']} "
        f"({signal['m15']['score']}/100)\n"
        f"📊 M5: {signal['m5']['trend']} "
        f"({signal['m5']['score']}/100)\n\n"
        "🧭 HIGHER TF BIAS: "
    )

    if (
        signal["h4"]["trend"] ==
        signal["h1"]["trend"]
    ):
        text += signal["h4"]["trend"]

    else:
        text += "MIXED"

    text += (
        "\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"💧 Liquidity: {smc['liquidity']}\n"
        f"🔨 BOS: {smc['bos']}\n"
        f"🧩 FVG: {smc['fvg']}\n"
        f"💥 Displacement: "
        f"{smc['displacement']}\n"
        f"📈 Momentum: "
        f"{smc['momentum']}\n"
        f"⚖️ Zone: "
        f"{smc['premium_discount']}\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"🎯 SCORE: {score}/100\n"
        f"🧠 CONFIDENCE: "
        f"{signal['confidence']}\n"
        f"📌 TYPE: {signal['type']}\n"
    )

    levels = calculate_levels(
        signal
    )

    if levels and direction != "WAIT":

        text += (
            "\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"📍 ENTRY: "
            f"{levels['entry']:.2f}\n"
            f"🛑 SL: "
            f"{levels['sl']:.2f}\n"
            f"🎯 TP1: "
            f"{levels['tp1']:.2f}\n"
            f"🎯 TP2: "
            f"{levels['tp2']:.2f}\n"
            f"📐 RR: 1:{levels['rr']:.0f}\n"
            f"📦 LOT: "
            f"{levels['lot']:.2f}\n\n"
        )

    text += (
        "━━━━━━━━━━━━━━━━━━\n"
        "🛡 AUTO TRADING: OFF\n"
        f"📉 RISK: {RISK_PERCENT:.1f}%\n"
        "🕯 CLOSED CANDLES: ON"
    )

    return text


# ============================================================
# PROCESS SIGNAL
# ============================================================

def process_signal(chat_id=None):

    target_chat = str(
        chat_id or CHAT_ID
    )

    try:

        print("=" * 60)
        print("SIGNAL ANALYSIS START")
        print("=" * 60)

        send_message(
            "🔎 GOLD SMART\n\n"
            "Анализирую XAU/USD...\n"
            "H4 → H1 → M15 → M5",
            target_chat
        )

        # ----------------------------------------------------
        # SPOT
        # ----------------------------------------------------

        spot = get_spot_price()

        print(
            f"XAUS SPOT: {spot:.2f}"
        )

        # ----------------------------------------------------
        # INTRADAY
        # ----------------------------------------------------

        raw = get_intraday_points()

        # ----------------------------------------------------
        # TIMEFRAMES
        # ----------------------------------------------------

        tfs = build_all_timeframes(
            raw
        )

        # ----------------------------------------------------
        # VALIDATE
        # ----------------------------------------------------

        validate_timeframes(
            tfs
        )

        # ----------------------------------------------------
        # SIGNAL
        # ----------------------------------------------------

        signal = generate_signal(
            tfs
        )

        # Use current spot for display
        signal["price"] = spot

        # ----------------------------------------------------
        # STATS
        # ----------------------------------------------------

        if signal["direction"] != "WAIT":

            STATE["signal_total"] += 1

            if signal["type"] == "FULL":
                STATE["full_signals"] += 1

            elif signal["type"] == "EARLY":
                STATE["early_signals"] += 1

            STATE["last_signal"] = (
                signal["direction"]
            )

            STATE["last_signal_time"] = (
                datetime.now(
                    timezone.utc
                ).isoformat()
            )

            STATE["last_signal_price"] = (
                spot
            )

            save_state()

        # ----------------------------------------------------
        # SEND
        # ----------------------------------------------------

        send_message(
            format_signal(signal),
            target_chat
        )

        print(
            f"SIGNAL COMPLETE | "
            f"{signal['direction']} | "
            f"{signal['score']}/100"
        )

    except Exception as e:

        error = str(e)

        STATE["last_error"] = error
        save_state()

        print(
            f"SIGNAL ERROR: {error}"
        )

        send_message(
            "🥇 GOLD SMART V5.9.4\n\n"
            "🔴 ERROR\n\n"
            f"❌ {error}",
            target_chat
        )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def process_update(update):

    try:

        message = update.get(
            "message",
            {}
        )

        chat = message.get(
            "chat",
            {}
        )

        chat_id = chat.get(
            "id"
        )

        text = (
            message.get(
                "text",
                ""
            )
            .strip()
            .lower()
        )

        if not chat_id:
            return

        print(
            f"TELEGRAM COMMAND: {text}"
        )

        # ====================================================
        # /START
        # ====================================================

        if text.startswith("/start"):

            send_message(
                "🥇 GOLD SMART V5.9.4\n\n"
                "🟢 Бот запущен.\n"
                "🟢 Telegram READY\n"
                "🟢 XAUS READY\n"
                "🛡 AUTO TRADING: OFF\n\n"
                "Команды:\n"
                "/status\n"
                "/signal\n"
                "/test\n"
                "/stats\n"
                "/help",
                chat_id
            )

        # ====================================================
        # /HELP
        # ====================================================

        elif text.startswith("/help"):

            send_message(
                "🥇 GOLD SMART — COMMANDS\n\n"
                "/start — запуск\n"
                "/status — состояние бота\n"
                "/signal — анализ XAU/USD\n"
                "/test — проверка Telegram\n"
                "/stats — статистика\n"
                "/help — команды",
                chat_id
            )

        # ====================================================
        # /TEST
        # ====================================================

        elif text.startswith("/test"):

            send_message(
                "🟢 TEST OK\n\n"
                "Telegram → Render → Bot\n"
                "Webhook работает.",
                chat_id
            )

        # ====================================================
        # /STATUS
        # ====================================================

        elif text.startswith("/status"):

            webhook = get_webhook_info()

            webhook_status = "UNKNOWN"

            if webhook:
                result = webhook.get(
                    "result",
                    {}
                )

                if result.get(
                    "url"
                ):
                    webhook_status = "READY"

            try:
                spot = get_spot_price()

                xaus_status = (
                    f"READY | {spot:.2f}"
                )

            except Exception:
                xaus_status = "ERROR"

            send_message(
                "🥇 GOLD SMART V5.9.4\n\n"
                "🟢 ENGINE: READY\n"
                f"🟢 XAUS: {xaus_status}\n"
                f"🟢 WEBHOOK: "
                f"{webhook_status}\n\n"
                "🛡 AUTO TRADING: OFF\n"
                f"📉 RISK: "
                f"{RISK_PERCENT:.1f}%\n"
                f"💰 DEPOSIT: "
                f"${DEPOSIT:.0f}\n\n"
                f"📊 SIGNALS: "
                f"{STATE['signal_total']}\n"
                f"🟢 FULL: "
                f"{STATE['full_signals']}\n"
                f"🟡 EARLY: "
                f"{STATE['early_signals']}",
                chat_id
            )

        # ====================================================
        # /STATS
        # ====================================================

        elif text.startswith("/stats"):

            total = STATE["signal_total"]
            wins = STATE["wins"]
            losses = STATE["losses"]

            closed = wins + losses

            if closed > 0:
                winrate = (
                    wins /
                    closed *
                    100
                )
            else:
                winrate = 0.0

            send_message(
                "📊 GOLD SMART STATS\n\n"
                f"📡 Signals: {total}\n"
                f"🟢 Full: "
                f"{STATE['full_signals']}\n"
                f"🟡 Early: "
                f"{STATE['early_signals']}\n\n"
                f"🏆 Wins: {wins}\n"
                f"❌ Losses: {losses}\n"
                f"📈 Winrate: "
                f"{winrate:.1f}%\n\n"
                f"🕒 Last signal: "
                f"{STATE['last_signal']}\n"
                f"💰 Last price: "
                f"{STATE['last_signal_price']:.2f}",
                chat_id
            )

        # ====================================================
        # /SIGNAL
        # ====================================================

        elif text.startswith("/signal"):

            threading.Thread(
                target=process_signal,
                args=(chat_id,),
                daemon=True
            ).start()

        # ====================================================
        # UNKNOWN
        # ====================================================

        else:

            send_message(
                "❓ Неизвестная команда.\n\n"
                "Используй /help",
                chat_id
            )

    except Exception as e:

        print(
            f"UPDATE ERROR: {e}"
        )


# ============================================================
# FLASK ROUTES
# ============================================================

@app.route("/", methods=["GET"])
def index():

    return jsonify(
        {
            "app": "GOLD SMART V5.9.4",
            "status": "READY",
            "auto_trading": False,
            "risk_percent": RISK_PERCENT,
            "data_source": "XAUS",
            "webhook": webhook_url(),
        }
    ), 200


@app.route("/health", methods=["GET"])
def health():

    return jsonify(
        {
            "status": "OK",
            "app": "GOLD SMART V5.9.4",
        }
    ), 200


@app.route("/telegram", methods=["POST"])
def telegram():

    print(
        "POST /telegram RECEIVED"
    )

    update = request.get_json(
        silent=True
    )

    if update:

        threading.Thread(
            target=process_update,
            args=(update,),
            daemon=True
        ).start()

    return jsonify(
        {
            "ok": True
        }
    ), 200


@app.route("/webhook-info", methods=["GET"])
def webhook_info_route():

    result = get_webhook_info()

    if result is None:
        return jsonify(
            {
                "ok": False
            }
        ), 500

    return jsonify(
        result
    ), 200


# ============================================================
# STARTUP
# ============================================================

def startup():

    print("=" * 60)
    print("🥇 GOLD SMART V5.9.4")
    print("=" * 60)

    print(
        "FLASK APP READY"
    )

    print(
        "AUTO TRADING: OFF"
    )

    print(
        "DATA: XAUS"
    )

    print(
        "TIMEFRAMES: H4 → H1 → M15 → M5"
    )

    print(
        f"WEBHOOK: {webhook_url()}"
    )

    print(
        f"DEPOSIT: ${DEPOSIT:.2f}"
    )

    print(
        f"RISK: {RISK_PERCENT:.1f}%"
    )

    print("=" * 60)

    threading.Thread(
        target=set_webhook,
        daemon=True
    ).start()


startup()


# ============================================================
# LOCAL
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "5000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
