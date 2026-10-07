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
# GOLD SMART V6.0.2
# XAU/USD SMART SMC SIGNAL ENGINE
#
# H4 -> H1 -> M15 -> M5
# DATA SOURCE: XAUS
# TELEGRAM: WEBHOOK
# AUTO TRADING: OFF
# ============================================================

APP_NAME = "GOLD SMART V6.0.2"

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

STATS_FILE = os.getenv("STATS_FILE", "gold_stats.json")
TRADE_FILE = os.getenv("TRADE_FILE", "gold_trade.json")

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
    return utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_float(value, default=None):
    try:
        if value is None or isinstance(value, bool):
            return default

        if isinstance(value, (int, float, np.integer, np.floating)):
            value = float(value)
        else:
            value = str(value).replace(",", "").strip()

        result = float(value)

        if not np.isfinite(result):
            return default

        return result
    except Exception:
        return default


def atomic_json_save(filename, data):
    directory = os.path.dirname(filename)
    if directory:
        os.makedirs(directory, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(
        prefix=".goldsmart_",
        suffix=".tmp",
        dir=directory or "."
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )

        os.replace(temp_path, filename)
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

        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ============================================================
# TELEGRAM
# ============================================================

def telegram_url(method):
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"


def send_telegram(text_message, chat_id=None):
    if not BOT_TOKEN:
        print("GOLD SMART: BOT_TOKEN missing")
        return False

    target_chat = str(chat_id or TELEGRAM_CHAT_ID).strip()

    if not target_chat:
        print("GOLD SMART: TELEGRAM_CHAT_ID missing")
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
            "GOLD SMART: Telegram sendMessage "
            f"HTTP {response.status_code}"
        )

        if response.status_code != 200:
            print(response.text[:1000])
            return False

        return bool(response.json().get("ok", True))

    except Exception as e:
        print("GOLD SMART: Telegram send error:", repr(e))
        return False


def configure_webhook():
    if not BOT_TOKEN:
        print("GOLD SMART: webhook not configured - BOT_TOKEN missing")
        return False

    if not RENDER_EXTERNAL_URL:
        print("GOLD SMART: webhook not configured - URL missing")
        return False

    webhook_url = RENDER_EXTERNAL_URL + "/telegram"

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
            f"GOLD SMART: setWebhook HTTP "
            f"{response.status_code}: {response.text[:1000]}"
        )

        if response.status_code != 200:
            return False

        try:
            info = requests.get(
                telegram_url("getWebhookInfo"),
                timeout=15
            )
            print(
                f"GOLD SMART: getWebhookInfo HTTP "
                f"{info.status_code}: {info.text[:1500]}"
            )
        except Exception as e:
            print("GOLD SMART: getWebhookInfo error:", repr(e))

        return True

    except Exception as e:
        print("GOLD SMART: webhook configuration error:", repr(e))
        return False


# ============================================================
# XAUS DATA
# ============================================================

def recursive_find_price(obj):
    if obj is None:
        return None

    if isinstance(obj, (int, float)):
        return safe_float(obj)

    if isinstance(obj, list):
        for item in obj:
            value = recursive_find_price(item)
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
            "rate"
        ]

        for key in preferred:
            if key in obj:
                value = safe_float(obj.get(key))
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
                value = recursive_find_price(obj[key])
                if value is not None:
                    return value

    return None


def fetch_spot():
    global last_price

    response = requests.get(
        XAUS_SPOT_URL,
        params={"symbol": "xau"},
        timeout=20
    )
    response.raise_for_status()

    data = response.json()
    price = recursive_find_price(data)

    if price is None:
        raise ValueError("XAUS spot price not found")

    last_price = price
    return price


