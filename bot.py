import os
import time
import threading
import traceback
from datetime import datetime, timezone
import requests
import pandas as pd
import numpy as np
import yfinance as yf
from flask import Flask, jsonify
# ============================================================
# GOLD SMART V5.4
# Yahoo Finance / GC=F
# CLOSED CANDLE ENGINE
# AUTO TRADING = OFF
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WEBHOOK_URL = os.getenv(
    "WEBHOOK_URL",
    "https://gold-bot-q8la.onrender.com",
).strip()
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
YF_ERROR_COOLDOWN = 300
# ============================================================
# CACHE
# ============================================================
CACHE_TTL = {
    "5m": 60,
    "15m": 180,
    "1h": 600,
    "4h": 1800,
}
cache = {}
# ============================================================
# GLOBAL STATE
# ============================================================
last_yahoo_error = 0.0
last_signal_key = None
last_signal_time = 0.0
last_telegram_error = 0.0
ENGINE_STARTED = False
stats = {
    "signals": 0,
    "buy": 0,
    "sell": 0,
    "early_buy": 0,
    "early_sell": 0,
    "wait": 0,
    "errors": 0,
}
# ============================================================
# FLASK
# ============================================================
app = Flask(__name__)
# ============================================================
# LOG
# ============================================================
def log(message):
    now = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    print(
        f"[{now}] {message}",
        flush=True,
    )
# ============================================================
# TELEGRAM
# ============================================================
def telegram_send(message):
    global last_telegram_error
    if not BOT_TOKEN or not CHAT_ID:
        log("⚠️ Telegram credentials are not configured.")
        return False
    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(
            url,
            json=payload,
            timeout=15,
        )
        if response.ok:
            return True
        if time.time() - last_telegram_error > 300:
            log(
                f"❌ Telegram HTTP "
                f"{response.status_code}: "
                f"{response.text[:300]}"
            )
            last_telegram_error = time.time()
    except Exception as exc:
        if time.time() - last_telegram_error > 300:
            log(f"❌ Telegram error: {exc}")
            last_telegram_error = time.time()
    return False
# ============================================================
# YAHOO DATA NORMALIZATION
# ============================================================
def normalize_yahoo_dataframe(df):
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    # Handle MultiIndex returned by Yahoo Finance
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [
            str(col[0])
            if isinstance(col, tuple)
            else str(col)
            for col in out.columns
        ]
    rename = {}
    for col in out.columns:
        name = str(col).strip().lower()
        if name == "open":
            rename[col] = "open"
        elif name == "high":
            rename[col] = "high"
        elif name == "low":
            rename[col] = "low"
        elif name == "close":
            rename[col] = "close"
        elif name == "volume":
            rename[col] = "volume"
    out = out.rename(columns=rename)
    required = [
        "open",
        "high",
        "low",
        "close",
    ]
    if not all(
        col in out.columns
        for col in required
    ):
        return pd.DataFrame()
    for col in required:
        out[col] = pd.to_numeric(
            out[col],
            errors="coerce",
        )
    if "volume" in out.columns:
        out["volume"] = pd.to_numeric(
            out["volume"],
            errors="coerce",
        )
    out = out.dropna(
        subset=required
    )
    out = out.sort_index()
    return out
# ============================================================
# DOWNLOAD YAHOO
# ============================================================
def download_yahoo(interval):
    global last_yahoo_error
    settings = {
        "5m": (
            "5d",
            "5m",
        ),
        "15m": (
            "5d",
            "15m",
        ),
        "1h": (
            "1mo",
            "1h",
        ),
        "4h": (
            "3mo",
            "1h",
        ),
    }
    period, yf_interval = settings[interval]
    try:
        df = yf.download(
            tickers=YF_SYMBOL,
            period=period,
            interval=yf_interval,
            auto_adjust=False,
            progress=False,
            threads=False,
            prepost=False,
        )
        df = normalize_yahoo_dataframe(df)
        if df.empty:
            raise ValueError(
                "Yahoo returned empty dataframe"
            )
        # Build 4H candles from 1H data
        if interval == "4h":
            df = (
                df.resample(
                    "4h",
                    origin="start_day",
                )
                .agg(
                    {
                        "open": "first",
                        "high": "max",
                        "low": "min",
                        "close": "last",
                        "volume": "sum",
                    }
                )
                .dropna(
                    subset=[
                        "open",
                        "high",
                        "low",
                        "close",
                    ]
                )
            )
        last_yahoo_error = 0.0
        return df
    except Exception as exc:
        now = time.time()
        if (
            now - last_yahoo_error
            > YF_ERROR_COOLDOWN
        ):
            log(
                f"❌ Yahoo Finance error "
                f"({interval}): {exc}"
            )
            last_yahoo_error = now
        return pd.DataFrame()
