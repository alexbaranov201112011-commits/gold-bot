import os
import json
import time
import threading
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from flask import Flask, request, jsonify
# =========================================================
# 🥇 GOLD SMART V5.3
#
# XAU/USD
#
# H4 → H1 → M15 → M5
# M5 = MAIN ENTRY TRIGGER
#
# SMC:
# Liquidity Sweep
# BOS
# FVG
# Displacement
# Momentum
# Premium / Discount
# RSI
#
# RISK = 1%
# LOT = 0.01 - 0.02
# AUTO TRADING = OFF
#
# V5.3:
# - SMART CACHE
# - 429 PROTECTION
# - API COOLDOWN
# - NO DUPLICATE REQUESTS
# - M5 CLOSED-CANDLE ENGINE
# =========================================================
# =========================================================
# SETTINGS
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TWELVE_KEY = os.getenv("TWELVE_DATA_API_KEY", "")
WEBHOOK_URL = (
    os.getenv("WEBHOOK_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or "https://gold-bot-q8la.onrender.com"
)
SYMBOL = "XAU/USD"
RISK_PERCENT = 1.0
DEFAULT_DEPOSIT = 800.0
MIN_LOT = 0.01
MAX_LOT = 0.02
MIN_SCORE = 70
EARLY_SCORE = 60
TARGET_RR = 3.0
MIN_RR = 2.0
POLL_SECONDS = 60
COOLDOWN_MIN = 15
STATS_FILE = "gold_stats.json"
# =========================================================
# CACHE SETTINGS
# =========================================================
# How long cached data can be reused.
CACHE_TTL = {
    "5min": 55,
    "15min": 180,
    "1h": 600,
    "4h": 1800,
    "quote": 30,
}
# After API 429 we stop making requests for this period.
API_COOLDOWN_SECONDS = 900
# Prevent duplicate error messages.
ERROR_MESSAGE_COOLDOWN = 900
# =========================================================
# FLASK
# =========================================================
app = Flask(__name__)
# =========================================================
# STATE
# =========================================================
state = {
    "last_m5": None,
    "last_signal_key": None,
    "last_signal_at": 0,
    "running": False,
    "api_blocked_until": 0,
    "last_api_error": None,
    "last_api_error_at": 0,
    "cache": {},
    "cache_lock": threading.Lock(),
}
lock = threading.Lock()
# =========================================================
# TELEGRAM
# =========================================================
def tg(text):
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text
            },
            timeout=15
        )
    except Exception as e:
        print("TELEGRAM ERROR:", e)
# =========================================================
# API ERROR REPORT
# =========================================================
def report_api_error(message):
    now = time.time()
    # Print always
    print("Twelve Data error:", message)
    # Avoid Telegram spam
    if (
        now
        - state["last_api_error_at"]
        < ERROR_MESSAGE_COOLDOWN
    ):
        return
    state["last_api_error"] = str(message)
    state["last_api_error_at"] = now
    tg(
        "⚠️ GOLD SMART V5.3\n\n"
        "Twelve Data временно недоступен.\n\n"
        f"{message}\n\n"
        "🛡 API запросы поставлены на паузу.\n"
        "🤖 AUTO TRADING: OFF"
    )
