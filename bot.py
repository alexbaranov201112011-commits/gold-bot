import os
import json
import time
import hmac
import hashlib
import tempfile
import threading
import traceback
import functools
from collections import deque
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd
from flask import Flask, request


# ============================================================
# 🥇 GOLD SMART V6.0
# XAU/USD SMART SMC SIGNAL ENGINE
#
# H4 → H1 → M15 → M5
#
# COMPRESSION
# → LIQUIDITY
# → SWEEP
# → BOS / CHoCH
# → FVG
# → DISPLACEMENT
# → MOMENTUM
# → SIGNAL
#
# AUTO TRADING = OFF
# ============================================================

APP_NAME = "GOLD SMART V6.0"

XAUS_SPOT_URL = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY_URL = "https://xaus.com/api/v1/intraday"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    ""
).strip()

AUTO_TRADING = False

# ============================================================
# SETTINGS
# ============================================================

RISK_PERCENT = 1.0
DEFAULT_DEPOSIT = 800.0

MIN_SCORE = 70
EARLY_SCORE = 60
HIGH_CONFIDENCE_SCORE = 85

SL_DISTANCE = 5.0
TP_DISTANCE = 15.0
BE_TRIGGER = 6.0

POLL_SECONDS = 60
COOLDOWN_MIN = 15
CACHE_SECONDS = 45

CLOSED_CANDLES = True
INTRADAY_HOURS = 48

MIN_CANDLES = {
    "M5": 30,
    "M15": 16,
    "H1": 12,
    "H4": 8,
}

EARLY_TRACKING = True
MAX_EARLY_OPEN = 10

KEEPALIVE = os.getenv(
    "KEEPALIVE",
    "0"
) == "1"

KEEPALIVE_SECONDS = 600

BOOT_NOTICE = os.getenv(
    "BOOT_NOTICE",
    "1"
) == "1"

STATS_FILE = os.getenv(
    "STATS_FILE",
    "gold_stats.json"
)

TRADE_FILE = os.getenv(
    "TRADE_FILE",
    "gold_trade.json"
)

LOCK_FILE = os.path.join(
    tempfile.gettempdir(),
    "gold_smart_v6.lock"
)

MAX_TRADES_HISTORY = 500

WEBHOOK_SECRET = hashlib.sha256(
    ("gold-smart:" + BOT_TOKEN).encode()
).hexdigest()[:48]


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# STATE
# ============================================================

price_cache = {
    "price": None,
    "time": 0,
}

intraday_cache = {
    "df": None,
    "time": 0,
    "last_ts": None,
}

state_lock = threading.RLock()
file_lock = threading.Lock()
spot_lock = threading.Lock()
intraday_lock = threading.Lock()

recent_updates = deque(maxlen=300)

engine_state = {
    "started": None,
    "last_cycle": None,
    "data_age": None,
    "last_error": None,
}

stats = {}

virtual_trade = None
early_trades = []

engine_started = False
engine_thread = None
keepalive_thread = None

last_full_signal_time = 0
last_early_signal_time = 0
last_signal_key = None

_engine_lock_handle = None


# ============================================================
# HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat()


def safe_float(value, default=np.nan):
    try:
        if value is None:
            return default

        if isinstance(value, str):
            value = value.replace(",", "").strip()

        return float(value)

    except Exception:
        return default


def fmt_price(value):
    value = safe_float(value)

    if not np.isfinite(value):
        return "N/A"

    return f"{value:.2f}"


def side_emoji(side):
    if side == "BUY":
        return "🟢"

    if side == "SELL":
        return "🔴"

    return "⚪"


def locked(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with state_lock:
            return fn(*args, **kwargs)

    return wrapper


# ============================================================
# JSON
# ============================================================

def atomic_write_json(path, data):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    fd, tmp = tempfile.mkstemp(
        prefix=".gold-smart-",
        suffix=".tmp",
        dir=directory
    )

    try:
        with os.fdopen(
            fd,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )

            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp, path)

    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def load_json(path, default):
    try:
        if not os.path.exists(path):
            return default

        with open(
            path,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)

    except Exception:
        return default


def save_stats():
    with file_lock:
        atomic_write_json(
            STATS_FILE,
            stats
        )


def load_stats():
    global stats

    default = {
        "full_signals": 0,
        "early_signals": 0,
        "wins": 0,
        "losses": 0,
        "be": 0,
        "closed": 0,
        "profit": 0.0,
        "last_signal": None,
        "history": [],
    }

    loaded = load_json(
        STATS_FILE,
        default
    )

    if not isinstance(loaded, dict):
        loaded = default

    for key, value in default.items():
        if key not in loaded:
            loaded[key] = value

    if not isinstance(
        loaded.get("history"),
        list
    ):
        loaded["history"] = []

    stats = loaded


def save_trade_state():
    with file_lock:
        atomic_write_json(
            TRADE_FILE,
            {
                "virtual_trade": virtual_trade,
                "early_trades": early_trades,
            }
        )


def load_trade_state():
    global virtual_trade
    global early_trades

    data = load_json(
        TRADE_FILE,
        {
            "virtual_trade": None,
            "early_trades": [],
        }
    )

    virtual_trade = data.get(
        "virtual_trade"
    )

    early_trades = data.get(
        "early_trades",
        []
    )

    if not isinstance(
        early_trades,
        list
    ):
        early_trades = []


# ============================================================
# TELEGRAM
# ============================================================

def telegram_configured():
    return bool(
        BOT_TOKEN and CHAT_ID
    )


def telegram_api(method):
    return (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )


