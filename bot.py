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
APP_NAME = "GOLD SMART V5.9.6"
XAUS_SPOT_URL = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY_URL = "https://xaus.com/api/v1/intraday"
XAUS_USER_AGENT = os.getenv(
    "XAUS_USER_AGENT",
    "Mozilla/5.0 (compatible; GOLD-SMART/5.9.6)"
)
HTTP_HEADERS = {
    "User-Agent": XAUS_USER_AGENT,
    "Accept": "application/json"
}
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
# ============================================================
# ⚙️ SETTINGS
# ============================================================
# AUTO TRADING must stay OFF.
# This bot contains no order-execution code.
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
# ============================================================
# 💾 PERSISTENCE
# ============================================================
# On Render use a persistent Disk (paid plans only)
# and set:
# STATS_FILE=/data/gold_stats.json
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
LOCK_RETRY_SECONDS = 5
MAX_TRADES_HISTORY = 500
# ============================================================
# 📊 XAUS DATA
# ============================================================
# XAUS intraday supports at most 48 hours
# and samples approximately every 2 minutes.
INTRADAY_HOURS = 48
MIN_CANDLES = {
    "M5": 30,
    "M15": 16,
    "H1": 12,
    "H4": 8
}
# ============================================================
# 🛡 DATA FRESHNESS GUARDS
# ============================================================
# Spot cache accepted only for this amount of time.
SPOT_MAX_AGE = 300
# Intraday cache accepted after failed fetch only for this amount.
INTRADAY_MAX_CACHE_AGE = 600
# Newest real XAUS point must not be older than this.
STALE_SECONDS = 30 * 60
# ============================================================
# 🟡 EARLY TRACKING
# ============================================================
# Evaluation only.
# Does not change signal generation.
EARLY_TRACKING = True
MAX_EARLY_OPEN = 10
# ============================================================
# 🌐 RENDER
# ============================================================
# Self-ping is OFF by default.
# It does NOT guarantee that Render Free stays awake.
KEEPALIVE = os.getenv("KEEPALIVE", "0") == "1"
KEEPALIVE_SECONDS = 600
# Small change:
# Boot notification is OFF by default to avoid
# unnecessary Telegram messages after restart.
BOOT_NOTICE = os.getenv("BOOT_NOTICE", "0") == "1"
# ============================================================
# 🔐 TELEGRAM WEBHOOK SECURITY
# ============================================================
WEBHOOK_SECRET = hashlib.sha256(
    ("gold-smart:" + BOT_TOKEN).encode()
).hexdigest()[:48]
app = Flask(__name__)
# ============================================================
# 🧠 GLOBAL STATE
# ============================================================
price_cache = {
    "price": None,
    "time": 0
}
intraday_cache = {
    "df": None,
    "time": 0,
    "last_ts": None
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
    "last_error": None
}
stats = {}
virtual_trade = None
engine_started = False
# ============================================================
# 🔒 LOCK DECORATOR
# ============================================================
def locked(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with state_lock:
            return fn(*args, **kwargs)
    return wrapper
# ============================================================
# 🕒 TIME
# ============================================================
def now_iso():
    return datetime.now(timezone.utc).isoformat()
# ============================================================
# 📡 TELEGRAM
# ============================================================
def telegram_url(method):
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
def send_telegram(text_message, chat_id=None):
    target = str(chat_id or CHAT_ID)
    if not BOT_TOKEN or not target:
        print("Telegram credentials missing")
        return False
    for attempt in range(2):
        try:
            r = requests.post(
                telegram_url("sendMessage"),
                json={
                    "chat_id": target,
                    "text": text_message
                },
                timeout=15
            )
            if r.status_code == 200:
                return True
            if r.status_code == 429 and attempt == 0:
                try:
                    wait = int(
                        r.json()
                        .get("parameters", {})
                        .get("retry_after", 3)
                    )
                except Exception:
                    wait = 3
                time.sleep(min(wait, 10))
                continue
            print("Telegram error:", r.text)
            return False
        except Exception as e:
            print("Telegram exception:", repr(e))
            return False
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
        r = requests.post(
            telegram_url("setWebhook"),
            json={
                "url": webhook_url,
                "drop_pending_updates": False,
                "allowed_updates": ["message"],
                "secret_token": WEBHOOK_SECRET
            },
            timeout=15
        )
        print("Webhook:", r.text)
        return r.status_code == 200
    except Exception as e:
        print("Webhook setup error:", repr(e))
        return False
# ============================================================
# 💾 STATS
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
        "early_be": 0,
        "early_open": [],
        "last_full_ts": 0.0,
        "last_early_ts": 0.0,
        "trades": []
    }
def atomic_write_json(path, data):
    with file_lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )
        os.replace(tmp, path)
