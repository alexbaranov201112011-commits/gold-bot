import os
import json
import time
import threading
import requests
import pandas as pd
import numpy as np

from datetime import datetime, timezone
from flask import Flask, request


# =========================================================
# 🥇 GOLD SMART V5
# AUTO SIGNALS + SMART SMC + STATISTICS
#
# XAU/USD
# Telegram
# Twelve Data
#
# AUTO TRADING = OFF
# RISK = 1%
# =========================================================


# =========================================================
# ENV
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")

PORT = int(os.environ.get("PORT", "10000"))

TELEGRAM_API = (
    f"https://api.telegram.org/bot{BOT_TOKEN}"
    if BOT_TOKEN
    else ""
)

TWELVE_URL = "https://api.twelvedata.com"


# =========================================================
# FLASK APP
# =========================================================

app = Flask(__name__)


# =========================================================
# SETTINGS
# =========================================================

SYMBOL = "XAU/USD"

MIN_SCORE = 70
EARLY_SCORE = 55

DEFAULT_DEPOSIT = 800.0
RISK_PERCENT = 1.0

MIN_LOT = 0.01
MAX_LOT = 0.02

MIN_RR = 2.0
TARGET_RR = 3.0

BUY_RSI_MAX = 70
SELL_RSI_MIN = 30

BOS_LOOKBACK = 11
FVG_MEMORY = 5

STATS_FILE = "gold_stats.json"

CHECK_INTERVAL = 60


# =========================================================
# TELEGRAM
# =========================================================

def telegram_send(chat_id, text):
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN is not set")
        return False

    if not chat_id:
        print("ERROR: TELEGRAM_CHAT_ID is not set")
        return False

    try:
        response = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text
            },
            timeout=15
        )

        print(
            "Telegram:",
            response.status_code,
            response.text[:300]
        )

        return response.ok

    except Exception as e:
        print("Telegram error:", e)
        return False


# =========================================================
# TWELVE DATA
# =========================================================

def td_request(endpoint, params=None):

    if not TWELVE_DATA_API_KEY:
        print("ERROR: TWELVE_DATA_API_KEY is not set")
        return None

    params = params or {}
    params["apikey"] = TWELVE_DATA_API_KEY

    try:

        response = requests.get(
            f"{TWELVE_URL}/{endpoint}",
            params=params,
            timeout=20
        )

        data = response.json()

        if "status" in data and data["status"] == "error":
            print("Twelve Data error:", data)
            return None

        return data

    except Exception as e:
        print("Twelve Data request error:", e)
        return None


# =========================================================
# PRICE
# =========================================================

def get_price():

    data = td_request(
        "price",
        {
            "symbol": SYMBOL
        }
    )

    if not data:
        return None

    try:
        return float(data["price"])
    except:
        return None


# =========================================================
# CANDLES
# =========================================================

def get_candles(interval, outputsize=200):

    data = td_request(
        "time_series",
        {
            "symbol": SYMBOL,
            "interval": interval,
            "outputsize": outputsize,
            "format": "JSON"
        }
    )

    if not data:
        return None

    values = data.get("values")

    if not values:
        return None

    df = pd.DataFrame(values)

    required = [
        "datetime",
        "open",
        "high",
        "low",
        "close"
    ]

    for col in required:

        if col not in df.columns:
            return None

    for col in [
        "open",
        "high",
        "low",
        "close"
    ]:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce"
        )

    df["datetime"] = pd.to_datetime(
        df["datetime"],
        errors="coerce"
    )

    df = df.dropna(
        subset=[
            "datetime",
            "open",
            "high",
            "low",
            "close"
        ]
    )

    df = df.sort_values(
        "datetime"
    ).reset_index(drop=True)

    return df


# =========================================================
# CLOSED CANDLES
# =========================================================

def closed_candles(df):

    if df is None or len(df) < 3:
        return None

    # Twelve Data normally returns the newest candle first
    # after sorting, the last candle can be the current candle.
    # We remove the newest candle so analysis works only
    # with completed candles.

    return df.iloc[:-1].copy().reset_index(drop=True)


