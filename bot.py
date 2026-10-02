import os
import time
import threading
import traceback
from datetime import datetime, timezone
import requests
import pandas as pd
import numpy as np
import yfinance as yf
from flask import Flask, request
# =========================================================
# 🥇 GOLD SMART V5.6
#
# XAUUSD SIGNAL ENGINE
# Yahoo Finance / GC=F
# Telegram Webhook
# Render
#
# AUTO TRADING = OFF
# RISK = 1%
# CLOSED CANDLES = ON
#
# IMPORTANT:
# Telegram webhook NEVER waits for market analysis.
# =========================================================
# =========================================================
# CONFIG
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://gold-bot-q8la.onrender.com"
).strip().rstrip("/")
YF_SYMBOL = "GC=F"
SOURCE_NAME = "Yahoo Finance / GC=F"
RISK_PERCENT = 1.0
DEFAULT_DEPOSIT = 800.0
MIN_LOT = 0.01
MAX_LOT = 0.02
MIN_SCORE = 70
EARLY_SCORE = 60
TARGET_RR = 3.0
POLL_SECONDS = 60
COOLDOWN_MIN = 15
# Yahoo protection
YAHOO_ERROR_COOLDOWN = 300
YAHOO_TIMEOUT = 15
# Telegram protection
TELEGRAM_TIMEOUT = 10
CACHE_TTL = {
    "5m": 60,
    "15m": 180,
    "1h": 600,
    "4h": 1800
}
# =========================================================
# FLASK
# =========================================================
app = Flask(__name__)
# =========================================================
# GLOBAL STATE
# =========================================================
ENGINE_STARTED = False
WEBHOOK_READY = False
_last_signal = None
_last_signal_time = 0
_yahoo_block_until = 0
_cache = {}
_stats = {
    "signals": 0,
    "buy": 0,
    "sell": 0,
    "early_buy": 0,
    "early_sell": 0,
    "wait": 0,
    "errors": 0
}
# Prevent simultaneous market-analysis requests
_analysis_lock = threading.Lock()
# =========================================================
# LOG
# =========================================================
def log(message):
    now = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    print(f"[{now}] {message}", flush=True)
# =========================================================
# TELEGRAM SEND
# =========================================================
def send_telegram_message(text, chat_id=None):
    target_chat = chat_id or CHAT_ID
    if not BOT_TOKEN:
        log("❌ BOT_TOKEN is missing")
        return False
    if not target_chat:
        log("❌ TELEGRAM_CHAT_ID is missing")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(
            url,
            json={
                "chat_id": target_chat,
                "text": text
            },
            timeout=TELEGRAM_TIMEOUT
        )
        if response.status_code != 200:
            log(
                f"❌ Telegram HTTP {response.status_code} | "
                f"{response.text[:300]}"
            )
            return False
        data = response.json()
        if not data.get("ok"):
            log(f"❌ Telegram API error | {data}")
            return False
        log("📤 Telegram message SENT")
        return True
    except Exception as e:
        log(f"❌ Telegram send exception | {e}")
        return False
# =========================================================
# FAST COMMANDS
# =========================================================
def telegram_start():
    return (
        "🥇 GOLD SMART V5.6\n\n"
        "🟢 Bot: ONLINE\n"
        "🟢 Telegram: READY\n"
        f"🟢 Webhook: {'READY' if WEBHOOK_READY else 'CHECKING'}\n"
        f"🟢 Engine: {'RUNNING' if ENGINE_STARTED else 'STARTING'}\n"
        "🟢 Source: Yahoo Finance / GC=F\n"
        "🕯 Closed candles: ON\n"
        "🛡 Risk: 1%\n"
        "🚫 Auto Trading: OFF\n\n"
        "Используй /help"
    )
