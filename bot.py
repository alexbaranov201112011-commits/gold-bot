import os
import json
import time
import threading
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests
from flask import Flask, request, jsonify

# =========================================================
# GOLD SMART V5.8
# XAUUSD SIGNAL ENGINE + VIRTUAL TRADE STATISTICS
# DATA: XAUS
# AUTO TRADING: OFF
# =========================================================

APP_NAME = "GOLD SMART V5.8"

XAUS_SPOT_URL = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY_URL = "https://xaus.com/api/v1/intraday"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

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

STATS_FILE = "gold_stats.json"

AUTO_TRADING = False
CLOSED_CANDLES = True

app = Flask(__name__)

session = requests.Session()
session.headers.update({
    "User-Agent": "GOLD-SMART-V5.8/1.0"
})

state_lock = threading.Lock()

price_cache = {
    "value": None,
    "timestamp": 0.0,
}

candle_cache = {
    "df": None,
    "timestamp": 0.0,
}

last_signal_time = 0.0
last_signal_side = None
open_trade = None


# =========================================================
# BASIC HELPERS
# =========================================================

def now_utc():
    return datetime.now(timezone.utc)


def now_text():
    return now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_float(value, default=np.nan):
    try:
        return float(value)
    except Exception:
        return default


def fmt_price(value):
    if value is None or not np.isfinite(value):
        return "N/A"
    return f"{value:.2f}"


# =========================================================
# STATS
# =========================================================

def default_stats():
    return {
        "total": 0,
        "win": 0,
        "loss": 0,
        "be": 0,
        "open": 0,
        "closed_trades": []
    }


def load_stats():
    try:
        if not os.path.exists(STATS_FILE):
            return default_stats()

        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        base = default_stats()

        if isinstance(data, dict):
            base.update(data)

        for key in ("total", "win", "loss", "be", "open"):
            try:
                base[key] = int(base.get(key, 0))
            except Exception:
                base[key] = 0

        if not isinstance(base.get("closed_trades"), list):
            base["closed_trades"] = []

        return base

    except Exception:
        return default_stats()


stats = load_stats()


def save_stats():
    tmp = STATS_FILE + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            stats,
            f,
            ensure_ascii=False,
            indent=2
        )

    os.replace(tmp, STATS_FILE)


# =========================================================
# TELEGRAM
# =========================================================

def telegram_send(text_message):
    if not BOT_TOKEN or not CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    try:
        response = session.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": text_message
            },
            timeout=15
        )

        return response.ok

    except Exception as exc:
        print(f"[TELEGRAM] send error: {exc}")
        return False


def telegram_reply(chat_id, text_message):
    if not BOT_TOKEN:
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    try:
        response = session.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text_message
            },
            timeout=15
        )

        return response.ok

    except Exception as exc:
        print(f"[TELEGRAM] reply error: {exc}")
        return False


# =========================================================
# XAUS SPOT
# =========================================================

def fetch_spot(force=False):
    global price_cache

    current = time.time()

    if (
        not force
        and price_cache["value"] is not None
        and current - price_cache["timestamp"] < CACHE_SECONDS
    ):
        return price_cache["value"]

    try:
        response = session.get(
            XAUS_SPOT_URL,
            params={
                "currency": "USD",
                "unit": "oz",
                "compact": "1"
            },
            timeout=15
        )

        response.raise_for_status()

        data = response.json()

        price = None

        # PRIMARY XAUS FIELD
        xau = data.get("xau")

        if isinstance(xau, dict):
            price = safe_float(xau.get("price"))

        # FALLBACK XAUS FIELD
        if not np.isfinite(price):
            price = safe_float(
                data.get("spot_usd_oz")
            )

        if not np.isfinite(price) or price <= 0:
            raise ValueError(
                f"Invalid XAUS response: {data}"
            )

        price_cache = {
            "value": float(price),
            "timestamp": current
        }

        return float(price)

    except Exception as exc:
        print(f"[XAUS] spot error: {exc}")

        if price_cache["value"] is not None:
            return price_cache["value"]

        return None


