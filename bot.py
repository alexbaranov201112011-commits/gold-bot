import os
import json
import time
import threading
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd

from flask import Flask, request, jsonify


# ============================================================
# 🥇 GOLD SMART V5.9.1
# SMART SMC + SMART TREND ENGINE
#
# DATA SOURCE:
# XAUS.COM
#
# XAU/USD
#
# TIMEFRAMES:
# H4 → H1 → M15 → M5
#
# AUTO TRADING = OFF
# SIGNALS = MANUAL
#
# RISK = 1%
# MAX VIRTUAL TRADES = 2
# TARGET RR = 1:3
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
# CONFIG
# ============================================================

APP_NAME = "GOLD SMART V5.9.1"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://gold-bot-q8la.onrender.com"
).strip()

WEBHOOK_PATH = "/telegram"

XAUS_SPOT_URL = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY_URL = "https://xaus.com/api/v1/intraday"

POLL_SECONDS = 60
COOLDOWN_SECONDS = 15 * 60

RISK_PERCENT = 1.0

DEFAULT_DEPOSIT = float(
    os.getenv("DEPOSIT", "800")
)

MIN_SCORE_FULL = 70
MIN_SCORE_EARLY = 60

TARGET_RR = 3.0

MAX_VIRTUAL_TRADES = 2

SESSION_START_HOUR = 12
SESSION_END_HOUR = 23

REQUEST_TIMEOUT = 15

STATE_FILE = "gold_smart_state.json"


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# GLOBAL STATE
# ============================================================

state_lock = threading.Lock()

state = {
    "last_price": None,
    "last_signal": None,
    "last_signal_time": None,
    "last_analysis_time": None,

    "virtual_trades": [],

    "stats": {
        "signals": 0,
        "wins": 0,
        "losses": 0,
        "breakeven": 0,
        "full_tp": 0,
        "partial_tp": 0
    },

    "last_error": None,

    "engine": "READY",
    "telegram": "READY" if BOT_TOKEN else "NOT_CONFIGURED",
    "xaus": "READY",

    "webhook_set": False
}


# ============================================================
# STATE LOAD / SAVE
# ============================================================

def load_state():
    global state

    try:
        if not os.path.exists(STATE_FILE):
            return

        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)

        with state_lock:
            state.update(saved)

    except Exception as e:
        print("STATE LOAD ERROR:", e)


def save_state():
    try:
        with state_lock:
            snapshot = dict(state)

        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                snapshot,
                f,
                ensure_ascii=False,
                indent=2
            )

    except Exception as e:
        print("STATE SAVE ERROR:", e)


load_state()


# ============================================================
# TIME
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def kz_hour():
    """
    Kazakhstan / Aktau = UTC+5.
    """
    return (utc_now().hour + 5) % 24


def kz_time_string():
    dt = utc_now()
    kz = dt + pd.Timedelta(hours=5)

    return kz.strftime("%Y-%m-%d %H:%M:%S")


def in_trading_session():
    h = kz_hour()

    if SESSION_START_HOUR <= SESSION_END_HOUR:
        return SESSION_START_HOUR <= h < SESSION_END_HOUR

    return h >= SESSION_START_HOUR or h < SESSION_END_HOUR


# ============================================================
# TELEGRAM
# ============================================================

def telegram_url(method):
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"


