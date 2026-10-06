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
# V6.0:
# COMPRESSION → LIQUIDITY ZONE → SWEEP → BOS/CHoCH
# → FVG / DISPLACEMENT → SIGNAL
#
# AUTO TRADING = OFF
# ============================================================

APP_NAME = "GOLD SMART V6.0"

XAUS_SPOT_URL = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY_URL = "https://xaus.com/api/v1/intraday"

XAUS_USER_AGENT = os.getenv(
    "XAUS_USER_AGENT",
    "Mozilla/5.0 (compatible; GOLD-SMART/6.0)"
)

HTTP_HEADERS = {
    "User-Agent": XAUS_USER_AGENT,
    "Accept": "application/json",
}

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()


# ============================================================
# ⚙️ SETTINGS
# ============================================================

AUTO_TRADING = False

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

SPOT_MAX_AGE = 300
INTRADAY_MAX_CACHE_AGE = 600
STALE_SECONDS = 30 * 60

EARLY_TRACKING = True
MAX_EARLY_OPEN = 10

KEEPALIVE = os.getenv("KEEPALIVE", "0") == "1"
KEEPALIVE_SECONDS = 600

BOOT_NOTICE = os.getenv("BOOT_NOTICE", "1") == "1"

STATS_FILE = os.getenv("STATS_FILE", "gold_stats.json")

TRADE_FILE = os.getenv(
    "TRADE_FILE",
    os.path.join(
        os.path.dirname(STATS_FILE) or ".",
        "gold_trade.json"
    )
)

LOCK_FILE = os.path.join(
    tempfile.gettempdir(),
    "gold_smart_engine.lock"
)

MAX_TRADES_HISTORY = 500

WEBHOOK_SECRET = hashlib.sha256(
    ("gold-smart:" + BOT_TOKEN).encode()
).hexdigest()[:48]


# ============================================================
# APP
# ============================================================

app = Flask(__name__)


# ============================================================
# GLOBAL STATE
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
spot_lock = threading.Lock()
intraday_lock = threading.Lock()
file_lock = threading.Lock()
updates_lock = threading.Lock()

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

last_full_signal_time = 0
last_early_signal_time = 0
last_signal_key = None

_engine_lock_handle = None


# ============================================================
# BASIC HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat()


def locked(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with state_lock:
            return fn(*args, **kwargs)

    return wrapper


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


# ============================================================
# JSON STORAGE
# ============================================================

def atomic_write_json(path, data):
    directory = os.path.dirname(path) or "."

    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        prefix=".gold-smart-",
        suffix=".tmp",
        dir=directory,
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
                indent=2,
            )

            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, path)

    finally:
        if os.path.exists(tmp_path):

            try:
                os.remove(tmp_path)

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


def save_virtual_trade():

    with file_lock:

        atomic_write_json(
            TRADE_FILE,
            {
                "virtual_trade": virtual_trade,
                "early_trades": early_trades,
            }
        )


def load_virtual_trade():

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
    text_message,
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
            telegram_api(
                "sendMessage"
            ),
            json={
                "chat_id": target,
                "text": text_message,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )

        return bool(response.ok)

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
            telegram_api(
                "setWebhook"
            ),
            json={
                "url": url,
                "secret_token": WEBHOOK_SECRET,
                "drop_pending_updates": False,
            },
            timeout=15,
        )

        return bool(response.ok)

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


