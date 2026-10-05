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
# 🥇 GOLD SMART V5.9.3
# XAU/USD SMART SMC SIGNAL ENGINE
#
# DATA SOURCE: XAUS
# TIMEFRAMES: H4 → H1 → M15 → M5
#
# AUTO TRADING = OFF
# RISK = 1%
# CLOSED CANDLES = ON
#
# IMPORTANT:
# XAUS may temporarily provide less than 8 H4 candles.
# We NEVER fabricate candles.
#
# H4 can operate in LIMITED mode when only 2+ candles exist.
# In LIMITED H4 mode, H1 becomes the main higher-TF bias.
#
# TELEGRAM:
# /start
# /signal
# /status
# /stats
# /test
# /help
# ============================================================


APP_NAME = "GOLD SMART V5.9.3"

XAUS_SPOT_URL = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY_URL = "https://xaus.com/api/v1/intraday"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    ""
).strip()

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    ""
).strip()


# ============================================================
# SETTINGS
# ============================================================

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

# XAUS maximum documented intraday window.
INTRADAY_HOURS = 48


# ============================================================
# MINIMUM DATA
# ============================================================

# We NEVER fabricate candles.
#
# M5  = 30
# M15 = 16
# H1  = 7
#
# H4:
#   2 = minimum operational context
#   8 = preferred full H4 context
#
# Why:
# XAUS can temporarily return less historical intraday
# data than requested. Blocking the whole engine because
# H4 has only 2 real candles is unnecessary.
#
# When H4 < 8:
#   H4 status = LIMITED
#   H1 becomes the main higher-TF bias.
#
# Full H4 structure still activates automatically
# once enough real candles become available.

MIN_CANDLES = {
    "M5": 30,
    "M15": 16,
    "H1": 7,
    "H4": 2,
}

PREFERRED_H4_CANDLES = 8


# ============================================================
# FLASK
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
}

state_lock = threading.Lock()

virtual_trade = None

last_signal_time = 0

last_signal_key = None

last_early_signal_time = 0

engine_started = False

webhook_configured = False

last_data_error = None

last_data_error_time = 0


# ============================================================
# TELEGRAM
# ============================================================

def telegram_url(method):

    return (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )


def send_telegram(text_message):

    if not BOT_TOKEN or not CHAT_ID:

        print(
            "Telegram credentials missing"
        )

        return False

    try:

        response = requests.post(
            telegram_url("sendMessage"),
            json={
                "chat_id": CHAT_ID,
                "text": text_message,
            },
            timeout=15,
        )

        if response.status_code != 200:

            print(
                "Telegram error:",
                response.status_code,
                response.text,
            )

            return False

        return True

    except Exception as e:

        print(
            "Telegram exception:",
            repr(e),
        )

        return False


def configure_webhook():

    global webhook_configured

    if not BOT_TOKEN:

        print(
            "Webhook skipped: BOT_TOKEN missing"
        )

        return False

    if not RENDER_EXTERNAL_URL:

        print(
            "Webhook skipped: "
            "RENDER_EXTERNAL_URL missing"
        )

        return False

    base = RENDER_EXTERNAL_URL.rstrip("/")

    webhook_url = (
        f"{base}/telegram"
    )

    try:

        response = requests.post(
            telegram_url("setWebhook"),
            json={
                "url": webhook_url,
                "drop_pending_updates": False,
            },
            timeout=15,
        )

        print(
            "Webhook:",
            response.text,
        )

        webhook_configured = (
            response.status_code == 200
        )

        return webhook_configured

    except Exception as e:

        print(
            "Webhook setup error:",
            repr(e),
        )

        return False


def telegram_get_updates_info():

    if not BOT_TOKEN:
        return None

    try:

        response = requests.get(
            telegram_url(
                "getWebhookInfo"
            ),
            timeout=15,
        )

        if response.status_code != 200:
            return None

        return response.json()

    except Exception as e:

        print(
            "Webhook info error:",
            repr(e),
        )

        return None


# ============================================================
# STATISTICS
# ============================================================

def default_stats():

    return {
        "total_signals": 0,

        "win": 0,
        "loss": 0,
        "be": 0,
        "open": 0,

        "full_signals": 0,
        "full_win": 0,
        "full_loss": 0,

        "early_signals": 0,
        "early_win": 0,
        "early_loss": 0,

        "trades": [],
    }


def load_stats():

    if not os.path.exists(STATS_FILE):

        return default_stats()

    try:

        with open(
            STATS_FILE,
            "r",
            encoding="utf-8",
        ) as f:

            data = json.load(f)

        base = default_stats()

        if isinstance(data, dict):

            base.update(data)

        return base

    except Exception as e:

        print(
            "Stats load error:",
            repr(e),
        )

        return default_stats()


def save_stats(data=None):

    if data is None:
        data = stats

    try:

        with open(
            STATS_FILE,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2,
            )

    except Exception as e:

        print(
            "Stats save error:",
            repr(e),
        )


stats = load_stats()


# ============================================================
# SPOT PRICE
# ============================================================

