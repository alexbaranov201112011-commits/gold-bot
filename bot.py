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
# 🥇 GOLD SMART V5.9
# XAU/USD SMART SMC SIGNAL ENGINE
#
# DATA: XAUS.com
# TELEGRAM
# AUTO TRADING = OFF
# RISK = 1%
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SPOT_URL = "https://xaus.com/api/v1/spot"
INTRADAY_URL = "https://xaus.com/api/v1/intraday"

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

STATS_FILE = "gold_stats.json"


app = Flask(__name__)


# ============================================================
# GLOBAL STATE
# ============================================================

price_cache = {
    "value": None,
    "ts": 0.0
}

intraday_cache = {}

virtual_trade = None

last_signal_time = 0.0

stats = None

_engine_started = False


# ============================================================
# TELEGRAM
# ============================================================

def telegram_url(method):
    return "https://api.telegram.org/bot{}/{}".format(
        BOT_TOKEN,
        method
    )


def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        return False

    try:
        response = requests.post(
            telegram_url("sendMessage"),
            json={
                "chat_id": CHAT_ID,
                "text": text
            },
            timeout=15
        )

        return response.ok

    except Exception as exc:
        print("Telegram error:", exc, flush=True)
        return False


# ============================================================
# STATISTICS
# ============================================================

def default_stats():
    return {
        "total_signals": 0,
        "full_signals": 0,
        "early_signals": 0,

        "win": 0,
        "loss": 0,
        "be": 0,
        "open": 0,

        "early_win": 0,
        "early_loss": 0,

        "last_signal": None
    }


def load_stats():
    if not os.path.exists(STATS_FILE):
        return default_stats()

    try:
        with open(
            STATS_FILE,
            "r",
            encoding="utf-8"
        ) as file:
            data = json.load(file)

        base = default_stats()
        base.update(data)

        return base

    except Exception as exc:
        print("Stats load error:", exc, flush=True)
        return default_stats()


def save_stats(data):
    try:
        with open(
            STATS_FILE,
            "w",
            encoding="utf-8"
        ) as file:
            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2
            )

    except Exception as exc:
        print("Stats save error:", exc, flush=True)


stats = load_stats()


# ============================================================
# XAUS SPOT
# ============================================================

def fetch_spot():

    now = time.time()

    if (
        price_cache["value"] is not None
        and now - price_cache["ts"] < CACHE_SECONDS
    ):
        return float(price_cache["value"])

    response = requests.get(
        SPOT_URL,
        timeout=15
    )

    response.raise_for_status()

    data = response.json()

    price = None

    if isinstance(data, dict):

        candidates = [data]

        for key in (
            "data",
            "xau",
            "gold",
            "result"
        ):

            value = data.get(key)

            if isinstance(value, dict):
                candidates.append(value)

        for item in candidates:

            for key in (
                "price",
                "spot_usd_oz",
                "close",
                "last"
            ):

                value = item.get(key)

                if value is not None:

                    try:
                        price = float(value)
                        break

                    except (
                        TypeError,
                        ValueError
                    ):
                        pass

            if price is not None:
                break

    if price is None:
        raise ValueError(
            "XAUS spot response does not contain a price"
        )

    price_cache["value"] = price
    price_cache["ts"] = now

    return price


# ============================================================
# XAUS INTRADAY PARSER
# ============================================================

def _parse_point(point):

    if not isinstance(point, dict):
        return None

    time_value = None

    for key in (
        "t",
        "timestamp",
        "time",
        "date",
        "datetime"
    ):

        if point.get(key) is not None:
            time_value = point.get(key)
            break

    close_value = None

    for key in (
        "p",
        "price",
        "close",
        "value"
    ):

        if point.get(key) is not None:
            close_value = point.get(key)
            break

    if time_value is None or close_value is None:
        return None

    try:

        close_value = float(close_value)

        if isinstance(
            time_value,
            (int, float)
        ):

            unit = (
                "ms"
                if float(time_value) > 10_000_000_000
                else "s"
            )

            dt = pd.to_datetime(
                time_value,
                unit=unit,
                utc=True
            )

        else:

            dt = pd.to_datetime(
                time_value,
                utc=True
            )

        return dt, close_value

    except Exception:
        return None