def extract_timestamp(item):
    if not isinstance(item, dict):
        return None

    for key in [
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

        if isinstance(value, (int, float)):
            value = float(value)
            if value > 100000000000:
                value /= 1000

            try:
                return pd.to_datetime(
                    value,
                    unit="s",
                    utc=True
                )
            except Exception:
                pass

        try:
            return pd.to_datetime(value, utc=True)
        except Exception:
            pass

    return None


def extract_intraday_items(data):
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in [
            "data",
            "prices",
            "intraday",
            "history",
            "candles",
            "results"
        ]:
            value = data.get(key)
            if isinstance(value, list):
                return value

        if isinstance(data.get("xau"), list):
            return data["xau"]

        if isinstance(data.get("xau"), dict):
            xau = data["xau"]

            for key in [
                "data",
                "prices",
                "intraday",
                "history",
                "candles",
                "results"
            ]:
                value = xau.get(key)
                if isinstance(value, list):
                    return value

    return []


def extract_candle_price(item):
    if not isinstance(item, dict):
        return None

    for key in [
        "price",
        "close",
        "spot_usd_oz",
        "value",
        "last"
    ]:
        if key in item:
            value = safe_float(item[key])
            if value is not None:
                return value

    return None


def fetch_intraday():
    now = int(time.time())

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
    items = extract_intraday_items(data)

    rows = []

    for item in items:
        timestamp = extract_timestamp(item)
        price = extract_candle_price(item)

        if timestamp is None or price is None:
            continue

        rows.append({
            "time": timestamp,
            "price": price
        })

    if not rows:
        raise ValueError("XAUS intraday returned no usable data")

    df = pd.DataFrame(rows)

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
    df = df.sort_values("time")
    df = df.drop_duplicates(
        subset=["time"],
        keep="last"
    )

    df = df.set_index("time")
    df = df[~df.index.duplicated(keep="last")]

    return df


# ============================================================
# TIMEFRAMES
# ============================================================

def build_ohlc(price_df, timeframe):
    if price_df is None or price_df.empty:
        return pd.DataFrame()

    series = price_df["price"].astype(float)

    result = series.resample(timeframe).ohlc()
    result = result.dropna()

    if CLOSED_CANDLES and not result.empty:
        now = pd.Timestamp.now(tz="UTC")
        current_bucket = now.floor(timeframe)
        result = result[result.index < current_bucket]

    return result


def build_timeframes(intraday):
    return {
        "M5": build_ohlc(intraday, "5min"),
        "M15": build_ohlc(intraday, "15min"),
        "H1": build_ohlc(intraday, "1h"),
        "H4": build_ohlc(intraday, "4h")
    }


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

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    result = 100 - (100 / (1 + rs))

    return result.fillna(50)


def atr(df, period=14):
    high = df["high"]
    low = df["low"]
    close = df["close"]

    previous_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)

    return tr.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()


def trend_analysis(df):
    if df is None or len(df) < 5:
        return {
            "side": "NEUTRAL",
            "score": 50,
            "rsi": 50,
            "ema20": None,
            "ema50": None,
            "ema200": None
        }

    close = df["close"]

    e20_series = ema(close, 20)
    e50_series = ema(close, 50)
    e200_series = ema(close, 200)

    current = float(close.iloc[-1])
    e20 = float(e20_series.iloc[-1])
    e50 = float(e50_series.iloc[-1])
    e200 = float(e200_series.iloc[-1])
    current_rsi = float(rsi(close).iloc[-1])

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
        old = float(close.iloc[-6])

        if current > old:
            bullish += 15
        elif current < old:
            bearish += 15

    if bullish > bearish:
        side = "BULLISH"
        score = min(100, bullish)
    elif bearish > bullish:
        side = "BEARISH"
        score = min(100, bearish)
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
    if df is None or len(df) < 5:
        return "NONE"

    previous = df.iloc[:-1].tail(4)
    current = df.iloc[-1]

    previous_high = previous["high"].max()
    previous_low = previous["low"].min()

    if current["close"] > previous_high:
        return "BULLISH"

    if current["close"] < previous_low:
        return "BEARISH"

    return "NONE"


def detect_liquidity_sweep(df):
    if df is None or len(df) < 6:
        return "NONE"

    previous = df.iloc[:-1].tail(5)
    current = df.iloc[-1]

    prior_high = previous["high"].max()
    prior_low = previous["low"].min()

    if (
        current["low"] < prior_low
        and current["close"] > prior_low
    ):
        return "BULLISH SWEEP"

    if (
        current["high"] > prior_high
        and current["close"] < prior_high
    ):
        return "BEARISH SWEEP"

    return "NONE"


def detect_fvg(df):
    if df is None or len(df) < 4:
        return "NONE"

    a = df.iloc[-3]
    c = df.iloc[-1]

    if c["low"] > a["high"]:
        return "BULLISH"

    if c["high"] < a["low"]:
        return "BEARISH"

    return "NONE"