def telegram_help():
    return (
        "🥇 GOLD SMART V5.6\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "📌 КОМАНДЫ:\n\n"
        "/start — информация\n"
        "/status — состояние рынка\n"
        "/signal — текущий сигнал\n"
        "/test — проверка системы\n"
        "/stats — статистика\n"
        "/help — помощь\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🥇 XAUUSD / GC=F\n"
        "🕯 Closed candles: ON\n"
        "🛡 Risk: 1%\n"
        "🚫 Auto Trading: OFF"
    )
def telegram_test():
    return (
        "🧪 GOLD SMART V5.6 TEST\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "🟢 Telegram: READY\n"
        f"🟢 Webhook: {'READY' if WEBHOOK_READY else 'NOT READY'}\n"
        f"🟢 Engine: {'READY' if ENGINE_STARTED else 'STARTING'}\n"
        "🟢 Command handler: READY\n"
        "🟢 Yahoo module: CONFIGURED\n"
        "🟢 Closed candles: ON\n"
        "🛡 Risk: 1%\n"
        "🚫 Auto Trading: OFF\n\n"
        "✅ Telegram command test OK."
    )
def telegram_stats():
    return (
        "📊 GOLD SMART V5.6 — STATISTICS\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        f"📌 TOTAL CHECKS: {_stats['signals']}\n\n"
        f"🟢 BUY: {_stats['buy']}\n"
        f"🔴 SELL: {_stats['sell']}\n"
        f"🟡 EARLY BUY: {_stats['early_buy']}\n"
        f"🟠 EARLY SELL: {_stats['early_sell']}\n"
        f"⚪ WAIT: {_stats['wait']}\n"
        f"❌ ERRORS: {_stats['errors']}\n\n"
        f"📡 SOURCE: {SOURCE_NAME}\n"
        "🛡 RISK: 1%\n"
        "🚫 AUTO TRADING: OFF"
    )
# =========================================================
# DATA NORMALIZATION
# =========================================================
def normalize_dataframe(df):
    if df is None or df.empty:
        return None
    try:
        df = df.copy()
        if isinstance(df.columns, pd.MultiIndex):
            new_columns = []
            for col in df.columns:
                if isinstance(col, tuple):
                    new_columns.append(str(col[0]).strip())
                else:
                    new_columns.append(str(col).strip())
            df.columns = new_columns
        rename_map = {}
        for col in df.columns:
            clean = str(col).strip().lower()
            if clean == "open":
                rename_map[col] = "Open"
            elif clean == "high":
                rename_map[col] = "High"
            elif clean == "low":
                rename_map[col] = "Low"
            elif clean == "close":
                rename_map[col] = "Close"
            elif clean == "volume":
                rename_map[col] = "Volume"
        df.rename(columns=rename_map, inplace=True)
        required = [
            "Open",
            "High",
            "Low",
            "Close"
        ]
        if not all(c in df.columns for c in required):
            log(
                "❌ Invalid Yahoo columns | "
                f"{list(df.columns)}"
            )
            return None
        for col in required:
            df[col] = pd.to_numeric(
                df[col],
                errors="coerce"
            )
        if "Volume" not in df.columns:
            df["Volume"] = 0
        df.dropna(
            subset=required,
            inplace=True
        )
        return df
    except Exception as e:
        log(f"❌ normalize_dataframe error | {e}")
        return None
# =========================================================
# CLOSED CANDLES
# =========================================================
def remove_incomplete_candle(df):
    if df is None or df.empty:
        return None
    try:
        df = df.copy()
        df.index = pd.to_datetime(
            df.index,
            utc=True
        )
        if len(df) > 1:
            df = df.iloc[:-1]
        return df
    except Exception as e:
        log(f"❌ Candle processing error | {e}")
        return df
