import os
import json
import time
import threading
import requests
import numpy as np
import pandas as pd

from datetime import datetime, timezone
from flask import Flask, request


# =========================================================
# 🥇 GOLD SMART V5.9
# SMART SMC + SMART TREND ENGINE
#
# XAU/USD
# Telegram Webhook
# XAUS.COM DATA
#
# AUTO TRADING = OFF
# RISK = 1%
# CLOSED CANDLES = ON
# =========================================================


APP_NAME = "GOLD SMART V5.9"

# =========================================================
# CONFIG
# =========================================================

XAUS_SPOT_URL = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY_URL = "https://xaus.com/api/v1/intraday"

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

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


# =========================================================
# GLOBAL STATE
# =========================================================

app = Flask(__name__)

price_cache = {
    "price": None,
    "time": 0
}

intraday_cache = {
    "df": None,
    "time": 0
}

state_lock = threading.Lock()

virtual_trade = None
last_signal_time = 0


# =========================================================
# TELEGRAM
# =========================================================

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
                "text": text_message
            },
            timeout=15
        )

        if response.status_code != 200:
            print("Telegram error:", response.text)
            return False

        return True

    except Exception as e:
        print("Telegram exception:", e)
        return False


# =========================================================
# STATS
# =========================================================

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
        "trades": []
    }


def load_stats():
    if not os.path.exists(STATS_FILE):
        return default_stats()

    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        base = default_stats()
        base.update(data)

        return base

    except Exception as e:
        print("Stats load error:", e)
        return default_stats()


def save_stats(stats):
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)

    except Exception as e:
        print("Stats save error:", e)


stats = load_stats()


# =========================================================
# PRICE
# =========================================================

def fetch_spot(force=False):

    now = time.time()

    if (
        not force
        and price_cache["price"] is not None
        and now - price_cache["time"] < CACHE_SECONDS
    ):
        return price_cache["price"]

    try:
        r = requests.get(
            XAUS_SPOT_URL,
            timeout=15
        )

        r.raise_for_status()

        data = r.json()

        price = None

        # Main XAUS structure
        if isinstance(data, dict):

            xau = data.get("xau")

            if isinstance(xau, dict):
                price = xau.get("price")

            if price is None:
                price = data.get("spot_usd_oz")

        if price is None:
            raise ValueError(f"Unknown spot response: {data}")

        price = float(price)

        price_cache["price"] = price
        price_cache["time"] = now

        return price

    except Exception as e:
        print("Spot error:", e)

        if price_cache["price"] is not None:
            return price_cache["price"]

        return None


# =========================================================
# INTRADAY DATA
# =========================================================

def fetch_intraday(force=False):

    now = time.time()

    if (
        not force
        and intraday_cache["df"] is not None
        and now - intraday_cache["time"] < CACHE_SECONDS
    ):
        return intraday_cache["df"].copy()

    try:

        params = {
            "symbol": "xau",
            "hours": 48
        }

        r = requests.get(
            XAUS_INTRADAY_URL,
            params=params,
            timeout=20
        )

        r.raise_for_status()

        data = r.json()

        points = None

        if isinstance(data, dict):

            for key in ["points", "data", "series"]:

                if isinstance(data.get(key), list):
                    points = data[key]
                    break

        if points is None and isinstance(data, list):
            points = data

        if not points:
            raise ValueError("No intraday data")

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

                if isinstance(timestamp, (int, float)):

                    # milliseconds
                    if timestamp > 100000000000:
                        dt = pd.to_datetime(
                            timestamp,
                            unit="ms",
                            utc=True
                        )
                    else:
                        dt = pd.to_datetime(
                            timestamp,
                            unit="s",
                            utc=True
                        )

                else:
                    dt = pd.to_datetime(
                        timestamp,
                        utc=True
                    )

                rows.append(
                    {
                        "time": dt,
                        "price": float(price)
                    }
                )

            except Exception:
                continue

        if len(rows) < 30:
            raise ValueError(
                f"Not enough intraday data: {len(rows)}"
            )

        df = pd.DataFrame(rows)

        df = df.sort_values("time")
        df = df.drop_duplicates("time")

        df = df.set_index("time")

        ohlc = df["price"].resample("1min").ohlc()

        ohlc = ohlc.dropna()

        if CLOSED_CANDLES and len(ohlc) > 2:

            # Remove current unfinished candle
            ohlc = ohlc.iloc[:-1]

        intraday_cache["df"] = ohlc.copy()
        intraday_cache["time"] = now

        return ohlc

    except Exception as e:

        print("Intraday error:", e)

        if intraday_cache["df"] is not None:
            return intraday_cache["df"].copy()

        return None