def fetch_spot(force=False):

    now = time.time()

    if (
        not force
        and price_cache["price"] is not None
        and now - price_cache["time"]
        < CACHE_SECONDS
    ):

        return price_cache["price"]

    try:

        response = requests.get(
            XAUS_SPOT_URL,
            params={
                "fresh": int(now),
                "compact": 1,
            },
            timeout=15,
        )

        response.raise_for_status()

        data = response.json()

        price = None

        if isinstance(data, dict):

            xau = data.get("xau")

            if isinstance(xau, dict):

                price = xau.get(
                    "price"
                )

            if price is None:

                price = data.get(
                    "spot_usd_oz"
                )

            if price is None:

                price = data.get(
                    "price"
                )

        if price is None:

            raise ValueError(
                f"Unknown spot response: {data}"
            )

        price = float(price)

        if (
            not np.isfinite(price)
            or price <= 0
        ):

            raise ValueError(
                f"Invalid XAU price: {price}"
            )

        price_cache.update(
            {
                "price": price,
                "time": now,
            }
        )

        return price

    except Exception as e:

        print(
            "Spot error:",
            repr(e),
        )

        return price_cache["price"]


# ============================================================
# DATA ERROR LOGGER
# ============================================================

def log_data_error(message):

    global last_data_error
    global last_data_error_time

    now = time.time()

    # Avoid flooding Render logs every 60 seconds
    # with exactly the same error.
    if (
        last_data_error == message
        and now - last_data_error_time
        < 300
    ):

        return

    last_data_error = message
    last_data_error_time = now

    print(
        "Market data:",
        message,
    )


# ============================================================
# INTRADAY DATA
# ============================================================

def fetch_intraday(force=False):

    now = time.time()

    if (
        not force
        and intraday_cache["df"] is not None
        and now - intraday_cache["time"]
        < CACHE_SECONDS
    ):

        return (
            intraday_cache["df"]
            .copy()
        )

    try:

        response = requests.get(
            XAUS_INTRADAY_URL,
            params={
                "symbol": "xau",
                "hours": INTRADAY_HOURS,
                "fresh": int(now),
            },
            timeout=20,
        )

        response.raise_for_status()

        data = response.json()

        points = None

        if isinstance(data, dict):

            for key in (
                "points",
                "data",
                "series",
            ):

                value = data.get(key)

                if isinstance(
                    value,
                    list,
                ):

                    points = value
                    break

        elif isinstance(data, list):

            points = data

        if not points:

            raise ValueError(
                "No intraday data"
            )

        rows = []

        for item in points:

            if not isinstance(
                item,
                dict,
            ):

                continue

            timestamp = (
                item.get("t")
                or item.get("timestamp")
                or item.get("time")
                or item.get("date")
            )

            price = (
                item.get("p")
                or item.get("price")
                or item.get("close")
            )

            if (
                timestamp is None
                or price is None
            ):

                continue

            try:

                if isinstance(
                    timestamp,
                    (int, float),
                ):

                    unit = (
                        "ms"
                        if timestamp
                        > 100000000000
                        else "s"
                    )

                    dt = pd.to_datetime(
                        timestamp,
                        unit=unit,
                        utc=True,
                    )

                else:

                    dt = pd.to_datetime(
                        timestamp,
                        utc=True,
                    )

                value = float(price)

                if not np.isfinite(value):

                    continue

                rows.append(
                    {
                        "time": dt,
                        "price": value,
                    }
                )

            except Exception:

                continue

        if len(rows) < 20:

            raise ValueError(
                "Not enough raw intraday "
                f"points: {len(rows)}"
            )

        df = (
            pd.DataFrame(rows)
            .sort_values("time")
            .drop_duplicates("time")
            .set_index("time")
        )

        # XAUS records approximately 2-minute data.
        ohlc = (
            df["price"]
            .resample("2min")
            .ohlc()
            .dropna()
        )

        # Remove current incomplete 2-minute candle.
        if (
            CLOSED_CANDLES
            and len(ohlc) > 2
        ):

            ohlc = ohlc.iloc[:-1]

        if len(ohlc) < 20:

            raise ValueError(
                "Not enough normalized "
                f"candles: {len(ohlc)}"
            )

        print(
            "XAUS intraday loaded:",
            f"raw={len(rows)}",
            f"normalized_2m={len(ohlc)}",
            f"from={ohlc.index[0]}",
            f"to={ohlc.index[-1]}",
        )

        intraday_cache.update(
            {
                "df": ohlc.copy(),
                "time": now,
            }
        )

        return ohlc

    except Exception as e:

        print(
            "Intraday error:",
            repr(e),
        )

        if (
            intraday_cache["df"]
            is not None
        ):

            print(
                "Using cached intraday data"
            )

            return (
                intraday_cache["df"]
                .copy()
            )

        return None


# ============================================================
# RESAMPLE
# ============================================================

def resample_ohlc(
    df,
    timeframe,
):

    if df is None or len(df) < 5:

        return None

    rules = {
        "M5": "5min",
        "M15": "15min",
        "H1": "1h",
        "H4": "4h",
    }

    if timeframe not in rules:

        return None

    try:

        out = (
            df.resample(
                rules[timeframe]
            )
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                }
            )
            .dropna()
        )

        # Remove current incomplete TF candle.
        if (
            CLOSED_CANDLES
            and len(out) > 2
        ):

            out = out.iloc[:-1]

        return out

    except Exception as e:

        print(
            f"Resample error {timeframe}:",
            repr(e),
        )

        return None


# ============================================================
# INDICATORS
# ============================================================

