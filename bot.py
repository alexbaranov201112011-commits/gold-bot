import os
import json
import time
import threading
import requests
import pandas as pd
import numpy as np

from datetime import datetime, timezone
from flask import Flask, request


# =========================================================
# 🥇 GOLD SMART V5.1 M5
#
# H4 → H1 → M15 → M5
#
# XAU/USD
# Telegram
# Twelve Data
#
# AUTO TRADING = OFF
# RISK = 1%
# =========================================================


# =========================================================
# ENV
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TWELVE_DATA_API_KEY = os.environ.get("TWELVE_DATA_API_KEY")

PORT = int(os.environ.get("PORT", "10000"))

# Если Render_EXTERNAL_URL отсутствует,
# можно задать WEBHOOK_URL вручную.
WEBHOOK_URL = (
    os.environ.get("WEBHOOK_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or "https://gold-bot-q8la.onrender.com"
)

TELEGRAM_API = (
    f"https://api.telegram.org/bot{BOT_TOKEN}"
    if BOT_TOKEN
    else ""
)

TWELVE_URL = "https://api.twelvedata.com"


# =========================================================
# FLASK
# =========================================================

app = Flask(__name__)


# =========================================================
# SETTINGS
# =========================================================

SYMBOL = "XAU/USD"

# Основной сигнал
MIN_SCORE = 70

# Ранний сигнал
EARLY_SCORE = 60

DEFAULT_DEPOSIT = 800.0
RISK_PERCENT = 1.0

MIN_LOT = 0.01
MAX_LOT = 0.02

MIN_RR = 2.0
TARGET_RR = 3.0

BUY_RSI_MAX = 70
SELL_RSI_MIN = 30

BOS_LOOKBACK = 11
FVG_MEMORY = 5

STATS_FILE = "gold_stats.json"

# M5 scanning
CHECK_INTERVAL = 30
M5_LOOKBACK = 150
M15_LOOKBACK = 150

# Не отправлять одинаковый сигнал повторно
SIGNAL_COOLDOWN_MINUTES = 15


# =========================================================
# GLOBAL STATE
# =========================================================

state_lock = threading.Lock()

last_m5_candle = None
last_signal_key = None
last_signal_time = 0

engine_started = False


# =========================================================
# TELEGRAM SEND
# =========================================================

def telegram_send(chat_id, text):

    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN is not set")
        return False

    if not chat_id:
        print("ERROR: Telegram chat_id is empty")
        return False

    try:

        response = requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text
            },
            timeout=15
        )

        print(
            "Telegram:",
            response.status_code,
            response.text[:300]
        )

        return response.ok

    except Exception as e:

        print(
            "Telegram error:",
            e
        )

        return False


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

def setup_webhook():

    if not BOT_TOKEN:

        print(
            "WEBHOOK: BOT_TOKEN missing"
        )

        return False

    webhook = (
        WEBHOOK_URL.rstrip("/")
        + "/telegram"
    )

    try:

        response = requests.post(
            f"{TELEGRAM_API}/setWebhook",
            json={
                "url": webhook,
                "drop_pending_updates": False
            },
            timeout=20
        )

        print(
            "Webhook setup:",
            response.status_code,
            response.text[:500]
        )

        # Проверяем webhook
        info = requests.get(
            f"{TELEGRAM_API}/getWebhookInfo",
            timeout=15
        )

        print(
            "Webhook info:",
            info.status_code,
            info.text[:1000]
        )

        return response.ok

    except Exception as e:

        print(
            "Webhook error:",
            e
        )

        return False


# =========================================================
# TELEGRAM COMMANDS
# =========================================================

def telegram_help():

    return """
🥇 GOLD SMART V5.1

Команды:

/start — запуск бота
/help — список команд
/gold — текущий анализ XAUUSD
/stats — статистика сигналов
/status — состояние бота
/test — проверка Telegram

Таймфрейм входа:
M5

Контекст:
H4 → H1 → M15 → M5

AUTO TRADING:
OFF
"""


def telegram_status():

    with state_lock:

        candle = last_m5_candle

    stats = load_stats()

    open_count = sum(
        1
        for x in stats
        if x.get("status") == "OPEN"
    )

    return f"""
🥇 GOLD SMART V5.1

🟢 ENGINE: RUNNING

📊 TIMEFRAME: M5
📊 CONTEXT: H4 → H1 → M15 → M5

🤖 AUTO TRADING: OFF

📂 TOTAL SIGNALS: {len(stats)}
🟡 OPEN: {open_count}

🕯 LAST M5 CANDLE:
{candle or "нет данных"}
"""