def send_telegram(
    message,
    chat_id=None
):
    if not BOT_TOKEN:
        return False

    target = str(
        chat_id or CHAT_ID
    ).strip()

    if not target:
        return False

    try:
        response = requests.post(
            telegram_api("sendMessage"),
            json={
                "chat_id": target,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=15
        )

        return response.ok

    except Exception:
        return False


def configure_webhook():
    if not BOT_TOKEN:
        return False

    if not RENDER_EXTERNAL_URL:
        return False

    url = (
        RENDER_EXTERNAL_URL.rstrip("/")
        + "/telegram"
    )

    try:
        response = requests.post(
            telegram_api("setWebhook"),
            json={
                "url": url,
                "secret_token": WEBHOOK_SECRET,
                "drop_pending_updates": False,
            },
            timeout=15
        )

        return response.ok

    except Exception:
        return False


# ============================================================
# XAUS
# ============================================================

def parse_timestamp(value):
    try:
        if value is None:
            return None

        if isinstance(
            value,
            (int, float)
        ):
            number = float(value)

            if number > 10_000_000_000:
                return pd.to_datetime(
                    number,
                    unit="ms",
                    utc=True
                )

            return pd.to_datetime(
                number,
                unit="s",
                utc=True
            )

        return pd.to_datetime(
            value,
            utc=True
        )

    except Exception:
        return None


def extract_price(payload):
    if not isinstance(
        payload,
        dict
    ):
        return np.nan

    candidates = []

    xau = payload.get("xau")

    if isinstance(xau, dict):
        candidates.extend([
            xau.get("price"),
            xau.get("spot_usd_oz"),
            xau.get("spot"),
        ])

    candidates.extend([
        payload.get("price"),
        payload.get("spot_usd_oz"),
        payload.get("spot"),
    ])

    data = payload.get("data")

    if isinstance(data, dict):
        candidates.extend([
            data.get("price"),
            data.get("spot_usd_oz"),
        ])

        nested = data.get("xau")

        if isinstance(
            nested,
            dict
        ):
            candidates.extend([
                nested.get("price"),
                nested.get("spot_usd_oz"),
            ])

    for value in candidates:
        price = safe_float(value)

        if (
            np.isfinite(price)
            and price > 100
        ):
            return price

    return np.nan


def fetch_spot(force=False):
    with spot_lock:

        now = time.time()

        if (
            not force
            and price_cache["price"] is not None
            and now - price_cache["time"]
            < CACHE_SECONDS
        ):
            return (
                price_cache["price"],
                price_cache["time"]
            )

        try:
            response = requests.get(
                XAUS_SPOT_URL,
                headers={
                    "User-Agent":
                        "GOLD-SMART/6.0",
                    "Accept":
                        "application/json",
                },
                timeout=15
            )

            response.raise_for_status()

            payload = response.json()

            price = extract_price(
                payload
            )

            if np.isfinite(price):

                price_cache["price"] = float(
                    price
                )

                price_cache["time"] = now

                return (
                    float(price),
                    now
                )

        except Exception:
            pass

        return (
            price_cache["price"],
            price_cache["time"]
        )


def find_points(payload):
    if not isinstance(
        payload,
        dict
    ):
        return []

    keys = (
        "points",
        "data",
        "series",
        "prices",
        "items",
    )

    for key in keys:

        value = payload.get(key)

        if isinstance(
            value,
            list
        ):
            return value

        if isinstance(
            value,
            dict
        ):

            for nested_key in keys:

                nested = value.get(
                    nested_key
                )

                if isinstance(
                    nested,
                    list
                ):
                    return nested

    return []


def parse_intraday(payload):
    points = find_points(payload)

    rows = []

    for point in points:

        if not isinstance(
            point,
            dict
        ):
            continue

        timestamp = None

        for key in (
            "t",
            "timestamp",
            "time",
            "date",
            "datetime",
        ):

            if key in point:

                timestamp = parse_timestamp(
                    point.get(key)
                )

                if timestamp is not None:
                    break

        if timestamp is None:
            continue

        price = np.nan

        for key in (
            "p",
            "price",
            "close",
            "value",
        ):

            if key in point:

                price = safe_float(
                    point.get(key)
                )

                if np.isfinite(price):
                    break

        if not np.isfinite(price):
            continue

        rows.append({
            "timestamp": timestamp,
            "price": float(price),
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    df = df.drop_duplicates(
        "timestamp"
    )

    df = df.sort_values(
        "timestamp"
    )

    df = df.set_index(
        "timestamp"
    )

    df["price"] = pd.to_numeric(
        df["price"],
        errors="coerce"
    )

    return df.dropna(
        subset=["price"]
    )


def fetch_intraday(force=False):
    with intraday_lock:

        now = time.time()

        if (
            not force
            and intraday_cache["df"] is not None
            and now - intraday_cache["time"]
            < CACHE_SECONDS
        ):
            return (
                intraday_cache["df"],
                intraday_cache["time"]
            )

        # ====================================================
        # IMPORTANT:
        # URL MUST use + between variable and strings.
        # ====================================================

        url = (
            XAUS_INTRADAY_URL
            + "?symbol=xau"
            + f"&hours={INTRADAY_HOURS}"
            + f"&fresh={int(now)}"
        )

        try:

            response = requests.get(
                url,
                headers={
                    "User-Agent":
                        "GOLD-SMART/6.0",
                    "Accept":
                        "application/json",
                },
                timeout=20
            )

            response.raise_for_status()

            raw = parse_intraday(
                response.json()
            )

            if raw.empty:
                return (
                    intraday_cache["df"],
                    intraday_cache["time"]
                )

            intraday_cache["df"] = raw
            intraday_cache["time"] = now

            try:
                intraday_cache["last_ts"] = (
                    raw.index[-1].isoformat()
                )
            except Exception:
                pass

            return (
                raw,
                now
            )

        except Exception as exc:

            engine_state["last_error"] = (
                f"XAUS intraday: {exc}"
            )

            return (
                intraday_cache["df"],
                intraday_cache["time"]
            )


# ============================================================
# OHLC
# ============================================================

def resample_ohlc(
    price_df,
    timeframe
):

    if (
        price_df is None
        or price_df.empty
    ):
        return pd.DataFrame()

    rules = {
        "M5": "5min",
        "M15": "15min",
        "H1": "1h",
        "H4": "4h",
    }

    rule = rules.get(
        timeframe
    )

    if not rule:
        return pd.DataFrame()

    result = (
        price_df["price"]
        .resample(rule)
        .ohlc()
        .dropna()
    )

    if (
        CLOSED_CANDLES
        and len(result) > 1
    ):
        result = result.iloc[:-1]

    return result


def add_indicators(df):
    data = df.copy()

    if data.empty:
        return data

    close = data["close"]

    data["ema20"] = close.ewm(
        span=20,
        adjust=False
    ).mean()

    data["ema50"] = close.ewm(
        span=50,
        adjust=False
    ).mean()

    data["ema200"] = close.ewm(
        span=200,
        adjust=False
    ).mean()

    delta = close.diff()

    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )

    avg_gain = gain.rolling(
        14
    ).mean()

    avg_loss = loss.rolling(
        14
    ).mean()

    rs = (
        avg_gain
        / avg_loss.replace(
            0,
            np.nan
        )
    )

    data["rsi"] = (
        100
        - 100 / (1 + rs)
    ).fillna(50)

    tr1 = data["high"] - data["low"]

    tr2 = (
        data["high"]
        - data["close"].shift()
    ).abs()

    tr3 = (
        data["low"]
        - data["close"].shift()
    ).abs()

    tr = pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)

    data["atr"] = tr.rolling(
        14
    ).mean()

    return data


# ============================================================
# PIVOTS
# ============================================================

def pivot_highs(
    df,
    window=2
):

    if (
        df is None
        or len(df) < window * 2 + 3
    ):
        return []

    values = df["high"].values
    result = []

    for i in range(
        window,
        len(df) - window
    ):

        left = values[
            i-window:i
        ]

        right = values[
            i+1:i+window+1
        ]

        if (
            values[i] > np.max(left)
            and values[i] >= np.max(right)
        ):

            result.append({
                "index": i,
                "time": df.index[i],
                "price": float(values[i])
            })

    return result


def pivot_lows(
    df,
    window=2
):

    if (
        df is None
        or len(df) < window * 2 + 3
    ):
        return []

    values = df["low"].values
    result = []

    for i in range(
        window,
        len(df) - window
    ):

        left = values[
            i-window:i
        ]

        right = values[
            i+1:i+window+1
        ]

        if (
            values[i] < np.min(left)
            and values[i] <= np.min(right)
        ):

            result.append({
                "index": i,
                "time": df.index[i],
                "price": float(values[i])
            })

    return result


# ============================================================
# TREND
# ============================================================

def trend_score(df):

    if (
        df is None
        or len(df) < 5
    ):
        return {
            "trend": "MIXED",
            "score": 50
        }

    data = add_indicators(df)

    last = data.iloc[-1]

    bullish = 0
    bearish = 0

    close = safe_float(
        last["close"]
    )

    ema20 = safe_float(
        last["ema20"]
    )

    ema50 = safe_float(
        last["ema50"]
    )

    ema200 = safe_float(
        last["ema200"]
    )

    if (
        np.isfinite(ema20)
        and close > ema20
    ):
        bullish += 1
    else:
        bearish += 1

    if (
        np.isfinite(ema50)
        and close > ema50
    ):
        bullish += 1
    else:
        bearish += 1

    if (
        np.isfinite(ema200)
        and close > ema200
    ):
        bullish += 1
    else:
        bearish += 1

    if len(data) >= 4:

        move = (
            data["close"].iloc[-1]
            - data["close"].iloc[-4]
        )

        if move > 0:
            bullish += 1

        elif move < 0:
            bearish += 1

    if bullish > bearish:

        score = min(
            100,
            50 + bullish * 12
        )

        return {
            "trend": "BULLISH",
            "score": score
        }

    if bearish > bullish:

        score = min(
            100,
            50 + bearish * 12
        )

        return {
            "trend": "BEARISH",
            "score": score
        }

    return {
        "trend": "MIXED",
        "score": 50
    }


# ============================================================
# LIQUIDITY
# ============================================================

def detect_liquidity_zones(df):

    highs = pivot_highs(
        df,
        2
    )

    lows = pivot_lows(
        df,
        2
    )

    ssl = None
    bsl = None

    if lows:
        ssl = lows[-1]["price"]

    if highs:
        bsl = highs[-1]["price"]

    return {
        "ssl": ssl,
        "bsl": bsl
    }


def detect_liquidity_sweep(df):

    result = {
        "direction": "NONE",
        "label": "NONE",
        "price": np.nan,
        "level": np.nan,
    }

    if (
        df is None
        or len(df) < 8
    ):
        return result

    last = df.iloc[-1]

    previous = df.iloc[-6:-1]

    prev_low = float(
        previous["low"].min()
    )

    prev_high = float(
        previous["high"].max()
    )

    if (
        float(last["low"]) < prev_low
        and float(last["close"]) > prev_low
    ):

        return {
            "direction": "BULLISH",
            "label": "SSL SWEEP",
            "price": float(last["low"]),
            "level": prev_low
        }

    if (
        float(last["high"]) > prev_high
        and float(last["close"]) < prev_high
    ):

        return {
            "direction": "BEARISH",
            "label": "BSL SWEEP",
            "price": float(last["high"]),
            "level": prev_high
        }

    return result


# ============================================================
# BOS / CHoCH
# ============================================================

def structure_confirmation(df):

    result = {
        "direction": "NONE",
        "label": "NONE"
    }

    if (
        df is None
        or len(df) < 10
    ):
        return result

    highs = pivot_highs(
        df,
        2
    )

    lows = pivot_lows(
        df,
        2
    )

    close = float(
        df["close"].iloc[-1]
    )

    if highs:

        last_high = highs[-1]["price"]

        if close > last_high:

            return {
                "direction": "BULLISH",
                "label": "BOS BULLISH"
            }

    if lows:

        last_low = lows[-1]["price"]

        if close < last_low:

            return {
                "direction": "BEARISH",
                "label": "BOS BEARISH"
            }

    return result


# ============================================================
# FVG
# ============================================================

def detect_fvg(df):

    if (
        df is None
        or len(df) < 4
    ):
        return "NONE"

    a = df.iloc[-3]
    c = df.iloc[-1]

    if (
        float(c["low"])
        > float(a["high"])
    ):
        return "BULLISH"

    if (
        float(c["high"])
        < float(a["low"])
    ):
        return "BEARISH"

    return "NONE"


# ============================================================
# DISPLACEMENT
# ============================================================

def detect_displacement(df):

    if (
        df is None
        or len(df) < 10
    ):
        return "NONE"

    data = add_indicators(df)

    last = data.iloc[-1]

    atr = safe_float(
        last["atr"]
    )

    if (
        not np.isfinite(atr)
        or atr <= 0
    ):
        return "NONE"

    body = abs(
        float(last["close"])
        - float(last["open"])
    )

    if body < atr * 1.10:
        return "NONE"

    if last["close"] > last["open"]:
        return "BULLISH"

    if last["close"] < last["open"]:
        return "BEARISH"

    return "NONE"


# ============================================================
# MOMENTUM
# ============================================================

def momentum_state(df):

    if (
        df is None
        or len(df) < 6
    ):
        return "NEUTRAL"

    change = (
        float(df["close"].iloc[-1])
        - float(df["close"].iloc[-6])
    )

    data = add_indicators(df)

    atr = safe_float(
        data["atr"].iloc[-1]
    )

    if (
        np.isfinite(atr)
        and atr > 0
    ):

        if change > atr * 0.35:
            return "BULLISH"

        if change < -atr * 0.35:
            return "BEARISH"

    if change > 0:
        return "BULLISH"

    if change < 0:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# PREMIUM / DISCOUNT
# ============================================================

def premium_discount(df):

    if (
        df is None
        or len(df) < 5
    ):
        return "EQUILIBRIUM"

    high = float(
        df["high"].tail(30).max()
    )

    low = float(
        df["low"].tail(30).min()
    )

    price = float(
        df["close"].iloc[-1]
    )

    if high <= low:
        return "EQUILIBRIUM"

    midpoint = (
        high + low
    ) / 2

    if price < midpoint:
        return "DISCOUNT"

    if price > midpoint:
        return "PREMIUM"

    return "EQUILIBRIUM"


# ============================================================
# COMPRESSION
# ============================================================

def detect_compression(df):

    result = {
        "direction": "NONE",
        "label": "NONE",
        "quality": 0
    }

    if (
        df is None
        or len(df) < 20
    ):
        return result

    data = df.tail(50).copy()

    highs = pivot_highs(
        data,
        2
    )

    lows = pivot_lows(
        data,
        2
    )

    current = float(
        data["close"].iloc[-1]
    )

    # Descending highs = bullish compression
    if len(highs) >= 2:

        h1 = highs[-2]["price"]
        h2 = highs[-1]["price"]

        if h2 < h1:

            distance_now = (
                h2 - current
            )

            previous = float(
                data["close"].iloc[-6]
            )

            distance_previous = (
                h1 - previous
            )

            if (
                distance_now > 0
                and distance_previous > distance_now
            ):

                return {
                    "direction": "BULLISH",
                    "label":
                        "BULLISH COMPRESSION",
                    "quality": 90
                }

    # Ascending lows = bearish compression
    if len(lows) >= 2:

        l1 = lows[-2]["price"]
        l2 = lows[-1]["price"]

        if l2 > l1:

            distance_now = (
                current - l2
            )

            previous = float(
                data["close"].iloc[-6]
            )

            distance_previous = (
                previous - l1
            )

            if (
                distance_now > 0
                and distance_previous > distance_now
            ):

                return {
                    "direction": "BEARISH",
                    "label":
                        "BEARISH COMPRESSION",
                    "quality": 90
                }

    return result


# ============================================================
# TIMEFRAMES
# ============================================================

def build_timeframes(raw):

    frames = {}

    if (
        raw is None
        or raw.empty
    ):
        return frames

    for tf in (
        "M5",
        "M15",
        "H1",
        "H4"
    ):

        frame = resample_ohlc(
            raw,
            tf
        )

        if frame.empty:
            continue

        if len(frame) < MIN_CANDLES[tf]:
            continue

        frames[tf] = add_indicators(
            frame
        )

    return frames


def higher_tf_bias(frames):

    h4 = frames.get("H4")
    h1 = frames.get("H1")

    if (
        h4 is None
        or h1 is None
    ):
        return "MIXED"

    h4_trend = trend_score(
        h4
    )["trend"]

    h1_trend = trend_score(
        h1
    )["trend"]

    if (
        h4_trend == "BULLISH"
        and h1_trend == "BULLISH"
    ):
        return "BULLISH"

    if (
        h4_trend == "BEARISH"
        and h1_trend == "BEARISH"
    ):
        return "BEARISH"

    return "MIXED"


def side_structure_score(
    side,
    frames
):

    score = 0

    for tf in (
        "H4",
        "H1",
        "M15",
        "M5"
    ):

        frame = frames.get(tf)

        if frame is None:
            continue

        trend = trend_score(
            frame
        )["trend"]

        if (
            side == "BUY"
            and trend == "BULLISH"
        ):
            score += 5

        elif (
            side == "SELL"
            and trend == "BEARISH"
        ):
            score += 5

    return min(
        score,
        20
    )


# ============================================================
# SETUP
# ============================================================

def evaluate_setup(
    frames,
    price
):

    m5 = frames.get("M5")
    m15 = frames.get("M15")
    h1 = frames.get("H1")
    h4 = frames.get("H4")

    if any(
        x is None
        for x in (
            m5,
            m15,
            h1,
            h4
        )
    ):

        return {
            "signal": "WAIT",
            "side": "NONE",
            "score": 0,
            "buy_score": 0,
            "sell_score": 0,
            "reason":
                "Недостаточно данных"
        }

    bias = higher_tf_bias(
        frames
    )

    h4_trend = trend_score(
        h4
    )["trend"]

    h1_trend = trend_score(
        h1
    )["trend"]

    m15_trend = trend_score(
        m15
    )["trend"]

    m5_trend = trend_score(
        m5
    )["trend"]

    zones = detect_liquidity_zones(
        m5
    )

    ssl = zones["ssl"]
    bsl = zones["bsl"]

    sweep = detect_liquidity_sweep(
        m5
    )

    confirmation = structure_confirmation(
        m5
    )

    fvg = detect_fvg(
        m5
    )

    displacement = detect_displacement(
        m5
    )

    momentum = momentum_state(
        m5
    )

    pd_zone = premium_discount(
        m5
    )

    compression_h1 = detect_compression(
        h1
    )

    compression_m15 = detect_compression(
        m15
    )

    compression = compression_h1

    if compression["direction"] == "NONE":
        compression = compression_m15

    buy = 0
    sell = 0

    # ========================================================
    # Higher TF = 15
    # ========================================================

    if bias == "BULLISH":
        buy += 15

    elif bias == "BEARISH":
        sell += 15

    # ========================================================
    # Structure = 20
    # ========================================================

    buy += side_structure_score(
        "BUY",
        frames
    )

    sell += side_structure_score(
        "SELL",
        frames
    )

    # ========================================================
    # Compression = 15
    # ========================================================

    if compression["direction"] == "BULLISH":
        buy += 15

    elif compression["direction"] == "BEARISH":
        sell += 15

    # ========================================================
    # Liquidity = 15
    # ========================================================

    if ssl is not None:
        buy += 15

    if bsl is not None:
        sell += 15

    # ========================================================
    # Sweep = 15
    # ========================================================

    if sweep["direction"] == "BULLISH":
        buy += 15

    elif sweep["direction"] == "BEARISH":
        sell += 15

    # ========================================================
    # BOS = 10
    # ========================================================

    if confirmation["direction"] == "BULLISH":
        buy += 10

    elif confirmation["direction"] == "BEARISH":
        sell += 10

    # ========================================================
    # FVG + displacement = 10
    # ========================================================

    if fvg == "BULLISH":
        buy += 5

    elif fvg == "BEARISH":
        sell += 5

    if displacement == "BULLISH":
        buy += 5

    elif displacement == "BEARISH":
        sell += 5

    buy = min(
        100,
        buy
    )

    sell = min(
        100,
        sell
    )

    # ========================================================
    # FULL
    # ========================================================

    buy_full = (
        buy >= MIN_SCORE
        and bias == "BULLISH"
        and compression["direction"]
            == "BULLISH"
        and ssl is not None
        and sweep["direction"]
            == "BULLISH"
        and confirmation["direction"]
            == "BULLISH"
    )

    sell_full = (
        sell >= MIN_SCORE
        and bias == "BEARISH"
        and compression["direction"]
            == "BEARISH"
        and bsl is not None
        and sweep["direction"]
            == "BEARISH"
        and confirmation["direction"]
            == "BEARISH"
    )

    # ========================================================
    # EARLY
    # ========================================================

    buy_early = (
        buy >= EARLY_SCORE
        and compression["direction"]
            == "BULLISH"
        and ssl is not None
        and sweep["direction"]
            == "BULLISH"
        and confirmation["direction"]
            != "BULLISH"
    )

    sell_early = (
        sell >= EARLY_SCORE
        and compression["direction"]
            == "BEARISH"
        and bsl is not None
        and sweep["direction"]
            == "BEARISH"
        and confirmation["direction"]
            != "BEARISH"
    )

    # ========================================================
    # DECISION
    # ========================================================

    if buy_full and buy > sell:

        signal = "BUY"
        side = "BUY"
        score = buy

    elif sell_full and sell > buy:

        signal = "SELL"
        side = "SELL"
        score = sell

    elif buy_early and buy > sell:

        signal = "EARLY BUY"
        side = "BUY"
        score = buy

    elif sell_early and sell > buy:

        signal = "EARLY SELL"
        side = "SELL"
        score = sell

    else:

        signal = "WAIT"
        side = "NONE"
        score = max(
            buy,
            sell
        )

    return {
        "signal": signal,
        "side": side,
        "score": score,
        "buy_score": buy,
        "sell_score": sell,
        "price": float(price),
        "bias": bias,
        "h4_trend": h4_trend,
        "h1_trend": h1_trend,
        "m15_trend": m15_trend,
        "m5_trend": m5_trend,
        "ssl": ssl,
        "bsl": bsl,
        "sweep": sweep["label"],
        "confirmation":
            confirmation["label"],
        "fvg": fvg,
        "displacement": displacement,
        "momentum": momentum,
        "pd_zone": pd_zone,
        "compression":
            compression["label"],
        "reason":
            "SMC V6.0"
    }


# ============================================================
# LOT
# ============================================================

def calculate_lot(
    deposit=DEFAULT_DEPOSIT,
    risk_percent=RISK_PERCENT,
    sl_distance=SL_DISTANCE
):

    deposit = safe_float(
        deposit,
        DEFAULT_DEPOSIT
    )

    risk_percent = safe_float(
        risk_percent,
        RISK_PERCENT
    )

    sl_distance = safe_float(
        sl_distance,
        SL_DISTANCE
    )

    if (
        deposit <= 0
        or sl_distance <= 0
    ):
        return 0.01

    risk_money = (
        deposit
        * risk_percent
        / 100
    )

    # Approximate XAUUSD sizing.
    # Intentionally capped for conservative manual trading.
    lot = (
        risk_money
        / (sl_distance * 100)
    )

    lot = round(
        lot,
        2
    )

    lot = max(
        0.01,
        lot
    )

    lot = min(
        0.02,
        lot
    )

    return lot


# ============================================================
# SIGNAL KEY
# ============================================================

def make_signal_key(signal):

    return (
        signal.get("signal"),
        round(
            safe_float(
                signal.get("price"),
                0
            ),
            2
        ),
        signal.get("bias"),
        signal.get("compression"),
        signal.get("sweep"),
        signal.get("confirmation"),
    )


def signal_allowed(signal):

    global last_full_signal_time
    global last_early_signal_time
    global last_signal_key

    signal_type = signal.get(
        "signal",
        "WAIT"
    )

    if signal_type == "WAIT":
        return False

    now = time.time()

    key = make_signal_key(
        signal
    )

    if key == last_signal_key:
        return False

    if signal_type in (
        "BUY",
        "SELL"
    ):

        if (
            now - last_full_signal_time
            < COOLDOWN_MIN * 60
        ):
            return False

    else:

        if (
            now - last_early_signal_time
            < COOLDOWN_MIN * 60
        ):
            return False

    return True


def mark_signal_sent(signal):

    global last_full_signal_time
    global last_early_signal_time
    global last_signal_key

    now = time.time()

    signal_type = signal.get(
        "signal"
    )

    if signal_type in (
        "BUY",
        "SELL"
    ):
        last_full_signal_time = now

    else:
        last_early_signal_time = now

    last_signal_key = make_signal_key(
        signal
    )


# ============================================================
# MESSAGE
# ============================================================

def build_signal_message(
    signal
):

    side = signal["side"]
    signal_type = signal["signal"]

    price = safe_float(
        signal["price"]
    )

    sl = (
        price - SL_DISTANCE
        if side == "BUY"
        else price + SL_DISTANCE
    )

    tp1 = (
        price + TP_DISTANCE / 2
        if side == "BUY"
        else price - TP_DISTANCE / 2
    )

    tp2 = (
        price + TP_DISTANCE
        if side == "BUY"
        else price - TP_DISTANCE
    )

    lot = calculate_lot()

    confidence = min(
        99,
        max(
            50,
            int(
                signal["score"]
            )
        )
    )

    if signal_type in (
        "BUY",
        "SELL"
    ):
        header = (
            f"{side_emoji(side)} "
            f"SIGNAL: {signal_type}"
        )
    else:
        header = (
            f"⚡ {side_emoji(side)} "
            f"{signal_type}"
        )

    message = (
        f"🥇 {APP_NAME}\n\n"
        f"{header}\n\n"
        f"💰 XAUUSD: {fmt_price(price)}\n"
        f"📊 Score: {signal['score']}/100\n"
        f"🎯 Confidence: {confidence}%\n\n"
        f"📊 H4: {signal['h4_trend']}\n"
        f"📊 H1: {signal['h1_trend']}\n"
        f"📊 M15: {signal['m15_trend']}\n"
        f"📊 M5: {signal['m5_trend']}\n\n"
        f"💧 Liquidity: "
        f"SSL {fmt_price(signal['ssl'])} | "
        f"BSL {fmt_price(signal['bsl'])}\n"
        f"🔨 Structure: "
        f"{signal['confirmation']}\n"
        f"💦 Sweep: {signal['sweep']}\n"
        f"🧩 Compression: "
        f"{signal['compression']}\n"
        f"📦 FVG: {signal['fvg']}\n"
        f"🚀 Displacement: "
        f"{signal['displacement']}\n"
        f"📈 Momentum: "
        f"{signal['momentum']}\n"
        f"⚖️ Zone: "
        f"{signal['pd_zone']}\n\n"
        f"📍 Entry: {fmt_price(price)}\n"
        f"🛑 SL: {fmt_price(sl)}\n"
        f"🎯 TP1: {fmt_price(tp1)}\n"
        f"🎯 TP2: {fmt_price(tp2)}\n"
        f"📐 RR: 1:3\n"
        f"📦 Lot: {lot:.2f}\n\n"
        f"🛡 AUTO TRADING: OFF\n"
        f"✋ MANUAL SIGNAL ONLY\n\n"
        f"⏱ {now_utc().strftime('%Y-%m-%d %H:%M:%S')} UTC"
    )

    return message


# ============================================================
# STATS
# ============================================================

def register_signal(signal):

    signal_type = signal.get(
        "signal"
    )

    if signal_type in (
        "BUY",
        "SELL"
    ):
        stats["full_signals"] = (
            stats.get("full_signals", 0)
            + 1
        )

    elif signal_type in (
        "EARLY BUY",
        "EARLY SELL"
    ):
        stats["early_signals"] = (
            stats.get("early_signals", 0)
            + 1
        )

    stats["last_signal"] = {
        "time": now_iso(),
        "signal": signal_type,
        "price": signal.get("price"),
        "score": signal.get("score"),
    }

    history = stats.setdefault(
        "history",
        []
    )

    history.append({
        "time": now_iso(),
        "signal": signal_type,
        "side": signal.get("side"),
        "price": signal.get("price"),
        "score": signal.get("score"),
    })

    if len(history) > MAX_TRADES_HISTORY:
        del history[
            :-MAX_TRADES_HISTORY
        ]

    save_stats()


def stats_message():

    full = stats.get(
        "full_signals",
        0
    )

    early = stats.get(
        "early_signals",
        0
    )

    wins = stats.get(
        "wins",
        0
    )

    losses = stats.get(
        "losses",
        0
    )

    be = stats.get(
        "be",
        0
    )

    closed = stats.get(
        "closed",
        0
    )

    profit = safe_float(
        stats.get(
            "profit",
            0
        ),
        0
    )

    total = wins + losses

    if total > 0:
        winrate = (
            wins
            / total
            * 100
        )
    else:
        winrate = 0

    return (
        f"📊 {APP_NAME}\n\n"
        f"🟢 FULL signals: {full}\n"
        f"⚡ EARLY signals: {early}\n\n"
        f"✅ Wins: {wins}\n"
        f"❌ Losses: {losses}\n"
        f"🛡 BE: {be}\n"
        f"📁 Closed: {closed}\n"
        f"📈 Winrate: {winrate:.1f}%\n"
        f"💰 Virtual P/L: {profit:.2f}\n\n"
        f"🤖 AUTO TRADING: OFF"
    )


# ============================================================
# VIRTUAL TRADE
# ============================================================

def open_virtual_trade(
    signal
):

    global virtual_trade
    global early_trades

    side = signal.get(
        "side"
    )

    if side not in (
        "BUY",
        "SELL"
    ):
        return

    entry = safe_float(
        signal.get("price")
    )

    if not np.isfinite(entry):
        return

    sl = (
        entry - SL_DISTANCE
        if side == "BUY"
        else entry + SL_DISTANCE
    )

    tp = (
        entry + TP_DISTANCE
        if side == "BUY"
        else entry - TP_DISTANCE
    )

    trade = {
        "id": f"{int(time.time())}-{side}",
        "signal": signal.get("signal"),
        "side": side,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "initial_sl": sl,
        "be": False,
        "opened_at": now_iso(),
    }

    if signal.get(
        "signal"
    ) in (
        "BUY",
        "SELL"
    ):

        virtual_trade = trade

    elif EARLY_TRACKING:

        early_trades.append(
            trade
        )

        if len(early_trades) > MAX_EARLY_OPEN:
            early_trades = (
                early_trades[
                    -MAX_EARLY_OPEN:
                ]
            )

    save_trade_state()


def check_virtual_trade(
    price
):

    global virtual_trade
    global early_trades

    price = safe_float(
        price
    )

    if not np.isfinite(price):
        return

    # ========================================================
    # MAIN TRADE
    # ========================================================

    if virtual_trade:

        trade = virtual_trade

        side = trade["side"]
        entry = safe_float(
            trade["entry"]
        )

        current_sl = safe_float(
            trade["sl"]
        )

        tp = safe_float(
            trade["tp"]
        )

        moved = (
            price - entry
            if side == "BUY"
            else entry - price
        )

        # BE
        if (
            not trade.get("be", False)
            and moved >= BE_TRIGGER
        ):

            trade["sl"] = entry
            trade["be"] = True

            stats["be"] = (
                stats.get("be", 0)
                + 1
            )

            save_trade_state()

            send_telegram(
                f"🛡 {APP_NAME}\n\n"
                f"🔒 BREAK EVEN\n\n"
                f"{side_emoji(side)} {side}\n"
                f"💰 Entry: {fmt_price(entry)}\n"
                f"📍 Current: {fmt_price(price)}\n"
                f"➡️ SL moved to BE: "
                f"{fmt_price(entry)}\n\n"
                f"AUTO TRADING: OFF"
            )

            current_sl = entry

        hit_sl = (
            price <= current_sl
            if side == "BUY"
            else price >= current_sl
        )

        hit_tp = (
            price >= tp
            if side == "BUY"
            else price <= tp
        )

        if hit_tp:

            profit = abs(
                tp - entry
            )

            stats["wins"] = (
                stats.get("wins", 0)
                + 1
            )

            stats["closed"] = (
                stats.get("closed", 0)
                + 1
            )

            stats["profit"] = (
                stats.get("profit", 0.0)
                + profit
            )

            send_telegram(
                f"🎯 {APP_NAME}\n\n"
                f"✅ TP HIT\n\n"
                f"{side_emoji(side)} {side}\n"
                f"Entry: {fmt_price(entry)}\n"
                f"Exit: {fmt_price(price)}\n"
                f"📈 Virtual P/L: +{profit:.2f}\n\n"
                f"AUTO TRADING: OFF"
            )

            virtual_trade = None
            save_stats()
            save_trade_state()

        elif hit_sl:

            result = (
                "BE"
                if trade.get("be", False)
                else "LOSS"
            )

            distance = (
                price - entry
                if side == "BUY"
                else entry - price
            )

            if result == "LOSS":
                stats["losses"] = (
                    stats.get("losses", 0)
                    + 1
                )
            else:
                stats["be"] = (
                    stats.get("be", 0)
                    + 1
                )

            stats["closed"] = (
                stats.get("closed", 0)
                + 1
            )

            stats["profit"] = (
                stats.get("profit", 0.0)
                + distance
            )

            emoji = (
                "🛡"
                if result == "BE"
                else "❌"
            )

            send_telegram(
                f"{emoji} {APP_NAME}\n\n"
                f"{result}\n\n"
                f"{side_emoji(side)} {side}\n"
                f"Entry: {fmt_price(entry)}\n"
                f"Exit: {fmt_price(price)}\n"
                f"Virtual P/L: "
                f"{distance:+.2f}\n\n"
                f"AUTO TRADING: OFF"
            )

            virtual_trade = None
            save_stats()
            save_trade_state()

    # ========================================================
    # EARLY TRADES
    # ========================================================

    remaining = []

    for trade in early_trades:

        side = trade["side"]

        entry = safe_float(
            trade["entry"]
        )

        sl = safe_float(
            trade["sl"]
        )

        tp = safe_float(
            trade["tp"]
        )

        hit_sl = (
            price <= sl
            if side == "BUY"
            else price >= sl
        )

        hit_tp = (
            price >= tp
            if side == "BUY"
            else price <= tp
        )

        if hit_tp:

            stats["wins"] = (
                stats.get("wins", 0)
                + 1
            )

            stats["closed"] = (
                stats.get("closed", 0)
                + 1
            )

            remaining.append(
                None
            )

        elif hit_sl:

            stats["losses"] = (
                stats.get("losses", 0)
                + 1
            )

            stats["closed"] = (
                stats.get("closed", 0)
                + 1
            )

        else:
            remaining.append(
                trade
            )

    early_trades = [
        x for x in remaining
        if x is not None
    ]

    save_trade_state()
    save_stats()


# ============================================================
# ENGINE
# ============================================================

def engine_cycle():

    try:

        price, price_time = fetch_spot()

        if (
            price is None
            or not np.isfinite(
                safe_float(price)
            )
        ):
            return

        raw, raw_time = fetch_intraday()

        if (
            raw is None
            or raw.empty
        ):
            return

        frames = build_timeframes(
            raw
        )

        if not all(
            tf in frames
            for tf in (
                "M5",
                "M15",
                "H1",
                "H4"
            )
        ):

            engine_state["last_error"] = (
                "Недостаточно свечей "
                "для H4/H1/M15/M5"
            )

            return

        signal = evaluate_setup(
            frames,
            price
        )

        engine_state["last_cycle"] = now_iso()

        if raw_time:
            engine_state["data_age"] = (
                time.time() - raw_time
            )

        engine_state["last_error"] = None

        # ====================================================
        # Virtual trade management
        # ====================================================

        check_virtual_trade(
            price
        )

        # ====================================================
        # SIGNAL
        # ====================================================

        if signal.get(
            "signal"
        ) == "WAIT":
            return

        if not signal_allowed(
            signal
        ):
            return

        message = build_signal_message(
            signal
        )

        sent = send_telegram(
            message
        )

        if sent:

            mark_signal_sent(
                signal
            )

            register_signal(
                signal
            )

            open_virtual_trade(
                signal
            )

    except Exception as exc:

        engine_state["last_error"] = (
            f"{type(exc).__name__}: {exc}"
        )

        traceback.print_exc()


def acquire_engine_lock():

    global _engine_lock_handle

    try:

        handle = open(
            LOCK_FILE,
            "a+"
        )

        try:
            import fcntl

            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB
            )

            _engine_lock_handle = handle

            return True

        except ImportError:
            _engine_lock_handle = handle
            return True

        except BlockingIOError:
            handle.close()
            return False

    except Exception:
        return True