def add_indicators(df):

    if df is None or len(df) < 5:

        return None

    x = df.copy()

    x["ema20"] = (
        x["close"]
        .ewm(
            span=20,
            adjust=False,
        )
        .mean()
    )

    x["ema50"] = (
        x["close"]
        .ewm(
            span=50,
            adjust=False,
        )
        .mean()
    )

    x["ema200"] = (
        x["close"]
        .ewm(
            span=200,
            adjust=False,
        )
        .mean()
    )

    delta = x["close"].diff()

    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )

    avg_gain = (
        gain
        .rolling(
            14,
            min_periods=1,
        )
        .mean()
    )

    avg_loss = (
        loss
        .rolling(
            14,
            min_periods=1,
        )
        .mean()
    )

    rs = (
        avg_gain
        / avg_loss.replace(
            0,
            np.nan,
        )
    )

    x["rsi"] = (
        100
        - 100 / (1 + rs)
    ).fillna(50)

    return x


# ============================================================
# MARKET STRUCTURE
# ============================================================

def structure_direction(
    df,
    lookback=30,
):

    if df is None or len(df) < 7:

        return "MIXED"

    x = df.tail(
        lookback
    )

    highs = []
    lows = []

    h = x["high"].values
    l = x["low"].values

    if len(x) < 5:

        return "MIXED"

    for i in range(
        2,
        len(x) - 2,
    ):

        if (
            h[i] > h[i - 1]
            and h[i] > h[i - 2]
            and h[i] > h[i + 1]
            and h[i] > h[i + 2]
        ):

            highs.append(h[i])

        if (
            l[i] < l[i - 1]
            and l[i] < l[i - 2]
            and l[i] < l[i + 1]
            and l[i] < l[i + 2]
        ):

            lows.append(l[i])

    if (
        len(highs) < 2
        or len(lows) < 2
    ):

        return "MIXED"

    hh = highs[-1]
    ph = highs[-2]

    ll = lows[-1]
    pl = lows[-2]

    if (
        hh > ph
        and ll > pl
    ):

        return "BULLISH"

    if (
        hh < ph
        and ll < pl
    ):

        return "BEARISH"

    return "MIXED"


def momentum_from_df(df):

    if df is None or len(df) < 6:

        return "MIXED"

    a = float(
        df["close"].iloc[-1]
    )

    b = float(
        df["close"].iloc[-5]
    )

    if a > b:

        return "BULLISH"

    if a < b:

        return "BEARISH"

    return "MIXED"


def detect_bos(
    df,
    lookback=8,
):

    if (
        df is None
        or len(df) < lookback + 2
    ):

        return "NONE"

    recent = df.iloc[
        -lookback - 1:-1
    ]

    last = df.iloc[-1]

    if (
        last["close"]
        > recent["high"].max()
    ):

        return "BULLISH BOS"

    if (
        last["close"]
        < recent["low"].min()
    ):

        return "BEARISH BOS"

    return "NONE"


def detect_liquidity(
    df,
    lookback=12,
):

    if (
        df is None
        or len(df) < lookback + 2
    ):

        return "NONE"

    previous = df.iloc[
        -lookback - 1:-1
    ]

    last = df.iloc[-1]

    ph = previous["high"].max()

    pl = previous["low"].min()

    if (
        last["high"] > ph
        and last["close"] < ph
    ):

        return "BSL SWEEP"

    if (
        last["low"] < pl
        and last["close"] > pl
    ):

        return "SSL SWEEP"

    return "NONE"


def detect_fvg(df):

    if df is None or len(df) < 5:

        return "NONE"

    a = df.iloc[-3]

    c = df.iloc[-1]

    if c["low"] > a["high"]:

        return "BULLISH FVG"

    if c["high"] < a["low"]:

        return "BEARISH FVG"

    return "NONE"


def detect_displacement(df):

    if df is None or len(df) < 12:

        return "NONE"

    bodies = (
        df["close"]
        - df["open"]
    ).abs()

    avg = bodies.iloc[
        -11:-1
    ].mean()

    body = bodies.iloc[-1]

    if (
        avg > 0
        and body >= avg * 1.8
    ):

        if (
            df["close"].iloc[-1]
            > df["open"].iloc[-1]
        ):

            return "BULLISH"

        return "BEARISH"

    return "NONE"


def detect_zone(df):

    if df is None or len(df) < 10:

        return "MID"

    recent = df.tail(20)

    hi = recent["high"].max()

    lo = recent["low"].min()

    price = df["close"].iloc[-1]

    mid = (
        hi + lo
    ) / 2

    if price < mid:

        return "DISCOUNT"

    if price > mid:

        return "PREMIUM"

    return "MID"


# ============================================================
# TREND SCORE
# ============================================================

def trend_score(df):

    if df is None or len(df) < 7:

        return {
            "direction": "MIXED",
            "score": 50,
            "structure": "MIXED",
            "momentum": "MIXED",
            "bos": "NONE",
        }

    x = add_indicators(df)

    if x is None:

        return {
            "direction": "MIXED",
            "score": 50,
            "structure": "MIXED",
            "momentum": "MIXED",
            "bos": "NONE",
        }

    last = x.iloc[-1]

    structure = structure_direction(x)

    momentum = momentum_from_df(x)

    bos = detect_bos(x)

    bull = 0

    bear = 0

    if structure == "BULLISH":

        bull += 40

    elif structure == "BEARISH":

        bear += 40

    close = float(
        last["close"]
    )

    e20 = float(
        last["ema20"]
    )

    e50 = float(
        last["ema50"]
    )

    e200 = float(
        last["ema200"]
    )

    if (
        close > e20
        and e20 > e50
        and e50 > e200
    ):

        bull += 25

    elif (
        close < e20
        and e20 < e50
        and e50 < e200
    ):

        bear += 25

    elif (
        close > e50
        and e20 > e50
    ):

        bull += 15

    elif (
        close < e50
        and e20 < e50
    ):

        bear += 15

    if momentum == "BULLISH":

        bull += 15

    elif momentum == "BEARISH":

        bear += 15

    if bos == "BULLISH BOS":

        bull += 20

    elif bos == "BEARISH BOS":

        bear += 20

    if bull == bear:

        return {
            "direction": "MIXED",
            "score": 50,
            "structure": structure,
            "momentum": momentum,
            "bos": bos,
        }

    if bull > bear:

        side = bull
        other = bear

        direction = (
            "BULLISH"
            if 50
            + (side - other) / 2
            >= 60
            else "MIXED"
        )

    else:

        side = bear
        other = bull

        direction = (
            "BEARISH"
            if 50
            + (side - other) / 2
            >= 60
            else "MIXED"
        )

    score = max(
        0,
        min(
            100,
            round(
                50
                + (side - other) / 2
            ),
        ),
    )

    return {
        "direction": direction,
        "score": score,
        "structure": structure,
        "momentum": momentum,
        "bos": bos,
    }


