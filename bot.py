# -*- coding: utf-8 -*-
"""
GOLD SMART V3
XAUUSD / GC=F
M15 + H1/H4 trend filter
Liquidity sweep + BOS
EMA 50/200
RSI
Realistic backtest
Risk / RR / BE calculations
Telegram + Flask webhook

ВАЖНО:
Это аналитический/сигнальный бот.
Он НЕ открывает сделки у брокера автоматически.
"""

import os
import time
import threading
import requests
import telebot
from flask import Flask, request
import yfinance as yf
import pandas as pd
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from datetime import datetime
import pytz


# ============================================================
# SETTINGS
# ============================================================

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)

API = f"https://api.telegram.org/bot{TOKEN}"

AKTAU = pytz.timezone("Asia/Aqtau")

TICKER = "GC=F"

# Анализ
M15_PERIOD = "30d"
H1_PERIOD = "60d"

# Risk
DEFAULT_BALANCE = 1000.0
RISK_PERCENT = 1.0

# Максимум сделок в сигнале
MAX_SIGNALS_PER_DAY = 3

# Минимальный RR
MIN_RR = 1.8

# Минимальный score
MIN_SCORE = 5

# Trading session — Aktau
SESSION_START = 12
SESSION_END = 23

# ATR
ATR_PERIOD = 14

# EMA
EMA_FAST = 50
EMA_SLOW = 200


# ============================================================
# TELEGRAM
# ============================================================

def send(chat_id, text):
    try:
        requests.post(
            f"{API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown"
            },
            timeout=15
        )
    except Exception as e:
        print("Telegram error:", e)


def send_photo(chat_id, path, caption):
    try:
        with open(path, "rb") as f:
            requests.post(
                f"{API}/sendPhoto",
                data={
                    "chat_id": chat_id,
                    "caption": caption,
                    "parse_mode": "Markdown"
                },
                files={"photo": f},
                timeout=25
            )
    except Exception as e:
        print("Photo error:", e)


# ============================================================
# DATA
# ============================================================

def clean_columns(df):
    """
    yfinance иногда возвращает MultiIndex.
    Приводим его к обычному OHLC.
    """

    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            c[0] if isinstance(c, tuple) else c
            for c in df.columns
        ]

    required = ["Open", "High", "Low", "Close"]

    for col in required:
        if col not in df.columns:
            return pd.DataFrame()

    df = df[required].copy()

    for col in required:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df.dropna(inplace=True)

    return df


def get_m15():
    try:
        df = yf.download(
            TICKER,
            period=M15_PERIOD,
            interval="15m",
            progress=False,
            auto_adjust=False,
            threads=False
        )

        return clean_columns(df)

    except Exception as e:
        print("M15 error:", e)
        return pd.DataFrame()


def get_h1():
    try:
        df = yf.download(
            TICKER,
            period=H1_PERIOD,
            interval="1h",
            progress=False,
            auto_adjust=False,
            threads=False
        )

        return clean_columns(df)

    except Exception as e:
        print("H1 error:", e)
        return pd.DataFrame()


# ============================================================
# INDICATORS
# ============================================================

def add_indicators(df):

    df = df.copy()

    df["EMA50"] = df["Close"].ewm(
        span=50,
        adjust=False
    ).mean()

    df["EMA200"] = df["Close"].ewm(
        span=200,
        adjust=False
    ).mean()

    delta = df["Close"].diff()

    gain = delta.clip(lower=0)

    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    df["RSI"] = 100 - (100 / (1 + rs))

    tr1 = df["High"] - df["Low"]

    tr2 = abs(
        df["High"] - df["Close"].shift(1)
    )

    tr3 = abs(
        df["Low"] - df["Close"].shift(1)
    )

    tr = pd.concat(
        [tr1, tr2, tr3],
        axis=1
    ).max(axis=1)

    df["ATR"] = tr.ewm(
        alpha=1 / ATR_PERIOD,
        adjust=False
    ).mean()

    return df.dropna()


# ============================================================
# STRUCTURE
# ============================================================

def get_structure(df, lookback=12):

    recent = df.iloc[-lookback:]

    high = float(recent["High"].max())
    low = float(recent["Low"].min())

    return high, low


def detect_liquidity_sweep(df):

    if len(df) < 20:
        return None

    previous = df.iloc[-13:-1]

    current = df.iloc[-1]

    previous_high = float(previous["High"].max())
    previous_low = float(previous["Low"].min())

    # Sweep high -> bearish rejection
    if (
        current["High"] > previous_high
        and current["Close"] < previous_high
    ):
        return "SELL_SWEEP"

    # Sweep low -> bullish rejection
    if (
        current["Low"] < previous_low
        and current["Close"] > previous_low
    ):
        return "BUY_SWEEP"

    return None