def engine_loop():

    global engine_started

    engine_started = True
    engine_state["started"] = now_iso()

    while True:

        try:
            engine_cycle()

        except Exception as exc:

            engine_state["last_error"] = (
                f"ENGINE: {exc}"
            )

            traceback.print_exc()

        time.sleep(
            POLL_SECONDS
        )


def start_engine():

    global engine_thread

    if engine_thread is not None:
        return

    if not acquire_engine_lock():
        print(
            "GOLD SMART: "
            "another engine instance is running"
        )
        return

    engine_thread = threading.Thread(
        target=engine_loop,
        daemon=True,
        name="gold-smart-engine"
    )

    engine_thread.start()

    print(
        f"{APP_NAME}: engine started"
    )


# ============================================================
# KEEPALIVE
# ============================================================

def keepalive_loop():

    while True:

        try:

            if RENDER_EXTERNAL_URL:

                requests.get(
                    RENDER_EXTERNAL_URL.rstrip("/")
                    + "/health",
                    timeout=15
                )

        except Exception:
            pass

        time.sleep(
            KEEPALIVE_SECONDS
        )


def start_keepalive():

    global keepalive_thread

    if not KEEPALIVE:
        return

    if not RENDER_EXTERNAL_URL:
        return

    if keepalive_thread is not None:
        return

    keepalive_thread = threading.Thread(
        target=keepalive_loop,
        daemon=True,
        name="gold-smart-keepalive"
    )

    keepalive_thread.start()


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def command_start():
    return (
        f"🥇 {APP_NAME}\n\n"
        f"XAU/USD Smart SMC Signal Engine\n\n"
        f"📊 H4 → H1 → M15 → M5\n"
        f"💧 Liquidity + Sweep\n"
        f"🔨 BOS / CHoCH\n"
        f"🧩 Compression\n"
        f"📦 FVG\n"
        f"🚀 Displacement\n\n"
        f"🤖 AUTO TRADING: OFF\n"
        f"✋ Manual signals only\n\n"
        f"Команды:\n"
        f"/start — запуск\n"
        f"/status — состояние\n"
        f"/signal — текущий анализ\n"
        f"/test — тест Telegram\n"
        f"/stats — статистика\n"
        f"/help — помощь"
    )