def detect_displacement(df):
    if df is None or len(df) < 15:
        return "NONE"

    current = df.iloc[-1]

    current_body = abs(
        float(current["close"]) -
        float(current["open"])
    )

    current_atr = float(atr(df, 14).iloc[-1])

    if current_atr <= 0:
        return "NONE"

    ratio = current_body / current_atr

    if ratio < 0.8:
        return "NONE"

    if current["close"] > current["open"]:
        return "BULLISH"

    if current["close"] < current["open"]:
        return "BEARISH"

    return "NONE"


def detect_momentum(df):
    if df is None or len(df) < 8:
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
    if df is None or len(df) < 20:
        return "NONE"

    atr_values = atr(df, 14)

    recent = float(atr_values.iloc[-1])
    old = float(atr_values.iloc[-10])

    if old <= 0:
        return "NONE"

    if recent < old * 0.75:
        return "COMPRESSION"

    return "NONE"


def premium_discount(df):
    if df is None or len(df) < 10:
        return "EQUILIBRIUM"

    window = df.tail(20)

    high = float(window["high"].max())
    low = float(window["low"].min())
    close = float(df["close"].iloc[-1])

    midpoint = (high + low) / 2

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
    displacement = detect_displacement(m5)
    momentum = detect_momentum(m5)
    compression = detect_compression(m5)
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

    if score >= MIN_SCORE:
        signal_type = "FULL"
    elif score >= EARLY_SCORE:
        signal_type = "EARLY"
    else:
        signal_type = "WAIT"

    return {
        "side": side,
        "score": int(score),
        "signal_type": signal_type,
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
        "htf_agreement": bullish_htf or bearish_htf
    }


# ============================================================
# DISPLAY LOT
# ============================================================

def calculate_lot():
    # Только display lot. Реальные ордера НЕ отправляются.
    risk_money = DEFAULT_DEPOSIT * RISK_PERCENT / 100.0

    if risk_money <= 0:
        return 0.01

    return 0.02


# ============================================================
# SIGNAL MESSAGE
# ============================================================

def build_signal_message(price, analysis, signal_type=None):
    side = analysis["side"]
    score = analysis["score"]

    if signal_type is None:
        signal_type = analysis["signal_type"]

    if side == "BUY":
        sl = price - SL_DISTANCE
        tp1 = price + TP1_DISTANCE
        tp2 = price + TP2_DISTANCE
        emoji = "🟢"

    elif side == "SELL":
        sl = price + SL_DISTANCE
        tp1 = price - TP1_DISTANCE
        tp2 = price - TP2_DISTANCE
        emoji = "🔴"

    else:
        return (
            f"🥇 {APP_NAME}\n\n"
            f"⚪ SIGNAL: WAIT\n\n"
            f"💰 XAUUSD: {price:.2f}\n\n"
            f"📊 H4: {analysis['h4']['side']} "
            f"({analysis['h4']['score']}/100)\n"
            f"📊 H1: {analysis['h1']['side']} "
            f"({analysis['h1']['score']}/100)\n"
            f"📊 M15: {analysis['m15']['side']} "
            f"({analysis['m15']['score']}/100)\n"
            f"📊 M5: {analysis['m5']['side']} "
            f"({analysis['m5']['score']}/100)\n\n"
            f"💧 Liquidity: {analysis['liquidity']}\n"
            f"🔨 BOS: {analysis['bos']}\n"
            f"🧩 FVG: {analysis['fvg']}\n"
            f"🚀 Displacement: {analysis['displacement']}\n"
            f"⚡ Momentum: {analysis['momentum']}\n"
            f"📦 Zone: {analysis['premium_discount']}\n\n"
            f"📈 Score: {score}/100\n\n"
            f"🤖 AUTO TRADING: OFF\n"
            f"✋ Manual signals only"
        )

    confidence = min(99, max(50, score))
    lot = calculate_lot()

    return (
        f"🥇 {APP_NAME}\n\n"
        f"{emoji} SIGNAL: {signal_type} {side}\n\n"
        f"💰 XAUUSD: {price:.2f}\n\n"
        f"📊 H4: {analysis['h4']['side']} "
        f"({analysis['h4']['score']}/100)\n"
        f"📊 H1: {analysis['h1']['side']} "
        f"({analysis['h1']['score']}/100)\n"
        f"📊 M15: {analysis['m15']['side']} "
        f"({analysis['m15']['score']}/100)\n"
        f"📊 M5: {analysis['m5']['side']} "
        f"({analysis['m5']['score']}/100)\n\n"
        f"💧 Liquidity: {analysis['liquidity']}\n"
        f"🔨 BOS: {analysis['bos']}\n"
        f"🧩 FVG: {analysis['fvg']}\n"
        f"🚀 Displacement: {analysis['displacement']}\n"
        f"⚡ Momentum: {analysis['momentum']}\n"
        f"📦 Zone: {analysis['premium_discount']}\n\n"
        f"🎯 ENTRY: {price:.2f}\n"
        f"🛑 SL: {sl:.2f}\n"
        f"🎯 TP1: {tp1:.2f}\n"
        f"🎯 TP2: {tp2:.2f}\n\n"
        f"📐 RR: 1:3\n"
        f"📦 Lot: {lot:.2f}\n"
        f"🔥 Confidence: {confidence}%\n\n"
        f"🤖 AUTO TRADING: OFF\n"
        f"✋ Manual signals only"
    )