# =========================================================
# TWELVE DATA
# =========================================================
def td(endpoint, **params):
    if not TWELVE_KEY:
        return None
    now = time.time()
    # -----------------------------------------------------
    # Global API protection
    # -----------------------------------------------------
    if now < state["api_blocked_until"]:
        return None
    params.update({
        "symbol": SYMBOL,
        "apikey": TWELVE_KEY,
        "timezone": "UTC",
    })
    try:
        r = requests.get(
            f"https://api.twelvedata.com/{endpoint}",
            params=params,
            timeout=20
        )
        # -------------------------------------------------
        # HTTP 429
        # -------------------------------------------------
        if r.status_code == 429:
            try:
                data = r.json()
            except Exception:
                data = {}
            message = data.get(
                "message",
                "HTTP 429 Too Many Requests"
            )
            # Stop all requests temporarily
            state["api_blocked_until"] = (
                now
                + API_COOLDOWN_SECONDS
            )
            report_api_error(
                f"HTTP 429: {message}"
            )
            return None
        # -------------------------------------------------
        # Other HTTP errors
        # -------------------------------------------------
        if r.status_code >= 400:
            try:
                data = r.json()
            except Exception:
                data = {}
            message = data.get(
                "message",
                f"HTTP {r.status_code}"
            )
            report_api_error(
                f"HTTP {r.status_code}: {message}"
            )
            return None
        data = r.json()
        # -------------------------------------------------
        # Twelve Data API error
        # -------------------------------------------------
        if data.get("status") == "error":
            code = data.get(
                "code",
                "UNKNOWN"
            )
            message = data.get(
                "message",
                "Unknown API error"
            )
            if str(code) == "429":
                state["api_blocked_until"] = (
                    now
                    + API_COOLDOWN_SECONDS
                )
            report_api_error(
                f"API {code}: {message}"
            )
            return None
        if "values" not in data and endpoint == "time_series":
            report_api_error(
                "time_series returned no values"
            )
            return None
        return data
    except requests.exceptions.Timeout:
        report_api_error(
            "Request timeout"
        )
        return None
    except Exception as e:
        report_api_error(
            f"Request exception: {e}"
        )
        return None
# =========================================================
# CACHED TWELVE DATA
# =========================================================
def cached_td(endpoint, cache_key, ttl, **params):
    now = time.time()
    # -----------------------------------------------------
    # Return cache if fresh
    # -----------------------------------------------------
    with state["cache_lock"]:
        cached = state["cache"].get(
            cache_key
        )
        if cached:
            age = now - cached["time"]
            if age < ttl:
                return cached["data"]
    # -----------------------------------------------------
    # API request
    # -----------------------------------------------------
    data = td(
        endpoint,
        **params
    )
    # -----------------------------------------------------
    # If API failed, use old cache if available
    # -----------------------------------------------------
    if data is None:
        with state["cache_lock"]:
            cached = state["cache"].get(
                cache_key
            )
            if cached:
                print(
                    "Using stale cache:",
                    cache_key
                )
                return cached["data"]
        return None
    # -----------------------------------------------------
    # Save cache
    # -----------------------------------------------------
    with state["cache_lock"]:
        state["cache"][cache_key] = {
            "time": now,
            "data": data
        }
    return data
# =========================================================
# CANDLES
# =========================================================
def candles(interval, size=250):
    ttl = CACHE_TTL.get(
        interval,
        60
    )
    cache_key = (
        f"candles:{interval}:{size}"
    )
    data = cached_td(
        "time_series",
        cache_key,
        ttl,
        interval=interval,
        outputsize=size
    )
    if not data:
        return None
    try:
        df = pd.DataFrame(
            data["values"]
        )
        df["datetime"] = pd.to_datetime(
            df["datetime"],
            utc=True
        )
        for col in [
            "open",
            "high",
            "low",
            "close"
        ]:
            df[col] = pd.to_numeric(
                df[col],
                errors="coerce"
            )
        df = (
            df
            .dropna()
            .sort_values("datetime")
            .set_index("datetime")
        )
        return df
    except Exception as e:
        print(
            "CANDLE PARSE ERROR:",
            e
        )
        return None
# =========================================================
# CURRENT PRICE
# =========================================================
def price():
    data = cached_td(
        "quote",
        "quote",
        CACHE_TTL["quote"]
    )
    try:
        if data:
            value = (
                data.get("close")
                or data.get("price")
            )
            if value is not None:
                return float(value)
    except Exception as e:
        print(
            "PRICE ERROR:",
            e
        )
    # -----------------------------------------------------
    # Fallback: latest M5 close
    # -----------------------------------------------------
    df = candles(
        "5min",
        250
    )
    try:
        if df is not None and len(df):
            return float(
                df["close"].iloc[-1]
            )
    except Exception:
        pass
    return None
