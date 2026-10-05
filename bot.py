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
# GOLD SMART V5.9.3
# XAU/USD SMART SIGNAL BOT
#
# DATA: XAUS
# TF: H4 -> H1 -> M15 -> M5
#
# AUTO TRADING: OFF
# RISK: 1%
# ============================================================


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BASE_URL = os.getenv(
    "RENDER_EXTERNAL_URL",
    "https://gold-bot-q8la.onrender.com"
).strip().rstrip("/")

DEPOSIT = float(os.getenv("DEPOSIT", "800"))

RISK_PERCENT = 1.0

MIN_FULL_SCORE = 70
MIN_EARLY_SCORE = 60

SESSION_START = 12
SESSION_END = 23

STATE_FILE = "gold_smart_state.json"

XAUS_SPOT_URL = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY_URL = "https://xaus.com/api/v1/intraday"


# ============================================================
# FLASK APP
# ============================================================

app = Flask(__name__)


# ============================================================
# STATE
# ============================================================

state = {
    "signals_total": 0,
    "signals_full": 0,
    "signals_early": 0,
    "wins": 0,
    "losses": 0,
    "last_signal": None,
    "last_signal_time": None,
    "last_price": None,
    "last_error": None,
}

state_lock = threading.Lock()


# ============================================================
# LOAD / SAVE STATE
# ============================================================

def load_state():
    global state

    try:
        if not os.path.exists(STATE_FILE):
            return

        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        if isinstance(data, dict):
            state.update(data)

        print("STATE LOADED")

    except Exception as e:
        print("STATE LOAD ERROR:", e)