def telegram_send(text, chat_id=None):
    if not BOT_TOKEN:
        print("TELEGRAM: BOT_TOKEN not configured")
        return False

    target = chat_id or CHAT_ID

    if not target:
        print("TELEGRAM: TELEGRAM_CHAT_ID not configured")
        return False

    try:
        r = requests.post(
            telegram_url("sendMessage"),
            json={
                "chat_id": target,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=REQUEST_TIMEOUT
        )

        if r.ok:
            return True

        print("TELEGRAM SEND ERROR:", r.status_code, r.text)

    except Exception as e:
        print("TELEGRAM EXCEPTION:", e)

    return False


def telegram_answer_callback(callback_id):
    if not BOT_TOKEN:
        return

    try:
        requests.post(
            telegram_url("answerCallbackQuery"),
            json={"callback_query_id": callback_id},
            timeout=REQUEST_TIMEOUT
        )
    except Exception:
        pass


def set_webhook():
    if not BOT_TOKEN:
        return False

    url = RENDER_EXTERNAL_URL.rstrip("/") + WEBHOOK_PATH

    try:
        r = requests.post(
            telegram_url("setWebhook"),
            json={
                "url": url,
                "drop_pending_updates": False
            },
            timeout=REQUEST_TIMEOUT
        )

        print("WEBHOOK:", r.text)

        if r.ok:
            with state_lock:
                state["webhook_set"] = True
                state["telegram"] = "READY"

            save_state()
            return True

    except Exception as e:
        print("WEBHOOK ERROR:", e)

    return False


# ============================================================
# XAUS API
# ============================================================

def get_xaus_spot():
    """
    Get live XAU/USD from XAUS.
    """

    try:
        r = requests.get(
            XAUS_SPOT_URL,
            params={
                "currency": "USD",
                "unit": "oz",
                "compact": "1",
                "fresh": int(time.time())
            },
            timeout=REQUEST_TIMEOUT
        )

        if not r.ok:
            raise RuntimeError(
                f"XAUS spot HTTP {r.status_code}: {r.text[:300]}"
            )

        data = r.json()

        price = data.get("spot_usd_oz")

        if price is None:
            xau = data.get("xau", {})
            price = xau.get("price")

        if price is None:
            raise RuntimeError("XAUS spot price missing")

        price = float(price)

        if price <= 0:
            raise RuntimeError("Invalid XAU price")

        data_state = data.get("data_state", {})
        status = data_state.get("status", "unknown")

        if status == "unavailable":
            raise RuntimeError("XAUS price unavailable")

        return {
            "price": price,
            "status": status,
            "as_of": data_state.get("as_of"),
            "updated_at": data.get("updated_at"),
            "source": data.get("source", "xaus.com")
        }

    except Exception as e:
        print("XAUS SPOT ERROR:", e)

        with state_lock:
            state["last_error"] = str(e)
            state["xaus"] = "ERROR"

        raise


def get_xaus_intraday(hours=48):
    """
    XAUS records gold every ~2 minutes.
    We use this first-party series and resample it internally.

    Returned points:
        {t: timestamp, p: price}
    """

    try:
        hours = max(1, min(int(hours), 48))

        r = requests.get(
            XAUS_INTRADAY_URL,
            params={
                "symbol": "xau",
                "hours": hours,
                "fresh": int(time.time())
            },
            timeout=REQUEST_TIMEOUT
        )

        if not r.ok:
            raise RuntimeError(
                f"XAUS intraday HTTP {r.status_code}: {r.text[:300]}"
            )

        data = r.json()

        points = data.get("points", [])

        if not points:
            raise RuntimeError("XAUS intraday returned no points")

        rows = []

        for p in points:
            try:
                timestamp = p.get("t")
                price = p.get("p")

                if timestamp is None or price is None:
                    continue

                rows.append({
                    "time": pd.to_datetime(
                        timestamp,
                        utc=True
                    ),
                    "price": float(price)
                })

            except Exception:
                continue

        if len(rows) < 20:
            raise RuntimeError(
                f"Not enough XAUS intraday points: {len(rows)}"
            )

        df = pd.DataFrame(rows)

        df = df.sort_values("time")
        df = df.drop_duplicates("time")

        df = df.set_index("time")

        df["price"] = pd.to_numeric(
            df["price"],
            errors="coerce"
        )

        df = df.dropna()

        if len(df) < 20:
            raise RuntimeError(
                "Not enough valid XAUS price points"
            )

        with state_lock:
            state["xaus"] = "READY"

        return df

    except Exception as e:
        print("XAUS INTRADAY ERROR:", e)

        with state_lock:
            state["last_error"] = str(e)
            state["xaus"] = "ERROR"

        raise


# ============================================================
# BUILD OHLC
# ============================================================

def build_ohlc(price_df, timeframe):
    """
    Convert XAUS 2-minute sampled prices into OHLC bars.

    Supported:
        M5
        M15
        H1
        H4
    """

    rule_map = {
        "M5": "5min",
        "M15": "15min",
        "H1": "1h",
        "H4": "4h"
    }

    if timeframe not in rule_map:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    rule = rule_map[timeframe]

    ohlc = price_df["price"].resample(
        rule,
        label="right",
        closed="right"
    ).agg(
        open="first",
        high="max",
        low="min",
        close="last"
    )

    ohlc = ohlc.dropna()

    if len(ohlc) == 0:
        raise RuntimeError(
            f"No candles generated for {timeframe}"
        )

    return ohlc


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
    prev_close = df["close"].shift(1)

    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()

    true_range = pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)

    return true_range.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()


# ============================================================
# STRUCTURE
# ============================================================

def market_structure(df):
    """
    Simple deterministic structure model.

    Uses:
      - EMA50
      - EMA200
      - recent swing high/low
      - RSI
      - momentum
    """

    if len(df) < 60:
        return {
            "bias": "MIXED",
            "score": 50,
            "bos": "NONE",
            "rsi": 50,
            "ema50": None,
            "ema200": None
        }

    work = df.copy()

    work["ema50"] = ema(work["close"], 50)
    work["ema200"] = ema(work["close"], 200)
    work["rsi"] = rsi(work["close"], 14)

    latest = work.iloc[-1]

    close = float(latest["close"])
    ema50_v