# =========================================================
# INDICATORS
# =========================================================

def add_indicators(df):

    df = df.copy()

    df["EMA50"] = (
        df["close"]
        .ewm(span=50, adjust=False)
        .mean()
    )

    df["EMA200"] = (
        df["close"]
        .ewm(span=200, adjust=False)
        .mean()
    )

    delta = df["close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    df["RSI"] = 100 - (
        100 / (1 + rs)
    )

    previous_close = df["close"].shift(1)

    tr1 = df["high"] - df["low"]
    tr2 = (
        df["high"] - previous_close
    ).abs()
    tr3 = (
        df["low"] - previous_close
    ).abs()

    true_range = pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)

    df["ATR"] = (
        true_range
        .rolling(14)
        .mean()
    )

    return df


# =========================================================
# TREND
# =========================================================

def get_trend(df):

    last = df.iloc[-1]

    close = last["close"]
    ema50 = last["EMA50"]
    ema200 = last["EMA200"]

    if close > ema50 > ema200:
        return "BULLISH"

    if close < ema50 < ema200:
        return "BEARISH"

    return "MIXED"


# =========================================================
# STRUCTURE
# =========================================================

def get_structure(df, lookback=10):

    if len(df) < lookback + 2:
        return "MIXED"

    recent = df.tail(lookback)

    highs = recent["high"].values
    lows = recent["low"].values

    higher_high = highs[-1] > highs[-2]
    higher_low = lows[-1] > lows[-2]

    lower_high = highs[-1] < highs[-2]
    lower_low = lows[-1] < lows[-2]

    if higher_high and higher_low:
        return "BULLISH"

    if lower_high and lower_low:
        return "BEARISH"

    return "MIXED"


# =========================================================
# LIQUIDITY SWEEP
# =========================================================

def get_liquidity(df):

    if len(df) < 5:
        return "NONE"

    last = df.iloc[-1]

    previous_high = df["high"].iloc[-5:-1].max()
    previous_low = df["low"].iloc[-5:-1].min()

    # BSL sweep
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


# =========================================================
# FVG
# =========================================================

def get_fvg(df):

    if len(df) < 3:
        return "NONE"

    recent = df.tail(
        FVG_MEMORY + 2
    ).reset_index(drop=True)

    result = "NONE"

    for i in range(2, len(recent)):

        a = recent.iloc[i - 2]
        c = recent.iloc[i]

        # Bullish FVG
        if a["high"] < c["low"]:
            result = "BULLISH FVG"

        # Bearish FVG
        elif a["low"] > c["high"]:
            result = "BEARISH FVG"

    return result


# =========================================================
# BOS
# =========================================================

def get_bos(df):

    if len(df) < BOS_LOOKBACK + 2:
        return "NONE"

    last = df.iloc[-1]

    previous = df.iloc[
        -BOS_LOOKBACK - 1:-1
    ]

    previous_high = previous["high"].max()
    previous_low = previous["low"].min()

    if last["close"] > previous_high:
        return "BULLISH BOS"

    if last["close"] < previous_low:
        return "BEARISH BOS"

    return "NONE"


# =========================================================
# DISPLACEMENT
# =========================================================

def get_displacement(df):

    last = df.iloc[-1]

    atr = last["ATR"]

    if pd.isna(atr) or atr <= 0:
        return "NONE"

    body = abs(
        last["close"] - last["open"]
    )

    if body < atr * 0.70:
        return "NONE"

    if last["close"] > last["open"]:
        return "BULLISH"

    if last["close"] < last["open"]:
        return "BEARISH"

    return "NONE"


# =========================================================
# MOMENTUM
# =========================================================

def get_momentum(df):

    if len(df) < 6:
        return "NEUTRAL"

    current = df["close"].iloc[-1]
    previous = df["close"].iloc[-6]

    if current > previous:
        return "BULLISH"

    if current < previous:
        return "BEARISH"

    return "NEUTRAL"