def command_status():

    price, price_time = fetch_spot()

    last_cycle = (
        engine_state.get(
            "last_cycle"
        )
        or "нет"
    )

    error = (
        engine_state.get(
            "last_error"
        )
        or "нет"
    )

    data_age = engine_state.get(
        "data_age"
    )

    if data_age is None:
        age_text = "N/A"
    else:
        age_text = (
            f"{data_age:.0f} сек"
        )

    trade_text = (
        "нет"
        if virtual_trade is None
        else (
            f"{virtual_trade.get('side')} "
            f"@ {fmt_price(virtual_trade.get('entry'))}"
        )
    )

    return (
        f"🥇 {APP_NAME}\n\n"
        f"🟢 Engine: "
        f"{'ON' if engine_started else 'OFF'}\n"
        f"🤖 Auto Trading: OFF\n\n"
        f"💰 XAUUSD: "
        f"{fmt_price(price)}\n"
        f"📡 Data age: {age_text}\n"
        f"🔄 Last cycle: {last_cycle}\n\n"
        f"📌 Virtual trade: {trade_text}\n\n"
        f"⚠️ Error: {error}"
    )


def command_signal():

    price, _ = fetch_spot(
        force=True
    )

    raw, _ = fetch_intraday(
        force=True
    )

    if (
        price is None
        or raw is None
        or raw.empty
    ):

        return (
            f"🥇 {APP_NAME}\n\n"
            f"⚪ SIGNAL: WAIT\n\n"
            f"❌ XAUS data unavailable."
        )

    frames = build_timeframes(
        raw
    )

    signal = evaluate_setup(
        frames,
        price
    )

    if signal.get(
        "signal"
    ) == "WAIT":

        return (
            f"🥇 {APP_NAME}\n\n"
            f"⚪ SIGNAL: WAIT\n\n"
            f"💰 XAUUSD: "
            f"{fmt_price(price)}\n"
            f"📊 BUY score: "
            f"{signal.get('buy_score', 0)}/100\n"
            f"📊 SELL score: "
            f"{signal.get('sell_score', 0)}/100\n\n"
            f"Причина: "
            f"{signal.get('reason', 'WAIT')}\n\n"
            f"🤖 AUTO TRADING: OFF"
        )

    return build_signal_message(
        signal
    )