# =========================================================
# RESAMPLE
# =========================================================

def resample_ohlc(df, timeframe):

    if df is None or len(df) < 10:
        return None

    rules = {
        "M5": "5min",
        "M15": "15min",
        "H1": "1h",
        "H4": "4h"
    }

    if timeframe not in rules:
        return None

    out = df.resample(rules[timeframe]).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last"
        }
    )

    out = out.dropna()

    if CLOSED_CANDLES and len(out) > 2:
        out = out.iloc[:-1]

    return out


# =========================================================
# INDICATORS
# =========================================================

def add_indicators(df):

    if df is None or len(df) < 20:
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

    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    x["rsi"] = 100 - (
        100 / (1 + rs)
    )

    x["rsi"] = x["rsi"].fillna(50)

    return x


# =========================================================
# STRUCTURE
# =========================================================

def structure_direction(df, lookback=30):

    if df is None or len(df) < 15:
        return "MIXED"

    x = df.tail(lookback).copy()

    highs = []
    lows = []

    h = x["high"].values
    l = x["low"].values

    for i in range(2, len(x) - 2):

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

    last_high = highs[-1]
    prev_high = highs[-2]

    last_low = lows[-1]
    prev_low = lows[-2]

    higher_high = last_high > prev_high
    higher_low = last_low > prev_low

    lower_high = last_high < prev_high
    lower_low = last_low < prev_low

    if higher_high and higher_low:
        return "BULLISH"

    if lower_high and lower_low:
        return "BEARISH"

    return "MIXED"


# =========================================================
# MOMENTUM
# =========================================================

def momentum_from_df(df):

    if df is None or len(df) < 6:
        return "MIXED"

    last_close = float(df["close"].iloc[-1])
    old_close = float(df["close"].iloc[-5])

    if last_close > old_close:
        return "BULLISH"

    if last_close < old_close:
        return "BEARISH"

    return "MIXED"


# =========================================================
# BOS
# =========================================================

def detect_bos(df, lookback=8):

    if df is None or len(df) < lookback + 2:
        return "NONE"

    recent = df.iloc[-lookback - 1:-1]

    last = df.iloc[-1]

    previous_high = recent["high"].max()
    previous_low = recent["low"].min()

    if last["close"] > previous_high:
        return "BULLISH BOS"

    if last["close"] < previous_low:
        return "BEARISH BOS"

    return "NONE"


# =========================================================
# LIQUIDITY
# =========================================================

def detect_liquidity(df, lookback=12):

    if df is None or len(df) < lookback + 2:
        return "NONE"

    previous = df.iloc[-lookback - 1:-1]

    last = df.iloc[-1]

    previous_high = previous["high"].max()
    previous_low = previous["low"].min()

    # Buy-side liquidity sweep
    if (
        last["high"] > previous_high
        and last["close"] < previous_high
    ):
        return "BSL SWEEP"

    # Sell-side liquidity sweep
    if (
        last["low"] < previous_low
        and last["close"] > previous_low
    ):
        return "SSL SWEEP"

    return "NONE"


# =========================================================
# FVG
# =========================================================

def detect_fvg(df):

    if df is None or len(df) < 5:
        return "NONE"

    a = df.iloc[-3]
    c = df.iloc[-1]

    # Bullish FVG
    if c["low"] > a["high"]:
        return "BULLISH FVG"

    # Bearish FVG
    if c["high"] < a["low"]:
        return "BEARISH FVG"

    return "NONE"


# =========================================================
# DISPLACEMENT
# =========================================================