def load_stats():
    if not os.path.exists(STATS_FILE):
        return default_stats()
    try:
        with open(
            STATS_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)
        base = default_stats()
        if isinstance(data, dict):
            base.update(data)
        if not isinstance(base.get("trades"), list):
            base["trades"] = []
        if not isinstance(base.get("early_open"), list):
            base["early_open"] = []
        return base
    except Exception as e:
        print("Stats load error:", repr(e))
        return default_stats()
def save_stats():
    with state_lock:
        stats["open"] = 1 if virtual_trade else 0
        if len(stats["trades"]) > MAX_TRADES_HISTORY:
            stats["trades"] = stats["trades"][
                -MAX_TRADES_HISTORY:
            ]
        try:
            atomic_write_json(
                STATS_FILE,
                stats
            )
        except Exception as e:
            print("Stats save error:", repr(e))
def save_trade():
    with state_lock:
        try:
            if virtual_trade is None:
                if os.path.exists(TRADE_FILE):
                    os.remove(TRADE_FILE)
            else:
                atomic_write_json(
                    TRADE_FILE,
                    virtual_trade
                )
        except Exception as e:
            print("Trade save error:", repr(e))
def load_trade():
    try:
        if not os.path.exists(TRADE_FILE):
            return None
        with open(
            TRADE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)
        needed = (
            "direction",
            "signal_type",
            "entry",
            "sl",
            "tp",
            "be"
        )
        if (
            isinstance(data, dict)
            and all(k in data for k in needed)
        ):
            return data
    except Exception as e:
        print("Trade load error:", repr(e))
    return None
def init_state():
    global stats, virtual_trade
    with state_lock:
        stats = load_stats()
        virtual_trade = load_trade()
        stats["open"] = 1 if virtual_trade else 0
# ============================================================
# 📈 DATA FETCH
# ============================================================
def fetch_spot(force=False):
    now = time.time()
    with spot_lock:
        cached = price_cache["price"]
        age = now - price_cache["time"]
        if (
            not force
            and cached is not None
            and age < CACHE_SECONDS
        ):
            return cached
        try:
            r = requests.get(
                XAUS_SPOT_URL,
                params={
                    "fresh": int(now)
                },
                headers=HTTP_HEADERS,
                timeout=15
            )
            r.raise_for_status()
            data = r.json()
            price = None
            if isinstance(data, dict):
                xau = data.get("xau")
                if isinstance(xau, dict):
                    price = xau.get("price")
                if price is None:
                    price = data.get("spot_usd_oz")
            if price is None:
                raise ValueError(
                    f"Unknown spot response: {data}"
                )
            price = float(price)
            if price <= 0:
                raise ValueError(
                    f"Invalid spot price: {price}"
                )
            price_cache.update({
                "price": price,
                "time": now
            })
            return price
        except Exception as e:
            print("Spot error:", repr(e))
            if (
                cached is not None
                and age < SPOT_MAX_AGE
            ):
                return cached
            return None
def fetch_intraday(force=False):
    """
    Returns:
        (ohlc_2min_dataframe, last_real_point_timestamp)
    or:
        (None, None)
    """
    now = time.time()
    with intraday_lock:
        if (
            not force
            and intraday_cache["df"] is not None
            and now - intraday_cache["time"] < CACHE_SECONDS
        ):
            return (
                intraday_cache["df"].copy(),
                intraday_cache["last_ts"]
            )
        try:
            r = requests.get(
                XAUS_INTRADAY_URL,
                params={
                    "symbol": "xau",
                    "hours": INTRADAY_HOURS,
                    "fresh": int(now)
                },
                headers=HTTP_HEADERS,
                timeout=20
            )
            r.raise_for_status()
            data = r.json()
            points = None
            if isinstance(data, dict):
                for key in (
                    "points",
                    "data",
                    "series"
                ):
                    if isinstance(
                        data.get(key),
                        list
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
                if (
                    timestamp is None
                    or price is None
                ):
                    continue
                try:
                    if isinstance(
                        timestamp,
                        str
                    ):
                        stripped = timestamp.strip()
                        if stripped.replace(
                            ".",
                            "",
                            1
                        ).isdigit():
                            timestamp = float(
                                stripped
                            )
                    if isinstance(
                        timestamp,
                        (int, float)
                    ):
                        unit = (
                            "ms"
                            if timestamp > 100000000000
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
                    value = float(price)
                    if value <= 0:
                        continue
                    rows.append({
                        "time": dt,
                        "price": value
                    })
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
            # Real timestamp BEFORE resampling.
            last_ts = df.index[-1]
            # XAUS is approximately 2-minute cadence.
            ohlc = (
                df["price"]
                .resample("2min")
                .ohlc()
                .dropna()
            )
            if CLOSED_CANDLES and len(ohlc) > 2:
                ohlc = ohlc.iloc[:-1]
            if len(ohlc) < 20:
                raise ValueError(
                    "Not enough normalized "
                    f"intraday candles: {len(ohlc)}"
                )
            intraday_cache.update({
                "df": ohlc.copy(),
                "time": now,
                "last_ts": last_ts
            })
            return ohlc, last_ts
        except Exception as e:
            print(
                "Intraday error:",
                repr(e)
            )
            if (
                intraday_cache["df"] is not None
                and now - intraday_cache["time"]
                < INTRADAY_MAX_CACHE_AGE
            ):
                return (
                    intraday_cache["df"].copy(),
                    intraday_cache["last_ts"]
                )
            return None, None
def resample_ohlc(df, timeframe):
    if df is None or len(df) < 5:
        return None
    rules = {
        "M5": "5min",
        "M15": "15min",
        "H1": "1h",
        "H4": "4h"
    }
    if timeframe not in rules:
        return None
    out = (
        df
        .resample(rules[timeframe])
        .agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last"
        })
        .dropna()
    )
    if CLOSED_CANDLES and len(out) > 2:
        out = out.iloc[:-1]
    return out