def command_help():
    return (
        f"🥇 {APP_NAME}\n\n"
        f"/start — информация\n"
        f"/status — состояние двигателя\n"
        f"/signal — текущий анализ XAUUSD\n"
        f"/test — тест Telegram\n"
        f"/stats — статистика\n"
        f"/help — список команд\n\n"
        f"Сигналы формируются автоматически.\n"
        f"Реальные сделки НЕ открываются."
    )


# ============================================================
# TELEGRAM UPDATE
# ============================================================

def process_telegram_update(update):

    if not isinstance(
        update,
        dict
    ):
        return

    message = update.get(
        "message"
    )

    if not isinstance(
        message,
        dict
    ):
        return

    chat = message.get(
        "chat"
    )

    if not isinstance(
        chat,
        dict
    ):
        return

    chat_id = str(
        chat.get(
            "id",
            ""
        )
    )

    text = str(
        message.get(
            "text",
            ""
        )
    ).strip().lower()

    if not chat_id:
        return

    if text.startswith("/start"):
        reply = command_start()

    elif text.startswith("/status"):
        reply = command_status()

    elif text.startswith("/signal"):
        reply = command_signal()

    elif text.startswith("/test"):
        reply = (
            f"🧪 {APP_NAME}\n\n"
            f"✅ Telegram connection OK\n"
            f"🤖 AUTO TRADING: OFF\n"
            f"⏱ {now_iso()}"
        )

    elif text.startswith("/stats"):
        reply = stats_message()

    elif text.startswith("/help"):
        reply = command_help()

    else:
        reply = (
            "Неизвестная команда.\n"
            "Используй /help"
        )

    send_telegram(
        reply,
        chat_id
    )