# =========================================================
# PREMIUM / DISCOUNT
# =========================================================

def get_zone(df):

    recent = df.tail(50)

    high = recent["high"].max()
    low = recent["low"].min()

    current = recent["close"].iloc[-1]

    midpoint = (
        high + low
    ) / 2

    if current > midpoint:
        return "PREMIUM"

    if current < midpoint:
        return "DISCOUNT"

    return "EQUILIBRIUM"


# =========================================================
# LOT CALCULATION
# =========================================================

def calculate_lot(
    entry,
    stop,
    deposit=DEFAULT_DEPOSIT
):

    risk_money = (
        deposit *
        RISK_PERCENT /
        100
    )

    stop_distance = abs(
        entry - stop
    )

    if stop_distance <= 0:
        return MIN_LOT

    # Approximation for XAUUSD
    value_per_price_unit = 100.0

    lot = (
        risk_money /
        (
            stop_distance *
            value_per_price_unit
        )
    )

    lot = max(
        MIN_LOT,
        min(MAX_LOT, lot)
    )

    return round(lot, 2)


# =========================================================
# SCORE
# =========================================================

def calculate_score(
    h4_trend,
    h1_trend,
    m15_structure,
    liquidity,
    fvg,
    bos,
    displacement,
    momentum,
    zone,
    direction,
    rr
):

    score = 0

    # H4
    if direction == "BUY" and h4_trend == "BULLISH":
        score += 15

    elif direction == "SELL" and h4_trend == "BEARISH":
        score += 15

    # H1
    if direction == "BUY" and h1_trend == "BULLISH":
        score += 15

    elif direction == "SELL" and h1_trend == "BEARISH":
        score += 15

    # Liquidity
    if direction == "BUY" and liquidity == "SSL SWEEP":
        score += 15

    elif direction == "SELL" and liquidity == "BSL SWEEP":
        score += 15

    # BOS
    if direction == "BUY" and bos == "BULLISH BOS":
        score += 15

    elif direction == "SELL" and bos == "BEARISH BOS":
        score += 15

    # FVG
    if direction == "BUY" and fvg == "BULLISH FVG":
        score += 10

    elif direction == "SELL" and fvg == "BEARISH FVG":
        score += 10

    # Displacement
    if direction == "BUY" and displacement == "BULLISH":
        score += 10

    elif direction == "SELL" and displacement == "BEARISH":
        score += 10

    # Structure
    if direction == "BUY" and m15_structure == "BULLISH":
        score += 5

    elif direction == "SELL" and m15_structure == "BEARISH":
        score += 5

    # Momentum
    if direction == "BUY" and momentum == "BULLISH":
        score += 5

    elif direction == "SELL" and momentum == "BEARISH":
        score += 5

    # Zone
    if direction == "BUY" and zone == "DISCOUNT":
        score += 5

    elif direction == "SELL" and zone == "PREMIUM":
        score += 5

    # RR
    if rr >= 3:
        score += 5

    elif rr >= 2:
        score += 3

    # SMC confluence bonuses
    if direction == "BUY":

        if (
            liquidity == "SSL SWEEP"
            and fvg == "BULLISH FVG"
        ):
            score += 10

        if (
            liquidity == "SSL SWEEP"
            and displacement == "BULLISH"
        ):
            score += 5

    elif direction == "SELL":

        if (
            liquidity == "BSL SWEEP"
            and fvg == "BEARISH FVG"
        ):
            score += 10

        if (
            liquidity == "BSL SWEEP"
            and displacement == "BEARISH"
        ):
            score += 5

    return min(
        score,
        100
    )


# =========================================================
# ANALYSIS
# =========================================================