def save_state():

    try:
        with state_lock:
            data = dict(state)

        with open(
            STATE_FILE,
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
        print("STATE SAVE ERROR:", e)


load_state()


# ============================================================
# TIME
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def kz_now():
    return (
        datetime.now(timezone.utc)
        + timedelta(hours=5)
    )


def in_session():

    hour = kz_now().hour

    return (
        SESSION_START
        <= hour
        < SESSION_END
    )


# ============================================================
# TELEGRAM API
# ============================================================

def telegram_call(
    method,
    payload=None
):

    if not BOT_TOKEN:
        print("BOT_TOKEN IS EMPTY")
        return None

    url = (
        "https://api.telegram.org/"
        f"bot{BOT_TOKEN}/{method}"
    )

    try:

        response = requests.post(
            url,
            json=payload or {},
            timeout=15
        )

        print(
            f"TELEGRAM {method}: "
            f"{response.status_code}"
        )

        try:
            return response.json()
        except Exception:
            return None

    except Exception as e:

        print(
            "TELEGRAM ERROR:",
            e
        )

        return None


def send_message(
    message,
    chat_id=None
):

    target = (
        str(chat_id)
        if chat_id
        else CHAT_ID
    )

    if not target:
        print("CHAT_ID EMPTY")
        return False

    result = telegram_call(
        "sendMessage",
        {
            "chat_id": target,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    )

    if result and result.get("ok"):
        return True

    return False


# ============================================================
# TELEGRAM WEBHOOK
# ============================================================

def webhook_url():

    return (
        f"{BASE_URL}/telegram"
    )


def set_webhook():

    if not BOT_TOKEN:
        print(
            "WEBHOOK NOT SET: "
            "BOT_TOKEN EMPTY"
        )
        return

    url = webhook_url()

    print(
        "SETTING WEBHOOK:",
        url
    )

    result = telegram_call(
        "setWebhook",
        {
            "url": url,
            "drop_pending_updates": False,
            "allowed_updates": [
                "message"
            ],
        }
    )

    if result and result.get("ok"):

        print(
            "TELEGRAM WEBHOOK READY"
        )

    else:

        print(
            "TELEGRAM WEBHOOK ERROR"
        )


def webhook_info():

    return telegram_call(
        "getWebhookInfo",
        {}
    )


# ============================================================
# XAUS SPOT
# ============================================================

def get_spot():

    try:

        params = {
            "currency": "USD",
            "unit": "oz",
            "compact": "1",
            "fresh": str(
                int(time.time())
            ),
        }

        response = requests.get(
            XAUS_SPOT_URL,
            params=params,
            timeout=15
        )

        response.raise_for_status()

        data = response.json()

        price = data.get(
            "spot_usd_oz"
        )

        if price is None:

            xau = data.get(
                "xau"
            )

            if isinstance(
                xau,
                dict
            ):

                price = xau.get(
                    "price"
                )

        if price is None:

            raise ValueError(
                "XAUS price not found"
            )

        price = float(price)

        if not np.isfinite(price):
            raise ValueError(
                "XAUS price invalid"
            )

        if price <= 0:
            raise ValueError(
                "XAUS price <= 0"
            )

        with state_lock:

            state["last_price"] = price
            state["last_error"] = None

        print(
            f"XAUS PRICE: {price:.2f}"
        )

        return price

    except Exception as e:

        print(
            "XAUS SPOT ERROR:",
            e
        )

        with state_lock:
            state["last_error"] = str(e)

        return None


# ============================================================
# XAUS INTRADAY
# ============================================================

def get_intraday():

    try:

        params = {
            "symbol": "xau",
            "hours": 48,
            "fresh": str(
                int(time.time())
            ),
        }

        response = requests.get(
            XAUS_INTRADAY_URL,
            params=params,
            timeout=20
        )

        response.raise_for_status()

        data = response.json()

        points = data.get(
            "points"
        )

        if not isinstance(
            points,
            list
        ):

            raise ValueError(
                "XAUS points not found"
            )

        rows = []

        for item in points:

            if not isinstance(
                item,
                dict
            ):
                continue

            timestamp = item.get("t")
            price = item.get("p")

            if (
                timestamp is None
                or price is None
            ):
                continue

            try:

                price = float(price)

                if not np.isfinite(
                    price
                ):
                    continue

                timestamp = pd.to_datetime(
                    timestamp,
                    utc=True
                )

                rows.append(
                    (
                        timestamp,
                        price
                    )
                )

            except Exception:
                continue

        if len(rows) < 20:

            raise ValueError(
                "Not enough XAUS points: "
                f"{len(rows)}"
            )

        df = pd.DataFrame(
            rows,
            columns=[
                "time",
                "price"
            ]
        )

        df = df.sort_values(
            "time"
        )

        df = df.drop_duplicates(
            subset="time"
        )

        df = df.set_index(
            "time"
        )

        return df

    except Exception as e:

        print(
            "XAUS INTRADAY ERROR:",
            e
        )

        with state_lock:
            state["last_error"] = str(e)

        return None


# ============================================================
# BUILD CANDLES
# ============================================================

def make_candles(
    df,
    timeframe
):

    if df is None:
        return None

    rules = {
        "M5": "5min",
        "M15": "15min",
        "H1": "1h",
        "H4": "4h",
    }

    rule = rules.get(
        timeframe
    )

    if rule is None:
        return None

    candles = (
        df["price"]
        .resample(
            rule,
            label="right",
            closed="right"
        )
        .ohlc()
        .dropna()
    )

    if len(candles) < 2:
        return None

    candles.columns = [
        "open",
        "high",
        "low",
        "close"
    ]

    # Remove currently unfinished candle.
    now = pd.Timestamp.now(
        tz="UTC"
    )

    if (
        len(candles) > 0
        and candles.index[-1] > now
    ):

        candles = candles.iloc[:-1]

    return candles


# ============================================================
# INDICATORS
# ============================================================

def calc_ema(
    series,
    period
):

    return series.ewm(
        span=period,
        adjust=False
    ).mean()


def calc_rsi(
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
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()

    rs = (
        avg_gain
        / avg_loss.replace(
            0,
            np.nan
        )
    )

    result = (
        100
        - 100 / (1 + rs)
    )

    return result.fillna(50)


# ============================================================
# TIMEFRAME ANALYSIS
# ============================================================

def analyze_tf(df):

    if (
        df is None
        or len(df) < 20
    ):

        return {
            "trend": "UNKNOWN",
            "score": 50,
            "rsi": 50,
            "ema50": 0,
            "ema200": 0,
            "bos": "NONE",
        }

    close = df["close"]

    e50 = calc_ema(
        close,
        50
    )

    e200 = calc_ema(
        close,
        200
    )

    rsi_series = calc_rsi(
        close
    )

    price = float(
        close.iloc[-1]
    )

    ema50 = float(
        e50.iloc[-1]
    )

    ema200 = float(
        e200.iloc[-1]
    )

    rsi = float(
        rsi_series.iloc[-1]
    )

    buy = 50
    sell = 50

    if price > ema50:
        buy += 10
    else:
        sell += 10

    if ema50 > ema200:
        buy += 10
    else:
        sell += 10

    if rsi > 55:
        buy += 10

    elif rsi < 45:
        sell += 10

    previous_high = float(
        df["high"]
        .iloc[-6:-1]
        .max()
    )

    previous_low = float(
        df["low"]
        .iloc[-6:-1]
        .min()
    )

    bos = "NONE"

    if price > previous_high:

        bos = "BULLISH"
        buy += 15

    elif price < previous_low:

        bos = "BEARISH"
        sell += 15

    if buy >= sell + 10:

        trend = "BULLISH"

    elif sell >= buy + 10:

        trend = "BEARISH"

    else:

        trend = "MIXED"

    return {
        "trend": trend,
        "score": min(
            100,
            max(
                buy,
                sell
            )
        ),
        "rsi": rsi,
        "ema50": ema50,
        "ema200": ema200,
        "bos": bos,
    }


# ============================================================
# M5 SMC
# ============================================================

def analyze_smc(df):

    result = {
        "liquidity": "NONE",
        "bos": "NONE",
        "fvg": "NONE",
        "displacement": "NONE",
        "momentum": "NEUTRAL",
        "pd": "EQUILIBRIUM",
    }

    if (
        df is None
        or len(df) < 20
    ):
        return result

    current = df.iloc[-1]

    previous_high = float(
        df["high"]
        .iloc[-6:-1]
        .max()
    )

    previous_low = float(
        df["low"]
        .iloc[-6:-1]
        .min()
    )

    close = float(
        current["close"]
    )

    high = float(
        current["high"]
    )

    low = float(
        current["low"]
    )

    # --------------------------------------------------------
    # LIQUIDITY
    # --------------------------------------------------------

    if (
        high > previous_high
        and close < previous_high
    ):

        result[
            "liquidity"
        ] = "BUY-SIDE SWEPT"

    elif (
        low < previous_low
        and close > previous_low
    ):

        result[
            "liquidity"
        ] = "SELL-SIDE SWEPT"

    # --------------------------------------------------------
    # BOS
    # --------------------------------------------------------

    if close > previous_high:

        result["bos"] = "BULLISH"

    elif close < previous_low:

        result["bos"] = "BEARISH"

    # --------------------------------------------------------
    # FVG
    # --------------------------------------------------------

    c1 = df.iloc[-3]
    c3 = df.iloc[-1]

    if float(
        c3["low"]
    ) > float(
        c1["high"]
    ):

        result["fvg"] = "BULLISH"

    elif float(
        c3["high"]
    ) < float(
        c1["low"]
    ):

        result["fvg"] = "BEARISH"

    # --------------------------------------------------------
    # DISPLACEMENT
    # --------------------------------------------------------

    ranges = (
        df["high"]
        - df["low"]
    )

    average_range = float(
        ranges.iloc[-20:].mean()
    )

    current_range = (
        high - low
    )

    if (
        average_range > 0
        and current_range
        > average_range * 1.5
    ):

        if close > float(
            current["open"]
        ):

            result[
                "displacement"
            ] = "BULLISH"

        else:

            result[
                "displacement"
            ] = "BEARISH"

    # --------------------------------------------------------
    # MOMENTUM
    # --------------------------------------------------------

    rsi = float(
        calc_rsi(
            df["close"]
        ).iloc[-1]
    )

    if rsi >= 55:

        result[
            "momentum"
        ] = "BULLISH"

    elif rsi <= 45:

        result[
            "momentum"
        ] = "BEARISH"

    # --------------------------------------------------------
    # PREMIUM / DISCOUNT
    # --------------------------------------------------------

    swing_high = float(
        df["high"]
        .iloc[-20:]
        .max()
    )

    swing_low = float(
        df["low"]
        .iloc[-20:]
        .min()
    )

    midpoint = (
        swing_high
        + swing_low
    ) / 2

    if close > midpoint:

        result["pd"] = "PREMIUM"

    elif close < midpoint:

        result["pd"] = "DISCOUNT"

    return result


# ============================================================
# SIGNAL SCORE
# ============================================================

def build_signal(
    price,
    h4,
    h1,
    m15,
    m5,
    smc
):

    buy = 0
    sell = 0

    # H4
    if h4["trend"] == "BULLISH":
        buy += 20

    elif h4["trend"] == "BEARISH":
        sell += 20

    # H1
    if h1["trend"] == "BULLISH":
        buy += 20

    elif h1["trend"] == "BEARISH":
        sell += 20

    # M15
    if m15["trend"] == "BULLISH":
        buy += 15

    elif m15["trend"] == "BEARISH":
        sell += 15

    # M5
    if m5["trend"] == "BULLISH":
        buy += 15

    elif m5["trend"] == "BEARISH":
        sell += 15

    # Liquidity
    if smc["liquidity"] == "SELL-SIDE SWEPT":
        buy += 10

    elif smc["liquidity"] == "BUY-SIDE SWEPT":
        sell += 10

    # BOS
    if smc["bos"] == "BULLISH":
        buy += 10

    elif smc["bos"] == "BEARISH":
        sell += 10

    # FVG
    if smc["fvg"] == "BULLISH":
        buy += 5

    elif smc["fvg"] == "BEARISH":
        sell += 5

    # Displacement
    if smc["displacement"] == "BULLISH":
        buy += 5

    elif smc["displacement"] == "BEARISH":
        sell += 5

    # Momentum
    if smc["momentum"] == "BULLISH":
        buy += 5

    elif smc["momentum"] == "BEARISH":
        sell += 5

    if buy > sell:

        direction = "BUY"
        score = buy

    elif sell > buy:

        direction = "SELL"
        score = sell

    else:

        direction = "WAIT"
        score = 50

    score = min(
        100,
        int(score)
    )

    # Higher TF protection
    if (
        direction == "BUY"
        and h4["trend"] == "BEARISH"
        and h1["trend"] == "BEARISH"
    ):

        direction = "WAIT"
        score = 50

    if (
        direction == "SELL"
        and h4["trend"] == "BULLISH"
        and h1["trend"] == "BULLISH"
    ):

        direction = "WAIT"
        score = 50

    if score >= MIN_FULL_SCORE:

        signal_type = "FULL"

    elif score >= MIN_EARLY_SCORE:

        signal_type = "EARLY"

    else:

        signal_type = "WAIT"

    # --------------------------------------------------------
    # Levels
    # --------------------------------------------------------

    if direction == "BUY":

        entry = price

        sl = entry - 4.0

        risk = entry - sl

        tp1 = entry + (
            risk * 1.5
        )

        tp2 = entry + (
            risk * 3.0
        )

    elif direction == "SELL":

        entry = price

        sl = entry + 4.0

        risk = sl - entry

        tp1 = entry - (
            risk * 1.5
        )

        tp2 = entry - (
            risk * 3.0
        )

    else:

        entry = price
        sl = price
        tp1 = price
        tp2 = price

    # Approximate lot size
    if direction != "WAIT":

        risk_money = (
            DEPOSIT
            * RISK_PERCENT
            / 100
        )

        distance = abs(
            entry - sl
        )

        lot = (
            risk_money
            / (
                distance * 100
            )
        )

        lot = max(
            0.01,
            lot
        )

        lot = min(
            0.02,
            lot
        )

        lot = round(
            lot,
            2
        )

    else:

        lot = 0.01

    return {
        "direction": direction,
        "score": score,
        "type": signal_type,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "lot": lot,
    }


# ============================================================
# FULL ANALYSIS
# ============================================================

def run_analysis():

    price = get_spot()

    if price is None:

        return {
            "ok": False,
            "error": "XAUS spot unavailable"
        }

    intraday = get_intraday()

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
        "H4"
    ]:

        candles[tf] = make_candles(
            intraday,
            tf
        )

        if (
            candles[tf] is None
            or len(candles[tf]) < 20
        ):

            return {
                "ok": False,
                "error":
                    f"Not enough {tf} candles"
            }

        print(
            f"{tf}: "
            f"{len(candles[tf])} candles"
        )

    h4 = analyze_tf(
        candles["H4"]
    )

    h1 = analyze_tf(
        candles["H1"]
    )

    m15 = analyze_tf(
        candles["M15"]
    )

    m5 = analyze_tf(
        candles["M5"]
    )

    smc = analyze_smc(
        candles["M5"]
    )

    signal = build_signal(
        price,
        h4,
        h1,
        m15,
        m5,
        smc
    )

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
# FORMAT ANALYSIS
# ============================================================

def format_analysis(
    result
):

    if not result.get("ok"):

        return (
            "🥇 <b>GOLD SMART V5.9.3</b>\n\n"
            "🔴 ERROR\n\n"
            f"❌ {result.get('error')}"
        )

    price = result["price"]

    h4 = result["h4"]
    h1 = result["h1"]
    m15 = result["m15"]
    m5 = result["m5"]

    smc = result["smc"]
    sig = result["signal"]

    direction = sig["direction"]

    if direction == "BUY":
        emoji = "🟢"

    elif direction == "SELL":
        emoji = "🔴"

    else:
        emoji = "⚪"

    text = (
        "🥇 <b>GOLD SMART V5.9.3</b>\n\n"
        f"{emoji} <b>SIGNAL: {direction}</b>\n"
        f"📊 Score: <b>{sig['score']}/100</b>\n"
        f"🏷 Type: <b>{sig['type']}</b>\n\n"
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
        f"🧩 FVG: {smc['fvg']}\n"
        f"💥 Displacement: "
        f"{smc['displacement']}\n"
        f"📈 Momentum: {smc['momentum']}\n"
        f"⚖️ PD: {smc['pd']}\n"
    )

    if direction != "WAIT":

        text += (
            "\n━━━━━━━━━━━━━━━━━━\n\n"
            f"🎯 ENTRY: "
            f"<b>{sig['entry']:.2f}</b>\n"
            f"🛑 SL: "
            f"<b>{sig['sl']:.2f}</b>\n"
            f"🥇 TP1: "
            f"<b>{sig['tp1']:.2f}</b>\n"
            f"🥈 TP2: "
            f"<b>{sig['tp2']:.2f}</b>\n"
            f"📐 RR: <b>1:3</b>\n"
            f"📦 Lot: <b>{sig['lot']:.2f}</b>\n"
        )

    else:

        text += (
            "\n━━━━━━━━━━━━━━━━━━\n\n"
            "⏳ <b>WAIT</b>\n"
            "Нет достаточного "
            "подтверждения.\n"
        )

    text += (
        "\n━━━━━━━━━━━━━━━━━━\n\n"
        "🛡 AUTO TRADING: <b>OFF</b>\n"
        f"📉 Risk: <b>{RISK_PERCENT:.1f}%</b>\n"
        "🕯 Closed candles: <b>ON</b>\n"
        "📡 Data: <b>XAUS</b>"
    )

    return text


# ============================================================
# STATUS
# ============================================================

def status_text():

    price = get_spot()

    info = webhook_info()

    webhook_ready = False

    if (
        info
        and info.get("ok")
    ):

        remote_url = (
            info.get("result", {})
            .get("url", "")
        )

        webhook_ready = (
            remote_url
            == webhook_url()
        )

    with state_lock:

        total = state[
            "signals_total"
        ]

        full = state[
            "signals_full"
        ]

        early = state[
            "signals_early"
        ]

        wins = state[
            "wins"
        ]

        losses = state[
            "losses"
        ]

        error = state[
            "last_error"
        ]

    return (
        "🥇 <b>GOLD SMART V5.9.3</b>\n\n"
        "🟢 Engine: <b>READY</b>\n"
        f"📡 XAUS: "
        f"<b>{'READY' if price else 'ERROR'}</b>\n"
        f"💰 XAUUSD: "
        f"<b>{f'{price:.2f}' if price else 'N/A'}</b>\n\n"
        f"📨 Webhook: "
        f"<b>{'READY' if webhook_ready else 'CHECK'}</b>\n\n"
        "🛡 AUTO TRADING: <b>OFF</b>\n"
        f"📉 Risk: <b>{RISK_PERCENT:.1f}%</b>\n"
        f"💵 Deposit: <b>${DEPOSIT:.0f}</b>\n\n"
        f"📊 Signals: <b>{total}</b>\n"
        f"🟢 Full: <b>{full}</b>\n"
        f"🟡 Early: <b>{early}</b>\n"
        f"✅ Wins: <b>{wins}</b>\n"
        f"❌ Losses: <b>{losses}</b>\n\n"
        f"🕐 KZ: "
        f"<b>{kz_now().strftime('%H:%M:%S')}</b>\n"
        "📅 Session: <b>12:00–23:00</b>\n\n"
        f"⚠️ Last error: "
        f"<code>{error or 'NONE'}</code>"
    )


# ============================================================
# STATS
# ============================================================

def stats_text():

    with state_lock:

        total = state[
            "signals_total"
        ]

        full = state[
            "signals_full"
        ]

        early = state[
            "signals_early"
        ]

        wins = state[
            "wins"
        ]

        losses = state[
            "losses"
        ]

    closed = wins + losses

    if closed:
        winrate = (
            wins
            / closed
            * 100
        )
    else:
        winrate = 0

    return (
        "📊 <b>GOLD SMART STATS</b>\n\n"
        f"Signals: <b>{total}</b>\n"
        f"Full: <b>{full}</b>\n"
        f"Early: <b>{early}</b>\n\n"
        f"✅ Wins: <b>{wins}</b>\n"
        f"❌ Losses: <b>{losses}</b>\n"
        f"🎯 Winrate: <b>{winrate:.1f}%</b>\n\n"
        "⚠️ Virtual statistics\n"
        "🛡 AUTO TRADING: OFF"
    )


# ============================================================
# HELP
# ============================================================

def help_text():

    return (
        "🥇 <b>GOLD SMART V5.9.3</b>\n\n"
        "Команды:\n\n"
        "/start — запуск\n"
        "/status — состояние\n"
        "/signal — анализ XAU/USD\n"
        "/test — проверка Telegram\n"
        "/stats — статистика\n"
        "/help — помощь\n\n"
        "📡 XAUS\n"
        "📊 H4 → H1 → M15 → M5\n"
        "🛡 AUTO TRADING: OFF"
    )


# ============================================================
# PROCESS SIGNAL
# ============================================================

def process_signal(
    chat_id
):

    try:

        send_message(
            "🔎 <b>GOLD SMART</b>\n\n"
            "Анализирую XAU/USD...\n"
            "H4 → H1 → M15 → M5",
            chat_id
        )

        result = run_analysis()

        if not result.get("ok"):

            send_message(
                format_analysis(result),
                chat_id
            )

            return

        signal = result[
            "signal"
        ]

        if (
            signal["type"]
            == "WAIT"
        ):

            send_message(
                format_analysis(result),
                chat_id
            )

            return

        if not in_session():

            send_message(
                format_analysis(result)
                + "\n\n"
                "⚠️ Вне сессии "
                "12:00–23:00 KZ.",
                chat_id
            )

            return

        with state_lock:

            state[
                "signals_total"
            ] += 1

            if signal["type"] == "FULL":

                state[
                    "signals_full"
                ] += 1

            elif signal["type"] == "EARLY":

                state[
                    "signals_early"
                ] += 1

            state[
                "last_signal"
            ] = signal["direction"]

            state[
                "last_signal_time"
            ] = utc_now().isoformat()

        save_state()

        send_message(
            format_analysis(result),
            chat_id
        )

    except Exception as e:

        print(
            "PROCESS SIGNAL ERROR:",
            e
        )

        send_message(
            "🔴 <b>GOLD SMART ERROR</b>\n\n"
            f"<code>{e}</code>",
            chat_id
        )


# ============================================================
# TELEGRAM UPDATE
# ============================================================

def process_update(
    update
):

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
            "TELEGRAM COMMAND:",
            command,
            "CHAT:",
            chat_id
        )

        if command == "/start":

            send_message(
                "🥇 <b>GOLD SMART V5.9.3</b>\n\n"
                "🟢 Бот работает.\n\n"
                "📡 XAUS: READY\n"
                "🛡 AUTO TRADING: OFF\n\n"
                "Напиши /help",
                chat_id
            )

        elif command == "/help":

            send_message(
                help_text(),
                chat_id
            )

        elif command == "/test":

            send_message(
                "🟢 <b>TEST OK</b>\n\n"
                "Telegram → Render → Bot\n"
                "Webhook работает.",
                chat_id
            )

        elif command == "/status":

            send_message(
                status_text(),
                chat_id
            )

        elif command == "/stats":

            send_message(
                stats_text(),
                chat_id
            )

        elif command == "/signal":

            threading.Thread(
                target=process_signal,
                args=(chat_id,),
                daemon=True
            ).start()

        else:

            send_message(
                "❓ Неизвестная команда.\n\n"
                "Напиши /help",
                chat_id
            )

    except Exception as e:

        print(
            "UPDATE ERROR:",
            e
        )


# ============================================================
# ROUTES
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def index():

    return jsonify(
        {
            "app": "GOLD SMART V5.9.3",
            "status": "READY",
            "auto_trading": False,
            "risk_percent": RISK_PERCENT,
            "data_source": "XAUS",
            "webhook": "/telegram",
        }
    ), 200


@app.route(
    "/health",
    methods=["GET"]
)
def health():

    return jsonify(
        {
            "status": "ok",
            "app": "GOLD SMART V5.9.3",
        }
    ), 200


@app.route(
    "/telegram",
    methods=["POST"]
)
def telegram():

    try:

        update = request.get_json(
            silent=True
        )

        print(
            "POST /telegram RECEIVED"
        )

        if update:

            threading.Thread(
                target=process_update,
                args=(update,),
                daemon=True
            ).start()

        return jsonify(
            {
                "ok": True
            }
        ), 200

    except Exception as e:

        print(
            "TELEGRAM ROUTE ERROR:",
            e
        )

        return jsonify(
            {
                "ok": True
            }
        ), 200


@app.route(
    "/webhook-info",
    methods=["GET"]
)
def webhook_info_route():

    data = webhook_info()

    return jsonify(
        data or {
            "ok": False
        }
    ), 200


# ============================================================
# STARTUP
# ============================================================

def startup():

    print(
        "=================================================="
    )

    print(
        "GOLD SMART V5.9.3"
    )

    print(
        "FLASK APP READY"
    )

    print(
        "AUTO TRADING: OFF"
    )

    print(
        f"RISK: {RISK_PERCENT}%"
    )

    print(
        f"DEPOSIT: ${DEPOSIT:.2f}"
    )

    print(
        "DATA: XAUS"
    )

    print(
        f"WEBHOOK: {webhook_url()}"
    )

    print(
        "=================================================="
    )

    if BOT_TOKEN:

        threading.Thread(
            target=set_webhook,
            daemon=True
        ).start()

    else:

        print(
            "WARNING: BOT_TOKEN is empty"
        )


startup()


# ============================================================
# LOCAL
# ============================================================

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