@app.route("/telegram", methods=["POST"])
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

        chat = message.get(
            "chat",
            {}
        )

        chat_id = chat.get(
            "id"
        )

        text = (
            message.get(
                "text",
                ""
            )
            .strip()
        )

        if not chat_id:
            return "OK"

        command = text.lower().split()[0] if text else ""

        # /start
        if command == "/start":

            telegram_send(
                chat_id,
                """
🥇 GOLD SMART V5.1

Бот запущен.

📊 H4 → H1 → M15 → M5
🎯 Основной вход: M5
💵 Risk: 1%
🤖 AUTO TRADING: OFF

Используй:
/gold
/stats
/status
/help
"""
            )

        # /help
        elif command == "/help":

            telegram_send(
                chat_id,
                telegram_help()
            )

        # /gold
        elif command == "/gold":

            result = analyze()

            telegram_send(
                chat_id,
                format_signal(result)
            )

        # /stats
        elif command == "/stats":

            telegram_send(
                chat_id,
                format_stats()
            )

        # /status
        elif command == "/status":

            telegram_send(
                chat_id,
                telegram_status()
            )

        # /test
        elif command == "/test":

            telegram_send(
                chat_id,
                """
✅ TELEGRAM TEST OK

🥇 GOLD SMART V5.1
M5 ENGINE ONLINE
AUTO TRADING OFF
"""
            )

        else:

            telegram_send(
                chat_id,
                "Используй /help для списка команд."
            )

        return "OK"

    except Exception as e:

        print(
            "Webhook processing error:",
            e
        )

        return "OK"


# =========================================================
# ROOT
# =========================================================

@app.route("/", methods=["GET"])
def home():

    return """
🥇 GOLD SMART V5.1

STATUS: ONLINE
TIMEFRAME: M5
AUTO TRADING: OFF
"""


# =========================================================
# HEALTH
# =========================================================

@app.route("/health", methods=["GET"])
def health():

    return {
        "status": "ok",
        "engine": "M5",
        "auto_trading": False
    }


# =========================================================
# TWELVE DATA
# =========================================================

def td_request(
    endpoint,
    params=None
):

    if not TWELVE_DATA_API_KEY:

        print(
            "ERROR: TWELVE_DATA_API_KEY is not set"
        )

        return None

    params = params or {}

    params["apikey"] = (
        TWELVE_DATA_API_KEY
    )

    try:

        response = requests.get(
            f"{TWELVE_URL}/{endpoint}",
            params=params,
            timeout=20
        )

        data = response.json()

        if (
            isinstance(data, dict)
            and data.get("status") == "error"
        ):

            print(
                "Twelve Data error:",
                data
            )

            return None

        return data

    except Exception as e:

        print(
            "Twelve Data error:",
            e
        )

        return None


# =========================================================
# PRICE
# =========================================================

def get_price():

    data = td_request(
        "price",
        {
            "symbol": SYMBOL
        }
    )

    if not data:
        return None

    try:

        return float(
            data["price"]
        )

    except Exception:

        return None


# =========================================================
# CANDLES
# =========================================================

def get_candles(
    interval,
    outputsize=200
):

    data = td_request(
        "time_series",
        {
            "symbol": SYMBOL,
            "interval": interval,
            "outputsize": outputsize,
            "format": "JSON"
        }
    )

    if not data:
        return None

    values = data.get(
        "values"
    )

    if not values:
        return None

    df = pd.DataFrame(
        values
    )

    required = [
        "datetime",
        "open",
        "high",
        "low",
        "close"
    ]

    for column in required:

        if column not in df.columns:
            return None

    for column in [
        "open",
        "high",
        "low",
        "close"
    ]:

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    df["datetime"] = pd.to_datetime(
        df["datetime"],
        errors="coerce"
    )

    df = df.dropna(
        subset=[
            "datetime",
            "open",
            "high",
            "low",
            "close"
        ]
    )

    df = df.sort_values(
        "datetime"
    ).reset_index(
        drop=True
    )

    return df


# =========================================================
# CLOSED CANDLES
# =========================================================

def closed_candles(df):

    if (
        df is None
        or len(df) < 3
    ):

        return None

    # Последняя свеча может быть текущей.
    # Используем только полностью закрытые свечи.

    return (
        df.iloc[:-1]
        .copy()
        .reset_index(drop=True)
    )


# =========================================================
# INDICATORS
# =========================================================

def add_indicators(df):

    df = df.copy()

    # EMA 50
    df["EMA50"] = (
        df["close"]
        .ewm(
            span=50,
            adjust=False
        )
        .mean()
    )

    # EMA 200
    df["EMA200"] = (
        df["close"]
        .ewm(
            span=200,
            adjust=False
        )
        .mean()
    )

    # RSI
    delta = df["close"].diff()

    gain = delta.clip(
        lower=0
    )

    loss = -delta.clip(
        upper=0
    )

    avg_gain = gain.rolling(
        14
    ).mean()

    avg_loss = loss.rolling(
        14
    ).mean()

    rs = (
        avg_gain /
        avg_loss.replace(
            0,
            np.nan
        )
    )

    df["RSI"] = (
        100 -
        (
            100 /
            (1 + rs)
        )
    )

    # ATR
    previous_close = (
        df["close"].shift(1)
    )

    tr1 = (
        df["high"] -
        df["low"]
    )

    tr2 = (
        df["high"] -
        previous_close
    ).abs()

    tr3 = (
        df["low"] -
        previous_close
    ).abs()

    true_range = pd.concat(
        [
            tr1,
            tr2,
            tr3
        ],
        axis=1
    ).max(
        axis=1
    )

    df["ATR"] = (
        true_range
        .rolling(14)
        .mean()
    )

    return df


# =========================================================
# TREND
# =========================================================

def get_trend(df):

    if len(df) < 200:
        return "MIXED"

    last = df.iloc[-1]

    close = last["close"]
    ema50 = last["EMA50"]
    ema200 = last["EMA200"]

    if close > ema50 > ema200:
        return "BULLISH"

    if close < ema50 < ema200:
        return "BEARISH"

    return "MIXED"


