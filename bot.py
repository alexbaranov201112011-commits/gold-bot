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
# 🥇 GOLD SMART V5.9.4
# XAU/USD SMART SMC SIGNAL ENGINE
#
# DATA SOURCE: XAUS
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
#
# RENDER:
# gunicorn bot:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT
# ============================================================


# ============================================================
# APP
# ============================================================

app = Flask(__name__)


# ============================================================
# CONFIG
# ============================================================

APP_NAME = "GOLD SMART V5.9.4"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://gold-bot-q8la.onrender.com"
).strip()

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

# Минимум реальных свечей.
# H4 допускаем LIMITED режим начиная с 2 свечей.
MIN_CANDLES = {
    "M5": 30,
    "M15": 16,
    "H1": 7,
    "H4": 2,
}

# Полноценный H4-контекст.
PREFERRED_H4_CANDLES = 8


# ============================================================
# XAUS
# ============================================================

XAUS_SPOT_URL = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY_URL = "https://xaus.com/api/v1/intraday"

HTTP_TIMEOUT = 15


# ============================================================
# GLOBAL STATE
# ============================================================

engine_started = False
startup_done = False
startup_lock = threading.Lock()

webhook_configured = False

last_market_error = ""
last_market_error_time = 0.0

last_signal_time = 0.0
last_signal_key = ""

price_cache = {
    "price": None,
    "time": 0.0,
}

market_cache = {
    "data": None,
    "time": 0.0,
}

virtual_trade = None

stats = {
    "signals_total": 0,
    "full_signals": 0,
    "early_signals": 0,
    "wins": 0,
    "losses": 0,
    "breakeven": 0,
    "closed": 0,
    "open": 0,
}


# ============================================================
# FILES
# ============================================================

STATS_FILE = "gold_smart_stats.json"


# ============================================================
# UTILS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def utc_string():
    return utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def normalize_direction(value):
    if not value:
        return "MIXED"

    value = str(value).upper()

    if value in ("BUY", "BULLISH", "UP"):
        return "BUY"

    if value in ("SELL", "BEARISH", "DOWN"):
        return "SELL"

    return "MIXED"


def direction_word(value):
    value = normalize_direction(value)

    if value == "BUY":
        return "BULLISH"

    if value == "SELL":
        return "BEARISH"

    return "MIXED"


def price_round(price):
    if price is None:
        return None

    return round(float(price), 2)


# ============================================================
# TELEGRAM
# ============================================================

def telegram_url(method):
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"


def telegram_send(text, chat_id=None):
    if not BOT_TOKEN:
        print("Telegram: BOT_TOKEN is empty")
        return False

    target = chat_id or TELEGRAM_CHAT_ID

    if not target:
        print("Telegram: TELEGRAM_CHAT_ID is empty")
        return False

    try:
        response = requests.post(
            telegram_url("sendMessage"),
            json={
                "chat_id": target,
                "text": text,
            },
            timeout=HTTP_TIMEOUT,
        )

        if response.ok:
            return True

        print(
            "Telegram send error:",
            response.status_code,
            response.text[:500],
        )

    except Exception as exc:
        print("Telegram send exception:", repr(exc))

    return False


def telegram_get_updates_info():
    if not BOT_TOKEN:
        return None

    try:
        response = requests.get(
            telegram_url("getWebhookInfo"),
            timeout=HTTP_TIMEOUT,
        )

        if response.ok:
            return response.json()

    except Exception as exc:
        print("Webhook info error:", repr(exc))

    return None


def configure_webhook():
    global webhook_configured

    if not BOT_TOKEN:
        print("⚠️ BOT_TOKEN not configured")
        return False

    if not RENDER_EXTERNAL_URL:
        print("⚠️ RENDER_EXTERNAL_URL not configured")
        return False

    webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/telegram"

    try:
        response = requests.post(
            telegram_url("setWebhook"),
            json={
                "url": webhook_url,
                "drop_pending_updates": False,
            },
            timeout=HTTP_TIMEOUT,
        )

        print(
            "Webhook:",
            response.status_code,
            response.text[:500],
        )

        if response.ok:
            try:
                result = response.json()
                webhook_configured = bool(result.get("ok"))
            except Exception:
                webhook_configured = True

            return webhook_configured

    except Exception as exc:
        print("Webhook configure exception:", repr(exc))

    return False