# ============================================================
# XAUS INTRADAY
# ============================================================

def fetch_intraday(minutes=1):

    cache_key = str(minutes)

    now = time.time()

    cached = intraday_cache.get(cache_key)

    if (
        cached
        and now - cached["ts"] < CACHE_SECONDS
    ):

        return cached["data"].copy()

    params = {
        "symbol": "XAUUSD",
        "interval": minutes
    }

    response = requests.get(
        INTRADAY_URL,
        params=params,
        timeout=20
    )

    response.raise_for_status()

    payload = response.json()

    points = None

    if isinstance(payload, list):

        points = payload

    elif isinstance(payload, dict):

        for key in (
            "points",
            "data",
            "series",
            "prices",
            "result"
        ):

            value = payload.get(key)

            if isinstance(value, list):
                points = value
                break

    if not points:
        raise ValueError(
            "XAUS intraday response contains no points"
        )

    rows = []

    for point in points:

        parsed = _parse_point(point)

        if parsed:
            rows.append(parsed)

    if len(rows) < 30:

        raise ValueError(
            "Not enough intraday data: {} points".format(
                len(rows)
            )
        )

    frame = pd.DataFrame(
        rows,
        columns=[
            "time",
            "close"
        ]
    )

    frame = frame.drop_duplicates(
        subset=["time"]
    )

    frame = frame.sort_values(
        "time"
    )

    frame = frame.set_index(
        "time"
    )

    frame["open"] = frame["close"].shift(1)

    frame["high"] = frame[
        ["open", "close"]
    ].max(axis=1)

    frame["low"] = frame[
        ["open", "close"]
    ].min(axis=1)

    frame = frame[
        [
            "open",
            "high",
            "low",
            "close"
        ]
    ].dropna()

    intraday_cache[cache_key] = {
        "data": frame.copy(),
        "ts": now
    }

    return frame


# ============================================================
# RESAMPLE
# ============================================================

def resample_ohlc(frame, rule):

    out = frame.resample(rule).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last"
        }
    ).dropna()

    if CLOSED_CANDLES and len(out) > 1:
        out = out.iloc[:-1]

    return out


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
    ).ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    loss = (
        -delta.clip(
            upper=0
        )
    ).ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    rs = gain / loss.replace(
        0,
        np.nan
    )

    value = 100 - (
        100 / (1 + rs)
    )

    return value.fillna(50)


def add_indicators(frame):

    df = frame.copy()

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

    df["range"] = (
        df["high"] -
        df["low"]
    )

    df["body"] = (
        df["close"] -
        df["open"]
    ).abs()

    df["avg_range"] = (
        df["range"]
        .rolling(20)
        .mean()
    )

    return df


# ============================================================
# MARKET STRUCTURE
# ============================================================

def structure_direction(
    df,
    lookback=30
):

    if len(df) < lookback:
        return "mixed"

    recent = df.tail(
        lookback
    )

    highs = (
        recent["high"]
        .rolling(
            3,
            center=True
        )
        .max()
    )

    lows = (
        recent["low"]
        .rolling(
            3,
            center=True
        )
        .min()
    )

    swing_highs = recent["high"][
        recent["high"] == highs
    ].dropna()

    swing_lows = recent["low"][
        recent["low"] == lows
    ].dropna()

    if (
        len(swing_highs) < 2
        or len(swing_lows) < 2
    ):
        return "mixed"

    hh = (
        swing_highs.iloc[-1]
        >
        swing_highs.iloc[-2]
    )

    hl = (
        swing_lows.iloc[-1]
        >
        swing_lows.iloc[-2]
    )

    lh = (
        swing_highs.iloc[-1]
        <
        swing_highs.iloc[-2]
    )

    ll = (
        swing_lows.iloc[-1]
        <
        swing_lows.iloc[-2]
    )

    if hh and hl:
        return "bullish"

    if lh and ll:
        return "bearish"

    return "mixed"


# ============================================================
# MOMENTUM
# ============================================================