# ============================================================
# MARKET DATA
# ============================================================

def build_market_data():

    raw = fetch_intraday()

    if raw is None:

        return None

    result = {}

    for tf in (
        "M5",
        "M15",
        "H1",
        "H4",
    ):

        tf_df = resample_ohlc(
            raw,
            tf,
        )

        minimum = MIN_CANDLES[tf]

        count = (
            0
            if tf_df is None
            else len(tf_df)
        )

        if tf == "H4":

            if count >= PREFERRED_H4_CANDLES:

                status = "FULL"

            elif count >= 2:

                status = "LIMITED"

            else:

                status = "INSUFFICIENT"

        else:

            status = (
                "OK"
                if count >= minimum
                else "INSUFFICIENT"
            )

        print(
            f"Market data: "
            f"{tf}={count} candles "
            f"(minimum={minimum}) "
            f"status={status}"
        )

        if (
            tf_df is None
            or count < minimum
        ):

            log_data_error(
                f"{tf} has {count}, "
                f"need {minimum}"
            )

            return None

        result[tf] = add_indicators(
            tf_df
        )

    h4_status = (
        "FULL"
        if len(result["H4"])
        >= PREFERRED_H4_CANDLES
        else "LIMITED"
    )

    result["_h4_status"] = h4_status

    print(
        "Market data OK:",
        f"M5={len(result['M5'])}",
        f"M15={len(result['M15'])}",
        f"H1={len(result['H1'])}",
        f"H4={len(result['H4'])}",
        f"H4_STATUS={h4_status}",
    )

    return result


# ============================================================
# SIGNAL ENGINE
# ============================================================

def score_signal(market):

    trend_data = {
        tf: trend_score(
            market[tf]
        )
        for tf in (
            "H4",
            "H1",
            "M15",
            "M5",
        )
    }

    buy_score = 0.0

    sell_score = 0.0

    weights = {
        "H4": 20,
        "H1": 15,
        "M15": 15,
        "M5": 10,
    }

    for tf, data in trend_data.items():

        contribution = (
            data["score"]
            / 100
            * weights[tf]
        )

        if data["direction"] == "BULLISH":

            buy_score += contribution

        elif data["direction"] == "BEARISH":

            sell_score += contribution

    m5 = market["M5"]

    liquidity = detect_liquidity(
        m5
    )

    bos = detect_bos(m5)

    fvg = detect_fvg(m5)

    displacement = detect_displacement(
        m5
    )

    momentum = momentum_from_df(
        m5
    )

    zone = detect_zone(m5)

    rsi = float(
        m5["rsi"].iloc[-1]
    )

    # --------------------------------------------------------
    # SMC CONFLUENCE
    # --------------------------------------------------------

    if liquidity == "SSL SWEEP":

        buy_score += 10

    elif liquidity == "BSL SWEEP":

        sell_score += 10

    if bos == "BULLISH BOS":

        buy_score += 15

    elif bos == "BEARISH BOS":

        sell_score += 15

    if fvg == "BULLISH FVG":

        buy_score += 5

    elif fvg == "BEARISH FVG":

        sell_score += 5

    if displacement == "BULLISH":

        buy_score += 10

    elif displacement == "BEARISH":

        sell_score += 10

    if momentum == "BULLISH":

        buy_score += 5

    elif momentum == "BEARISH":

        sell_score += 5

    if rsi < 35:

        buy_score += 5

    elif rsi > 65:

        sell_score += 5

    if zone == "DISCOUNT":

        buy_score += 3

    elif zone == "PREMIUM":

        sell_score += 3

    # --------------------------------------------------------
    # HIGHER TIMEFRAME BIAS
    # --------------------------------------------------------

    h4d = trend_data[
        "H4"
    ]["direction"]

    h1d = trend_data[
        "H1"
    ]["direction"]

    h4_status = market.get(
        "_h4_status",
        "LIMITED",
    )

    # FULL H4 MODE
    if h4_status == "FULL":

        if (
            h4d == "BULLISH"
            and h1d == "BULLISH"
        ):

            higher_bias = "BULLISH"

        elif (
            h4d == "BEARISH"
            and h1d == "BEARISH"
        ):

            higher_bias = "BEARISH"

        else:

            higher_bias = "MIXED"

    # LIMITED H4 MODE
    else:

        # H1 becomes the operational
        # higher-TF bias.
        if h1d == "BULLISH":

            higher_bias = "BULLISH"

        elif h1d == "BEARISH":

            higher_bias = "BEARISH"

        else:

            higher_bias = "MIXED"

    # --------------------------------------------------------
    # DOMINANT SIDE
    # --------------------------------------------------------

    if buy_score > sell_score:

        dominant = "BUY"

        dominant_score = buy_score

    elif sell_score > buy_score:

        dominant = "SELL"

        dominant_score = sell_score

    else:

        dominant = "NONE"

        dominant_score = 0

    # --------------------------------------------------------
    # SIGNAL
    # --------------------------------------------------------

    signal = "WAIT"

    # FULL BUY
    if (
        dominant == "BUY"
        and buy_score >= MIN_SCORE
        and higher_bias == "BULLISH"
        and (
            liquidity == "SSL SWEEP"
            or bos == "BULLISH BOS"
            or displacement == "BULLISH"
        )
    ):

        signal = "BUY"

    # FULL SELL
    elif (
        dominant == "SELL"
        and sell_score >= MIN_SCORE
        and higher_bias == "BEARISH"
        and (
            liquidity == "BSL SWEEP"
            or bos == "BEARISH BOS"
            or displacement == "BEARISH"
        )
    ):

        signal = "SELL"

    # EARLY BUY
    elif (
        dominant == "BUY"
        and buy_score >= EARLY_SCORE
        and higher_bias != "BEARISH"
        and (
            liquidity == "SSL SWEEP"
            or bos == "BULLISH BOS"
        )
    ):

        signal = "EARLY BUY"

    # EARLY SELL
    elif (
        dominant == "SELL"
        and sell_score >= EARLY_SCORE
        and higher_bias != "BULLISH"
        and (
            liquidity == "BSL SWEEP"
            or bos == "BEARISH BOS"
        )
    ):

        signal = "EARLY SELL"

    return {
        "signal": signal,

        "buy_score": round(
            buy_score,
            1,
        ),

        "sell_score": round(
            sell_score,
            1,
        ),

        "dominant": dominant,

        "dominant_score": round(
            dominant_score,
            1,
        ),

        "higher_bias": higher_bias,

        "h4_status": h4_status,

        "trend_data": trend_data,

        "liquidity": liquidity,

        "bos": bos,

        "fvg": fvg,

        "displacement": displacement,

        "momentum": momentum,

        "zone": zone,

        "rsi": round(
            rsi,
            1,
        ),
    }


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def trend_line(
    tf,
    data,
):

    return (
        f"📊 {tf}: "
        f"{data['direction']} "
        f"({data['score']}/100)"
    )