# =========================================================
# RSI
# =========================================================
def rsi(series, period=14):
    delta = series.diff()
    gain = (
        delta
        .clip(lower=0)
        .ewm(
            alpha=1 / period,
            adjust=False
        )
        .mean()
    )
    loss = (
        -delta
        .clip(upper=0)
        .ewm(
            alpha=1 / period,
            adjust=False
        )
        .mean()
    )
    rs = gain / loss.replace(
        0,
        np.nan
    )
    value = (
        100
        - 100 / (1 + rs)
    ).iloc[-1]
    return float(value)
# =========================================================
# ATR
# =========================================================
def atr(df, period=14):
    previous_close = (
        df["close"].shift()
    )
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (
                df["high"]
                - previous_close
            ).abs(),
            (
                df["low"]
                - previous_close
            ).abs(),
        ],
        axis=1
    ).max(axis=1)
    value = (
        tr
        .ewm(
            alpha=1 / period,
            adjust=False
        )
        .mean()
        .iloc[-1]
    )
    return float(value)
# =========================================================
# MARKET FEATURES
# =========================================================
def features(df):
    if df is None or len(df) < 60:
        return None
    close = df["close"]
    high = df["high"]
    low = df["low"]
    ema50 = (
        close
        .ewm(
            span=50,
            adjust=False
        )
        .mean()
        .iloc[-1]
    )
    ema200 = (
        close
        .ewm(
            span=200,
            adjust=False
        )
        .mean()
        .iloc[-1]
    )
    # -----------------------------------------------------
    # Previous liquidity range
    # -----------------------------------------------------
    previous_high = float(
        high.iloc[-21:-1].max()
    )
    previous_low = float(
        low.iloc[-21:-1].min()
    )
    last = df.iloc[-1]
    # -----------------------------------------------------
    # BOS
    # -----------------------------------------------------
    bullish_bos = (
        last.close
        > previous_high
    )
    bearish_bos = (
        last.close
        < previous_low
    )
    if bullish_bos:
        bos = "BULLISH"
    elif bearish_bos:
        bos = "BEARISH"
    else:
        bos = "NONE"
    # -----------------------------------------------------
    # Liquidity Sweep
    # -----------------------------------------------------
    bsl = (
        last.high > previous_high
        and last.close < previous_high
    )
    ssl = (
        last.low < previous_low
        and last.close > previous_low
    )
    # -----------------------------------------------------
    # FVG
    # -----------------------------------------------------
    bullish_fvg = (
        len(df) >= 3
        and float(df.low.iloc[-1])
        > float(df.high.iloc[-3])
    )
    bearish_fvg = (
        len(df) >= 3
        and float(df.high.iloc[-1])
        < float(df.low.iloc[-3])
    )
    if bullish_fvg:
        fvg = "BULLISH FVG"
    elif bearish_fvg:
        fvg = "BEARISH FVG"
    else:
        fvg = "NONE"
    # -----------------------------------------------------
    # Displacement
    # -----------------------------------------------------
    body = abs(
        last.close
        - last.open
    )
    candle_range = max(
        last.high
        - last.low,
        0.00001
    )
    average_range = (
        df["high"]
        - df["low"]
    ).rolling(20).mean().iloc[-1]
    displacement = (
        body / candle_range >= 0.65
        and body >= average_range * 1.15
    )
    if displacement:
        if last.close > last.open:
            displacement_type = "BULLISH"
        else:
            displacement_type = "BEARISH"
    else:
        displacement_type = "NONE"
    # -----------------------------------------------------
    # Momentum
    # -----------------------------------------------------
    if close.iloc[-1] > close.iloc[-4]:
        momentum = "BULLISH"
    elif close.iloc[-1] < close.iloc[-4]:
        momentum = "BEARISH"
    else:
        momentum = "NEUTRAL"
    # -----------------------------------------------------
    # Trend
    # -----------------------------------------------------
    if (
        ema50 > ema200
        and close.iloc[-1] > ema50
    ):
        trend = "BULLISH"
    elif (
        ema50 < ema200
        and close.iloc[-1] < ema50
    ):
        trend = "BEARISH"
    else:
        trend = "MIXED"
    # -----------------------------------------------------
    # Structure
    # -----------------------------------------------------
    if close.iloc[-1] > close.iloc[-6]:
        structure = "BULLISH"
    elif close.iloc[-1] < close.iloc[-6]:
        structure = "BEARISH"
    else:
        structure = "MIXED"
    # -----------------------------------------------------
    # Premium / Discount
    # -----------------------------------------------------
    midpoint = (
        previous_high
        + previous_low
    ) / 2
    if close.iloc[-1] > midpoint:
        zone = "PREMIUM"
    else:
        zone = "DISCOUNT"
    return {
        "trend": trend,
        "structure": structure,
        "rsi": rsi(close),
        "atr": atr(df),
        "bsl": bsl,
        "ssl": ssl,
        "bos": bos,
        "fvg": fvg,
        "disp": displacement_type,
        "momentum": momentum,
        "zone": zone,
        "candle":
            df.index[-1].isoformat(),
    }