def momentum_direction(df):

    if len(df) < 5:
        return "mixed"

    last = df.iloc[-1]
    prev = df.iloc[-4]

    if (
        last["close"] > prev["close"]
        and
        last["ema20"] > prev["ema20"]
    ):
        return "bullish"

    if (
        last["close"] < prev["close"]
        and
        last["ema20"] < prev["ema20"]
    ):
        return "bearish"

    return "mixed"


# ============================================================
# BOS
# ============================================================

def detect_bos(df):

    if len(df) < 10:
        return "none"

    recent = df.iloc[-2]

    prior = df.iloc[-7:-2]

    high = prior["high"].max()
    low = prior["low"].min()

    if recent["close"] > high:
        return "bullish"

    if recent["close"] < low:
        return "bearish"

    return "none"


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def detect_liquidity(df):

    if len(df) < 10:
        return "none"

    last = df.iloc[-1]

    prior = df.iloc[-7:-1]

    prior_high = prior["high"].max()
    prior_low = prior["low"].min()

    # Buy-side liquidity sweep
    if (
        last["high"] > prior_high
        and
        last["close"] < prior_high
    ):
        return "bsl_sweep"

    # Sell-side liquidity sweep
    if (
        last["low"] < prior_low
        and
        last["close"] > prior_low
    ):
        return "ssl_sweep"

    return "none"


# ============================================================
# FVG
# ============================================================

def detect_fvg(df):

    if len(df) < 3:
        return "none"

    a = df.iloc[-3]
    c = df.iloc[-1]

    if c["low"] > a["high"]:
        return "bullish"

    if c["high"] < a["low"]:
        return "bearish"

    return "none"


# ============================================================
# DISPLACEMENT
# ============================================================

def detect_displacement(df):

    if len(df) < 21:
        return "none"

    last = df.iloc[-1]

    avg = (
        df["range"]
        .iloc[-21:-1]
        .mean()
    )

    if (
        not np.isfinite(avg)
        or avg <= 0
    ):
        return "none"

    if (
        last["range"] >= avg * 1.5
        and
        last["body"] >= last["range"] * 0.6
    ):

        if last["close"] > last["open"]:
            return "bullish"

        if last["close"] < last["open"]:
            return "bearish"

    return "none"


# ============================================================
# PREMIUM / DISCOUNT
# ============================================================

def premium_discount(df):

    if len(df) < 20:
        return "mid"

    recent = df.tail(20)

    high = recent["high"].max()
    low = recent["low"].min()

    mid = (
        high + low
    ) / 2

    price = recent[
        "close"
    ].iloc[-1]

    if price > mid:
        return "premium"

    if price < mid:
        return "discount"

    return "mid"


# ============================================================
# 🧠 SMART TREND ENGINE
# ============================================================

def trend_score(df):

    df = add_indicators(df)

    structure = structure_direction(df)

    momentum = momentum_direction(df)

    bos = detect_bos(df)

    last = df.iloc[-1]

    score_bull = 0
    score_bear = 0

    # -------------------------
    # STRUCTURE = 40
    # -------------------------

    if structure == "bullish":
        score_bull += 40

    elif structure == "bearish":
        score_bear += 40

    # -------------------------
    # EMA = 25
    # -------------------------

    if (
        last["ema20"]
        >
        last["ema50"]
        >
        last["ema200"]
    ):

        score_bull += 25

    elif (
        last["ema20"]
        <
        last["ema50"]
        <
        last["ema200"]
    ):

        score_bear += 25

    elif last["ema20"] > last["ema50"]:

        score_bull += 12

    elif last["ema20"] < last["ema50"]:

        score_bear += 12

    # -------------------------
    # MOMENTUM = 15
    # -------------------------

    if momentum == "bullish":
        score_bull += 15

    elif momentum == "bearish":
        score_bear += 15

    # -------------------------
    # BOS = 20
    # -------------------------

    if bos == "bullish":
        score_bull += 20

    elif bos == "bearish":
        score_bear += 20

    # -------------------------
    # FINAL TREND
    # -------------------------

    if score_bull >= score_bear:

        direction = (
            "bullish"
            if score_bull >= 60
            else "mixed"
        )

        return {
            "direction": direction,
            "score": score_bull,
            "structure": structure,
            "momentum": momentum
        }

    direction = (
        "bearish"
        if score_bear >= 60
        else "mixed"
    )

    return {
        "direction": direction,
        "score": score_bear,
        "structure": structure,
        "momentum": momentum
    }