# =========================================================
# YAHOO DOWNLOAD
# =========================================================
def yahoo_download(period, interval):
    global _yahoo_block_until
    now = time.time()
    if now < _yahoo_block_until:
        remaining = int(
            _yahoo_block_until - now
        )
        log(
            f"⏳ Yahoo cooldown active | "
            f"{remaining}s"
        )
        return None
    try:
        log(
            f"📡 Yahoo request | "
            f"{YF_SYMBOL} | {interval} | {period}"
        )
        started = time.time()
        df = yf.download(
            tickers=YF_SYMBOL,
            period=period,
            interval=interval,
            auto_adjust=False,
            progress=False,
            threads=False
        )
        elapsed = round(
            time.time() - started,
            2
        )
        log(
            f"📡 Yahoo response | "
            f"{interval} | {elapsed}s"
        )
        if df is None or df.empty:
            log("⚠️ Yahoo returned EMPTY")
            return None
        return normalize_dataframe(df)
    except Exception as e:
        error_text = str(e)
        if (
            "429" in error_text
            or "rate" in error_text.lower()
            or "crumb" in error_text.lower()
        ):
            _yahoo_block_until = (
                time.time()
                + YAHOO_ERROR_COOLDOWN
            )
            log(
                "⚠️ Yahoo rate-limit/crumb error | "
                f"cooldown {YAHOO_ERROR_COOLDOWN}s"
            )
        else:
            log(
                f"❌ Yahoo exception | "
                f"{error_text}"
            )
        return None
# =========================================================
# CACHE
# =========================================================
def get_cached_data(interval):
    item = _cache.get(interval)
    if not item:
        return None
    timestamp, df = item
    if df is None or df.empty:
        return None
    return df.copy()
def get_data(interval):
    now = time.time()
    ttl = CACHE_TTL.get(
        interval,
        60
    )
    cached = _cache.get(interval)
    # -----------------------------------------------------
    # Fresh cache
    # -----------------------------------------------------
    if cached:
        timestamp, df = cached
        if (
            df is not None
            and not df.empty
            and now - timestamp < ttl
        ):
            log(
                f"🟢 CACHE HIT | {interval}"
            )
            return df.copy()
    # -----------------------------------------------------
    # If Yahoo is currently blocked:
    # immediately use old cache.
    # -----------------------------------------------------
    if now < _yahoo_block_until:
        if cached:
            log(
                f"🟡 CACHE FALLBACK | "
                f"{interval}"
            )
            return cached[1].copy()
        log(
            f"⚠️ No cache available | "
            f"{interval}"
        )
        return None
    # -----------------------------------------------------
    # Download
    # -----------------------------------------------------
    if interval == "5m":
        period = "5d"
    elif interval == "15m":
        period = "10d"
    elif interval == "1h":
        period = "30d"
    else:
        period = "30d"
    df = yahoo_download(
        period,
        interval
    )
    if df is None:
        if cached:
            log(
                f"🟡 OLD CACHE FALLBACK | "
                f"{interval}"
            )
            return cached[1].copy()
        return None
    df = remove_incomplete_candle(df)
    if df is not None and not df.empty:
        _cache[interval] = (
            time.time(),
            df.copy()
        )
    return df
# =========================================================
# 4H
# =========================================================
def get_4h_data():
    cached = _cache.get("4h")
    if cached:
        timestamp, df = cached
        if (
            df is not None
            and not df.empty
            and time.time() - timestamp
            < CACHE_TTL["4h"]
        ):
            log("🟢 CACHE HIT | 4h")
            return df.copy()
    df1h = get_data("1h")
    if df1h is None or df1h.empty:
        return None
    try:
        df4h = df1h.resample("4h").agg({
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum"
        }).dropna()
        df4h = remove_incomplete_candle(
            df4h
        )
        if df4h is not None and not df4h.empty:
            _cache["4h"] = (
                time.time(),
                df4h.copy()
            )
        return df4h
    except Exception as e:
        log(
            f"❌ 4H resample error | {e}"
        )
        return None
# =========================================================
# INDICATORS
# =========================================================
def ema(series, period):
    return series.ewm(
        span=period,
        adjust=False
    ).mean()