# =========================================================
# SCORE
# =========================================================
def calculate_score(
    direction,
    h4,
    h1,
    m15,
    m5,
    rr
):
    wanted = (
        "BULLISH"
        if direction == "BUY"
        else "BEARISH"
    )
    score = 0
    # H4
    if h4["trend"] == wanted:
        score += 15
    # H1
    if h1["trend"] == wanted:
        score += 15
    # M15
    if m15["trend"] == wanted:
        score += 10
    if m15["structure"] == wanted:
        score += 5
    if m15["momentum"] == wanted:
        score += 5
    # Liquidity
    if direction == "BUY" and m15["ssl"]:
        score += 12
    if direction == "SELL" and m15["bsl"]:
        score += 12
    # BOS
    if m15["bos"] == wanted:
        score += 12
    # FVG
    wanted_fvg = (
        "BULLISH FVG"
        if direction == "BUY"
        else "BEARISH FVG"
    )
    if m15["fvg"] == wanted_fvg:
        score += 8
    # Displacement
    if m15["disp"] == wanted:
        score += 8
    # Zone
    if (
        direction == "BUY"
        and m15["zone"] == "DISCOUNT"
    ):
        score += 5
    if (
        direction == "SELL"
        and m15["zone"] == "PREMIUM"
    ):
        score += 5
    # M5
    if m5["bos"] == wanted:
        score += 5
    if direction == "BUY" and m5["ssl"]:
        score += 5
    if direction == "SELL" and m5["bsl"]:
        score += 5
    if m5["fvg"] == wanted_fvg:
        score += 3
    if m5["disp"] == wanted:
        score += 3
    if m5["momentum"] == wanted:
        score += 2
    # RR
    if rr >= TARGET_RR:
        score += 5
    return min(
        100,
        score
    )
# =========================================================
# TRADE SETUP
# =========================================================
def make_setup(
    direction,
    current_price,
    m5
):
    volatility = max(
        m5["atr"],
        0.8
    )
    sl_distance = max(
        volatility * 1.25,
        3.0
    )
    if direction == "BUY":
        sl = (
            current_price
            - sl_distance
        )
        tp1 = (
            current_price
            + sl_distance * 1.5
        )
        tp2 = (
            current_price
            + sl_distance * TARGET_RR
        )
    else:
        sl = (
            current_price
            + sl_distance
        )
        tp1 = (
            current_price
            - sl_distance * 1.5
        )
        tp2 = (
            current_price
            - sl_distance * TARGET_RR
        )
    # -----------------------------------------------------
    # Lot calculation
    # -----------------------------------------------------
    risk_money = (
        DEFAULT_DEPOSIT
        * RISK_PERCENT
        / 100
    )
    estimated_lot = (
        risk_money
        / (sl_distance * 100)
    )
    lot = max(
        MIN_LOT,
        min(
            MAX_LOT,
            round(
                estimated_lot,
                2
            )
        )
    )
    return {
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "rr": TARGET_RR,
        "lot": lot,
    }