def detect_bos(df):

    if len(df) < 15:
        return None

    previous = df.iloc[-13:-1]

    current = df.iloc[-1]

    previous_high = float(previous["High"].max())
    previous_low = float(previous["Low"].min())

    if current["Close"] > previous_high:
        return "BULLISH_BOS"

    if current["Close"] < previous_low:
        return "BEARISH_BOS"

    return None


# ============================================================
# TREND
# ============================================================

def trend_from_df(df):

    if len(df) < 210:
        return "NEUTRAL"

    last = df.iloc[-1]

    price = float(last["Close"])
    ema50 = float(last["EMA50"])
    ema200 = float(last["EMA200"])

    if price > ema50 > ema200:
        return "BULLISH"

    if price < ema50 < ema200:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# SESSION
# ============================================================

def session_status():

    now = datetime.now(AKTAU)

    hour = now.hour

    if hour < SESSION_START:
        return "WAIT", False

    if hour >= SESSION_END:
        return "WAIT", False

    if hour < 17:
        return "LONDON", True

    return "NEW YORK", True


# ============================================================
# SIGNAL ENGINE
# ============================================================

def build_signal():

    m15 = get_m15()
    h1 = get_h1()

    if m15.empty or h1.empty:

        return {
            "status": "ERROR",
            "message": "Не удалось получить данные GOLD."
        }

    m15 = add_indicators(m15)
    h1 = add_indicators(h1)

    if len(m15) < 250 or len(h1) < 210:

        return {
            "status": "ERROR",
            "message": "Недостаточно исторических данных."
        }

    last = m15.iloc[-1]

    price = float(last["Close"])

    rsi = float(last["RSI"])

    atr = float(last["ATR"])

    ema50 = float(last["EMA50"])

    ema200 = float(last["EMA200"])

    h1_trend = trend_from_df(h1)

    m15_trend = trend_from_df(m15)

    session, can_trade = session_status()

    asia_high, asia_low = get_structure(
        m15,
        lookback=32
    )

    sweep = detect_liquidity_sweep(m15)

    bos = detect_bos(m15)

    score_buy = 0
    score_sell = 0

    reasons_buy = []
    reasons_sell = []

    # ========================================================
    # TREND
    # ========================================================

    if h1_trend == "BULLISH":
        score_buy += 2
        reasons_buy.append("H1 bullish")

    if h1_trend == "BEARISH":
        score_sell += 2
        reasons_sell.append("H1 bearish")

    if m15_trend == "BULLISH":
        score_buy += 1
        reasons_buy.append("M15 bullish")

    if m15_trend == "BEARISH":
        score_sell += 1
        reasons_sell.append("M15 bearish")

    # ========================================================
    # EMA
    # ========================================================

    if price > ema50 > ema200:
        score_buy += 1
        reasons_buy.append("EMA50 > EMA200")

    if price < ema50 < ema200:
        score_sell += 1
        reasons_sell.append("EMA50 < EMA200")

    # ========================================================
    # RSI
    # ========================================================

    if 50 <= rsi <= 70:
        score_buy += 1
        reasons_buy.append(f"RSI {rsi:.0f}")

    if 30 <= rsi <= 50:
        score_sell += 1
        reasons_sell.append(f"RSI {rsi:.0f}")

    # ========================================================
    # LIQUIDITY SWEEP
    # ========================================================

    if sweep == "BUY_SWEEP":
        score_buy += 2
        reasons_buy.append("Liquidity sweep LOW")

    if sweep == "SELL_SWEEP":
        score_sell += 2
        reasons_sell.append("Liquidity sweep HIGH")

    # ========================================================
    # BOS
    # ========================================================

    if bos == "BULLISH_BOS":
        score_buy += 2
        reasons_buy.append("Bullish BOS")

    if bos == "BEARISH_BOS":
        score_sell += 2
        reasons_sell.append("Bearish BOS")

    # ========================================================
    # SESSION FILTER
    # ========================================================

    if not can_trade:

        return {
            "status": "WAIT",
            "price": price,
            "session": session,
            "rsi": rsi,
            "h1_trend": h1_trend,
            "m15_trend": m15_trend,
            "asia_high": asia_high,
            "asia_low": asia_low,
            "score_buy": score_buy,
            "score_sell": score_sell,
            "message": "Вне торговой сессии."
        }

    # ========================================================
    # SIGNAL
    # ========================================================

    signal = "WAIT"

    score = max(score_buy, score_sell)

    entry = 0
    sl = 0
    tp1 = 0
    tp2 = 0

    reasons = []

    # BUY
    if (
        score_buy >= MIN_SCORE
        and score_buy > score_sell
        and h1_trend == "BULLISH"
    ):

        signal = "BUY"

        entry = price

        structural_sl = min(
            asia_low,
            float(m15["Low"].tail(8).min())
        )

        sl = structural_sl - atr * 0.25

        risk = entry - sl

        if risk > 0:

            tp1 = entry + risk * 1.8
            tp2 = entry + risk * 2.5

            reasons = reasons_buy

    # SELL
    elif (
        score_sell >= MIN_SCORE
        and score_sell > score_buy
        and h1_trend == "BEARISH"
    ):

        signal = "SELL"

        entry = price

        structural_sl = max(
            asia_high,
            float(m15["High"].tail(8).max())
        )

        sl = structural_sl + atr * 0.25

        risk = sl - entry

        if risk > 0:

            tp1 = entry - risk * 1.8
            tp2 = entry - risk * 2.5

            reasons = reasons_sell

    # ========================================================
    # FINAL RR CHECK
    # ========================================================

    rr = 0

    if signal == "BUY" and entry > sl:
        rr = (tp2 - entry) / (entry - sl)

    elif signal == "SELL" and sl > entry:
        rr = (entry - tp2) / (sl - entry)

    if signal != "WAIT" and rr < MIN_RR:
        signal = "WAIT"
        entry = sl = tp1 = tp2 = 0
        reasons = []

    return {
        "status": signal,
        "price": price,
        "session": session,
        "rsi": rsi,
        "atr": atr,
        "ema50": ema50,
        "ema200": ema200,
        "h1_trend": h1_trend,
        "m15_trend": m15_trend,
        "asia_high": asia_high,
        "asia_low": asia_low,
        "sweep": sweep,
        "bos": bos,
        "score_buy": score_buy,
        "score_sell": score_sell,
        "score": score,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "rr": rr,
        "reasons": reasons,
        "df": m15.tail(100)
    }


