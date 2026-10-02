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
# DATA: XAUS.COM
# SYMBOL: xau
# TELEGRAM POLLING
# AUTO TRADING = OFF
# RISK = 1%
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SPOT_URL = "https://xaus.com/api/v1/spot"
INTRADAY_URL = "https://xaus.com/api/v1/intraday"
TG_URL = "https://api.telegram.org/bot{}".format(BOT_TOKEN)

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

_engine_started = False
_polling_started = False
_stats_lock = threading.Lock()

stats = None


# ============================================================
# TELEGRAM SEND
# ============================================================

def telegram_url(method):
    return "{}/{}".format(TG_URL, method)


def send_telegram(message, chat_id=None):
    target = chat_id or CHAT_ID

    if not BOT_TOKEN or not target:
        print("Telegram credentials missing", flush=True)
        return False

    try:
        response = requests.post(
            telegram_url("sendMessage"),
            json={
                "chat_id": target,
                "text": message
            },
            timeout=15
        )

        if not response.ok:
            print(
                "Telegram HTTP:",
                response.status_code,
                response.text[:500],
                flush=True
            )

        return response.ok

    except Exception as exc:
        print(
            "Telegram error:",
            repr(exc),
            flush=True
        )
        return False


def telegram_get(method, params=None):
    if not BOT_TOKEN:
        return None

    try:
        response = requests.get(
            telegram_url(method),
            params=params or {},
            timeout=35
        )

        if not response.ok:
            print(
                "Telegram GET HTTP:",
                response.status_code,
                response.text[:500],
                flush=True
            )
            return None

        return response.json()

    except Exception as exc:
        print(
            "Telegram GET error:",
            repr(exc),
            flush=True
        )
        return None


# ============================================================
# TELEGRAM POLLING
# ============================================================

def telegram_poll_loop():
    global _polling_started

    if _polling_started:
        return

    _polling_started = True

    print(
        "🥇 GOLD SMART V5.9 Telegram polling started",
        flush=True
    )

    # Remove webhook so getUpdates can work.
    try:
        telegram_get(
            "deleteWebhook",
            {"drop_pending_updates": False}
        )
    except Exception as exc:
        print(
            "Webhook delete error:",
            repr(exc),
            flush=True
        )

    offset = None

    while True:

        try:

            params = {
                "timeout": 25
            }

            if offset is not None:
                params["offset"] = offset

            result = telegram_get(
                "getUpdates",
                params
            )

            if not result:
                time.sleep(2)
                continue

            if not result.get("ok"):
                time.sleep(3)
                continue

            updates = result.get(
                "result",
                []
            )

            for update in updates:

                try:

                    update_id = update.get(
                        "update_id"
                    )

                    if update_id is not None:
                        offset = update_id + 1

                    message = update.get(
                        "message"
                    )

                    if not message:
                        continue

                    chat = message.get(
                        "chat",
                        {}
                    )

                    chat_id = str(
                        chat.get(
                            "id",
                            ""
                        )
                    )

                    text = message.get(
                        "text",
                        ""
                    ).strip()

                    if not text:
                        continue

                    # Security:
                    # if TELEGRAM_CHAT_ID is configured,
                    # accept commands only from that chat.
                    if CHAT_ID and chat_id != str(CHAT_ID):
                        print(
                            "Ignored Telegram chat:",
                            chat_id,
                            flush=True
                        )
                        continue

                    handle_telegram_command(
                        text,
                        chat_id
                    )

                except Exception as exc:
                    print(
                        "Telegram update error:",
                        repr(exc),
                        flush=True
                    )

        except Exception as exc:

            print(
                "Telegram polling error:",
                repr(exc),
                flush=True
            )

            time.sleep(5)


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

        result = default_stats()
        result.update(data)

        return result

    except Exception as exc:

        print(
            "Stats load error:",
            repr(exc),
            flush=True
        )

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

        print(
            "Stats save error:",
            repr(exc),
            flush=True
        )


stats = load_stats()


# ============================================================
# SPOT PRICE
# ============================================================

