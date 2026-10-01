import os
import json
import time
import threading
import requests
import pandas as pd
import numpy as np

from flask import Flask, request, jsonify


# =========================================================
# 🥇 GOLD SMART V5.2
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
    except Exception:
        pass


# =========================================================
# TWELVE DATA
# =========================================================

def td(endpoint, **params):

    if not TWELVE_KEY:
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

        data = r.json()

        if "values" not in data:
            return None

        return data

    except Exception:
        return None


# =========================================================
# CANDLES
# =========================================================

def candles(interval, size=250):

    data = td(
        "time_series",
        interval=interval,
        outputsize=size
    )

    if not data:
        return None

    try:

        df = pd.DataFrame(data["values"])

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

    except Exception:
        return None


# =========================================================
# CURRENT PRICE
# =========================================================

def price():

    data = td("quote")

    try:

        if data and data.get("close"):
            return float(data["close"])

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

    rs = gain / loss.replace(0, np.nan)

    value = (
        100
        - 100 / (1 + rs)
    ).iloc[-1]

    return float(value)


# =========================================================
# ATR
# =========================================================

def atr(df, period=14):

    previous_close = df["close"].shift()

    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs(),
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

    # ---------------------------------------------
    # Previous liquidity range
    # ---------------------------------------------

    previous_high = float(
        high.iloc[-21:-1].max()
    )

    previous_low = float(
        low.iloc[-21:-1].min()
    )

    last = df.iloc[-1]

    # ---------------------------------------------
    # BOS
    # ---------------------------------------------

    bullish_bos = (
        last.close > previous_high
    )

    bearish_bos = (
        last.close < previous_low
    )

    if bullish_bos:
        bos = "BULLISH"

    elif bearish_bos:
        bos = "BEARISH"

    else:
        bos = "NONE"

    # ---------------------------------------------
    # Liquidity Sweep
    # ---------------------------------------------

    bsl = (
        last.high > previous_high
        and last.close < previous_high
    )

    ssl = (
        last.low < previous_low
        and last.close > previous_low
    )

    # ---------------------------------------------
    # FVG
    # ---------------------------------------------

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

    # ---------------------------------------------
    # Displacement
    # ---------------------------------------------

    body = abs(
        last.close - last.open
    )

    candle_range = max(
        last.high - last.low,
        0.00001
    )

    average_range = (
        df["high"] - df["low"]
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

    # ---------------------------------------------
    # Momentum
    # ---------------------------------------------

    if close.iloc[-1] > close.iloc[-4]:
        momentum = "BULLISH"

    elif close.iloc[-1] < close.iloc[-4]:
        momentum = "BEARISH"

    else:
        momentum = "NEUTRAL"

    # ---------------------------------------------
    # Trend
    # ---------------------------------------------

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

    # ---------------------------------------------
    # Structure
    # ---------------------------------------------

    if close.iloc[-1] > close.iloc[-6]:
        structure = "BULLISH"

    elif close.iloc[-1] < close.iloc[-6]:
        structure = "BEARISH"

    else:
        structure = "MIXED"

    # ---------------------------------------------
    # Premium / Discount
    # ---------------------------------------------

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

        "candle": df.index[-1].isoformat(),
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

    # ---------------------------------------------
    # H4
    # ---------------------------------------------

    if h4["trend"] == wanted:
        score += 15

    # ---------------------------------------------
    # H1
    # ---------------------------------------------

    if h1["trend"] == wanted:
        score += 15

    # ---------------------------------------------
    # M15
    # ---------------------------------------------

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

    # ---------------------------------------------
    # M5
    # ---------------------------------------------

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

    # ---------------------------------------------
    # RR
    # ---------------------------------------------

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

    # ---------------------------------------------
    # Lot calculation
    # ---------------------------------------------

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
# ANALYSIS
# =========================================================

def analyze():

    datasets = {
        "H4": candles("4h", 180),
        "H1": candles("1h", 180),
        "M15": candles("15min", 180),
        "M5": candles("5min", 250),
    }

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

        # ---------------------------------------------
        # RSI filter
        # ---------------------------------------------

        if direction == "BUY":

            rsi_ok = (
                m5["rsi"] < 70
            )

        else:

            rsi_ok = (
                m5["rsi"] > 30
            )

        # ---------------------------------------------
        # M5 trigger
        # ---------------------------------------------

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

        # ---------------------------------------------
        # Full signal
        # ---------------------------------------------

        full_signal = (
            score >= MIN_SCORE
            and rsi_ok
            and m5_trigger
        )

        # ---------------------------------------------
        # Early signal
        # ---------------------------------------------

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
            "🥇 GOLD SMART V5.2\n\n"
            "⚪ SIGNAL: WAIT\n\n"

            f"💰 XAUUSD: {a['price']:.2f}\n\n"

            f"📊 H4: {a['h4']['trend']}\n"
            f"📊 H1: {a['h1']['trend']}\n"
            f"📊 M15: {a['m15']['trend']}\n"
            f"📊 M5: {a['m5']['trend']}\n\n"

            f"💧 M5 Liquidity: "
            f"{liquidity_text(a['m5'])}\n"

            f"🔨 M5 BOS: {a['m5']['bos']}\n"
            f"🧩 M5 FVG: {a['m5']['fvg']}\n"
            f"💥 M5 Displacement: "
            f"{a['m5']['disp']}\n"

            f"📈 M5 Momentum: "
            f"{a['m5']['momentum']}\n"

            f"📐 M5 Zone: "
            f"{a['m5']['zone']}\n"

            f"RSI: {a['m5']['rsi']:.1f}\n\n"

            f"🎯 SCORE: {a['score']}/100"
        )

    s = a["setup"]

    emoji = (
        "🟢"
        if a["direction"] == "BUY"
        else "🔴"
    )

    return (
        "🥇 GOLD SMART V5.2\n\n"

        f"{emoji} SIGNAL: {a['signal']}\n\n"

        f"💰 XAUUSD: {a['price']:.2f}\n"
        f"🎯 SCORE: {a['score']}/100\n\n"

        f"📊 H4: {a['h4']['trend']}\n"
        f"📊 H1: {a['h1']['trend']}\n"
        f"📊 M15: {a['m15']['trend']}\n"
        f"📊 M5: {a['m5']['trend']}\n\n"

        f"💧 M5 Liquidity: "
        f"{liquidity_text(a['m5'])}\n"

        f"🔨 M5 BOS: {a['m5']['bos']}\n"
        f"🧩 M5 FVG: {a['m5']['fvg']}\n"
        f"💥 M5 Displacement: "
        f"{a['m5']['disp']}\n"

        f"📈 M5 Momentum: "
        f"{a['m5']['momentum']}\n"

        f"📐 M5 Zone: "
        f"{a['m5']['zone']}\n"

        f"RSI: {a['m5']['rsi']:.1f}\n\n"

        f"📍 ENTRY: {a['price']:.2f}\n"
        f"🛑 SL: {s['sl']:.2f}\n"
        f"🎯 TP1: {s['tp1']:.2f}\n"
        f"🏁 TP2: {s['tp2']:.2f}\n\n"

        f"📐 RR: 1:{s['rr']:.1f}\n"
        f"📦 LOT: {s['lot']:.2f}\n"
        f"⚠️ RISK: {RISK_PERCENT}%\n\n"

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
        "📊 GOLD SMART V5.2 — STATISTICS\n\n"

        f"📌 TOTAL SIGNALS: "
        f"{stats['signals']}\n\n"

        f"🟢 WIN: {stats['WIN']}\n"
        f"🔴 LOSS: {stats['LOSS']}\n"
        f"⚪ BE: {stats['BE']}\n"
        f"🟡 OPEN: {stats['OPEN']}\n\n"

        f"🎯 CLOSED: {closed}\n\n"

        f"📈 WIN RATE: "
        f"{winrate:.1f}%"
    )