def analyze():

    h4 = get_candles(
        "4h",
        250
    )

    h1 = get_candles(
        "1h",
        250
    )

    m15 = get_candles(
        "15min",
        250
    )

    if (
        h4 is None
        or h1 is None
        or m15 is None
    ):
        return {
            "signal": "WAIT",
            "reason": "Market data unavailable"
        }

    h4 = closed_candles(h4)
    h1 = closed_candles(h1)
    m15 = closed_candles(m15)

    if (
        h4 is None
        or h1 is None
        or m15 is None
    ):
        return {
            "signal": "WAIT",
            "reason": "Not enough closed candles"
        }

    h4 = add_indicators(h4)
    h1 = add_indicators(h1)
    m15 = add_indicators(m15)

    if len(m15) < 50:
        return {
            "signal": "WAIT",
            "reason": "Not enough M15 data"
        }

    price = get_price()

    if price is None:
        price = float(
            m15["close"].iloc[-1]
        )

    h4_trend = get_trend(h4)
    h1_trend = get_trend(h1)

    m15_structure = get_structure(
        m15
    )

    liquidity = get_liquidity(
        m15
    )

    fvg = get_fvg(
        m15
    )

    bos = get_bos(
        m15
    )

    displacement = get_displacement(
        m15
    )

    momentum = get_momentum(
        m15
    )

    zone = get_zone(
        m15
    )

    rsi = float(
        m15["RSI"].iloc[-1]
    )

    atr = float(
        m15["ATR"].iloc[-1]
    )

    if np.isnan(atr) or atr <= 0:
        atr = 3.0

    # =====================================================
    # BUY / SELL CANDIDATES
    # =====================================================

    buy_candidate = (
        h4_trend == "BULLISH"
        or h1_trend == "BULLISH"
        or liquidity == "SSL SWEEP"
        or bos == "BULLISH BOS"
        or fvg == "BULLISH FVG"
    )

    sell_candidate = (
        h4_trend == "BEARISH"
        or h1_trend == "BEARISH"
        or liquidity == "BSL SWEEP"
        or bos == "BEARISH BOS"
        or fvg == "BEARISH FVG"
    )

    # =====================================================
    # BUY SETUP
    # =====================================================

    buy_sl = price - (
        atr * 1.5
    )

    buy_risk = abs(
        price - buy_sl
    )

    buy_tp1 = price + (
        buy_risk * 2
    )

    buy_tp2 = price + (
        buy_risk * 3
    )

    buy_rr = 3.0

    buy_score = calculate_score(
        h4_trend,
        h1_trend,
        m15_structure,
        liquidity,
        fvg,
        bos,
        displacement,
        momentum,
        zone,
        "BUY",
        buy_rr
    )

    # =====================================================
    # SELL SETUP
    # =====================================================

    sell_sl = price + (
        atr * 1.5
    )

    sell_risk = abs(
        price - sell_sl
    )

    sell_tp1 = price - (
        sell_risk * 2
    )

    sell_tp2 = price - (
        sell_risk * 3
    )

    sell_rr = 3.0

    sell_score = calculate_score(
        h4_trend,
        h1_trend,
        m15_structure,
        liquidity,
        fvg,
        bos,
        displacement,
        momentum,
        zone,
        "SELL",
        sell_rr
    )

    # =====================================================
    # CONFIRMATION
    # =====================================================

    full_buy = (
        buy_candidate
        and buy_score >= MIN_SCORE
        and rsi < BUY_RSI_MAX
        and buy_rr >= MIN_RR
        and (
            bos == "BULLISH BOS"
            or (
                liquidity == "SSL SWEEP"
                and fvg == "BULLISH FVG"
                and displacement == "BULLISH"
            )
        )
    )

    full_sell = (
        sell_candidate
        and sell_score >= MIN_SCORE
        and rsi > SELL_RSI_MIN
        and sell_rr >= MIN_RR
        and (
            bos == "BEARISH BOS"
            or (
                liquidity == "BSL SWEEP"
                and fvg == "BEARISH FVG"
                and displacement == "BEARISH"
            )
        )
    )

    early_buy = (
        buy_candidate
        and buy_score >= EARLY_SCORE
        and rsi < BUY_RSI_MAX
        and buy_rr >= MIN_RR
        and (
            liquidity == "SSL SWEEP"
            or bos == "BULLISH BOS"
            or fvg == "BULLISH FVG"
            or displacement == "BULLISH"
        )
    )

    early_sell = (
        sell_candidate
        and sell_score >= EARLY_SCORE
        and rsi > SELL_RSI_MIN
        and sell_rr >= MIN_RR
        and (
            liquidity == "BSL SWEEP"
            or bos == "BEARISH BOS"
            or fvg == "BEARISH FVG"
            or displacement == "BEARISH"
        )
    )

    # =====================================================
    # FINAL SIGNAL
    # =====================================================

    signal = "WAIT"

    if full_buy and buy_score >= sell_score:
        signal = "BUY"

    elif full_sell and sell_score > buy_score:
        signal = "SELL"

    elif early_buy and buy_score >= sell_score:
        signal = "EARLY BUY"

    elif early_sell and sell_score > buy_score:
        signal = "EARLY SELL"

    # =====================================================
    # SELECT SETUP
    # =====================================================

    if signal in ["BUY", "EARLY BUY"]:

        direction = "BUY"
        score = buy_score
        entry = price
        sl = buy_sl
        tp1 = buy_tp1
        tp2 = buy_tp2
        rr = buy_rr

    elif signal in ["SELL", "EARLY SELL"]:

        direction = "SELL"
        score = sell_score
        entry = price
        sl = sell_sl
        tp1 = sell_tp1
        tp2 = sell_tp2
        rr = sell_rr

    else:

        direction = "NONE"
        score = max(
            buy_score,
            sell_score
        )

        entry = price
        sl = 0
        tp1 = 0
        tp2 = 0
        rr = 0

    lot = calculate_lot(
        entry,
        sl
    ) if direction != "NONE" else 0.01

    if score >= 85:
        grade = "A+"

    elif score >= 70:
        grade = "A"

    elif score >= 55:
        grade = "B"

    else:
        grade = "WAIT"

    return {
        "signal": signal,
        "direction": direction,
        "price": price,

        "h4_trend": h4_trend,
        "h1_trend": h1_trend,
        "m15_structure": m15_structure,

        "liquidity": liquidity,
        "fvg": fvg,
        "bos": bos,
        "displacement": displacement,
        "momentum": momentum,
        "zone": zone,

        "rsi": rsi,

        "buy_score": buy_score,
        "sell_score": sell_score,
        "score": score,

        "grade": grade,

        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,

        "rr": rr,
        "lot": lot,

        "candle_time": str(
            m15["datetime"].iloc[-1]
        )
    }