# =========================================================
# STRUCTURE
# =========================================================

def get_structure(
    df,
    lookback=10
):

    if len(df) < lookback + 2:
        return "MIXED"

    recent = df.tail(
        lookback
    )

    highs = recent[
        "high"
    ].values

    lows = recent[
        "low"
    ].values

    if (
        highs[-1] > highs[-2]
        and
        lows[-1] > lows[-2]
    ):

        return "BULLISH"

    if (
        highs[-1] < highs[-2]
        and
        lows[-1] < lows[-2]
    ):

        return "BEARISH"

    return "MIXED"


# =========================================================
# LIQUIDITY
# =========================================================

def get_liquidity(df):

    if len(df) < 5:
        return "NONE"

    last = df.iloc[-1]

    previous_high = (
        df["high"]
        .iloc[-5:-1]
        .max()
    )

    previous_low = (
        df["low"]
        .iloc[-5:-1]
        .min()
    )

    if (
        last["high"] > previous_high
        and
        last["close"] < previous_high
    ):

        return "BSL SWEEP"

    if (
        last["low"] < previous_low
        and
        last["close"] > previous_low
    ):

        return "SSL SWEEP"

    return "NONE"


# =========================================================
# FVG
# =========================================================

def get_fvg(df):

    if len(df) < 3:
        return "NONE"

    recent = (
        df.tail(
            FVG_MEMORY + 2
        )
        .reset_index(drop=True)
    )

    result = "NONE"

    for i in range(
        2,
        len(recent)
    ):

        a = recent.iloc[
            i - 2
        ]

        c = recent.iloc[i]

        if (
            a["high"] <
            c["low"]
        ):

            result = "BULLISH FVG"

        elif (
            a["low"] >
            c["high"]
        ):

            result = "BEARISH FVG"

    return result


# =========================================================
# BOS
# =========================================================

def get_bos(df):

    if len(df) < BOS_LOOKBACK + 2:
        return "NONE"

    last = df.iloc[-1]

    previous = df.iloc[
        -BOS_LOOKBACK - 1:-1
    ]

    previous_high = (
        previous["high"].max()
    )

    previous_low = (
        previous["low"].min()
    )

    if (
        last["close"] >
        previous_high
    ):

        return "BULLISH BOS"

    if (
        last["close"] <
        previous_low
    ):

        return "BEARISH BOS"

    return "NONE"


# =========================================================
# DISPLACEMENT
# =========================================================

def get_displacement(df):

    last = df.iloc[-1]

    atr = last["ATR"]

    if (
        pd.isna(atr)
        or atr <= 0
    ):

        return "NONE"

    body = abs(
        last["close"] -
        last["open"]
    )

    if body < atr * 0.70:
        return "NONE"

    if last["close"] > last["open"]:
        return "BULLISH"

    if last["close"] < last["open"]:
        return "BEARISH"

    return "NONE"


# =========================================================
# MOMENTUM
# =========================================================

def get_momentum(df):

    if len(df) < 6:
        return "NEUTRAL"

    current = (
        df["close"].iloc[-1]
    )

    previous = (
        df["close"].iloc[-6]
    )

    if current > previous:
        return "BULLISH"

    if current < previous:
        return "BEARISH"

    return "NEUTRAL"


# =========================================================
# PREMIUM / DISCOUNT
# =========================================================

def get_zone(df):

    recent = df.tail(50)

    high = recent["high"].max()
    low = recent["low"].min()

    current = (
        recent["close"].iloc[-1]
    )

    midpoint = (
        high + low
    ) / 2

    if current > midpoint:
        return "PREMIUM"

    if current < midpoint:
        return "DISCOUNT"

    return "EQUILIBRIUM"


# =========================================================
# LOT
# =========================================================

def calculate_lot(
    entry,
    stop,
    deposit=DEFAULT_DEPOSIT
):

    risk_money = (
        deposit *
        RISK_PERCENT /
        100
    )

    stop_distance = abs(
        entry - stop
    )

    if stop_distance <= 0:
        return MIN_LOT

    value_per_price_unit = 100.0

    lot = (
        risk_money /
        (
            stop_distance *
            value_per_price_unit
        )
    )

    lot = max(
        MIN_LOT,
        min(
            MAX_LOT,
            lot
        )
    )

    return round(
        lot,
        2
    )


# =========================================================
# SCORE
# =========================================================