# ============================================================
# VIRTUAL TRADE / STATS
# ============================================================

def save_trade():
    with STATE_LOCK:
        atomic_json_save(TRADE_FILE, active_trade)


def load_trade():
    global active_trade

    active_trade = load_json(
        TRADE_FILE,
        None
    )

    if not isinstance(active_trade, dict):
        active_trade = None


def load_stats():
    default = {
        "total": 0,
        "wins": 0,
        "losses": 0,
        "breakeven": 0,
        "points": 0.0
    }

    stats = load_json(STATS_FILE, default)

    if not isinstance(stats, dict):
        return default

    for key in default:
        stats.setdefault(key, default[key])

    return stats


def save_stats(stats):
    atomic_json_save(STATS_FILE, stats)


def close_virtual_trade(result, points):
    global active_trade

    stats = load_stats()

    stats["total"] = int(stats.get("total", 0)) + 1

    if result == "WIN":
        stats["wins"] = int(stats.get("wins", 0)) + 1
    elif result == "LOSS":
        stats["losses"] = int(stats.get("losses", 0)) + 1
    else:
        stats["breakeven"] = int(stats.get("breakeven", 0)) + 1

    stats["points"] = round(
        float(stats.get("points", 0.0)) + float(points),
        2
    )

    save_stats(stats)

    with STATE_LOCK:
        active_trade = None
        atomic_json_save(TRADE_FILE, None)