# =========================================================
# ANALYSIS DATA CACHE
# =========================================================
def get_market_data():
    datasets = {}
    # -----------------------------------------------------
    # H4
    # -----------------------------------------------------
    datasets["H4"] = candles(
        "4h",
        180
    )
    # -----------------------------------------------------
    # H1
    # -----------------------------------------------------
    datasets["H1"] = candles(
        "1h",
        180
    )
    # -----------------------------------------------------
    # M15
    # -----------------------------------------------------
    datasets["M15"] = candles(
        "15min",
        180
    )
    # -----------------------------------------------------
    # M5
    # -----------------------------------------------------
    datasets["M5"] = candles(
        "5min",
        250
    )
    return datasets
# =========================================================
# ANALYSIS
# =========================================================
def analyze():
    datasets = get_market_data()
    feature_set = {
        key: features(value)
        for key, value in datasets.items()
    }
    if any(
        value is None
        for value in feature_set.values()
    ):
        return None
    current_price = price()
    if current_price is None:
        return None
    h4 = feature_set["H4"]
    h1 = feature_set["H1"]
    m15 = feature_set["M15"]
    m5 = feature_set["M5"]
    candidates = []
    # =====================================================
    # BUY / SELL
    # =====================================================
    for direction in [
        "BUY",
        "SELL"
    ]:
        setup = make_setup(
            direction,
            current_price,
            m5
        )
        score = calculate_score(
            direction,
            h4,
            h1,
            m15,
            m5,
            setup["rr"]
        )
        # RSI filter
        if direction == "BUY":
            rsi_ok = (
                m5["rsi"] < 70
            )
        else:
            rsi_ok = (
                m5["rsi"] > 30
            )
        # M5 trigger
        wanted = (
            "BULLISH"
            if direction == "BUY"
            else "BEARISH"
        )
        m5_trigger = (
            m5["bos"] == wanted
            or (
                direction == "BUY"
                and m5["ssl"]
            )
            or (
                direction == "SELL"
                and m5["bsl"]
            )
        )
        # Full
        full_signal = (
            score >= MIN_SCORE
            and rsi_ok
            and m5_trigger
        )
        # Early
        early_signal = (
            score >= EARLY_SCORE
            and rsi_ok
            and m5_trigger
        )
        signal = None
        if full_signal:
            signal = direction
        elif early_signal:
            signal = (
                "EARLY "
                + direction
            )
        if signal:
            candidates.append(
                {
                    "signal": signal,
                    "direction": direction,
                    "price": current_price,
                    "score": score,
                    "setup": setup,
                    "h4": h4,
                    "h1": h1,
                    "m15": m15,
                    "m5": m5,
                }
            )
    # =====================================================
    # BEST SIGNAL
    # =====================================================
    if candidates:
        candidates.sort(
            key=lambda x: x["score"],
            reverse=True
        )
        return candidates[0]
    # =====================================================
    # WAIT
    # =====================================================
    buy_score = calculate_score(
        "BUY",
        h4,
        h1,
        m15,
        m5,
        TARGET_RR
    )
    sell_score = calculate_score(
        "SELL",
        h4,
        h1,
        m15,
        m5,
        TARGET_RR
    )
    return {
        "signal": "WAIT",
        "price": current_price,
        "score": max(
            buy_score,
            sell_score
        ),
        "h4": h4,
        "h1": h1,
        "m15": m15,
        "m5": m5,
    }
# =========================================================
# LIQUIDITY TEXT
# =========================================================
def liquidity_text(f):
    if f["ssl"]:
        return "SSL SWEEP"
    if f["bsl"]:
        return "BSL SWEEP"
    return "NONE"