# ============================================================
# CLOSED CANDLE FILTER
# ============================================================
def remove_incomplete_last_candle(
    df,
    interval,
):
    if df is None or df.empty:
        return df
    durations = {
        "5m": pd.Timedelta(
            minutes=5
        ),
        "15m": pd.Timedelta(
            minutes=15
        ),
        "1h": pd.Timedelta(
            hours=1
        ),
        "4h": pd.Timedelta(
            hours=4
        ),
    }
    duration = durations[interval]
    out = df.copy()
    try:
        index = pd.DatetimeIndex(
            out.index
        )
        if index.tz is None:
            index = index.tz_localize(
                "UTC"
            )
        else:
            index = index.tz_convert(
                "UTC"
            )
        out.index = index
        now = pd.Timestamp.now(
            tz="UTC"
        )
        last_start = out.index[-1]
        # Remove current unfinished candle
        if now < last_start + duration:
            out = out.iloc[:-1]
    except Exception as exc:
        log(
            f"⚠️ Candle completeness "
            f"check error ({interval}): {exc}"
        )
    return out
# ============================================================
# CANDLE CACHE
# ============================================================
def candles(interval):
    now = time.time()
    item = cache.get(interval)
    if item:
        timestamp, old_df = item
        if (
            now - timestamp
            < CACHE_TTL[interval]
            and not old_df.empty
        ):
            return old_df.copy()
    df = download_yahoo(interval)
    if not df.empty:
        df = remove_incomplete_last_candle(
            df,
            interval,
        )
    if not df.empty:
        cache[interval] = (
            now,
            df.copy(),
        )
        return df
    if item:
        log(
            f"⚠️ Using cached "
            f"{interval} data."
        )
        return item[1].copy()
    return pd.DataFrame()
# ============================================================
# CURRENT PRICE
# ============================================================
def current_price():
    df = candles("5m")
    if df.empty:
        return None
    try:
        return float(
            df["close"].iloc[-1]
        )
    except Exception:
        return None
# ============================================================
# EMA
# ============================================================
def ema(
    series,
    period,
):
    return series.ewm(
        span=period,
        adjust=False,
    ).mean()
# ============================================================
# RSI
# ============================================================
def rsi(
    series,
    period=14,
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
        adjust=False,
    ).mean()
    avg_loss = loss.ewm(
        alpha=1 / period,
        min_periods=period,
        adjust=False,
    ).mean()
    rs = (
        avg_gain
        /
        avg_loss.replace(
            0,
            np.nan,
        )
    )
    result = (
        100
        -
        (
            100
            /
            (1 + rs)
        )
    )
    return result.fillna(50)
# ============================================================
# TIMEFRAME TREND
# ============================================================
def timeframe_trend(df):
    if (
        df.empty
        or len(df) < 60
    ):
        return "MIXED"
    close = df["close"]
    e50 = ema(
        close,
        50,
    ).iloc[-1]
    e200 = ema(
        close,
        200,
    ).iloc[-1]
    price = close.iloc[-1]
    if price > e50 > e200:
        return "BULLISH"
    if price < e50 < e200:
        return "BEARISH"
    return "MIXED"
# ============================================================
# BOS
# ============================================================
def detect_bos(df):
    if (
        df.empty
        or len(df) < 15
    ):
        return "NONE"
    recent = df.iloc[:-1]
    last = df.iloc[-1]
    high_ref = (
        recent["high"]
        .tail(12)
        .max()
    )
    low_ref = (
        recent["low"]
        .tail(12)
        .min()
    )
    if last["close"] > high_ref:
        return "BULLISH BOS"
    if last["close"] < low_ref:
        return "BEARISH BOS"
    return "NONE"