# ============================================================
# MARKET DATA
# ============================================================

def build_market_data():

    base = fetch_intraday(1)

    return {
        "M5": add_indicators(
            resample_ohlc(
                base,
                "5min"
            )
        ),

        "M15": add_indicators(
            resample_ohlc(
                base,
                "15min"
            )
        ),

        "H1": add_indicators(
            resample_ohlc(
                base,
                "1h"
            )
        ),

        "H4": add_indicators(
            resample_ohlc(
                base,
                "4h"
            )
        )
    }


# ============================================================
# SIGNAL SCORE
# ============================================================

def side_score(
    direction,
    analysis
):

    weights = {
        "H4": 20,
        "H1": 15,
        "M15": 15,
        "M5": 10
    }

    score = 0

    # -------------------------
    # HIGHER TIMEFRAME TREND
    # -------------------------

    for tf, weight in weights.items():

        trend = analysis[
            tf
        ]["trend"]

        if trend["direction"] == direction:
            score += weight

    m5 = analysis["M5"]

    # ========================================================
    # BUY
    # ========================================================

    if direction == "bullish":

        if m5["liquidity"] == "ssl_sweep":
            score += 10

        if m5["bos"] == "bullish":
            score += 15

        if m5["fvg"] == "bullish":
            score += 5

        if m5["displacement"] == "bullish":
            score += 10

        if m5["momentum"] == "bullish":
            score += 5

        if m5["rsi"] < 70:
            score += 5

        if m5["zone"] == "discount":
            score += 3

    # ========================================================
    # SELL
    # ========================================================

    else:

        if m5["liquidity"] == "bsl_sweep":
            score += 10

        if m5["bos"] == "bearish":
            score += 15

        if m5["fvg"] == "bearish":
            score += 5

        if m5["displacement"] == "bearish":
            score += 10

        if m5["momentum"] == "bearish":
            score += 5

        if m5["rsi"] > 30:
            score += 5

        if m5["zone"] == "premium":
            score += 3

    return min(
        score,
        100
    )


# ============================================================
# FULL MARKET ANALYSIS
# ============================================================

def analyze_market():

    data = build_market_data()

    analysis = {}

    for tf, df in data.items():

        trend = trend_score(df)

        analysis[tf] = {

            "trend": trend,

            "bos": detect_bos(df),

            "liquidity": detect_liquidity(df),

            "fvg": detect_fvg(df),

            "displacement": detect_displacement(df),

            "momentum": momentum_direction(df),

            "zone": premium_discount(df),

            "rsi": float(
                df["rsi"].iloc[-1]
            ),

            "price": float(
                df["close"].iloc[-1]
            )
        }

    # ========================================================
    # HTF BIAS
    # ========================================================

    h4 = analysis[
        "H4"
    ]["trend"]["direction"]

    h1 = analysis[
        "H1"
    ]["trend"]["direction"]

    if (
        h4 == "bullish"
        and
        h1 == "bullish"
    ):

        bias = "bullish"

    elif (
        h4 == "bearish"
        and
        h1 == "bearish"
    ):

        bias = "bearish"

    else:

        bias = "mixed"

    # ========================================================
    # SCORES
    # ========================================================

    buy_score = side_score(
        "bullish",
        analysis
    )

    sell_score = side_score(
        "bearish",
        analysis
    )

    # ========================================================
    # M5 TRIGGERS
    # ========================================================

    m5 = analysis["M5"]

    buy_trigger = (
        m5["liquidity"] == "ssl_sweep"
        or
        m5["bos"] == "bullish"
        or
        m5["displacement"] == "bullish"
    )

    sell_trigger = (
        m5["liquidity"] == "bsl_sweep"
        or
        m5["bos"] == "bearish"
        or
        m5["displacement"] == "bearish"
    )

    # ========================================================
    # FULL SIGNAL
    # ========================================================

    full_buy = (
        buy_score >= MIN_SCORE
        and
        buy_trigger
        and
        bias != "bearish"
    )

    full_sell = (
        sell_score >= MIN_SCORE
        and
        sell_trigger
        and
        bias != "bullish"
    )

    # ========================================================
    # EARLY SIGNAL
    # ========================================================

    early_buy = (
        buy_score >= EARLY_SCORE
        and
        buy_score > sell_score
        and
        buy_trigger
        and
        bias != "bearish"
    )

    early_sell = (
        sell_score >= EARLY_SCORE
        and
        sell_score > buy_score
        and
        sell_trigger
        and
        bias != "bullish"
    )

    # ========================================================
    # FINAL SIGNAL
    # ========================================================

    signal = "WAIT"

    score = max(
        buy_score,
        sell_score
    )

    if (
        full_buy
        and
        buy_score > sell_score
    ):

        signal = "BUY"
        score = buy_score

    elif (
        full_sell
        and
        sell_score > buy_score
    ):

        signal = "SELL"
        score = sell_score

    elif early_buy:

        signal = "EARLY BUY"
        score = buy_score

    elif early_sell:

        signal = "EARLY SELL"
        score = sell_score

    return {

        "signal": signal,

        "score": score,

        "buy_score": buy_score,

        "sell_score": sell_score,

        "bias": bias,

        "time": datetime.now(
            timezone.utc
        ).isoformat(),

        "analysis": analysis
    }