def detect_displacement(df):

    if df is None or len(df) < 12:
        return "NONE"

    bodies = (
        df["close"] - df["open"]
    ).abs()

    average_body = bodies.iloc[-11:-1].mean()

    last_body = bodies.iloc[-1]

    if average_body <= 0:
        return "NONE"

    if last_body >= average_body * 1.8:

        if df["close"].iloc[-1] > df["open"].iloc[-1]:
            return "BULLISH"

        if df["close"].iloc[-1] < df["open"].iloc[-1]:
            return "BEARISH"

    return "NONE"


# =========================================================
# ZONE
# =========================================================

def detect_zone(df):

    if df is None or len(df) < 20:
        return "MID"

    recent = df.tail(20)

    high = recent["high"].max()
    low = recent["low"].min()

    price = df["close"].iloc[-1]

    midpoint = (
        high + low
    ) / 2

    if price < midpoint:
        return "DISCOUNT"

    if price > midpoint:
        return "PREMIUM"

    return "MID"


# =========================================================
# SMART TREND ENGINE
# =========================================================

def trend_score(df):

    if df is None or len(df) < 25:
        return {
            "direction": "MIXED",
            "score": 50,
            "structure": "MIXED",
            "momentum": "MIXED",
            "bos": "NONE"
        }

    x = add_indicators(df)

    if x is None or len(x) < 20:
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

    bullish_points = 0
    bearish_points = 0

    # -----------------------------------------------------
    # STRUCTURE = 40
    # -----------------------------------------------------

    if structure == "BULLISH":
        bullish_points += 40

    elif structure == "BEARISH":
        bearish_points += 40

    # -----------------------------------------------------
    # EMA STRUCTURE = 25
    # -----------------------------------------------------

    close = float(last["close"])
    ema20 = float(last["ema20"])
    ema50 = float(last["ema50"])
    ema200 = float(last["ema200"])

    if close > ema20 > ema50 > ema200:
        bullish_points += 25

    elif close < ema20 < ema50 < ema200:
        bearish_points += 25

    elif close > ema50 and ema20 > ema50:
        bullish_points += 15

    elif close < ema50 and ema20 < ema50:
        bearish_points += 15

    # -----------------------------------------------------
    # MOMENTUM = 15
    # -----------------------------------------------------

    if momentum == "BULLISH":
        bullish_points += 15

    elif momentum == "BEARISH":
        bearish_points += 15

    # -----------------------------------------------------
    # BOS = 20
    # -----------------------------------------------------

    if bos == "BULLISH BOS":
        bullish_points += 20

    elif bos == "BEARISH BOS":
        bearish_points += 20

    # -----------------------------------------------------
    # FINAL SCORE
    # -----------------------------------------------------

    total = bullish_points + bearish_points

    if total == 0:

        return {
            "direction": "MIXED",
            "score": 50,
            "structure": structure,
            "momentum": momentum,
            "bos": bos
        }

    if bullish_points > bearish_points:

        score = 50 + (
            bullish_points - bearish_points
        ) / 2

        direction = (
            "BULLISH"
            if score >= 60
            else "MIXED"
        )

    elif bearish_points > bullish_points:

        score = 50 + (
            bearish_points - bullish_points
        ) / 2

        direction = (
            "BEARISH"
            if score >= 60
            else "MIXED"
        )

    else:

        score = 50
        direction = "MIXED"

    score = max(
        0,
        min(100, round(score))
    )

    return {
        "direction": direction,
        "score": score,
        "structure": structure,
        "momentum": momentum,
        "bos": bos
    }


def trend_from_df(df):
    return trend_score(df)["direction"]


# =========================================================
# ANALYSIS
# =========================================================

def build_market_data():

    raw = fetch_intraday()

    if raw is None:
        return None

    result = {}

    for tf in ["M5", "M15", "H1", "H4"]:

        tf_df = resample_ohlc(
            raw,
            tf
        )

        if tf_df is None or len(tf_df) < 20:
            return None

        result[tf] = add_indicators(tf_df)

    return result


# =========================================================
# SMART SIGNAL SCORE
# =========================================================