# ============================================================
# STATS
# ============================================================

def load_stats():
    global stats

    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, "r", encoding="utf-8") as file:
                loaded = json.load(file)

            if isinstance(loaded, dict):
                for key in stats:
                    if key in loaded:
                        stats[key] = loaded[key]

    except Exception as exc:
        print("Stats load error:", repr(exc))


def save_stats():
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as file:
            json.dump(
                stats,
                file,
                ensure_ascii=False,
                indent=2,
            )

    except Exception as exc:
        print("Stats save error:", repr(exc))


# ============================================================
# XAUS SPOT
# ============================================================

def fetch_spot(force=False):
    now = time.time()

    if (
        not force
        and price_cache["price"] is not None
        and now - price_cache["time"] < CACHE_SECONDS
    ):
        return price_cache["price"]

    try:
        response = requests.get(
            XAUS_SPOT_URL,
            params={
                "symbol": "xau",
            },
            timeout=HTTP_TIMEOUT,
        )

        response.raise_for_status()

        payload = response.json()

        price = None

        if isinstance(payload, dict):
            candidates = [
                payload.get("price"),
                payload.get("last"),
                payload.get("close"),
            ]

            data = payload.get("data")

            if isinstance(data, dict):
                candidates.extend([
                    data.get("price"),
                    data.get("last"),
                    data.get("close"),
                ])

            for candidate in candidates:
                price = safe_float(candidate)

                if price is not None:
                    break

        if price is None:
            raise ValueError(
                f"XAUS spot price not found: {payload}"
            )

        price_cache["price"] = price
        price_cache["time"] = now

        return price

    except Exception as exc:
        print("XAUS spot error:", repr(exc))
        return price_cache["price"]


# ============================================================
# XAUS INTRADAY
# ============================================================

def _extract_intraday_points(payload):
    points = []

    if isinstance(payload, list):
        points = payload

    elif isinstance(payload, dict):

        for key in (
            "data",
            "points",
            "series",
            "prices",
            "result",
        ):
            value = payload.get(key)

            if isinstance(value, list):
                points = value
                break

            if isinstance(value, dict):
                for subkey in (
                    "data",
                    "points",
                    "series",
                    "prices",
                ):
                    subvalue = value.get(subkey)

                    if isinstance(subvalue, list):
                        points = subvalue
                        break

                if points:
                    break

    return points


def _parse_point(point):
    timestamp = None
    price = None

    if isinstance(point, dict):

        timestamp = (
            point.get("timestamp")
            or point.get("time")
            or point.get("datetime")
            or point.get("date")
            or point.get("ts")
        )

        price = (
            point.get("price")
            or point.get("close")
            or point.get("value")
            or point.get("last")
        )

    elif isinstance(point, (list, tuple)):

        if len(point) >= 2:
            timestamp = point[0]
            price = point[1]

    return timestamp, safe_float(price)


def _timestamp_to_datetime(value):
    if value is None:
        return None

    try:
        if isinstance(value, (int, float)):

            number = float(value)

            if number > 10_000_000_000:
                number /= 1000.0

            return datetime.fromtimestamp(
                number,
                tz=timezone.utc,
            )

        text_value = str(value).strip()

        if text_value.isdigit():

            number = float(text_value)

            if number > 10_000_000_000:
                number /= 1000.0

            return datetime.fromtimestamp(
                number,
                tz=timezone.utc,
            )

        parsed = pd.to_datetime(
            text_value,
            utc=True,
            errors="coerce",
        )

        if pd.isna(parsed):
            return None

        return parsed.to_pydatetime()

    except Exception:
        return None