def fetch_spot():

    now = time.time()

    if (
        price_cache["value"] is not None
        and
        now - price_cache["ts"] < CACHE_SECONDS
    ):
        return float(
            price_cache["value"]
        )

    response = requests.get(
        SPOT_URL,
        timeout=15
    )

    response.raise_for_status()

    payload = response.json()

    price = None

    if isinstance(
        payload,
        dict
    ):

        candidates = [payload]

        for key in (
            "data",
            "xau",
            "gold",
            "result"
        ):

            value = payload.get(key)

            if isinstance(
                value,
                dict
            ):

                candidates.append(value)

        for item in candidates:

            for key in (
                "price",
                "spot_usd_oz",
                "close",
                "last",
                "value"
            ):

                value = item.get(key)

                if value is None:
                    continue

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
            "XAUS spot price not found: {}".format(
                str(payload)[:500]
            )
        )

    price_cache["value"] = price
    price_cache["ts"] = now

    return price


# ============================================================
# INTRADAY PARSER
# ============================================================

def parse_point(point):

    if not isinstance(
        point,
        dict
    ):
        return None

    timestamp = None

    for key in (
        "t",
        "timestamp",
        "time",
        "date",
        "datetime"
    ):

        if point.get(key) is not None:
            timestamp = point.get(key)
            break

    price = None

    for key in (
        "p",
        "price",
        "close",
        "value"
    ):

        if point.get(key) is not None:
            price = point.get(key)
            break

    if timestamp is None or price is None:
        return None

    try:

        price = float(price)

        if isinstance(
            timestamp,
            (int, float)
        ):

            unit = (
                "ms"
                if float(timestamp) > 10000000000
                else "s"
            )

            dt = pd.to_datetime(
                timestamp,
                unit=unit,
                utc=True
            )

        else:

            dt = pd.to_datetime(
                timestamp,
                utc=True
            )

        return dt, price

    except Exception:
        return None


# ============================================================
# INTRADAY DATA
# ============================================================

def fetch_intraday(minutes=1):

    cache_key = str(minutes)
    now = time.time()

    cached = intraday_cache.get(
        cache_key
    )

    if (
        cached
        and
        now - cached["ts"] < CACHE_SECONDS
    ):

        return cached["data"].copy()

    # XAUS requires xau.
    params = {
        "symbol": "xau",
        "interval": minutes
    }

    response = requests.get(
        INTRADAY_URL,
        params=params,
        timeout=20
    )

    if not response.ok:

        raise RuntimeError(
            "XAUS intraday HTTP {}: {}".format(
                response.status_code,
                response.text[:500]
            )
        )

    payload = response.json()

    points = None

    if isinstance(
        payload,
        list
    ):

        points = payload

    elif isinstance(
        payload,
        dict
    ):

        for key in (
            "points",
            "data",
            "series",
            "prices",
            "result"
        ):

            value = payload.get(key)

            if isinstance(
                value,
                list
            ):

                points = value
                break

    if not points:

        raise ValueError(
            "XAUS returned no intraday points: {}".format(
                str(payload)[:500]
            )
        )

    rows = []

    for point in points:

        parsed = parse_point(
            point
        )

        if parsed:
            rows.append(parsed)

    if len(rows) < 30:

        raise ValueError(
            "Not enough XAU data: {} points".format(
                len(rows)
            )
        )

    df = pd.DataFrame(
        rows,
        columns=[
            "time",
            "close"
        ]
    )

    df = df.drop_duplicates(
        subset=["time"]
    )

    df = df.sort_values(
        "time"
    )

    df = df.set_index(
        "time"
    )

    df["open"] = df[
        "close"
    ].shift(1)

    df["high"] = df[
        ["open", "close"]
    ].max(axis=1)

    df["low"] = df[
        ["open", "close"]
    ].min(axis=1)

    df = df[
        [
            "open",
            "high",
            "low",
            "close"
        ]
    ].dropna()

    intraday_cache[
        cache_key
    ] = {
        "data": df.copy(),
        "ts": now
    }

    return df


# ============================================================
# RESAMPLE
# ============================================================

def resample_ohlc(
    df,
    timeframe
):

    result = df.resample(
        timeframe
    ).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last"
        }
    ).dropna()

    if (
        CLOSED_CANDLES
        and
        len(result) > 1
    ):

        result = result.iloc[:-1]

    return result


# ============================================================
# INDICATORS
# ============================================================

def calculate_ema(
    series,
    period
):

    return series.ewm(
        span=period,
        adjust=False
    ).mean()