# =========================================================
# FORMAT SIGNAL
# =========================================================

def format_signal(a):

    signal = a["signal"]

    if signal in [
        "BUY",
        "EARLY BUY"
    ]:

        emoji = "🟢"

    elif signal in [
        "SELL",
        "EARLY SELL"
    ]:

        emoji = "🔴"

    else:

        emoji = "⚪"

    return f"""
🥇 GOLD SMART V5

{emoji} SIGNAL: {signal}

💰 XAUUSD: {a["price"]:.2f}

📊 H4: {a["h4_trend"]}
📊 H1: {a["h1_trend"]}
📊 M15: {a["m15_structure"]}

💧 Liquidity: {a["liquidity"]}
🧩 FVG: {a["fvg"]}
🔨 BOS: {a["bos"]}
💥 Displacement: {a["displacement"]}
📈 Momentum: {a["momentum"]}
📐 Zone: {a["zone"]}

RSI: {a["rsi"]:.1f}

🟢 BUY SCORE: {a["buy_score"]}/100
🔴 SELL SCORE: {a["sell_score"]}/100

⭐ QUALITY: {a["grade"]}
📊 SCORE: {a["score"]}/100

━━━━━━━━━━━━━━

🎯 ENTRY: {a["entry"]:.2f}
🛑 SL: {a["sl"]:.2f}

💰 TP1: {a["tp1"]:.2f}
💰 TP2: {a["tp2"]:.2f}

📐 RR: 1:{a["rr"]:.1f}

💵 Risk: {RISK_PERCENT:.1f}%
📦 Lot: {a["lot"]:.2f}

🚫 AUTO TRADING: OFF

🕯 Candle:
{a["candle_time"]}
"""