# ============================================================
# BACKTEST
# ============================================================

def backtest():

    df = get_m15()

    if df.empty:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "winrate": 0,
            "expectancy": 0,
            "profit_factor": 0
        }

    df = add_indicators(df)

    trades = []

    # Используем только закрытые свечи.
    # Следующие свечи используются только для проверки результата.

    for i in range(220, len(df) - 12):

        hist = df.iloc[:i].copy()

        last = hist.iloc[-1]

        price = float(last["Close"])

        h1trend = trend_from_df(hist)

        if h1trend not in ["BULLISH", "BEARISH"]:
            continue

        trend = trend_from_df(hist)

        sweep = detect_liquidity_sweep(hist)

        bos = detect_bos(hist)

        rsi = float(last["RSI"])

        atr = float(last["ATR"])

        score_buy = 0
        score_sell = 0

        if trend == "BULLISH":
            score_buy += 2

        if trend == "BEARISH":
            score_sell += 2

        if price > last["EMA50"] > last["EMA200"]:
            score_buy += 1

        if price < last["EMA50"] < last["EMA200"]:
            score_sell += 1

        if 50 <= rsi <= 70:
            score_buy += 1

        if 30 <= rsi <= 50:
            score_sell += 1

        if sweep == "BUY_SWEEP":
            score_buy += 2

        if sweep == "SELL_SWEEP":
            score_sell += 2

        if bos == "BULLISH_BOS":
            score_buy += 2

        if bos == "BEARISH_BOS":
            score_sell += 2

        direction = None

        if (
            score_buy >= MIN_SCORE
            and score_buy > score_sell
            and h1trend == "BULLISH"
        ):
            direction = "BUY"

        elif (
            score_sell >= MIN_SCORE
            and score_sell > score_buy
            and h1trend == "BEARISH"
        ):
            direction = "SELL"

        if direction is None:
            continue

        recent = hist.tail(8)

        if direction == "BUY":

            sl = min(
                float(hist["Low"].tail(32).min()),
                float(recent["Low"].min())
            ) - atr * 0.25

            risk = price - sl

            if risk <= 0:
                continue

            tp = price + risk * 2.0

        else:

            sl = max(
                float(hist["High"].tail(32).max()),
                float(recent["High"].max())
            ) + atr * 0.25

            risk = sl - price

            if risk <= 0:
                continue

            tp = price - risk * 2.0

        future = df.iloc[i + 1:i + 13]

        result = None

        for _, candle in future.iterrows():

            high = float(candle["High"])
            low = float(candle["Low"])

            if direction == "BUY":

                hit_sl = low <= sl
                hit_tp = high >= tp

                if hit_sl and hit_tp:
                    # Консервативно считаем SL первым.
                    result = -1
                    break

                if hit_sl:
                    result = -1
                    break

                if hit_tp:
                    result = 1
                    break

            else:

                hit_sl = high >= sl
                hit_tp = low <= tp

                if hit_sl and hit_tp:
                    result = -1
                    break

                if hit_sl:
                    result = -1
                    break

                if hit_tp:
                    result = 1
                    break

        if result is not None:
            trades.append(result)

    total = len(trades)

    wins = sum(
        1 for x in trades if x == 1
    )

    losses = sum(
        1 for x in trades if x == -1
    )

    winrate = (
        wins / total * 100
        if total
        else 0
    )

    expectancy = (
        sum(trades) / total
        if total
        else 0
    )

    gross_profit = sum(
        x for x in trades if x > 0
    )

    gross_loss = abs(
        sum(x for x in trades if x < 0)
    )

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else 0
    )

    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "winrate": winrate,
        "expectancy": expectancy,
        "profit_factor": profit_factor
    }