# ============================================================
# 🧠 STRATEGY
# ============================================================
def add_indicators(df):
    if df is None or len(df) < 5:
        return None
    x = df.copy()
    x["ema20"] = x["close"].ewm(
        span=20,
        adjust=False
    ).mean()
    x["ema50"] = x["close"].ewm(
        span=50,
        adjust=False
    ).mean()
    x["ema200"] = x["close"].ewm(
        span=200,
        adjust=False
    ).mean()
    delta = x["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(
        14,
        min_periods=1
    ).mean()
    avg_loss = loss.rolling(
        14,
        min_periods=1
    ).mean()
    rs = avg_gain / avg_loss.replace(
        0,
        np.nan
    )
    x["rsi"] = (
        100 - 100 / (1 + rs)
    ).fillna(50)
    return x
def structure_direction(df, lookback=30):
    if df is None or len(df) < 7:
        return "MIXED"
    x = df.tail(lookback)
    highs = []
    lows = []
    h = x["high"].values
    l = x["low"].values
    if len(x) < 5:
        return "MIXED"
    for i in range(2, len(x) - 2):
        if (
            h[i] > h[i-1]
            and h[i] > h[i-2]
            and h[i] > h[i+1]
            and h[i] > h[i+2]
        ):
            highs.append(h[i])
        if (
            l[i] < l[i-1]
            and l[i] < l[i-2]
            and l[i] < l[i+1]
            and l[i] < l[i+2]
        ):
            lows.append(l[i])
    if len(highs) < 2 or len(lows) < 2:
        return "MIXED"
    hh, ph = highs[-1], highs[-2]
    ll, pl = lows[-1], lows[-2]
    if hh > ph and ll > pl:
        return "BULLISH"
    if hh < ph and ll < pl:
        return "BEARISH"
    return "MIXED"
def momentum_from_df(df):
    if df is None or len(df) < 6:
        return "MIXED"
    a = float(df["close"].iloc[-1])
    b = float(df["close"].iloc[-5])
    if a > b:
        return "BULLISH"
    if a < b:
        return "BEARISH"
    return "MIXED"
def detect_bos(df, lookback=8):
    if (
        df is None
        or len(df) < lookback + 2
    ):
        return "NONE"
    recent = df.iloc[
        -lookback-1:-1
    ]
    last = df.iloc[-1]
    if last["close"] > recent["high"].max():
        return "BULLISH BOS"
    if last["close"] < recent["low"].min():
        return "BEARISH BOS"
    return "NONE"
def detect_liquidity(df, lookback=12):
    if (
        df is None
        or len(df) < lookback + 2
    ):
        return "NONE"
    previous = df.iloc[
        -lookback-1:-1
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
        df["close"] - df["open"]
    ).abs()
    avg = bodies.iloc[-11:-1].mean()
    body = bodies.iloc[-1]
    if (
        avg > 0
        and body >= avg * 1.8
    ):
        if df["close"].iloc[-1] > df["open"].iloc[-1]:
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
def trend_score(df):
    if df is None or len(df) < 7:
        return {
            "direction": "MIXED",
            "score": 50,
            "structure": "MIXED",
            "momentum": "MIXED",
            "bos": "NONE"
        }
    x = add_indicators(df)
    if x is None:
        return {
            "direction": "MIXED",
            "score": 50,
            "structure": "MIXED",
            "momentum": "MIXED",
            "bos": "NONE"
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
    close, e20, e50, e200 = map(
        float,
        (
            last["close"],
            last["ema20"],
            last["ema50"],
            last["ema200"]
        )
    )
    if close > e20 > e50 > e200:
        bull += 25
    elif close < e20 < e50 < e200:
        bear += 25
    elif close > e50 and e20 > e50:
        bull += 15
    elif close < e50 and e20 < e50:
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
            "bos": bos
        }
    side = (
        bull
        if bull > bear
        else bear
    )
    other = (
        bear
        if bull > bear
        else bull
    )
    score = max(
        0,
        min(
            100,
            round(
                50 + (side - other) / 2
            )
        )
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
        "bos": bos
    }