def build_signal_message(
    price,
    analysis,
):

    td = analysis["trend_data"]

    signal = analysis["signal"]

    emoji = {
        "BUY": "🟢",
        "SELL": "🔴",
        "EARLY BUY": "🟡",
        "EARLY SELL": "🟠",
    }.get(
        signal,
        "⚪",
    )

    h4_status = analysis.get(
        "h4_status",
        "LIMITED",
    )

    if h4_status == "FULL":

        h4_note = (
            "🟢 H4 CONTEXT: FULL"
        )

    else:

        h4_note = (
            "🟡 H4 CONTEXT: LIMITED\n"
            "   H1 используется как "
            "основной higher-TF bias"
        )

    return f"""🥇 {APP_NAME}

{emoji} SIGNAL: {signal}

💰 XAUUSD: {price:.2f}

━━━━━━━━━━━━━━━━━━

{trend_line("H4", td["H4"])}
{trend_line("H1", td["H1"])}
{trend_line("M15", td["M15"])}
{trend_line("M5", td["M5"])}

{h4_note}

🧭 HIGHER TF BIAS:
{analysis["higher_bias"]}

━━━━━━━━━━━━━━━━━━

💧 M5 Liquidity:
{analysis["liquidity"]}

🔨 M5 BOS:
{analysis["bos"]}

🧩 M5 FVG:
{analysis["fvg"]}

💥 M5 Displacement:
{analysis["displacement"]}

📈 M5 Momentum:
{analysis["momentum"]}

🎯 M5 Zone:
{analysis["zone"]}

📊 RSI:
{analysis["rsi"]}

━━━━━━━━━━━━━━━━━━

🟢 BUY SCORE:
{analysis["buy_score"]}

🔴 SELL SCORE:
{analysis["sell_score"]}

🎯 MIN SCORE:
{MIN_SCORE}

🎯 EARLY SCORE:
{EARLY_SCORE}

━━━━━━━━━━━━━━━━━━

📡 SOURCE: XAUS

🕯 CLOSED CANDLES:
{"ON" if CLOSED_CANDLES else "OFF"}

🛡 AUTO TRADING:
{"ON" if AUTO_TRADING else "OFF"}

📉 RISK:
{RISK_PERCENT:.1f}%

💼 DEMO DEPOSIT:
${DEFAULT_DEPOSIT:.2f}"""


# ============================================================
# VIRTUAL TRADE
# ============================================================

def open_virtual_trade(
    direction,
    price,
    signal_type,
):

    global virtual_trade

    if (
        direction not in (
            "BUY",
            "SELL",
        )
        or signal_type not in (
            "BUY",
            "SELL",
        )
        or virtual_trade is not None
    ):

        return

    if direction == "BUY":

        sl = price - SL_DISTANCE

        tp = price + TP_DISTANCE

    else:

        sl = price + SL_DISTANCE

        tp = price - TP_DISTANCE

    virtual_trade = {

        "direction": direction,

        "signal_type": signal_type,

        "entry": round(
            price,
            2,
        ),

        "sl": round(
            sl,
            2,
        ),

        "tp": round(
            tp,
            2,
        ),

        "original_sl": round(
            sl,
            2,
        ),

        "be": False,

        "opened_at":
            datetime.now(
                timezone.utc
            ).isoformat(),
    }

    stats["open"] = 1

    stats["full_signals"] += 1

    save_stats()


