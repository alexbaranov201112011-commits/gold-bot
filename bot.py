import os
import json
import time
import threading
from datetime import datetime, timezone, timedelta

import requests
import numpy as np
import pandas as pd

from flask import Flask, request, jsonify


# ============================================================
# 🥇 GOLD SMART V5.9.2
# XAU/USD SMART SMC SIGNAL ENGINE
#
# DATA SOURCE:
#   XAUS.COM
#
# TIMEFRAMES:
#   H4 → H1 → M15 → M5
#
# TELEGRAM:
#   WEBHOOK /telegram
#
# AUTO TRADING:
#   OFF
#
# RISK:
#   1%
#
# CLOSED CANDLES:
#   ON
#
# ============================================================


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://gold-bot-q8la.onrender.com"
).strip().rstrip("/")

DEPOSIT = float(os.getenv("DEPOSIT", "800"))

RISK_PERCENT = 1.0

MIN_SCORE_FULL = 70
MIN_SCORE_EARLY = 60

COOLDOWN_MINUTES = 15

POLL_SECONDS = 60

SESSION_START = 12
SESSION_END = 23

STATE_FILE = "gold_smart_state.json"


# ============================================================
# XAUS API
# ============================================================

XAUS_SPOT_URL = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY_URL = "https://xaus.com/api/v1/intraday"


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# GLOBAL STATE
# ============================================================

state_lock = threading.Lock()