# ============================================================
# 🏗 MARKET DATA
# ============================================================
def build_market_data():
    raw, last_ts = fetch_intraday()
    if raw is None or last_ts is None:
        engine_state["data_age"] = None
        return None
    age = (
        pd.Timestamp.now(tz="UTC")
        - last_ts
    ).total_seconds()
    engine_state["data_age"] = age
    if age > STALE_SECONDS:
        print(
            "Market data stale: newest XAUS point "
            f"is {age/60:.0f} min old "
            "(market closed or API frozen)"
        )
        return None
    if age < -600:
        print(
            "Market data timestamp is "
            f"{abs(age)/60:.0f} min in the future "
            "(clock or timezone problem)"
        )
        return None
    result = {}
    for tf in (
        "M5",
        "M15",
        "H1",
        "H4"
    ):
        tf_df = resample_ohlc(
            raw,
            tf
        )
        minimum = MIN_CANDLES[tf]
        if (
            tf_df is None
            or len(tf_df) < minimum
        ):
            print(
                f"Market data: {tf} has "
                f"{0 if tf_df is None else len(tf_df)} "
                f"candles; need {minimum}"
            )
            return None
        result[tf] = add_indicators(tf_df)
    return result
# ============================================================
# 🎯 SIGNAL SCORING
# ============================================================
def score_signal(market):
    h4, h1, m15, m5 = (
        market[k]
        for k in (
            "H4",
            "H1",
            "M15",
            "M5"
        )
    )
    trend_data = {
        tf: trend_score(market[tf])
        for tf in (
            "H4",
            "H1",
            "M15",
            "M5"
        )
    }
    buy_score = 0.0
    sell_score = 0.0
    weights = {
        "H4": 20,
        "H1": 15,
        "M15": 15,
        "M5": 10
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
    if buy_score > sell_score:
        dominant = "BUY"
        dominant_score = buy_score
    elif sell_score > buy_score:
        dominant = "SELL"
        dominant_score = sell_score
    else:
        dominant = "NONE"
        dominant_score = 0
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
            1
        ),
        "sell_score": round(
            sell_score,
            1
        ),
        "dominant": dominant,
        "dominant_score": round(
            dominant_score,
            1
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
            1
        )
    }
# ============================================================
# 💬 MESSAGES
# ============================================================
def trend_line(tf, data):
    return (
        f"📊 {tf}: "
        f"{data['direction']} "
        f"({data['score']}/100)"
    )
def build_signal_message(price, analysis):
    td = analysis["trend_data"]
    signal = analysis["signal"]
    emoji = {
        "BUY": "🟢",
        "SELL": "🔴",
        "EARLY BUY": "🟡",
        "EARLY SELL": "🟠"
    }.get(signal, "⚪")
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
# 📌 VIRTUAL TRADES
# ============================================================
def make_trade(
    direction,
    price,
    signal_type
):
    if direction == "BUY":
        sl = price - SL_DISTANCE
        tp = price + TP_DISTANCE
    else:
        sl = price + SL_DISTANCE
        tp = price - TP_DISTANCE
    return {
        "direction": direction,
        "signal_type": signal_type,
        "entry": round(
            price,
            2
        ),
        "sl": round(
            sl,
            2
        ),
        "tp": round(
            tp,
            2
        ),
        "original_sl": round(
            sl,
            2
        ),
        "be": False,
        "opened_at": now_iso()
    }
def evaluate_trade(
    trade,
    price
):
    entry = trade["entry"]
    be_now = False
    buy = (
        trade["direction"]
        == "BUY"
    )
    move = (
        price - entry
        if buy
        else entry - price
    )
    # BE first
    if (
        not trade["be"]
        and move >= BE_TRIGGER
    ):
        trade["sl"] = entry
        trade["be"] = True
        be_now = True
    if buy:
        if price <= trade["sl"]:
            return (
                "BE"
                if trade["be"]
                else "LOSS"
            ), be_now
        if price >= trade["tp"]:
            return "WIN", be_now
    else:
        if price >= trade["sl"]:
            return (
                "BE"
                if trade["be"]
                else "LOSS"
            ), be_now
        if price <= trade["tp"]:
            return "WIN", be_now
    return None, be_now
def be_message(
    trade,
    price
):
    side = (
        "🟢 BUY"
        if trade["direction"] == "BUY"
        else "🔴 SELL"
    )
    entry = trade["entry"]
    return (
        f"🛡 {APP_NAME}\n\n"
        f"🔒 BREAK EVEN\n\n"
        f"{side}\n\n"
        f"💰 Entry: {entry:.2f}\n"
        f"📍 Current: {price:.2f}\n"
        f"➡️ SL moved to BE: {entry:.2f}\n\n"
        f"AUTO TRADING: OFF"
    )
