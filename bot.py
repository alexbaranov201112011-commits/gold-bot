import os
import time
import threading
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import numpy as np
import telebot

from flask import Flask, request

# ============================================================
# GOLD SMART V3.2
# CLOUD VERSION
# TELEGRAM WEBHOOK
# NO MT5
# NO AUTO TRADING
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "gold-smart-2026")
PORT = int(os.getenv("PORT", "8080"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode="HTML"
)

app = Flask(__name__)

# ------------------------------------------------------------
# SETTINGS
# ------------------------------------------------------------

RISK_PERCENT = 1.0
RR = 2.0

# Approximate gold risk model.
# It is NOT used to execute trades.
# It is only for displaying an indicative lot.
GOLD_LOSS_PER_1LOT_PER_1USD = 100.0

API_TIMEOUT = 15

XAUS_SPOT = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY = "https://xaus.com/api/v1/intraday"


# ============================================================
# DATA
# ============================================================

def get_json(url, params=None):

    try:

        r = requests.get(
            url,
            params=params,
            timeout=API_TIMEOUT
        )

        r.raise_for_status()

        return r.json()

    except Exception as e:

        print("DATA ERROR:", e)

        return None


def get_gold_price():

    data = get_json(
        XAUS_SPOT,
        {
            "compact": "1",
            "fresh": str(int(time.time()))
        }
    )

    if not data:
        return None

    price = data.get("spot_usd_oz")

    if price is None:
        return None

    state = data.get(
        "data_state",
        {}
    )

    return {
        "price": float(price),
        "status": state.get(
            "status",
            "unknown"
        ),
        "source": data.get(
            "price_source",
            "XAUS"
        ),
        "as_of": data.get(
            "price_as_of",
            data.get("updated_at")
        )
    }


# ============================================================
# INTRADAY DATA
# ============================================================

def get_intraday():

    data = get_json(
        XAUS_INTRADAY,
        {
            "symbol": "xau",
            "hours": 48,
            "fresh": str(int(time.time()))
        }
    )

    if not data:
        return None

    points = data.get(
        "points",
        []
    )

    if not points:
        return None

    rows = []

    for p in points:

        try:

            ts = pd.to_datetime(
                p["t"],
                utc=True
            )

            price = float(
                p["p"]
            )

            rows.append(
                {
                    "time": ts,
                    "price": price
                }
            )

        except Exception:
            continue

    if not rows:
        return None

    df = pd.DataFrame(rows)

    df = (
        df
        .drop_duplicates("time")
        .sort_values("time")
        .set_index("time")
    )

    return df


# ============================================================
# OHLC RESAMPLING
# ============================================================

def make_ohlc(df, timeframe):

    if df is None or df.empty:
        return None

    ohlc = df["price"].resample(
        timeframe
    ).agg(
        [
            "first",
            "max",
            "min",
            "last"
        ]
    )

    ohlc.columns = [
        "open",
        "high",
        "low",
        "close"
    ]

    ohlc = ohlc.dropna()

    return ohlc


# ============================================================
# INDICATORS
# ============================================================

def EMA(series, period):

    return series.ewm(
        span=period,
        adjust=False
    ).mean()


def RSI(series, period=14):

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

    rs = avg_gain / avg_loss.replace(
        0,
        np.nan
    )

    return 100 - (
        100 / (1 + rs)
    )


def ATR(df, period=14):

    high_low = (
        df["high"] -
        df["low"]
    )

    high_close = (
        df["high"] -
        df["close"].shift()
    ).abs()

    low_close = (
        df["low"] -
        df["close"].shift()
    ).abs()

    tr = pd.concat(
        [
            high_low,
            high_close,
            low_close
        ],
        axis=1
    ).max(axis=1)

    return tr.rolling(
        period
    ).mean()


def prepare(df):

    df = df.copy()

    df["ema50"] = EMA(
        df["close"],
        50
    )

    df["ema200"] = EMA(
        df["close"],
        200
    )

    df["rsi"] = RSI(
        df["close"]
    )

    df["atr"] = ATR(
        df
    )

    return df


# ============================================================
# TREND
# ============================================================

def trend(df):

    if df is None or len(df) < 50:
        return "UNKNOWN"

    df = prepare(df)

    last = df.iloc[-1]

    if (
        last["ema50"] >
        last["ema200"]
        and
        last["close"] >
        last["ema50"]
    ):
        return "BULLISH"

    if (
        last["ema50"] <
        last["ema200"]
        and
        last["close"] <
        last["ema50"]
    ):
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# STRUCTURE
# ============================================================

def detect_bos(df):

    if len(df) < 15:
        return "—"

    recent = df.iloc[-3]

    previous = df.iloc[-13:-3]

    high = previous["high"].max()
    low = previous["low"].min()

    if recent["close"] > high:
        return "BULLISH"

    if recent["close"] < low:
        return "BEARISH"

    return "—"


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def detect_sweep(df):

    if len(df) < 25:
        return "—"

    candle = df.iloc[-2]

    previous = df.iloc[-22:-2]

    high = previous["high"].max()
    low = previous["low"].min()

    if (
        candle["high"] > high
        and
        candle["close"] < high
    ):
        return "SELL"

    if (
        candle["low"] < low
        and
        candle["close"] > low
    ):
        return "BUY"

    return "—"


# ============================================================
# FVG
# ============================================================

def detect_fvg(df):

    if len(df) < 5:
        return "—"

    a = df.iloc[-4]
    c = df.iloc[-2]

    if c["low"] > a["high"]:
        return "BUY"

    if c["high"] < a["low"]:
        return "SELL"

    return "—"


# ============================================================
# ASIA RANGE
# ============================================================

def get_asia(df):

    if df is None or df.empty:
        return None, None

    local = df.copy()

    local["local"] = (
        local.index +
        pd.Timedelta(hours=5)
    )

    local["hour"] = (
        local["local"].hour
    )

    asia = local[
        (local["hour"] >= 0)
        &
        (local["hour"] < 8)
    ]

    if asia.empty:
        return None, None

    return (
        float(asia["high"].max()),
        float(asia["low"].min())
    )


# ============================================================
# PREVIOUS DAY
# ============================================================

def previous_day(df):

    if df is None or df.empty:
        return None, None

    local = df.copy()

    local["local"] = (
        local.index +
        pd.Timedelta(hours=5)
    )

    local["date"] = (
        local["local"].dt.date
    )

    dates = sorted(
        local["date"].unique()
    )

    if len(dates) < 2:
        return None, None

    prev = dates[-2]

    d = local[
        local["date"] == prev
    ]

    return (
        float(d["high"].max()),
        float(d["low"].min())
    )


# ============================================================
# SESSION
# ============================================================

def get_session():

    now = datetime.now(
        timezone.utc
    )

    hour = (
        now.hour + 5
    ) % 24

    if 7 <= hour < 13:
        return "LONDON 🇬🇧"

    if 13 <= hour < 22:
        return "NEW YORK 🇺🇸"

    if 0 <= hour < 7:
        return "ASIA 🌏"

    return "OFF SESSION"


# ============================================================
# SIGNAL ENGINE
# ============================================================

def analyze():

    gold = get_gold_price()

    if not gold:

        return {
            "error":
            "Не удалось получить цену золота."
        }

    raw = get_intraday()

    if raw is None:

        return {
            "error":
            "Нет intraday данных XAU."
        }

    # 2-minute data -> timeframes

    m15 = make_ohlc(
        raw,
        "15min"
    )

    h1 = make_ohlc(
        raw,
        "1h"
    )

    h4 = make_ohlc(
        raw,
        "4h"
    )

    if (
        m15 is None
        or
        h1 is None
        or
        h4 is None
    ):

        return {
            "error":
            "Недостаточно intraday данных."
        }

    # We cannot honestly calculate EMA200
    # on only 48h of intraday data.
    #
    # Therefore H4/H1 trend uses
    # shorter adaptive structure.

    h4_work = h4.copy()
    h1_work = h1.copy()
    m15_work = m15.copy()

    def adaptive_trend(df):

        if len(df) < 10:
            return "UNKNOWN"

        df = df.copy()

        fast = min(
            5,
            max(3, len(df) // 3)
        )

        slow = min(
            10,
            max(6, len(df) // 2)
        )

        df["fast"] = EMA(
            df["close"],
            fast
        )

        df["slow"] = EMA(
            df["close"],
            slow
        )

        last = df.iloc[-1]

        if (
            last["fast"] >
            last["slow"]
            and
            last["close"] >
            last["fast"]
        ):
            return "BULLISH"

        if (
            last["fast"] <
            last["slow"]
            and
            last["close"] <
            last["fast"]
        ):
            return "BEARISH"

        return "NEUTRAL"

    h4_trend = adaptive_trend(
        h4_work
    )

    h1_trend = adaptive_trend(
        h1_work
    )

    m15 = prepare(
        m15_work
    )

    m15_trend = adaptive_trend(
        m15
    )

    last = m15.iloc[-1]

    current = gold["price"]

    rsi = float(
        last["rsi"]
    )

    atr = float(
        last["atr"]
    )

    bos = detect_bos(
        m15
    )

    sweep = detect_sweep(
        m15
    )

    fvg = detect_fvg(
        m15
    )

    asia_high, asia_low = get_asia(
        m15
    )

    pdh, pdl = previous_day(
        m15
    )

    # --------------------------------------------------------
    # SCORE
    # --------------------------------------------------------

    buy = 0
    sell = 0

    buy_reasons = []
    sell_reasons = []

    # H4
    if h4_trend == "BULLISH":
        buy += 2
        buy_reasons.append(
            "H4 bullish"
        )

    elif h4_trend == "BEARISH":
        sell += 2
        sell_reasons.append(
            "H4 bearish"
        )

    # H1
    if h1_trend == "BULLISH":
        buy += 2
        buy_reasons.append(
            "H1 bullish"
        )

    elif h1_trend == "BEARISH":
        sell += 2
        sell_reasons.append(
            "H1 bearish"
        )

    # M15
    if m15_trend == "BULLISH":
        buy += 1

    elif m15_trend == "BEARISH":
        sell += 1

    # BOS
    if bos == "BULLISH":
        buy += 2
        buy_reasons.append(
            "BOS"
        )

    elif bos == "BEARISH":
        sell += 2
        sell_reasons.append(
            "BOS"
        )

    # Sweep
    if sweep == "BUY":
        buy += 2
        buy_reasons.append(
            "liquidity sweep"
        )

    elif sweep == "SELL":
        sell += 2
        sell_reasons.append(
            "liquidity sweep"
        )

    # FVG
    if fvg == "BUY":
        buy += 1
        buy_reasons.append(
            "FVG"
        )

    elif fvg == "SELL":
        sell += 1
        sell_reasons.append(
            "FVG"
        )

    # RSI protection
    if rsi < 25:
        sell = max(
            0,
            sell - 3
        )

    if rsi > 75:
        buy = max(
            0,
            buy - 3
        )

    # --------------------------------------------------------
    # DECISION
    # --------------------------------------------------------

    decision = "WAIT"
    direction = None

    if (
        buy >= 7
        and
        buy >= sell + 2
        and
        rsi < 75
    ):

        decision = "BUY"
        direction = "BUY"

    elif (
        sell >= 7
        and
        sell >= buy + 2
        and
        rsi > 25
    ):

        decision = "SELL"
        direction = "SELL"

    # --------------------------------------------------------
    # TRADE PLAN
    # --------------------------------------------------------

    entry = None
    sl = None
    tp = None
    lot = None

    if direction == "BUY":

        entry = current

        structure = float(
            m15.iloc[-8:]["low"].min()
        )

        sl = min(
            structure,
            entry - atr * 1.2
        )

        risk = entry - sl

        if risk > 0:

            tp = (
                entry +
                risk * RR
            )

            lot = (
                1.0 /
                (
                    risk *
                    GOLD_LOSS_PER_1LOT_PER_1USD
                )
            )

    elif direction == "SELL":

        entry = current

        structure = float(
            m15.iloc[-8:]["high"].max()
        )

        sl = max(
            structure,
            entry + atr * 1.2
        )

        risk = sl - entry

        if risk > 0:

            tp = (
                entry -
                risk * RR
            )

            lot = (
                1.0 /
                (
                    risk *
                    GOLD_LOSS_PER_1LOT_PER_1USD
                )
            )

    return {
        "price": current,
        "status": gold["status"],
        "as_of": gold["as_of"],

        "session": get_session(),

        "h4": h4_trend,
        "h1": h1_trend,
        "m15": m15_trend,

        "rsi": rsi,
        "atr": atr,

        "bos": bos,
        "sweep": sweep,
        "fvg": fvg,

        "asia_high": asia_high,
        "asia_low": asia_low,

        "pdh": pdh,
        "pdl": pdl,

        "buy": buy,
        "sell": sell,

        "decision": decision,
        "direction": direction,

        "entry": entry,
        "sl": sl,
        "tp": tp,
        "lot": lot,

        "buy_reasons": buy_reasons,
        "sell_reasons": sell_reasons
    }


# ============================================================
# MESSAGE
# ============================================================

def format_signal(d):

    if "error" in d:

        return (
            "⛔ <b>GOLD SMART V3.2</b>\n\n"
            f"{d['error']}"
        )

    if d["decision"] == "BUY":
        icon = "🟢"

    elif d["decision"] == "SELL":
        icon = "🔴"

    else:
        icon = "🟡"

    text = (
        "🧠 <b>GOLD SMART V3.2</b>\n\n"

        f"🥇 XAU/USD: "
        f"<b>{d['price']:.2f}</b>\n"

        f"⏰ Сессия: "
        f"{d['session']}\n\n"

        "━━━━━━━━━━━━━━\n"
        "📊 <b>ТЕХНИКА</b>\n"
        "━━━━━━━━━━━━━━\n"

        f"🔵 H4: {d['h4']}\n"
        f"🔵 H1: {d['h1']}\n"
        f"🟠 M15: {d['m15']}\n\n"

        f"💧 Liquidity Sweep: "
        f"{d['sweep']}\n"

        f"🧱 BOS: {d['bos']}\n"

        f"📦 FVG: {d['fvg']}\n"

        f"📈 RSI: "
        f"{d['rsi']:.1f}\n"

        f"📏 ATR: "
        f"{d['atr']:.2f}\n\n"

        "━━━━━━━━━━━━━━\n"
        "🧠 <b>SCORE</b>\n"
        "━━━━━━━━━━━━━━\n"

        f"🟢 BUY: {d['buy']}\n"
        f"🔴 SELL: {d['sell']}\n\n"

        "━━━━━━━━━━━━━━\n"
        "🎯 <b>РЕШЕНИЕ</b>\n"
        "━━━━━━━━━━━━━━\n"

        f"{icon} <b>{d['decision']}</b>\n"
    )

    if d["direction"]:

        text += (
            "\n━━━━━━━━━━━━━━\n"
            "📌 <b>ПЛАН</b>\n"
            "━━━━━━━━━━━━━━\n"

            f"🎯 Entry: "
            f"{d['entry']:.2f}\n"

            f"🛑 SL: "
            f"{d['sl']:.2f}\n"

            f"💰 TP: "
            f"{d['tp']:.2f}\n"

            f"📦 Lot ≈ "
            f"{d['lot']:.2f}\n"

            f"⚖️ RR: 1:{RR}\n"
        )

    if d["rsi"] < 25:

        text += (
            "\n⚠️ <b>RSI перепродан</b>\n"
            "Не продаём золото просто потому, "
            "что оно падает."
        )

    elif d["rsi"] > 75:

        text += (
            "\n⚠️ <b>RSI перекуплен</b>\n"
            "Не покупаем импульс вслепую."
        )

    text += (
        "\n\nℹ️ Цена: "
        "индикативный XAU/USD spot."
    )

    return text


# ============================================================
# COMMANDS
# ============================================================

@bot.message_handler(
    commands=["start"]
)
def start(message):

    bot.send_message(
        message.chat.id,
        """
🧠 <b>GOLD SMART V3.2</b>

🥇 XAU/USD
📊 H4 → H1 → M15
💧 Liquidity
🧱 BOS
📦 FVG
📈 RSI + ATR

Команды:

/gold — полный анализ
/quick — короткий сигнал
/levels — уровни
/status — состояние бота

⚠️ Только сигналы.
Автоматических сделок нет.
"""
    )


@bot.message_handler(
    commands=["gold"]
)
def gold(message):

    bot.send_message(
        message.chat.id,
        "⏳ Анализирую золото..."
    )

    try:

        result = analyze()

        bot.send_message(
            message.chat.id,
            format_signal(result)
        )

    except Exception as e:

        print(
            "GOLD ERROR:",
            e
        )

        bot.send_message(
            message.chat.id,
            (
                "⛔ Ошибка анализа.\n\n"
                f"<code>{e}</code>"
            )
        )


@bot.message_handler(
    commands=["quick"]
)
def quick(message):

    try:

        d = analyze()

        if "error" in d:

            bot.send_message(
                message.chat.id,
                d["error"]
            )

            return

        icon = {
            "BUY": "🟢",
            "SELL": "🔴",
            "WAIT": "🟡"
        }.get(
            d["decision"],
            "⛔"
        )

        bot.send_message(
            message.chat.id,
            (
                "⚡ <b>GOLD QUICK</b>\n\n"

                f"🥇 {d['price']:.2f}\n\n"

                f"H4: {d['h4']}\n"
                f"H1: {d['h1']}\n"
                f"M15: {d['m15']}\n\n"

                f"🟢 BUY: {d['buy']}\n"
                f"🔴 SELL: {d['sell']}\n\n"

                f"{icon} "
                f"<b>{d['decision']}</b>"
            )
        )

    except Exception as e:

        bot.send_message(
            message.chat.id,
            f"⛔ {e}"
        )


@bot.message_handler(
    commands=["levels"]
)
def levels(message):

    try:

        d = analyze()

        if "error" in d:

            bot.send_message(
                message.chat.id,
                d["error"]
            )

            return

        bot.send_message(
            message.chat.id,
            (
                "📍 <b>GOLD LEVELS</b>\n\n"

                f"🥇 Price: "
                f"{d['price']:.2f}\n\n"

                f"🌏 Asian High: "
                f"{fmt(d['asia_high'])}\n"

                f"🌏 Asian Low: "
                f"{fmt(d['asia_low'])}\n\n"

                f"📅 PDH: "
                f"{fmt(d['pdh'])}\n"

                f"📅 PDL: "
                f"{fmt(d['pdl'])}"
            )
        )

    except Exception as e:

        bot.send_message(
            message.chat.id,
            f"⛔ {e}"
        )


@bot.message_handler(
    commands=["status"]
)
def status(message):

    gold = get_gold_price()

    if not gold:

        bot.send_message(
            message.chat.id,
            "🔴 DATA OFFLINE"
        )

        return

    bot.send_message(
        message.chat.id,
        (
            "🟢 <b>GOLD SMART ONLINE</b>\n\n"

            f"🥇 XAU/USD: "
            f"{gold['price']:.2f}\n"

            f"📡 Source: "
            f"{gold['source']}\n"

            f"🟢 Data: "
            f"{gold['status']}\n"

            f"⏰ Session: "
            f"{get_session()}"
        )
    )


def fmt(value):

    if value is None:
        return "—"

    return f"{value:.2f}"


# ============================================================
# TEXT COMMAND
# ============================================================

@bot.message_handler(
    func=lambda message:
        message.text
        and
        message.text.lower().strip()
        in [
            "gold",
            "золото"
        ]
)
def gold_text(message):

    gold(message)


# ============================================================
# WEBHOOK
# ============================================================

@app.route(
    "/",
    methods=["GET", "HEAD"]
)
def home():

    return "GOLD SMART V3.2 ONLINE", 200


@app.route(
    "/telegram",
    methods=["POST"]
)
def telegram_webhook():

    if request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    ) != WEBHOOK_SECRET:

        return "forbidden", 403

    try:

        update = telebot.types.Update.de_json(
            request.data.decode(
                "utf-8"
            )
        )

        bot.process_new_updates(
            [update]
        )

        return "OK", 200

    except Exception as e:

        print(
            "WEBHOOK ERROR:",
            e
        )

        return "error", 500


# ============================================================
# SET WEBHOOK
# ============================================================

def setup_webhook():

    render_url = os.getenv(
        "RENDER_EXTERNAL_URL"
    )

    if not render_url:

        print(
            "RENDER_EXTERNAL_URL missing"
        )

        return

    webhook_url = (
        render_url.rstrip("/")
        +
        "/telegram"
    )

    try:

        bot.remove_webhook()

        time.sleep(1)

        bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True
        )

        print(
            "WEBHOOK SET:",
            webhook_url
        )

    except Exception as e:

        print(
            "WEBHOOK ERROR:",
            e
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    print(
        "================================"
    )

    print(
        "GOLD SMART V3.2"
    )

    print(
        "CLOUD MODE"
    )

    print(
        "WEBHOOK MODE"
    )

    print(
        "NO AUTO TRADING"
    )

    print(
        "================================"
    )

    setup_webhook()

    app.run(
        host="0.0.0.0",
        port=PORT
    )