# =========================================================
# XAUS INTRADAY
# =========================================================

def parse_intraday_points(data):
    points = None

    if isinstance(data, dict):

        points = data.get("points")

        if points is None:
            points = data.get("data")

        if points is None:
            series = data.get("series")

            if isinstance(series, list):
                points = series

    if not isinstance(points, list):
        return []

    rows = []

    for item in points:

        if not isinstance(item, dict):
            continue

        ts = item.get("t")

        if ts is None:
            ts = item.get("timestamp")

        price = item.get("p")

        if price is None:
            price = item.get("price")

        if ts is None or price is None:
            continue

        price = safe_float(price)

        if not np.isfinite(price):
            continue

        try:

            if isinstance(ts, (int, float)):

                if ts > 10_000_000_000:
                    timestamp = pd.to_datetime(
                        ts,
                        unit="ms",
                        utc=True
                    )
                else:
                    timestamp = pd.to_datetime(
                        ts,
                        unit="s",
                        utc=True
                    )

            else:

                timestamp = pd.to_datetime(
                    ts,
                    utc=True
                )

            rows.append(
                (
                    timestamp,
                    float(price)
                )
            )

        except Exception:
            continue

    return rows


def fetch_intraday(force=False):
    global candle_cache

    current = time.time()

    if (
        not force
        and candle_cache["df"] is not None
        and current - candle_cache["timestamp"] < CACHE_SECONDS
    ):
        return candle_cache["df"].copy()

    try:

        response = session.get(
            XAUS_INTRADAY_URL,
            params={
                "symbol": "xau",
                "hours": 48
            },
            timeout=20
        )

        response.raise_for_status()

        data = response.json()

        rows = parse_intraday_points(data)

        if len(rows) < 20:
            raise ValueError(
                f"Not enough intraday points: {len(rows)}"
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

        df = df.set_index("time")
        df = df.sort_index()

        df["close"] = pd.to_numeric(
            df["close"],
            errors="coerce"
        )

        df = df.dropna(
            subset=["close"]
        )

        if len(df) < 20:
            raise ValueError(
                "Too few valid price rows"
            )

        candle_cache = {
            "df": df.copy(),
            "timestamp": current
        }

        return df.copy()

    except Exception as exc:

        print(
            f"[XAUS] intraday error: {exc}"
        )

        if candle_cache["df"] is not None:
            return candle_cache["df"].copy()

        return None


# =========================================================
# CANDLE BUILDING
# =========================================================

def resample_ohlc(raw_df, timeframe):

    if raw_df is None or raw_df.empty:
        return pd.DataFrame()

    data = raw_df.copy()

    ohlc = data["close"].resample(
        timeframe
    ).ohlc()

    ohlc["volume"] = (
        data["close"]
        .resample(timeframe)
        .size()
    )

    ohlc = ohlc.dropna(
        subset=[
            "open",
            "high",
            "low",
            "close"
        ]
    )

    # Use closed candles only.
    if CLOSED_CANDLES and len(ohlc) > 1:
        ohlc = ohlc.iloc[:-1]

    return ohlc


# =========================================================
# INDICATORS
# =========================================================

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
        min_periods=period,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(
        0,
        np.nan
    )

    result = (
        100
        - (
            100
            / (1 + rs)
        )
    )

    return result.fillna(50)


def add_indicators(df):

    data = df.copy()

    data["ema20"] = ema(
        data["close"],
        20
    )

    data["ema50"] = ema(
        data["close"],
        50
    )

    data["ema200"] = ema(
        data["close"],
        200
    )

    data["rsi"] = rsi(
        data["close"],
        14
    )

    return data


# =========================================================
# MARKET STRUCTURE
# =========================================================

def trend_from_df(df):

    if df is None or len(df) < 50:
        return "MIXED"

    last = df.iloc[-1]

    if (
        last["close"] > last["ema20"]
        and last["ema20"] > last["ema50"]
    ):
        return "BULLISH"

    if (
        last["close"] < last["ema20"]
        and last["ema20"] < last["ema50"]
    ):
        return "BEARISH"

    return "MIXED"


def momentum_from_df(df):

    if df is None or len(df) < 5:
        return "NEUTRAL"

    last = df.iloc[-1]
    previous = df.iloc[-4]

    if last["close"] > previous["close"]:
        return "BULLISH"

    if last["close"] < previous["close"]:
        return "BEARISH"

    return "NEUTRAL"


def detect_bos(df, lookback=8):

    if df is None or len(df) < lookback + 3:
        return "NONE"

    recent = df.iloc[
        -lookback - 1:-1
    ]

    last = df.iloc[-1]

    previous_high = recent["high"].max()
    previous_low = recent["low"].min()

    if last["close"] > previous_high:
        return "BULLISH"

    if last["close"] < previous_low:
        return "BEARISH"

    return "NONE"


def detect_liquidity(df, lookback=12):

    if df is None or len(df) < lookback + 3:
        return "NONE"

    recent = df.iloc[
        -lookback - 1:-1
    ]

    last = df.iloc[-1]

    high = recent["high"].max()
    low = recent["low"].min()

    # Buy-side liquidity sweep.
    if (
        last["high"] > high
        and last["close"] < high
    ):
        return "BSL SWEEP"

    # Sell-side liquidity sweep.
    if (
        last["low"] < low
        and last["close"] > low
    ):
        return "SSL SWEEP"

    return "NONE"


def detect_fvg(df):

    if df is None or len(df) < 3:
        return "NONE"

    a = df.iloc[-3]
    c = df.iloc[-1]

    if c["low"] > a["high"]:
        return "BULLISH FVG"

    if c["high"] < a["low"]:
        return "BEARISH FVG"

    return "NONE"


def detect_displacement(df):

    if df is None or len(df) < 10:
        return "NONE"

    recent = df.iloc[-9:-1]
    last = df.iloc[-1]

    body = abs(
        last["close"]
        - last["open"]
    )

    avg_body = (
        abs(
            recent["close"]
            - recent["open"]
        ).mean()
    )

    if (
        not np.isfinite(avg_body)
        or avg_body == 0
    ):
        return "NONE"

    if body >= avg_body * 1.8:

        if last["close"] > last["open"]:
            return "BULLISH"

        if last["close"] < last["open"]:
            return "BEARISH"

    return "NONE"


def detect_zone(df):

    if df is None or len(df) < 20:
        return "EQUILIBRIUM"

    recent = df.iloc[-20:]

    high = recent["high"].max()
    low = recent["low"].min()

    price = recent["close"].iloc[-1]

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


# =========================================================
# SIGNAL SCORING
# =========================================================

def score_signal(frames):

    h4 = frames["H4"]
    h1 = frames["H1"]
    m15 = frames["M15"]
    m5 = frames["M5"]

    trends = {
        "H4": trend_from_df(h4),
        "H1": trend_from_df(h1),
        "M15": trend_from_df(m15),
        "M5": trend_from_df(m5),
    }

    liquidity = detect_liquidity(m5)
    bos = detect_bos(m5)
    fvg = detect_fvg(m5)
    displacement = detect_displacement(m5)
    momentum = momentum_from_df(m5)
    zone = detect_zone(m5)

    rsi_value = float(
        m5["rsi"].iloc[-1]
    )

    buy = 0
    sell = 0

    weights = {
        "H4": 20,
        "H1": 15,
        "M15": 15,
        "M5": 10,
    }

    for timeframe, weight in weights.items():

        if trends[timeframe] == "BULLISH":
            buy += weight

        elif trends[timeframe] == "BEARISH":
            sell += weight

    if liquidity == "SSL SWEEP":
        buy += 10

    elif liquidity == "BSL SWEEP":
        sell += 10

    if bos == "BULLISH":
        buy += 15

    elif bos == "BEARISH":
        sell += 15

    if fvg == "BULLISH FVG":
        buy += 5

    elif fvg == "BEARISH FVG":
        sell += 5

    if displacement == "BULLISH":
        buy += 10

    elif displacement == "BEARISH":
        sell += 10

    if momentum == "BULLISH":
        buy += 5

    elif momentum == "BEARISH":
        sell += 5

    if rsi_value < 35:
        buy += 5

    elif rsi_value > 65:
        sell += 5

    # Small context bonus.
    if zone == "DISCOUNT":
        buy += 3

    elif zone == "PREMIUM":
        sell += 3

    if buy >= sell:
        dominant = "BUY"
        dominant_score = buy
    else:
        dominant = "SELL"
        dominant_score = sell

    if dominant_score >= MIN_SCORE:
        signal = dominant

    elif dominant_score >= EARLY_SCORE:
        signal = f"EARLY {dominant}"

    else:
        signal = "WAIT"

    return {
        "signal": signal,
        "buy_score": int(buy),
        "sell_score": int(sell),
        "dominant": dominant,
        "dominant_score": int(
            dominant_score
        ),
        "trends": trends,
        "liquidity": liquidity,
        "bos": bos,
        "fvg": fvg,
        "displacement": displacement,
        "momentum": momentum,
        "zone": zone,
        "rsi": rsi_value,
    }


# =========================================================
# SIGNAL MESSAGE
# =========================================================

def build_signal_message(
    result,
    price
):

    signal = result["signal"]

    if signal == "BUY":
        icon = "🟢"

    elif signal == "SELL":
        icon = "🔴"

    elif signal.startswith("EARLY"):
        icon = "🟡"

    else:
        icon = "⚪"

    t = result["trends"]

    return (
        f"🥇 {APP_NAME}\n\n"
        f"{icon} SIGNAL: {signal}\n\n"
        f"💰 XAUUSD: {fmt_price(price)}\n\n"

        f"📊 H4: {t['H4']}\n"
        f"📊 H1: {t['H1']}\n"
        f"📊 M15: {t['M15']}\n"
        f"📊 M5: {t['M5']}\n\n"

        f"💧 M5 Liquidity: "
        f"{result['liquidity']}\n"

        f"🔨 M5 BOS: "
        f"{result['bos']}\n"

        f"🧩 M5 FVG: "
        f"{result['fvg']}\n"

        f"💥 M5 Displacement: "
        f"{result['displacement']}\n"

        f"📈 Momentum: "
        f"{result['momentum']}\n"

        f"📐 Zone: "
        f"{result['zone']}\n"

        f"📉 RSI: "
        f"{result['rsi']:.1f}\n\n"

        f"🟢 BUY SCORE: "
        f"{result['buy_score']}\n"

        f"🔴 SELL SCORE: "
        f"{result['sell_score']}\n\n"

        "📡 Source: XAUS\n"
        f"🕯 Closed candles: "
        f"{'ON' if CLOSED_CANDLES else 'OFF'}\n"

        f"🛡 Auto trading: "
        f"{'ON' if AUTO_TRADING else 'OFF'}"
    )


# =========================================================
# VIRTUAL TRADES
# =========================================================

def open_virtual_trade(
    side,
    entry
):

    global open_trade

    with state_lock:

        if open_trade is not None:
            return False

        if side == "BUY":

            sl = entry - SL_DISTANCE
            tp = entry + TP_DISTANCE

        else:

            sl = entry + SL_DISTANCE
            tp = entry - TP_DISTANCE

        open_trade = {
            "side": side,
            "entry": float(entry),
            "sl": float(sl),
            "tp": float(tp),
            "initial_sl": float(sl),
            "be": False,
            "opened_at": now_text()
        }

        stats["total"] += 1
        stats["open"] = 1

        save_stats()

    return True


def close_virtual_trade(
    result,
    exit_price,
    reason
):

    global open_trade

    with state_lock:

        if open_trade is None:
            return

        trade = dict(
            open_trade
        )

        record = {
            "side": trade["side"],
            "entry": trade["entry"],
            "exit": float(exit_price),
            "sl": trade["sl"],
            "tp": trade["tp"],
            "result": result,
            "reason": reason,
            "opened_at": trade["opened_at"],
            "closed_at": now_text()
        }

        stats["closed_trades"].append(
            record
        )

        if result == "WIN":
            stats["win"] += 1

        elif result == "LOSS":
            stats["loss"] += 1

        elif result == "BE":
            stats["be"] += 1

        stats["open"] = 0

        open_trade = None

        save_stats()

    if result == "WIN":
        icon = "🟢"

    elif result == "LOSS":
        icon = "🔴"

    else:
        icon = "🟡"

    telegram_send(
        "📊 VIRTUAL TRADE CLOSED\n\n"
        f"{icon} {result}\n"
        f"Side: {trade['side']}\n"
        f"Entry: {fmt_price(trade['entry'])}\n"
        f"Exit: {fmt_price(exit_price)}\n"
        f"Reason: {reason}"
    )


def update_virtual_trade(price):

    global open_trade

    with state_lock:

        trade = (
            dict(open_trade)
            if open_trade
            else None
        )

    if trade is None:
        return

    side = trade["side"]

    entry = trade["entry"]
    sl = trade["sl"]
    tp = trade["tp"]

    # =====================================================
    # BREAK EVEN
    # =====================================================

    if (
        side == "BUY"
        and not trade["be"]
        and price >= entry + BE_TRIGGER
    ):

        with state_lock:

            if open_trade is not None:

                open_trade["be"] = True
                open_trade["sl"] = entry

                save_stats()

        sl = entry

        telegram_send(
            "🟡 BREAK-EVEN\n"
            f"BUY entry: "
            f"{fmt_price(entry)}\n"
            f"SL moved to: "
            f"{fmt_price(entry)}"
        )

    elif (
        side == "SELL"
        and not trade["be"]
        and price <= entry - BE_TRIGGER
    ):

        with state_lock:

            if open_trade is not None:

                open_trade["be"] = True
                open_trade["sl"] = entry

                save_stats()

        sl = entry

        telegram_send(
            "🟡 BREAK-EVEN\n"
            f"SELL entry: "
            f"{fmt_price(entry)}\n"
            f"SL moved to: "
            f"{fmt_price(entry)}"
        )

    # =====================================================
    # BUY
    # =====================================================

    if side == "BUY":

        if price >= tp:

            close_virtual_trade(
                "WIN",
                price,
                "TP"
            )

        elif price <= sl:

            close_virtual_trade(
                "BE"
                if trade["be"]
                else "LOSS",
                price,
                "BE"
                if trade["be"]
                else "SL"
            )

    # =====================================================
    # SELL
    # =====================================================

    elif side == "SELL":

        if price <= tp:

            close_virtual_trade(
                "WIN",
                price,
                "TP"
            )

        elif price >= sl:

            close_virtual_trade(
                "BE"
                if trade["be"]
                else "LOSS",
                price,
                "BE"
                if trade["be"]
                else "SL"
            )


# =========================================================
# ANALYSIS
# =========================================================

def get_frames():

    raw = fetch_intraday()

    if raw is None or raw.empty:
        return None

    frames = {}

    timeframes = (
        ("M5", "5min"),
        ("M15", "15min"),
        ("H1", "1h"),
        ("H4", "4h")
    )

    for name, timeframe in timeframes:

        frame = resample_ohlc(
            raw,
            timeframe
        )

        if len(frame) < 10:
            return None

        frame = add_indicators(
            frame
        )

        frames[name] = frame

    return frames


def run_analysis():

    price = fetch_spot()

    if price is None:
        return None, None

    frames = get_frames()

    if frames is None:
        return price, None

    result = score_signal(
        frames
    )

    return price, result


# =========================================================
# AUTOMATIC ENGINE
# =========================================================

def engine_loop():

    global last_signal_time
    global last_signal_side

    print(
        f"[{now_text()}] "
        f"{APP_NAME} engine started"
    )

    while True:

        try:

            price = fetch_spot()

            if price is not None:
                update_virtual_trade(
                    price
                )

            price, result = run_analysis()

            if (
                price is None
                or result is None
            ):
                time.sleep(
                    POLL_SECONDS
                )
                continue

            signal = result["signal"]

            if signal in (
                "BUY",
                "SELL"
            ):

                side = signal

                current_time = time.time()

                cooldown_ok = (
                    current_time
                    - last_signal_time
                    >= COOLDOWN_MIN * 60
                )

                no_open_trade = (
                    open_trade is None
                )

                if (
                    cooldown_ok
                    and no_open_trade
                ):

                    message = (
                        build_signal_message(
                            result,
                            price
                        )
                    )

                    telegram_sent = (
                        telegram_send(
                            message
                        )
                    )

                    opened = (
                        open_virtual_trade(
                            side,
                            price
                        )
                    )

                    if opened:

                        last_signal_time = (
                            current_time
                        )

                        last_signal_side = (
                            side
                        )

                        if side == "BUY":

                            sl_value = (
                                price
                                - SL_DISTANCE
                            )

                            tp_value = (
                                price
                                + TP_DISTANCE
                            )

                        else:

                            sl_value = (
                                price
                                + SL_DISTANCE
                            )

                            tp_value = (
                                price
                                - TP_DISTANCE
                            )

                        print(
                            f"[{now_text()}] "
                            f"AUTO {side} "
                            f"@ {fmt_price(price)} "
                            f"telegram="
                            f"{telegram_sent}"
                        )

                        telegram_send(
                            "🧪 VIRTUAL TRADE OPENED\n\n"
                            f"Side: {side}\n"
                            f"Entry: {fmt_price(price)}\n"
                            f"SL: {fmt_price(sl_value)}\n"
                            f"TP: {fmt_price(tp_value)}\n"
                            "Real trading: OFF"
                        )

            time.sleep(
                POLL_SECONDS
            )

        except Exception as exc:

            print(
                f"[ENGINE] error: {exc}"
            )

            time.sleep(
                POLL_SECONDS
            )


# =========================================================
# TELEGRAM COMMANDS
# =========================================================

def stats_text():

    total = stats["total"]

    closed = (
        stats["win"]
        + stats["loss"]
        + stats["be"]
    )

    if closed > 0:

        win_rate = (
            stats["win"]
            / closed
            * 100
        )

    else:

        win_rate = 0.0

    return (
        f"📊 {APP_NAME} — STATISTICS\n\n"

        f"📌 TOTAL TRADES: "
        f"{total}\n\n"

        f"🟢 WIN: "
        f"{stats['win']}\n"

        f"🔴 LOSS: "
        f"{stats['loss']}\n"

        f"🟡 BE: "
        f"{stats['be']}\n"

        f"🟠 OPEN: "
        f"{stats['open']}\n\n"

        f"🎯 CLOSED: "
        f"{closed}\n"

        f"📈 WIN RATE: "
        f"{win_rate:.1f}%\n\n"

        f"💰 Risk: "
        f"{RISK_PERCENT:.1f}%\n"

        f"🛡 Auto trading: "
        f"{'ON' if AUTO_TRADING else 'OFF'}"
    )


def status_text():

    price = fetch_spot()

    if price is None:

        price_line = "N/A"
        xaus_state = "ERROR"

    else:

        price_line = fmt_price(price)
        xaus_state = "READY"

    with state_lock:

        trade = (
            dict(open_trade)
            if open_trade
            else None
        )

    if trade:

        trade_line = (
            f"🟠 OPEN "
            f"{trade['side']} "
            f"@ {fmt_price(trade['entry'])}"
        )

    else:

        trade_line = (
            "⚪ No virtual trade"
        )

    return (
        f"🥇 {APP_NAME}\n\n"

        "🟢 Engine: READY\n"

        f"🟢 Telegram: "
        f"{'READY' if BOT_TOKEN else 'NOT CONFIGURED'}\n"

        f"🟢 XAUS API: "
        f"{xaus_state}\n\n"

        f"💰 XAUUSD: "
        f"{price_line}\n"

        f"{trade_line}\n\n"

        "🛡 AUTO TRADING: OFF\n"

        f"📉 Risk: "
        f"{RISK_PERCENT:.1f}%\n"

        f"🕯 Closed candles: "
        f"{'ON' if CLOSED_CANDLES else 'OFF'}"
    )


def help_text():

    return (
        f"🥇 {APP_NAME}\n\n"

        "Команды:\n"

        "/start — запуск\n"
        "/status — состояние бота\n"
        "/signal — текущий анализ\n"
        "/test — тест подключения\n"
        "/stats — статистика виртуальных сделок\n"
        "/help — помощь\n\n"

        "🛡 Реальная торговля отключена.\n"
        "🧪 Сигналы тестируются виртуальными сделками."
    )


def process_command(
    chat_id,
    command
):

    command = (
        command
        .split()[0]
        .lower()
    )

    if command == "/start":

        telegram_reply(
            chat_id,
            help_text()
        )

    elif command == "/help":

        telegram_reply(
            chat_id,
            help_text()
        )

    elif command == "/status":

        telegram_reply(
            chat_id,
            status_text()
        )

    elif command == "/stats":

        telegram_reply(
            chat_id,
            stats_text()
        )

    elif command == "/test":

        price = fetch_spot(
            force=True
        )

        if price is None:

            telegram_reply(
                chat_id,
                f"❌ {APP_NAME} TEST\n\n"
                "XAUS API: ERROR"
            )

        else:

            telegram_reply(
                chat_id,
                f"✅ {APP_NAME} TEST\n\n"
                "🟢 Engine: READY\n"
                "🟢 Telegram: READY\n"
                "🟢 XAUS API: READY\n"
                f"💰 XAUUSD: "
                f"{fmt_price(price)}\n"
                "🛡 AUTO TRADING: OFF"
            )

    elif command == "/signal":

        price, result = (
            run_analysis()
        )

        if price is None:

            telegram_reply(
                chat_id,
                f"❌ {APP_NAME}\n\n"
                "XAUS price unavailable."
            )

            return

        if result is None:

            telegram_reply(
                chat_id,
                f"⚠️ {APP_NAME}\n\n"
                "Недостаточно intraday "
                "данных для анализа."
            )

            return

        telegram_reply(
            chat_id,
            build_signal_message(
                result,
                price
            )
        )


# =========================================================
# FLASK
# =========================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():

    return jsonify({
        "app": APP_NAME,
        "status": "running",
        "auto_trading": AUTO_TRADING,
        "source": "XAUS"
    })


@app.route(
    "/health",
    methods=["GET"]
)
def health():

    return jsonify({
        "status": "ok",
        "app": APP_NAME
    })


@app.route(
    "/telegram",
    methods=["POST"]
)
def telegram_webhook():

    try:

        update = (
            request
            .get_json(
                silent=True
            )
            or {}
        )

        message = (
            update.get("message")
            or {}
        )

        chat = (
            message.get("chat")
            or {}
        )

        chat_id = chat.get(
            "id"
        )

        text_message = (
            message.get("text", "")
            .strip()
        )

        if (
            chat_id
            and text_message.startswith("/")
        ):

            process_command(
                chat_id,
                text_message
            )

        return jsonify({
            "ok": True
        })

    except Exception as exc:

        print(
            f"[TELEGRAM] "
            f"webhook error: {exc}"
        )

        return jsonify({
            "ok": True
        })


@app.route(
    "/test",
    methods=["GET"]
)
def web_test():

    price = fetch_spot(
        force=True
    )

    return jsonify({
        "app": APP_NAME,
        "engine": "READY",
        "telegram": (
            "READY"
            if BOT_TOKEN
            else "NOT CONFIGURED"
        ),
        "xaus_api": (
            "READY"
            if price is not None
            else "ERROR"
        ),
        "xauusd": price,
        "auto_trading": AUTO_TRADING
    })


# =========================================================
# STARTUP
# =========================================================

def start_engine():

    thread = threading.Thread(
        target=engine_loop,
        name="gold-smart-engine",
        daemon=True
    )

    thread.start()


start_engine()


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
        debug=False
    )