# =========================================================
# STATUS
# =========================================================

def status_text():

    return (
        "🥇 GOLD SMART V5.2\n\n"

        "🟢 BOT: ONLINE\n"
        "🤖 AUTO TRADING: OFF\n\n"

        f"⚠️ RISK: {RISK_PERCENT}%\n"
        f"📦 LOT: {MIN_LOT:.2f}–{MAX_LOT:.2f}\n\n"

        "📊 TIMEFRAME:\n"
        "H4 → H1 → M15 → M5\n\n"

        "🎯 M5 = ENTRY TRIGGER\n"
        f"⏱ CHECK: {POLL_SECONDS}s"
    )


# =========================================================
# TELEGRAM COMMANDS
# =========================================================

def handle_command(text):

    command = (
        text
        .split()[0]
        .lower()
        .split("@")[0]
    )

    # ---------------------------------------------
    # START / HELP
    # ---------------------------------------------

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

    # ---------------------------------------------
    # STATUS
    # ---------------------------------------------

    if command == "/status":
        return status_text()

    # ---------------------------------------------
    # STATS
    # ---------------------------------------------

    if command == "/stats":
        return stats_text()

    # ---------------------------------------------
    # GOLD
    # ---------------------------------------------

    if command == "/gold":

        analysis = analyze()

        if analysis is None:

            return (
                "❌ Не удалось получить "
                "данные XAU/USD."
            )

        return signal_text(
            analysis
        )

    # ---------------------------------------------
    # TEST
    # ---------------------------------------------

    if command == "/test":

        return (
            "✅ GOLD SMART V5.2 TEST OK\n\n"
            "🟢 Engine: READY\n"
            "🟢 Telegram: READY\n"
            "🟢 Twelve Data: "
            + (
                "CONFIGURED"
                if TWELVE_KEY
                else "MISSING"
            )
            + "\n"
            "🤖 AUTO TRADING: OFF"
        )

    return None


# =========================================================
# FLASK ROUTES
# =========================================================

@app.get("/")
def home():

    return "GOLD SMART V5.2 OK"


@app.get("/health")
def health():

    return jsonify(
        {
            "status": "ok",
            "version": "5.2",
            "running": state["running"],
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

    while True:

        try:

            df = candles(
                "5min",
                250
            )

            if (
                df is not None
                and len(df) > 5
            ):

                # Используем только закрытую M5 свечу
                closed = df.iloc[:-1]

                candle_stamp = str(
                    closed.index[-1]
                )

                # Только один анализ
                # на новую закрытую M5 свечу
                if (
                    candle_stamp
                    != state["last_m5"]
                ):

                    state["last_m5"] = (
                        candle_stamp
                    )

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
                            - state["last_signal_at"]
                            >= COOLDOWN_MIN * 60
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

    # Telegram webhook
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

    except Exception as e:

        print(
            "WEBHOOK ERROR:",
            e
        )


startup()