# =========================================================
# SIGNAL MESSAGE
# =========================================================
def signal_text(a):
    if a["signal"] == "WAIT":
        return (
            "🥇 GOLD SMART V5.3\n\n"
            "⚪ SIGNAL: WAIT\n\n"
            f"💰 XAUUSD: {a['price']:.2f}\n\n"
            f"📊 H4: {a['h4']['trend']}\n"
            f"📊 H1: {a['h1']['trend']}\n"
            f"📊 M15: {a['m15']['trend']}\n"
            f"📊 M5: {a['m5']['trend']}\n\n"
            f"💧 M5 Liquidity: "
            f"{liquidity_text(a['m5'])}\n"
            f"🔨 M5 BOS: "
            f"{a['m5']['bos']}\n"
            f"🧩 M5 FVG: "
            f"{a['m5']['fvg']}\n"
            f"💥 M5 Displacement: "
            f"{a['m5']['disp']}\n"
            f"📈 M5 Momentum: "
            f"{a['m5']['momentum']}\n"
            f"📐 M5 Zone: "
            f"{a['m5']['zone']}\n"
            f"RSI: "
            f"{a['m5']['rsi']:.1f}\n\n"
            f"🎯 SCORE: "
            f"{a['score']}/100"
        )
    s = a["setup"]
    emoji = (
        "🟢"
        if a["direction"] == "BUY"
        else "🔴"
    )
    return (
        "🥇 GOLD SMART V5.3\n\n"
        f"{emoji} SIGNAL: "
        f"{a['signal']}\n\n"
        f"💰 XAUUSD: "
        f"{a['price']:.2f}\n"
        f"🎯 SCORE: "
        f"{a['score']}/100\n\n"
        f"📊 H4: "
        f"{a['h4']['trend']}\n"
        f"📊 H1: "
        f"{a['h1']['trend']}\n"
        f"📊 M15: "
        f"{a['m15']['trend']}\n"
        f"📊 M5: "
        f"{a['m5']['trend']}\n\n"
        f"💧 M5 Liquidity: "
        f"{liquidity_text(a['m5'])}\n"
        f"🔨 M5 BOS: "
        f"{a['m5']['bos']}\n"
        f"🧩 M5 FVG: "
        f"{a['m5']['fvg']}\n"
        f"💥 M5 Displacement: "
        f"{a['m5']['disp']}\n"
        f"📈 M5 Momentum: "
        f"{a['m5']['momentum']}\n"
        f"📐 M5 Zone: "
        f"{a['m5']['zone']}\n"
        f"RSI: "
        f"{a['m5']['rsi']:.1f}\n\n"
        f"📍 ENTRY: "
        f"{a['price']:.2f}\n"
        f"🛑 SL: "
        f"{s['sl']:.2f}\n"
        f"🎯 TP1: "
        f"{s['tp1']:.2f}\n"
        f"🏁 TP2: "
        f"{s['tp2']:.2f}\n\n"
        f"📐 RR: "
        f"1:{s['rr']:.1f}\n"
        f"📦 LOT: "
        f"{s['lot']:.2f}\n"
        f"⚠️ RISK: "
        f"{RISK_PERCENT}%\n\n"
        "🤖 AUTO TRADING: OFF"
    )
