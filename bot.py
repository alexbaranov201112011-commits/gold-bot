import os
import json
import time
import hashlib
import tempfile
import threading
import traceback
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd
from flask import Flask, request, jsonify


# ============================================================
# 🥇 GOLD SMART V6.0.3
# XAU/USD SMART SMC SIGNAL ENGINE
#
# H4 → H1 → M15 → M5
# DATA SOURCE: XAUS
# TELEGRAM: WEBHOOK
# AUTO TRADING: OFF
#
# V6.0.3
# FIX:
# - XAUS intraday format {t, p}
# - robust timestamp parsing
# - robust price parsing
# - robust nested response parsing
# - extra XAUS diagnostics in Render logs
# ============================================================

APP_NAME = "GOLD SMART V6.0.3"

AUTO_TRADING = False

DEFAULT_DEPOSIT = 800.0
RISK_PERCENT = 1.0

MIN_SCORE = 70
EARLY_SCORE = 60

SL_DISTANCE = 5.0
TP1_DISTANCE = 7.5
TP2_DISTANCE = 15.0

BE_TRIGGER = 6.0

POLL_SECONDS = 60
SIGNAL_COOLDOWN_MIN = 15
INTRADAY_HOURS = 48
CLOSED_CANDLES = True

XAUS_SPOT_URL = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY_URL = "https://xaus.com/api/v1/intraday"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://gold-bot-q8la.onrender.com"
).strip().rstrip("/")

KEEPALIVE = os.getenv("KEEPALIVE", "0") == "1"
BOOT_NOTICE = os.getenv("BOOT_NOTICE", "1") == "1"

STATS_FILE = os.getenv(
    "STATS_FILE",
    "gold_stats.json"
)

TRADE_FILE = os.getenv(
    "TRADE_FILE",
    "gold_trade.json"
)

WEBHOOK_SECRET = hashlib.sha256(
    ("gold-smart:" + BOT_TOKEN).encode("utf-8")
).hexdigest()[:48]


# ============================================================
# FLASK / GLOBAL STATE
# ============================================================

app = Flask(__name__)

ENGINE_STARTED = False
KEEPALIVE_STARTED = False
STARTUP_DONE = False

ENGINE_LOCK = threading.Lock()
STATE_LOCK = threading.RLock()

last_cycle = None
last_error = None
last_price = None
last_data_counts = {}
last_signal_time = 0.0

active_trade = None


# ============================================================
# BASIC HELPERS
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def utc_string():
    return utc_now().strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def safe_float(value, default=None):
    try:
        if value is None or isinstance(value, bool):
            return default

        if isinstance(
            value,
            (
                int,
                float,
                np.integer,
                np.floating
            )
        ):
            value = float(value)
        else:
            value = str(value).replace(
                ",",
                ""
            ).strip()

        result = float(value)

        if not np.isfinite(result):
            return default

        return result

    except Exception:
        return default