def score_signal(market):

    h4 = market["H4"]
    h1 = market["H1"]
    m15 = market["M15"]
    m5 = market["M5"]

    trend_h4 = trend_score(h4)
    trend_h1 = trend_score(h1)
    trend_m15 = trend_score(m15)
    trend_m5 = trend_score(m5)

    trend_data = {
        "H4": trend_h4,
        "H1": trend_h1,
        "M15": trend_m15,
        "M5": trend_m5
    }

    buy_score = 0.0
    sell_score = 0.0

    # =====================================================
    # HIGHER TIMEFRAME TREND
    # =====================================================

    weights = {
        "H4": 20,
        "H1": 15,
        "M15": 15,
        "M5": 10
    }

    for tf, data in trend_data.items():

        score = data["score"]
        weight = weights[tf]

        contribution = (
            score / 100
        ) * weight

        if data["direction"] == "BULLISH":
            buy_score += contribution

        elif data["direction"] == "BEARISH":
            sell_score += contribution

    # =====================================================
    # M5 SMC ENTRY ENGINE
    # =====================================================

    liquidity = detect_liquidity(m5)
    bos = detect_bos(m5)
    fvg = detect_fvg(m5)
    displacement = detect_displacement(m5)
    momentum = momentum_from_df(m5)
    zone = detect_zone(m5)

    rsi = float(
        m5["rsi"].iloc[-1]
    )

    # -----------------------------------------------------
    # LIQUIDITY
    # -----------------------------------------------------

    if liquidity == "SSL SWEEP":
        buy_score += 10

    elif liquidity == "BSL SWEEP":
        sell_score += 10

    # -----------------------------------------------------
    # BOS
    # -----------------------------------------------------

    if bos == "BULLISH BOS":
        buy_score += 15

    elif bos == "BEARISH BOS":
        sell_score += 15

    # -----------------------------------------------------
    # FVG
    # -----------------------------------------------------

    if fvg == "BULLISH FVG":
        buy_score += 5

    elif fvg == "BEARISH FVG":
        sell_score += 5

    # -----------------------------------------------------
    # DISPLACEMENT
    # -----------------------------------------------------

    if displacement == "BULLISH":
        buy_score += 10

    elif displacement == "BEARISH":
        sell_score += 10

    # -----------------------------------------------------
    # MOMENTUM
    # -----------------------------------------------------

    if momentum == "BULLISH":
        buy_score += 5

    elif momentum == "BEARISH":
        sell_score += 5

    # -----------------------------------------------------
    # RSI
    # -----------------------------------------------------

    if rsi < 35:
        buy_score += 5

    elif rsi > 65:
        sell_score += 5

    # -----------------------------------------------------
    # PREMIUM / DISCOUNT
    # -----------------------------------------------------

    if zone == "DISCOUNT":
        buy_score += 3

    elif zone == "PREMIUM":
        sell_score += 3

    # =====================================================
    # HIGHER TF BIAS
    # =====================================================

    if (
        trend_h4["direction"] == "BULLISH"
        and trend_h1["direction"] == "BULLISH"
    ):
        higher_bias = "BULLISH"

    elif (
        trend_h4["direction"] == "BEARISH"
        and trend_h1["direction"] == "BEARISH"
    ):
        higher_bias = "BEARISH"

    else:
        higher_bias = "MIXED"

    # =====================================================
    # DOMINANT SIDE
    # =====================================================

    if buy_score > sell_score:

        dominant = "BUY"
        dominant_score = buy_score

    elif sell_score > buy_score:

        dominant = "SELL"
        dominant_score = sell_score

    else:

        dominant = "NONE"
        dominant_score = 0

    # =====================================================
    # FULL SIGNAL
    # =====================================================

    signal = "WAIT"

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

    # =====================================================
    # EARLY SIGNAL
    # =====================================================

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

        "buy_score": round(buy_score, 1),
        "sell_score": round(sell_score, 1),

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
        "rsi": round(rsi, 1)
    }


# =========================================================
# FORMAT TREND
# =========================================================