def fetch_intraday():
    try:
        response = requests.get(
            XAUS_INTRADAY_URL,
            params={
                "symbol": "xau",
                "hours": 48,
                "fresh": int(time.time()),
            },
            timeout=HTTP_TIMEOUT,
        )

        response.raise_for_status()

        payload = response.json()

        raw_points = _extract_intraday_points(payload)

        if not raw_points:
            raise ValueError(
                "XAUS intraday returned no points"
            )

        rows = []

        for point in raw_points:

            timestamp, price = _parse_point(point)

            dt = _timestamp_to_datetime(timestamp)

            if dt is None or price is None:
                continue

            rows.append({
                "timestamp": dt,
                "price": price,
            })

        if not rows:
            raise ValueError(
                "XAUS intraday points could not be parsed"
            )

        df = pd.DataFrame(rows)

        df = df.drop_duplicates(
            subset=["timestamp"]
        )

        df = df.sort_values("timestamp")

        df = df.set_index("timestamp")

        df["price"] = pd.to_numeric(
            df["price"],
            errors="coerce",
        )

        df = df.dropna(
            subset=["price"]
        )

        print(
            "XAUS intraday loaded:",
            f"raw={len(raw_points)}",
            f"normalized_2m={len(df)}",
            f"from={df.index.min()}",
            f"to={df.index.max()}",
        )

        return df

    except Exception as exc:
        print(
            "XAUS intraday error:",
            repr(exc),
        )

        return None


# ============================================================
# RESAMPLE
# ============================================================

def resample_ohlc(df, timeframe):
    if df is None or df.empty:
        return pd.DataFrame()

    rule_map = {
        "M5": "5min",
        "M15": "15min",
        "H1": "1h",
        "H4": "4h",
    }

    rule = rule_map[timeframe]

    result = df["price"].resample(rule).ohlc()

    result = result.dropna()

    if result.empty:
        return result

    # Удаляем только реально незакрытую текущую свечу.
    # Не удаляем последнюю свечу вслепую.
    now = pd.Timestamp.now(tz="UTC")

    duration = pd.Timedelta(rule)

    last_open = result.index[-1]

    if last_open + duration > now:
        result = result.iloc[:-1]

    return result


# ============================================================
# INDICATORS
# ============================================================

def add_indicators(df):
    if df is None or df.empty:
        return df

    result = df.copy()

    result["ema20"] = (
        result["close"]
        .ewm(span=20, adjust=False)
        .mean()
    )

    result["ema50"] = (
        result["close"]
        .ewm(span=50, adjust=False)
        .mean()
    )

    result["ema200"] = (
        result["close"]
        .ewm(span=200, adjust=False)
        .mean()
    )

    delta = result["close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    result["rsi"] = 100 - (
        100 / (1 + rs)
    )

    return result


# ============================================================
# TREND
# ============================================================

def trend_score(df):
    if df is None or len(df) < 7:
        return "INSUFFICIENT", 0

    last = df.iloc[-1]

    close = safe_float(last.get("close"))
    ema20 = safe_float(last.get("ema20"))
    ema50 = safe_float(last.get("ema50"))
    ema200 = safe_float(last.get("ema200"))
    rsi = safe_float(last.get("rsi"))

    if close is None:
        return "MIXED", 50

    bullish = 0
    bearish = 0

    if ema20 is not None:
        if close > ema20:
            bullish += 1
        elif close < ema20:
            bearish += 1

    if ema50 is not None:
        if close > ema50:
            bullish += 1
        elif close < ema50:
            bearish += 1

    if ema200 is not None:
        if close > ema200:
            bullish += 1
        elif close < ema200:
            bearish += 1

    if rsi is not None:
        if rsi > 55:
            bullish += 1
        elif rsi < 45:
            bearish += 1

    if bullish >= 3 and bullish > bearish:
        score = min(
            100,
            50 + bullish * 12 - bearish * 5
        )

        return "BULLISH", score

    if bearish >= 3 and bearish > bullish:
        score = min(
            100,
            50 + bearish * 12 - bullish * 5
        )

        return "BEARISH", score

    return "MIXED", 50