@locked
def open_virtual_trade(
    direction,
    price,
    signal_type
):
    global virtual_trade
    if (
        direction not in (
            "BUY",
            "SELL"
        )
        or signal_type not in (
            "BUY",
            "SELL"
        )
        or virtual_trade is not None
    ):
        return None
    virtual_trade = make_trade(
        direction,
        price,
        signal_type
    )
    save_stats()
    save_trade()
    side = (
        "🟢 BUY"
        if direction == "BUY"
        else "🔴 SELL"
    )
    rr = (
        TP_DISTANCE
        / SL_DISTANCE
    )
    return (
        "📌 VIRTUAL TRADE OPENED\n\n"
        f"{side}\n\n"
        f"💰 Entry: "
        f"{virtual_trade['entry']:.2f}\n"
        f"🛑 SL: "
        f"{virtual_trade['sl']:.2f}\n"
        f"🎯 TP: "
        f"{virtual_trade['tp']:.2f}\n"
        f"📐 RR: 1:{rr:.0f}\n"
        f"📉 Risk: {RISK_PERCENT:.1f}%\n"
        f"🛡 AUTO TRADING: OFF"
    )
@locked
def close_virtual_trade(
    result,
    price=None
):
    global virtual_trade
    if virtual_trade is None:
        return None
    direction = virtual_trade["direction"]
    signal_type = virtual_trade["signal_type"]
    if result == "WIN":
        stats["win"] += 1
    elif result == "LOSS":
        stats["loss"] += 1
    elif result == "BE":
        stats["be"] += 1
    if result == "WIN":
        stats["full_win"] += 1
    elif result == "LOSS":
        stats["full_loss"] += 1
    stats["trades"].append({
        "kind": "FULL",
        "direction": direction,
        "signal_type": signal_type,
        "entry": virtual_trade["entry"],
        "sl": virtual_trade["sl"],
        "tp": virtual_trade["tp"],
        "result": result,
        "closed_at": now_iso()
    })
    label = {
        "WIN": "✅ WIN",
        "LOSS": "❌ LOSS",
        "BE": "⚪ BREAK EVEN"
    }.get(
        result,
        result
    )
    exit_txt = (
        "N/A"
        if price is None
        else f"{price:.2f}"
    )
    message = (
        f"📌 {APP_NAME}\n\n"
        f"VIRTUAL TRADE CLOSED\n\n"
        f"{label}\n\n"
        f"{direction}\n"
        f"💰 Entry: "
        f"{virtual_trade['entry']:.2f}\n"
        f"📍 Exit price: "
        f"{exit_txt}\n"
        f"🛑 SL: "
        f"{virtual_trade['sl']:.2f}\n"
        f"🎯 TP: "
        f"{virtual_trade['tp']:.2f}\n\n"
        f"🛡 AUTO TRADING: OFF"
    )
    virtual_trade = None
    save_stats()
    save_trade()
    return message
@locked
def update_virtual_trade(price):
    if virtual_trade is None:
        return None, []
    messages = []
    result, be_now = evaluate_trade(
        virtual_trade,
        price
    )
    if be_now:
        save_stats()
        save_trade()
        messages.append(
            be_message(
                virtual_trade,
                price
            )
        )
    if result:
        closed_message = close_virtual_trade(
            result,
            price
        )
        if closed_message:
            messages.append(
                closed_message
            )
    return result, messages
# ============================================================
# 🟡 EARLY VIRTUAL TRADES
# ============================================================
@locked
def open_early_trade(
    direction,
    price
):
    if (
        not EARLY_TRACKING
        or direction not in (
            "BUY",
            "SELL"
        )
    ):
        return
    if (
        len(stats["early_open"])
        >= MAX_EARLY_OPEN
    ):
        return
    stats["early_open"].append(
        make_trade(
            direction,
            price,
            "EARLY"
        )
    )
    save_stats()
@locked
def update_early_trades(price):
    if not stats["early_open"]:
        return []
    still_open = []
    results = []
    changed = False
    for trade in stats["early_open"]:
        be_before = trade["be"]
        result, _ = evaluate_trade(
            trade,
            price
        )
        if trade["be"] != be_before:
            changed = True
        if result is None:
            still_open.append(trade)
            continue
        changed = True
        if result == "WIN":
            stats["early_win"] += 1
        elif result == "LOSS":
            stats["early_loss"] += 1
        elif result == "BE":
            stats["early_be"] += 1
        stats["trades"].append({
            "kind": "EARLY",
            "direction": trade["direction"],
            "signal_type": "EARLY",
            "entry": trade["entry"],
            "sl": trade["sl"],
            "tp": trade["tp"],
            "result": result,
            "closed_at": now_iso()
        })
        results.append(result)
    stats["early_open"] = still_open
    if changed:
        save_stats()
    return results
# ============================================================
# 📊 SIGNAL REGISTRATION
# ============================================================
@locked
def register_signal(
    kind,
    ts
):
    stats["total_signals"] += 1
    if kind == "full":
        stats["full_signals"] += 1
        stats["last_full_ts"] = ts
    else:
        stats["early_signals"] += 1
        stats["last_early_ts"] = ts
    save_stats()