def extract_price_from_spot(payload):

    if not isinstance(
        payload,
        dict
    ):
        return np.nan

    candidates = []

    xau = payload.get("xau")

    if isinstance(
        xau,
        dict
    ):

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

    if isinstance(
        data,
        dict
    ):

        candidates.extend([
            data.get("price"),
            data.get("spot_usd_oz"),
        ])

        xau_data = data.get("xau")

        if isinstance(
            xau_data,
            dict
        ):

            candidates.extend([
                xau_data.get("price"),
                xau_data.get("spot_usd_oz"),
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

        now_ts = time.time()

        if (
            not force
            and price_cache["price"] is not None
            and (
                now_ts
                - price_cache["time"]
                < CACHE_SECONDS
            )
        ):

            return (
                price_cache["price"],
                price_cache["time"],
            )

        try:

            response = requests.get(
                XAUS_SPOT_URL,
                headers=HTTP_HEADERS,
                timeout=15,
            )

            response.raise_for_status()

            payload = response.json()

            price = extract_price_from_spot(
                payload
            )

            if np.isfinite(price):

                price_cache["price"] = float(
                    price
                )

                price_cache["time"] = now_ts

                return (
                    float(price),
                    now_ts,
                )

        except Exception:
            pass

        return (
            price_cache["price"],
            price_cache["time"],
        )


def find_points(payload):

    if not isinstance(
        payload,
        dict
    ):
        return []

    for key in (
        "points",
        "data",
        "series",
        "prices",
        "items",
    ):

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

            for nested_key in (
                "points",
                "data",
                "series",
                "prices",
                "items",
            ):

                nested = value.get(
                    nested_key
                )

                if isinstance(
                    nested,
                    list
                ):
                    return nested

    return []


def parse_intraday_payload(payload):

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

    df = df.dropna(
        subset=["price"]
    )

    return df


def resample_ohlc(
    price_df,
    timeframe
):

    if (
        price_df is None
        or price_df.empty
    ):
        return pd.DataFrame()

    rule_map = {
        "M5": "5min",
        "M15": "15min",
        "H1": "1h",
        "H4": "4h",
    }

    rule = rule_map.get(
        timeframe
    )

    if not rule:
        return pd.DataFrame()

    series = price_df[
        "price"
    ].copy()

    result = series.resample(
        rule
    ).ohlc()

    result = result.dropna()

    if (
        CLOSED_CANDLES
        and len(result) > 1
    ):
        result = result.iloc[:-1]

    return result


def fetch_intraday(force=False):

    with intraday_lock:

        now_ts = time.time()

        if (
            not force
            and intraday_cache["df"]
            is not None
            and (
                now_ts
                - intraday_cache["time"]
                < CACHE_SECONDS
            )
        ):

            return (
                intraday_cache["df"],
                intraday_cache["time"],
            )

        url = (
            XAUS_INTRADAY_URL
            "?symbol=xau"
            f"&hours={INTRADAY_HOURS}"
            f"&fresh={int(now_ts)}"
        )

        try:

            response = requests.get(
                url,
                headers=HTTP_HEADERS,
                timeout=20,
            )

            response.raise_for_status()

            payload = response.json()

            raw = parse_intraday_payload(
                payload
            )

            if raw.empty:
                return (
                    intraday_cache["df"],
                    intraday_cache["time"],
                )

            intraday_cache["df"] = raw
            intraday_cache["time"] = now_ts

            try:

                intraday_cache["last_ts"] = (
                    raw.index[-1].isoformat()
                )

            except Exception:

                intraday_cache["last_ts"] = None

            return (
                raw,
                now_ts,
            )

        except Exception:

            return (
                intraday_cache["df"],
                intraday_cache["time"],
            )


# ============================================================
# INDICATORS
# ============================================================

def add_indicators(df):

    data = df.copy()

    if data.empty:
        return data

    close = data["close"]

    data["ema20"] = (
        close.ewm(
            span=20,
            adjust=False
        ).mean()
    )

    data["ema50"] = (
        close.ewm(
            span=50,
            adjust=False
        ).mean()
    )

    data["ema200"] = (
        close.ewm(
            span=200,
            adjust=False
        ).mean()
    )

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
        - (
            100
            / (1 + rs)
        )
    )

    data["rsi"] = data[
        "rsi"
    ].fillna(50)

    tr1 = (
        data["high"]
        - data["low"]
    )

    tr2 = (
        data["high"]
        - data["close"].shift()
    ).abs()

    tr3 = (
        data["low"]
        - data["close"].shift()
    ).abs()

    tr = pd.concat(
        [
            tr1,
            tr2,
            tr3,
        ],
        axis=1
    ).max(axis=1)

    data["atr"] = (
        tr.rolling(14).mean()
    )

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
        or len(df)
        < window * 2 + 3
    ):
        return []

    highs = df["high"].values

    result = []

    for i in range(
        window,
        len(df) - window
    ):

        left = highs[
            i - window:i
        ]

        right = highs[
            i + 1:
            i + window + 1
        ]

        if (
            highs[i] > np.max(left)
            and highs[i] >= np.max(right)
        ):

            result.append({
                "index": i,
                "time": df.index[i],
                "price": float(
                    highs[i]
                ),
            })

    return result