def trend_line(name, df):
    trend, score = trend_score(df)

    if trend == "BULLISH":
        icon = "🟢"

    elif trend == "BEARISH":
        icon = "🔴"

    elif trend == "INSUFFICIENT":
        icon = "⚠️"

    else:
        icon = "⚪"

    return (
        f"{icon} {name}: "
        f"{trend} ({score}/100)"
    )


# ============================================================
# STRUCTURE
# ============================================================

def detect_structure(df):
    if df is None or len(df) < 6:
        return "NONE"

    recent = df.tail(6)

    highs = recent["high"]
    lows = recent["low"]

    previous_high = highs.iloc[:-1].max()
    previous_low = lows.iloc[:-1].min()

    current_close = recent["close"].iloc[-1]

    if current_close > previous_high:
        return "BOS_UP"

    if current_close < previous_low:
        return "BOS_DOWN"

    return "NONE"


# ============================================================
# LIQUIDITY
# ============================================================

def detect_liquidity(df):
    if df is None or len(df) < 8:
        return "NONE"

    recent = df.tail(8)

    last = recent.iloc[-1]

    previous_high = recent["high"].iloc[:-1].max()
    previous_low = recent["low"].iloc[:-1].min()

    high = last["high"]
    low = last["low"]
    close = last["close"]

    if (
        high > previous_high
        and close < previous_high
    ):
        return "SELL_SIDE_SWEEP"

    if (
        low < previous_low
        and close > previous_low
    ):
        return "BUY_SIDE_SWEEP"

    return "NONE"


# ============================================================
# FVG
# ============================================================

def detect_fvg(df):
    if df is None or len(df) < 4:
        return "NONE"

    a = df.iloc[-3]
    c = df.iloc[-1]

    if c["low"] > a["high"]:
        return "BULLISH_FVG"

    if c["high"] < a["low"]:
        return "BEARISH_FVG"

    return "NONE"


# ============================================================
# DISPLACEMENT
# ============================================================

def detect_displacement(df):
    if df is None or len(df) < 10:
        return "NONE"

    recent = df.tail(10)

    bodies = (
        recent["close"] - recent["open"]
    ).abs()

    average_body = bodies.iloc[:-1].mean()

    last = recent.iloc[-1]

    body = abs(
        last["close"] - last["open"]
    )

    if average_body <= 0:
        return "NONE"

    if body >= average_body * 1.8:

        if last["close"] > last["open"]:
            return "BULLISH"

        if last["close"] < last["open"]:
            return "BEARISH"

    return "NONE"


# ============================================================
# ZONE
# ============================================================

def detect_zone(df):
    if df is None or len(df) < 5:
        return "NONE"

    last = df.iloc[-1]

    high = last["high"]
    low = last["low"]
    close = last["close"]

    midpoint = (
        high + low
    ) / 2

    if close > midpoint:
        return "PREMIUM"

    if close < midpoint:
        return "DISCOUNT"

    return "EQUILIBRIUM"


# ============================================================
# MTF MARKET DATA
# ============================================================