def calculate_rsi(
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
    rsi = 100 - (
        100 / (1 + rs)
    )
    return rsi.fillna(50)
def get_trend(df):
    if df is None or len(df) < 200:
        return "MIXED"
    e50 = ema(
        df["Close"],
        50
    ).iloc[-1]
    e200 = ema(
        df["Close"],
        200
    ).iloc[-1]
    price = df["Close"].iloc[-1]
    if price > e50 > e200:
        return "BULLISH"
    if price < e50 < e200:
        return "BEARISH"
    return "MIXED"
def get_bos(
    df,
    lookback=12
):
    if df is None or len(df) < lookback + 3:
        return "NONE"
    current_close = df["Close"].iloc[-1]
    previous_high = (
        df["High"]
        .iloc[-lookback-1:-1]
        .max()
    )
    previous_low = (
        df["Low"]
        .iloc[-lookback-1:-1]
        .min()
    )
    if current_close > previous_high:
        return "BULLISH BOS"
    if current_close < previous_low:
        return "BEARISH BOS"
    return "NONE"
def get_liquidity_sweep(
    df,
    lookback=10
):
    if df is None or len(df) < lookback + 3:
        return "NONE"
    recent = df.iloc[
        -lookback-1:-1
    ]
    high = recent["High"].max()
    low = recent["Low"].min()
    current_high = df["High"].iloc[-1]
    current_low = df["Low"].iloc[-1]
    current_close = df["Close"].iloc[-1]
    if (
        current_high > high
        and current_close < high
    ):
        return "BSL SWEEP"
    if (
        current_low < low
        and current_close > low
    ):
        return "SSL SWEEP"
    return "NONE"
def get_fvg(df):
    if df is None or len(df) < 5:
        return "NONE"
    c1 = df.iloc[-3]
    c3 = df.iloc[-1]
    if c3["Low"] > c1["High"]:
        return "BULLISH FVG"
    if c3["High"] < c1["Low"]:
        return "BEARISH FVG"
    return "NONE"
def get_displacement(df):
    if df is None or len(df) < 25:
        return "NONE"
    body = abs(
        df["Close"].iloc[-1]
        - df["Open"].iloc[-1]
    )
    ranges = (
        df["High"].iloc[-21:-1]
        - df["Low"].iloc[-21:-1]
    )
    mean_range = ranges.mean()
    if mean_range <= 0:
        return "NONE"
    if body >= mean_range * 1.5:
        if (
            df["Close"].iloc[-1]
            > df["Open"].iloc[-1]
        ):
            return "BULLISH"
        return "BEARISH"
    return "NONE"
def get_momentum(df):
    if df is None or len(df) < 60:
        return "MIXED"
    e20 = ema(
        df["Close"],
        20
    ).iloc[-1]
    e50 = ema(
        df["Close"],
        50
    ).iloc[-1]
    if e20 > e50:
        return "BULLISH"
    if e20 < e50:
        return "BEARISH"
    return "MIXED"
def get_zone(df):
    if df is None or len(df) < 50:
        return "UNKNOWN"
    recent = df.iloc[-50:]
    high = recent["High"].max()
    low = recent["Low"].min()
    midpoint = (
        high + low
    ) / 2
    price = df["Close"].iloc[-1]
    if price > midpoint:
        return "PREMIUM"
    return "DISCOUNT"
# =========================================================
# TIMEFRAME ANALYSIS
# =========================================================
def analyze_timeframe(df):
    if df is None or df.empty:
        return {
            "trend": "UNKNOWN",
            "bos": "NONE",
            "liquidity": "NONE",
            "fvg": "NONE",
            "displacement": "NONE",
            "momentum": "MIXED",
            "zone": "UNKNOWN",
            "rsi": 50
        }
    rsi = calculate_rsi(
        df["Close"]
    ).iloc[-1]
    return {
        "trend": get_trend(df),
        "bos": get_bos(df),
        "liquidity": get_liquidity_sweep(df),
        "fvg": get_fvg(df),
        "displacement": get_displacement(df),
        "momentum": get_momentum(df),
        "zone": get_zone(df),
        "rsi": float(rsi)
    }
# =========================================================
# MARKET ANALYSIS
# =========================================================
def analyze_market():
    if not _analysis_lock.acquire(
        blocking=False
    ):
        raise RuntimeError(
            "Another market analysis is already running"
        )
    try:
        started = time.time()
        log("🔎 ANALYSIS START")
        df4h = get_4h_data()
        df1h = get_data("1h")
        df15 = get_data("15m")
        df5 = get_data("5m")
        if any(
            x is None or x.empty
            for x in [
                df4h,
                df1h,
                df15,
                df5
            ]
        ):
            raise RuntimeError(
                "Market data unavailable"
            )
        h4 = analyze_timeframe(
            df4h
        )
        h1 = analyze_timeframe(
            df1h
        )
        m15 = analyze_timeframe(
            df15
        )
        m5 = analyze_timeframe(
            df5
        )
        price = float(
            df5["Close"].iloc[-1]
        )
        buy = 0
        sell = 0
        # H4 = 20
        if h4["trend"] == "BULLISH":
            buy += 20
        elif h4["trend"] == "BEARISH":
            sell += 20
        # H1 = 15
        if h1["trend"] == "BULLISH":
            buy += 15
        elif h1["trend"] == "BEARISH":
            sell += 15
        # M15 = 15
        if m15["trend"] == "BULLISH":
            buy += 15
        elif m15["trend"] == "BEARISH":
            sell += 15
        # M5 trend = 10
        if m5["trend"] == "BULLISH":
            buy += 10
        elif m5["trend"] == "BEARISH":
            sell += 10
        # Liquidity = 10
        if m5["liquidity"] == "SSL SWEEP":
            buy += 10
        elif m5["liquidity"] == "BSL SWEEP":
            sell += 10
        # BOS = 15
        if m5["bos"] == "BULLISH BOS":
            buy += 15
        elif m5["bos"] == "BEARISH BOS":
            sell += 15
        # FVG = 5
        if m5["fvg"] == "BULLISH FVG":
            buy += 5
        elif m5["fvg"] == "BEARISH FVG":
            sell += 5
        # Displacement = 10
        if m5["displacement"] == "BULLISH":
            buy += 10
        elif m5["displacement"] == "BEARISH":
            sell += 10
        # Momentum = 5
        if m5["momentum"] == "BULLISH":
            buy += 5
        elif m5["momentum"] == "BEARISH":
            sell += 5
        # RSI = 5
        rsi = m5["rsi"]
        if 50 <= rsi <= 70:
            buy += 5
        elif 30 <= rsi < 50:
            sell += 5
        total = max(
            buy + sell,
            1
        )
        up_pct = round(
            buy / total * 100
        )
        down_pct = 100 - up_pct
        if (
            buy >= MIN_SCORE
            and buy > sell
        ):
            signal = "BUY"
        elif (
            sell >= MIN_SCORE
            and sell > buy
        ):
            signal = "SELL"
        elif (
            buy >= EARLY_SCORE
            and buy > sell
        ):
            signal = "EARLY BUY"
        elif (
            sell >= EARLY_SCORE
            and sell > buy
        ):
            signal = "EARLY SELL"
        else:
            signal = "WAIT"
        elapsed = round(
            time.time() - started,
            2
        )
        log(
            f"🔎 ANALYSIS DONE | "
            f"{signal} | "
            f"{price:.2f} | "
            f"BUY {buy} | SELL {sell} | "
            f"{elapsed}s"
        )
        return {
            "signal": signal,
            "price": price,
            "h4": h4,
            "h1": h1,
            "m15": m15,
            "m5": m5,
            "buy": buy,
            "sell": sell,
            "up_pct": up_pct,
            "down_pct": down_pct
        }
    except Exception:
        _stats["errors"] += 1
        log(
            "❌ ANALYSIS ERROR\n"
            + traceback.format_exc()
        )
        raise
    finally:
        _analysis_lock.release()
# =========================================================
# SETUP
# =========================================================
def make_setup(result):
    signal = result["signal"]
    if signal not in [
        "BUY",
        "SELL",
        "EARLY BUY",
        "EARLY SELL"
    ]:
        return None
    entry = result["price"]
    sl_distance = 5.0
    if signal in [
        "BUY",
        "EARLY BUY"
    ]:
        direction = "BUY"
        sl = entry - sl_distance
        tp = entry + (
            sl_distance * TARGET_RR
        )
    else:
        direction = "SELL"
        sl = entry + sl_distance
        tp = entry - (
            sl_distance * TARGET_RR
        )
    risk_money = (
        DEFAULT_DEPOSIT
        * RISK_PERCENT
        / 100
    )
    lot = risk_money / (
        sl_distance * 100
    )
    lot = max(
        MIN_LOT,
        min(MAX_LOT, lot)
    )
    return {
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "lot": lot,
        "rr": TARGET_RR,
        "risk_money": risk_money
    }
# =========================================================
# FORMAT SIGNAL
# =========================================================
def format_signal(result):
    signal = result["signal"]
    if "BUY" in signal:
        icon = "🟢"
    elif "SELL" in signal:
        icon = "🔴"
    else:
        icon = "⚪"
    h4 = result["h4"]
    h1 = result["h1"]
    m15 = result["m15"]
    m5 = result["m5"]
    setup = make_setup(
        result
    )
    lines = [
        "🥇 GOLD SMART V5.6",
        "",
        f"{icon} SIGNAL: {signal}",
        "",
        f"💰 XAUUSD: {result['price']:.2f}",
        "",
        f"📊 H4: {h4['trend']}",
        f"📊 H1: {h1['trend']}",
        f"📊 M15: {m15['trend']}",
        f"📊 M5: {m5['trend']}",
        "",
        f"💧 M5 Liquidity: {m5['liquidity']}",
        f"🔨 M5 BOS: {m5['bos']}",
        f"🧩 M5 FVG: {m5['fvg']}",
        f"💥 M5 Displacement: {m5['displacement']}",
        f"📈 Momentum: {m5['momentum']}",
        f"📐 Zone: {m5['zone']}",
        f"📉 RSI: {m5['rsi']:.1f}",
        "",
        f"📈 UP: {result['up_pct']}%",
        f"📉 DOWN: {result['down_pct']}%",
        "",
        f"🎯 BUY SCORE: {result['buy']}",
        f"🎯 SELL SCORE: {result['sell']}"
    ]
    if setup:
        lines.extend([
            "",
            "━━━━━━━━━━━━━━━━━━",
            "📌 SETUP",
            "",
            f"📍 Direction: {setup['direction']}",
            f"🎯 Entry: {setup['entry']:.2f}",
            f"🛑 SL: {setup['sl']:.2f}",
            f"💰 TP: {setup['tp']:.2f}",
            f"📦 Lot: {setup['lot']:.2f}",
            f"⚖️ RR: 1:{setup['rr']:.0f}",
            f"🛡 Risk: {RISK_PERCENT:.1f}%"
        ])
    lines.extend([
        "",
        "━━━━━━━━━━━━━━━━━━",
        "🕯 Closed candles: ON",
        f"📡 Source: {SOURCE_NAME}",
        "🚫 AUTO TRADING: OFF",
        "ℹ️ Signal is informational."
    ])
    return "\n".join(lines)
# =========================================================
# ENGINE SIGNAL
# =========================================================
def process_signal(result):
    global _last_signal
    global _last_signal_time
    signal = result["signal"]
    _stats["signals"] += 1
    if signal == "BUY":
        _stats["buy"] += 1
    elif signal == "SELL":
        _stats["sell"] += 1
    elif signal == "EARLY BUY":
        _stats["early_buy"] += 1
    elif signal == "EARLY SELL":
        _stats["early_sell"] += 1
    else:
        _stats["wait"] += 1
        log(
            f"📊 WAIT | "
            f"XAUUSD {result['price']:.2f} | "
            f"BUY {result['buy']} | "
            f"SELL {result['sell']}"
        )
        return
    now = time.time()
    if (
        signal == _last_signal
        and now - _last_signal_time
        < COOLDOWN_MIN * 60
    ):
        log(
            f"⏳ Signal cooldown | {signal}"
        )
        return
    _last_signal = signal
    _last_signal_time = now
    log(
        f"🚨 NEW SIGNAL | {signal}"
    )
    send_telegram_message(
        format_signal(result)
    )
# =========================================================
# ASYNC MARKET COMMAND
# =========================================================
def market_command_worker(
    command,
    chat_id
):
    try:
        log(
            f"🔎 COMMAND ANALYSIS START | "
            f"{command}"
        )
        result = analyze_market()
        send_telegram_message(
            format_signal(result),
            chat_id
        )
        log(
            f"✅ COMMAND ANALYSIS DONE | "
            f"{command}"
        )
    except Exception as e:
        log(
            f"❌ COMMAND ANALYSIS ERROR | "
            f"{command} | {e}"
        )
        send_telegram_message(
            "⚠️ GOLD SMART V5.6\n\n"
            "❌ Не удалось получить данные XAUUSD.\n\n"
            "Возможная причина:\n"
            "Yahoo Finance временно ограничил запросы "
            "(HTTP 429 / Crumb rate limit).\n\n"
            "Попробуй /status через несколько минут.",
            chat_id
        )
# =========================================================
# TELEGRAM HANDLER
# =========================================================
def handle_telegram_update(update):
    try:
        message = update.get(
            "message"
        )
        if not message:
            log(
                "ℹ️ Telegram update without message"
            )
            return
        chat = message.get(
            "chat",
            {}
        )
        chat_id = chat.get("id")
        raw_text = message.get(
            "text",
            ""
        )
        if not raw_text:
            return
        text_value = raw_text.strip()
        command = (
            text_value
            .split("@", 1)[0]
            .split(" ", 1)[0]
            .lower()
        )
        log(
            f"📩 Telegram command: {command}"
        )
        # =================================================
        # FAST COMMANDS
        # =================================================
        if command == "/start":
            send_telegram_message(
                telegram_start(),
                chat_id
            )
            return
        if command == "/help":
            send_telegram_message(
                telegram_help(),
                chat_id
            )
            return
        if command == "/test":
            send_telegram_message(
                telegram_test(),
                chat_id
            )
            return
        if command == "/stats":
            send_telegram_message(
                telegram_stats(),
                chat_id
            )
            return
        # =================================================
        # MARKET COMMANDS
        # =================================================
        if command in [
            "/status",
            "/signal"
        ]:
            # Immediate response.
            send_telegram_message(
                "⏳ GOLD SMART V5.6\n"
                "Получаю данные XAUUSD...",
                chat_id
            )
            # IMPORTANT:
            # Do NOT wait for Yahoo inside webhook.
            thread = threading.Thread(
                target=market_command_worker,
                args=(
                    command,
                    chat_id
                ),
                daemon=True
            )
            thread.start()
            log(
                f"🧵 Market worker started | "
                f"{command}"
            )
            return
        # =================================================
        # UNKNOWN
        # =================================================
        send_telegram_message(
            "⚠️ Неизвестная команда.\n\n"
            "Используй /help",
            chat_id
        )
    except Exception:
        log(
            "❌ Telegram handler exception\n"
            + traceback.format_exc()
        )
# =========================================================
# WEB ROUTES
# =========================================================
@app.route("/", methods=["GET"])
def home():
    return (
        "🥇 GOLD SMART V5.6 ONLINE",
        200
    )
@app.route("/health", methods=["GET"])
def health():
    return (
        "🥇 GOLD SMART V5.6 HEALTHY",
        200
    )
@app.route(
    "/telegram",
    methods=["POST"]
)
def telegram_webhook():
    log(
        "📩 Telegram webhook received"
    )
    try:
        update = request.get_json(
            silent=True
        )
        if update:
            handle_telegram_update(
                update
            )
        else:
            log(
                "⚠️ Empty Telegram update"
            )
    except Exception:
        log(
            "❌ Webhook exception\n"
            + traceback.format_exc()
        )
    # ALWAYS 200.
    return "OK", 200
# =========================================================
# SET WEBHOOK
# =========================================================
def setup_telegram_webhook():
    global WEBHOOK_READY
    if not BOT_TOKEN:
        log(
            "❌ BOT_TOKEN missing"
        )
        return False
    webhook_url = (
        RENDER_EXTERNAL_URL
        + "/telegram"
    )
    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/setWebhook"
    )
    try:
        response = requests.post(
            url,
            json={
                "url": webhook_url,
                "allowed_updates": [
                    "message"
                ],
                "drop_pending_updates": False
            },
            timeout=20
        )
        data = response.json()
        if (
            response.status_code == 200
            and data.get("ok")
        ):
            WEBHOOK_READY = True
            log(
                "🟢 Telegram webhook READY | "
                f"{webhook_url}"
            )
            return True
        log(
            f"❌ Webhook setup failed | "
            f"{data}"
        )
        return False
    except Exception as e:
        log(
            f"❌ Webhook setup exception | "
            f"{e}"
        )
        return False