def open_virtual_trade(side, entry, analysis):
    global active_trade

    if side not in ("BUY", "SELL"):
        return False

    with STATE_LOCK:
        if active_trade:
            return False

        if side == "BUY":
            sl = entry - SL_DISTANCE
            tp1 = entry + TP1_DISTANCE
            tp2 = entry + TP2_DISTANCE
        else:
            sl = entry + SL_DISTANCE
            tp1 = entry - TP1_DISTANCE
            tp2 = entry - TP2_DISTANCE

        active_trade = {
            "side": side,
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "original_sl": round(sl, 2),
            "tp1": round(tp1, 2),
            "tp2": round(tp2, 2),
            "score": int(analysis["score"]),
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


def manage_virtual_trade(price):
    global active_trade

    if not active_trade:
        return

    try:
        side = active_trade["side"]
        entry = float(active_trade["entry"])
        sl = float(active_trade["sl"])
        tp1 = float(active_trade["tp1"])
        tp2 = float(active_trade["tp2"])
        be = bool(active_trade.get("be", False))
        tp1_hit = bool(active_trade.get("tp1_hit", False))

        if side == "BUY":
            favorable_move = price - entry

            if not be and favorable_move >= BE_TRIGGER:
                active_trade["sl"] = entry
                active_trade["be"] = True
                save_trade()

                send_telegram(
                    f"🛡 {APP_NAME}\n\n"
                    f"🔒 BREAK EVEN\n\n"
                    f"🟢 BUY\n\n"
                    f"💰 Entry: {entry:.2f}\n"
                    f"📍 Current: {price:.2f}\n"
                    f"➡️ SL moved to BE: {entry:.2f}\n\n"
                    f"AUTO TRADING: OFF"
                )

                sl = entry

            if not tp1_hit and price >= tp1:
                active_trade["tp1_hit"] = True
                save_trade()

                send_telegram(
                    f"🎯 {APP_NAME}\n\n"
                    f"✅ TP1 HIT\n\n"
                    f"🟢 BUY\n\n"
                    f"Entry: {entry:.2f}\n"
                    f"TP1: {tp1:.2f}\n"
                    f"Result: +{tp1 - entry:.2f}\n\n"
                    f"🛡️ Virtual trade remains active to TP2\n"
                    f"AUTO TRADING: OFF"
                )

            if price <= sl:
                points = price - entry
                result = "BREAKEVEN" if active_trade.get("be") else "LOSS"

                close_virtual_trade(result, points)

                send_telegram(
                    f"📊 {APP_NAME}\n\n"
                    f"{'🟡 BREAK EVEN' if result == 'BREAKEVEN' else '❌ SL HIT'}\n\n"
                    f"🟢 BUY\n"
                    f"Entry: {entry:.2f}\n"
                    f"Close: {price:.2f}\n"
                    f"Result: {points:+.2f}\n\n"
                    f"AUTO TRADING: OFF"
                )
                return

            if price >= tp2:
                points = tp2 - entry
                close_virtual_trade("WIN", points)

                send_telegram(
                    f"🏆 {APP_NAME}\n\n"
                    f"✅ TP2 HIT\n\n"
                    f"🟢 BUY\n\n"
                    f"Entry: {entry:.2f}\n"
                    f"TP2: {tp2:.2f}\n"
                    f"Result: +{points:.2f}\n\n"
                    f"AUTO TRADING: OFF"
                )
                return

        elif side == "SELL":
            favorable_move = entry - price

            if not be and favorable_move >= BE_TRIGGER:
                active_trade["sl"] = entry
                active_trade["be"] = True
                save_trade()

                send_telegram(
                    f"🛡 {APP_NAME}\n\n"
                    f"🔒 BREAK EVEN\n\n"
                    f"🔴 SELL\n\n"
                    f"💰 Entry: {entry:.2f}\n"
                    f"📍 Current: {price:.2f}\n"
                    f"➡️ SL moved to BE: {entry:.2f}\n\n"
                    f"AUTO TRADING: OFF"
                )

                sl = entry

            if not tp1_hit and price <= tp1:
                active_trade["tp1_hit"] = True
                save_trade()

                send_telegram(
                    f"🎯 {APP_NAME}\n\n"
                    f"✅ TP1 HIT\n\n"
                    f"🔴 SELL\n\n"
                    f"Entry: {entry:.2f}\n"
                    f"TP1: {tp1:.2f}\n"
                    f"Result: +{entry - tp1:.2f}\n\n"
                    f"🛡️ Virtual trade remains active to TP2\n"
                    f"AUTO TRADING: OFF"
                )

            if price >= sl:
                points = entry - price
                result = "BREAKEVEN" if active_trade.get("be") else "LOSS"

                close_virtual_trade(result, points)

                send_telegram(
                    f"📊 {APP_NAME}\n\n"
                    f"{'🟡 BREAK EVEN' if result == 'BREAKEVEN' else '❌ SL HIT'}\n\n"
                    f"🔴 SELL\n"
                    f"Entry: {entry:.2f}\n"
                    f"Close: {price:.2f}\n"
                    f"Result: {points:+.2f}\n\n"
                    f"AUTO TRADING: OFF"
                )
                return

            if price <= tp2:
                points = entry - tp2
                close_virtual_trade("WIN", points)

                send_telegram(
                    f"🏆 {APP_NAME}\n\n"
                    f"✅ TP2 HIT\n\n"
                    f"🔴 SELL\n\n"
                    f"Entry: {entry:.2f}\n"
                    f"TP2: {tp2:.2f}\n"
                    f"Result: +{points:.2f}\n\n"
                    f"AUTO TRADING: OFF"
                )
                return

    except Exception as e:
        print("GOLD SMART: virtual trade error:", repr(e))


# ============================================================
# MARKET CYCLE
# ============================================================

def market_cycle(send_signal=True, force=False):
    global last_cycle
    global last_error
    global last_data_counts
    global last_signal_time

    try:
        print("GOLD SMART: cycle started")

        price = fetch_spot()
        print(f"GOLD SMART: spot={price:.2f}")

        intraday = fetch_intraday()
        print(f"GOLD SMART: intraday rows={len(intraday)}")

        frames = build_timeframes(intraday)

        counts = {
            name: len(df)
            for name, df in frames.items()
        }

        last_data_counts = counts
        print("GOLD SMART: timeframe counts:", counts)

        # XAUS provides up to 48h.
        # H4 therefore normally gives around 11-12 closed candles.
        minimums = {
            "M5": 30,
            "M15": 20,
            "H1": 7,
            "H4": 8
        }

        for name, minimum in minimums.items():
            if len(frames[name]) < minimum:
                message = (
                    f"GOLD SMART: insufficient "
                    f"{name} candles: "
                    f"{len(frames[name])}/{minimum}"
                )

                print(message)
                last_error = message
                return None

        analysis = analyze_market(frames)

        last_cycle = utc_string()
        last_error = None

        print(
            "GOLD SMART:",
            analysis["side"],
            analysis["score"],
            analysis["signal_type"]
        )

        # Manage existing virtual position first.
        manage_virtual_trade(price)

        is_real_signal = (
            analysis["side"] in ("BUY", "SELL")
            and analysis["score"] >= EARLY_SCORE
        )

        if not is_real_signal:
            return analysis

        now = time.time()
        cooldown_seconds = SIGNAL_COOLDOWN_MIN * 60

        if (
            not force
            and now - last_signal_time < cooldown_seconds
        ):
            print("GOLD SMART: signal cooldown")
            return analysis

        if active_trade:
            print("GOLD SMART: active virtual trade")
            return analysis

        if send_signal:
            message = build_signal_message(
                price,
                analysis
            )

            sent = send_telegram(message)

            if sent:
                last_signal_time = now

                opened = open_virtual_trade(
                    analysis["side"],
                    price,
                    analysis
                )

                if opened:
                    print("GOLD SMART: virtual trade opened")

        return analysis

    except Exception as e:
        last_error = f"{type(e).__name__}: {e}"

        print(
            "GOLD SMART: cycle ERROR:",
            last_error
        )

        traceback.print_exc()
        return None


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

def get_command(update):
    message = update.get("message") or {}

    if not message:
        return None, None, None

    chat = message.get("chat") or {}
    chat_id = chat.get("id")

    text_message = (
        message.get("text")
        or ""
    ).strip()

    if not text_message:
        return None, chat_id, None

    first = text_message.split()[0]
    command = first.split("@")[0].lower()

    return command, chat_id, text_message


def command_start(chat_id):
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
    active = "YES" if active_trade else "NO"

    counts = (
        ", ".join(
            f"{k}:{v}"
            for k, v in last_data_counts.items()
        )
        if last_data_counts
        else "нет данных"
    )

    return (
        f"🥇 {APP_NAME}\n\n"
        f"🟢 ENGINE: {'ON' if ENGINE_STARTED else 'OFF'}\n"
        f"🤖 AUTO TRADING: OFF\n"
        f"💰 XAUUSD: "
        f"{last_price:.2f}\n" if last_price is not None
        else
        f"🥇 {APP_NAME}\n\n"
        f"🟢 ENGINE: {'ON' if ENGINE_STARTED else 'OFF'}\n"
        f"🤖 AUTO TRADING: OFF\n"
        f"💰 XAUUSD: unavailable\n"
    ) + (
        f"📊 Candles: {counts}\n"
        f"🕐 Last cycle: {last_cycle or 'нет'}\n"
        f"⚠️ Error: {last_error or 'нет'}\n"
        f"📦 Virtual trade: {active}\n"
    )


def command_signal(chat_id):
    analysis = market_cycle(
        send_signal=False,
        force=True
    )

    if not analysis or last_price is None:
        return (
            f"⚠️ {APP_NAME}\n\n"
            f"Не удалось получить анализ XAU/USD.\n"
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
        f"✋ Manual signals only\n"
        f"🕐 {utc_string()}"
    )


def command_stats():
    stats = load_stats()

    total = int(stats.get("total", 0))
    wins = int(stats.get("wins", 0))
    losses = int(stats.get("losses", 0))
    breakeven = int(stats.get("breakeven", 0))
    points = float(stats.get("points", 0.0))

    closed = wins + losses + breakeven

    winrate = (
        wins / closed * 100
        if closed > 0
        else 0
    )

    return (
        f"📊 {APP_NAME} STATS\n\n"
        f"🔢 Total: {total}\n"
        f"🏆 Wins: {wins}\n"
        f"❌ Losses: {losses}\n"
        f"🟡 Break-even: {breakeven}\n"
        f"📈 Win rate: {winrate:.1f}%\n"
        f"💵 Points: {points:+.2f}\n\n"
        f"🤖 AUTO TRADING: OFF"
    )


def command_help():
    return (
        f"🥇 {APP_NAME}\n\n"
        f"/start — меню\n"
        f"/status — состояние engine/data\n"
        f"/signal — принудительный текущий анализ\n"
        f"/test — проверка Telegram\n"
        f"/stats — виртуальная статистика\n"
        f"/help — эта справка\n\n"
        f"⚠️ Бот не открывает реальные сделки.\n"
        f"🤖 AUTO TRADING: OFF"
    )


def handle_telegram_update(update):
    command, chat_id, text_message = get_command(update)

    if not command or chat_id is None:
        return

    # Безопасность: команды принимает только заданный chat ID,
    # если TELEGRAM_CHAT_ID установлен.
    if TELEGRAM_CHAT_ID:
        if str(chat_id) != str(TELEGRAM_CHAT_ID):
            print(
                "GOLD SMART: ignored Telegram chat:",
                chat_id
            )
            return

    try:
        if command == "/start":
            reply = command_start(chat_id)

        elif command == "/status":
            reply = command_status()

        elif command == "/signal":
            reply = command_signal(chat_id)

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
            "GOLD SMART: command error:",
            repr(e)
        )

        send_telegram(
            "⚠️ GOLD SMART command error.\n"
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
            "GOLD SMART: Telegram update received"
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
            "GOLD SMART: webhook error:",
            repr(e)
        )

        return jsonify({
            "ok": False
        }), 500