def pivot_lows(
    df,
    window=2
):

    if (
        df is None
        or len(df)
        < window * 2 + 3
    ):
        return []

    lows = df["low"].values

    result = []

    for i in range(
        window,
        len(df) - window
    ):

        left = lows[
            i - window:i
        ]

        right = lows[
            i + 1:
            i + window + 1
        ]

        if (
            lows[i] < np.min(left)
            and lows[i] <= np.min(right)
        ):

            result.append({
                "index": i,
                "time": df.index[i],
                "price": float(
                    lows[i]
                ),
            })

    return result


# ============================================================
# STRUCTURE
# ============================================================

def structure_state(df):

    if (
        df is None
        or len(df) < 8
    ):

        return {
            "trend": "MIXED",
            "score": 50,
            "last_high": np.nan,
            "last_low": np.nan,
            "prev_high": np.nan,
            "prev_low": np.nan,
        }

    highs = pivot_highs(df)
    lows = pivot_lows(df)

    if (
        len(highs) < 2
        or len(lows) < 2
    ):

        return {
            "trend": "MIXED",
            "score": 50,
            "last_high": (
                highs[-1]["price"]
                if highs else np.nan
            ),
            "last_low": (
                lows[-1]["price"]
                if lows else np.nan
            ),
            "prev_high": (
                highs[-2]["price"]
                if len(highs) >= 2
                else np.nan
            ),
            "prev_low": (
                lows[-2]["price"]
                if len(lows) >= 2
                else np.nan
            ),
        }

    last_high = highs[-1]["price"]
    prev_high = highs[-2]["price"]

    last_low = lows[-1]["price"]
    prev_low = lows[-2]["price"]

    bullish = (
        last_high > prev_high
        and last_low > prev_low
    )

    bearish = (
        last_high < prev_high
        and last_low < prev_low
    )

    if bullish:

        trend = "BULLISH"
        score = 80

    elif bearish:

        trend = "BEARISH"
        score = 80

    else:

        trend = "MIXED"
        score = 50

    return {
        "trend": trend,
        "score": score,
        "last_high": last_high,
        "last_low": last_low,
        "prev_high": prev_high,
        "prev_low": prev_low,
    }


# ============================================================
# BOS
# ============================================================

def detect_bos(
    df,
    lookback=8
):

    if (
        df is None
        or len(df)
        < lookback + 3
    ):
        return "NONE"

    recent = df.iloc[
        -lookback - 1:-1
    ]

    current = df.iloc[-1]

    previous_high = recent[
        "high"
    ].max()

    previous_low = recent[
        "low"
    ].min()

    if (
        current["close"]
        > previous_high
    ):
        return "BULLISH"

    if (
        current["close"]
        < previous_low
    ):
        return "BEARISH"

    return "NONE"


# ============================================================
# CHoCH
# ============================================================

def detect_choch(df):

    if (
        df is None
        or len(df) < 12
    ):
        return "NONE"

    structure = structure_state(
        df
    )

    recent = df.iloc[-6:]

    previous = df.iloc[-12:-6]

    if previous.empty:
        return "NONE"

    prev_high = previous[
        "high"
    ].max()

    prev_low = previous[
        "low"
    ].min()

    current_close = (
        recent["close"].iloc[-1]
    )

    if (
        structure["trend"] == "BEARISH"
        and current_close > prev_high
    ):
        return "BULLISH"

    if (
        structure["trend"] == "BULLISH"
        and current_close < prev_low
    ):
        return "BEARISH"

    return "NONE"