def build_market_data(force=False):
    now = time.time()

    if (
        not force
        and market_cache["data"] is not None
        and now - market_cache["time"] < CACHE_SECONDS
    ):
        return market_cache["data"]

    intraday = fetch_intraday()

    if intraday is None or intraday.empty:
        return None

    frames = {}

    for timeframe in (
        "M5",
        "M15",
        "H1",
        "H4",
    ):

        frame = resample_ohlc(
            intraday,
            timeframe,
        )

        frame = add_indicators(frame)

        frames[timeframe] = frame

        print(
            f"Market data: "
            f"{timeframe}={len(frame)} "
            f"candles "
            f"(minimum={MIN_CANDLES[timeframe]})"
        )

    # Проверяем обязательные минимумы.
    for timeframe in (
        "M5",
        "M15",
        "H1",
    ):

        minimum = MIN_CANDLES[timeframe]

        if len(frames[timeframe]) < minimum:

            error = (
                f"{timeframe} has "
                f"{len(frames[timeframe])}, "
                f"need {minimum}"
            )

            print(
                "Market data unavailable:",
                error,
            )

            return None

    # H4:
    # 8+ = FULL
    # 2-7 = LIMITED
    # <2 = insufficient

    h4_count = len(frames["H4"])

    if h4_count >= PREFERRED_H4_CANDLES:
        h4_status = "FULL"

    elif h4_count >= MIN_CANDLES["H4"]:
        h4_status = "LIMITED"

    else:
        error = (
            f"H4 has {h4_count}, "
            f"need {MIN_CANDLES['H4']}"
        )

        print(
            "Market data unavailable:",
            error,
        )

        return None

    result = {
        "M5": frames["M5"],
        "M15": frames["M15"],
        "H1": frames["H1"],
        "H4": frames["H4"],
        "_h4_status": h4_status,
        "_h4_count": h4_count,
    }

    market_cache["data"] = result
    market_cache["time"] = now

    return result


# ============================================================
# SMC SNAPSHOT
# ============================================================

def smc_snapshot(df):
    return {
        "liquidity": detect_liquidity(df),
        "bos": detect_structure(df),
        "fvg": detect_fvg(df),
        "displacement": detect_displacement(df),
        "zone": detect_zone(df),
    }


# ============================================================
# HIGHER TF BIAS
# ============================================================

def get_higher_bias(market):
    h4 = market["H4"]
    h1 = market["H1"]
    m15 = market["M15"]

    h4_status = market["_h4_status"]

    h4_trend, _ = trend_score(h4)
    h1_trend, _ = trend_score(h1)
    m15_trend, _ = trend_score(m15)

    if h4_status == "FULL":

        if (
            h4_trend == "BULLISH"
            and h1_trend == "BULLISH"
        ):
            return "BUY"

        if (
            h4_trend == "BEARISH"
            and h1_trend == "BEARISH"
        ):
            return "SELL"

        return "MIXED"

    # LIMITED H4:
    # H1 + M15 become operational higher-TF filter.

    if (
        h1_trend == "BULLISH"
        and m15_trend == "BULLISH"
    ):
        return "BUY"

    if (
        h1_trend == "BEARISH"
        and m15_trend == "BEARISH"
    ):
        return "SELL"

    return "MIXED"


# ============================================================
# SIGNAL SCORE
# ============================================================