def atomic_json_save(filename, data):
    directory = os.path.dirname(filename)

    if directory:
        os.makedirs(
            directory,
            exist_ok=True
        )

    fd, temp_path = tempfile.mkstemp(
        prefix=".goldsmart_",
        suffix=".tmp",
        dir=directory or "."
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

        os.replace(
            temp_path,
            filename
        )

    except Exception:
        try:
            os.remove(temp_path)
        except Exception:
            pass

        raise


def load_json(filename, default):
    try:
        if not os.path.exists(filename):
            return default

        with open(
            filename,
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception:
        return default


# ============================================================
# TELEGRAM
# ============================================================

def telegram_url(method):
    return (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )


def send_telegram(
    text_message,
    chat_id=None
):
    if not BOT_TOKEN:
        print(
            "GOLD SMART: BOT_TOKEN missing"
        )
        return False

    target_chat = str(
        chat_id or TELEGRAM_CHAT_ID
    ).strip()

    if not target_chat:
        print(
            "GOLD SMART: "
            "TELEGRAM_CHAT_ID missing"
        )
        return False

    try:
        response = requests.post(
            telegram_url("sendMessage"),
            json={
                "chat_id": target_chat,
                "text": text_message
            },
            timeout=20
        )

        print(
            "GOLD SMART: Telegram "
            f"sendMessage HTTP "
            f"{response.status_code}"
        )

        if response.status_code != 200:
            print(
                response.text[:1000]
            )
            return False

        return bool(
            response.json().get(
                "ok",
                True
            )
        )

    except Exception as e:
        print(
            "GOLD SMART: "
            "Telegram send error:",
            repr(e)
        )

        return False


def configure_webhook():
    if not BOT_TOKEN:
        print(
            "GOLD SMART: webhook not configured "
            "- BOT_TOKEN missing"
        )
        return False

    if not RENDER_EXTERNAL_URL:
        print(
            "GOLD SMART: webhook not configured "
            "- URL missing"
        )
        return False

    webhook_url = (
        RENDER_EXTERNAL_URL +
        "/telegram"
    )

    payload = {
        "url": webhook_url,
        "secret_token": WEBHOOK_SECRET,
        "drop_pending_updates": False
    }

    try:
        response = requests.post(
            telegram_url("setWebhook"),
            json=payload,
            timeout=20
        )

        print(
            "GOLD SMART: setWebhook HTTP "
            f"{response.status_code}: "
            f"{response.text[:1000]}"
        )

        if response.status_code != 200:
            return False

        try:
            info = requests.get(
                telegram_url(
                    "getWebhookInfo"
                ),
                timeout=15
            )

            print(
                "GOLD SMART: "
                "getWebhookInfo HTTP "
                f"{info.status_code}: "
                f"{info.text[:1500]}"
            )

        except Exception as e:
            print(
                "GOLD SMART: "
                "getWebhookInfo error:",
                repr(e)
            )

        return True

    except Exception as e:
        print(
            "GOLD SMART: "
            "webhook configuration error:",
            repr(e)
        )

        return False


# ============================================================
# XAUS DATA
# ============================================================

def recursive_find_price(obj):
    if obj is None:
        return None

    if isinstance(
        obj,
        (
            int,
            float
        )
    ):
        return safe_float(obj)

    if isinstance(obj, list):
        for item in obj:
            value = recursive_find_price(
                item
            )

            if value is not None:
                return value

        return None

    if isinstance(obj, dict):

        preferred = [
            "price",
            "spot_usd_oz",
            "close",
            "value",
            "last",
            "rate",
            "p"
        ]

        for key in preferred:

            if key in obj:

                value = safe_float(
                    obj.get(key)
                )

                if value is not None:
                    return value

        for key in [
            "xau",
            "gold",
            "data",
            "result",
            "quote"
        ]:

            if key in obj:

                value = recursive_find_price(
                    obj[key]
                )

                if value is not None:
                    return value

    return None


def fetch_spot():
    global last_price

    response = requests.get(
        XAUS_SPOT_URL,
        params={
            "symbol": "xau"
        },
        timeout=20
    )

    response.raise_for_status()

    data = response.json()

    price = recursive_find_price(
        data
    )

    if price is None:
        raise ValueError(
            "XAUS spot price not found"
        )

    last_price = price

    return price


# ============================================================
# XAUS TIMESTAMP PARSER
# ============================================================

def extract_timestamp(item):
    if not isinstance(
        item,
        dict
    ):
        return None

    # XAUS official/current format:
    # "t": timestamp
    #
    # Other supported formats:
    # timestamp
    # time
    # datetime
    # date
    # created_at

    for key in [
        "t",
        "timestamp",
        "time",
        "datetime",
        "date",
        "created_at"
    ]:

        if key not in item:
            continue

        value = item[key]

        if value is None:
            continue

        # Unix timestamp
        if isinstance(
            value,
            (
                int,
                float,
                np.integer,
                np.floating
            )
        ):

            value = float(value)

            # milliseconds -> seconds
            if value > 100000000000:
                value /= 1000

            try:
                result = pd.to_datetime(
                    value,
                    unit="s",
                    utc=True
                )

                if pd.notna(result):
                    return result

            except Exception:
                pass

        # String timestamp / ISO
        try:
            result = pd.to_datetime(
                value,
                utc=True,
                errors="coerce"
            )

            if pd.notna(result):
                return result

        except Exception:
            pass

    return None


# ============================================================
# XAUS PRICE PARSER
# ============================================================

def extract_candle_price(item):
    if not isinstance(
        item,
        dict
    ):
        return None

    # XAUS current intraday format:
    #
    # {
    #     "t": timestamp,
    #     "p": price
    # }
    #
    # "p" is therefore checked FIRST.

    for key in [
        "p",
        "price",
        "close",
        "spot_usd_oz",
        "value",
        "last"
    ]:

        if key in item:

            value = safe_float(
                item[key]
            )

            if value is not None:
                return value

    return None


# ============================================================
# XAUS INTRADAY RESPONSE PARSER
# ============================================================

def extract_intraday_items(data):

    # Direct list
    if isinstance(
        data,
        list
    ):
        return data

    if not isinstance(
        data,
        dict
    ):
        return []

    # XAUS intraday response normally
    # contains "points":
    #
    # {
    #     "points": [
    #         {"t": ..., "p": ...},
    #         {"t": ..., "p": ...}
    #     ]
    # }

    for key in [
        "points",
        "data",
        "prices",
        "intraday",
        "history",
        "candles",
        "results"
    ]:

        value = data.get(key)

        if isinstance(
            value,
            list
        ):
            return value

    # Nested XAU
    xau = data.get("xau")

    if isinstance(
        xau,
        list
    ):
        return xau

    if isinstance(
        xau,
        dict
    ):

        for key in [
            "points",
            "data",
            "prices",
            "intraday",
            "history",
            "candles",
            "results"
        ]:

            value = xau.get(key)

            if isinstance(
                value,
                list
            ):
                return value

    return []


# ============================================================
# FETCH XAUS INTRADAY
# ============================================================

def fetch_intraday():

    now = int(
        time.time()
    )

    response = requests.get(
        XAUS_INTRADAY_URL,
        params={
            "symbol": "xau",
            "hours": INTRADAY_HOURS,
            "fresh": now
        },
        timeout=25
    )

    response.raise_for_status()

    data = response.json()

    items = extract_intraday_items(
        data
    )

    # Diagnostic logging
    print(
        "GOLD SMART: "
        f"XAUS intraday raw items="
        f"{len(items)}"
    )

    if isinstance(
        data,
        dict
    ):

        print(
            "GOLD SMART: "
            "XAUS response keys:",
            list(data.keys())
        )

    rows = []

    skipped_timestamp = 0
    skipped_price = 0

    for item in items:

        timestamp = extract_timestamp(
            item
        )

        price = extract_candle_price(
            item
        )

        if timestamp is None:
            skipped_timestamp += 1
            continue

        if price is None:
            skipped_price += 1
            continue

        rows.append({
            "time": timestamp,
            "price": price
        })

    print(
        "GOLD SMART: XAUS parser "
        f"accepted={len(rows)} "
        f"skipped_timestamp="
        f"{skipped_timestamp} "
        f"skipped_price="
        f"{skipped_price}"
    )

    if not rows:

        # Print first item for debugging
        if items:
            print(
                "GOLD SMART: "
                "XAUS first raw item:",
                repr(items[0])[:2000]
            )

        raise ValueError(
            "XAUS intraday returned "
            "no usable data"
        )

    df = pd.DataFrame(
        rows
    )

    df["time"] = pd.to_datetime(
        df["time"],
        utc=True,
        errors="coerce"
    )

    df["price"] = pd.to_numeric(
        df["price"],
        errors="coerce"
    )

    df = df.dropna()

    df = df.sort_values(
        "time"
    )

    df = df.drop_duplicates(
        subset=["time"],
        keep="last"
    )

    df = df.set_index(
        "time"
    )

    df = df[
        ~df.index.duplicated(
            keep="last"
        )
    ]

    print(
        "GOLD SMART: "
        f"XAUS usable rows="
        f"{len(df)}"
    )

    if not df.empty:
        print(
            "GOLD SMART: "
            f"XAUS first="
            f"{df.index[0]} "
            f"last="
            f"{df.index[-1]}"
        )

    return df


# ============================================================
# TIMEFRAMES
# ============================================================

def build_ohlc(
    price_df,
    timeframe
):

    if (
        price_df is None
        or price_df.empty
    ):
        return pd.DataFrame()

    series = (
        price_df["price"]
        .astype(float)
    )

    result = (
        series
        .resample(timeframe)
        .ohlc()
    )

    result = result.dropna()

    if (
        CLOSED_CANDLES
        and not result.empty
    ):

        now = pd.Timestamp.now(
            tz="UTC"
        )

        current_bucket = now.floor(
            timeframe
        )

        result = result[
            result.index < current_bucket
        ]

    return result


def build_timeframes(
    intraday
):

    return {
        "M5": build_ohlc(
            intraday,
            "5min"
        ),

        "M15": build_ohlc(
            intraday,
            "15min"
        ),

        "H1": build_ohlc(
            intraday,
            "1h"
        ),

        "H4": build_ohlc(
            intraday,
            "4h"
        )
    }


# ============================================================
# INDICATORS
# ============================================================

def ema(
    series,
    period
):

    return series.ewm(
        span=period,
        adjust=False
    ).mean()


def rsi(
    series,
    period=14
):

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

    rs = (
        avg_gain /
        avg_loss.replace(
            0,
            np.nan
        )
    )

    result = (
        100 -
        (
            100 /
            (1 + rs)
        )
    )

    return result.fillna(
        50
    )


def atr(
    df,
    period=14
):

    high = df["high"]
    low = df["low"]
    close = df["close"]

    previous_close = close.shift(
        1
    )

    tr = pd.concat(
        [
            high - low,
            (
                high -
                previous_close
            ).abs(),
            (
                low -
                previous_close
            ).abs()
        ],
        axis=1
    ).max(
        axis=1
    )

    return tr.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()


# ============================================================
# TREND ANALYSIS
# ============================================================

def trend_analysis(df):

    if (
        df is None
        or len(df) < 5
    ):

        return {
            "side": "NEUTRAL",
            "score": 50,
            "rsi": 50,
            "ema20": None,
            "ema50": None,
            "ema200": None
        }

    close = df["close"]

    e20_series = ema(
        close,
        20
    )

    e50_series = ema(
        close,
        50
    )

    e200_series = ema(
        close,
        200
    )

    current = float(
        close.iloc[-1]
    )

    e20 = float(
        e20_series.iloc[-1]
    )

    e50 = float(
        e50_series.iloc[-1]
    )

    e200 = float(
        e200_series.iloc[-1]
    )

    current_rsi = float(
        rsi(close).iloc[-1]
    )

    bullish = 0
    bearish = 0

    if current > e20:
        bullish += 15
    else:
        bearish += 15

    if current > e50:
        bullish += 15
    else:
        bearish += 15

    if current > e200:
        bullish += 20
    else:
        bearish += 20

    if e20 > e50:
        bullish += 10
    else:
        bearish += 10

    if e50 > e200:
        bullish += 10
    else:
        bearish += 10

    if current_rsi >= 55:
        bullish += 15

    elif current_rsi <= 45:
        bearish += 15

    if len(close) >= 6:

        old = float(
            close.iloc[-6]
        )

        if current > old:
            bullish += 15

        elif current < old:
            bearish += 15

    if bullish > bearish:

        side = "BULLISH"
        score = min(
            100,
            bullish
        )

    elif bearish > bullish:

        side = "BEARISH"
        score = min(
            100,
            bearish
        )

    else:

        side = "NEUTRAL"
        score = 50

    return {
        "side": side,
        "score": int(score),
        "rsi": current_rsi,
        "ema20": e20,
        "ema50": e50,
        "ema200": e200
    }


# ============================================================
# MARKET STRUCTURE / SMC
# ============================================================

def detect_bos(df):

    if (
        df is None
        or len(df) < 5
    ):
        return "NONE"

    previous = (
        df.iloc[:-1]
        .tail(4)
    )

    current = df.iloc[-1]

    previous_high = (
        previous["high"].max()
    )

    previous_low = (
        previous["low"].min()
    )

    if current["close"] > previous_high:
        return "BULLISH"

    if current["close"] < previous_low:
        return "BEARISH"

    return "NONE"



def detect_liquidity_sweep(df):
    """Detect a recent liquidity sweep on closed candles."""
    if df is None or len(df) < 6:
        return "NONE"

    start = max(5, len(df) - 3)

    for idx in range(len(df) - 1, start - 1, -1):
        previous = df.iloc[idx - 5:idx]
        current = df.iloc[idx]

        if previous.empty:
            continue

        prior_high = float(previous["high"].max())
        prior_low = float(previous["low"].min())
        current_low = float(current["low"])
        current_high = float(current["high"])
        current_close = float(current["close"])

        # Sweep below lows, then close back above the prior low.
        if current_low < prior_low and current_close > prior_low:
            return "BULLISH SWEEP"

        # Sweep above highs, then close back below the prior high.
        if current_high > prior_high and current_close < prior_high:
            return "BEARISH SWEEP"

    return "NONE"


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


def detect_displacement(df):

    if (
        df is None
        or len(df) < 15
    ):
        return "NONE"

    current = df.iloc[-1]

    current_body = abs(
        float(current["close"]) -
        float(current["open"])
    )

    current_atr = float(
        atr(
            df,
            14
        ).iloc[-1]
    )

    if current_atr <= 0:
        return "NONE"

    ratio = (
        current_body /
        current_atr
    )

    if ratio < 0.8:
        return "NONE"

    if current["close"] > current["open"]:
        return "BULLISH"

    if current["close"] < current["open"]:
        return "BEARISH"

    return "NONE"


def detect_momentum(df):

    if (
        df is None
        or len(df) < 8
    ):
        return "NONE"

    close = df["close"]

    change = (
        float(close.iloc[-1]) -
        float(close.iloc[-6])
    )

    if change > 0:
        return "BULLISH"

    if change < 0:
        return "BEARISH"

    return "NONE"



def detect_compression(df):
    """Detect volatility contraction using closed candles."""
    if df is None or len(df) < 20:
        return "NONE"

    atr_values = atr(df, 14).dropna()

    if len(atr_values) < 10:
        return "NONE"

    recent_atr = float(atr_values.iloc[-3:].mean())
    prior_atr = float(atr_values.iloc[-10:-5].mean())

    if prior_atr <= 0:
        return "NONE"

    recent_range = float(
        (df["high"].iloc[-5:] - df["low"].iloc[-5:]).mean()
    )
    prior_range = float(
        (df["high"].iloc[-10:-5] - df["low"].iloc[-10:-5]).mean()
    )

    if (
        recent_atr < prior_atr * 0.90
        and prior_range > 0
        and recent_range < prior_range * 0.90
    ):
        return "COMPRESSION"

    return "NONE"


def detect_trendline_pressure(df):
    """Detect approximate pressure toward a local range boundary."""
    if df is None or len(df) < 20:
        return "NONE"

    window = df.tail(12).reset_index(drop=True)
    x = np.arange(len(window), dtype=float)

    try:
        high_slope = float(
            np.polyfit(x, window["high"].astype(float), 1)[0]
        )
        low_slope = float(
            np.polyfit(x, window["low"].astype(float), 1)[0]
        )
    except (ValueError, TypeError, np.linalg.LinAlgError):
        return "NONE"

    high = float(window["high"].max())
    low = float(window["low"].min())
    close = float(window["close"].iloc[-1])
    width = high - low

    if width <= 0:
        return "NONE"

    near_high = (high - close) <= width * 0.22
    near_low = (close - low) <= width * 0.22

    # Rising lows pressing toward the upper boundary.
    if low_slope > 0 and near_high:
        return "BULLISH PRESSURE"

    # Falling highs pressing toward the lower boundary.
    if high_slope < 0 and near_low:
        return "BEARISH PRESSURE"

    return "NONE"


def premium_discount(df):

    if (
        df is None
        or len(df) < 10
    ):
        return "EQUILIBRIUM"

    window = df.tail(20)

    high = float(
        window["high"].max()
    )

    low = float(
        window["low"].min()
    )

    close = float(
        df["close"].iloc[-1]
    )

    midpoint = (
        high + low
    ) / 2

    if close > midpoint:
        return "PREMIUM"

    if close < midpoint:
        return "DISCOUNT"

    return "EQUILIBRIUM"


# ============================================================
# MARKET ANALYSIS
# ============================================================

def analyze_market(frames):
    h4 = frames["H4"]
    h1 = frames["H1"]
    m15 = frames["M15"]
    m5 = frames["M5"]

    h4_trend = trend_analysis(h4)
    h1_trend = trend_analysis(h1)
    m15_trend = trend_analysis(m15)
    m5_trend = trend_analysis(m5)

    bos = detect_bos(m5)
    sweep = detect_liquidity_sweep(m5)
    fvg = detect_fvg(m5)
    compression = detect_compression(m5)
    trendline_pressure = detect_trendline_pressure(m5)
    pd_zone = premium_discount(m5)

    bullish_htf = (
        h4_trend["side"] == "BULLISH"
        and h1_trend["side"] == "BULLISH"
    )
    bearish_htf = (
        h4_trend["side"] == "BEARISH"
        and h1_trend["side"] == "BEARISH"
    )

    buy_score = 0
    sell_score = 0

    if h4_trend["side"] == "BULLISH":
        buy_score += 20
    elif h4_trend["side"] == "BEARISH":
        sell_score += 20

    if h1_trend["side"] == "BULLISH":
        buy_score += 20
    elif h1_trend["side"] == "BEARISH":
        sell_score += 20

    if m15_trend["side"] == "BULLISH":
        buy_score += 12
    elif m15_trend["side"] == "BEARISH":
        sell_score += 12

    if m5_trend["side"] == "BULLISH":
        buy_score += 10
    elif m5_trend["side"] == "BEARISH":
        sell_score += 10

    if bos == "BULLISH":
        buy_score += 12
    elif bos == "BEARISH":
        sell_score += 12

    if sweep == "BULLISH SWEEP":
        buy_score += 10
    elif sweep == "BEARISH SWEEP":
        sell_score += 10

    if fvg == "BULLISH":
        buy_score += 6
    elif fvg == "BEARISH":
        sell_score += 6

    if displacement == "BULLISH":
        buy_score += 5
    elif displacement == "BEARISH":
        sell_score += 5

    if momentum == "BULLISH":
        buy_score += 5
    elif momentum == "BEARISH":
        sell_score += 5
    if trendline_pressure == "BULLISH PRESSURE":
        buy_score += 4
    elif trendline_pressure == "BEARISH PRESSURE":
        sell_score += 4
    if pd_zone == "DISCOUNT":
        buy_score += 5
    elif pd_zone == "PREMIUM":
        sell_score += 5

    buy_score = min(100, buy_score)
    sell_score = min(100, sell_score)

    if buy_score > sell_score:
        side = "BUY"
        score = buy_score
    elif sell_score > buy_score:
        side = "SELL"
        score = sell_score
    else:
        side = "WAIT"
        score = max(buy_score, sell_score)

    # V6.1: confirmation gate.
    # A high score alone is NOT permission to enter.
    setup_reasons = []

    if side in ("BUY", "SELL"):
        expected = "BULLISH" if side == "BUY" else "BEARISH"
        opposite = "BEARISH" if side == "BUY" else "BULLISH"
        expected_sweep = (
            "BULLISH SWEEP" if side == "BUY"
            else "BEARISH SWEEP"
        )

        if not (h4_trend["side"] == expected
                and h1_trend["side"] == expected):
            setup_reasons.append("H4/H1 not aligned")

        if m15_trend["side"] != expected:
            setup_reasons.append("M15 not confirmed")

        if sweep != expected_sweep:
            setup_reasons.append("No matching liquidity sweep")

        if bos != expected:
            setup_reasons.append("No matching BOS")

        if momentum == opposite:
            setup_reasons.append("Momentum contradicts direction")

        if fvg == opposite:
            setup_reasons.append("FVG contradicts direction")

        if displacement == opposite:
            setup_reasons.append("Displacement contradicts direction")

        if setup_reasons:
            side = "WAIT"

    if side == "WAIT":
        signal_type = "WAIT"
    elif score >= MIN_SCORE:
        signal_type = "FULL"
    elif score >= EARLY_SCORE:
        signal_type = "EARLY"
    else:
        side = "WAIT"
        signal_type = "WAIT"

    return {
        "side": side,
        "score": int(score),
        "signal_type": signal_type,
        "setup_reasons": setup_reasons,

        "h4": h4_trend,
        "h1": h1_trend,
        "m15": m15_trend,
        "m5": m5_trend,

        "liquidity": sweep,
        "bos": bos,
        "fvg": fvg,
        "displacement": displacement,
        "momentum": momentum,
        "compression": compression,
        "premium_discount": pd_zone,
        "compression": compression,
        "trendline_pressure": trendline_pressure,
        "premium_discount": pd_zone,
        "htf_agreement": bullish_htf or bearish_htf
    }

# ============================================================
# DISPLAY LOT
# ============================================================

def calculate_lot():

    # Только display lot.
    # Реальные ордера НЕ отправляются.

    risk_money = (
        DEFAULT_DEPOSIT *
        RISK_PERCENT /
        100.0
    )

    if risk_money <= 0:
        return 0.01

    return 0.02


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def build_signal_message(
    price,
    analysis,
    signal_type=None
):
    side = analysis["side"]
    score = analysis["score"]
    if signal_type is None:
        signal_type = (
            analysis["signal_type"]
        )
    # ========================================================
    # WAIT
    # НЕТ ВХОДА → НЕТ ENTRY / SL / TP / LOT / RR
    # ========================================================
    if (
        signal_type == "WAIT"
        or
        side == "WAIT"
    ):
        return (
            f"🥇 {APP_NAME}\n\n"
            f"⚪ SIGNAL: WAIT\n\n"
            f"💰 XAUUSD: "
            f"{price:.2f}\n\n"
            f"📊 H4: "
            f"{analysis['h4']['side']} "
            f"({analysis['h4']['score']}/100)\n"
            f"📊 H1: "
            f"{analysis['h1']['side']} "
            f"({analysis['h1']['score']}/100)\n"
            f"📊 M15: "
            f"{analysis['m15']['side']} "
            f"({analysis['m15']['score']}/100)\n"
            f"📊 M5: "
            f"{analysis['m5']['side']} "
            f"({analysis['m5']['score']}/100)\n\n"
            f"💧 Liquidity: "
            f"{analysis['liquidity']}\n"
            f"🔨 BOS: "
            f"{analysis['bos']}\n"
            f"🧩 FVG: "
            f"{analysis['fvg']}\n"
            f"🚀 Displacement: "
            f"{analysis['displacement']}\n"
            f"⚡ Momentum: "
            f"{analysis['momentum']}\n"
            f"📦 Zone: "
            f"{analysis['premium_discount']}\n\n"
            f"📈 Score: "
            f"{score}/100\n\n"
            f"⏳ Ожидание подтверждения входа\n\n"
            f"🤖 AUTO TRADING: OFF\n"
            f"✋ Manual signals only"
        )
    # ========================================================
    # BUY
    # ========================================================
    if side == "BUY":
        sl = (
            price -
            SL_DISTANCE
        )
        tp1 = (
            price +
            TP1_DISTANCE
        )
        tp2 = (
            price +
            TP2_DISTANCE
        )
        emoji = "🟢"
    # ========================================================
    # SELL
    # ========================================================
    elif side == "SELL":
        sl = (
            price +
            SL_DISTANCE
        )
        tp1 = (
            price -
            TP1_DISTANCE
        )
        tp2 = (
            price -
            TP2_DISTANCE
        )
        emoji = "🔴"
    # ========================================================
    # SAFETY FALLBACK
    # ========================================================
    else:
        return (
            f"🥇 {APP_NAME}\n\n"
            f"⚪ SIGNAL: WAIT\n\n"
            f"💰 XAUUSD: "
            f"{price:.2f}\n\n"
            f"📊 H4: "
            f"{analysis['h4']['side']} "
            f"({analysis['h4']['score']}/100)\n"
            f"📊 H1: "
            f"{analysis['h1']['side']} "
            f"({analysis['h1']['score']}/100)\n"
            f"📊 M15: "
            f"{analysis['m15']['side']} "
            f"({analysis['m15']['score']}/100)\n"
            f"📊 M5: "
            f"{analysis['m5']['side']} "
            f"({analysis['m5']['score']}/100)\n\n"
            f"💧 Liquidity: "
            f"{analysis['liquidity']}\n"
            f"🔨 BOS: "
            f"{analysis['bos']}\n"
            f"🧩 FVG: "
            f"{analysis['fvg']}\n"
            f"🚀 Displacement: "
            f"{analysis['displacement']}\n"
            f"⚡ Momentum: "
            f"{analysis['momentum']}\n"
            f"📦 Zone: "
            f"{analysis['premium_discount']}\n\n"
            f"📈 Score: "
            f"{score}/100\n\n"
            f"⏳ Ожидание подтверждения входа\n\n"
            f"🤖 AUTO TRADING: OFF\n"
            f"✋ Manual signals only"
        )
    # ========================================================
    # REAL SIGNAL: EARLY / FULL
    # ========================================================
    confidence = min(
        99,
        max(
            50,
            score
        )
    )
    lot = calculate_lot()
    return (
        f"🥇 {APP_NAME}\n\n"
        f"{emoji} SIGNAL: "
        f"{signal_type} {side}\n\n"
        f"💰 XAUUSD: "
        f"{price:.2f}\n\n"
        f"📊 H4: "
        f"{analysis['h4']['side']} "
        f"({analysis['h4']['score']}/100)\n"
        f"📊 H1: "
        f"{analysis['h1']['side']} "
        f"({analysis['h1']['score']}/100)\n"
        f"📊 M15: "
        f"{analysis['m15']['side']} "
        f"({analysis['m15']['score']}/100)\n"
        f"📊 M5: "
        f"{analysis['m5']['side']} "
        f"({analysis['m5']['score']}/100)\n\n"
        f"💧 Liquidity: "
        f"{analysis['liquidity']}\n"
        f"🔨 BOS: "
        f"{analysis['bos']}\n"
        f"🧩 FVG: "
        f"{analysis['fvg']}\n"
        f"🚀 Displacement: "
        f"{analysis['displacement']}\n"
        f"⚡ Momentum: "
        f"{analysis['momentum']}\n"
        f"📦 Zone: "
        f"{analysis['premium_discount']}\n\n"
        f"🎯 ENTRY: "
        f"{price:.2f}\n"
        f"🛑 SL: "
        f"{sl:.2f}\n"
        f"🎯 TP1: "
        f"{tp1:.2f}\n"
        f"🎯 TP2: "
        f"{tp2:.2f}\n\n"
        f"📐 RR: 1:3\n"
        f"📦 Lot: {lot:.2f}\n"
        f"🔥 Confidence: "
        f"{confidence}%\n\n"
        f"🤖 AUTO TRADING: OFF\n"
        f"✋ Manual signals only"
    )

# ============================================================
# VIRTUAL TRADE / STATS
# ============================================================

def save_trade():

    with STATE_LOCK:
        atomic_json_save(
            TRADE_FILE,
            active_trade
        )


def load_trade():

    global active_trade

    active_trade = load_json(
        TRADE_FILE,
        None
    )

    if not isinstance(
        active_trade,
        dict
    ):
        active_trade = None


def load_stats():

    default = {
        "total": 0,
        "wins": 0,
        "losses": 0,
        "breakeven": 0,
        "points": 0.0
    }

    stats = load_json(
        STATS_FILE,
        default
    )

    if not isinstance(
        stats,
        dict
    ):
        return default

    for key in default:
        stats.setdefault(
            key,
            default[key]
        )

    return stats


def save_stats(stats):

    atomic_json_save(
        STATS_FILE,
        stats
    )


def close_virtual_trade(
    result,
    points
):

    global active_trade

    stats = load_stats()

    stats["total"] = (
        int(
            stats.get(
                "total",
                0
            )
        ) + 1
    )

    if result == "WIN":

        stats["wins"] = (
            int(
                stats.get(
                    "wins",
                    0
                )
            ) + 1
        )

    elif result == "LOSS":

        stats["losses"] = (
            int(
                stats.get(
                    "losses",
                    0
                )
            ) + 1
        )

    else:

        stats["breakeven"] = (
            int(
                stats.get(
                    "breakeven",
                    0
                )
            ) + 1
        )

    stats["points"] = round(
        float(
            stats.get(
                "points",
                0.0
            )
        ) +
        float(points),
        2
    )

    save_stats(
        stats
    )

    with STATE_LOCK:

        active_trade = None

        atomic_json_save(
            TRADE_FILE,
            None
        )


def open_virtual_trade(
    side,
    entry,
    analysis
):

    global active_trade

    if side not in (
        "BUY",
        "SELL"
    ):
        return False

    with STATE_LOCK:

        if active_trade:
            return False

        if side == "BUY":

            sl = (
                entry -
                SL_DISTANCE
            )

            tp1 = (
                entry +
                TP1_DISTANCE
            )

            tp2 = (
                entry +
                TP2_DISTANCE
            )

        else:

            sl = (
                entry +
                SL_DISTANCE
            )

            tp1 = (
                entry -
                TP1_DISTANCE
            )

            tp2 = (
                entry -
                TP2_DISTANCE
            )

        active_trade = {
            "side": side,
            "entry": round(
                entry,
                2
            ),
            "sl": round(
                sl,
                2
            ),
            "original_sl": round(
                sl,
                2
            ),
            "tp1": round(
                tp1,
                2
            ),
            "tp2": round(
                tp2,
                2
            ),
            "score": int(
                analysis["score"]
            ),
            "opened_at": utc_string(),
            "be": False,
            "tp1_hit": False,
            "status": "OPEN"
        }

        atomic_json_save(
            TRADE_FILE,
            active_trade
        )

    return True


# ============================================================
# MANAGE VIRTUAL TRADE
# ============================================================

def manage_virtual_trade(
    price
):

    global active_trade

    if not active_trade:
        return

    try:

        side = active_trade["side"]

        entry = float(
            active_trade["entry"]
        )

        sl = float(
            active_trade["sl"]
        )

        tp1 = float(
            active_trade["tp1"]
        )

        tp2 = float(
            active_trade["tp2"]
        )

        be = bool(
            active_trade.get(
                "be",
                False
            )
        )

        tp1_hit = bool(
            active_trade.get(
                "tp1_hit",
                False
            )
        )

        # ====================================================
        # BUY
        # ====================================================

        if side == "BUY":

            favorable_move = (
                price -
                entry
            )

            # BREAK EVEN
            if (
                not be
                and
                favorable_move >= BE_TRIGGER
            ):

                active_trade["sl"] = entry
                active_trade["be"] = True

                save_trade()

                send_telegram(
                    f"🛡 {APP_NAME}\n\n"
                    f"🔒 BREAK EVEN\n\n"
                    f"🟢 BUY\n\n"
                    f"💰 Entry: "
                    f"{entry:.2f}\n"
                    f"📍 Current: "
                    f"{price:.2f}\n"
                    f"➡️ SL moved to BE: "
                    f"{entry:.2f}\n\n"
                    f"AUTO TRADING: OFF"
                )

                sl = entry

            # TP1
            if (
                not tp1_hit
                and
                price >= tp1
            ):

                active_trade[
                    "tp1_hit"
                ] = True

                save_trade()

                send_telegram(
                    f"🎯 {APP_NAME}\n\n"
                    f"✅ TP1 HIT\n\n"
                    f"🟢 BUY\n\n"
                    f"Entry: "
                    f"{entry:.2f}\n"
                    f"TP1: "
                    f"{tp1:.2f}\n"
                    f"Result: "
                    f"+{tp1 - entry:.2f}\n\n"
                    f"🛡️ Virtual trade "
                    f"remains active to TP2\n"
                    f"AUTO TRADING: OFF"
                )

            # SL
            if price <= sl:

                points = (
                    price -
                    entry
                )

                result = (
                    "BREAKEVEN"
                    if active_trade.get(
                        "be"
                    )
                    else "LOSS"
                )

                close_virtual_trade(
                    result,
                    points
                )

                send_telegram(
                    f"📊 {APP_NAME}\n\n"
                    f"{'🟡 BREAK EVEN' if result == 'BREAKEVEN' else '❌ SL HIT'}\n\n"
                    f"🟢 BUY\n"
                    f"Entry: "
                    f"{entry:.2f}\n"
                    f"Close: "
                    f"{price:.2f}\n"
                    f"Result: "
                    f"{points:+.2f}\n\n"
                    f"AUTO TRADING: OFF"
                )

                return

            # TP2
            if price >= tp2:

                points = (
                    tp2 -
                    entry
                )

                close_virtual_trade(
                    "WIN",
                    points
                )

                send_telegram(
                    f"🏆 {APP_NAME}\n\n"
                    f"✅ TP2 HIT\n\n"
                    f"🟢 BUY\n\n"
                    f"Entry: "
                    f"{entry:.2f}\n"
                    f"TP2: "
                    f"{tp2:.2f}\n"
                    f"Result: "
                    f"+{points:.2f}\n\n"
                    f"AUTO TRADING: OFF"
                )

                return

        # ====================================================
        # SELL
        # ====================================================

        elif side == "SELL":

            favorable_move = (
                entry -
                price
            )

            # BREAK EVEN
            if (
                not be
                and
                favorable_move >= BE_TRIGGER
            ):

                active_trade["sl"] = entry
                active_trade["be"] = True

                save_trade()

                send_telegram(
                    f"🛡 {APP_NAME}\n\n"
                    f"🔒 BREAK EVEN\n\n"
                    f"🔴 SELL\n\n"
                    f"💰 Entry: "
                    f"{entry:.2f}\n"
                    f"📍 Current: "
                    f"{price:.2f}\n"
                    f"➡️ SL moved to BE: "
                    f"{entry:.2f}\n\n"
                    f"AUTO TRADING: OFF"
                )

                sl = entry

            # TP1
            if (
                not tp1_hit
                and
                price <= tp1
            ):

                active_trade[
                    "tp1_hit"
                ] = True

                save_trade()

                send_telegram(
                    f"🎯 {APP_NAME}\n\n"
                    f"✅ TP1 HIT\n\n"
                    f"🔴 SELL\n\n"
                    f"Entry: "
                    f"{entry:.2f}\n"
                    f"TP1: "
                    f"{tp1:.2f}\n"
                    f"Result: "
                    f"+{entry - tp1:.2f}\n\n"
                    f"🛡️ Virtual trade "
                    f"remains active to TP2\n"
                    f"AUTO TRADING: OFF"
                )

            # SL
            if price >= sl:

                points = (
                    entry -
                    price
                )

                result = (
                    "BREAKEVEN"
                    if active_trade.get(
                        "be"
                    )
                    else "LOSS"
                )

                close_virtual_trade(
                    result,
                    points
                )

                send_telegram(
                    f"📊 {APP_NAME}\n\n"
                    f"{'🟡 BREAK EVEN' if result == 'BREAKEVEN' else '❌ SL HIT'}\n\n"
                    f"🔴 SELL\n"
                    f"Entry: "
                    f"{entry:.2f}\n"
                    f"Close: "
                    f"{price:.2f}\n"
                    f"Result: "
                    f"{points:+.2f}\n\n"
                    f"AUTO TRADING: OFF"
                )

                return

            # TP2
            if price <= tp2:

                points = (
                    entry -
                    tp2
                )

                close_virtual_trade(
                    "WIN",
                    points
                )

                send_telegram(
                    f"🏆 {APP_NAME}\n\n"
                    f"✅ TP2 HIT\n\n"
                    f"🔴 SELL\n\n"
                    f"Entry: "
                    f"{entry:.2f}\n"
                    f"TP2: "
                    f"{tp2:.2f}\n"
                    f"Result: "
                    f"+{points:.2f}\n\n"
                    f"AUTO TRADING: OFF"
                )

                return

    except Exception as e:

        print(
            "GOLD SMART: "
            "virtual trade error:",
            repr(e)
        )


# ============================================================
# MARKET CYCLE
# ============================================================

def market_cycle(
    send_signal=True,
    force=False
):

    global last_cycle
    global last_error
    global last_data_counts
    global last_signal_time

    try:

        print(
            "GOLD SMART: "
            "cycle started"
        )

        # ----------------------------------------------------
        # SPOT
        # ----------------------------------------------------

        price = fetch_spot()

        print(
            f"GOLD SMART: "
            f"spot={price:.2f}"
        )

        # ----------------------------------------------------
        # INTRADAY
        # ----------------------------------------------------

        intraday = fetch_intraday()

        print(
            "GOLD SMART: "
            f"intraday rows="
            f"{len(intraday)}"
        )

        # ----------------------------------------------------
        # TIMEFRAMES
        # ----------------------------------------------------

        frames = build_timeframes(
            intraday
        )

        counts = {
            name: len(df)
            for name, df
            in frames.items()
        }

        last_data_counts = counts

        print(
            "GOLD SMART: "
            "timeframe counts:",
            counts
        )

        # XAUS provides up to 48h.
        #
        # Expected approximately:
        # M5  = hundreds
        # M15 = hundreds
        # H1  = around 47
        # H4  = around 11-12
        #
        # Closed candles remove current candle.

        minimums = {
            "M5": 30,
            "M15": 20,
            "H1": 7,
            "H4": 8
        }

        for name, minimum in (
            minimums.items()
        ):

            if len(
                frames[name]
            ) < minimum:

                message = (
                    f"GOLD SMART: "
                    f"insufficient "
                    f"{name} candles: "
                    f"{len(frames[name])}/"
                    f"{minimum}"
                )

                print(
                    message
                )

                last_error = message

                return None

        # ----------------------------------------------------
        # ANALYSIS
        # ----------------------------------------------------

        analysis = analyze_market(
            frames
        )

        last_cycle = utc_string()
        last_error = None

        print(
            "GOLD SMART:",
            analysis["side"],
            analysis["score"],
            analysis["signal_type"]
        )

        # ----------------------------------------------------
        # MANAGE VIRTUAL TRADE
        # ----------------------------------------------------

        manage_virtual_trade(
            price
        )
        # ----------------------------------------------------
        # SIGNAL
        # ----------------------------------------------------
    

        

    is_real_signal = (
        analysis["side"] in ("BUY", "SELL")
        and analysis["signal_type"] == "FULL"
        and analysis["score"] >= MIN_SCORE
    )

    # Автоматическая отправка только FULL-сигналов.is_real_signal = (
        analysis["side"] in ("BUY", "SELL")
        and analysis["signal_type"] == "FULL"
        and analysis["score"] >= MIN_SCORE
    )

        # Автоматическая отправка только FULL-сигналов.

        # Команда /signal не отправляет повторное уведомление

        # и не открывает виртуальную сделку.

        if send_signal and is_real_signal:

            cooldown_seconds = (

                SIGNAL_COOLDOWN_MIN * 60

            )

            cooldown_passed = (

                time.time() - last_signal_time

                >= cooldown_seconds

            )

            if cooldown_passed and not active_trade:

                trade_opened = open_virtual_trade(

                    analysis["side"],

                    price,

                    analysis

                )

                if trade_opened:

                    message = build_signal_message(

                        price,

                        analysis

                    )

                    send_telegram(message)

                    last_signal_time = time.time()

                    print(

                        "GOLD SMART: "

                        "FULL signal sent:",

                        analysis["side"],

                        analysis["score"]

                    )

        return analysis

    except Exception as e:

        last_error = (

            f"{type(e).__name__}: {e}"

        )

        print(

            "GOLD SMART market_cycle error:",

            repr(e)

        )

        traceback.print_exc()

        return None
def get_command(update):

    message = (
        update.get("message")
        or {}
    )

    if not message:
        return None, None, None

    chat = (
        message.get("chat")
        or {}
    )

    chat_id = chat.get(
        "id"
    )

    text_message = (
        message.get("text")
        or ""
    ).strip()

    if not text_message:
        return (
            None,
            chat_id,
            None
        )

    first = (
        text_message
        .split()[0]
    )

    command = (
        first
        .split("@")[0]
        .lower()
    )

    return (
        command,
        chat_id,
        text_message
    )


def command_start(
    chat_id
):

    return (
        f"🥇 {APP_NAME}\n\n"

        f"✅ GOLD SMART online\n"
        f"🤖 AUTO TRADING: OFF\n"
        f"✋ Manual signals only\n\n"

        f"📡 Data: XAUS\n"
        f"🧠 H4 → H1 → M15 → M5\n\n"

        f"Доступные команды:\n"

        f"/start — запуск\n"
        f"/status — состояние бота\n"
        f"/signal — текущий анализ\n"
        f"/test — тест Telegram\n"
        f"/stats — статистика\n"
        f"/help — помощь"
    )


def command_status():

    active = (
        "YES"
        if active_trade
        else "NO"
    )

    counts = (
        ", ".join(
            f"{k}:{v}"
            for k, v
            in last_data_counts.items()
        )
        if last_data_counts
        else "нет данных"
    )

    price_line = (
        f"💰 XAUUSD: "
        f"{last_price:.2f}\n"
        if last_price is not None
        else
        "💰 XAUUSD: unavailable\n"
    )

    return (
        f"🥇 {APP_NAME}\n\n"

        f"🟢 ENGINE: "
        f"{'ON' if ENGINE_STARTED else 'OFF'}\n"

        f"🤖 AUTO TRADING: OFF\n"

        f"{price_line}"

        f"📊 Candles: "
        f"{counts}\n"

        f"🕐 Last cycle: "
        f"{last_cycle or 'нет'}\n"

        f"⚠️ Error: "
        f"{last_error or 'нет'}\n"

        f"📦 Virtual trade: "
        f"{active}\n"
    )


def command_signal(
    chat_id
):

    analysis = market_cycle(
        send_signal=False,
        force=True
    )

    if (
        not analysis
        or
        last_price is None
    ):

        return (
            f"⚠️ {APP_NAME}\n\n"
            f"Не удалось получить "
            f"анализ XAU/USD.\n\n"
            f"Проверь Render logs."
        )

    return build_signal_message(
        last_price,
        analysis
    )


def command_test():

    return (
        f"🧪 {APP_NAME}\n\n"

        f"✅ Telegram connection works\n"
        f"📡 Webhook works\n"

        f"🤖 AUTO TRADING: OFF\n"
        f"✋ Manual signals only\n\n"

        f"🕐 {utc_string()}"
    )


def command_stats():

    stats = load_stats()

    total = int(
        stats.get(
            "total",
            0
        )
    )

    wins = int(
        stats.get(
            "wins",
            0
        )
    )

    losses = int(
        stats.get(
            "losses",
            0
        )
    )

    breakeven = int(
        stats.get(
            "breakeven",
            0
        )
    )

    points = float(
        stats.get(
            "points",
            0.0
        )
    )

    closed = (
        wins +
        losses +
        breakeven
    )

    winrate = (
        wins /
        closed *
        100
        if closed > 0
        else 0
    )

    return (
        f"📊 {APP_NAME} STATS\n\n"

        f"🔢 Total: "
        f"{total}\n"

        f"🏆 Wins: "
        f"{wins}\n"

        f"❌ Losses: "
        f"{losses}\n"

        f"🟡 Break-even: "
        f"{breakeven}\n"

        f"📈 Win rate: "
        f"{winrate:.1f}%\n"

        f"💵 Points: "
        f"{points:+.2f}\n\n"

        f"🤖 AUTO TRADING: OFF"
    )


def command_help():

    return (
        f"🥇 {APP_NAME}\n\n"

        f"/start — меню\n"
        f"/status — состояние engine/data\n"
        f"/signal — принудительный "
        f"текущий анализ\n"
        f"/test — проверка Telegram\n"
        f"/stats — виртуальная "
        f"статистика\n"
        f"/help — эта справка\n\n"

        f"⚠️ Бот не открывает "
        f"реальные сделки.\n"

        f"🤖 AUTO TRADING: OFF"
    )


def handle_telegram_update(
    update
):

    command, chat_id, text_message = (
        get_command(update)
    )

    if (
        not command
        or
        chat_id is None
    ):
        return

    # Security:
    # only configured Telegram chat
    # can use commands.

    if TELEGRAM_CHAT_ID:

        if str(chat_id) != str(
            TELEGRAM_CHAT_ID
        ):

            print(
                "GOLD SMART: "
                "ignored Telegram chat:",
                chat_id
            )

            return

    try:

        if command == "/start":

            reply = command_start(
                chat_id
            )

        elif command == "/status":

            reply = command_status()

        elif command == "/signal":

            reply = command_signal(
                chat_id
            )

        elif command == "/test":

            reply = command_test()

        elif command == "/stats":

            reply = command_stats()

        elif command == "/help":

            reply = command_help()

        else:

            reply = (
                "❓ Неизвестная команда.\n\n"
                "Используй /help"
            )

        send_telegram(
            reply,
            chat_id=chat_id
        )

    except Exception as e:

        print(
            "GOLD SMART: "
            "command error:",
            repr(e)
        )

        send_telegram(
            "⚠️ GOLD SMART "
            "command error.\n"
            "Проверь Render logs.",
            chat_id=chat_id
        )


# ============================================================
# FLASK ROUTES
# ============================================================

@app.get("/")
def home():

    return jsonify({
        "app": APP_NAME,
        "status": "online",
        "auto_trading": False,
        "engine": ENGINE_STARTED,
        "last_cycle": last_cycle,
        "last_price": last_price,
        "last_error": last_error
    })


@app.get("/health")
def health():

    return jsonify({
        "status": "ok",
        "app": APP_NAME,
        "engine": ENGINE_STARTED,
        "auto_trading": False,
        "time": utc_string()
    })


@app.get("/status")
def http_status():

    return jsonify({
        "app": APP_NAME,
        "engine": ENGINE_STARTED,
        "auto_trading": False,
        "last_cycle": last_cycle,
        "last_price": last_price,
        "last_error": last_error,
        "data_counts": last_data_counts,
        "active_trade": active_trade
    })


@app.post("/telegram")
def telegram_webhook():

    supplied_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        ""
    )

    if not hmac_compare(
        supplied_secret,
        WEBHOOK_SECRET
    ):

        return jsonify({
            "ok": False,
            "error": "unauthorized"
        }), 401

    try:

        update = request.get_json(
            silent=True
        ) or {}

        print(
            "GOLD SMART: "
            "Telegram update received"
        )

        thread = threading.Thread(
            target=handle_telegram_update,
            args=(update,),
            daemon=True
        )

        thread.start()

        return jsonify({
            "ok": True
        })

    except Exception as e:

        print(
            "GOLD SMART: "
            "webhook error:",
            repr(e)
        )

        return jsonify({
            "ok": False
        }), 500