def structure_confirmation(df):

    bos = detect_bos(df)

    choch = detect_choch(df)

    if (
        bos == "BULLISH"
        or choch == "BULLISH"
    ):

        return {
            "direction": "BULLISH",
            "label": "BOS/CHoCH BULLISH",
            "bos": bos,
            "choch": choch,
        }

    if (
        bos == "BEARISH"
        or choch == "BEARISH"
    ):

        return {
            "direction": "BEARISH",
            "label": "BOS/CHoCH BEARISH",
            "bos": bos,
            "choch": choch,
        }

    return {
        "direction": "NONE",
        "label": "NONE",
        "bos": bos,
        "choch": choch,
    }


# ============================================================
# TREND SCORE
# ============================================================

def trend_score(df):

    if (
        df is None
        or len(df) < 8
    ):

        return {
            "trend": "MIXED",
            "score": 50,
            "structure": "NONE",
            "momentum": "NEUTRAL",
            "bos": "NONE",
        }

    data = add_indicators(df)

    structure = structure_state(
        data
    )

    score = 50

    if structure["trend"] == "BULLISH":
        score += 15

    elif structure["trend"] == "BEARISH":
        score -= 15

    last = data.iloc[-1]

    if (
        np.isfinite(last["ema20"])
        and np.isfinite(last["ema50"])
    ):

        if last["ema20"] > last["ema50"]:
            score += 8

        elif last["ema20"] < last["ema50"]:
            score -= 8

    if (
        np.isfinite(last["ema50"])
        and np.isfinite(last["ema200"])
    ):

        if last["ema50"] > last["ema200"]:
            score += 10

        elif last["ema50"] < last["ema200"]:
            score -= 10

    momentum = "NEUTRAL"

    if len(data) >= 5:

        change = float(
            data["close"].iloc[-1]
            - data["close"].iloc[-5]
        )

        if change > 0:

            score += 8
            momentum = "BULLISH"

        elif change < 0:

            score -= 8
            momentum = "BEARISH"

    bos = detect_bos(data)

    if bos == "BULLISH":
        score += 12

    elif bos == "BEARISH":
        score -= 12

    score = int(
        max(
            0,
            min(100, score)
        )
    )

    if score >= 65:
        trend = "BULLISH"

    elif score <= 35:
        trend = "BEARISH"

    else:
        trend = "MIXED"

    return {
        "trend": trend,
        "score": score,
        "structure": structure["trend"],
        "momentum": momentum,
        "bos": bos,
    }


# ============================================================
# LIQUIDITY ZONES
# ============================================================

def cluster_zone(
    values,
    tolerance
):

    values = [
        safe_float(v)
        for v in values
    ]

    values = [
        v for v in values
        if np.isfinite(v)
    ]

    if not values:
        return None

    values = sorted(values)

    clusters = []

    current = [
        values[0]
    ]

    for value in values[1:]:

        center = float(
            np.mean(current)
        )

        if (
            abs(value - center)
            <= tolerance
        ):

            current.append(value)

        else:

            clusters.append(
                current
            )

            current = [
                value
            ]

    clusters.append(
        current
    )

    best = max(
        clusters,
        key=len
    )

    return {
        "low": float(
            min(best)
        ),
        "high": float(
            max(best)
        ),
        "mid": float(
            np.mean(best)
        ),
        "count": len(best),
    }