# ============================================================
# 📊 STATISTICS MESSAGE
# ============================================================
@locked
def stats_message():
    total = stats.get(
        "total_signals",
        0
    )
    win = stats.get(
        "win",
        0
    )
    loss = stats.get(
        "loss",
        0
    )
    be = stats.get(
        "be",
        0
    )
    closed = win + loss + be
    wr = (
        win / closed * 100
        if closed
        else 0
    )
    full = stats.get(
        "full_signals",
        0
    )
    fw = stats.get(
        "full_win",
        0
    )
    fl = stats.get(
        "full_loss",
        0
    )
    early = stats.get(
        "early_signals",
        0
    )
    ew = stats.get(
        "early_win",
        0
    )
    el = stats.get(
        "early_loss",
        0
    )
    eb = stats.get(
        "early_be",
        0
    )
    fwr = (
        fw / (fw + fl) * 100
        if fw + fl
        else 0
    )
    ewr = (
        ew / (ew + el) * 100
        if ew + el
        else 0
    )
    early_open = len(
        stats.get(
            "early_open",
            []
        )
    )
    open_count = (
        1
        if virtual_trade
        else 0
    )
    if not virtual_trade:
        vt = "⚪ No virtual trade"
    else:
        vt = f"""🟡 OPEN
{virtual_trade["direction"]}
Entry: {virtual_trade["entry"]:.2f}
SL: {virtual_trade["sl"]:.2f}
TP: {virtual_trade["tp"]:.2f}
BE: {"ON" if virtual_trade["be"] else "OFF"}"""
    return f"""📊 {APP_NAME} — STATISTICS
━━━━━━━━━━━━━━━━━━
📌 TOTAL SIGNALS: {total}
🟢 WIN: {win}
🔴 LOSS: {loss}
⚪ BE: {be}
🟡 OPEN: {open_count}
━━━━━━━━━━━━━━━━━━
🎯 CLOSED: {closed}
📈 WIN RATE: {wr:.1f}%
━━━━━━━━━━━━━━━━━━
🔥 FULL SIGNALS: {full}
🟢 FULL WIN: {fw}
🔴 FULL LOSS: {fl}
🎯 FULL WIN RATE: {fwr:.1f}%
━━━━━━━━━━━━━━━━━━
🟡 EARLY SIGNALS: {early}
🟢 EARLY WIN: {ew}
🔴 EARLY LOSS: {el}
⚪ EARLY BE: {eb}
🔎 EARLY OPEN (virtual): {early_open}
🎯 EARLY WIN RATE: {ewr:.1f}%
ℹ️ EARLY результаты считаются виртуально
(SL/TP/BE как у FULL) только для сигналов,
отправленных после включения учёта.
━━━━━━━━━━━━━━━━━━
{vt}
━━━━━━━━━━━━━━━━━━
🛡 AUTO TRADING: OFF
📉 RISK: {RISK_PERCENT:.1f}%"""
# ============================================================
# 📡 STATUS
# ============================================================
def engine_status_text():
    now = time.time()
    if engine_state["started"] is None:
        return "🟡 Engine: STANDBY / STARTING"
    last = engine_state["last_cycle"]
    if last is None:
        return "🟡 Engine: STARTING"
    since = now - last
    if since > POLL_SECONDS * 3 + 60:
        return (
            f"🔴 Engine: STALLED "
            f"({since:.0f}s since last cycle)"
        )
    return "🟢 Engine: RUNNING"
def status_message():
    price = fetch_spot()
    p = (
        "N/A"
        if price is None
        else f"{price:.2f}"
    )
    with state_lock:
        vt = "⚪ No virtual trade"
        if virtual_trade:
            vt = f"""🟡 Virtual trade:
{virtual_trade["direction"]}
Entry: {virtual_trade["entry"]:.2f}
SL: {virtual_trade["sl"]:.2f}
TP: {virtual_trade["tp"]:.2f}
BE: {"ON" if virtual_trade["be"] else "OFF"}"""
        early_open = len(
            stats.get(
                "early_open",
                []
            )
        )
    age = engine_state["data_age"]
    age_txt = (
        "N/A"
        if age is None
        else f"{age/60:.1f} min"
    )
    started = engine_state["started"]
    uptime_txt = (
        "N/A"
        if started is None
        else f"{(time.time() - started)/60:.0f} min"
    )
    return f"""🥇 {APP_NAME}
{engine_status_text()}
🟢 Telegram: READY
{"🟢" if price is not None else "🔴"} XAUS API:
{"READY" if price is not None else "UNAVAILABLE"}
💰 XAUUSD: {p}
🕯 Data age: {age_txt}
⏲ Engine uptime: {uptime_txt}
{vt}
🔎 Early virtual open: {early_open}
━━━━━━━━━━━━━━━━━━
🛡 AUTO TRADING: OFF
📉 Risk: {RISK_PERCENT:.1f}%
🎯 Min Score: {MIN_SCORE}
🕯 Closed candles: {"ON" if CLOSED_CANDLES else "OFF"}
⏱ Poll: {POLL_SECONDS}s
⏳ Cooldown: {COOLDOWN_MIN} min
(FULL и EARLY раздельно)"""
def test_message():
    price = fetch_spot(
        force=True
    )
    return f"""🧪 {APP_NAME} TEST
🟢 Engine: READY
🟢 Telegram: READY
{"🟢" if price is not None else "🔴"} XAUS API:
{"READY" if price is not None else "UNAVAILABLE"}
💰 XAUUSD:
{"N/A" if price is None else f"{price:.2f}"}
🛡 AUTO TRADING: OFF
📉 Risk: {RISK_PERCENT:.1f}%"""
# ============================================================
# 🔎 GENERATE SIGNAL
# ============================================================
def generate_signal():
    price = fetch_spot(
        force=True
    )
    market = build_market_data()
    if (
        price is None
        or market is None
    ):
        return f"""⚠️ {APP_NAME}
❌ Нет свежих данных XAU/USD
(API недоступен, рынок закрыт,
данные устарели или мало свечей).
В логах Render будет указана причина."""
    return build_signal_message(
        price,
        score_signal(market)
    )