# ============================================================
# LIQUIDITY SWEEP
# ============================================================
def detect_liquidity(df):
    if (
        df.empty
        or len(df) < 4
    ):
        return "NONE"
    prev = df.iloc[-2]
    last = df.iloc[-1]
    previous_high = (
        df["high"]
        .iloc[-12:-2]
        .max()
    )
    previous_low = (
        df["low"]
        .iloc[-12:-2]
        .min()
    )
    # Buy-side liquidity sweep
    if (
        last["high"]
        >
        max(
            prev["high"],
            previous_high,
        )
        and
        last["close"]
        <
        previous_high
    ):
        return "BSL SWEEP"
    # Sell-side liquidity sweep
    if (
        last["low"]
        <
        min(
            prev["low"],
            previous_low,
        )
        and
        last["close"]
        >
        previous_low
    ):
        return "SSL SWEEP"
    return "NONE"
# ============================================================
# FVG
# ============================================================
def detect_fvg(df):
    if (
        df.empty
        or len(df) < 4
    ):
        return "NONE"
    a = df.iloc[-3]
    c = df.iloc[-1]
    if c["low"] > a["high"]:
        return "BULLISH FVG"
    if c["high"] < a["low"]:
        return "BEARISH FVG"
    return "NONE"
# ============================================================
# DISPLACEMENT
# ============================================================
def detect_displacement(df):
    if (
        df.empty
        or len(df) < 25
    ):
        return "NONE"
    last = df.iloc[-1]
    body = abs(
        last["close"]
        -
        last["open"]
    )
    ranges = (
        df["high"]
        -
        df["low"]
    ).iloc[-21:-1]
    average_range = ranges.mean()
    if average_range <= 0:
        return "NONE"
    if body >= average_range * 1.5:
        if (
            last["close"]
            >
            last["open"]
        ):
            return "BULLISH"
        return "BEARISH"
    return "NONE"
# ============================================================
# MOMENTUM
# ============================================================
def detect_momentum(df):
    if (
        df.empty
        or len(df) < 60
    ):
        return "MIXED"
    close = df["close"]
    e20 = ema(
        close,
        20,
    ).iloc[-1]
    e50 = ema(
        close,
        50,
    ).iloc[-1]
    price = close.iloc[-1]
    if price > e20 > e50:
        return "BULLISH"
    if price < e20 < e50:
        return "BEARISH"
    return "MIXED"
# ============================================================
# PREMIUM / DISCOUNT
# ============================================================
def detect_zone(df):
    if (
        df.empty
        or len(df) < 20
    ):
        return "MID"
    window = df.tail(50)
    high = float(
        window["high"].max()
    )
    low = float(
        window["low"].min()
    )
    price = float(
        window["close"].iloc[-1]
    )
    if high <= low:
        return "MID"
    position = (
        price - low
    ) / (
        high - low
    )
    if position >= 0.70:
        return "PREMIUM"
    if position <= 0.30:
        return "DISCOUNT"
    return "MID"
# ============================================================
# FEATURES
# ============================================================
def features(df):
    if df.empty:
        return {
            "price": None,
            "trend": "MIXED",
            "liquidity": "NONE",
            "bos": "NONE",
            "fvg": "NONE",
            "displacement": "NONE",
            "momentum": "MIXED",
            "zone": "MID",
            "rsi": 50.0,
        }
    return {
        "price": float(
            df["close"].iloc[-1]
        ),
        "trend": timeframe_trend(
            df
        ),
        "liquidity": detect_liquidity(
            df
        ),
        "bos": detect_bos(
            df
        ),
        "fvg": detect_fvg(
            df
        ),
        "displacement":
            detect_displacement(
                df
            ),
        "momentum":
            detect_momentum(
                df
            ),
        "zone":
            detect_zone(
                df
            ),
        "rsi": float(
            rsi(
                df["close"]
            ).iloc[-1]
        ),
    }