def score_signal(market):
    h4 = market["H4"]
    h1 = market["H1"]
    m15 = market["M15"]
    m5 = market["M5"]

    h4_status = market["_h4_status"]

    h4_trend, h4_score = trend_score(h4)
    h1_trend, h1_score = trend_score(h1)
    m15_trend, m15_score = trend_score(m15)
    m5_trend, m5_score = trend_score(m5)

    liquidity = detect_liquidity(m5)
    bos = detect_structure(m5)
    fvg = detect_fvg(m5)
    displacement = detect_displacement(m5)
    zone = detect_zone(m5)

    higher_bias = get_higher_bias(market)

    buy_score = 0
    sell_score = 0

    # --------------------------------------------------------
    # TREND
    # --------------------------------------------------------

    trend_weights = {
        "H4": 20,
        "H1": 15,
        "M15": 15,
        "M5": 10,
    }

    trend_data = {
        "H4": h4_trend,
        "H1": h1_trend,
        "M15": m15_trend,
        "M5": m5_trend,
    }

    for name, trend in trend_data.items():

        weight = trend_weights[name]

        if trend == "BULLISH":
            buy_score += weight

        elif trend == "BEARISH":
            sell_score += weight

    # --------------------------------------------------------
    # SMC
    # --------------------------------------------------------

    if liquidity == "BUY_SIDE_SWEEP":
        buy_score += 15

    elif liquidity == "SELL_SIDE_SWEEP":
        sell_score += 15

    if bos == "BOS_UP":
        buy_score += 15

    elif bos == "BOS_DOWN":
        sell_score += 15

    if fvg == "BULLISH_FVG":
        buy_score += 8

    elif fvg == "BEARISH_FVG":
        sell_score += 8

    if displacement == "BULLISH":
        buy_score += 12

    elif displacement == "BEARISH":
        sell_score += 12

    if zone == "DISCOUNT":
        buy_score += 5

    elif zone == "PREMIUM":
        sell_score += 5

    buy_score = min(100, buy_score)
    sell_score = min(100, sell_score)

    if buy_score > sell_score:
        direction = "BUY"
        score = buy_score

    elif sell_score > buy_score:
        direction = "SELL"
        score = sell_score

    else:
        direction = "WAIT"
        score = max(
            buy_score,
            sell_score,
        )

    # --------------------------------------------------------
    # HIGHER TF FILTER
    # --------------------------------------------------------

    higher_ok = (
        direction in ("BUY", "SELL")
        and higher_bias == direction
    )

    # --------------------------------------------------------
    # M5 CONFIRMATION
    # --------------------------------------------------------

    m5_smc_confirmation = (
        (
            direction == "BUY"
            and (
                liquidity == "BUY_SIDE_SWEEP"
                or bos == "BOS_UP"
                or displacement == "BULLISH"
            )
        )
        or
        (
            direction == "SELL"
            and (
                liquidity == "SELL_SIDE_SWEEP"
                or bos == "BOS_DOWN"
                or displacement == "BEARISH"
            )
        )
    )

    # --------------------------------------------------------
    # FULL SIGNAL
    # --------------------------------------------------------

    full_signal = (
        direction in ("BUY", "SELL")
        and score >= MIN_SCORE
        and higher_ok
        and m5_smc_confirmation
    )

    # --------------------------------------------------------
    # EARLY SIGNAL
    # --------------------------------------------------------

    early_signal = (
        not full_signal
        and direction in ("BUY", "SELL")
        and score >= EARLY_SCORE
        and higher_bias in (
            direction,
            "MIXED",
        )
        and m5_smc_confirmation
    )

    if full_signal:
        signal_type = "FULL"

    elif early_signal:
        signal_type = "EARLY"

    else:
        signal_type = "WAIT"

    return {
        "signal": direction if signal_type != "WAIT" else "WAIT",
        "type": signal_type,
        "score": score,

        "buy_score": buy_score,
        "sell_score": sell_score,

        "higher_bias": higher_bias,

        "h4_status": h4_status,
        "h4_count": market["_h4_count"],

        "h4_trend": h4_trend,
        "h4_score": h4_score,

        "h1_trend": h1_trend,
        "h1_score": h1_score,

        "m15_trend": m15_trend,
        "m15_score": m15_score,

        "m5_trend": m5_trend,
        "m5_score": m5_score,

        "liquidity": liquidity,
        "bos": bos,
        "fvg": fvg,
        "displacement": displacement,
        "zone": zone,
    }


# ============================================================
# ENTRY / SL / TP
# ============================================================

def build_trade(price, direction):
    if direction == "BUY":

        entry = price

        sl = price - SL_DISTANCE

        tp1 = price + TP_DISTANCE * 0.50

        tp2 = price + TP_DISTANCE

    else:

        entry = price

        sl = price + SL_DISTANCE

        tp1 = price - TP_DISTANCE * 0.50

        tp2 = price - TP_DISTANCE

    return {
        "direction": direction,
        "entry": price_round(entry),
        "sl": price_round(sl),
        "tp1": price_round(tp1),
        "tp2": price_round(tp2),
        "rr": 3.0,
    }


# ============================================================
# VIRTUAL TRADE
# ============================================================