def calculate_rsi(
    series,
    period=14
):

    delta = series.diff()

    gain = (
        delta.clip(
            lower=0
        )
        .ewm(
            alpha=1 / period,
            adjust=False
        )
        .mean()
    )

    loss = (
        -delta.clip(
            upper=0
        )
        .ewm(
            alpha=1 / period,
            adjust=False
        )
        .mean()
    )

    rs = gain / loss.replace(
        0,
        np.nan
    )

    result = (
        100 -
        100 / (1 + rs)
    )

    return result.fillna(50)


def add_indicators(df):

    result = df.copy()

    result["ema20"] = calculate_ema(
        result["close"],
        20
    )

    result["ema50"] = calculate_ema(
        result["close"],
        50
    )

    result["ema200"] = calculate_ema(
        result["close"],
        200
    )

    result["rsi"] = calculate_rsi(
        result["close"],
        14
    )

    result["range"] = (
        result["high"] -
        result["low"]
    )

    result["body"] = (
        result["close"] -
        result["open"]
    ).abs()

    result["avg_range"] = (
        result["range"]
        .rolling(20)
        .mean()
    )

    return result


# ============================================================
# STRUCTURE
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

    rolling_high = (
        recent["high"]
        .rolling(
            3,
            center=True
        )
        .max()
    )

    rolling_low = (
        recent["low"]
        .rolling(
            3,
            center=True
        )
        .min()
    )

    swing_highs = recent[
        "high"
    ][
        recent["high"] ==
        rolling_high
    ].dropna()

    swing_lows = recent[
        "low"
    ][
        recent["low"] ==
        rolling_low
    ].dropna()

    if (
        len(swing_highs) < 2
        or
        len(swing_lows) < 2
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
    previous = df.iloc[-4]

    if (
        last["close"] > previous["close"]
        and
        last["ema20"] > previous["ema20"]
    ):

        return "bullish"

    if (
        last["close"] < previous["close"]
        and
        last["ema20"] < previous["ema20"]
    ):

        return "bearish"

    return "mixed"


# ============================================================
# BOS
# ============================================================

def detect_bos(df):

    if len(df) < 10:
        return "none"

    candle = df.iloc[-2]
    previous = df.iloc[-7:-2]

    previous_high = previous[
        "high"
    ].max()

    previous_low = previous[
        "low"
    ].min()

    if candle["close"] > previous_high:
        return "bullish"

    if candle["close"] < previous_low:
        return "bearish"

    return "none"


# ============================================================
# LIQUIDITY
# ============================================================

def detect_liquidity(df):

    if len(df) < 10:
        return "none"

    candle = df.iloc[-1]
    previous = df.iloc[-7:-1]

    previous_high = previous[
        "high"
    ].max()

    previous_low = previous[
        "low"
    ].min()

    if (
        candle["high"] > previous_high
        and
        candle["close"] < previous_high
    ):

        return "bsl_sweep"

    if (
        candle["low"] < previous_low
        and
        candle["close"] > previous_low
    ):

        return "ssl_sweep"

    return "none"


# ============================================================
# FVG
# ============================================================

def detect_fvg(df):

    if len(df) < 3:
        return "none"

    first = df.iloc[-3]
    last = df.iloc[-1]

    if last["low"] > first["high"]:
        return "bullish"

    if last["high"] < first["low"]:
        return "bearish"

    return "none"


# ============================================================
# DISPLACEMENT
# ============================================================

def detect_displacement(df):

    if len(df) < 21:
        return "none"

    last = df.iloc[-1]

    average_range = (
        df["range"]
        .iloc[-21:-1]
        .mean()
    )

    if (
        not np.isfinite(
            average_range
        )
        or
        average_range <= 0
    ):

        return "none"

    if (
        last["range"] >=
        average_range * 1.5
        and
        last["body"] >=
        last["range"] * 0.6
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

    high = recent[
        "high"
    ].max()

    low = recent[
        "low"
    ].min()

    midpoint = (
        high + low
    ) / 2

    price = recent[
        "close"
    ].iloc[-1]

    if price > midpoint:
        return "premium"

    if price < midpoint:
        return "discount"

    return "mid"


# ============================================================
# SMART TREND ENGINE
# ============================================================

def calculate_trend_score(df):

    df = add_indicators(df)

    structure = structure_direction(df)
    momentum = momentum_direction(df)
    bos = detect_bos(df)

    last = df.iloc[-1]

    bullish_score = 0
    bearish_score = 0

    if structure == "bullish":
        bullish_score += 40

    elif structure == "bearish":
        bearish_score += 40

    if (
        last["ema20"] >
        last["ema50"] >
        last["ema200"]
    ):

        bullish_score += 25

    elif (
        last["ema20"] <
        last["ema50"] <
        last["ema200"]
    ):

        bearish_score += 25

    elif last["ema20"] > last["ema50"]:

        bullish_score += 12

    elif last["ema20"] < last["ema50"]:

        bearish_score += 12

    if momentum == "bullish":
        bullish_score += 15

    elif momentum == "bearish":
        bearish_score += 15

    if bos == "bullish":
        bullish_score += 20

    elif bos == "bearish":
        bearish_score += 20

    if bullish_score >= bearish_score:

        direction = (
            "bullish"
            if bullish_score >= 60
            else "mixed"
        )

        return {
            "direction": direction,
            "score": bullish_score,
            "structure": structure,
            "momentum": momentum
        }

    direction = (
        "bearish"
        if bearish_score >= 60
        else "mixed"
    )

    return {
        "direction": direction,
        "score": bearish_score,
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
# SIDE SCORE
# ============================================================

def calculate_side_score(
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

    for timeframe, weight in weights.items():

        trend = analysis[
            timeframe
        ]["trend"]

        if trend["direction"] == direction:
            score += weight

    m5 = analysis["M5"]

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
# ANALYZE MARKET
# ============================================================

def analyze_market():

    market = build_market_data()
    analysis = {}

    for timeframe, df in market.items():

        if len(df) < 30:
            raise ValueError(
                "{}: not enough candles ({})".format(
                    timeframe,
                    len(df)
                )
            )

        trend = calculate_trend_score(df)

        analysis[timeframe] = {
            "trend": trend,
            "bos": detect_bos(df),
            "liquidity": detect_liquidity(df),
            "fvg": detect_fvg(df),
            "displacement": detect_displacement(df),
            "momentum": momentum_direction(df),
            "zone": premium_discount(df),
            "rsi": float(df["rsi"].iloc[-1]),
            "price": float(df["close"].iloc[-1])
        }

    h4 = analysis["H4"]["trend"]["direction"]
    h1 = analysis["H1"]["trend"]["direction"]

    if h4 == "bullish" and h1 == "bullish":
        bias = "bullish"

    elif h4 == "bearish" and h1 == "bearish":
        bias = "bearish"

    else:
        bias = "mixed"

    buy_score = calculate_side_score(
        "bullish",
        analysis
    )

    sell_score = calculate_side_score(
        "bearish",
        analysis
    )

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

    full_buy = (
        buy_score >= MIN_SCORE
        and
        buy_score > sell_score
        and
        buy_trigger
        and
        bias != "bearish"
    )

    full_sell = (
        sell_score >= MIN_SCORE
        and
        sell_score > buy_score
        and
        sell_trigger
        and
        bias != "bullish"
    )

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

    signal = "WAIT"
    score = max(
        buy_score,
        sell_score
    )

    if full_buy:
        signal = "BUY"
        score = buy_score

    elif full_sell:
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
        "analysis": analysis,
        "time": datetime.now(
            timezone.utc
        ).isoformat()
    }


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def build_signal_message(
    result,
    price
):

    analysis = result["analysis"]

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
        "📊 H4: {} {}/100".format(
            analysis["H4"]["trend"]["direction"].upper(),
            analysis["H4"]["trend"]["score"]
        ),
        "📊 H1: {} {}/100".format(
            analysis["H1"]["trend"]["direction"].upper(),
            analysis["H1"]["trend"]["score"]
        ),
        "📊 M15: {} {}/100".format(
            analysis["M15"]["trend"]["direction"].upper(),
            analysis["M15"]["trend"]["score"]
        ),
        "📊 M5: {} {}/100".format(
            analysis["M5"]["trend"]["direction"].upper(),
            analysis["M5"]["trend"]["score"]
        ),
        "",
        "🎯 HTF BIAS: {}".format(
            result["bias"].upper()
        ),
        "🟢 BUY SCORE: {}/100".format(
            result["buy_score"]
        ),
        "🔴 SELL SCORE: {}/100".format(
            result["sell_score"]
        ),
        "",
        "💧 M5 Liquidity: {}".format(
            analysis["M5"]["liquidity"].upper()
        ),
        "🔨 M5 BOS: {}".format(
            analysis["M5"]["bos"].upper()
        ),
        "🧩 M5 FVG: {}".format(
            analysis["M5"]["fvg"].upper()
        ),
        "💥 M5 Displacement: {}".format(
            analysis["M5"]["displacement"].upper()
        ),
        "📈 M5 Momentum: {}".format(
            analysis["M5"]["momentum"].upper()
        ),
        "📍 M5 Zone: {}".format(
            analysis["M5"]["zone"].upper()
        ),
        "📉 M5 RSI: {:.1f}".format(
            analysis["M5"]["rsi"]
        ),
        "",
        "🕯 Closed candles: {}".format(
            "ON" if CLOSED_CANDLES else "OFF"
        ),
        "🛡 AUTO TRADING: OFF",
        "📉 Risk: {:.1f}%".format(
            RISK_PERCENT
        )
    ]

    if result["signal"] in (
        "BUY",
        "SELL"
    ):

        entry = price

        if result["signal"] == "BUY":
            sl = entry - SL_DISTANCE
            tp = entry + TP_DISTANCE
        else:
            sl = entry + SL_DISTANCE
            tp = entry - TP_DISTANCE

        lines.extend([
            "",
            "🎯 ENTRY: {:.2f}".format(entry),
            "🛑 SL: {:.2f}".format(sl),
            "💰 TP: {:.2f}".format(tp),
            "⚖️ RR: 1:3",
            "📦 Virtual lot: 0.01–0.02"
        ])

    elif result["signal"] in (
        "EARLY BUY",
        "EARLY SELL"
    ):

        lines.extend([
            "",
            "⚠️ EARLY SIGNAL",
            "ℹ️ Notification only",
            "ℹ️ Virtual trade is NOT opened"
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

        sl = price - SL_DISTANCE
        tp = price + TP_DISTANCE

    else:

        sl = price + SL_DISTANCE
        tp = price - TP_DISTANCE

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

    with _stats_lock:
        stats["open"] = 1
        save_stats(stats)


# ============================================================
# VIRTUAL TRADE MANAGEMENT
# ============================================================

def update_virtual_trade(price):

    global virtual_trade

    if virtual_trade is None:
        return

    trade = virtual_trade
    side = trade["side"]
    entry = float(trade["entry"])

    if side == "BUY":

        profit_move = price - entry

        if (
            not trade["be"]
            and
            profit_move >= BE_TRIGGER
        ):

            trade["sl"] = entry
            trade["be"] = True

            send_telegram(
                "\n".join([
                    "🥇 GOLD SMART V5.9",
                    "",
                    "🛡 BREAK EVEN",
                    "📈 BUY",
                    "🎯 SL moved to ENTRY",
                    "💰 XAUUSD: {:.2f}".format(price)
                ])
            )

        if price >= trade["tp"]:
            close_virtual_trade("WIN")
            return

        if price <= trade["sl"]:
            if trade["be"]:
                close_virtual_trade("BE")
            else:
                close_virtual_trade("LOSS")

    else:

        profit_move = entry - price

        if (
            not trade["be"]
            and
            profit_move >= BE_TRIGGER
        ):

            trade["sl"] = entry
            trade["be"] = True

            send_telegram(
                "\n".join([
                    "🥇 GOLD SMART V5.9",
                    "",
                    "🛡 BREAK EVEN",
                    "📉 SELL",
                    "🎯 SL moved to ENTRY",
                    "💰 XAUUSD: {:.2f}".format(price)
                ])
            )

        if price <= trade["tp"]:
            close_virtual_trade("WIN")
            return

        if price >= trade["sl"]:
            if trade["be"]:
                close_virtual_trade("BE")
            else:
                close_virtual_trade("LOSS")


# ============================================================
# CLOSE VIRTUAL TRADE
# ============================================================

def close_virtual_trade(result):

    global virtual_trade

    if virtual_trade is None:
        return

    side = virtual_trade["side"]

    with _stats_lock:

        if result == "WIN":
            stats["win"] += 1

        elif result == "LOSS":
            stats["loss"] += 1

        else:
            stats["be"] += 1

        stats["open"] = 0
        save_stats(stats)

    virtual_trade = None

    send_telegram(
        "\n".join([
            "🥇 GOLD SMART V5.9",
            "",
            "📌 VIRTUAL TRADE CLOSED",
            "📈 {}".format(side),
            "🎯 RESULT: {}".format(result)
        ])
    )


# ============================================================
# PROCESS SIGNAL
# ============================================================

def process_signal(
    result,
    price,
    force=False
):

    global last_signal_time

    signal = result["signal"]

    if signal == "WAIT":
        return False

    now = time.time()

    if (
        not force
        and
        now - last_signal_time
        < COOLDOWN_MIN * 60
    ):
        return False

    message = build_signal_message(
        result,
        price
    )

    sent = send_telegram(
        message
    )

    if not sent:
        return False

    last_signal_time = now

    with _stats_lock:

        stats["total_signals"] += 1

        if signal in (
            "BUY",
            "SELL"
        ):

            stats["full_signals"] += 1

        elif signal in (
            "EARLY BUY",
            "EARLY SELL"
        ):

            stats["early_signals"] += 1

        stats["last_signal"] = {
            "signal": signal,
            "price": price,
            "score": result["score"],
            "time": datetime.now(
                timezone.utc
            ).isoformat()
        }

        save_stats(stats)

    if signal in (
        "BUY",
        "SELL"
    ):

        open_virtual_trade(
            signal,
            price,
            result["score"]
        )

    return True


# ============================================================
# ENGINE LOOP
# ============================================================

def engine_loop():

    print(
        "🥇 GOLD SMART V5.9 engine started",
        flush=True
    )

    print(
        "🟢 Telegram:",
        "READY" if BOT_TOKEN and CHAT_ID else "NOT CONFIGURED",
        flush=True
    )

    print(
        "🟢 XAUS API: READY",
        flush=True
    )

    while True:

        try:

            price = fetch_spot()

            update_virtual_trade(
                price
            )

            result = analyze_market()

            print(
                "[{} UTC] {} | XAUUSD {:.2f} | BUY {} | SELL {} | BIAS {}".format(
                    datetime.now(
                        timezone.utc
                    ).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    result["signal"],
                    price,
                    result["buy_score"],
                    result["sell_score"],
                    result["bias"]
                ),
                flush=True
            )

            process_signal(
                result,
                price
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
# START THREADS
# ============================================================

def start_engine():

    global _engine_started

    if not _engine_started:

        _engine_started = True

        thread = threading.Thread(
            target=engine_loop,
            daemon=True,
            name="gold-smart-engine"
        )

        thread.start()


def start_telegram():

    if not BOT_TOKEN:
        print(
            "Telegram polling NOT started: BOT_TOKEN missing",
            flush=True
        )
        return

    thread = threading.Thread(
        target=telegram_poll_loop,
        daemon=True,
        name="telegram-polling"
    )

    thread.start()


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def format_stats():

    closed = (
        stats["win"]
        +
        stats["loss"]
        +
        stats["be"]
    )

    if closed > 0:
        win_rate = (
            stats["win"] /
            closed *
            100
        )
    else:
        win_rate = 0.0

    return "\n".join([
        "📊 GOLD SMART V5.9 — STATISTICS",
        "",
        "📌 TOTAL SIGNALS: {}".format(
            stats["total_signals"]
        ),
        "",
        "🟢 FULL: {}".format(
            stats["full_signals"]
        ),
        "🟡 EARLY: {}".format(
            stats["early_signals"]
        ),
        "",
        "🏆 WIN: {}".format(
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
        "📈 CLOSED: {}".format(
            closed
        ),
        "🎯 WIN RATE: {:.1f}%".format(
            win_rate
        )
    ])


def get_status_message():

    try:
        price = fetch_spot()

        price_text = "{:.2f}".format(
            price
        )

    except Exception:
        price_text = "ERROR"

    virtual_text = "None"

    if virtual_trade is not None:

        virtual_text = "{} @ {:.2f}".format(
            virtual_trade["side"],
            virtual_trade["entry"]
        )

    return "\n".join([
        "🥇 GOLD SMART V5.9",
        "",
        "🟢 Engine: READY",
        "🟢 Telegram: {}".format(
            "READY"
            if BOT_TOKEN and CHAT_ID
            else "NOT CONFIGURED"
        ),
        "🟢 XAUS API: READY",
        "",
        "💰 XAUUSD: {}".format(
            price_text
        ),
        "⚪ Virtual trade: {}".format(
            virtual_text
        ),
        "",
        "🛡 AUTO TRADING: OFF",
        "📉 Risk: {:.1f}%".format(
            RISK_PERCENT
        ),
        "📦 Virtual lot: 0.01–0.02",
        "",
        "⏱ Poll: {} sec".format(
            POLL_SECONDS
        ),
        "⏳ Cooldown: {} min".format(
            COOLDOWN_MIN
        )
    ])


def get_help_message():

    return "\n".join([
        "🥇 GOLD SMART V5.9",
        "",
        "📋 COMMANDS",
        "",
        "/start — запуск",
        "/status — статус системы",
        "/signal — текущий анализ",
        "/test — тест API",
        "/stats — статистика",
        "/help — команды",
        "",
        "🤖 Автоторговля: OFF",
        "📉 Risk: 1%",
        "⚖️ RR: 1:3"
    ])


def handle_telegram_command(
    text,
    chat_id
):

    command = text.lower().split()[0]

    if command == "/start":

        send_telegram(
            "\n".join([
                "🥇 GOLD SMART V5.9",
                "",
                "🟢 Бот запущен.",
                "📡 Сигналы работают автоматически.",
                "🛡 AUTO TRADING: OFF",
                "📉 Risk: 1%",
                "",
                "Используй /help"
            ]),
            chat_id
        )

    elif command == "/help":

        send_telegram(
            get_help_message(),
            chat_id
        )

    elif command == "/status":

        send_telegram(
            get_status_message(),
            chat_id
        )

    elif command == "/stats":

        send_telegram(
            format_stats(),
            chat_id
        )

    elif command == "/test":

        try:

            price = fetch_spot()

            intraday = fetch_intraday(1)

            message = "\n".join([
                "🥇 GOLD SMART V5.9 TEST",
                "",
                "🟢 Engine: READY",
                "🟢 Telegram: READY",
                "🟢 XAUS Spot: READY",
                "🟢 XAUS Intraday: READY",
                "",
                "💰 XAUUSD: {:.2f}".format(
                    price
                ),
                "📊 1m points: {}".format(
                    len(intraday)
                ),
                "",
                "🛡 AUTO TRADING: OFF"
            ])

        except Exception as exc:

            message = "\n".join([
                "🥇 GOLD SMART V5.9 TEST",
                "",
                "🔴 TEST ERROR",
                "",
                str(exc)
            ])

        send_telegram(
            message,
            chat_id
        )

    elif command == "/signal":

        try:

            price = fetch_spot()

            result = analyze_market()

            message = build_signal_message(
                result,
                price
            )

        except Exception as exc:

            message = "\n".join([
                "🥇 GOLD SMART V5.9",
                "",
                "🔴 ANALYSIS ERROR",
                "",
                str(exc)
            ])

        send_telegram(
            message,
            chat_id
        )

    else:

        send_telegram(
            "❓ Неизвестная команда.\n\nИспользуй /help",
            chat_id
        )


# ============================================================
# FLASK ROUTES
# ============================================================

@app.route("/")
def home():

    return {
        "status": "ok",
        "bot": "GOLD SMART V5.9",
        "engine": "running",
        "auto_trading": False
    }


@app.route("/health")
def health():

    return {
        "status": "healthy",
        "bot": "GOLD SMART V5.9",
        "engine": _engine_started,
        "telegram": bool(
            BOT_TOKEN and CHAT_ID
        )
    }


@app.route("/test")
def http_test():

    try:

        price = fetch_spot()

        return {
            "status": "ok",
            "price": price,
            "symbol": "xau"
        }

    except Exception as exc:

        return {
            "status": "error",
            "error": str(exc)
        }, 500


@app.route("/telegram", methods=["POST"])
def telegram_webhook():

    try:

        update = request.get_json(
            silent=True
        ) or {}

        message = update.get(
            "message"
        )

        if message:

            chat = message.get(
                "chat",
                {}
            )

            chat_id = str(
                chat.get(
                    "id",
                    ""
                )
            )

            text = message.get(
                "text",
                ""
            ).strip()

            if text:

                if (
                    not CHAT_ID
                    or
                    chat_id == str(CHAT_ID)
                ):

                    handle_telegram_command(
                        text,
                        chat_id
                    )

        return {
            "ok": True
        }

    except Exception as exc:

        print(
            "Webhook error:",
            repr(exc),
            flush=True
        )

        return {
            "ok": False
        }, 500


# ============================================================
# START EVERYTHING
# ============================================================

start_engine()
start_telegram()


# ============================================================
# LOCAL
# ============================================================

if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