# ============================================================
# ANALYSIS
# ============================================================
def analyze():
    frames = {
        "H4": candles("4h"),
        "H1": candles("1h"),
        "M15": candles("15m"),
        "M5": candles("5m"),
    }
    if any(
        df.empty
        for df in frames.values()
    ):
        return None
    f4 = features(
        frames["H4"]
    )
    f1 = features(
        frames["H1"]
    )
    f15 = features(
        frames["M15"]
    )
    f5 = features(
        frames["M5"]
    )
    buy_score = 0
    sell_score = 0
    # --------------------------------------------------------
    # TIMEFRAME TREND
    # --------------------------------------------------------
    def add_trend(
        feature,
        weight,
    ):
        nonlocal buy_score
        nonlocal sell_score
        if (
            feature["trend"]
            == "BULLISH"
        ):
            buy_score += weight
        elif (
            feature["trend"]
            == "BEARISH"
        ):
            sell_score += weight
    add_trend(
        f4,
        20,
    )
    add_trend(
        f1,
        15,
    )
    add_trend(
        f15,
        15,
    )
    add_trend(
        f5,
        10,
    )
    # --------------------------------------------------------
    # LIQUIDITY
    # --------------------------------------------------------
    if (
        f5["liquidity"]
        == "SSL SWEEP"
    ):
        buy_score += 10
    elif (
        f5["liquidity"]
        == "BSL SWEEP"
    ):
        sell_score += 10
    # --------------------------------------------------------
    # BOS
    # --------------------------------------------------------
    if (
        f5["bos"]
        == "BULLISH BOS"
    ):
        buy_score += 15
    elif (
        f5["bos"]
        == "BEARISH BOS"
    ):
        sell_score += 15
    # --------------------------------------------------------
    # FVG
    # --------------------------------------------------------
    if (
        f5["fvg"]
        == "BULLISH FVG"
    ):
        buy_score += 5
    elif (
        f5["fvg"]
        == "BEARISH FVG"
    ):
        sell_score += 5
    # --------------------------------------------------------
    # DISPLACEMENT
    # --------------------------------------------------------
    if (
        f5["displacement"]
        == "BULLISH"
    ):
        buy_score += 10
    elif (
        f5["displacement"]
        == "BEARISH"
    ):
        sell_score += 10
    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------
    if (
        f5["momentum"]
        == "BULLISH"
    ):
        buy_score += 5
    elif (
        f5["momentum"]
        == "BEARISH"
    ):
        sell_score += 5
    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------
    if f5["rsi"] <= 35:
        buy_score += 5
    elif f5["rsi"] >= 65:
        sell_score += 5
    # --------------------------------------------------------
    # PERCENTAGES
    # --------------------------------------------------------
    total = max(
        buy_score + sell_score,
        1,
    )
    up_pct = round(
        (
            buy_score
            /
            total
        )
        * 100,
        1,
    )
    down_pct = round(
        (
            sell_score
            /
            total
        )
        * 100,
        1,
    )
    # --------------------------------------------------------
    # SIGNAL
    # --------------------------------------------------------
    if (
        buy_score >= MIN_SCORE
        and
        buy_score > sell_score
    ):
        signal = "BUY"
    elif (
        sell_score >= MIN_SCORE
        and
        sell_score > buy_score
    ):
        signal = "SELL"
    elif (
        buy_score >= EARLY_SCORE
        and
        buy_score > sell_score
    ):
        signal = "EARLY BUY"
    elif (
        sell_score >= EARLY_SCORE
        and
        sell_score > buy_score
    ):
        signal = "EARLY SELL"
    else:
        signal = "WAIT"
    return {
        "signal": signal,
        "price": f5["price"],
        "H4": f4,
        "H1": f1,
        "M15": f15,
        "M5": f5,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "up_pct": up_pct,
        "down_pct": down_pct,
    }