# =========================================================
# STATISTICS STORAGE
# =========================================================

stats_lock = threading.Lock()


def load_stats():

    if not os.path.exists(
        STATS_FILE
    ):
        return []

    try:

        with open(
            STATS_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

            if isinstance(data, list):
                return data

    except Exception as e:

        print(
            "Stats load error:",
            e
        )

    return []


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
            e
        )


# =========================================================
# REGISTER SIGNAL
# =========================================================

def register_signal(a):

    if a["signal"] not in [
        "BUY",
        "SELL",
        "EARLY BUY",
        "EARLY SELL"
    ]:
        return

    signal = {
        "id": (
            str(a["candle_time"])
            + "_"
            + a["signal"]
        ),

        "created_at": datetime.now(
            timezone.utc
        ).isoformat(),

        "candle_time": a["candle_time"],

        "signal": a["signal"],

        "direction": a["direction"],

        "entry": round(
            a["entry"],
            2
        ),

        "sl": round(
            a["sl"],
            2
        ),

        "tp1": round(
            a["tp1"],
            2
        ),

        "tp2": round(
            a["tp2"],
            2
        ),

        "score": a["score"],

        "grade": a["grade"],

        "status": "OPEN",

        "tp1_hit": False,

        "result_price": None,

        "closed_at": None
    }

    with stats_lock:

        data = load_stats()

        # Prevent duplicate
        if any(
            x.get("id") == signal["id"]
            for x in data
        ):
            return

        data.append(signal)

        # Keep last 500 signals
        data = data[-500:]

        save_stats(data)

    print(
        "STAT REGISTERED:",
        signal["id"]
    )


# =========================================================
# UPDATE STATISTICS
# =========================================================

def update_statistics():

    with stats_lock:

        data = load_stats()

        if not data:
            return

    price = get_price()

    if price is None:
        return

    changed = False

    for item in data:

        if item.get("status") not in [
            "OPEN",
            "TP1"
        ]:
            continue

        direction = item.get(
            "direction"
        )

        entry = float(
            item["entry"]
        )

        sl = float(
            item["sl"]
        )

        tp1 = float(
            item["tp1"]
        )

        tp2 = float(
            item["tp2"]
        )

        status = item.get(
            "status",
            "OPEN"
        )

        # =================================================
        # BUY
        # =================================================

        if direction == "BUY":

            # TP1 reached
            if (
                not item.get("tp1_hit", False)
                and price >= tp1
            ):

                item["tp1_hit"] = True
                item["status"] = "TP1"

                changed = True

            # After TP1 we consider SL moved to BE
            if (
                item.get("tp1_hit", False)
                and price <= entry
            ):

                item["status"] = "BE"
                item["result_price"] = price
                item["closed_at"] = datetime.now(
                    timezone.utc
                ).isoformat()

                changed = True

            elif price >= tp2:

                item["status"] = "WIN"
                item["result_price"] = price
                item["closed_at"] = datetime.now(
                    timezone.utc
                ).isoformat()

                changed = True

            elif (
                not item.get("tp1_hit", False)
                and price <= sl
            ):

                item["status"] = "LOSS"
                item["result_price"] = price
                item["closed_at"] = datetime.now(
                    timezone.utc
                ).isoformat()

                changed = True

        # =================================================
        # SELL
        # =================================================

        elif direction == "SELL":

            # TP1 reached
            if (
                not item.get("tp1_hit", False)
                and price <= tp1
            ):

                item["tp1_hit"] = True
                item["status"] = "TP1"

                changed = True

            # After TP1 -> virtual BE
            if (
                item.get("tp1_hit", False)
                and price >= entry
            ):

                item["status"] = "BE"
                item["result_price"] = price
                item["closed_at"] = datetime.now(
                    timezone.utc
                ).isoformat()

                changed = True

            elif price <= tp2:

                item["status"] = "WIN"
                item["result_price"] = price
                item["closed_at"] = datetime.now(
                    timezone.utc
                ).isoformat()

                changed = True

            elif (
                not item.get("tp1_hit", False)
                and price >= sl
            ):

                item["status"] = "LOSS"
                item["result_price"] = price
                item["closed_at"] = datetime.now(
                    timezone.utc
                ).isoformat()

                changed = True

    if changed:

        with stats_lock:
            save_stats(data)