# ============================================================
# TELEGRAM SIGNAL MESSAGE
# ============================================================

def build_signal_message(
    result,
    price
):

    a = result["analysis"]

    lines = [

        "🥇 GOLD SMART V5.9",

        "",

        "📡 SIGNAL: {}".format(
            result["signal"]
        ),

        "💰 XAUUSD: {:.2f}".format(
            price
        ),

        "",

        "🧠 SMART TREND ENGINE",

        "H4: {} {}/100".format(
            a["H4"]["trend"]["direction"].upper(),
            a["H4"]["trend"]["score"]
        ),

        "H1: {} {}/100".format(
            a["H1"]["trend"]["direction"].upper(),
            a["H1"]["trend"]["score"]
        ),

        "M15: {} {}/100".format(
            a["M15"]["trend"]["direction"].upper(),
            a["M15"]["trend"]["score"]
        ),

        "M5: {} {}/100".format(
            a["M5"]["trend"]["direction"].upper(),
            a["M5"]["trend"]["score"]
        ),

        "",

        "🎯 HTF BIAS: {}".format(
            result["bias"].upper()
        ),

        "📊 BUY SCORE: {}/100".format(
            result["buy_score"]
        ),

        "📊 SELL SCORE: {}/100".format(
            result["sell_score"]
        ),

        "",

        "💧 M5 Liquidity: {}".format(
            a["M5"]["liquidity"].upper()
        ),

        "🔨 M5 BOS: {}".format(
            a["M5"]["bos"].upper()
        ),

        "🧩 M5 FVG: {}".format(
            a["M5"]["fvg"].upper()
        ),

        "💥 M5 Displacement: {}".format(
            a["M5"]["displacement"].upper()
        ),

        "📈 M5 Momentum: {}".format(
            a["M5"]["momentum"].upper()
        ),

        "📍 M5 Zone: {}".format(
            a["M5"]["zone"].upper()
        ),

        "📉 M5 RSI: {:.1f}".format(
            a["M5"]["rsi"]
        ),

        "",

        "🕯 Closed candles: {}".format(
            "ON"
            if CLOSED_CANDLES
            else "OFF"
        ),

        "🛡 AUTO TRADING: OFF",

        "📉 Risk: {:.1f}%".format(
            RISK_PERCENT
        )
    ]

    # ========================================================
    # TRADE PARAMETERS
    # ========================================================

    if result["signal"] in (
        "BUY",
        "SELL"
    ):

        entry = price

        if result["signal"] == "BUY":

            sl = (
                entry -
                SL_DISTANCE
            )

            tp = (
                entry +
                TP_DISTANCE
            )

        else:

            sl = (
                entry +
                SL_DISTANCE
            )

            tp = (
                entry -
                TP_DISTANCE
            )

        lines.extend([

            "",

            "🎯 ENTRY: {:.2f}".format(
                entry
            ),

            "🛑 SL: {:.2f}".format(
                sl
            ),

            "💰 TP: {:.2f}".format(
                tp
            ),

            "⚖️ RR: 1:3",

            "📦 Virtual lot: 0.01–0.02"
        ])

    return "\n".join(lines)