def detect_liquidity_zones(
    df,
    lookback=30
):

    if (
        df is None
        or len(df) < 10
    ):

        return {
            "ssl": None,
            "bsl": None,
        }

    data = df.iloc[
        -lookback:
    ].copy()

    indicators = add_indicators(
        data
    )

    atr = safe_float(
        indicators["atr"].iloc[-1]
    )

    if (
        not np.isfinite(atr)
        or atr <= 0
    ):

        atr = max(
            float(
                data["close"].iloc[-1]
            ) * 0.0015,
            0.8
        )

    tolerance = max(
        atr * 0.30,
        0.60
    )

    lows = [
        x["price"]
        for x in pivot_lows(
            data,
            2
        )
    ]

    highs = [
        x["price"]
        for x in pivot_highs(
            data,
            2
        )
    ]

    if not lows:

        lows = (
            data["low"]
            .tail(8)
            .tolist()
        )

    if not highs:

        highs = (
            data["high"]
            .tail(8)
            .tolist()
        )

    ssl = cluster_zone(
        lows,
        tolerance
    )

    bsl = cluster_zone(
        highs,
        tolerance
    )

    return {
        "ssl": ssl,
        "bsl": bsl,
    }


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def detect_liquidity_sweep(
    df,
    lookback=12,
    recent_bars=3
):

    if (
        df is None
        or len(df)
        < lookback
        + recent_bars
        + 2
    ):

        return {
            "direction": "NONE",
            "label": "NONE",
            "price": np.nan,
            "level": np.nan,
        }

    data = df.copy()

    start = max(
        lookback,
        len(data) - recent_bars
    )

    for i in range(
        start,
        len(data)
    ):

        candle = data.iloc[i]

        previous = data.iloc[
            max(0, i - lookback):i
        ]

        if previous.empty:
            continue

        prev_low = previous[
            "low"
        ].min()

        prev_high = previous[
            "high"
        ].max()

        # SSL sweep
        if (
            candle["low"] < prev_low
            and candle["close"] > prev_low
        ):

            return {
                "direction": "BULLISH",
                "label": "SSL SWEEP",
                "price": float(
                    candle["low"]
                ),
                "level": float(
                    prev_low
                ),
            }

        # BSL sweep
        if (
            candle["high"] > prev_high
            and candle["close"] < prev_high
        ):

            return {
                "direction": "BEARISH",
                "label": "BSL SWEEP",
                "price": float(
                    candle["high"]
                ),
                "level": float(
                    prev_high
                ),
            }

    return {
        "direction": "NONE",
        "label": "NONE",
        "price": np.nan,
        "level": np.nan,
    }


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

    if c["low"] > a["high"]:
        return "BULLISH"

    if c["high"] < a["low"]:
        return "BEARISH"

    return "NONE"


# ============================================================
# DISPLACEMENT
# ============================================================