def calculate_score(
    h4_trend,
    h1_trend,
    m15_structure,
    liquidity,
    fvg,
    bos,
    displacement,
    momentum,
    zone,
    direction,
    rr,
    m5_structure,
    m5_liquidity,
    m5_fvg,
    m5_bos,
    m5_displacement,
    m5_momentum
):

    score = 0

    # H4
    if (
        direction == "BUY"
        and h4_trend == "BULLISH"
    ):

        score += 15

    elif (
        direction == "SELL"
        and h4_trend == "BEARISH"
    ):

        score += 15

    # H1
    if (
        direction == "BUY"
        and h1_trend == "BULLISH"
    ):

        score += 15

    elif (
        direction == "SELL"
        and h1_trend == "BEARISH"
    ):

        score += 15

    # M15 liquidity
    if (
        direction == "BUY"
        and liquidity == "SSL SWEEP"
    ):

        score += 15

    elif (
        direction == "SELL"
        and liquidity == "BSL SWEEP"
    ):

        score += 15

    # M15 BOS
    if (
        direction == "BUY"
        and bos == "BULLISH BOS"
    ):

        score += 15

    elif (
        direction == "SELL"
        and bos == "BEARISH BOS"
    ):

        score += 15

    # M15 FVG
    if (
        direction == "BUY"
        and fvg == "BULLISH FVG"
    ):

        score += 10

    elif (
        direction == "SELL"
        and fvg == "BEARISH FVG"
    ):

        score += 10

    # M15 displacement
    if (
        direction == "BUY"
        and displacement == "BULLISH"
    ):

        score += 10

    elif (
        direction == "SELL"
        and displacement == "BEARISH"
    ):

        score += 10

    # M15 structure
    if (
        direction == "BUY"
        and m15_structure == "BULLISH"
    ):

        score += 5

    elif (
        direction == "SELL"
        and m15_structure == "BEARISH"
    ):

        score += 5

    # M15 momentum
    if (
        direction == "BUY"
        and momentum == "BULLISH"
    ):

        score += 5

    elif (
        direction == "SELL"
        and momentum == "BEARISH"
    ):

        score += 5

    # Zone
    if (
        direction == "BUY"
        and zone == "DISCOUNT"
    ):

        score += 5

    elif (
        direction == "SELL"
        and zone == "PREMIUM"
    ):

        score += 5

    # RR
    if rr >= 3:
        score += 5

    elif rr >= 2:
        score += 3

    # =====================================================
    # M5 BONUS
    # =====================================================

    if direction == "BUY":

        if m5_bos == "BULLISH BOS":
            score += 5

        if m5_liquidity == "SSL SWEEP":
            score += 5

        if m5_fvg == "BULLISH FVG":
            score += 4

        if m5_displacement == "BULLISH":
            score += 4

        if m5_momentum == "BULLISH":
            score += 2

        if m5_structure == "BULLISH":
            score += 2

    else:

        if m5_bos == "BEARISH BOS":
            score += 5

        if m5_liquidity == "BSL SWEEP":
            score += 5

        if m5_fvg == "BEARISH FVG":
            score += 4

        if m5_displacement == "BEARISH":
            score += 4

        if m5_momentum == "BEARISH":
            score += 2

        if m5_structure == "BEARISH":
            score += 2

    # =====================================================
    # STRONG M15 CONFLUENCE
    # =====================================================

    if direction == "BUY":

        if (
            liquidity == "SSL SWEEP"
            and
            fvg == "BULLISH FVG"
        ):

            score += 10

        if (
            liquidity == "SSL SWEEP"
            and
            displacement == "BULLISH"
        ):

            score += 5

    else:

        if (
            liquidity == "BSL SWEEP"
            and
            fvg == "BEARISH FVG"
        ):

            score += 10

        if (
            liquidity == "BSL SWEEP"
            and
            displacement == "BEARISH"
        ):

            score += 5

    return min(
        score,
        100
    )


# =========================================================
# ANALYSIS
# =========================================================