# =========================================================
# STATS TEXT
# =========================================================

def get_stats_text():

    with stats_lock:

        data = load_stats()

    total = len(data)

    wins = sum(
        1 for x in data
        if x.get("status") == "WIN"
    )

    losses = sum(
        1 for x in data
        if x.get("status") == "LOSS"
    )

    be = sum(
        1 for x in data
        if x.get("status") == "BE"
    )

    open_trades = sum(
        1 for x in data
        if x.get("status") in [
            "OPEN",
            "TP1"
        ]
    )

    closed = wins + losses + be

    if closed > 0:
        winrate = (
            wins / closed
        ) * 100
    else:
        winrate = 0

    full = [
        x for x in data
        if x.get("signal") in [
            "BUY",
            "SELL"
        ]
    ]

    early = [
        x for x in data
        if x.get("signal") in [
            "EARLY BUY",
            "EARLY SELL"
        ]
    ]

    full_closed = [
        x for x in full
        if x.get("status") in [
            "WIN",
            "LOSS",
            "BE"
        ]
    ]

    early_closed = [
        x for x in early
        if x.get("status") in [
            "WIN",
            "LOSS",
            "BE"
        ]
    ]

    full_wins = sum(
        1 for x in full_closed
        if x.get("status") == "WIN"
    )

    early_wins = sum(
        1 for x in early_closed
        if x.get("status") == "WIN"
    )

    full_wr = (
        full_wins /
        len(full_closed) *
        100
        if full_closed
        else 0
    )

    early_wr = (
        early_wins /
        len(early_closed) *
        100
        if early_closed
        else 0
    )

    return f"""
📊 GOLD SMART V5 — STATISTICS

📌 TOTAL SIGNALS: {total}

🟢 WIN: {wins}
🔴 LOSS: {losses}
⚪ BE: {be}
🟡 OPEN: {open_trades}

━━━━━━━━━━━━━━

🎯 CLOSED: {closed}

📈 WIN RATE:
{winrate:.1f}%

━━━━━━━━━━━━━━

🔥 FULL SIGNALS:
{len(full_closed)} closed
WIN RATE: {full_wr:.1f}%

🟡 EARLY SIGNALS:
{len(early_closed)} closed
WIN RATE: {early_wr:.1f}%

━━━━━━━━━━━━━━

⚠️ Статистика виртуальная.
Автоторговля выключена.
"""


# =========================================================
# TELEGRAM COMMANDS
# =========================================================

@app.route(
    "/telegram",
    methods=["POST"]
)
def telegram_webhook():

    try:

        update = request.get_json(
            silent=True
        ) or {}

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

        text = message.get(
            "text",
            ""
        )

        if not chat_id:
            return "OK"

        if text.startswith("/start"):

            telegram_send(
                chat_id,
                """
🥇 GOLD SMART V5

Бот запущен.

Команды:

🟡 /gold
Анализ XAUUSD

📊 /stats
Статистика сигналов

🧠 SMART SMC
FULL ≥ 70
EARLY ≥ 55

💰 Risk: 1%

🤖 Автосигналы: ON
🚫 Автоторговля: OFF
"""
            )

        elif text.startswith("/gold"):

            analysis = analyze()

            telegram_send(
                chat_id,
                format_signal(
                    analysis
                )
            )

        elif text.startswith("/stats"):

            telegram_send(
                chat_id,
                get_stats_text()
            )

        return "OK"

    except Exception as e:

        print(
            "Webhook error:",
            e
        )

        return "OK"