@app.get("/telegram")
def telegram_get():

    return jsonify({
        "ok": True,
        "message": (
            "Telegram webhook endpoint"
        )
    })


def hmac_compare(
    a,
    b
):

    try:

        return __import__(
            "hmac"
        ).compare_digest(
            str(a),
            str(b)
        )

    except Exception:

        return False


# ============================================================
# ENGINE
# ============================================================

def engine_loop():

    global last_error

    print(
        f"{APP_NAME}: "
        f"engine loop started"
    )

    while True:

        try:

            market_cycle(
                send_signal=True,
                force=False
            )

        except Exception as e:

            last_error = (
                f"{type(e).__name__}: {e}"
            )

            print(
                "GOLD SMART ENGINE ERROR:",
                repr(e)
            )

            traceback.print_exc()

        time.sleep(
            POLL_SECONDS
        )


def start_engine():

    global ENGINE_STARTED

    with ENGINE_LOCK:

        if ENGINE_STARTED:
            return

        ENGINE_STARTED = True

        thread = threading.Thread(
            target=engine_loop,
            name="gold-smart-engine",
            daemon=True
        )

        thread.start()

        print(
            f"{APP_NAME}: "
            f"engine started"
        )


# ============================================================
# KEEPALIVE
# ============================================================

def keepalive_loop():

    print(
        "GOLD SMART: "
        "keepalive started"
    )

    while True:

        try:

            if RENDER_EXTERNAL_URL:

                requests.get(
                    RENDER_EXTERNAL_URL +
                    "/health",
                    timeout=15
                )

        except Exception as e:

            print(
                "GOLD SMART "
                "keepalive error:",
                repr(e)
            )

        time.sleep(
            600
        )