# ============================================================
# VIRTUAL TRADE
# ============================================================

def open_virtual_trade(
    signal,
    price,
    score
):

    global virtual_trade

    if signal not in (
        "BUY",
        "SELL"
    ):
        return

    if virtual_trade is not None:
        return

    if signal == "BUY":

        sl = (
            price -
            SL_DISTANCE
        )

        tp = (
            price +
            TP_DISTANCE
        )

    else:

        sl = (
            price +
            SL_DISTANCE
        )

        tp = (
            price -
            TP_DISTANCE
        )

    virtual_trade = {

        "side": signal,

        "entry": price,

        "sl": sl,

        "tp": tp,

        "score": score,

        "opened_at": datetime.now(
            timezone.utc
        ).isoformat(),

        "be": False
    }

    stats["open"] = 1

    save_stats(stats)


# ============================================================
# CLOSE VIRTUAL TRADE
# ============================================================

def close_virtual_trade(
    reason
):

    global virtual_trade

    if virtual_trade is None:
        return

    side = virtual_trade[
        "side"
    ]

    if reason == "WIN":

        stats["win"] += 1

    elif reason == "LOSS":

        stats["loss"] += 1

    else:

        stats["be"] += 1

    stats["open"] = 0

    save_stats(stats)

    send_telegram(
        "🥇 GOLD SMART V5.9\n\n"
        "📌 VIRTUAL TRADE CLOSED\n"
        "{}\n"
        "Result: {}".format(
            side,
            reason
        )
    )

    virtual_trade = None


# ============================================================
# UPDATE VIRTUAL TRADE
# ============================================================

def update_virtual_trade(
    price
):

    if virtual_trade is None:
        return

    side = virtual_trade[
        "side"
    ]

    entry = virtual_trade[
        "entry"
    ]

    # ========================================================
    # BUY
    # ========================================================

    if side == "BUY":

        if (
            price >= entry + BE_TRIGGER
            and
            not virtual_trade["be"]
        ):

            virtual_trade["sl"] = entry

            virtual_trade["be"] = True

            send_telegram(
                "🥇 GOLD SMART V5.9\n\n"
                "🔒 BUY → BREAK EVEN"
            )

        if price >= virtual_trade["tp"]:

            close_virtual_trade(
                "WIN"
            )

        elif price <= virtual_trade["sl"]:

            close_virtual_trade(
                "BE"
                if virtual_trade["be"]
                else
                "LOSS"
            )

    # ========================================================
    # SELL
    # ========================================================

    else:

        if (
            price <= entry - BE_TRIGGER
            and
            not virtual_trade["be"]
        ):

            virtual_trade["sl"] = entry

            virtual_trade["be"] = True

            send_telegram(
                "🥇 GOLD SMART V5.9\n\n"
                "🔒 SELL → BREAK EVEN"
            )

        if price <= virtual_trade["tp"]:

            close_virtual_trade(
                "WIN"
            )

        elif price >= virtual_trade["sl"]:

            close_virtual_trade(
                "BE"
                if virtual_trade["be"]
                else
                "LOSS"
            )


# ============================================================
# STATISTICS MESSAGE
# ============================================================