# ============================================================
# CHART
# ============================================================

def make_chart(r, path="/tmp/gold_v3.png"):

    df = r["df"]

    plt.figure(figsize=(12, 6))

    plt.plot(
        df["Close"].values,
        label="GOLD"
    )

    plt.plot(
        df["EMA50"].values,
        label="EMA50"
    )

    plt.plot(
        df["EMA200"].values,
        label="EMA200"
    )

    plt.axhline(
        r["asia_high"],
        linestyle="--",
        label=f"Structure H {r['asia_high']:.2f}"
    )

    plt.axhline(
        r["asia_low"],
        linestyle="--",
        label=f"Structure L {r['asia_low']:.2f}"
    )

    if r["status"] in ["BUY", "SELL"]:

        plt.axhline(
            r["entry"],
            linestyle="-",
            label=f"ENTRY {r['entry']:.2f}"
        )

        plt.axhline(
            r["sl"],
            linestyle=":",
            label=f"SL {r['sl']:.2f}"
        )

        plt.axhline(
            r["tp1"],
            linestyle=":",
            label=f"TP1 {r['tp1']:.2f}"
        )

        plt.axhline(
            r["tp2"],
            linestyle=":",
            label=f"TP2 {r['tp2']:.2f}"
        )

    plt.title(
        f"GOLD SMART V3 | {r['status']} | "
        f"Score {r['score_buy']}/{r['score_sell']}"
    )

    plt.legend(fontsize=8)

    plt.grid(alpha=0.25)

    plt.tight_layout()

    plt.savefig(path)

    plt.close()

    return path


# ============================================================
# MESSAGE
# ============================================================

def format_signal(r, stats):

    if r["status"] == "ERROR":

        return f"❌ *GOLD SMART V3*\n\n{r['message']}"

    if r["status"] == "WAIT":

        return (
            "🧠 *GOLD SMART V3*\n\n"
            f"💰 Price: `${r['price']:.2f}`\n"
            f"⏰ Session: `{r['session']}`\n"
            f"📈 H1: `{r['h1_trend']}`\n"
            f"📊 M15: `{r['m15_trend']}`\n"
            f"RSI: `{r['rsi']:.0f}`\n\n"
            f"📦 Structure: `{r['asia_low']:.2f} - {r['asia_high']:.2f}`\n"
            f"🧠 Score BUY: `{r['score_buy']}`\n"
            f"🧠 Score SELL: `{r['score_sell']}`\n\n"
            "⏳ *WAIT — подтверждения недостаточно.*\n\n"
            f"📊 Backtest: `{stats['trades']}` trades\n"
            f"WinRate: `{stats['winrate']:.1f}%`\n"
            f"PF: `{stats['profit_factor']:.2f}`"
        )

    direction_emoji = "🟢" if r["status"] == "BUY" else "🔴"

    msg = (
        "🧠 *GOLD SMART V3*\n\n"
        f"{direction_emoji} *{r['status']}*\n\n"
        f"💰 Entry: `${r['entry']:.2f}`\n"
        f"🛑 SL: `${r['sl']:.2f}`\n"
        f"🎯 TP1: `${r['tp1']:.2f}`\n"
        f"🎯 TP2: `${r['tp2']:.2f}`\n"
        f"📐 RR: `1:{r['rr']:.2f}`\n\n"
        f"📈 H1: `{r['h1_trend']}`\n"
        f"📊 M15: `{r['m15_trend']}`\n"
        f"RSI: `{r['rsi']:.0f}`\n"
        f"Score: `{r['score']}`\n\n"
        "🔎 *Confirmation:*\n"
    )

    for reason in r["reasons"]:
        msg += f"• {reason}\n"

    msg += (
        "\n📊 *REAL BACKTEST*\n"
        f"Trades: `{stats['trades']}`\n"
        f"Wins: `{stats['wins']}`\n"
        f"Losses: `{stats['losses']}`\n"
        f"WinRate: `{stats['winrate']:.1f}%`\n"
        f"Expectancy: `{stats['expectancy']:+.3f}R`\n"
        f"Profit Factor: `{stats['profit_factor']:.2f}`\n\n"
        "⚠️ Сигнал аналитический. Перед реальной торговлей "
        "нужен форвард-тест."
    )

    return msg