def analyze():

    try:

        # =================================================
        # DATA
        # =================================================

        h4 = get_candles(
            "4h",
            250
        )

        h1 = get_candles(
            "1h",
            250
        )

        m15 = get_candles(
            "15min",
            M15_LOOKBACK
        )

        m5 = get_candles(
            "5min",
            M5_LOOKBACK
        )

        if any(
            x is None
            for x in [
                h4,
                h1,
                m15,
                m5
            ]
        ):

            return {
                "signal": "WAIT",
                "reason": "Market data unavailable"
            }

        # =================================================
        # CLOSED
        # =================================================

        h4 = closed_candles(h4)
        h1 = closed_candles(h1)
        m15 = closed_candles(m15)
        m5 = closed_candles(m5)

        if any(
            x is None
            for x in [
                h4,
                h1,
                m15,
                m5
            ]
        ):

            return {
                "signal": "WAIT",
                "reason": "Not enough closed candles"
            }

        # =================================================
        # INDICATORS
        # =================================================

        h4 = add_indicators(h4)
        h1 = add_indicators(h1)
        m15 = add_indicators(m15)
        m5 = add_indicators(m5)

        if len(h4) < 200:
            return {
                "signal": "WAIT",
                "reason": "Not enough H4 data"
            }

        if len(h1) < 200:
            return {
                "signal": "WAIT",
                "reason": "Not enough H1 data"
            }

        if len(m15) < 50:
            return {
                "signal": "WAIT",
                "reason": "Not enough M15 data"
            }

        if len(m5) < 50:
            return {
                "signal": "WAIT",
                "reason": "Not enough M5 data"
            }

        # =================================================
        # PRICE
        # =================================================

        price = get_price()

        if price is None:

            price = float(
                m5["close"].iloc[-1]
            )

        # =================================================
        # H4 / H1
        # =================================================

        h4_trend = get_trend(h4)
        h1_trend = get_trend(h1)

        # =================================================
        # M15
        # =================================================

        m15_structure = get_structure(
            m15
        )

        liquidity = get_liquidity(
            m15
        )

        fvg = get_fvg(
            m15
        )

        bos = get_bos(
            m15
        )

        displacement = get_displacement(
            m15
        )

        momentum = get_momentum(
            m15
        )

        zone = get_zone(
            m15
        )

        m15_rsi = float(
            m15["RSI"].iloc[-1]
        )

        m15_atr = float(
            m15["ATR"].iloc[-1]
        )

        if (
            np.isnan(m15_atr)
            or
            m15_atr <= 0
        ):

            m15_atr = 3.0

        # =================================================
        # M5
        # =================================================

        m5_structure = get_structure(
            m5
        )

        m5_liquidity = get_liquidity(
            m5
        )

        m5_fvg = get_fvg(
            m5
        )

        m5_bos = get_bos(
            m5
        )

        m5_displacement = get_displacement(
            m5
        )

        m5_momentum = get_momentum(
            m5
        )

        m5_zone = get_zone(
            m5
        )

        m5_rsi = float(
            m5["RSI"].iloc[-1]
        )

        m5_atr = float(
            m5["ATR"].iloc[-1]
        )

        if (
            np.isnan(m5_atr)
            or
            m5_atr <= 0
        ):

            m5_atr = 1.5

        # =================================================
        # M5 CONFIRMATION
        # =================================================

        m5_buy_confirmation = (
            m5_bos == "BULLISH BOS"
            or
            (
                m5_liquidity == "SSL SWEEP"
                and
                m5_displacement == "BULLISH"
            )
            or
            (
                m5_fvg == "BULLISH FVG"
                and
                m5_momentum == "BULLISH"
            )
        )

        m5_sell_confirmation = (
            m5_bos == "BEARISH BOS"
            or
            (
                m5_liquidity == "BSL SWEEP"
                and
                m5_displacement == "BEARISH"
            )
            or
            (
                m5_fvg == "BEARISH FVG"
                and
                m5_momentum == "BEARISH"
            )
        )

        # =================================================
        # CANDIDATES
        # =================================================

        buy_candidate = (
            h4_trend == "BULLISH"
            or
            h1_trend == "BULLISH"
            or
            liquidity == "SSL SWEEP"
            or
            bos == "BULLISH BOS"
            or
            fvg == "BULLISH FVG"
        )

        sell_candidate = (
            h4_trend == "BEARISH"
            or
            h1_trend == "BEARISH"
            or
            liquidity == "BSL SWEEP"
            or
            bos == "BEARISH BOS"
            or
            fvg == "BEARISH FVG"
        )

        # =================================================
        # SETUPS
        # =================================================

        buy_sl = (
            price -
            m5_atr * 1.5
        )

        buy_risk = abs(
            price -
            buy_sl
        )

        buy_tp1 = (
            price +
            buy_risk * 2
        )

        buy_tp2 = (
            price +
            buy_risk * 3
        )

        sell_sl = (
            price +
            m5_atr * 1.5
        )

        sell_risk = abs(
            price -
            sell_sl
        )

        sell_tp1 = (
            price -
            sell_risk * 2
        )

        sell_tp2 = (
            price -
            sell_risk * 3
        )

        buy_score = calculate_score(
            h4_trend,
            h1_trend,
            m15_structure,
            liquidity,
            fvg,
            bos,
            displacement,
            momentum,
            zone,
            "BUY",
            TARGET_RR,
            m5_structure,
            m5_liquidity,
            m5_fvg,
            m5_bos,
            m5_displacement,
            m5_momentum
        )

        sell_score = calculate_score(
            h4_trend,
            h1_trend,
            m15_structure,
            liquidity,
            fvg,
            bos,
            displacement,
            momentum,
            zone,
            "SELL",
            TARGET_RR,
            m5_structure,
            m5_liquidity,
            m5_fvg,
            m5_bos,
            m5_displacement,
            m5_momentum
        )

        # =================================================
        # FULL BUY
        # =================================================

        full_buy = (
            buy_candidate
            and
            buy_score >= MIN_SCORE
            and
            m15_rsi < BUY_RSI_MAX
            and
            m5_buy_confirmation
            and
            (
                bos == "BULLISH BOS"
                or
                (
                    liquidity == "SSL SWEEP"
                    and
                    fvg == "BULLISH FVG"
                    and
                    displacement == "BULLISH"
                )
            )
        )

        # =================================================
        # FULL SELL
        # =================================================

        full_sell = (
            sell_candidate
            and
            sell_score >= MIN_SCORE
            and
            m15_rsi > SELL_RSI_MIN
            and
            m5_sell_confirmation
            and
            (
                bos == "BEARISH BOS"
                or
                (
                    liquidity == "BSL SWEEP"
                    and
                    fvg == "BEARISH FVG"
                    and
                    displacement == "BEARISH"
                )
            )
        )

        # =================================================
        # EARLY BUY
        # =================================================

        early_buy = (
            buy_candidate
            and
            buy_score >= EARLY_SCORE
            and
            m15_rsi < BUY_RSI_MAX
            and
            m5_buy_confirmation
        )

        # =================================================
        # EARLY SELL
        # =================================================

        early_sell = (
            sell_candidate
            and
            sell_score >= EARLY_SCORE
            and
            m15_rsi > SELL_RSI_MIN
            and
            m5_sell_confirmation
        )

        # =================================================
        # FINAL
        # =================================================

        signal = "WAIT"

        if (
            full_buy
            and
            buy_score >= sell_score
        ):

            signal = "BUY"

        elif (
            full_sell
            and
            sell_score > buy_score
        ):

            signal = "SELL"

        elif (
            early_buy
            and
            buy_score >= sell_score
        ):

            signal = "EARLY BUY"

        elif (
            early_sell
            and
            sell_score > buy_score
        ):

            signal = "EARLY SELL"

        # =================================================
        # SELECT
        # =================================================

        if signal in [
            "BUY",
            "EARLY BUY"
        ]:

            direction = "BUY"

            score = buy_score

            entry = price
            sl = buy_sl
            tp1 = buy_tp1
            tp2 = buy_tp2
            rr = TARGET_RR

        elif signal in [
            "SELL",
            "EARLY SELL"
        ]:

            direction = "SELL"

            score = sell_score

            entry = price
            sl = sell_sl
            tp1 = sell_tp1
            tp2 = sell_tp2
            rr = TARGET_RR

        else:

            direction = "NONE"

            score = max(
                buy_score,
                sell_score
            )

            entry = price
            sl = 0
            tp1 = 0
            tp2 = 0
            rr = 0

        # =================================================
        # GRADE
        # =================================================

        if score >= 85:

            grade = "A+"

        elif score >= 70:

            grade = "A"

        elif score >= 60:

            grade = "B"

        else:

            grade = "WAIT"

        # =================================================
        # LOT
        # =================================================

        if direction != "NONE":

            lot = calculate_lot(
                entry,
                sl
            )

        else:

            lot = MIN_LOT

        # =================================================
        # RESULT
        # =================================================

        return {

            "signal": signal,

            "direction": direction,

            "price": price,

            "h4_trend": h4_trend,

            "h1_trend": h1_trend,

            "m15_structure": m15_structure,

            "m5_structure": m5_structure,

            "liquidity": liquidity,

            "fvg": fvg,

            "bos": bos,

            "displacement": displacement,

            "momentum": momentum,

            "zone": zone,

            "m5_liquidity": m5_liquidity,

            "m5_fvg": m5_fvg,

            "m5_bos": m5_bos,

            "m5_displacement": m5_displacement,

            "m5_momentum": m5_momentum,

            "m5_zone": m5_zone,

            "rsi": m15_rsi,

            "m5_rsi": m5_rsi,

            "buy_score": buy_score,

            "sell_score": sell_score,

            "score": score,

            "grade": grade,

            "entry": entry,

            "sl": sl,

            "tp1": tp1,

            "tp2": tp2,

            "rr": rr,

            "lot": lot,

            "candle_time": str(
                m5["datetime"].iloc[-1]
            )
        }

    except Exception as e:

        print(
            "ANALYZE ERROR:",
            repr(e)
        )

        return {
            "signal": "WAIT",
            "reason": f"Analysis error: {e}"
        }