def close_virtual_trade(
    result,
):

    global virtual_trade

    if virtual_trade is None:

        return

    direction = virtual_trade[
        "direction"
    ]

    signal_type = virtual_trade[
        "signal_type"
    ]

    stats["open"] = 0

    if result == "WIN":

        stats["win"] += 1

    elif result == "LOSS":

        stats["loss"] += 1

    elif result == "BE":

        stats["be"] += 1

    if signal_type in (
        "BUY",
        "SELL",
    ):

        if result == "WIN":

            stats["full_win"] += 1

        elif result == "LOSS":

            stats["full_loss"] += 1

    stats["trades"].append(
        {
            "direction":
                direction,

            "signal_type":
                signal_type,

            "entry":
                virtual_trade["entry"],

            "sl":
                virtual_trade["sl"],

            "tp":
                virtual_trade["tp"],

            "result":
                result,

            "closed_at":
                datetime.now(
                    timezone.utc
                ).isoformat(),
        }
    )

    virtual_trade = None

    save_stats()


def update_virtual_trade(
    price,
):

    global virtual_trade

    if virtual_trade is None:

        return None

    direction = virtual_trade[
        "direction"
    ]

    entry = virtual_trade[
        "entry"
    ]

    tp = virtual_trade[
        "tp"
    ]

    # --------------------------------------------------------
    # BUY
    # --------------------------------------------------------

    if direction == "BUY":

        move = (
            price - entry
        )

        if (
            not virtual_trade["be"]
            and move >= BE_TRIGGER
        ):

            virtual_trade["sl"] = entry

            virtual_trade["be"] = True

            save_stats()

            send_telegram(
                f"""🛡 {APP_NAME}

🔒 BREAK EVEN

🟢 BUY

💰 Entry: {entry:.2f}

📍 Current: {price:.2f}

➡️ SL moved to BE:
{entry:.2f}

AUTO TRADING: OFF"""
            )

        if (
            price
            <= virtual_trade["sl"]
        ):

            result = (
                "BE"
                if virtual_trade["be"]
                else "LOSS"
            )

            close_virtual_trade(
                result
            )

            return result

        if price >= tp:

            close_virtual_trade(
                "WIN"
            )

            return "WIN"

    # --------------------------------------------------------
    # SELL
    # --------------------------------------------------------

    else:

        move = (
            entry - price
        )

        if (
            not virtual_trade["be"]
            and move >= BE_TRIGGER
        ):

            virtual_trade["sl"] = entry

            virtual_trade["be"] = True

            save_stats()

            send_telegram(
                f"""🛡 {APP_NAME}

🔒 BREAK EVEN

🔴 SELL

💰 Entry: {entry:.2f}

📍 Current: {price:.2f}

➡️ SL moved to BE:
{entry:.2f}

AUTO TRADING: OFF"""
            )

        if (
            price
            >= virtual_trade["sl"]
        ):

            result = (
                "BE"
                if virtual_trade["be"]
                else "LOSS"
            )

            close_virtual_trade(
                result
            )

            return result

        if price <= tp:

            close_virtual_trade(
                "WIN"
            )

            return "WIN"

    return None


# ============================================================
# STATS MESSAGE
# ============================================================

def stats_message():

    total = stats.get(
        "total_signals",
        0,
    )

    win = stats.get(
        "win",
        0,
    )

    loss = stats.get(
        "loss",
        0,
    )

    be = stats.get(
        "be",
        0,
    )

    closed = (
        win
        + loss
        + be
    )

    wr = (
        win / closed * 100
        if closed
        else 0
    )

    full = stats.get(
        "full_signals",
        0,
    )

    fw = stats.get(
        "full_win",
        0,
    )

    fl = stats.get(
        "full_loss",
        0,
    )

    early = stats.get(
        "early_signals",
        0,
    )

    ew = stats.get(
        "early_win",
        0,
    )

    el = stats.get(
        "early_loss",
        0,
    )

    fwr = (
        fw
        / (fw + fl)
        * 100
        if fw + fl
        else 0
    )

    ewr = (
        ew
        / (ew + el)
        * 100
        if ew + el
        else 0
    )

    open_status = (
        "YES"
        if virtual_trade is not None
        else "NO"
    )

    return f"""🥇 {APP_NAME}

📊 VIRTUAL STATISTICS

━━━━━━━━━━━━━━━━━━

📡 TOTAL SIGNALS:
{total}

🟢 WIN:
{win}

🔴 LOSS:
{loss}

🛡 BREAK EVEN:
{be}

📂 CLOSED:
{closed}

🎯 WIN RATE:
{wr:.1f}%

━━━━━━━━━━━━━━━━━━

💎 FULL SIGNALS:
{full}

🟢 FULL WIN:
{fw}

🔴 FULL LOSS:
{fl}

📈 FULL WR:
{fwr:.1f}%

━━━━━━━━━━━━━━━━━━

🟡 EARLY SIGNALS:
{early}

🟢 EARLY WIN:
{ew}

🔴 EARLY LOSS:
{el}

📈 EARLY WR:
{ewr:.1f}%

━━━━━━━━━━━━━━━━━━

📌 OPEN VIRTUAL TRADE:
{open_status}

🛡 AUTO TRADING:
{"ON" if AUTO_TRADING else "OFF"}"""


# ============================================================
# STATUS
# ============================================================

