import os
import threading
import time
import requests
import pandas as pd
import numpy as np

from flask import Flask, request

# =========================================================
# 🥇 GOLD SMART V4.6
# SMART BALANCED SMC + FLEXIBLE CONFIRMATION
#
# XAU/USD
# Telegram Webhook + Twelve Data
# MANUAL SIGNALS ONLY
# AUTO TRADING = OFF
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")

PORT = int(os.environ.get("PORT", "10000"))

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TWELVE_URL = "https://api.twelvedata.com"

app = Flask(__name__)

# =========================================================
# SETTINGS
# =========================================================

# Полноценный сигнал
MIN_SCORE = 70

# Ранний сигнал
EARLY_SCORE = 55

# Risk management
DEFAULT_DEPOSIT = 800.0
RISK_PERCENT = 1.0

MIN_LOT = 0.01
MAX_LOT = 0.02

MAX_DAILY_LOSS_PERCENT = 3.0
MAX_CONSECUTIVE_LOSSES = 2

# RR
MIN_RR = 2.0
TARGET_RR = 3.0

# RSI protection
BUY_RSI_MAX = 70
SELL_RSI_MIN = 30

# SMC memory
LOOKBACK_CONFIRMATION = 5

# BOS
BOS_LOOKBACK = 11

# FVG
FVG_MEMORY = 5

# Session Kazakhstan time
SESSION_START = 12
SESSION_END = 23


# =========================================================
# TELEGRAM
# =========================================================

def telegram_send(chat_id, text):

    if not BOT_TOKEN:
        print("BOT_TOKEN missing")
        return

    try:

        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text
            },
            timeout=15
        )

    except Exception as e:
        print("Telegram error:", e)


# =========================================================
# TWELVE DATA
# =========================================================

def td_request(endpoint, params):

    if not TWELVE_DATA_API_KEY:
        print("Twelve Data API key missing")
        return None

    params = dict(params)
    params["apikey"] = TWELVE_DATA_API_KEY

    try:

        r = requests.get(
            f"{TWELVE_URL}/{endpoint}",
            params=params,
            timeout=20
        )

        data = r.json()

        if data.get("status") == "error":
            print("Twelve Data error:", data)
            return None

        if "code" in data and data.get("code") != 200:
            print("Twelve Data error:", data)
            return None

        return data

    except Exception as e:

        print("Twelve Data request error:", e)
        return None


def get_price():

    data = td_request(
        "price",
        {
            "symbol": "XAU/USD"
        }
    )

    if not data:
        return None

    try:
        return float(data["price"])

    except Exception:
        return None


def get_candles(interval, outputsize=250):

    data = td_request(
        "time_series",
        {
            "symbol": "XAU/USD",
            "interval": interval,
            "outputsize": outputsize,
            "format": "JSON"
        }
    )

    if not data or "values" not in data:
        return None

    try:

        df = pd.DataFrame(data["values"])

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

        df["datetime"] = pd.to_datetime(
            df["datetime"]
        )

        df = df.sort_values(
            "datetime"
        ).reset_index(drop=True)

        df = df.dropna(
            subset=[
                "open",
                "high",
                "low",
                "close"
            ]
        )

        return df

    except Exception as e:

        print("Candle parsing error:", e)
        return None


# =========================================================
# CLOSED CANDLES
# =========================================================

def closed_candles(df):

    if df is None or len(df) < 5:
        return df

    return df.iloc[:-1].copy().reset_index(drop=True)


# =========================================================
# INDICATORS
# =========================================================