def trend_line(tf, data):

    return (
        f"📊 {tf}: "
        f"{data['direction']} "
        f"({data['score']}/100)"
    )


# =========================================================
# SIGNAL MESSAGE
# =========================================================

def build_signal_message(price, analysis):

    td = analysis["trend_data"]

    signal = analysis["signal"]

    if signal == "BUY":
        emoji = "🟢"

    elif signal == "SELL":
        emoji = "🔴"

    elif signal == "EARLY BUY":
        emoji = "🟡"

    elif signal == "EARLY SELL":
        emoji = "🟠"

    else:
        emoji = "⚪"

    message = f"""
🥇 {APP_NAME}

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

🛡 AUTO TRADING:
{"ON" if AUTO_TRADING else "OFF"}

📉 RISK:
{RISK_PERCENT:.1f}%

━━━━━━━━━━━━━━━━━━

💼 DEMO DEPOSIT:
${DEFAULT_DEPOSIT:.2f}
"""

    return message.strip()


# =========================================================
# VIRTUAL TRADE
# =========================================================

def open_virtual_trade(
    direction,
    price,
    signal_type
):

    global virtual_trade

    if direction not in ["BUY", "SELL"]:
        return

    if signal_type not in [
        "BUY",
        "SELL"
    ]:
        return

    if virtual_trade is not None:
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
        "entry": round(price, 2),
        "sl": round(sl, 2),
        "tp": round(tp, 2),
        "original_sl": round(sl, 2),
        "be": False,
        "opened_at": datetime.now(
            timezone.utc
        ).isoformat()
    }

    stats["open"] = 1

    save_stats(stats)


def close_virtual_trade(result):

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

    if signal_type in ["BUY", "SELL"]:

        stats["full_signals"] += 1

        if result == "WIN":
            stats["full_win"] += 1

        elif result == "LOSS":
            stats["full_loss"] += 1

    trade_record = {
        "direction": direction,
        "signal_type": signal_type,
        "entry": virtual_trade["entry"],
        "sl": virtual_trade["sl"],
        "tp": virtual_trade["tp"],
        "result": result,
        "closed_at": datetime.now(
            timezone.utc
        ).isoformat()
    }

    stats["trades"].append(
        trade_record
    )

    virtual_trade = None

    save_stats(stats)


# =========================================================
# UPDATE VIRTUAL TRADE
# =========================================================

def update_virtual_trade(price):

    global virtual_trade

    if virtual_trade is None:
        return None

    direction = virtual_trade["direction"]

    entry = virtual_trade["entry"]

    sl = virtual_trade["sl"]

    tp = virtual_trade["tp"]

    # =====================================================
    # BUY
    # =====================================================

    if direction == "BUY":

        profit_move = price - entry

        # Break-even
        if (
            not virtual_trade["be"]
            and profit_move >= BE_TRIGGER
        ):

            virtual_trade["sl"] = entry
            virtual_trade["be"] = True

            save_stats(stats)

            send_telegram(
                f"""
🛡 GOLD SMART V5.9

🔒 BREAK EVEN

🟢 BUY

💰 Entry: {entry:.2f}
📍 Current: {price:.2f}

➡️ SL moved to BE:
{entry:.2f}

AUTO TRADING: OFF
""".strip()
            )

        current_sl = virtual_trade["sl"]

        if price <= current_sl:

            if virtual_trade["be"]:
                close_virtual_trade("BE")
                return "BE"

            close_virtual_trade("LOSS")
            return "LOSS"

        if price >= tp:

            close_virtual_trade("WIN")
            return "WIN"

    # =====================================================
    # SELL
    # =====================================================

    elif direction == "SELL":

        profit_move = entry - price

        # Break-even
        if (
            not virtual_trade["be"]
            and profit_move >= BE_TRIGGER
        ):

            virtual_trade["sl"] = entry
            virtual_trade["be"] = True

            save_stats(stats)

            send_telegram(
                f"""
🛡 GOLD SMART V5.9

🔒 BREAK EVEN

🔴 SELL

💰 Entry: {entry:.2f}
📍 Current: {price:.2f}

➡️ SL moved to BE:
{entry:.2f}

AUTO TRADING: OFF
""".strip()
            )

        current_sl = virtual_trade["sl"]

        if price >= current_sl:

            if virtual_trade["be"]:
                close_virtual_trade("BE")
                return "BE"

            close_virtual_trade("LOSS")
            return "LOSS"

        if price <= tp:

            close_virtual_trade("WIN")
            return "WIN"

    return None