def status_message():

    price = fetch_spot()

    price_text = (
        f"${price:.2f}"
        if price is not None
        else "N/A"
    )

    webhook = (
        "READY"
        if webhook_configured
        else "CHECK"
    )

    trade_text = "NONE"

    if virtual_trade is not None:

        trade_text = (
            f"{virtual_trade['direction']} "
            f"@ "
            f"{virtual_trade['entry']:.2f}"
        )

    return f"""🥇 {APP_NAME}

🟢 Engine: READY

📡 XAUS API: READY

📲 Telegram: {
    "READY"
    if BOT_TOKEN and CHAT_ID
    else "CHECK"
}

🔗 Webhook: {webhook}

💰 XAUUSD:
{price_text}

📊 MIN SCORE:
{MIN_SCORE}

🟡 EARLY SCORE:
{EARLY_SCORE}

🕯 Closed candles:
{"ON" if CLOSED_CANDLES else "OFF"}

⏱ XAUS HISTORY:
{INTRADAY_HOURS}h request

🛡 AUTO TRADING:
{"ON" if AUTO_TRADING else "OFF"}

📉 RISK:
{RISK_PERCENT:.1f}%

📂 Virtual trade:
{trade_text}"""


# ============================================================
# HELP
# ============================================================

def help_message():

    return f"""🥇 {APP_NAME}

📚 TELEGRAM COMMANDS

/start
Запуск бота

/status
Статус engine / XAUS / Telegram

/signal
Текущий анализ XAUUSD

/test
Тестовое сообщение Telegram

/stats
Статистика виртуальных сигналов

/help
Список команд

━━━━━━━━━━━━━━━━━━

📊 TIMEFRAMES

H4 → H1 → M15 → M5

💧 Liquidity
🔨 BOS
🧩 FVG
💥 Displacement
📈 Momentum
📊 RSI
🎯 Premium / Discount

━━━━━━━━━━━━━━━━━━

🟡 H4 LIMITED MODE:
если XAUS временно не даёт
полную H4 историю,
H1 используется как
основной higher-TF bias.

🛡 AUTO TRADING: OFF
📉 RISK: 1%"""


# ============================================================
# ANALYSIS
# ============================================================

def perform_analysis():

    price = fetch_spot()

    market = build_market_data()

    if market is None:

        return {
            "ok": False,
            "price": price,
            "analysis": None,
        }

    analysis = score_signal(
        market
    )

    return {
        "ok": True,
        "price": price,
        "analysis": analysis,
    }


# ============================================================
# SIGNAL KEY
# ============================================================

def make_signal_key(
    analysis,
):

    if not analysis:

        return None

    return (
        analysis["signal"],

        analysis["higher_bias"],

        analysis["dominant"],

        round(
            analysis["dominant_score"],
            1,
        ),

        analysis["liquidity"],

        analysis["bos"],

        analysis["fvg"],
    )


# ============================================================
# SIGNAL PROCESSING
# ============================================================

def process_signal(
    send_message=True,
):

    global last_signal_time
    global last_signal_key
    global last_early_signal_time

    result = perform_analysis()

    if not result["ok"]:

        print(
            "Signal processing: "
            "market data unavailable"
        )

        return result

    price = result["price"]

    analysis = result["analysis"]

    signal = analysis["signal"]

    print(
        f"📊 {signal} | "
        f"XAUUSD {price:.2f} | "
        f"BUY {analysis['buy_score']} | "
        f"SELL {analysis['sell_score']} | "
        f"HIGHER {analysis['higher_bias']} | "
        f"H4 {analysis['h4_status']}"
    )

    if signal == "WAIT":

        return result

    now = time.time()

    key = make_signal_key(
        analysis
    )

    # --------------------------------------------------------
    # FULL SIGNAL
    # --------------------------------------------------------

    if signal in (
        "BUY",
        "SELL",
    ):

        if (
            last_signal_key == key
            and now
            - last_signal_time
            < COOLDOWN_MIN * 60
        ):

            print(
                "Full signal cooldown"
            )

            return result

        last_signal_key = key

        last_signal_time = now

        stats["total_signals"] += 1

        if send_message:

            message = (
                build_signal_message(
                    price,
                    analysis,
                )
            )

            send_telegram(
                message
            )

        # Only one virtual trade.
        if virtual_trade is None:

            open_virtual_trade(
                signal,
                price,
                signal,
            )

        return result

    # --------------------------------------------------------
    # EARLY SIGNAL
    # --------------------------------------------------------

    if signal in (
        "EARLY BUY",
        "EARLY SELL",
    ):

        if (
            now
            - last_early_signal_time
            < COOLDOWN_MIN * 60
        ):

            print(
                "Early signal cooldown"
            )

            return result

        last_early_signal_time = now

        stats["total_signals"] += 1

        stats["early_signals"] += 1

        if send_message:

            message = (
                build_signal_message(
                    price,
                    analysis,
                )
            )

            send_telegram(
                message
            )

        # EARLY does not create
        # virtual trade.

        save_stats()

        return result

    return result


# ============================================================
# ENGINE LOOP
# ============================================================

def engine_loop():

    global engine_started

    if engine_started:

        print(
            "Engine loop already running"
        )

        return

    engine_started = True

    print(
        f"🚀 {APP_NAME} engine started"
    )

    while True:

        try:

            price = fetch_spot()

            if price is not None:

                update_virtual_trade(
                    price
                )

            process_signal(
                send_message=True
            )

        except Exception as e:

            print(
                "ENGINE ERROR:",
                repr(e),
            )

        time.sleep(
            POLL_SECONDS
        )