def start_keepalive():

    global KEEPALIVE_STARTED

    if not KEEPALIVE:
        return

    if KEEPALIVE_STARTED:
        return

    KEEPALIVE_STARTED = True

    thread = threading.Thread(
        target=keepalive_loop,
        name="gold-smart-keepalive",
        daemon=True
    )

    thread.start()


# ============================================================
# STARTUP
# ============================================================

def startup():

    global STARTUP_DONE

    if STARTUP_DONE:
        return

    STARTUP_DONE = True

    print("=" * 60)
    print(
        f"{APP_NAME} STARTING"
    )
    print("=" * 60)

    print(
        "AUTO TRADING:",
        AUTO_TRADING
    )

    print(
        "XAUS:",
        XAUS_SPOT_URL
    )

    print(
        "XAUS INTRADAY:",
        XAUS_INTRADAY_URL
    )

    print(
        "POLL:",
        POLL_SECONDS
    )

    print(
        "COOLDOWN:",
        SIGNAL_COOLDOWN_MIN,
        "min"
    )

    print(
        "INTRADAY HOURS:",
        INTRADAY_HOURS
    )

    print(
        "CLOSED CANDLES:",
        CLOSED_CANDLES
    )

    print(
        "STATS_FILE:",
        STATS_FILE
    )

    print(
        "TRADE_FILE:",
        TRADE_FILE
    )

    load_trade()

    # --------------------------------------------------------
    # TELEGRAM WEBHOOK
    # --------------------------------------------------------

    if BOT_TOKEN:

        configure_webhook()

        if (
            BOOT_NOTICE
            and
            TELEGRAM_CHAT_ID
        ):

            send_telegram(
                f"🟢 {APP_NAME}\n\n"
                f"Бот запущен.\n"
                f"📡 XAUS connected mode\n"
                f"🧠 H4 → H1 → M15 → M5\n"
                f"🤖 AUTO TRADING: OFF\n"
                f"✋ Manual signals only"
            )

    else:

        print(
            "GOLD SMART: "
            "BOT_TOKEN missing"
        )

    start_engine()

    start_keepalive()


# ============================================================
# IMPORTANT
#
# Gunicorn imports this module.
# Startup runs once and is protected
# by STARTUP_DONE.
# ============================================================

startup()


if __name__ == "__main__":

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