# =========================================================
# STATS MESSAGE
# =========================================================

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

    if closed > 0:
        win_rate = (
            win / closed
        ) * 100
    else:
        win_rate = 0

    full = stats.get(
        "full_signals",
        0
    )

    full_win = stats.get(
        "full_win",
        0
    )

    full_loss = stats.get(
        "full_loss",
        0
    )

    early = stats.get(
        "early_signals",
        0
    )

    early_win = stats.get(
        "early_win",
        0
    )

    early_loss = stats.get(
        "early_loss",
        0
    )

    if full_win + full_loss > 0:
        full_wr = (
            full_win
            / (full_win + full_loss)
        ) * 100
    else:
        full_wr = 0

    if early_win + early_loss > 0:
        early_wr = (
            early_win
            / (early_win + early_loss)
        ) * 100
    else:
        early_wr = 0

    if virtual_trade:

        vt = (
            f"""
🟡 OPEN

{virtual_trade["direction"]}

Entry:
{virtual_trade["entry"]:.2f}

SL:
{virtual_trade["sl"]:.2f}

TP:
{virtual_trade["tp"]:.2f}

BE:
{"ON" if virtual_trade["be"] else "OFF"}
"""
        )

    else:
        vt = "⚪ No virtual trade"

    return f"""
📊 {APP_NAME} — STATISTICS

━━━━━━━━━━━━━━━━━━

📌 TOTAL SIGNALS:
{total}

🟢 WIN:
{win}

🔴 LOSS:
{loss}

⚪ BE:
{be}

🟡 OPEN:
{stats.get("open", 0)}

━━━━━━━━━━━━━━━━━━

🎯 CLOSED:
{closed}

📈 WIN RATE:
{win_rate:.1f}%

━━━━━━━━━━━━━━━━━━

🔥 FULL SIGNALS:
{full}

🟢 FULL WIN:
{full_win}

🔴 FULL LOSS:
{full_loss}

🎯 FULL WIN RATE:
{full_wr:.1f}%

━━━━━━━━━━━━━━━━━━

🟡 EARLY SIGNALS:
{early}

🟢 EARLY WIN:
{early_win}

🔴 EARLY LOSS:
{early_loss}

🎯 EARLY WIN RATE:
{early_wr:.1f}%

━━━━━━━━━━━━━━━━━━

{vt}

━━━━━━━━━━━━━━━━━━

🛡 AUTO TRADING:
{"ON" if AUTO_TRADING else "OFF"}

📉 RISK:
{RISK_PERCENT:.1f}%
""".strip()


# =========================================================
# STATUS
# =========================================================

def status_message():

    price = fetch_spot()

    if price is None:
        price_text = "N/A"
    else:
        price_text = f"{price:.2f}"

    virtual_text = "⚪ No virtual trade"

    if virtual_trade:

        virtual_text = f"""
🟡 Virtual trade:

{virtual_trade["direction"]}

Entry:
{virtual_trade["entry"]:.2f}

SL:
{virtual_trade["sl"]:.2f}

TP:
{virtual_trade["tp"]:.2f}

BE:
{"ON" if virtual_trade["be"] else "OFF"}
""".strip()

    return f"""
🥇 {APP_NAME}

🟢 Engine: READY
🟢 Telegram: READY
🟢 XAUS API: READY

💰 XAUUSD:
{price_text}

{virtual_text}

━━━━━━━━━━━━━━━━━━

🛡 AUTO TRADING:
{"ON" if AUTO_TRADING else "OFF"}

📉 Risk:
{RISK_PERCENT:.1f}%

🎯 Min Score:
{MIN_SCORE}

🕯 Closed candles:
{"ON" if CLOSED_CANDLES else "OFF"}

⏱ Poll:
{POLL_SECONDS}s

⏳ Cooldown:
{COOLDOWN_MIN} min
""".strip()