# ============================================================
# 🤖 TELEGRAM COMMANDS
# ============================================================
def handle_command(command):
    command = (
        command.strip()
        .lower()
        .split()[0]
        .split("@")[0]
        if command.strip()
        else ""
    )
    if command == "/start":
        return f"""🥇 {APP_NAME}
🟢 GOLD SMART запущен.
H4 → H1 → M15 → M5
🧠 Smart Trend
💧 Liquidity
🔨 BOS
🧩 FVG
💥 Displacement
📈 Momentum
📊 RSI
🎯 Premium / Discount
Команды:
/signal
/status
/stats
/test
/help
🛡 AUTO TRADING: OFF
📉 RISK: 1%"""
    if command == "/help":
        return f"""🥇 {APP_NAME}
📌 КОМАНДЫ
/signal — текущий анализ XAU/USD
/status — состояние бота
/stats — статистика
/test — Telegram + XAUS
/help — помощь
🧭 H4 + H1 = Higher TF Bias
🎯 M5 = основной trigger
🛡 AUTO TRADING: OFF
📉 RISK: 1%"""
    if command == "/status":
        return status_message()
    if command == "/stats":
        return stats_message()
    if command == "/test":
        return test_message()
    if command == "/signal":
        return generate_signal()
    return (
        f"🥇 {APP_NAME}\n\n"
        "Неизвестная команда.\n"
        "Используй /help"
    )
# ============================================================
# 🔄 ENGINE
# ============================================================
def process_cycle(price):
    result, messages = update_virtual_trade(
        price
    )
    for message in messages:
        send_telegram(message)
    if result:
        print(
            "Virtual trade closed:",
            result
        )
    for early_result in update_early_trades(
        price
    ):
        print(
            "Early virtual trade closed:",
            early_result
        )
    market = build_market_data()
    if market is None:
        return
    analysis = score_signal(
        market
    )
    signal = analysis["signal"]
    print(
        f"[{now_iso()}] "
        f"{signal} | "
        f"XAUUSD {price:.2f} | "
        f"BUY {analysis['buy_score']} | "
        f"SELL {analysis['sell_score']} | "
        f"BIAS {analysis['higher_bias']}"
    )
    now = time.time()
    with state_lock:
        full_ok = (
            now
            - float(
                stats.get(
                    "last_full_ts",
                    0
                ) or 0
            )
            >= COOLDOWN_MIN * 60
        )
        early_ok = (
            now
            - float(
                stats.get(
                    "last_early_ts",
                    0
                ) or 0
            )
            >= COOLDOWN_MIN * 60
        )
        trade_open = (
            virtual_trade is not None
        )
    # FULL SIGNAL
    if (
        signal in (
            "BUY",
            "SELL"
        )
        and full_ok
        and not trade_open
    ):
        if send_telegram(
            build_signal_message(
                price,
                analysis
            )
        ):
            register_signal(
                "full",
                now
            )
            opened_message = (
                open_virtual_trade(
                    signal,
                    price,
                    signal
                )
            )
            if opened_message:
                send_telegram(
                    opened_message
                )
    # EARLY SIGNAL
    elif (
        signal in (
            "EARLY BUY",
            "EARLY SELL"
        )
        and early_ok
    ):
        if send_telegram(
            build_signal_message(
                price,
                analysis
            )
        ):
            register_signal(
                "early",
                now
            )
            open_early_trade(
                signal.split()[1],
                price
            )
def engine_loop():
    print(
        f"{APP_NAME} engine started"
    )
    engine_state["started"] = time.time()
    while True:
        engine_state["last_cycle"] = time.time()
        try:
            price = fetch_spot()
            if price is None:
                print(
                    "Price unavailable"
                )
            else:
                process_cycle(
                    price
                )
        except Exception as e:
            engine_state["last_error"] = repr(e)
            print(
                "ENGINE ERROR:",
                repr(e)
            )
            traceback.print_exc()
        time.sleep(
            POLL_SECONDS
        )
