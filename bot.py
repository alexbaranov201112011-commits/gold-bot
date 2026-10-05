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
# 🥇 GOLD SMART V5.9.1
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
# /signal
# /status
# /stats
# /test
# /help
# ============================================================


APP_NAME = "GOLD SMART V5.9.1"

XAUS_SPOT_URL = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY_URL = "https://xaus.com/api/v1/intraday"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()


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


# ============================================================
# XAUS
# ============================================================

# XAUS intraday supports up to 48 hours.
INTRADAY_HOURS = 48


# IMPORTANT:
# Do NOT require 12 H1 candles.
#
# Current trend_score() can operate from 7 candles.
# The previous value 12 caused the entire engine to stop
# when XAUS temporarily returned only 7-8 H1 candles.
#
# We do NOT fabricate candles.
MIN_CANDLES = {
    "M5": 30,
    "M15": 16,
    "H1": 7,
    "H4": 8,
}


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


# ============================================================
# TELEGRAM
# ============================================================

def telegram_url(method):
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"


def send_telegram(text_message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram credentials missing")
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
            print("Telegram error:", response.text)
            return False

        return True

    except Exception as e:
        print("Telegram exception:", repr(e))
        return False


def configure_webhook():
    if not BOT_TOKEN or not RENDER_EXTERNAL_URL:
        print(
            "Webhook setup skipped: "
            "BOT_TOKEN or RENDER_EXTERNAL_URL missing"
        )
        return False

    base = RENDER_EXTERNAL_URL.rstrip("/")
    webhook_url = f"{base}/telegram"

    try:
        response = requests.post(
            telegram_url("setWebhook"),
            json={
                "url": webhook_url,
                "drop_pending_updates": False,
            },
            timeout=15,
        )

        print("Webhook:", response.text)

        return response.status_code == 200

    except Exception as e:
        print("Webhook setup error:", repr(e))
        return False


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
        print("Stats load error:", repr(e))
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
        print("Stats save error:", repr(e))


stats = load_stats()


# ============================================================
# SPOT PRICE
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
                price = xau.get("price")

            if price is None:
                price = data.get("spot_usd_oz")

            if price is None:
                price = data.get("price")

        if price is None:
            raise ValueError(
                f"Unknown spot response: {data}"
            )

        price = float(price)

        if not np.isfinite(price) or price <= 0:
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
# INTRADAY DATA
# ============================================================

def fetch_intraday(force=False):

    now = time.time()

    if (
        not force
        and intraday_cache["df"] is not None
        and now - intraday_cache["time"] < CACHE_SECONDS
    ):
        return intraday_cache["df"].copy()

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

                if isinstance(
                    data.get(key),
                    list,
                ):
                    points = data[key]
                    break

        elif isinstance(data, list):

            points = data

        if not points:
            raise ValueError(
                "No intraday data"
            )

        rows = []

        for item in points:

            if not isinstance(item, dict):
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

            if timestamp is None or price is None:
                continue

            try:

                if isinstance(
                    timestamp,
                    (int, float),
                ):

                    unit = (
                        "ms"
                        if timestamp > 100000000000
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
                "Not enough raw intraday points: "
                f"{len(rows)}"
            )

        df = (
            pd.DataFrame(rows)
            .sort_values("time")
            .drop_duplicates("time")
            .set_index("time")
        )

        # XAUS source is approximately 2-minute data.
        # Keep native source cadence.
        ohlc = (
            df["price"]
            .resample("2min")
            .ohlc()
            .dropna()
        )

        # Ignore the current incomplete source candle.
        if CLOSED_CANDLES and len(ohlc) > 2:
            ohlc = ohlc.iloc[:-1]

        if len(ohlc) < 20:

            raise ValueError(
                "Not enough normalized intraday candles: "
                f"{len(ohlc)}"
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

        if intraday_cache["df"] is not None:
            print(
                "Using cached intraday data"
            )

            return intraday_cache["df"].copy()

        return None


# ============================================================
# RESAMPLE
# ============================================================

def resample_ohlc(df, timeframe):

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

        if CLOSED_CANDLES and len(out) > 2:
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

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

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

    x = df.tail(lookback)

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

    if len(highs) < 2 or len(lows) < 2:
        return "MIXED"

    hh = highs[-1]
    ph = highs[-2]

    ll = lows[-1]
    pl = lows[-2]

    if hh > ph and ll > pl:
        return "BULLISH"

    if hh < ph and ll < pl:
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

    if df is None or len(df) < lookback + 2:
        return "NONE"

    recent = df.iloc[
        -lookback - 1:-1
    ]

    last = df.iloc[-1]

    if last["close"] > recent["high"].max():
        return "BULLISH BOS"

    if last["close"] < recent["low"].min():
        return "BEARISH BOS"

    return "NONE"


def detect_liquidity(
    df,
    lookback=12,
):

    if df is None or len(df) < lookback + 2:
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

    mid = (hi + lo) / 2

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

    close = float(last["close"])
    e20 = float(last["ema20"])
    e50 = float(last["ema50"])
    e200 = float(last["ema200"])

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
    else:
        side = bear
        other = bull

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

    if bull > bear and score >= 60:
        direction = "BULLISH"

    elif bear > bull and score >= 60:
        direction = "BEARISH"

    else:
        direction = "MIXED"

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

        print(
            f"Market data: "
            f"{tf}={count} candles "
            f"(minimum={minimum})"
        )

        if (
            tf_df is None
            or count < minimum
        ):

            print(
                f"Market data unavailable: "
                f"{tf} has {count}, "
                f"need {minimum}"
            )

            return None

        result[tf] = add_indicators(
            tf_df
        )

    print(
        "Market data OK:",
        "M5=", len(result["M5"]),
        "M15=", len(result["M15"]),
        "H1=", len(result["H1"]),
        "H4=", len(result["H4"]),
    )

    return result


# ============================================================
# SIGNAL ENGINE
# ============================================================

def score_signal(market):

    h4 = market["H4"]
    h1 = market["H1"]
    m15 = market["M15"]
    m5 = market["M5"]

    trend_data = {
        tf: trend_score(market[tf])
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

    liquidity = detect_liquidity(m5)
    bos = detect_bos(m5)
    fvg = detect_fvg(m5)
    displacement = detect_displacement(m5)
    momentum = momentum_from_df(m5)
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

    h4d = trend_data["H4"]["direction"]
    h1d = trend_data["H1"]["direction"]

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

def trend_line(tf, data):

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

    return f"""🥇 {APP_NAME}

{emoji} SIGNAL: {signal}

💰 XAUUSD: {price:.2f}

━━━━━━━━━━━━━━━━━━

{trend_line("H4", td["H4"])}
{trend_line("H1", td["H1"])}
{trend_line("M15", td["M15"])}
{trend_line("M5", td["M5"])}

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

🟢 BUY SCORE: {analysis["buy_score"]}
🔴 SELL SCORE: {analysis["sell_score"]}

🎯 MIN SCORE: {MIN_SCORE}

━━━━━━━━━━━━━━━━━━

📡 SOURCE: XAUS
🕯 CLOSED CANDLES: {"ON" if CLOSED_CANDLES else "OFF"}

🛡 AUTO TRADING: {"ON" if AUTO_TRADING else "OFF"}
📉 RISK: {RISK_PERCENT:.1f}%

💼 DEMO DEPOSIT: ${DEFAULT_DEPOSIT:.2f}"""


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
        direction not in ("BUY", "SELL")
        or signal_type not in ("BUY", "SELL")
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

    save_stats()


def close_virtual_trade(
    result,
):

    global virtual_trade

    if virtual_trade is None:
        return

    direction = virtual_trade["direction"]
    signal_type = virtual_trade["signal_type"]

    stats["open"] = 0

    if result == "WIN":

        stats["win"] += 1

    elif result == "LOSS":

        stats["loss"] += 1

    elif result == "BE":

        stats["be"] += 1

    # IMPORTANT:
    # full_signals is NOT incremented here.
    # It is incremented only when the FULL signal is opened.

    if signal_type == "BUY" or signal_type == "SELL":

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

    direction = virtual_trade["direction"]

    entry = virtual_trade["entry"]

    tp = virtual_trade["tp"]

    # --------------------------------------------------------
    # BUY
    # --------------------------------------------------------

    if direction == "BUY":

        move = price - entry

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
➡️ SL moved to BE: {entry:.2f}

AUTO TRADING: OFF"""
            )

        if price <= virtual_trade["sl"]:

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

        move = entry - price

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
➡️ SL moved to BE: {entry:.2f}

AUTO TRADING: OFF"""
            )

        if price >= virtual_trade["sl"]:

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
        win
        / closed
        * 100
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