# =========================================================
# STATISTICS
# =========================================================
def load_stats():
    try:
        with open(
            STATS_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            return json.load(f)
    except Exception:
        return {
            "WIN": 0,
            "LOSS": 0,
            "BE": 0,
            "OPEN": 0,
            "signals": 0,
        }
def save_stats(stats):
    try:
        with open(
            STATS_FILE,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                stats,
                f,
                ensure_ascii=False,
                indent=2
            )
    except Exception:
        pass
def register_signal(a):
    if a["signal"] == "WAIT":
        return
    stats = load_stats()
    stats["signals"] += 1
    stats["OPEN"] += 1
    save_stats(stats)
# =========================================================
# STATS MESSAGE
# =========================================================
def stats_text():
    stats = load_stats()
    closed = (
        stats["WIN"]
        + stats["LOSS"]
        + stats["BE"]
    )
    if closed > 0:
        winrate = (
            stats["WIN"]
            / closed
            * 100
        )
    else:
        winrate = 0
    return (
        "📊 GOLD SMART V5.3 — STATISTICS\n\n"
        f"📌 TOTAL SIGNALS: "
        f"{stats['signals']}\n\n"
        f"🟢 WIN: "
        f"{stats['WIN']}\n"
        f"🔴 LOSS: "
        f"{stats['LOSS']}\n"
        f"⚪ BE: "
        f"{stats['BE']}\n"
        f"🟡 OPEN: "
        f"{stats['OPEN']}\n\n"
        f"🎯 CLOSED: "
        f"{closed}\n\n"
        f"📈 WIN RATE: "
        f"{winrate:.1f}%"
    )
# =========================================================
# STATUS
# =========================================================
def status_text():
    blocked = (
        time.time()
        < state["api_blocked_until"]
    )
    if blocked:
        remaining = int(
            state["api_blocked_until"]
            - time.time()
        )
        api_status = (
            f"🔴 API PAUSED "
            f"({remaining}s)"
        )
    else:
        api_status = (
            "🟢 API READY"
        )
    return (
        "🥇 GOLD SMART V5.3\n\n"
        "🟢 BOT: ONLINE\n"
        f"{api_status}\n"
        "🤖 AUTO TRADING: OFF\n\n"
        f"⚠️ RISK: "
        f"{RISK_PERCENT}%\n"
        f"📦 LOT: "
        f"{MIN_LOT:.2f}–"
        f"{MAX_LOT:.2f}\n\n"
        "📊 TIMEFRAME:\n"
        "H4 → H1 → M15 → M5\n\n"
        "🎯 M5 = ENTRY TRIGGER\n"
        f"⏱ CHECK: "
        f"{POLL_SECONDS}s\n\n"
        "🛡 SMART CACHE: ON\n"
        "🛡 429 PROTECTION: ON"
    )
# =========================================================
# TELEGRAM COMMANDS
# =========================================================
def handle_command(text):
    parts = text.split()
    if not parts:
        return None
    command = (
        parts[0]
        .lower()
        .split("@")[0]
    )
    # -----------------------------------------------------
    # START / HELP
    # -----------------------------------------------------
    if command in [
        "/start",
        "/help"
    ]:
        return (
            status_text()
            + "\n\n"
            "Команды:\n"
            "/gold — анализ золота\n"
            "/stats — статистика\n"
            "/status — статус бота\n"
            "/test — тест"
        )
    # -----------------------------------------------------
    # STATUS
    # -----------------------------------------------------
    if command == "/status":
        return status_text()
    # -----------------------------------------------------
    # STATS
    # -----------------------------------------------------
    if command == "/stats":
        return stats_text()
    # -----------------------------------------------------
    # GOLD
    # -----------------------------------------------------
    if command == "/gold":
        analysis = analyze()
        if analysis is None:
            if time.time() < state[
                "api_blocked_until"
            ]:
                remaining = int(
                    state[
                        "api_blocked_until"
                    ]
                    - time.time()
                )
                return (
                    "⚠️ GOLD SMART V5.3\n\n"
                    "❌ Twelve Data временно "
                    "заблокирован из-за лимита API.\n\n"
                    f"⏳ Пауза: "
                    f"{remaining} сек.\n\n"
                    "🛡 Запросы защищены от "
                    "зацикливания."
                )
            return (
                "❌ Не удалось получить "
                "данные XAU/USD."
            )
        return signal_text(
            analysis
        )
    # -----------------------------------------------------
    # TEST
    # -----------------------------------------------------
    if command == "/test":
        api_status = (
            "PAUSED"
            if time.time()
            < state["api_blocked_until"]
            else "READY"
        )
        return (
            "✅ GOLD SMART V5.3 TEST OK\n\n"
            "🟢 Engine: READY\n"
            "🟢 Telegram: READY\n"
            "🟢 Twelve Data: "
            + (
                "CONFIGURED"
                if TWELVE_KEY
                else "MISSING"
            )
            + "\n"
            f"📡 API STATUS: "
            f"{api_status}\n"
            "🛡 CACHE: ON\n"
            "🛡 429 PROTECTION: ON\n"
            "🤖 AUTO TRADING: OFF"
        )
    return None
# =========================================================
# FLASK ROUTES
# =========================================================
@app.get("/")
def home():
    return "GOLD SMART V5.3 OK"
@app.get("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "version": "5.3",
            "running": state["running"],
            "api_blocked": (
                time.time()
                < state["api_blocked_until"]
            ),
        }
    )