def add_indicators(df):

    df = df.copy()

    # EMA 50
    df["ema50"] = df["close"].ewm(
        span=50,
        adjust=False
    ).mean()

    # EMA 200
    df["ema200"] = df["close"].ewm(
        span=200,
        adjust=False
    ).mean()

    # RSI 14
    delta = df["close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()

    rs = avg_gain / avg_loss.replace(
        0,
        np.nan
    )

    df["rsi"] = 100 - (
        100 / (1 + rs)
    )

    # ATR 14
    high_low = (
        df["high"] -
        df["low"]
    )

    high_close = abs(
        df["high"] -
        df["close"].shift()
    )

    low_close = abs(
        df["low"] -
        df["close"].shift()
    )

    tr = pd.concat(
        [
            high_low,
            high_close,
            low_close
        ],
        axis=1
    ).max(axis=1)

    df["tr"] = tr

    df["atr"] = tr.rolling(14).mean()

    return df


# =========================================================
# TREND
# =========================================================

def get_trend(df):

    if df is None or len(df) < 210:
        return "UNKNOWN"

    last = df.iloc[-1]

    if (
        last["close"] > last["ema50"]
        and last["ema50"] > last["ema200"]
    ):
        return "BULLISH"

    if (
        last["close"] < last["ema50"]
        and last["ema50"] < last["ema200"]
    ):
        return "BEARISH"

    return "MIXED"


# =========================================================
# MARKET STRUCTURE
# =========================================================

def get_structure(df, lookback=5):

    if df is None or len(df) < lookback * 3:
        return "UNKNOWN"

    recent = df.tail(
        lookback * 3
    ).copy()

    highs = recent["high"].rolling(
        lookback,
        center=True
    ).max()

    lows = recent["low"].rolling(
        lookback,
        center=True
    ).min()

    swing_highs = recent[
        recent["high"] == highs
    ]["high"]

    swing_lows = recent[
        recent["low"] == lows
    ]["low"]

    if (
        len(swing_highs) < 2
        or len(swing_lows) < 2
    ):
        return "MIXED"

    h1 = swing_highs.iloc[-2]
    h2 = swing_highs.iloc[-1]

    l1 = swing_lows.iloc[-2]
    l2 = swing_lows.iloc[-1]

    if h2 > h1 and l2 > l1:
        return "HH/HL"

    if h2 < h1 and l2 < l1:
        return "LH/LL"

    return "MIXED"


# =========================================================
# LIQUIDITY SWEEP MEMORY
# =========================================================

def detect_liquidity_memory(
    df,
    lookback=5
):

    if df is None or len(df) < 15:
        return "NONE"

    recent_results = []

    start = max(
        8,
        len(df) - lookback
    )

    for i in range(start, len(df)):

        candle = df.iloc[i]

        previous_high = df[
            "high"
        ].iloc[
            max(0, i - 7):i
        ].max()

        previous_low = df[
            "low"
        ].iloc[
            max(0, i - 7):i
        ].min()

        if (
            candle["high"] > previous_high
            and candle["close"] < previous_high
        ):
            recent_results.append(
                "BSL SWEEP"
            )

        elif (
            candle["low"] < previous_low
            and candle["close"] > previous_low
        ):
            recent_results.append(
                "SSL SWEEP"
            )

    if not recent_results:
        return "NONE"

    return recent_results[-1]


# =========================================================
# FVG MEMORY
# =========================================================

def detect_fvg_memory(
    df,
    memory=5
):

    if df is None or len(df) < 6:
        return "NONE"

    results = []

    start = max(
        2,
        len(df) - memory - 2
    )

    for i in range(start, len(df)):

        a = df.iloc[i - 2]
        c = df.iloc[i]

        # Bullish FVG
        if a["high"] < c["low"]:

            results.append(
                "BULLISH FVG"
            )

        # Bearish FVG
        elif a["low"] > c["high"]:

            results.append(
                "BEARISH FVG"
            )

    if not results:
        return "NONE"

    return results[-1]


# =========================================================
# BOS / CHoCH
# =========================================================

def detect_structure_break(df):

    if df is None or len(df) < 20:
        return "NONE"

    last = df.iloc[-1]

    previous_high = df[
        "high"
    ].iloc[-(BOS_LOOKBACK + 1):-1].max()

    previous_low = df[
        "low"
    ].iloc[-(BOS_LOOKBACK + 1):-1].min()

    if last["close"] > previous_high:
        return "BULLISH BOS"

    if last["close"] < previous_low:
        return "BEARISH BOS"

    return "NONE"


# =========================================================
# DISPLACEMENT
# =========================================================

def detect_displacement(df):

    if df is None or len(df) < 20:
        return "NONE"

    last = df.iloc[-1]

    atr = last["atr"]

    if not np.isfinite(atr) or atr <= 0:
        return "NONE"

    body = abs(
        last["close"] -
        last["open"]
    )

    bullish = (
        last["close"] > last["open"]
        and body >= atr * 0.70
    )

    bearish = (
        last["close"] < last["open"]
        and body >= atr * 0.70
    )

    if bullish:
        return "BULLISH"

    if bearish:
        return "BEARISH"

    return "NONE"


# =========================================================
# MOMENTUM
# =========================================================

def get_momentum(df):

    if df is None or len(df) < 10:
        return "UNKNOWN"

    c1 = df["close"].iloc[-1]
    c5 = df["close"].iloc[-6]

    if c1 > c5:
        return "BULLISH"

    if c1 < c5:
        return "BEARISH"

    return "NEUTRAL"


# =========================================================
# PDH / PDL
# =========================================================

def get_previous_day_levels(df):

    if df is None or len(df) < 30:
        return None, None

    data = df.copy()

    data["date"] = data["datetime"].dt.date

    unique_dates = sorted(
        data["date"].unique()
    )

    if len(unique_dates) < 2:
        return None, None

    previous_day = unique_dates[-2]

    day_data = data[
        data["date"] == previous_day
    ]

    if day_data.empty:
        return None, None

    pdh = day_data["high"].max()
    pdl = day_data["low"].min()

    return pdh, pdl


# =========================================================
# PREMIUM / DISCOUNT
# =========================================================

def get_premium_discount(df):

    if df is None or len(df) < 20:
        return "UNKNOWN", None, None

    recent_high = df[
        "high"
    ].iloc[-50:].max()

    recent_low = df[
        "low"
    ].iloc[-50:].min()

    if recent_high <= recent_low:
        return "UNKNOWN", None, None

    equilibrium = (
        recent_high +
        recent_low
    ) / 2

    price = df["close"].iloc[-1]

    if price < equilibrium:

        return (
            "DISCOUNT",
            recent_low,
            recent_high
        )

    return (
        "PREMIUM",
        recent_low,
        recent_high
    )


# =========================================================
# ATR VOLATILITY
# =========================================================

def volatility_state(df):

    if df is None or len(df) < 30:
        return "UNKNOWN"

    atr = df["atr"].iloc[-1]

    atr_avg = df[
        "atr"
    ].iloc[-20:].mean()

    if not np.isfinite(atr):
        return "UNKNOWN"

    if atr > atr_avg * 1.8:
        return "HIGH"

    if atr < atr_avg * 0.60:
        return "LOW"

    return "NORMAL"


# =========================================================
# RISK / LOT CALCULATION
# =========================================================

def calculate_risk(
    deposit,
    risk_percent
):

    return (
        deposit *
        risk_percent /
        100
    )


def calculate_lot(
    deposit,
    risk_percent,
    sl_distance
):

    if sl_distance <= 0:
        return MIN_LOT

    risk_money = calculate_risk(
        deposit,
        risk_percent
    )

    value_per_price_unit = 100.0

    raw_lot = (
        risk_money /
        (
            sl_distance *
            value_per_price_unit
        )
    )

    lot = max(
        MIN_LOT,
        min(
            MAX_LOT,
            raw_lot
        )
    )

    return round(
        lot,
        2
    )


# =========================================================
# SCORE V4.6
# =========================================================

def calculate_quality_score(
    direction,
    h4_trend,
    h1_trend,
    m15_trend,
    h1_structure,
    m15_structure,
    liquidity,
    fvg,
    bos,
    displacement,
    momentum,
    premium_discount,
    rr
):

    score = 0

    # =====================================================
    # TREND
    # =====================================================

    if direction == "BUY":

        if h4_trend == "BULLISH":
            score += 15

        elif h4_trend == "MIXED":
            score += 5

        if h1_trend == "BULLISH":
            score += 15

        elif h1_trend == "MIXED":
            score += 5

    else:

        if h4_trend == "BEARISH":
            score += 15

        elif h4_trend == "MIXED":
            score += 5

        if h1_trend == "BEARISH":
            score += 15

        elif h1_trend == "MIXED":
            score += 5

    # =====================================================
    # LIQUIDITY
    # =====================================================

    if (
        direction == "BUY"
        and liquidity == "SSL SWEEP"
    ):
        score += 15

    if (
        direction == "SELL"
        and liquidity == "BSL SWEEP"
    ):
        score += 15

    # =====================================================
    # BOS
    # =====================================================

    if (
        direction == "BUY"
        and bos == "BULLISH BOS"
    ):
        score += 15

    if (
        direction == "SELL"
        and bos == "BEARISH BOS"
    ):
        score += 15

    # =====================================================
    # FVG
    # =====================================================

    if (
        direction == "BUY"
        and fvg == "BULLISH FVG"
    ):
        score += 10

    if (
        direction == "SELL"
        and fvg == "BEARISH FVG"
    ):
        score += 10

    # =====================================================
    # DISPLACEMENT
    # =====================================================

    if (
        direction == "BUY"
        and displacement == "BULLISH"
    ):
        score += 10

    if (
        direction == "SELL"
        and displacement == "BEARISH"
    ):
        score += 10

    # =====================================================
    # M15 STRUCTURE
    # =====================================================

    if (
        direction == "BUY"
        and m15_structure == "HH/HL"
    ):
        score += 5

    if (
        direction == "SELL"
        and m15_structure == "LH/LL"
    ):
        score += 5

    # =====================================================
    # MOMENTUM
    # =====================================================

    if (
        direction == "BUY"
        and momentum == "BULLISH"
    ):
        score += 5

    if (
        direction == "SELL"
        and momentum == "BEARISH"
    ):
        score += 5

    # =====================================================
    # PREMIUM / DISCOUNT
    # =====================================================

    if (
        direction == "BUY"
        and premium_discount == "DISCOUNT"
    ):
        score += 5

    if (
        direction == "SELL"
        and premium_discount == "PREMIUM"
    ):
        score += 5

    # =====================================================
    # RR
    # =====================================================

    if rr >= 3:
        score += 5

    elif rr >= 2:
        score += 3

    return min(
        100,
        score
    )


# =========================================================
# SIGNAL GRADE
# =========================================================

def signal_grade(score):

    if score >= 85:
        return "A+"

    if score >= 70:
        return "A"

    if score >= 55:
        return "B"

    return "WAIT"


# =========================================================
# ANALYSIS
# =========================================================

def analyze():

    price = get_price()

    if price is None:

        return {
            "signal": "NO TRADE",
            "reason": "Не удалось получить цену XAU/USD."
        }

    h4 = get_candles("4h")
    h1 = get_candles("1h")
    m15 = get_candles("15min")

    if (
        h4 is None
        or h1 is None
        or m15 is None
    ):

        return {
            "signal": "NO TRADE",
            "reason": "Недостаточно рыночных данных."
        }

    # Closed candles only

    h4 = closed_candles(h4)
    h1 = closed_candles(h1)
    m15 = closed_candles(m15)

    # Indicators

    h4 = add_indicators(h4)
    h1 = add_indicators(h1)
    m15 = add_indicators(m15)

    if (
        len(h4) < 210
        or len(h1) < 210
        or len(m15) < 210
    ):

        return {
            "signal": "NO TRADE",
            "reason": "Недостаточно свечей для EMA200."
        }

    # =====================================================
    # TRENDS
    # =====================================================

    h4_trend = get_trend(h4)
    h1_trend = get_trend(h1)
    m15_trend = get_trend(m15)

    # =====================================================
    # STRUCTURE
    # =====================================================

    h1_structure = get_structure(h1)
    m15_structure = get_structure(m15)

    # =====================================================
    # SMC
    # =====================================================

    liquidity = detect_liquidity_memory(
        m15,
        LOOKBACK_CONFIRMATION
    )

    fvg = detect_fvg_memory(
        m15,
        FVG_MEMORY
    )

    bos = detect_structure_break(
        m15
    )

    displacement = detect_displacement(
        m15
    )

    momentum = get_momentum(
        m15
    )

    # =====================================================
    # RSI / ATR
    # =====================================================

    rsi = float(
        m15["rsi"].iloc[-1]
    )

    atr = float(
        m15["atr"].iloc[-1]
    )

    if not np.isfinite(atr) or atr <= 0:

        return {
            "signal": "NO TRADE",
            "reason": "ATR недоступен."
        }

    # =====================================================
    # PDH / PDL
    # =====================================================

    pdh, pdl = get_previous_day_levels(
        m15
    )

    # =====================================================
    # PREMIUM / DISCOUNT
    # =====================================================

    (
        premium_discount,
        swing_low,
        swing_high
    ) = get_premium_discount(m15)

    # =====================================================
    # VOLATILITY
    # =====================================================

    volatility = volatility_state(
        m15
    )

    # =====================================================
    # CANDIDATES
    # =====================================================

    # Основное направление
    buy_trend = (
        h4_trend == "BULLISH"
        or (
            h4_trend == "MIXED"
            and h1_trend == "BULLISH"
        )
    )

    sell_trend = (
        h4_trend == "BEARISH"
        or (
            h4_trend == "MIXED"
            and h1_trend == "BEARISH"
        )
    )

    # Reversal
    buy_reversal = (
        liquidity == "SSL SWEEP"
        and (
            fvg == "BULLISH FVG"
            or displacement == "BULLISH"
            or momentum == "BULLISH"
        )
    )

    sell_reversal = (
        liquidity == "BSL SWEEP"
        and (
            fvg == "BEARISH FVG"
            or displacement == "BEARISH"
            or momentum == "BEARISH"
        )
    )

    buy_candidate = (
        buy_trend
        or buy_reversal
    )

    sell_candidate = (
        sell_trend
        or sell_reversal
    )

    # =====================================================
    # TRADE LEVELS
    # =====================================================

    buy_sl = None
    buy_tp1 = None
    buy_tp2 = None

    sell_sl = None
    sell_tp1 = None
    sell_tp2 = None

    buy_rr = 0
    sell_rr = 0

    # BUY
    if buy_candidate:

        recent_low = m15[
            "low"
        ].iloc[-12:].min()

        buy_sl = min(
            recent_low - atr * 0.20,
            price - atr * 1.20
        )

        risk = price - buy_sl

        if risk > 0:

            buy_tp1 = price + risk * 2.0
            buy_tp2 = price + risk * 3.0

            buy_rr = 3.0

    # SELL
    if sell_candidate:

        recent_high = m15[
            "high"
        ].iloc[-12:].max()

        sell_sl = max(
            recent_high + atr * 0.20,
            price + atr * 1.20
        )

        risk = sell_sl - price

        if risk > 0:

            sell_tp1 = price - risk * 2.0
            sell_tp2 = price - risk * 3.0

            sell_rr = 3.0

    # =====================================================
    # SCORES
    # =====================================================

    buy_score = calculate_quality_score(
        "BUY",
        h4_trend,
        h1_trend,
        m15_trend,
        h1_structure,
        m15_structure,
        liquidity,
        fvg,
        bos,
        displacement,
        momentum,
        premium_discount,
        buy_rr
    )

    sell_score = calculate_quality_score(
        "SELL",
        h4_trend,
        h1_trend,
        m15_trend,
        h1_structure,
        m15_structure,
        liquidity,
        fvg,
        bos,
        displacement,
        momentum,
        premium_discount,
        sell_rr
    )

    buy_grade = signal_grade(
        buy_score
    )

    sell_grade = signal_grade(
        sell_score
    )

    # =====================================================
    # RSI
    # =====================================================

    buy_rsi_ok = (
        rsi < BUY_RSI_MAX
    )

    sell_rsi_ok = (
        rsi > SELL_RSI_MIN
    )

    # =====================================================
    # FULL CONFIRMATION
    # =====================================================

    buy_full = (
        buy_candidate
        and buy_score >= MIN_SCORE
        and buy_rsi_ok
        and buy_rr >= MIN_RR
        and (
            bos == "BULLISH BOS"
            or (
                liquidity == "SSL SWEEP"
                and fvg == "BULLISH FVG"
                and displacement == "BULLISH"
            )
        )
    )

    sell_full = (
        sell_candidate
        and sell_score >= MIN_SCORE
        and sell_rsi_ok
        and sell_rr >= MIN_RR
        and (
            bos == "BEARISH BOS"
            or (
                liquidity == "BSL SWEEP"
                and fvg == "BEARISH FVG"
                and displacement == "BEARISH"
            )
        )
    )

    # =====================================================
    # EARLY CONFIRMATION
    # =====================================================

    buy_early_setup = (
        buy_candidate
        and buy_score >= EARLY_SCORE
        and buy_rsi_ok
        and buy_rr >= MIN_RR
        and (
            (
                liquidity == "SSL SWEEP"
                and fvg == "BULLISH FVG"
            )
            or (
                liquidity == "SSL SWEEP"
                and displacement == "BULLISH"
            )
            or (
                fvg == "BULLISH FVG"
                and displacement == "BULLISH"
                and momentum == "BULLISH"
            )
            or (
                bos == "BULLISH BOS"
                and momentum == "BULLISH"
            )
        )
    )

    sell_early_setup = (
        sell_candidate
        and sell_score >= EARLY_SCORE
        and sell_rsi_ok
        and sell_rr >= MIN_RR
        and (
            (
                liquidity == "BSL SWEEP"
                and fvg == "BEARISH FVG"
            )
            or (
                liquidity == "BSL SWEEP"
                and displacement == "BEARISH"
            )
            or (
                fvg == "BEARISH FVG"
                and displacement == "BEARISH"
                and momentum == "BEARISH"
            )
            or (
                bos == "BEARISH BOS"
                and momentum == "BEARISH"
            )
        )
    )

    # =====================================================
    # FINAL SIGNAL
    # =====================================================

    signal = "WAIT"
    reason = ""

    # FULL BUY
    if buy_full and not sell_full:

        signal = "BUY"

    # FULL SELL
    elif sell_full and not buy_full:

        signal = "SELL"

    # EARLY BUY
    elif (
        buy_early_setup
        and not sell_early_setup
        and not sell_full
    ):

        signal = "EARLY BUY"

    # EARLY SELL
    elif (
        sell_early_setup
        and not buy_early_setup
        and not buy_full
    ):

        signal = "EARLY SELL"

    # CONFLICT
    elif (
        (
            buy_full
            or buy_early_setup
        )
        and
        (
            sell_full
            or sell_early_setup
        )
    ):

        signal = "WAIT"

        reason = (
            "конфликт BUY/SELL подтверждений"
        )

    else:

        reasons = []

        if not (
            buy_candidate
            or sell_candidate
        ):

            reasons.append(
                "нет направления"
            )

        if (
            liquidity == "NONE"
            and fvg == "NONE"
            and bos == "NONE"
        ):

            reasons.append(
                "нет Liquidity/FVG/BOS"
            )

        if (
            buy_score < EARLY_SCORE
            and sell_score < EARLY_SCORE
        ):

            reasons.append(
                f"score ниже {EARLY_SCORE}"
            )

        if volatility == "HIGH":

            reasons.append(
                "высокая волатильность"
            )

        if volatility == "LOW":

            reasons.append(
                "низкая волатильность"
            )

        if not reasons:

            reasons.append(
                "ожидание подтверждения"
            )

        reason = "; ".join(
            reasons
        )

    # =====================================================
    # FINAL SCORE
    # =====================================================

    if signal in [
        "BUY",
        "EARLY BUY"
    ]:

        final_score = buy_score
        grade = buy_grade

        sl = buy_sl
        tp1 = buy_tp1
        tp2 = buy_tp2

        rr = buy_rr

        lot = calculate_lot(
            DEFAULT_DEPOSIT,
            RISK_PERCENT,
            abs(price - sl)
        )

    elif signal in [
        "SELL",
        "EARLY SELL"
    ]:

        final_score = sell_score
        grade = sell_grade

        sl = sell_sl
        tp1 = sell_tp1
        tp2 = sell_tp2

        rr = sell_rr

        lot = calculate_lot(
            DEFAULT_DEPOSIT,
            RISK_PERCENT,
            abs(sl - price)
        )

    else:

        final_score = max(
            buy_score,
            sell_score
        )

        grade = signal_grade(
            final_score
        )

        sl = None
        tp1 = None
        tp2 = None

        rr = 0
        lot = 0.0

    # =====================================================
    # RESULT
    # =====================================================

    return {

        "signal": signal,

        "price": price,

        "h4": h4_trend,
        "h1": h1_trend,
        "m15": m15_trend,

        "h1_structure": h1_structure,
        "m15_structure": m15_structure,

        "liquidity": liquidity,
        "fvg": fvg,
        "bos": bos,

        "displacement": displacement,
        "momentum": momentum,

        "rsi": rsi,
        "atr": atr,

        "volatility": volatility,

        "pdh": pdh,
        "pdl": pdl,

        "premium_discount":
            premium_discount,

        "buy_score":
            buy_score,

        "sell_score":
            sell_score,

        "buy_grade":
            buy_grade,

        "sell_grade":
            sell_grade,

        "final_score":
            final_score,

        "grade":
            grade,

        "sl":
            sl,

        "tp1":
            tp1,

        "tp2":
            tp2,

        "rr":
            rr,

        "lot":
            lot,

        "risk_percent":
            RISK_PERCENT,

        "risk_money":
            calculate_risk(
                DEFAULT_DEPOSIT,
                RISK_PERCENT
            ),

        "reason":
            reason
    }


# =========================================================
# FORMAT MESSAGE
# =========================================================

def format_signal(a):

    if a["signal"] == "NO TRADE":

        return (
            "🥇 GOLD SMART V4.6\n\n"
            "⛔ NO TRADE\n"
            f"Причина: {a['reason']}"
        )

    signal = a["signal"]

    if signal == "BUY":

        signal_text = "🟢 SIGNAL: BUY"

    elif signal == "SELL":

        signal_text = "🔴 SIGNAL: SELL"

    elif signal == "EARLY BUY":

        signal_text = "🟡 EARLY BUY"

    elif signal == "EARLY SELL":

        signal_text = "🟠 EARLY SELL"

    else:

        signal_text = "⚪ SIGNAL: WAIT"

    text = (

        "🥇 GOLD SMART V4.6\n\n"

        f"💰 XAUUSD: "
        f"{a['price']:.2f}\n\n"

        f"📊 H4: {a['h4']}\n"
        f"📊 H1: {a['h1']}\n"
        f"📊 M15: {a['m15']}\n\n"

        f"💧 Liquidity: "
        f"{a['liquidity']}\n"

        f"🧩 FVG: "
        f"{a['fvg']}\n"

        f"🔨 BOS: "
        f"{a['bos']}\n"

        f"💥 Displacement: "
        f"{a['displacement']}\n"

        f"📐 Zone: "
        f"{a['premium_discount']}\n"

        f"RSI: "
        f"{a['rsi']:.1f}\n\n"

        f"🟢 BUY: "
        f"{a['buy_score']}/100\n"

        f"🔴 SELL: "
        f"{a['sell_score']}/100\n\n"

        f"{signal_text}\n"
    )

    # =====================================================
    # TRADE INFORMATION
    # =====================================================

    if signal in [
        "BUY",
        "SELL",
        "EARLY BUY",
        "EARLY SELL"
    ]:

        text += (

            "\n"

            f"⭐ Quality: "
            f"{a['grade']}\n"

            f"📊 Score: "
            f"{a['final_score']}/100\n\n"

            f"🎯 Entry: "
            f"{a['price']:.2f}\n"

            f"🛡 SL: "
            f"{a['sl']:.2f}\n"

            f"🎯 TP1: "
            f"{a['tp1']:.2f}\n"

            f"🎯 TP2: "
            f"{a['tp2']:.2f}\n"

            f"⚖️ RR: "
            f"1:{a['rr']:.1f}\n"

            f"💵 Risk: "
            f"{a['risk_percent']:.1f}% "
            f"≈ ${a['risk_money']:.2f}\n"

            f"📦 Lot: "
            f"{a['lot']:.2f}\n"
        )

        if signal in [
            "EARLY BUY",
            "EARLY SELL"
        ]:

            text += (
                "\n⚠️ EARLY: "
                "подтверждение ещё развивается."
            )

    else:

        text += (

            "\n"

            f"⭐ Quality: "
            f"{a['grade']}\n"

            f"⏳ "
            f"{a['reason']}\n"
        )

    return text


# =========================================================
# COMMANDS
# =========================================================

def process_message(message):

    chat = message.get(
        "chat",
        {}
    )

    chat_id = chat.get(
        "id"
    )

    if not chat_id:
        return

    text = message.get(
        "text",
        ""
    ).strip().lower()

    # =====================================================
    # START
    # =====================================================

    if text in [
        "/start",
        "start"
    ]:

        telegram_send(

            chat_id,

            "🥇 GOLD SMART V4.6\n\n"

            "Бот запущен.\n\n"

            "Команда:\n"
            "🟡 /gold — анализ XAU/USD\n\n"

            "🧠 SMART FLEXIBLE SMC\n"
            "📊 FULL SIGNAL ≥ 70\n"
            "🟡 EARLY SIGNAL ≥ 55\n"
            "🧮 Математический фильтр ON\n"
            "🛡 Risk Management ON\n"
            "🚫 Автоторговля OFF."
        )

        return

    # =====================================================
    # GOLD
    # =====================================================

    if text in [
        "/gold",
        "gold",
        "золото"
    ]:

        telegram_send(

            chat_id,

            "⏳ GOLD SMART V4.6\n"
            "SMC + Flexible Confirmation...\n"
            "Анализ XAU/USD..."
        )

        try:

            analysis = analyze()

            result = format_signal(
                analysis
            )

            telegram_send(
                chat_id,
                result
            )

        except Exception as e:

            print(
                "Analysis error:",
                e
            )

            telegram_send(

                chat_id,

                "⛔ Ошибка анализа.\n\n"
                "Попробуй ещё раз через несколько секунд."
            )

        return

    # =====================================================
    # UNKNOWN
    # =====================================================

    telegram_send(

        chat_id,

        "Используй /gold "
        "для анализа золота."
    )


# =========================================================
# WEBHOOK
# =========================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():

    return "Gold Smart V4.6 is running."


@app.route(
    "/health",
    methods=["GET"]
)
def health():

    return "OK"


@app.route(
    "/telegram",
    methods=["POST"]
)
def telegram_webhook():

    try:

        update = request.get_json(
            silent=True
        )

        if (
            update
            and "message" in update
        ):

            process_message(
                update["message"]
            )

    except Exception as e:

        print(
            "Webhook error:",
            e
        )

    return "OK", 200


# =========================================================
# SET WEBHOOK
# =========================================================

def setup_webhook():

    time.sleep(5)

    render_url = os.environ.get(
        "RENDER_EXTERNAL_URL"
    )

    if not render_url:

        print(
            "RENDER_EXTERNAL_URL not found"
        )

        return

    webhook_url = (
        f"{render_url}/telegram"
    )

    try:

        response = requests.post(

            f"{TELEGRAM_API}/setWebhook",

            json={
                "url": webhook_url,
                "allowed_updates": [
                    "message"
                ]
            },

            timeout=15
        )

        print(
            "Webhook setup:",
            response.text
        )

    except Exception as e:

        print(
            "Webhook setup error:",
            e
        )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    threading.Thread(
        target=setup_webhook,
        daemon=True
    ).start()

    app.run(
        host="0.0.0.0",
        port=PORT
    )