state = {
    "last_signal": None,
    "last_signal_time": None,
    "virtual_trades": [],
    "signals_total": 0,
    "signals_full": 0,
    "signals_early": 0,
    "wins": 0,
    "losses": 0,
    "last_price": None,
    "last_update": None,
    "last_error": None,
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

        if isinstance(saved, dict):
            state.update(saved)

        print("🟢 State loaded")

    except Exception as e:
        print(f"⚠️ State load error: {e}")


def save_state():
    try:
        with state_lock:
            snapshot = dict(state)

        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

    except Exception as e:
        print(f"⚠️ State save error: {e}")


load_state()


# ============================================================
# TIME
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def now_kz():
    # Aktau / Kazakhstan UTC+5
    return datetime.now(timezone.utc) + timedelta(hours=5)


def in_trading_session():
    hour = now_kz().hour
    return SESSION_START <= hour < SESSION_END


# ============================================================
# TELEGRAM
# ============================================================

def telegram_api(method, payload=None, timeout=15):
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN is missing")
        return None

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    try:
        r = requests.post(
            url,
            json=payload or {},
            timeout=timeout
        )

        print(
            f"[TELEGRAM API] {method} "
            f"HTTP {r.status_code} "
            f"{r.text[:500]}"
        )

        try:
            return r.json()
        except Exception:
            return {
                "ok": False,
                "status_code": r.status_code,
                "text": r.text
            }

    except Exception as e:
        print(f"❌ Telegram API error: {e}")
        return None


def send_telegram(text_message, chat_id=None):
    target = str(chat_id or CHAT_ID).strip()

    if not target:
        print("⚠️ No Telegram chat_id")
        return False

    result = telegram_api(
        "sendMessage",
        {
            "chat_id": target,
            "text": text_message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15
    )

    return bool(result and result.get("ok"))


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

def webhook_url():
    return f"{RENDER_EXTERNAL_URL}/telegram"


def set_webhook():
    if not BOT_TOKEN:
        print("⚠️ BOT_TOKEN not configured")
        return

    url = webhook_url()

    print(f"🔗 Setting Telegram webhook:")
    print(url)

    result = telegram_api(
        "setWebhook",
        {
            "url": url,
            "drop_pending_updates": False,
            "allowed_updates": ["message"],
        },
        timeout=20
    )

    if result and result.get("ok"):
        print("🟢 Telegram webhook READY")
    else:
        print("🔴 Telegram webhook FAILED")


def get_webhook_info():
    return telegram_api(
        "getWebhookInfo",
        {},
        timeout=15
    )


# ============================================================
# XAUS SPOT
# ============================================================

def get_xaus_spot():
    """
    Получает реальный XAU/USD spot.

    Основное поле:
        spot_usd_oz

    Fallback:
        xau.price
    """

    try:
        params = {
            "currency": "USD",
            "unit": "oz",
            "compact": "1",
            "fresh": str(int(time.time())),
        }

        r = requests.get(
            XAUS_SPOT_URL,
            params=params,
            timeout=15
        )

        r.raise_for_status()

        data = r.json()

        price = data.get("spot_usd_oz")

        if price is None:
            xau = data.get("xau")

            if isinstance(xau, dict):
                price = xau.get("price")

        if price is None:
            raise ValueError(
                f"XAUS price not found: {data}"
            )

        price = float(price)

        if not np.isfinite(price) or price <= 0:
            raise ValueError(
                f"Invalid XAUS price: {price}"
            )

        data_state = data.get("data_state", {})

        status = data_state.get(
            "status",
            "unknown"
        )

        state_age = data_state.get(
            "age_seconds"
        )

        print(
            f"🟢 XAUS SPOT "
            f"{price:.2f} "
            f"state={status} "
            f"age={state_age}"
        )

        with state_lock:
            state["last_price"] = price
            state["last_update"] = now_utc().isoformat()
            state["last_error"] = None

        return price, data

    except Exception as e:

        print(f"❌ XAUS spot error: {e}")

        with state_lock:
            state["last_error"] = str(e)

        return None, None


# ============================================================
# XAUS INTRADAY
# ============================================================

def get_xaus_intraday(hours=48):
    """
    XAUS first-party intraday series.

    Returns:
        DataFrame
        columns:
            open
            high
            low
            close
    """

    try:
        hours = max(1, min(int(hours), 48))

        params = {
            "symbol": "xau",
            "hours": hours,
            "fresh": str(int(time.time())),
        }

        r = requests.get(
            XAUS_INTRADAY_URL,
            params=params,
            timeout=20
        )

        r.raise_for_status()

        data = r.json()

        points = data.get("points")

        if not isinstance(points, list):
            raise ValueError(
                f"XAUS intraday points not found: {data}"
            )

        rows = []

        for point in points:

            if not isinstance(point, dict):
                continue

            t = point.get("t")
            p = point.get("p")

            if t is None or p is None:
                continue

            try:
                price = float(p)

                if not np.isfinite(price):
                    continue

                timestamp = pd.to_datetime(
                    t,
                    utc=True
                )

                rows.append(
                    {
                        "time": timestamp,
                        "price": price,
                    }
                )

            except Exception:
                continue

        if len(rows) < 20:
            raise ValueError(
                f"Not enough intraday points: {len(rows)}"
            )

        df = pd.DataFrame(rows)

        df = df.sort_values("time")

        df = df.drop_duplicates(
            subset=["time"]
        )

        df = df.set_index("time")

        df["price"] = pd.to_numeric(
            df["price"],
            errors="coerce"
        )

        df = df.dropna()

        print(
            f"🟢 XAUS INTRADAY "
            f"points={len(df)} "
            f"from={df.index.min()} "
            f"to={df.index.max()}"
        )

        return df

    except Exception as e:

        print(
            f"❌ XAUS intraday error: {e}"
        )

        with state_lock:
            state["last_error"] = str(e)

        return None


# ============================================================
# BUILD CANDLES
# ============================================================

def build_candles(price_df, timeframe):
    """
    Resample XAUS 2-minute series into candles.

    IMPORTANT:
    Only closed candles are used later.
    """

    if price_df is None or price_df.empty:
        return None

    rule_map = {
        "M5": "5min",
        "M15": "15min",
        "H1": "1h",
        "H4": "4h",
    }

    rule = rule_map.get(timeframe)

    if not rule:
        return None

    ohlc = price_df["price"].resample(
        rule,
        label="right",
        closed="right"
    ).ohlc()

    ohlc = ohlc.dropna()

    if ohlc.empty:
        return None

    ohlc.columns = [
        "open",
        "high",
        "low",
        "close"
    ]

    # Remove current unfinished candle.
    current_time = pd.Timestamp.now(
        tz="UTC"
    )

    if len(ohlc) > 0:

        last_index = ohlc.index[-1]

        if last_index > current_time:
            ohlc = ohlc.iloc[:-1]

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

    rs = avg_gain / avg_loss.replace(
        0,
        np.nan
    )

    result = 100 - (
        100 / (1 + rs)
    )

    return result.fillna(50)


def atr(df, period=14):

    high = df["high"]

    low = df["low"]

    close = df["close"]

    prev_close = close.shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1
    ).max(axis=1)

    return tr.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()


# ============================================================
# STRUCTURE ANALYSIS
# ============================================================

def timeframe_analysis(df):

    if df is None or len(df) < 30:

        return {
            "trend": "UNKNOWN",
            "score": 50,
            "ema50": None,
            "ema200": None,
            "rsi": 50,
            "bos": "NONE",
            "choch": "NONE",
        }

    close = df["close"]

    ema50 = ema(
        close,
        50
    )

    ema200 = ema(
        close,
        200
    )

    current = float(close.iloc[-1])

    e50 = float(ema50.iloc[-1])

    e200 = float(
        ema200.iloc[-1]
    )

    rsi_value = float(
        rsi(close).iloc[-1]
    )

    score_buy = 50
    score_sell = 50

    # EMA trend

    if current > e50:
        score_buy += 10
    else:
        score_sell += 10

    if e50 > e200:
        score_buy += 10
    else:
        score_sell += 10

    # RSI

    if 52 <= rsi_value <= 68:
        score_buy += 10

    if 32 <= rsi_value <= 48:
        score_sell += 10

    # Recent structure

    recent_high = float(
        df["high"].iloc[-6:-1].max()
    )

    recent_low = float(
        df["low"].iloc[-6:-1].min()
    )

    bos = "NONE"
    choch = "NONE"

    if current > recent_high:
        bos = "BULLISH"
        score_buy += 15

    elif current < recent_low:
        bos = "BEARISH"
        score_sell += 15

    # Simple trend classification

    if score_buy >= score_sell + 10:

        trend = "BULLISH"

    elif score_sell >= score_buy + 10:

        trend = "BEARISH"

    else:

        trend = "MIXED"

    score = max(
        score_buy,
        score_sell
    )

    score = min(
        100,
        int(score)
    )

    return {
        "trend": trend,
        "score": score,
        "ema50": e50,
        "ema200": e200,
        "rsi": rsi_value,
        "bos": bos,
        "choch": choch,
    }


# ============================================================
# M5 SMC
# ============================================================

def m5_smc(df):

    result = {
        "liquidity": "NONE",
        "bos": "NONE",
        "choch": "NONE",
        "fvg": "NONE",
        "displacement": "NONE",
        "momentum": "NEUTRAL",
        "premium_discount": "EQUILIBRIUM",
    }

    if df is None or len(df) < 30:
        return result

    last = df.iloc[-1]

    prev = df.iloc[-2]

    close = float(last["close"])

    high = float(last["high"])

    low = float(last["low"])

    prev_high = float(
        df["high"].iloc[-6:-1].max()
    )

    prev_low = float(
        df["low"].iloc[-6:-1].min()
    )

    # --------------------------------------------------------
    # Liquidity sweep
    # --------------------------------------------------------

    if (
        high > prev_high
        and close < prev_high
    ):
        result["liquidity"] = "BUY-SIDE SWEPT"

    elif (
        low < prev_low
        and close > prev_low
    ):
        result["liquidity"] = "SELL-SIDE SWEPT"

    # --------------------------------------------------------
    # BOS
    # --------------------------------------------------------

    if close > prev_high:
        result["bos"] = "BULLISH"

    elif close < prev_low:
        result["bos"] = "BEARISH"

    # --------------------------------------------------------
    # CHoCH approximation
    # --------------------------------------------------------

    old_mid = (
        df["high"].iloc[-10:-5].max()
        + df["low"].iloc[-10:-5].min()
    ) / 2

    new_mid = (
        df["high"].iloc[-5:].max()
        + df["low"].iloc[-5:].min()
    ) / 2

    if new_mid > old_mid and close < old_mid:
        result["choch"] = "BEARISH"

    elif new_mid < old_mid and close > old_mid:
        result["choch"] = "BULLISH"

    # --------------------------------------------------------
    # FVG
    # --------------------------------------------------------

    c1 = df.iloc[-3]
    c3 = df.iloc[-1]

    if float(c3["low"]) > float(c1["high"]):
        result["fvg"] = "BULLISH"

    elif float(c3["high"]) < float(c1["low"]):
        result["fvg"] = "BEARISH"

    # --------------------------------------------------------
    # Displacement
    # --------------------------------------------------------

    ranges = (
        df["high"] - df["low"]
    ).iloc[-20:]

    avg_range = float(
        ranges.mean()
    )

    current_range = (
        high - low
    )

    if avg_range > 0:

        if (
            current_range
            > avg_range * 1.5
        ):

            if close > float(
                last["open"]
            ):
                result[
                    "displacement"
                ] = "BULLISH"

            else:
                result[
                    "displacement"
                ] = "BEARISH"

    # --------------------------------------------------------
    # Momentum
    # --------------------------------------------------------

    rsi_value = float(
        rsi(df["close"]).iloc[-1]
    )

    if rsi_value >= 55:
        result["momentum"] = "BULLISH"

    elif rsi_value <= 45:
        result["momentum"] = "BEARISH"

    # --------------------------------------------------------
    # Premium / Discount
    # --------------------------------------------------------

    swing_high = float(
        df["high"].iloc[-20:].max()
    )

    swing_low = float(
        df["low"].iloc[-20:].min()
    )

    midpoint = (
        swing_high + swing_low
    ) / 2

    if close > midpoint:
        result[
            "premium_discount"
        ] = "PREMIUM"

    elif close < midpoint:
        result[
            "premium_discount"
        ] = "DISCOUNT"

    return result


# ============================================================
# SCORE ENGINE
# ============================================================

def calculate_signal(
    h4_info,
    h1_info,
    m15_info,
    m5_info,
    smc,
    price,
):
    """
    SMART CONFLUENCE ENGINE.

    Full signal:
        score >= 70

    Early signal:
        score >= 60

    Otherwise:
        WAIT
    """

    buy = 0
    sell = 0

    # ========================================================
    # H4
    # ========================================================

    if h4_info["trend"] == "BULLISH":
        buy += 20

    elif h4_info["trend"] == "BEARISH":
        sell += 20

    # ========================================================
    # H1
    # ========================================================

    if h1_info["trend"] == "BULLISH":
        buy += 20

    elif h1_info["trend"] == "BEARISH":
        sell += 20

    # ========================================================
    # M15
    # ========================================================

    if m15_info["trend"] == "BULLISH":
        buy += 15

    elif m15_info["trend"] == "BEARISH":
        sell += 15

    # ========================================================
    # M5
    # ========================================================

    if m5_info["trend"] == "BULLISH":
        buy += 15

    elif m5_info["trend"] == "BEARISH":
        sell += 15

    # ========================================================
    # LIQUIDITY
    # ========================================================

    if smc["liquidity"] == "SELL-SIDE SWEPT":
        buy += 10

    elif smc["liquidity"] == "BUY-SIDE SWEPT":
        sell += 10

    # ========================================================
    # BOS
    # ========================================================

    if smc["bos"] == "BULLISH":
        buy += 10

    elif smc["bos"] == "BEARISH":
        sell += 10

    # ========================================================
    # FVG
    # ========================================================

    if smc["fvg"] == "BULLISH":
        buy += 5

    elif smc["fvg"] == "BEARISH":
        sell += 5

    # ========================================================
    # DISPLACEMENT
    # ========================================================

    if smc["displacement"] == "BULLISH":
        buy += 5

    elif smc["displacement"] == "BEARISH":
        sell += 5

    # ========================================================
    # MOMENTUM
    # ========================================================

    if smc["momentum"] == "BULLISH":
        buy += 5

    elif smc["momentum"] == "BEARISH":
        sell += 5

    # ========================================================
    # Direction
    # ========================================================

    if buy > sell:

        direction = "BUY"
        raw_score = buy

    elif sell > buy:

        direction = "SELL"
        raw_score = sell

    else:

        direction = "WAIT"
        raw_score = 50

    score = min(
        100,
        int(raw_score)
    )

    # ========================================================
    # Signal type
    # ========================================================

    if score >= MIN_SCORE_FULL:

        signal_type = "FULL"

    elif score >= MIN_SCORE_EARLY:

        signal_type = "EARLY"

    else:

        signal_type = "WAIT"

    # ========================================================
    # Prevent weak higher-TF contradiction
    # ========================================================

    if direction == "BUY":

        if (
            h4_info["trend"] == "BEARISH"
            and h1_info["trend"] == "BEARISH"
        ):
            signal_type = "WAIT"

    elif direction == "SELL":

        if (
            h4_info["trend"] == "BULLISH"
            and h1_info["trend"] == "BULLISH"
        ):
            signal_type = "WAIT"

    # ========================================================
    # Levels
    # ========================================================

    if direction == "BUY":

        entry = price

        recent_low = min(
            m5_info.get("recent_low", price),
            price
        )

        sl = entry - 4.0

        if recent_low < entry:
            sl = min(
                sl,
                recent_low - 0.5
            )

        risk = max(
            1.0,
            entry - sl
        )

        tp1 = entry + risk * 1.5
        tp2 = entry + risk * 3.0

    elif direction == "SELL":

        entry = price

        sl = entry + 4.0

        risk = max(
            1.0,
            sl - entry
        )

        tp1 = entry - risk * 1.5
        tp2 = entry - risk * 3.0

    else:

        entry = price
        sl = price
        tp1 = price
        tp2 = price

    return {
        "direction": direction,
        "score": score,
        "signal_type": signal_type,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "buy_score": buy,
        "sell_score": sell,
    }


# ============================================================
# LOT CALCULATION
# ============================================================

def calculate_lot(entry, sl):

    risk_money = (
        DEPOSIT
        * RISK_PERCENT
        / 100
    )

    distance = abs(
        entry - sl
    )

    if distance <= 0:
        return 0.01

    # Approximate XAUUSD sizing.
    # 1 lot ≈ $100 per $1 move.

    raw_lot = (
        risk_money
        / (distance * 100)
    )

    raw_lot = max(
        0.01,
        raw_lot
    )

    raw_lot = min(
        0.02,
        raw_lot
    )

    return round(
        raw_lot,
        2
    )


# ============================================================
# COOLDOWN
# ============================================================

def cooldown_active():

    with state_lock:
        last_time = state.get(
            "last_signal_time"
        )

    if not last_time:
        return False

    try:

        last_dt = datetime.fromisoformat(
            last_time
        )

        diff = (
            now_utc() - last_dt
        ).total_seconds()

        return (
            diff
            < COOLDOWN_MINUTES * 60
        )

    except Exception:

        return False


# ============================================================
# ANALYSIS
# ============================================================

def run_analysis():

    price, spot_data = get_xaus_spot()

    if price is None:
        return {
            "ok": False,
            "error": "XAUS spot unavailable"
        }

    intraday = get_xaus_intraday(
        hours=48
    )

    if intraday is None:
        return {
            "ok": False,
            "error": "XAUS intraday unavailable"
        }

    candles = {}

    for tf in [
        "M5",
        "M15",
        "H1",
        "H4",
    ]:

        df = build_candles(
            intraday,
            tf
        )

        if df is None or len(df) < 20:

            print(
                f"⚠️ Not enough {tf} candles"
            )

            return {
                "ok": False,
                "error":
                    f"Not enough {tf} candles"
            }

        candles[tf] = df

        print(
            f"🕯 {tf}: "
            f"{len(df)} closed candles"
        )

    h4 = timeframe_analysis(
        candles["H4"]
    )

    h1 = timeframe_analysis(
        candles["H1"]
    )

    m15 = timeframe_analysis(
        candles["M15"]
    )

    m5 = timeframe_analysis(
        candles["M5"]
    )

    # Add recent low/high for levels

    m5["recent_low"] = float(
        candles["M5"]["low"]
        .iloc[-6:-1]
        .min()
    )

    m5["recent_high"] = float(
        candles["M5"]["high"]
        .iloc[-6:-1]
        .max()
    )

    smc = m5_smc(
        candles["M5"]
    )

    signal = calculate_signal(
        h4,
        h1,
        m15,
        m5,
        smc,
        price,
    )

    lot = calculate_lot(
        signal["entry"],
        signal["sl"]
    )

    signal["lot"] = lot

    signal["price"] = price

    return {
        "ok": True,
        "price": price,
        "h4": h4,
        "h1": h1,
        "m15": m15,
        "m5": m5,
        "smc": smc,
        "signal": signal,
    }


# ============================================================
# FORMAT SIGNAL
# ============================================================

def format_signal(result):

    if not result or not result.get("ok"):

        error = (
            result.get("error")
            if result
            else "Unknown error"
        )

        return (
            "🥇 <b>GOLD SMART V5.9.2</b>\n\n"
            "🔴 SIGNAL ENGINE ERROR\n\n"
            f"❌ {error}"
        )

    price = result["price"]

    h4 = result["h4"]
    h1 = result["h1"]
    m15 = result["m15"]
    m5 = result["m5"]

    smc = result["smc"]

    sig = result["signal"]

    direction = sig["direction"]

    score = sig["score"]

    signal_type = sig["signal_type"]

    if direction == "BUY":

        emoji = "🟢"

    elif direction == "SELL":

        emoji = "🔴"

    else:

        emoji = "⚪"

    text = (
        "🥇 <b>GOLD SMART V5.9.2</b>\n\n"
        f"{emoji} <b>SIGNAL: {direction}</b>\n"
        f"📊 Score: <b>{score}/100</b>\n"
        f"🏷 Type: <b>{signal_type}</b>\n\n"
        f"💰 XAUUSD: <b>{price:.2f}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"📊 H4: {h4['trend']} "
        f"({h4['score']}/100)\n"
        f"📊 H1: {h1['trend']} "
        f"({h1['score']}/100)\n"
        f"📊 M15: {m15['trend']} "
        f"({m15['score']}/100)\n"
        f"📊 M5: {m5['trend']} "
        f"({m5['score']}/100)\n\n"
        "🧩 <b>M5 SMC</b>\n"
        f"💧 Liquidity: {smc['liquidity']}\n"
        f"🔨 BOS: {smc['bos']}\n"
        f"🔄 CHoCH: {smc['choch']}\n"
        f"🧩 FVG: {smc['fvg']}\n"
        f"💥 Displacement: {smc['displacement']}\n"
        f"📈 Momentum: {smc['momentum']}\n"
        f"⚖️ PD: {smc['premium_discount']}\n\n"
    )

    if direction != "WAIT":

        text += (
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"🎯 ENTRY: <b>{sig['entry']:.2f}</b>\n"
            f"🛑 SL: <b>{sig['sl']:.2f}</b>\n"
            f"🥇 TP1: <b>{sig['tp1']:.2f}</b>\n"
            f"🥈 TP2: <b>{sig['tp2']:.2f}</b>\n"
            f"📐 RR: <b>1:3</b>\n"
            f"📦 Lot: <b>{sig['lot']:.2f}</b>\n\n"
        )

    else:

        text += (
            "━━━━━━━━━━━━━━━━━━\n\n"
            "⏳ <b>WAIT</b>\n"
            "Нет достаточного подтверждения "
            "для входа.\n\n"
        )

    text += (
        "🛡 AUTO TRADING: <b>OFF</b>\n"
        f"📉 Risk: <b>{RISK_PERCENT:.1f}%</b>\n"
        "🕯 Closed candles: <b>ON</b>\n"
        "📡 Source: <b>XAUS</b>"
    )

    return text


# ============================================================
# STATUS
# ============================================================

def format_status():

    price, _ = get_xaus_spot()

    with state_lock:
        error = state.get(
            "last_error"
        )

        total = state.get(
            "signals_total",
            0
        )

        full = state.get(
            "signals_full",
            0
        )

        early = state.get(
            "signals_early",
            0
        )

        wins = state.get(
            "wins",
            0
        )

        losses = state.get(
            "losses",
            0
        )

    webhook = get_webhook_info()

    webhook_ok = False

    if webhook and webhook.get("ok"):

        result = webhook.get(
            "result",
            {}
        )

        webhook_url_value = result.get(
            "url",
            ""
        )

        webhook_ok = (
            webhook_url_value
            == webhook_url()
        )

    price_text = (
        f"{price:.2f}"
        if price
        else "N/A"
    )

    return (
        "🥇 <b>GOLD SMART V5.9.2</b>\n\n"
        "🟢 Engine: <b>READY</b>\n"
        f"📡 XAUS: <b>{'READY' if price else 'ERROR'}</b>\n"
        f"💰 XAUUSD: <b>{price_text}</b>\n\n"
        f"📨 Telegram webhook: "
        f"<b>{'READY' if webhook_ok else 'CHECK'}</b>\n\n"
        "🛡 AUTO TRADING: <b>OFF</b>\n"
        f"📉 Risk: <b>{RISK_PERCENT:.1f}%</b>\n"
        f"💵 Deposit: <b>${DEPOSIT:.0f}</b>\n\n"
        f"📊 Signals: <b>{total}</b>\n"
        f"🟢 Full: <b>{full}</b>\n"
        f"🟡 Early: <b>{early}</b>\n"
        f"✅ Wins: <b>{wins}</b>\n"
        f"❌ Losses: <b>{losses}</b>\n\n"
        f"🕐 KZ time: "
        f"<b>{now_kz().strftime('%H:%M:%S')}</b>\n"
        f"📅 Session 12:00–23:00\n\n"
        f"⚠️ Error: "
        f"<code>{error or 'NONE'}</code>"
    )


# ============================================================
# HELP
# ============================================================

def help_text():

    return (
        "🥇 <b>GOLD SMART V5.9.2</b>\n\n"
        "📌 Команды:\n\n"
        "/start — запуск бота\n"
        "/status — состояние системы\n"
        "/signal — полный анализ XAU/USD\n"
        "/test — тест Telegram\n"
        "/stats — статистика сигналов\n"
        "/help — список команд\n\n"
        "🛡 AUTO TRADING: OFF\n"
        "📡 DATA: XAUS\n"
        "🕯 CLOSED CANDLES: ON\n"
        "📊 H4 → H1 → M15 → M5"
    )


# ============================================================
# STATS
# ============================================================

def stats_text():

    with state_lock:

        total = int(
            state.get(
                "signals_total",
                0
            )
        )

        full = int(
            state.get(
                "signals_full",
                0
            )
        )

        early = int(
            state.get(
                "signals_early",
                0
            )
        )

        wins = int(
            state.get(
                "wins",
                0
            )
        )

        losses = int(
            state.get(
                "losses",
                0
            )
        )

    closed = wins + losses

    if closed > 0:

        winrate = (
            wins / closed
        ) * 100

    else:

        winrate = 0

    return (
        "📊 <b>GOLD SMART V5.9.2 STATS</b>\n\n"
        f"📡 Total signals: <b>{total}</b>\n"
        f"🟢 Full: <b>{full}</b>\n"
        f"🟡 Early: <b>{early}</b>\n\n"
        f"✅ Wins: <b>{wins}</b>\n"
        f"❌ Losses: <b>{losses}</b>\n"
        f"🎯 Winrate: <b>{winrate:.1f}%</b>\n\n"
        "⚠️ Virtual statistics only.\n"
        "AUTO TRADING: OFF"
    )


# ============================================================
# PROCESS SIGNAL
# ============================================================

def process_signal(
    chat_id,
    force=False
):

    if (
        not force
        and cooldown_active()
    ):

        send_telegram(
            "⏳ <b>COOLDOWN</b>\n\n"
            f"Следующий анализ через "
            f"{COOLDOWN_MINUTES} минут.",
            chat_id
        )

        return

    send_telegram(
        "🔎 <b>GOLD SMART</b>\n\n"
        "Анализирую XAU/USD...\n"
        "H4 → H1 → M15 → M5",
        chat_id
    )

    result = run_analysis()

    if not result.get("ok"):

        send_telegram(
            format_signal(result),
            chat_id
        )

        return

    signal = result["signal"]

    direction = signal["direction"]

    signal_type = signal["signal_type"]

    score = signal["score"]

    # ========================================================
    # WAIT
    # ========================================================

    if signal_type == "WAIT":

        send_telegram(
            format_signal(result),
            chat_id
        )

        return

    # ========================================================
    # SESSION
    # ========================================================

    if not in_trading_session():

        send_telegram(
            format_signal(result)
            + "\n\n"
            "⚠️ <b>Вне торговой сессии 12:00–23:00 KZ.</b>\n"
            "Сигнал показан только для анализа.",
            chat_id
        )

        return

    # ========================================================
    # Register signal
    # ========================================================

    with state_lock:

        state["last_signal"] = direction

        state["last_signal_time"] = (
            now_utc().isoformat()
        )

        state["signals_total"] = int(
            state.get(
                "signals_total",
                0
            )
        ) + 1

        if signal_type == "FULL":

            state["signals_full"] = int(
                state.get(
                    "signals_full",
                    0
                )
            ) + 1

        elif signal_type == "EARLY":

            state["signals_early"] = int(
                state.get(
                    "signals_early",
                    0
                )
            ) + 1

    save_state()

    send_telegram(
        format_signal(result),
        chat_id
    )


# ============================================================
# TELEGRAM COMMAND PROCESSOR
# ============================================================

def process_telegram_update(update):

    try:

        message = update.get(
            "message"
        )

        if not message:
            return

        chat = message.get(
            "chat",
            {}
        )

        chat_id = chat.get(
            "id"
        )

        if not chat_id:
            return

        text_value = (
            message.get(
                "text",
                ""
            )
            .strip()
        )

        if not text_value:
            return

        command = (
            text_value
            .split()[0]
            .lower()
            .split("@")[0]
        )

        print(
            f"📨 TELEGRAM "
            f"chat_id={chat_id} "
            f"command={command}"
        )

        # ----------------------------------------------------
        # /start
        # ----------------------------------------------------

        if command == "/start":

            send_telegram(
                "🥇 <b>GOLD SMART V5.9.2</b>\n\n"
                "🟢 Бот запущен.\n\n"
                "📡 XAUS: READY\n"
                "🛡 AUTO TRADING: OFF\n"
                "🕯 Closed candles: ON\n\n"
                "Напиши /help",
                chat_id
            )

            return

        # ----------------------------------------------------
        # /help
        # ----------------------------------------------------

        if command == "/help":

            send_telegram(
                help_text(),
                chat_id
            )

            return

        # ----------------------------------------------------
        # /test
        # ----------------------------------------------------

        if command == "/test":

            send_telegram(
                "🟢 <b>TEST OK</b>\n\n"
                "Telegram → Render → Bot\n"
                "Webhook работает.",
                chat_id
            )

            return

        # ----------------------------------------------------
        # /status
        # ----------------------------------------------------

        if command == "/status":

            send_telegram(
                format_status(),
                chat_id
            )

            return

        # ----------------------------------------------------
        # /stats
        # ----------------------------------------------------

        if command == "/stats":

            send_telegram(
                stats_text(),
                chat_id
            )

            return

        # ----------------------------------------------------
        # /signal
        # ----------------------------------------------------

        if command == "/signal":

            threading.Thread(
                target=process_signal,
                args=(chat_id,),
                kwargs={
                    "force": True
                },
                daemon=True
            ).start()

            return

        # ----------------------------------------------------
        # Unknown
        # ----------------------------------------------------

        send_telegram(
            "❓ Неизвестная команда.\n\n"
            "Напиши /help",
            chat_id
        )

    except Exception as e:

        print(
            f"❌ Telegram update error: {e}"
        )


# ============================================================
# FLASK ROUTES
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():

    return jsonify(
        {
            "app": "GOLD SMART V5.9.2",
            "status": "READY",
            "auto_trading": False,
            "risk_percent": RISK_PERCENT,
            "data_source": "XAUS",
            "webhook": "/telegram",
        }
    )


@app.route