# ============================================================
# TELEGRAM COMMAND HANDLER
# ============================================================

def handle_command(
    command,
):

    command = (
        command
        .strip()
        .lower()
        .split()[0]
        if command
        else ""
    )

    if command == "/start":

        return f"""🥇 {APP_NAME}

🟢 BOT ONLINE

XAU/USD SMART SMC ENGINE

📡 XAUS: READY

🕯 Closed candles: ON

🛡 Auto trading: OFF

📉 Risk: 1%

Используй:

/signal
/status
/stats
/test
/help"""

    if command == "/status":

        return status_message()

    if command == "/signal":

        result = perform_analysis()

        if not result["ok"]:

            return f"""🥇 {APP_NAME}

⚠️ SIGNAL: WAIT

❌ Недостаточно реальных
данных XAU/USD.

Проверь Render Logs.

Бот НЕ создаёт
искусственные свечи."""

        return build_signal_message(
            result["price"],
            result["analysis"],
        )

    if command == "/stats":

        return stats_message()

    if command == "/test":

        return f"""🥇 {APP_NAME}

🧪 TEST MESSAGE

🟢 Telegram connection works.

📡 XAUS:
READY

🛡 AUTO TRADING:
OFF

📉 RISK:
{RISK_PERCENT:.1f}%"""

    if command == "/help":

        return help_message()

    return (
        "❓ Неизвестная команда.\n\n"
        "Используй /help"
    )


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

@app.route(
    "/telegram",
    methods=["POST"],
)
def telegram_webhook():

    try:

        update = request.get_json(
            silent=True
        )

        if not update:

            return {
                "ok": True
            }

        message = update.get(
            "message"
        )

        if not message:

            return {
                "ok": True
            }

        text_message = (
            message.get("text")
            or ""
        ).strip()

        if not text_message:

            return {
                "ok": True
            }

        chat = (
            message.get("chat")
            or {}
        )

        incoming_chat_id = str(
            chat.get(
                "id",
                "",
            )
        )

        print(
            "Telegram command:",
            text_message,
            "chat_id:",
            incoming_chat_id,
        )

        if (
            CHAT_ID
            and incoming_chat_id
            != CHAT_ID
        ):

            print(
                "Ignoring unauthorized "
                "chat:",
                incoming_chat_id,
            )

            return {
                "ok": True
            }

        response_text = (
            handle_command(
                text_message
            )
        )

        send_telegram(
            response_text
        )

        return {
            "ok": True
        }

    except Exception as e:

        print(
            "Webhook handler error:",
            repr(e),
        )

        return {
            "ok": True
        }


# ============================================================
# HEALTH
# ============================================================

@app.route(
    "/",
    methods=["GET"],
)
def home():

    return {
        "app": APP_NAME,

        "status": "READY",

        "auto_trading":
            AUTO_TRADING,

        "risk_percent":
            RISK_PERCENT,

        "data_source":
            "XAUS",

        "timeframes": [
            "H4",
            "H1",
            "M15",
            "M5",
        ],

        "closed_candles":
            CLOSED_CANDLES,

        "h4_min_candles":
            MIN_CANDLES["H4"],

        "h4_preferred_candles":
            PREFERRED_H4_CANDLES,
    }


@app.route(
    "/health",
    methods=["GET"],
)
def health():

    return {
        "app": APP_NAME,

        "status": "READY",

        "engine_started":
            engine_started,

        "telegram": bool(
            BOT_TOKEN
            and CHAT_ID
        ),

        "webhook":
            webhook_configured,

        "xau_price":
            price_cache["price"],
    }


# ============================================================
# STARTUP
# ============================================================

def startup():

    print(
        "=========================================="
    )

    print(
        f"🥇 {APP_NAME}"
    )

    print(
        "=========================================="
    )

    print(
        "AUTO TRADING:",
        AUTO_TRADING,
    )

    print(
        "RISK:",
        RISK_PERCENT,
        "%"
    )

    print(
        "DATA SOURCE:",
        "XAUS",
    )

    print(
        "CLOSED CANDLES:",
        CLOSED_CANDLES,
    )

    print(
        "INTRADAY HOURS:",
        INTRADAY_HOURS,
    )

    print(
        "MIN CANDLES:",
        MIN_CANDLES,
    )

    print(
        "PREFERRED H4:",
        PREFERRED_H4_CANDLES,
    )

    print(
        "BOT TOKEN:",
        "OK"
        if BOT_TOKEN
        else "MISSING",
    )

    print(
        "CHAT ID:",
        "OK"
        if CHAT_ID
        else "MISSING",
    )

    print(
        "RENDER URL:",
        RENDER_EXTERNAL_URL
        if RENDER_EXTERNAL_URL
        else "MISSING",
    )

    # --------------------------------------------------------
    # WEBHOOK
    # --------------------------------------------------------

    if BOT_TOKEN:

        configure_webhook()

        info = (
            telegram_get_updates_info()
        )

        if info:

            result = info.get(
                "result",
                {}
            )

            print(
                "Webhook URL:",
                result.get(
                    "url",
                    "",
                )
            )

            print(
                "Pending updates:",
                result.get(
                    "pending_update_count",
                    0,
                )
            )

    # --------------------------------------------------------
    # TEST XAUS
    # --------------------------------------------------------

    price = fetch_spot(
        force=True
    )

    print(
        "Initial XAUUSD:",
        price,
    )

    # --------------------------------------------------------
    # ENGINE
    # --------------------------------------------------------

    thread = threading.Thread(
        target=engine