# ============================================================
# SETUP
# ============================================================
def make_setup(analysis):
    price = float(
        analysis["price"]
    )
    # Display setup only.
    # Broker-specific contract sizing is NOT implemented here.
    sl_distance = 5.0
    tp_distance = (
        sl_distance
        *
        TARGET_RR
    )
    signal = analysis["signal"]
    if signal in (
        "BUY",
        "EARLY BUY",
    ):
        entry = price
        sl = (
            price
            -
            sl_distance
        )
        tp = (
            price
            +
            tp_distance
        )
    else:
        entry = price
        sl = (
            price
            +
            sl_distance
        )
        tp = (
            price
            -
            tp_distance
        )
    risk_money = (
        DEFAULT_DEPOSIT
        *
        (
            RISK_PERCENT
            /
            100.0
        )
    )
    # Approximation ONLY.
    estimated_lot = (
        risk_money
        /
        sl_distance
    )
    lot = max(
        MIN_LOT,
        min(
            MAX_LOT,
            estimated_lot,
        ),
    )
    return {
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rr": TARGET_RR,
        "lot": lot,
        "risk_money": risk_money,
    }
# ============================================================
# FORMAT TELEGRAM SIGNAL
# ============================================================
def format_signal(analysis):
    setup = make_setup(
        analysis
    )
    f4 = analysis["H4"]
    f1 = analysis["H1"]
    f15 = analysis["M15"]
    f5 = analysis["M5"]
    lines = []
    lines.append(
        "🥇 GOLD SMART V5.4"
    )
    lines.append("")
    lines.append(
        f"⚪ SIGNAL: "
        f"{analysis['signal']}"
    )
    lines.append(
        f"💰 XAUUSD: "
        f"{analysis['price']:.2f}"
    )
    lines.append("")
    lines.append(
        f"📊 H4: "
        f"{f4['trend']}"
    )
    lines.append(
        f"📊 H1: "
        f"{f1['trend']}"
    )
    lines.append(
        f"📊 M15: "
        f"{f15['trend']}"
    )
    lines.append(
        f"📊 M5: "
        f"{f5['trend']}"
    )
    lines.append("")
    lines.append(
        f"💧 M5 Liquidity: "
        f"{f5['liquidity']}"
    )
    lines.append(
        f"🔨 M5 BOS: "
        f"{f5['bos']}"
    )
    lines.append(
        f"🧩 M5 FVG: "
        f"{f5['fvg']}"
    )
    lines.append(
        f"💥 M5 Displacement: "
        f"{f5['displacement']}"
    )
    lines.append(
        f"📈 M5 Momentum: "
        f"{f5['momentum']}"
    )
    lines.append(
        f"📐 M5 Zone: "
        f"{f5['zone']}"
    )
    lines.append(
        f"📉 M5 RSI: "
        f"{f5['rsi']:.1f}"
    )
    lines.append("")
    lines.append(
        f"🟢 UP: "
        f"{analysis['up_pct']:.1f}%"
    )
    lines.append(
        f"🔴 DOWN: "
        f"{analysis['down_pct']:.1f}%"
    )
    lines.append(
        f"🎯 SCORE: "
        f"BUY {analysis['buy_score']} / "
        f"SELL {analysis['sell_score']}"
    )
    if analysis["signal"] != "WAIT":
        lines.append("")
        lines.append(
            "📌 SETUP"
        )
        lines.append(
            f"ENTRY: "
            f"{setup['entry']:.2f}"
        )
        lines.append(
            f"SL: "
            f"{setup['sl']:.2f}"
        )
        lines.append(
            f"TP: "
            f"{setup['tp']:.2f}"
        )
        lines.append(
            f"RR: "
            f"1:{setup['rr']:.1f}"
        )
        lines.append(
            f"LOT: "
            f"{setup['lot']:.2f}"
        )
        lines.append(
            f"RISK: "
            f"{RISK_PERCENT:.1f}%"
        )
    else:
        lines.append("")
        lines.append(
            "⏳ NO VALID ENTRY"
        )
    lines.append("")
    lines.append(
        "🤖 AUTO TRADING: OFF"
    )
    lines.append(
        f"📡 SOURCE: "
        f"{SOURCE_NAME}"
    )
    lines.append(
        "🕯 CLOSED CANDLE ENGINE"
    )
    return "\n".join(
        lines
    )