# =========================================================
# HEALTH
# =========================================================

@app.route("/")
def home():

    return (
        "🥇 GOLD SMART V5 ONLINE"
    )


@app.route("/health")
def health():

    return {
        "status": "ok",
        "bot": "GOLD SMART V5",
        "auto_trading": False,
        "risk_percent": RISK_PERCENT
    }


# =========================================================
# WEBHOOK SETUP
# =========================================================

def setup_webhook():

    if not BOT_TOKEN:
        print(
            "ERROR: BOT_TOKEN is not set"
        )
        return

    render_url = os.environ.get(
        "RENDER_EXTERNAL_URL"
    )

    if not render_url:
        print(
            "RENDER_EXTERNAL_URL not found"
        )
        return

    webhook_url = (
        render_url.rstrip("/")
        + "/telegram"
    )

    try:

        response = requests.post(
            f"{TELEGRAM_API}/setWebhook",
            json={
                "url": webhook_url
            },
            timeout=15
        )

        print(
            "Webhook:",
            response.text
        )

    except Exception as e:

        print(
            "Webhook error:",
            e
        )


# =========================================================
# AUTO SIGNAL ENGINE
# =========================================================

def auto_send_signal():

    last_candle_time = None
    last_signal = None

    print(
        "AUTO SIGNAL ENGINE STARTED"
    )

    while True:

        try:

            if not TELEGRAM_CHAT_ID:

                print(
                    "ERROR: TELEGRAM_CHAT_ID is not set"
                )

                time.sleep(
                    CHECK_INTERVAL
                )

                continue

            m15 = get_candles(
                "15min",
                100
            )

            if (
                m15 is None
                or len(m15) < 10
            ):

                time.sleep(
                    CHECK_INTERVAL
                )

                continue

            closed = closed_candles(
                m15
            )

            if (
                closed is None
                or len(closed) < 2
            ):

                time.sleep(
                    CHECK_INTERVAL
                )

                continue

            current_candle_time = str(
                closed[
                    "datetime"
                ].iloc[-1]
            )

            # =================================================
            # NEW CLOSED M15 CANDLE
            # =================================================

            if (
                current_candle_time
                != last_candle_time
            ):

                last_candle_time = (
                    current_candle_time
                )

                print(
                    "NEW M15 CANDLE:",
                    current_candle_time
                )

                analysis = analyze()

                signal = analysis.get(
                    "signal"
                )

                print(
                    "AUTO:",
                    signal,
                    "| SCORE:",
                    analysis.get("score")
                )

                # =================================================
                # SEND SIGNAL
                # =================================================

                if signal in [
                    "BUY",
                    "SELL",
                    "EARLY BUY",
                    "EARLY SELL"
                ]:

                    signal_key = (
                        current_candle_time,
                        signal
                    )

                    if (
                        signal_key
                        != last_signal
                    ):

                        message = (
                            format_signal(
                                analysis
                            )
                        )

                        sent = telegram_send(
                            TELEGRAM_CHAT_ID,
                            message
                        )

                        if sent:

                            last_signal = (
                                signal_key
                            )

                            register_signal(
                                analysis
                            )

                            print(
                                "AUTO SIGNAL SENT:",
                                signal
                            )

            # =================================================
            # UPDATE EXISTING STATS
            # =================================================

            update_statistics()

        except Exception as e:

            print(
                "Auto signal error:",
                e
            )

        time.sleep(
            CHECK_INTERVAL
        )


# =========================================================
# START BACKGROUND THREADS
# =========================================================

def start_background_threads():

    # Webhook
    threading.Thread(
        target=setup_webhook,
        daemon=True
    ).start()

    # Auto signals
    threading.Thread(
        target=auto_send_signal,
        daemon=True
    ).start()


# =========================================================
# START
# =========================================================

start_background_threads()


# =========================================================
# LOCAL RUN
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
    )