@app.post("/telegram")
def telegram():
    try:
        update = (
            request
            .get_json(
                silent=True
            )
            or {}
        )
        message = update.get(
            "message",
            {}
        )
        text = message.get(
            "text",
            ""
        )
        if text:
            answer = handle_command(
                text
            )
            if answer:
                tg(answer)
        return jsonify(
            {"ok": True}
        )
    except Exception as e:
        print(
            "TELEGRAM ROUTE ERROR:",
            e
        )
        return jsonify(
            {
                "ok": False,
                "error": str(e)
            }
        ), 500
# =========================================================
# AUTO SIGNAL ENGINE
# =========================================================
def engine():
    state["running"] = True
    print(
        "🥇 GOLD SMART V5.3 ENGINE STARTED"
    )
    while True:
        try:
            # -------------------------------------------------
            # If API is blocked, DO NOT REQUEST ANYTHING.
            # -------------------------------------------------
            if time.time() < state[
                "api_blocked_until"
            ]:
                time.sleep(
                    POLL_SECONDS
                )
                continue
            # -------------------------------------------------
            # Only M5 is checked here.
            # Cache prevents unnecessary API requests.
            # -------------------------------------------------
            df = candles(
                "5min",
                250
            )
            if (
                df is not None
                and len(df) > 5
            ):
                # Use only closed M5 candle
                closed = df.iloc[:-1]
                candle_stamp = str(
                    closed.index[-1]
                )
                # -------------------------------------------------
                # New closed M5 candle?
                # -------------------------------------------------
                if (
                    candle_stamp
                    != state["last_m5"]
                ):
                    state["last_m5"] = (
                        candle_stamp
                    )
                    print(
                        "New closed M5 candle:",
                        candle_stamp
                    )
                    # -------------------------------------------------
                    # Full analysis
                    # Cached H4/H1/M15/M5 + quote
                    # -------------------------------------------------
                    analysis = analyze()
                    if (
                        analysis
                        and analysis["signal"]
                        != "WAIT"
                    ):
                        signal_key = (
                            f"{analysis['signal']}"
                            f"|{analysis['price']:.2f}"
                            f"|{candle_stamp}"
                        )
                        now = time.time()
                        cooldown_ok = (
                            now
                            - state[
                                "last_signal_at"
                            ]
                            >=
                            COOLDOWN_MIN * 60
                        )
                        if (
                            signal_key
                            != state[
                                "last_signal_key"
                            ]
                            and cooldown_ok
                        ):
                            state[
                                "last_signal_key"
                            ] = signal_key
                            state[
                                "last_signal_at"
                            ] = now
                            register_signal(
                                analysis
                            )
                            tg(
                                signal_text(
                                    analysis
                                )
                            )
                            print(
                                "SIGNAL SENT:",
                                analysis["signal"],
                                analysis["score"]
                            )
        except Exception as e:
            print(
                "ENGINE ERROR:",
                e
            )
        time.sleep(
            POLL_SECONDS
        )
# =========================================================
# STARTUP
# =========================================================
_started = False
def startup():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(
        target=engine,
        daemon=True
    ).start()
    # -----------------------------------------------------
    # Telegram webhook
    # -----------------------------------------------------
    if BOT_TOKEN:
        try:
            requests.get(
                f"https://api.telegram.org/"
                f"bot{BOT_TOKEN}/setWebhook",
                params={
                    "url":
                        WEBHOOK_URL.rstrip("/")
                        + "/telegram"
                },
                timeout=15
            )
            print(
                "Telegram webhook configured"
            )
        except Exception as e:
            print(
                "WEBHOOK ERROR:",
                e
            )
startup()