# ============================================================
# GOLD COMMAND
# ============================================================

def do_gold(chat_id):

    try:

        send(
            chat_id,
            "🧠 *GOLD SMART V3*\n"
            "Собираю M15/H1 данные и проверяю структуру..."
        )

        r = build_signal()

        stats = backtest()

        msg = format_signal(
            r,
            stats
        )

        if r["status"] in ["BUY", "SELL"]:

            chart = make_chart(r)

            send_photo(
                chat_id,
                chart,
                msg
            )

        else:

            send(
                chat_id,
                msg
            )

    except Exception as e:

        import traceback

        print(
            traceback.format_exc()
        )

        send(
            chat_id,
            f"❌ Error: `{e}`"
        )


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

@bot.message_handler(commands=["start"])
def start_command(message):

    send(
        message.chat.id,
        "🧠 *GOLD SMART V3*\n\n"
        "Команды:\n"
        "`/gold` — анализ GOLD\n"
        "`/stats` — backtest\n"
        "`/status` — статус бота\n\n"
        "Или просто напиши *gold*."
    )


@bot.message_handler(commands=["gold"])
def gold_command(message):

    threading.Thread(
        target=do_gold,
        args=(message.chat.id,),
        daemon=True
    ).start()


@bot.message_handler(commands=["stats"])
def stats_command(message):

    try:

        send(
            message.chat.id,
            "📊 Запускаю реальный backtest..."
        )

        stats = backtest()

        msg = (
            "📊 *GOLD SMART V3 BACKTEST*\n\n"
            f"Trades: `{stats['trades']}`\n"
            f"Wins: `{stats['wins']}`\n"
            f"Losses: `{stats['losses']}`\n"
            f"WinRate: `{stats['winrate']:.1f}%`\n"
            f"Expectancy: `{stats['expectancy']:+.3f}R`\n"
            f"Profit Factor: `{stats['profit_factor']:.2f}`"
        )

        send(
            message.chat.id,
            msg
        )

    except Exception as e:

        send(
            message.chat.id,
            f"❌ `{e}`"
        )


@bot.message_handler(commands=["status"])
def status_command(message):

    now = datetime.now(AKTAU)

    send(
        message.chat.id,
        "🟢 *GOLD SMART V3 ONLINE*\n\n"
        f"Time: `{now.strftime('%Y-%m-%d %H:%M:%S')}`\n"
        "Market: `XAUUSD / GC=F`\n"
        "Timeframe: `M15`\n"
        "Trend: `H1/H4-style filter`\n"
        "Risk model: `1%`\n"
        "Martingale: `OFF`\n"
        "Auto trading: `OFF`"
    )


@bot.message_handler(
    func=lambda m:
    "gold" in (m.text or "").lower()
)
def text_gold(message):

    threading.Thread(
        target=do_gold,
        args=(message.chat.id,),
        daemon=True
    ).start()


# ============================================================
# FLASK
# ============================================================

@app.route("/")
def home():

    return "GOLD SMART V3 ONLINE"


@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():

    try:

        update = telebot.types.Update.de_json(
            request.get_data().decode("utf-8")
        )

        bot.process_new_updates(
            [update]
        )

    except Exception as e:

        print(
            "Webhook error:",
            e
        )

    return "", 200


@app.route("/setwebhook")
def set_webhook():

    try:

        bot.remove_webhook()

        result = bot.set_webhook(
            url=f"https://gold-bot-q8la.onrender.com/{TOKEN}"
        )

        return f"SET {result}"

    except Exception as e:

        return f"ERROR {e}"


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