# =========================================================
# FORMAT SIGNAL
# =========================================================

def format_signal(a):

    signal = a.get(
        "signal",
        "WAIT"
    )

    if signal in [
        "BUY",
        "EARLY BUY"
    ]:

        emoji = "🟢"

    elif signal in [
        "SELL",
        "EARLY SELL"
    ]:

        emoji = "🔴"

    else:

        emoji = "⚪"

    if signal == "WAIT":

        return f"""
🥇 GOLD SMART V5.1

⚪ SIGNAL: WAIT

💰 XAUUSD: {a.get("price", 0):.2f}

📊 H4: {a.get("h4_trend", "N/A")}
📊 H1: {a.get("h1_trend", "N/A")}

🏗 M15: {a.get("m15_structure", "N/A")}
🎯 M5: {a.get("m5_structure", "N/A")}

💧 M15 Liquidity: {a.get("liquidity", "N/A")}
🧩 M15 FVG: {a.get("fvg", "N/A")}
🔨 M15 BOS: {a.get("bos", "N/A")}
💥 M15 Displacement: {a.get("displacement", "N/A")}

🎯 M5 Liquidity: {a.get("m5_liquidity", "N/A")}
🧩 M5 FVG: {a.get("m5_fvg", "N/A")}
🔨 M5 BOS: {a.get("m5_bos", "N/A")}
💥 M5 Displacement: {a.get("m5_displacement", "N/A")}
📈 M5 Momentum: {a.get("m5_momentum", "N/A")}

📐 M15 Zone: {a.get("zone", "N/A")}
📐 M5 Zone: {a.get("m5_zone", "N/A")}

━━━━━━━━━━━━━━

RSI M15: {a.get("rsi", 0):.1f}
RSI M5: {a.get("m5_rsi", 0):.1f}

🟢 BUY SCORE: {a.get("buy_score", 0)}/100
🔴 SELL SCORE: {a.get("sell_score", 0)}/100

⭐ QUALITY: {a.get("grade", "WAIT")}

🚫 AUTO TRADING: OFF

🕯 M5 Candle:
{a.get("candle_time", "N/A")}

Причина:
{a.get("reason", "Нет полного подтверждения")}
"""

    return f"""
🥇 GOLD SMART V5.1

{emoji} SIGNAL: {signal}

💰 XAUUSD: {a["price"]:.2f}

━━━━━━━━━━━━━━

📊 H4: {a["h4_trend"]}
📊 H1: {a["h1_trend"]}

🏗 M15: {a["m15_structure"]}
🎯 M5: {a["m5_structure"]}

💧 M15 Liquidity: {a["liquidity"]}
🧩 M15 FVG: {a["fvg"]}
🔨 M15 BOS: {a["bos"]}
💥 M15 Displacement: {a["displacement"]}

🎯 M5 Liquidity: {a["m5_liquidity"]}
🧩 M5 FVG: {a["m5_fvg"]}
🔨 M5 BOS: {a["m5_bos"]}
💥 M5 Displacement: {a["m5_displacement"]}
📈 M5 Momentum: {a["m5_momentum"]}

📐 M15 Zone: {a["zone"]}
📐 M5 Zone: {a["m5_zone"]}

━━━━━━━━━━━━━━

RSI M15: {a["rsi"]:.1f}
RSI M5: {a["m5_rsi"]:.1f}

🟢 BUY SCORE: {a["buy_score"]}/100
🔴 SELL SCORE: {a["sell_score"]}/100

⭐ QUALITY: {a["grade"]}
📊 SCORE: {a["score"]}/100

━━━━━━━━━━━━━━

🎯 ENTRY: {a["entry"]:.2f}
🛑 SL: {a["sl"]:.2f}

💰 TP1: {a["tp1"]:.2f}
💰 TP2: {a["tp2"]:.2f}

📐 RR: 1:{a["rr"]:.1f}

💵 Risk: {RISK_PERCENT:.1f}%
📦 Lot: {a["lot"]:.2f}

🚫 AUTO TRADING: OFF

🕯 M5 Candle:
{a["candle_time"]}
"""