# ============================================================
# SIGNAL KEY
# ============================================================
def signal_key(analysis):
    m5 = analysis["M5"]
    return (
        analysis["signal"],
        round(
            analysis["price"],
            2,
        ),
        m5["liquidity"],
        m5["bos"],
        m5["fvg"],
        m5["displacement"],
    )
# ============================================================
# PROCESS SIGNAL
# ============================================================
def process_signal(analysis):
    global last_signal_key
    global last_signal_time
    if analysis is None:
        return
    signal = analysis["signal"]
    if signal == "WAIT":
        stats["wait"] += 1
        return
    key = signal_key(
        analysis
    )
    now = time.time()
    if key == last_signal_key:
        return
    if (
        now - last_signal_time
        <
        COOLDOWN_MIN * 60
    ):
        log(
            "⏳ Signal cooldown active."
        )
        return
    message = format_signal(
        analysis
    )
    if telegram_send(
        message
    ):
        last_signal_key = key
        last_signal_time = now
        stats["signals"] += 1
        if signal == "BUY":
            stats["buy"] += 1
        elif signal == "SELL":
            stats["sell"] += 1
        elif signal == "EARLY BUY":
            stats["early_buy"] += 1
        elif signal == "EARLY SELL":
            stats["early_sell"] += 1
        log(
            f"📨 Telegram signal sent: "
            f"{signal}"
        )
# ============================================================
# ENGINE LOOP
# ============================================================
def engine_loop():
    log(
        "🥇 GOLD SMART V5.4 ENGINE STARTED"
    )
    log(
        f"📡 SOURCE: "
        f"{SOURCE_NAME}"
    )
    log(
        f"🛡 RISK: "
        f"{RISK_PERCENT:.0f}%"
    )
    log(
        "🤖 AUTO TRADING: OFF"
    )
    log(
        "🕯 CLOSED CANDLE ENGINE"
    )
    while True:
        try:
            analysis = analyze()
            if analysis is None:
                log(
                    "⚠️ Market data unavailable."
                )
            else:
                log(
                    f"📊 "
                    f"{analysis['signal']} | "
                    f"XAUUSD "
                    f"{analysis['price']:.2f} | "
                    f"BUY "
                    f"{analysis['buy_score']} | "
                    f"SELL "
                    f"{analysis['sell_score']}"
                )
                process_signal(
                    analysis
                )
        except Exception as exc:
            stats["errors"] += 1
            log(
                f"❌ Engine error: "
                f"{exc}"
            )
            traceback.print_exc()
        time.sleep(
            POLL_SECONDS
        )
# ============================================================
# START ENGINE
# ============================================================
def start_engine():
    global ENGINE_STARTED
    if ENGINE_STARTED:
        return
    ENGINE_STARTED = True
    thread = threading.Thread(
        target=engine_loop,
        name="gold-smart-engine",
        daemon=True,
    )
    thread.start()
    log(
        "🟢 Engine thread launched"
    )
# ============================================================
# FLASK ROUTES
# ============================================================
@app.route("/")
def home():
    return (
        "GOLD SMART V5.4 OK"
    )
@app.route("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "engine_started":
                ENGINE_STARTED,
            "source":
                SOURCE_NAME,
            "auto_trading":
                False,
            "risk_percent":
                RISK_PERCENT,
        }
    )
@app.route("/test")
def test():
    try:
        analysis = analyze()
        if analysis is None:
            return jsonify(
                {
                    "status":
                        "error",
                    "message":
                        "Market data unavailable",
                }
            ), 503
        return jsonify(
            analysis
        )
    except Exception as exc:
        return jsonify(
            {
                "status":
                    "error",
                "message":
                    str(exc),
            }
        ), 500
@app.route("/stats")
def statistics():
    return jsonify(
        stats
    )
@app.route(
    "/webhook",
    methods=["GET", "POST"],
)
def webhook():
    return jsonify(
        {
            "status": "ok",
            "message":
                "GOLD SMART V5.4 webhook active",
        }
    )
# ============================================================
# IMPORTANT FOR GUNICORN
# ============================================================
# Gunicorn imports bot.py directly.
# Therefore the engine must start during module import.
start_engine()
# ============================================================
# LOCAL RUN
# ============================================================
if __name__ == "__main__":
    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
    )