def open_virtual_trade(signal):
    global virtual_trade

    if virtual_trade is not None:
        return False

    price = fetch_spot()

    if price is None:
        return False

    trade = build_trade(
        price,
        signal["signal"],
    )

    trade.update({
        "opened_at": time.time(),
        "score": signal["score"],
        "be": False,
    })

    virtual_trade = trade

    stats["open"] = 1
    save_stats()

    print(
        "Virtual trade OPEN:",
        trade,
    )

    return True


def close_virtual_trade(result):
    global virtual_trade

    if virtual_trade is None:
        return

    virtual_trade = None

    stats["open"] = 0
    stats["closed"] += 1

    if result == "WIN":
        stats["wins"] += 1

    elif result == "LOSS":
        stats["losses"] += 1

    elif result == "BE":
        stats["breakeven"] += 1

    save_stats()


def update_virtual_trade(price):
    global virtual_trade

    if virtual_trade is None:
        return

    direction = virtual_trade["direction"]

    entry = virtual_trade["entry"]
    sl = virtual_trade["sl"]
    tp2 = virtual_trade["tp2"]

    # --------------------------------------------------------
    # BUY
    # --------------------------------------------------------

    if direction == "BUY":

        move = price - entry

        # BE
        if (
            not virtual_trade["be"]
            and move >= BE_TRIGGER
        ):
            virtual_trade["sl"] = entry
            virtual_trade["be"] = True

            print(
                "Virtual BUY moved to BE"
            )

        if price <= virtual_trade["sl"]:

            if virtual_trade["be"]:
                close_virtual_trade("BE")
            else:
                close_virtual_trade("LOSS")

            return

        if price >= tp2:

            close_virtual_trade("WIN")

            return

    # --------------------------------------------------------
    # SELL
    # --------------------------------------------------------

    else:

        move = entry - price

        # BE
        if (
            not virtual_trade["be"]
            and move >= BE_TRIGGER
        ):
            virtual_trade["sl"] = entry
            virtual_trade["be"] = True

            print(
                "Virtual SELL moved to BE"
            )

        if price >= virtual_trade["sl"]:

            if virtual_trade["be"]:
                close_virtual_trade("BE")
            else:
                close_virtual_trade("LOSS")

            return

        if price <= tp2:

            close_virtual_trade("WIN")

            return


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def format_signal_message(signal, price):
    signal_type = signal["type"]
    direction = signal["signal"]
    score = signal["score"]

    if signal_type == "FULL":
        header = "🟢 SIGNAL: BUY"

        if direction == "SELL":
            header = "🔴 SIGNAL: SELL"

    elif signal_type == "EARLY":
        header = "🟡 EARLY SIGNAL: BUY"

        if direction == "SELL":
            header = "🟠 EARLY SIGNAL: SELL"

    else:
        header = "⚪ SIGNAL: WAIT"

    lines = [
        f"🥇 {APP_NAME}",
        "",
        header,
        "",
        f"💰 XAUUSD: {price:.2f}",
        "",
        trend_line(
            "H4",
            market_cache["data"]["H4"],
        ),
        trend_line(
            "H1",
            market_cache["data"]["H1"],
        ),
        trend_line(
            "M15",
            market_cache["data"]["M15"],
        ),
        trend_line(
            "M5",
            market_cache["data"]["M5"],
        ),
        "",
        (
            f"🧭 HIGHER TF BIAS: "
            f"{signal['higher_bias']}"
        ),
        (
            f"📊 SCORE: "
            f"{score}/100"
        ),
        "",
        f"💧 Liquidity: {signal['liquidity']}",
        f"🔨 BOS: {signal['bos']}",
        f"🧩 FVG: {signal['fvg']}",
        f"💥 Displacement: {signal['displacement']}",
        f"📍 Zone: {signal['zone']}",
        "",
        (
            f"🕯 H4 DATA: "
            f"{signal['h4_status']} "
            f"({signal['h4_count']} candles)"
        ),
    ]

    if (
        signal_type in ("FULL", "EARLY")
        and direction in ("BUY", "SELL")
    ):

        trade = build_trade(
            price,
            direction,
        )

        lines.extend([
            "",
            "━━━━━━━━━━━━━━━━━━",
            f"📌 ENTRY: {trade['entry']:.2f}",
            f"🛑 SL: {trade['sl']:.2f}",
            f"🎯 TP1: {trade['tp1']:.2f}",
            f"🎯 TP2: {trade['tp2']:.2f}",
            f"📐 RR: 1:{trade['rr']:.0f}",
            f"📉 Risk: {RISK_PERCENT:.1f}%",
            "",
            "🛡 AUTO TRADING: OFF",
        ])

    else:

        lines.extend([
            "",
            "🛡 AUTO TRADING: OFF",
            "⏳ Ждём подтверждение структуры.",
        ])

    return "\n".join(lines)