def stats_message():

    closed = (
        stats["win"]
        +
        stats["loss"]
        +
        stats["be"]
    )

    win_rate = (
        stats["win"] /
        closed *
        100
        if closed
        else
        0.0
    )

    return "\n".join([

        "📊 GOLD SMART V5.9 — STATISTICS",

        "",

        "📌 TOTAL SIGNALS: {}".format(
            stats["total_signals"]
        ),

        "🟢 FULL: {}".format(
            stats["full_signals"]
        ),

        "🟡 EARLY: {}".format(
            stats["early_signals"]
        ),

        "",

        "🟢 WIN: {}".format(
            stats["win"]
        ),

        "🔴 LOSS: {}".format(
            stats["loss"]
        ),

        "⚪ BE: {}".format(
            stats["be"]
        ),

        "🟡 OPEN: {}".format(
            stats["open"]
        ),

        "",

        "🎯 CLOSED: {}".format(
            closed
        ),

        "📈 WIN RATE: {:.1f}%".format(
            win_rate
        ),

        "",

        "🛡 AUTO TRADING: OFF",

        "📉 RISK: {:.1f}%".format(
            RISK_PERCENT
        )
    ])


# ============================================================
# STATUS
# ============================================================

def status_message():

    price_text = "N/A"

    try:

        price_text = "{:.2f}".format(
            fetch_spot()
        )

    except Exception:
        pass

    virtual = "No virtual trade"

    if virtual_trade:

        virtual = (
            "{} @ {:.2f} | SL {:.2f} | TP {:.2f}"
            .format(
                virtual_trade["side"],
                virtual_trade["entry"],
                virtual_trade["sl"],
                virtual_trade["tp"]
            )
        )

    return "\n".join([

        "🥇 GOLD SMART V5.9",

        "",

        "🟢 Engine: READY",

        "{} Telegram: {}".format(
            "🟢"
            if BOT_TOKEN and CHAT_ID
            else
            "🔴",

            "READY"
            if BOT_TOKEN and CHAT_ID
            else
            "NOT CONFIGURED"
        ),

        "",

        "💰 XAUUSD: {}".format(
            price_text
        ),

        "⚪ {}".format(
            virtual
        ),

        "",

        "🛡 AUTO TRADING: OFF",

        "📉 Risk: {:.1f}%".format(
            RISK_PERCENT
        ),

        "🕯 Closed candles: {}".format(
            "ON"
            if CLOSED_CANDLES
            else
            "OFF"
        )
    ])


# ============================================================
# HELP
# ============================================================

def help_message():

    return "\n".join([

        "🥇 GOLD SMART V5.9",

        "",

        "/start — запуск",

        "/status — состояние",

        "/signal — текущий анализ",

        "/test — тест Telegram",

        "/stats — статистика",

        "/help — команды",

        "",

        "AUTO TRADING: OFF",

        "Risk: 1%"
    ])


# ============================================================
# AUTHORIZATION
# ============================================================

def authorized(chat_id):

    return str(chat_id) == str(CHAT_ID)


# ============================================================
# COMMAND PROCESSOR
# ============================================================

def process_command(
    chat_id,
    text_value
):

    if not authorized(chat_id):
        return

    command = (
        text_value
        or ""
    ).strip().split()[0].lower()

    # -------------------------
    # START
    # -------------------------

    if command == "/start":

        send_telegram(
            "🥇 GOLD SMART V5.9\n\n"
            "Engine READY.\n"
            "AUTO TRADING: OFF\n"
            "Risk: 1%"
        )

    # -------------------------
    # STATUS
    # -------------------------

    elif command == "/status":

        send_telegram(
            status_message()
        )

    # -------------------------
    # HELP
    # -------------------------

    elif command == "/help":

        send_telegram(
            help_message()
        )

    # -------------------------
    # TEST
    # -------------------------

    elif command == "/test":

        send_telegram(
            "✅ GOLD SMART V5.9 TEST OK\n"
            "🟢 Engine: READY\n"
            "🟢 Telegram: READY\n"
            "🟢 XAUS API: READY\n"
            "🤖 AUTO TRADING: OFF"
        )

    # -------------------------
    # STATS
    # -------------------------

    elif command == "/stats":

        send_telegram(
            stats_message()
        )

    # -------------------------
    # SIGNAL
    # -------------------------

    elif command == "/signal":

        try:

            price = fetch_spot()

            result = analyze_market()

            send_telegram(
                build_signal_message(
                    result,
                    price
                )
            )

        except Exception as exc:

            send_telegram(
                "⚠️ GOLD SMART V5.9\n\n"
                "❌ Analysis error: {}".format(
                    exc
                )
            )