# =========================================================
# TEST
# =========================================================

def test_message():

    price = fetch_spot(force=True)

    if price is None:

        return f"""
⚠️ {APP_NAME}

❌ XAUS API unavailable

Telegram:
🟢 READY

Engine:
🟢 READY

AUTO TRADING:
OFF
""".strip()

    return f"""
🧪 {APP_NAME} TEST

🟢 Engine: READY
🟢 Telegram: READY
🟢 XAUS API: READY

💰 XAUUSD:
{price:.2f}

🛡 AUTO TRADING:
OFF

📉 Risk:
{RISK_PERCENT:.1f}%
""".strip()


# =========================================================
# SIGNAL COMMAND
# =========================================================

def generate_signal():

    price = fetch_spot(force=True)

    market = build_market_data()

    if price is None or market is None:

        return f"""
⚠️ {APP_NAME}

❌ Не удалось получить данные XAU/USD.

Проверь XAUS API.
""".strip()

    analysis = score_signal(market)

    return build_signal_message(
        price,
        analysis
    )


# =========================================================
# TELEGRAM COMMAND HANDLER
# =========================================================

def handle_command(command):

    command = command.strip().lower()

    if command.startswith("/start"):

        return f"""
🥇 {APP_NAME}

🟢 GOLD SMART запущен.

Я анализирую XAU/USD через:

• H4
• H1
• M15
• M5

🧠 Smart Trend Engine
💧 Liquidity
🔨 BOS
🧩 FVG
💥 Displacement
📈 Momentum
📊 RSI
🎯 Premium / Discount

━━━━━━━━━━━━━━━━━━

Команды:

/signal — текущий анализ
/status — состояние бота
/stats — статистика
/test — проверка системы
/help — помощь

🛡 AUTO TRADING: OFF
📉 RISK: 1%
""".strip()

    if command.startswith("/help"):

        return f"""
🥇 {APP_NAME}

📌 КОМАНДЫ

/signal
Текущий анализ XAU/USD

/status
Состояние двигателя, API и виртуальной сделки

/stats
Статистика сигналов

/test
Проверка Telegram + XAUS API

/help
Список команд

━━━━━━━━━━━━━━━━━━

🧠 TREND ENGINE

H4 → H1 → M15 → M5

H4 + H1 формируют
Higher Timeframe Bias.

M5 используется как
основной trigger.

━━━━━━━━━━━━━━━━━━

🛡 AUTO TRADING: OFF
📉 RISK: 1%
""".strip()

    if command.startswith("/status"):
        return status_message()

    if command.startswith("/stats"):
        return stats_message()

    if command.startswith("/test"):
        return test_message()

    if command.startswith("/signal"):
        return generate_signal()

    return f"""
🥇 {APP_NAME}

Неизвестная команда.

Используй:
/signal
/status
/stats
/test
/help
""".strip()


# =========================================================
# ENGINE
# =========================================================