# =========================================================
# VERIFY WEBHOOK
# =========================================================
def verify_telegram_webhook():
    if not BOT_TOKEN:
        return False
    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/getWebhookInfo"
    )
    expected = (
        RENDER_EXTERNAL_URL
        + "/telegram"
    )
    try:
        response = requests.get(
            url,
            timeout=20
        )
        data = response.json()
        if not data.get("ok"):
            log(
                f"❌ Webhook verify error | "
                f"{data}"
            )
            return False
        info = data.get(
            "result",
            {}
        )
        actual_url = info.get(
            "url",
            ""
        )
        pending = info.get(
            "pending_update_count",
            0
        )
        last_error = info.get(
            "last_error_message",
            ""
        )
        log(
            f"📡 Webhook URL: {actual_url}"
        )
        log(
            f"📬 Pending updates: {pending}"
        )
        if last_error:
            log(
                f"⚠️ Telegram last error: "
                f"{last_error}"
            )
        if actual_url == expected:
            log(
                "🟢 Telegram webhook VERIFIED"
            )
            return True
        log(
            "⚠️ Webhook URL MISMATCH | "
            f"expected={expected}"
        )
        return False
    except Exception as e:
        log(
            f"❌ Webhook verify exception | "
            f"{e}"
        )
        return False
# =========================================================
# ENGINE LOOP
# =========================================================
def engine_loop():
    global ENGINE_STARTED
    ENGINE_STARTED = True
    log(
        "🟢 GOLD SMART V5.6 ENGINE STARTED"
    )
    while True:
        try:
            result = analyze_market()
            process_signal(
                result
            )
        except Exception as e:
            log(
                f"⚠️ Engine cycle error | "
                f"{e}"
            )
        time.sleep(
            POLL_SECONDS
        )
# =========================================================
# STARTUP
# =========================================================
def startup():
    log(
        "🥇 GOLD SMART V5.6 STARTING..."
    )
    try:
        setup_telegram_webhook()
        verify_telegram_webhook()
    except Exception:
        log(
            "❌ Telegram startup exception\n"
            + traceback.format_exc()
        )
    thread = threading.Thread(
        target=engine_loop,
        daemon=True
    )
    thread.start()
    log(
        "🟢 GOLD SMART V5.6 ONLINE"
    )
# =========================================================
# START
# =========================================================
startup()