# ============================================================
# FLASK
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def root():

    return (
        "GOLD SMART V5.9 OK",
        200
    )


@app.route(
    "/health",
    methods=["GET"]
)
def health():

    return {
        "status": "ok",
        "version": "V5.9",
        "auto_trading": False
    }, 200


@app.route(
    "/test",
    methods=["GET"]
)
def http_test():

    return (
        "GOLD SMART V5.9 TEST OK",
        200
    )


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.route(
    "/telegram",
    methods=["POST"]
)
def telegram_webhook():

    try:

        update = (
            request.get_json(
                silent=True
            )
            or {}
        )

        message = (
            update.get("message")
            or
            update.get("edited_message")
            or
            {}
        )

        chat = (
            message.get("chat")
            or
            {}
        )

        chat_id = chat.get(
            "id"
        )

        text_value = message.get(
            "text",
            ""
        )

        if (
            chat_id is not None
            and
            text_value
        ):

            process_command(
                chat_id,
                text_value
            )

        return (
            "OK",
            200
        )

    except Exception as exc:

        print(
            "Webhook error:",
            exc,
            flush=True
        )

        return (
            "OK",
            200
        )


# ============================================================
# 🧠 ENGINE LOOP
# ============================================================

def engine_loop():

    global last_signal_time

    print(
        "🥇 GOLD SMART V5.9 engine started",
        flush=True
    )

    while True:

        try:

            # -------------------------
            # PRICE
            # -------------------------

            price = fetch_spot()

            # -------------------------
            # VIRTUAL TRADE
            # -------------------------

            update_virtual_trade(
                price
            )

            # -------------------------
            # MARKET ANALYSIS
            # -------------------------

            result = analyze_market()

            signal = result[
                "signal"
            ]

            now = time.time()

            # -------------------------
            # RENDER LOG
            # -------------------------

            print(

                "[{}] {} | "
                "XAUUSD {:.2f} | "
                "BUY {} | "
                "SELL {} | "
                "BIAS {}".format(

                    datetime.now(
                        timezone.utc
                    ).strftime(
                        "%Y-%m-%d %H:%M:%S UTC"
                    ),

                    signal,

                    price,

                    result["buy_score"],

                    result["sell_score"],

                    result["bias"].upper()
                ),

                flush=True
            )

            # -------------------------
            # COOLDOWN
            # -------------------------

            can_send = (
                now -
                last_signal_time
                >=
                COOLDOWN_MIN * 60
            )

            # -------------------------
            # SIGNAL
            # -------------------------

            if (
                signal != "WAIT"
                and
                can_send
            ):

                message = build_signal_message(
                    result,
                    price
                )

                sent = send_telegram(
                    message
                )

                if sent:

                    last_signal_time = now

                    stats[
                        "total_signals"
                    ] += 1

                    stats[
                        "last_signal"
                    ] = signal

                    # -----------------
                    # FULL
                    # -----------------

                    if signal in (
                        "BUY",
                        "SELL"
                    ):

                        stats[
                            "full_signals"
                        ] += 1

                        open_virtual_trade(
                            signal,
                            price,
                            result["score"]
                        )

                    # -----------------
                    # EARLY
                    # -----------------

                    elif signal in (
                        "EARLY BUY",
                        "EARLY SELL"
                    ):

                        stats[
                            "early_signals"
                        ] += 1

                    save_stats(
                        stats
                    )

        except Exception as exc:

            print(
                "Engine error:",
                repr(exc),
                flush=True
            )

        time.sleep(
            POLL_SECONDS
        )


# ============================================================
# START ENGINE
# ============================================================

def start_engine():

    global _engine_started

    if _engine_started:
        return

    _engine_started = True

    thread = threading.Thread(
        target=engine_loop,
        daemon=True,
        name="gold-smart-engine"
    )

    thread.start()


# ============================================================
# IMPORTANT:
# Render + Gunicorn imports bot.py.
# Therefore engine must start on import.
# ============================================================

start_engine()


# ============================================================
# LOCAL START
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv(
                "PORT",
                "10000"
            )
        )
    )