# =========================================================
# STATISTICS
# =========================================================

stats_lock = threading.Lock()


def load_stats():

    if not os.path.exists(
        STATS_FILE
    ):

        return []

    try:

        with open(
            STATS_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            data = json.load(f)

        if isinstance(
            data,
            list
        ):

            return data

    except Exception as e:

        print(
            "Stats load error:",
            e
        )

    return []


def save_stats(data):

    try:

        with open(
            STATS_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                data,
                f,
                ensure_ascii=False,
                indent=2
            )

    except Exception as e:

        print(
            "Stats save error:",
            e
        )


# =========================================================
# REGISTER SIGNAL
# =========================================================

def register_signal(a):

    if a.get("signal") not in [
        "BUY",
        "SELL",
        "EARLY BUY",
        "EARLY SELL"
    ]:

        return None

    now = datetime.now(
        timezone.utc
    ).isoformat()

    signal = {

        "id": int(
            time.time()
        ),

        "created": now,

        "status": "OPEN",

        "signal": a["signal"],

        "direction": a["direction"],

        "entry": float(
            a["entry"]
        ),

        "sl": float(
            a["sl"]
        ),

        "tp1": float(
            a["tp1"]
        ),

        "tp2": float(
            a["tp2"]
        ),

        "lot": float(
            a["lot"]
        ),

        "score": int(
            a["score"]
        ),

        "grade": a["grade"],

        "candle_time": a[
            "candle_time"
        ],

        "tp1_hit": False,

        "be_price": float(
            a["entry"]
        )
    }

    with stats_lock:

        data = load_stats()

        data.append(
            signal
        )

        save_stats(
            data
        )

    print(
        "REGISTERED SIGNAL:",
        signal
    )

    return signal


# =========================================================
# UPDATE STATISTICS
# =========================================================

def update_statistics(
    current_price
):

    if current_price is None:
        return

    changed = False

    with stats_lock:

        data = load_stats()

        for item in data:

            if item.get(
                "status"
            ) != "OPEN":

                continue

            direction = item.get(
                "direction"
            )

            entry = float(
                item.get(
                    "entry",
                    0
                )
            )

            sl = float(
                item.get(
                    "sl",
                    0
                )
            )

            tp1 = float(
                item.get(
                    "tp1",
                    0
                )
            )

            tp2 = float(
                item.get(
                    "tp2",
                    0
                )
            )

            # =============================================
            # BUY
            # =============================================

            if direction == "BUY":

                # SL before TP1
                if (
                    current_price <= sl
                    and
                    not item.get(
                        "tp1_hit",
                        False
                    )
                ):

                    item["status"] = "LOSS"

                    item["closed_price"] = (
                        current_price
                    )

                    item["closed_at"] = (
                        datetime.now(
                            timezone.utc
                        ).isoformat()
                    )

                    changed = True

                    continue

                # TP1
                if (
                    current_price >= tp1
                    and
                    not item.get(
                        "tp1_hit",
                        False
                    )
                ):

                    item["tp1_hit"] = True

                    item["sl"] = entry

                    item["be_price"] = entry

                    changed = True

                # TP2 after TP1
                if (
                    current_price >= tp2
                    and
                    item.get(
                        "tp1_hit",
                        False
                    )
                ):

                    item["status"] = "WIN"

                    item["closed_price"] = (
                        current_price
                    )

                    item["closed_at"] = (
                        datetime.now(
                            timezone.utc
                        ).isoformat()
                    )

                    changed = True

            # =============================================
            # SELL
            # =============================================

            elif direction == "SELL":

                # SL before TP1
                if (
                    current_price >= sl
                    and
                    not item.get(
                        "tp1_hit",
                        False
                    )
                ):

                    item["status"] = "LOSS"

                    item["closed_price"] = (
                        current_price
                    )

                    item["closed_at"] = (
                        datetime.now(
                            timezone.utc
                        ).isoformat()
                    )

                    changed = True

                    continue

                # TP1
                if (
                    current_price <= tp1
                    and
                    not item.get(
                        "tp1_hit",
                        False
                    )
                ):

                    item["tp1_hit"] = True

                    item["sl"] = entry

                    item["be_price"] = entry

                    changed = True

                # TP2
                if (
                    current_price <= tp2
                    and
                    item.get(
                        "tp1_hit",
                        False
                    )
                ):

                    item["status"] = "WIN"

                    item["closed_price"] = (
                        current_price
                    )

                    item["closed_at"] = (
                        datetime.now(
                            timezone.utc
                        ).isoformat()
                    )

                    changed = True

        if changed:

            save_stats(
                data
            )


# =========================================================
# FORMAT STATS
# =========================================================

def format_stats():

    with stats_lock:

        data = load_stats()

    total = len(data)

    wins = sum(
        1
        for x in data
        if x.get("status") == "WIN"
    )

    losses = sum(
        1
        for x in data
        if x.get("status") == "LOSS"
    )

    be = sum(
        1
        for x in data
        if x.get("status") == "BE"
    )

    opened = sum(
        1
        for x in data
        if x.get("status") == "OPEN"
    )

    closed = (
        wins +
        losses +
        be
    )

    if closed > 0:

        winrate = (
            wins /
            closed *
            100
        )

    else:

        winrate = 0.0

    full = [
        x
        for x in data
        if x.get("signal") in [
            "BUY",
            "SELL"
        ]
    ]

    early = [
        x
        for x in data
        if x.get("signal") in [
            "EARLY BUY",
            "EARLY SELL"
        ]
    ]

    full_closed = [
        x
        for x in full
        if x.get("status") in [
            "WIN",
            "LOSS",
            "BE"
        ]
    ]

    early_closed = [
        x
        for x in early
        if x.get("status") in [
            "WIN",
            "LOSS",
            "BE"
        ]
    ]

    full_wins = sum(
        1
        for x in full_closed
        if x.get("status") == "WIN"
    )

    early_wins = sum(
        1
        for x in early_closed
        if x.get("status") == "WIN"
    )

    full_wr = (
        full_wins /
        len(full_closed) *
        100
        if full_closed
        else 0
    )

    early_wr = (
        early_wins /
        len(early_closed) *
        100
        if early_closed
        else 0
    )

    return f"""
📊 GOLD SMART V5.1 — STATISTICS

📌 TOTAL SIGNALS: {total}

🟢 WIN: {wins}
🔴 LOSS: {losses}
⚪ BE: {be}
🟡 OPEN: {opened}

━━━━━━━━━━━━━━

🎯 CLOSED: {closed}

📈 WIN RATE:
{winrate:.1f}%

━━━━━━━━━━━━━━

🔥 FULL SIGNALS:
{len(full_closed)} closed

WIN RATE:
{full_wr:.1f}%

🟡 EARLY SIGNALS:
{len(early_closed)} closed

WIN RATE:
{early_wr:.1f}%

━━━━━━━━━━━━━━

📊 TIMEFRAME: M5

📊 CONTEXT:
H4 → H1 → M15 → M5

💵 RISK:
{RISK_PERCENT:.1f}%

🚫 AUTO TRADING:
OFF

ℹ️ Статистика виртуальная.
"""


# =========================================================
# AUTO ENGINE
# =========================================================

def auto_engine():

    global last_m5_candle
    global last_signal_key
    global last_signal_time

    print(
        "===================================="
    )

    print(
        "🥇 GOLD SMART V5.1"
    )

    print(
        "AUTO SIGNAL ENGINE STARTED"
    )

    print(
        "TIMEFRAME: M5"
    )

    print(
        "AUTO TRADING: OFF"
    )

    print(
        "===================================="
    )

    while True:

        try:

            # =============================================
            # M5 DATA
            # =============================================

            m5 = get_candles(
                "5min",
                50
            )

            if m5 is None:

                print(
                    "ENGINE: M5 data unavailable"
                )

                time.sleep(
                    CHECK_INTERVAL
                )

                continue

            closed = closed_candles(
                m5
            )

            if closed is None:

                time.sleep(
                    CHECK_INTERVAL
                )

                continue

            candle_time = str(
                closed[
                    "datetime"
                ].iloc[-1]
            )

            with state_lock:

                previous_candle = (
                    last_m5_candle
                )

                last_m5_candle = (
                    candle_time
                )

            # =============================================
            # CURRENT PRICE FOR OPEN STATS
            # =============================================

            stats = load_stats()

            has_open = any(
                x.get("status") == "OPEN"
                for x in stats
            )

            if has_open:

                current_price = get_price()

                if current_price:

                    update_statistics(
                        current_price
                    )

            # =============================================
            # NEW CLOSED M5 CANDLE
            # =============================================

            if (
                previous_candle is not None
                and
                candle_time == previous_candle
            ):

                time.sleep(
                    CHECK_INTERVAL
                )

                continue

            print(
                "NEW M5 CANDLE:",
                candle_time
            )

            # =============================================
            # FULL ANALYSIS
            # =============================================

            result = analyze()

            signal = result.get(
                "signal",
                "WAIT"
            )

            print