@app.get("/telegram")
def telegram_get():
    return jsonify({
        "ok": True,
        "message": "Telegram webhook endpoint"
    })


def hmac_compare(a, b):
    try:
        return __import__("hmac").compare_digest(
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
        f"{APP_NAME}: engine loop started"
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

        time.sleep(POLL_SECONDS)


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
            f"{APP_NAME}: engine started"
        )


# ============================================================
# KEEPALIVE
# ============================================================

def keepalive_loop():
    print("GOLD SMART: keepalive started")

    while True:
        try:
            if RENDER_EXTERNAL_URL:
                requests.get(
                    RENDER_EXTERNAL_URL + "/health",
                    timeout=15
                )
        except Exception as e:
            print(
                "GOLD SMART keepalive error:",
                repr(e)
            )

        time.sleep(600)


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
    print(f"{APP_NAME} STARTING")
    print("=" * 60)
    print("AUTO TRADING:", AUTO_TRADING)
    print("XAUS:", XAUS_SPOT_URL)
    print("POLL:", POLL_SECONDS)
    print("COOLDOWN:", SIGNAL_COOLDOWN_MIN, "min")
    print("STATS_FILE:", STATS_FILE)
    print("TRADE_FILE:", TRADE_FILE)

    load_trade()

    # Webhook setup happens once per process.
    if BOT_TOKEN:
        configure_webhook()

        if BOOT_NOTICE and TELEGRAM_CHAT_ID:
            send_telegram(
                f"🟢 {APP_NAME}\n\n"
                f"Бот запущен.\n"
                f"📡 XAUS connected mode\n"
                f"🧠 H4 → H1 → M15 → M5\n"
                f"🤖 AUTO TRADING: OFF\n"
                f"✋ Manual signals only"
            )
    else:
        print("GOLD SMART: BOT_TOKEN missing")

    start_engine()
    start_keepalive()


# ============================================================
# IMPORTANT:
# Gunicorn imports this module.
# Startup runs once and is protected by STARTUP_DONE.
# ============================================================

startup()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