def engine_loop():

    global last_signal_time

    print(
        f"{APP_NAME} engine started"
    )

    while True:

        try:

            price = fetch_spot()

            if price is None:
                print("Price unavailable")
                time.sleep(POLL_SECONDS)
                continue

            # =================================================
            # UPDATE VIRTUAL TRADE
            # =================================================

            if virtual_trade is not None:

                result = update_virtual_trade(
                    price
                )

                if result:
                    print(
                        "Virtual trade closed:",
                        result
                    )

            # =================================================
            # BUILD MARKET
            # =================================================

            market = build_market_data()

            if market is None:

                print(
                    "Market data unavailable"
                )

                time.sleep(
                    POLL_SECONDS
                )

                continue

            # =================================================
            # ANALYSIS
            # =================================================

            analysis = score_signal(
                market
            )

            signal = analysis["signal"]

            print(
                f"[{datetime.now(timezone.utc)}] "
                f"{signal} | "
                f"XAUUSD {price:.2f} | "
                f"BUY {analysis['buy_score']} | "
                f"SELL {analysis['sell_score']} | "
                f"BIAS {analysis['higher_bias']}"
            )

            # =================================================
            # FULL SIGNAL ONLY FOR AUTO VIRTUAL TRADE
            # =================================================

            if signal in ["BUY", "SELL"]:

                now = time.time()

                cooldown_ok = (
                    now - last_signal_time
                    >= COOLDOWN_MIN * 60
                )

                no_open_trade = (
                    virtual_trade is None
                )

                if (
                    cooldown_ok
                    and no_open_trade
                ):

                    message = build_signal_message(
                        price,
                        analysis
                    )

                    sent = send_telegram(
                        message
                    )

                    if sent:

                        last_signal_time = now

                        stats["total_signals"] += 1

                        if signal in [
                            "BUY",
                            "SELL"
                        ]:
                            stats["full_signals"] += 1

                        save_stats(stats)

                        # Open virtual trade
                        open_virtual_trade(
                            direction=signal,
                            price=price,
                            signal_type=signal
                        )

                        send_telegram(
                            f"""
📌 VIRTUAL TRADE OPENED

{"🟢 BUY" if signal == "BUY" else "🔴 SELL"}

💰 Entry:
{price:.2f}

🛑 SL:
{virtual_trade["sl"]:.2f}

🎯 TP:
{virtual_trade["tp"]:.2f}

📐 RR:
1:3

📉 Risk:
{RISK_PERCENT:.1f}%

🛡 AUTO TRADING:
OFF
""".strip()
                        )

            # =================================================
            # EARLY SIGNAL
            # =================================================

            elif signal in [
                "EARLY BUY",
                "EARLY SELL"
            ]:

                now = time.time()

                cooldown_ok = (
                    now - last_signal_time
                    >= COOLDOWN_MIN * 60
                )

                # Early signals are notifications only.
                # They do NOT open virtual trades.

                if cooldown_ok:

                    message = build_signal_message(
                        price,
                        analysis
                    )

                    sent = send_telegram(
                        message
                    )

                    if sent:

                        last_signal_time = now

                        stats["total_signals"] += 1
                        stats["early_signals"] += 1

                        save_stats(stats)

        except Exception as e:

            print(
                "ENGINE ERROR:",
                repr(e)
            )

        time.sleep(
            POLL_SECONDS
        )


# =========================================================
# FLASK
# =========================================================

@app.route("/", methods=["GET"])
def home():

    return {
        "app": APP_NAME,
        "status": "READY",
        "auto_trading": AUTO_TRADING,
        "risk_percent": RISK_PERCENT
    }


@app.route("/health", methods=["GET"])
def health():

    return {
        "status": "ok",
        "app": APP_NAME,
        "time": datetime.now(
            timezone.utc
        ).isoformat()
    }


@app.route("/test", methods=["GET"])
def http_test():

    return test_message()


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

@app.route(
    "/telegram",
    methods=["POST"]
)
def telegram_webhook():

    try:

        update = request.get_json(
            silent=True
        )

        if not update:
            return "OK"

        message = update.get(
            "message"
        )

        if not message:
            return "OK"

        text_message = message.get(
            "text",
            ""
        )

        if not text_message:
            return "OK"

        chat = message.get(
            "chat",
            {}
        )

        incoming_chat_id = str(
            chat.get("id", "")
        )

        # Security:
        # respond only to configured chat
        if CHAT_ID and incoming_chat_id != str(CHAT_ID):
            return "OK"

        response = handle_command(
            text_message
        )

        send_telegram(response)

        return "OK"

    except Exception as e:

        print(
            "Webhook error:",
            repr(e)
        )

        return "OK"


# =========================================================
# START ENGINE
# =========================================================

def start_engine():

    thread = threading.Thread(
        target=engine_loop,
        daemon=True
    )

    thread.start()


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    print("=" * 60)
    print(APP_NAME)
    print("SMART TREND ENGINE")
    print("AUTO TRADING:", AUTO_TRADING)
    print("RISK:", RISK_PERCENT)
    print("SOURCE: XAUS")
    print("=" * 60)

    start_engine()

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