# ============================================================
# SIGNAL PROCESSING
# ============================================================

def process_signal(send_message=True):
    global last_signal_time
    global last_signal_key
    global last_market_error
    global last_market_error_time

    price = fetch_spot()

    if price is None:

        error = (
            "❌ Не удалось получить текущую "
            "цену XAU/USD."
        )

        if (
            time.time() - last_market_error_time
            > 300
        ):
            telegram_send(error)
            last_market_error = error
            last_market_error_time = time.time()

        return None

    market = build_market_data()

    if market is None:

        error = (
            "⚠️ SIGNAL: WAIT\n\n"
            "❌ Не удалось получить "
            "достаточно данных XAU/USD.\n\n"
            "Проверь Render Logs.\n"
            "Бот НЕ создаёт искусственные свечи."
        )

        print(error)

        if (
            send_message
            and time.time() - last_market_error_time
            > 300
        ):
            telegram_send(error)

            last_market_error = error
            last_market_error_time = time.time()

        return None

    signal = score_signal(market)

    print(
        f"Signal engine: "
        f"{signal['type']} "
        f"{signal['signal']} "
        f"score={signal['score']} "
        f"H4={signal['h4_status']} "
        f"Higher={signal['higher_bias']}"
    )

    # --------------------------------------------------------
    # WAIT
    # --------------------------------------------------------

    if signal["type"] == "WAIT":

        if send_message:

            message = format_signal_message(
                signal,
                price,
            )

            # /signal должен получать ответ всегда.
            telegram_send(message)

        return signal

    # --------------------------------------------------------
    # SIGNAL KEY
    # --------------------------------------------------------

    signal_key = (
        f"{signal['type']}_"
        f"{signal['signal']}_"
        f"{signal['score']}"
    )

    now = time.time()

    # --------------------------------------------------------
    # COOLDOWN
    # --------------------------------------------------------

    if (
        now - last_signal_time
        < COOLDOWN_MIN * 60
    ):

        print(
            "Signal skipped by cooldown:",
            signal_key,
        )

        return signal

    # --------------------------------------------------------
    # DUPLICATE
    # --------------------------------------------------------

    if signal_key == last_signal_key:

        print(
            "Signal skipped duplicate:",
            signal_key,
        )

        return signal

    last_signal_time = now
    last_signal_key = signal_key

    stats["signals_total"] += 1

    if signal["type"] == "FULL":
        stats["full_signals"] += 1

    elif signal["type"] == "EARLY":
        stats["early_signals"] += 1

    save_stats()

    message = format_signal_message(
        signal,
        price,
    )

    if send_message:
        telegram_send(message)

    # Только FULL создаёт виртуальную сделку.
    if (
        signal["type"] == "FULL"
        and virtual_trade is None
    ):
        open_virtual_trade(signal)

    return signal


# ============================================================
# STATUS MESSAGE
# ============================================================

def build_status():
    price = fetch_spot()

    market = market_cache.get("data")

    if market:

        h4_count = market["_h4_count"]
        h4_status = market["_h4_status"]

        data_status = (
            f"🕯 H4: {h4_status} "
            f"({h4_count})"
        )

    else:
       