def detect_displacement(df):

    if (
        df is None
        or len(df) < 8
    ):
        return "NONE"

    data = add_indicators(
        df
    )

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
        float(
            last["close"]
            - last["open"]
        )
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

    change = float(
        df["close"].iloc[-1]
        - df["close"].iloc[-6]
    )

    indicators = add_indicators(
        df
    )

    atr = safe_float(
        indicators["atr"].iloc[-1]
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
        df["high"]
        .tail(30)
        .max()
    )

    low = float(
        df["low"]
        .tail(30)
        .min()
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
# TRENDLINE COMPRESSION — KEY V6.0 FILTER
# ============================================================

def line_value(
    p1_index,
    p1_price,
    p2_index,
    p2_price,
    target_index
):

    if p2_index == p1_index:
        return np.nan

    slope = (
        p2_price - p1_price
    ) / (
        p2_index - p1_index
    )

    return (
        p2_price
        + slope
        * (
            target_index
            - p2_index
        )
    )


def detect_compression(
    df,
    lookback=50,
    recent_window=6
):
    """
    BULLISH COMPRESSION:
    price approaches a descending resistance.

    BEARISH COMPRESSION:
    price approaches an ascending support.

    Compression is NOT an entry by itself.
    """

    result = {
        "direction": "NONE",
        "label": "NONE",
        "trendline": np.nan,
        "distance": np.nan,
        "distance_prev": np.nan,
        "quality": 0,
    }

    if (
        df is None
        or len(df) < 20
    ):
        return result

    data = df.iloc[
        -lookback:
    ].copy()

    highs = pivot_highs(
        data,
        2
    )

    lows = pivot_lows(
        data,
        2
    )

    current_index = (
        len(data) - 1
    )

    current_price = float(
        data["close"].iloc[-1]
    )

    # --------------------------------------------------------
    # DESCENDING RESISTANCE
    # --------------------------------------------------------

    if len(highs) >= 2:

        p1 = highs[-2]
        p2 = highs[-1]

        if (
            p2["price"]
            < p1["price"]
        ):

            resistance = line_value(
                p1["index"],
                p1["price"],
                p2["index"],
                p2["price"],
                current_index,
            )

            if (
                np.isfinite(resistance)
                and resistance
                > current_price
            ):

                distance = (
                    resistance
                    - current_price
                )

                prev_index = max(
                    0,
                    current_index
                    - recent_window
                )

                previous_price = float(
                    data["close"].iloc[
                        prev_index
                    ]
                )

                previous_resistance = (
                    line_value(
                        p1["index"],
                        p1["price"],
                        p2["index"],
                        p2["price"],
                        prev_index,
                    )
                )

                previous_distance = (
                    previous_resistance
                    - previous_price
                )

                indicators = add_indicators(
                    data
                )

                atr = safe_float(
                    indicators["atr"].iloc[-1]
                )

                if (
                    np.isfinite(atr)
                    and atr > 0
                ):
                    tolerance = (
                        atr * 3.0
                    )
                else:
                    tolerance = (
                        current_price
                        * 0.003
                    )

                shrinking = (
                    np.isfinite(
                        previous_distance
                    )
                    and previous_distance
                    > distance
                )

                near_line = (
                    distance
                    <= tolerance
                )

                if (
                    shrinking
                    and near_line
                ):

                    quality = 70

                    if (
                        distance
                        <= tolerance * 0.60
                    ):
                        quality = 90

                    return {
                        "direction": "BULLISH",
                        "label": (
                            "BULLISH "
                            "COMPRESSION"
                        ),
                        "trendline": float(
                            resistance
                        ),
                        "distance": float(
                            distance
                        ),
                        "distance_prev": float(
                            previous_distance
                        ),
                        "quality": quality,
                    }

    # --------------------------------------------------------
    # ASCENDING SUPPORT
    # --------------------------------------------------------

    if len(lows) >= 2:

        p1 = lows[-2]
        p2 = lows[-1]

        if (
            p2["price"]
            > p1["price"]
        ):

            support = line_value(
                p1["index"],
                p1["price"],
                p2["index"],
                p2["price"],
                current_index,
            )

            if (
                np.isfinite(support)
                and support
                < current_price
            ):

                distance = (
                    current_price
                    - support
                )

                prev_index = max(
                    0,
                    current_index
                    - recent_window
                )

                previous_price = float(
                    data["close"].iloc[
                        prev_index
                    ]
                )

                previous_support = (
                    line_value(
                        p1["index"],
                        p1["price"],
                        p2["index"],
                        p2["price"],
                        prev_index,
                    )
                )

                previous_distance = (
                    previous_price
                    - previous_support
                )

                indicators = add_indicators(
                    data
                )

                atr = safe_float(
                    indicators["atr"].iloc[-1]
                )

                if (
                    np.isfinite(atr)
                    and atr > 0
                ):
                    tolerance = (
                        atr * 3.0
                    )
                else:
                    tolerance = (
                        current_price
                        * 0.003
                    )

                shrinking = (
                    np.isfinite(
                        previous_distance
                    )
                    and previous_distance
                    > distance
                )

                near_line = (
                    distance
                    <= tolerance
                )

                if (
                    shrinking
                    and near_line
                ):

                    quality = 70

                    if (
                        distance
                        <= tolerance * 0.60
                    ):
                        quality = 90

                    return {
                        "direction": "BEARISH",
                        "label": (
                            "BEARISH "
                            "COMPRESSION"
                        ),
                        "trendline": float(
                            support
                        ),
                        "distance": float(
                            distance
                        ),
                        "distance_prev": float(
                            previous_distance
                        ),
                        "quality": quality,
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
        "H4",
    ):

        frame = resample_ohlc(
            raw,
            tf
        )

        if frame.empty:
            continue

        if (
            len(frame)
            < MIN_CANDLES[tf]
        ):
            continue

        frames[tf] = add_indicators(
            frame
        )

    return frames


def data_status(frames):

    missing = []

    for tf in (
        "M5",
        "M15",
        "H1",
        "H4",
    ):

        if tf not in frames:
            missing.append(tf)

    return missing


# ============================================================
# HIGHER TF BIAS
# ============================================================

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


# ============================================================
# STRUCTURE SCORE
# ============================================================

def side_structure_score(
    side,
    frames
):

    score = 0

    for tf in (
        "H4",
        "H1",
        "M15",
        "M5",
    ):

        frame = frames.get(
            tf
        )

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
# V6.0 SETUP ENGINE
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
            h4,
        )
    ):

        return {
            "signal": "WAIT",
            "side": "NONE",
            "score": 0,
            "buy_score": 0,
            "sell_score": 0,
            "reason": (
                "Недостаточно данных"
            ),
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

    confirmation = (
        structure_confirmation(
            m5
        )
    )

    fvg = detect_fvg(
        m5
    )

    displacement = (
        detect_displacement(
            m5
        )
    )

    momentum = momentum_state(
        m5
    )

    pd_zone = premium_discount(
        m5
    )

    compression_h1 = (
        detect_compression(h1)
    )

    compression_m15 = (
        detect_compression(m15)
    )

    compression = compression_h1

    if (
        compression["direction"]
        == "NONE"
    ):

        compression = (
            compression_m15
        )

    buy = 0
    sell = 0

    # --------------------------------------------------------
    # 1. HIGHER TF CONTEXT = 15
    # --------------------------------------------------------

    if bias == "BULLISH":
        buy += 15

    elif bias == "BEARISH":
        sell += 15

    # --------------------------------------------------------
    # 2. STRUCTURE = 20
    # --------------------------------------------------------

    buy += side_structure_score(
        "BUY",
        frames
    )

    sell += side_structure_score(
        "SELL",
        frames
    )

    # --------------------------------------------------------
    # 3. COMPRESSION = 15
    # --------------------------------------------------------

    if (
        compression["direction"]
        == "BULLISH"
    ):
        buy += 15

    elif (
        compression["direction"]
        == "BEARISH"
    ):
        sell += 15

    # --------------------------------------------------------
    # 4. LIQUIDITY ZONE = 15
    # --------------------------------------------------------

    if ssl is not None:
        buy += 15

    if bsl is not None:
        sell += 15

    # --------------------------------------------------------
    # 5. SWEEP = 15
    # --------------------------------------------------------

    if (
        sweep["direction"]
        == "BULLISH"
    ):
        buy += 15

    elif (
        sweep["direction"]
        == "BEARISH"
    ):
        sell += 15

    # --------------------------------------------------------
    # 6. BOS / CHoCH = 10
    # --------------------------------------------------------

    if (
        confirmation["direction"]
        == "BULLISH"
    ):
        buy += 10

    elif (
        confirmation["direction"]
        == "BEARISH"
    ):
        sell += 10

    # --------------------------------------------------------
    # 7. FVG / DISPLACEMENT = 10
    # --------------------------------------------------------

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
    # FULL BUY
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

    # ========================================================
    # FULL SELL
    # ========================================================

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
    # EARLY BUY
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

    # ========================================================
    # EARLY SELL
    # ========================================================

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
    # FINAL DECISION
    # ========================================================

    if (
        buy_full
        and buy > sell
    ):

        signal = "BUY"
        side = "BUY"
        score = buy

    elif (
        sell_full
        and sell > buy
    ):

        signal = "SELL"
        side = "SELL"
        score = sell

    elif (
        buy_early
        and buy > sell
    ):

        signal = "EARLY BUY"
        side = "BUY"
        score = buy

    elif (
        sell