# ============================================================
# FLASK ROUTES
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():

    return {
        "app": APP_NAME,
        "status": "online",
        "auto_trading": False,
        "engine": engine_started,
        "time": now_iso(),
    }


@app.route(
    "/health",
    methods=["GET"]
)
def health():

    return {
        "status": "ok",
        "app": APP_NAME,
        "engine": engine_started,
        "auto_trading": False,
        "last_cycle":
            engine_state.get(
                "last_cycle"
            ),
        "last_error":
            engine_state.get(
                "last_error"
            ),
    }


@app.route(
    "/signal",
    methods=["GET"]
)
def signal_endpoint():

    try:
        price, _ = fetch_spot(
            force=True
        )

        raw, _ = fetch_intraday(
            force=True
        )

        if (
            price is None
            or raw is None
            or raw.empty
        ):
            return {
                "signal": "WAIT",
                "reason":
                    "XAUS data unavailable",
                "auto_trading": False,
            }

        frames = build_timeframes(
            raw
        )

        signal = evaluate_setup(
            frames,
            price
        )

        signal["auto_trading"] = False

        return signal

    except Exception as exc:

        return {
            "signal": "WAIT",
            "error": str(exc),
            "auto_trading": False,
        }, 500


@app.route(
    "/telegram",
    methods=["POST"]
)
def telegram_webhook():

    if BOT_TOKEN:

        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            ""
        )

        if not hmac.compare_digest(
            received_secret,
            WEBHOOK_SECRET
        ):
            return {
                "ok": False
            }, 403

    try:

        update = request.get_json(
            silent=True
        )

        if update:
            process_telegram_update(
                update
            )

        return {
            "ok": True
        }

    except Exception as exc:

        traceback.print_exc()

        return {
            "ok": False,
            "error": str(exc)
        }, 500


# ============================================================
# STARTUP
# ============================================================

def startup():

    load_stats()
    load_trade_state()

    start_engine()
    start_keepalive()

    if BOT_TOKEN:
        configure_webhook()

    if (
        BOOT_NOTICE
        and telegram_configured()
    ):

        send_telegram(
            f"🥇 {APP_NAME}\n\n"
            f"🟢 BOT ONLINE\n\n"
            f"📊 H4 → H1 → M15 → M5\n"
            f"💧 Liquidity / Sweep\n"
            f"🔨 BOS / CHoCH\n"
            f"🧩 Compression\n"
            f"📦 FVG\n"
            f"🚀 Displacement\n\n"
            f"🤖 AUTO TRADING: OFF\n"
            f"✋ Manual signals only\n\n"
            f"⏱ {now_iso()}"
        )


# ============================================================
# RUN STARTUP ON IMPORT
# ============================================================

startup()


# ============================================================
# LOCAL RUN
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
        port=port,
        threaded=True
    )