# ============================================================
# 🌐 HTTP / WEBHOOK
# ============================================================
@app.route(
    "/",
    methods=["GET"]
)
def home():
    return {
        "app": APP_NAME,
        "status": "READY",
        "auto_trading": AUTO_TRADING,
        "risk_percent": RISK_PERCENT
    }
@app.route(
    "/health",
    methods=["GET"]
)
def health():
    last = engine_state["last_cycle"]
    return {
        "status": "ok",
        "app": APP_NAME,
        "time": now_iso(),
        "engine_running": (
            engine_state["started"]
            is not None
        ),
        "seconds_since_last_cycle": (
            None
            if last is None
            else round(
                time.time() - last
            )
        )
    }
@app.route(
    "/test",
    methods=["GET"]
)
def http_test():
    return test_message()
def process_command(
    text_message,
    chat_id
):
    try:
        send_telegram(
            handle_command(
                text_message
            ),
            chat_id
        )
    except Exception as e:
        print(
            "Command error:",
            repr(e)
        )
@app.route(
    "/telegram",
    methods=["POST"]
)
def telegram_webhook():
    try:
        token = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            ""
        )
        if not hmac.compare_digest(
            token.encode("utf-8"),
            WEBHOOK_SECRET.encode("utf-8")
        ):
            return "forbidden", 403
        update = (
            request.get_json(
                silent=True
            )
            or {}
        )
        # Prevent duplicate Telegram updates.
        update_id = update.get(
            "update_id"
        )
        if update_id is not None:
            with updates_lock:
                if update_id in recent_updates:
                    return "OK"
                recent_updates.append(
                    update_id
                )
        message = (
            update.get("message")
            or {}
        )
        text_message = message.get(
            "text",
            ""
        )
        if (
            not text_message
            or not text_message.startswith("/")
        ):
            return "OK"
        incoming_chat_id = str(
            (
                message.get("chat")
                or {}
            ).get(
                "id",
                ""
            )
        )
        if not CHAT_ID:
            print(
                "TELEGRAM_CHAT_ID is not set. "
                "Incoming chat id:",
                incoming_chat_id
            )
            return "OK"
        if incoming_chat_id != CHAT_ID:
            return "OK"
        # Respond immediately to Telegram.
        threading.Thread(
            target=process_command,
            args=(
                text_message,
                incoming_chat_id
            ),
            daemon=True
        ).start()
        return "OK"
    except Exception as e:
        print(
            "Webhook error:",
            repr(e)
        )
        return "OK"
# ============================================================
# 🔐 SINGLE ENGINE INSTANCE
# ============================================================
_lock_handle = None
def acquire_single_instance_lock():
    global _lock_handle
    if _lock_handle is not None:
        return True
    try:
        import fcntl
    except ImportError:
        return True
    fh = None
    try:
        fh = open(
            LOCK_FILE,
            "w"
        )
        fcntl.flock(
            fh,
            fcntl.LOCK_EX
            | fcntl.LOCK_NB
        )
        _lock_handle = fh
        return True
    except OSError:
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        return False
def keepalive_loop():
    url = (
        RENDER_EXTERNAL_URL.rstrip("/")
        + "/health"
    )
    while True:
        time.sleep(
            KEEPALIVE_SECONDS
        )
        try:
            requests.get(
                url,
                timeout=10
            )
        except Exception as e:
            print(
                "Keepalive error:",
                repr(e)
            )
def engine_supervisor():
    while not acquire_single_instance_lock():
        time.sleep(
            LOCK_RETRY_SECONDS
        )
    # Load state after acquiring engine ownership.
    init_state()
    configure_webhook()
    if (
        KEEPALIVE
        and RENDER_EXTERNAL_URL
    ):
        threading.Thread(
            target=keepalive_loop,
            daemon=True,
            name="gold-smart-keepalive"
        ).start()
    # Boot notification is disabled by default.
    if BOOT_NOTICE:
        with state_lock:
            trade_txt = (
                "yes"
                if virtual_trade
                else "no"
            )
        send_telegram(
            f"🟢 {APP_NAME}\n\n"
            f"ENGINE STARTED\n\n"
            f"📌 Open virtual trade: "
            f"{trade_txt}\n"
            f"🛡 AUTO TRADING: OFF"
        )
    engine_loop()
def start_engine():
    global engine_started
    if engine_started:
        return
    engine_started = True
    threading.Thread(
        target=engine_supervisor,
        daemon=True,
        name="gold-smart-engine"
    ).start()
# ============================================================
# 🚀 STARTUP
# ============================================================
# Gunicorn imports bot:app,
# therefore engine starts on import.
# IMPORTANT:
# Render Start Command:
#
# gunicorn bot:app --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT
#
# DO NOT use --preload.
init_state()
start_engine()
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
