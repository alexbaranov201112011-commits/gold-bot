# ============================================================
# 🧠 GOLD SMART V3.1
# XAU/USD Telegram Analyzer
# 🇷🇺 Русский язык | Только сигналы | Без автоторговли
#
# Команды:
# /gold   - полный анализ
# /quick  - краткий анализ
# /news   - экономический календарь
# /levels - ключевые уровни
# /stats  - статистика backtest
# /status - статус бота
#
# Требуемые переменные Replit:
# BOT_TOKEN
#
# pip install:
# pyTelegramBotAPI pandas numpy yfinance requests flask pytz
# ============================================================

import os
import time
import math
import threading
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
import yfinance as yf
import pytz
import telebot

from flask import Flask


# ============================================================
# НАСТРОЙКИ
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN в Secrets/Replit.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

TZ = pytz.timezone("Asia/Aqtau")

# ВАЖНО:
# GC=F = COMEX Gold Futures.
# Для точной котировки брокера в будущем можно заменить
# источник на MT5/API брокера.
GOLD_TICKER = "GC=F"

# Дополнительные инструменты
DXY_TICKER = "DX-Y.NYB"
US10Y_TICKER = "^TNX"

# История
M15_PERIOD = "60d"
H1_PERIOD = "730d"
H4_PERIOD = "730d"

# Риск
DEFAULT_BALANCE = 1000.0
RISK_PERCENT = 1.0

# Сигнал
MIN_SCORE = 8
MIN_RR = 2.0

# Торговые часы Актобе/Актау
SESSION_START = 12
SESSION_END = 23

# Новости
NEWS_BLOCK_BEFORE_MIN = 30
NEWS_BLOCK_AFTER_MIN = 30


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "GOLD SMART V3.1 ONLINE"


@app.route("/health")
def health():
    return "OK"


def run_server():
    app.run(host="0.0.0.0", port=8080)


threading.Thread(target=run_server, daemon=True).start()


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def now_local():
    return datetime.now(TZ)


def fmt_price(x):
    if x is None or pd.isna(x):
        return "—"
    return f"{float(x):.2f}"


def safe_float(x):
    try:
        return float(x)
    except Exception:
        return np.nan


# ============================================================
# ЗАГРУЗКА ДАННЫХ
# ============================================================

def download_data(ticker, interval, period):
    try:
        df = yf.download(
            ticker,
            interval=interval,
            period=period,
            auto_adjust=False,
            progress=False,
            threads=False
        )

        if df is None or df.empty:
            return pd.DataFrame()

        # Иногда yfinance возвращает MultiIndex
        if isinstance(df.columns, pd.MultiIndex):
            try:
                df.columns = df.columns.get_level_values(0)
            except Exception:
                df.columns = [
                    c[0] if isinstance(c, tuple) else c
                    for c in df.columns
                ]

        required = ["Open", "High", "Low", "Close", "Volume"]

        for col in required:
            if col not in df.columns:
                if col == "Volume":
                    df["Volume"] = 0
                else:
                    return pd.DataFrame()

        df = df[required].copy()

        for col in required:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)

        # Приводим время к UTC → потом локально
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")

        return df

    except Exception as e:
        print("DATA ERROR:", ticker, interval, e)
        return pd.DataFrame()


def closed_candles(df):
    """
    Используем только полностью закрытые свечи.
    Последнюю свечу исключаем, чтобы не было сигнала
    по незакрытой свече.
    """
    if df.empty or len(df) < 10:
        return df

    return df.iloc[:-1].copy()


# ============================================================
# ИНДИКАТОРЫ
# ============================================================

def add_indicators(df):
    df = df.copy()

    if df.empty:
        return df

    # EMA
    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()
    df["EMA200"] = df["Close"].ewm(span=200, adjust=False).mean()

    # RSI
    delta = df["Close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / 14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)

    df["RSI"] = 100 - (100 / (1 + rs))

    # ATR
    prev_close = df["Close"].shift(1)

    tr1 = df["High"] - df["Low"]
    tr2 = (df["High"] - prev_close).abs()
    tr3 = (df["Low"] - prev_close).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    df["ATR"] = tr.rolling(14).mean()

    # ATR relative
    df["ATR_PCT"] = (
        df["ATR"] / df["Close"] * 100
    )

    # Volume MA
    df["VolumeMA"] = df["Volume"].rolling(20).mean()

    return df


# ============================================================
# TREND
# ============================================================

def get_trend(df):
    if df.empty or len(df) < 205:
        return "UNKNOWN"

    row = df.iloc[-1]

    close = row["Close"]
    ema50 = row["EMA50"]
    ema200 = row["EMA200"]

    if close > ema50 > ema200:
        return "BULLISH"

    if close < ema50 < ema200:
        return "BEARISH"

    return "NEUTRAL"


# ============================================================
# MARKET STRUCTURE
# ============================================================

def swing_high(df, lookback=3):
    if len(df) < lookback * 2 + 1:
        return np.nan

    highs = df["High"]

    recent = highs.iloc[-lookback * 2 - 1:]

    center = recent.iloc[lookback]

    left = recent.iloc[:lookback]
    right = recent.iloc[lookback + 1:]

    if center > left.max() and center > right.max():
        return float(center)

    return np.nan


def swing_low(df, lookback=3):
    if len(df) < lookback * 2 + 1:
        return np.nan

    lows = df["Low"]

    recent = lows.iloc[-lookback * 2 - 1:]

    center = recent.iloc[lookback]

    left = recent.iloc[:lookback]
    right = recent.iloc[lookback + 1:]

    if center < left.min() and center < right.min():
        return float(center)

    return np.nan


def market_structure(df):
    if len(df) < 20:
        return "UNKNOWN"

    highs = []
    lows = []

    for i in range(3, len(df) - 3):
        h = df["High"].iloc[i]
        l = df["Low"].iloc[i]

        if (
            h > df["High"].iloc[i - 3:i].max()
            and h > df["High"].iloc[i + 1:i + 4].max()
        ):
            highs.append(h)

        if (
            l < df["Low"].iloc[i - 3:i].min()
            and l < df["Low"].iloc[i + 1:i + 4].min()
        ):
            lows.append(l)

    if len(highs) < 2 or len(lows) < 2:
        return "UNKNOWN"

    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "HH/HL"

    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "LH/LL"

    return "RANGE"


# ============================================================
# ASIA HIGH / LOW
# ============================================================

def asia_levels(df):
    if df.empty:
        return np.nan, np.nan

    local = df.copy()
    local.index = local.index.tz_convert(TZ)

    today = now_local().date()

    # Азия: 00:00–08:00 Aktau
    day = local[
        (local.index.date == today) &
        (local.index.hour >= 0) &
        (local.index.hour < 8)
    ]

    # Если сегодня мало данных — берём предыдущий день
    if len(day) < 3:
        prev_day = today - timedelta(days=1)

        day = local[
            (local.index.date == prev_day) &
            (local.index.hour >= 0) &
            (local.index.hour < 8)
        ]

    if day.empty:
        return np.nan, np.nan

    return float(day["High"].max()), float(day["Low"].min())


# ============================================================
# PDH / PDL
# ============================================================

def previous_day_levels(df):
    if df.empty:
        return np.nan, np.nan

    local = df.copy()
    local.index = local.index.tz_convert(TZ)

    today = now_local().date()
    prev = today - timedelta(days=1)

    day = local[local.index.date == prev]

    if day.empty:
        return np.nan, np.nan

    return float(day["High"].max()), float(day["Low"].min())


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def liquidity_sweep(df, asia_high, asia_low, pdh, pdl):
    if len(df) < 3:
        return "NONE"

    c = df.iloc[-1]

    # Sweep sell-side liquidity:
    # цена пробила минимум и закрылась обратно выше
    sell_sweep = False

    for level in [asia_low, pdl]:
        if not np.isnan(level):
            if c["Low"] < level and c["Close"] > level:
                sell_sweep = True

    # Sweep buy-side liquidity:
    # цена пробила максимум и закрылась обратно ниже
    buy_sweep = False

    for level in [asia_high, pdh]:
        if not np.isnan(level):
            if c["High"] > level and c["Close"] < level:
                buy_sweep = True

    if sell_sweep:
        return "SELL_SIDE"

    if buy_sweep:
        return "BUY_SIDE"

    return "NONE"


# ============================================================
# BOS / CHOCH
# ============================================================

def detect_bos(df):
    if len(df) < 15:
        return "NONE"

    recent = df.iloc[-8:-1]

    last = df.iloc[-1]

    prev_high = recent["High"].max()
    prev_low = recent["Low"].min()

    if last["Close"] > prev_high:
        return "BULLISH_BOS"

    if last["Close"] < prev_low:
        return "BEARISH_BOS"

    return "NONE"


# ============================================================
# FVG
# ============================================================

def detect_fvg(df):
    if len(df) < 5:
        return None

    a = df.iloc[-3]
    b = df.iloc[-2]
    c = df.iloc[-1]

    # Bullish FVG
    if c["Low"] > a["High"]:
        return {
            "type": "BULLISH",
            "low": float(a["High"]),
            "high": float(c["Low"]),
            "ce": float((a["High"] + c["Low"]) / 2)
        }

    # Bearish FVG
    if c["High"] < a["Low"]:
        return {
            "type": "BEARISH",
            "low": float(c["High"]),
            "high": float(a["Low"]),
            "ce": float((c["High"] + a["Low"]) / 2)
        }

    return None


# ============================================================
# DXY / US10Y
# ============================================================

def get_external_market():
    result = {
        "dxy": np.nan,
        "dxy_change": np.nan,
        "us10y": np.nan,
        "us10y_change": np.nan
    }

    try:
        dxy = download_data(DXY_TICKER, "1h", "5d")

        if not dxy.empty and len(dxy) >= 3:
            result["dxy"] = float(dxy["Close"].iloc[-1])

            old = float(dxy["Close"].iloc[-2])

            if old != 0:
                result["dxy_change"] = (
                    result["dxy"] - old
                ) / old * 100

    except Exception as e:
        print("DXY:", e)

    try:
        us10y = download_data(US10Y_TICKER, "1h", "5d")

        if not us10y.empty and len(us10y) >= 3:
            result["us10y"] = float(us10y["Close"].iloc[-1])

            old = float(us10y["Close"].iloc[-2])

            if old != 0:
                result["us10y_change"] = (
                    result["us10y"] - old
                ) / old * 100

    except Exception as e:
        print("US10Y:", e)

    return result


# ============================================================
# ЭКОНОМИЧЕСКИЙ КАЛЕНДАРЬ
# ============================================================

def get_economic_calendar():
    """
    Используем публичный Myfxbook calendar.

    Если сайт недоступен, бот НЕ придумывает новости.
    """

    url = "https://www.myfxbook.com/forex-economic-calendar/USD%2CCVX"

    try:
        r = requests.get(
            url,
            timeout=10,
            headers={
                "User-Agent": "Mozilla/5.0"
            }
        )

        if r.status_code != 200:
            return []

        text = r.text.lower()

        events = []

        important_words = [
            "fomc",
            "fed",
            "interest rate",
            "cpi",
            "core pce",
            "pce",
            "nonfarm",
            "payroll",
            "unemployment",
            "gdp",
            "ism",
            "jolts",
            "retail sales",
            "powell",
            "bowman",
            "cook",
            "barkin"
        ]

        for word in important_words:
            if word in text:
                events.append(word.upper())

        return list(dict.fromkeys(events))

    except Exception as e:
        print("CALENDAR ERROR:", e)
        return []


# ============================================================
# НОВОСТНОЙ РИСК
# ============================================================

def news_risk():
    """
    Для текущей версии:
    если в календаре обнаружены ключевые USD/FED события,
    повышаем осторожность.

    В будущем можно подключить API календаря с точными
    timestamp событий.
    """

    events = get_economic_calendar()

    high_words = [
        "FOMC",
        "INTEREST RATE",
        "CPI",
        "CORE PCE",
        "PCE",
        "NONFARM",
        "PAYROLL",
        "UNEMPLOYMENT",
        "POWELL"
    ]

    medium_words = [
        "GDP",
        "ISM",
        "JOLTS",
        "RETAIL SALES",
        "FED",
        "BOWMAN",
        "COOK",
        "BARKIN"
    ]

    high = any(x in events for x in high_words)
    medium = any(x in events for x in medium_words)

    if high:
        return "HIGH", events

    if medium:
        return "MEDIUM", events

    return "LOW", events


# ============================================================
# SCORE
# ============================================================

def calculate_score(
    h4_trend,
    h1_trend,
    m15_trend,
    structure,
    sweep,
    bos,
    fvg,
    rsi,
    dxy_change,
    us10y_change,
    news
):

    buy = 0
    sell = 0

    # H4
    if h4_trend == "BULLISH":
        buy += 2
    elif h4_trend == "BEARISH":
        sell += 2

    # H1
    if h1_trend == "BULLISH":
        buy += 2
    elif h1_trend == "BEARISH":
        sell += 2

    # M15
    if m15_trend == "BULLISH":
        buy += 1
    elif m15_trend == "BEARISH":
        sell += 1

    # Structure
    if structure == "HH/HL":
        buy += 1

    if structure == "LH/LL":
        sell += 1

    # Liquidity
    if sweep == "SELL_SIDE":
        buy += 2

    if sweep == "BUY_SIDE":
        sell += 2

    # BOS
    if bos == "BULLISH_BOS":
        buy += 2

    if bos == "BEARISH_BOS":
        sell += 2

    # FVG
    if fvg:
        if fvg["type"] == "BULLISH":
            buy += 1
        elif fvg["type"] == "BEARISH":
            sell += 1

    # RSI
    if not np.isnan(rsi):

        # Перепроданность запрещает новый SELL
        if rsi < 25:
            sell -= 2

        # Перекупленность запрещает новый BUY
        if rsi > 75:
            buy -= 2

        # Умеренная зона
        if 45 <= rsi <= 60:
            if h1_trend == "BULLISH":
                buy += 1
            elif h1_trend == "BEARISH":
                sell += 1

    # DXY
    if not np.isnan(dxy_change):

        if dxy_change > 0.05:
            sell += 1

        elif dxy_change < -0.05:
            buy += 1

    # US10Y
    if not np.isnan(us10y_change):

        if us10y_change > 0.5:
            sell += 1

        elif us10y_change < -0.5:
            buy += 1

    # Новости
    if news == "HIGH":
        # Сильные новости НЕ дают дополнительный score.
        # Они блокируют вход.
        pass

    return max(0, buy), max(0, sell)


# ============================================================
# ENTRY / SL / TP
# ============================================================

def build_trade_plan(df, direction):
    if df.empty or len(df) < 30:
        return None

    last = df.iloc[-1]

    price = float(last["Close"])
    atr = float(last["ATR"])

    if np.isnan(atr) or atr <= 0:
        return None

    recent = df.iloc[-12:]

    if direction == "BUY":

        structure_low = float(recent["Low"].min())

        sl = min(
            structure_low - atr * 0.20,
            price - atr * 1.2
        )

        risk = price - sl

        if risk <= 0:
            return None

        entry_low = price
        entry_high = price + atr * 0.25

        tp1 = entry_high + risk * 2.0
        tp2 = entry_high + risk * 3.0

    else:

        structure_high = float(recent["High"].max())

        sl = max(
            structure_high + atr * 0.20,
            price + atr * 1.2
        )

        risk = sl - price

        if risk <= 0:
            return None

        entry_low = price - atr * 0.25
        entry_high = price

        tp1 = entry_low - risk * 2.0
        tp2 = entry_low - risk * 3.0

    return {
        "entry_low": entry_low,
        "entry_high": entry_high,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "risk": risk,
        "rr1": 2.0,
        "rr2": 3.0
    }


# ============================================================
# РАЗМЕР ПОЗИЦИИ
# ============================================================

def calculate_lot(balance, risk_percent, risk_price):
    """
    Упрощённая оценка для XAUUSD.

    Для точного расчёта под конкретного брокера необходимо
    получить contract size/tick value из MT5/API.
    """

    if risk_price <= 0:
        return 0.01

    risk_money = balance * risk_percent / 100

    # Приближённая модель:
    # 1 lot XAUUSD ≈ $100 на движение $1
    estimated_lot = risk_money / (risk_price * 100)

    estimated_lot = max(0.01, estimated_lot)

    return round(estimated_lot, 2)


# ============================================================
# АНТИ-CHASE
# ============================================================

def stretched_market(df):
    if len(df) < 20:
        return False

    last = df.iloc[-1]

    atr = last["ATR"]

    if np.isnan(atr):
        return False

    body = abs(
        float(last["Close"]) -
        float(last["Open"])
    )

    # Огромная импульсная свеча
    if body > atr * 1.8:
        return True

    # RSI extreme
    rsi = last["RSI"]

    if not np.isnan(rsi):
        if rsi < 20 or rsi > 80:
            return True

    return False


# ============================================================
# ПОЛНЫЙ АНАЛИЗ
# ============================================================

def analyze_gold():

    # -----------------------------
    # Загрузка
    # -----------------------------

    m15 = download_data(
        GOLD_TICKER,
        "15m",
        M15_PERIOD
    )

    h1 = download_data(
        GOLD_TICKER,
        "1h",
        H1_PERIOD
    )

    h4 = download_data(
        GOLD_TICKER,
        "4h",
        H4_PERIOD
    )

    if m15.empty or h1.empty or h4.empty:
        return {
            "error": "Не удалось получить данные золота."
        }

    m15 = closed_candles(add_indicators(m15))
    h1 = closed_candles(add_indicators(h1))
    h4 = closed_candles(add_indicators(h4))

    if len(m15) < 250:
        return {
            "error": "Недостаточно M15 истории."
        }

    # -----------------------------
    # Цена
    # -----------------------------

    price = float(m15["Close"].iloc[-1])

    # -----------------------------
    # Тренды
    # -----------------------------

    h4_trend = get_trend(h4)
    h1_trend = get_trend(h1)
    m15_trend = get_trend(m15)

    structure = market_structure(m15)

    # -----------------------------
    # Уровни
    # -----------------------------

    asia_high, asia_low = asia_levels(m15)
    pdh, pdl = previous_day_levels(m15)

    # -----------------------------
    # Liquidity
    # -----------------------------

    sweep = liquidity_sweep(
        m15,
        asia_high,
        asia_low,
        pdh,
        pdl
    )

    # -----------------------------
    # BOS
    # -----------------------------

    bos = detect_bos(m15)

    # -----------------------------
    # FVG
    # -----------------------------

    fvg = detect_fvg(m15)

    # -----------------------------
    # RSI / ATR
    # -----------------------------

    rsi = float(m15["RSI"].iloc[-1])
    atr = float(m15["ATR"].iloc[-1])

    # -----------------------------
    # DXY / US10Y
    # -----------------------------

    external = get_external_market()

    # -----------------------------
    # NEWS
    # -----------------------------

    news, events = news_risk()

    # -----------------------------
    # SCORE
    # -----------------------------

    buy_score, sell_score = calculate_score(
        h4_trend,
        h1_trend,
        m15_trend,
        structure,
        sweep,
        bos,
        fvg,
        rsi,
        external["dxy_change"],
        external["us10y_change"],
        news
    )

    # -----------------------------
    # Decision
    # -----------------------------

    stretched = stretched_market(m15)

    direction = "WAIT"
    reason = []

    # Сильная новость = NO TRADE
    if news == "HIGH":
        direction = "NO TRADE"
        reason.append("🔴 Высокий новостной риск")

    elif stretched:
        direction = "WAIT"
        reason.append("⚠️ Рынок слишком растянут")

    else:

        if (
            buy_score >= MIN_SCORE
            and h4_trend == "BULLISH"
            and h1_trend == "BULLISH"
            and bos == "BULLISH_BOS"
        ):
            direction = "BUY"

        elif (
            sell_score >= MIN_SCORE
            and h4_trend == "BEARISH"
            and h1_trend == "BEARISH"
            and bos == "BEARISH_BOS"
        ):
            direction = "SELL"

        else:
            direction = "WAIT"
            reason.append(
                "⏳ Недостаточно подтверждений"
            )

    # -----------------------------
    # План
    # -----------------------------

    plan = None

    if direction in ["BUY", "SELL"]:
        plan = build_trade_plan(
            m15,
            direction
        )

        if plan is None:
            direction = "WAIT"
            reason.append(
                "⚠️ Не удалось безопасно рассчитать SL/TP"
            )

    # -----------------------------
    # Session
    # -----------------------------

    hour = now_local().hour

    if 12 <= hour < 17:
        session = "LONDON 🇬🇧"

    elif 17 <= hour < 23:
        session = "NEW YORK 🇺🇸"

    elif hour < 12:
        session = "ASIA 🌏"

    else:
        session = "ВНЕ СЕССИИ 🌙"

    return {
        "price": price,
        "session": session,

        "h4": h4_trend,
        "h1": h1_trend,
        "m15": m15_trend,

        "structure": structure,
        "sweep": sweep,
        "bos": bos,

        "fvg": fvg,

        "rsi": rsi,
        "atr": atr,

        "asia_high": asia_high,
        "asia_low": asia_low,

        "pdh": pdh,
        "pdl": pdl,

        "dxy": external["dxy"],
        "dxy_change": external["dxy_change"],

        "us10y": external["us10y"],
        "us10y_change": external["us10y_change"],

        "news": news,
        "events": events,

        "buy_score": buy_score,
        "sell_score": sell_score,

        "direction": direction,
        "reason": reason,

        "plan": plan,

        "timestamp": now_local().strftime(
            "%d.%m.%Y %H:%M"
        )
    }


# ============================================================
# ФОРМАТИРОВАНИЕ
# ============================================================

def trend_ru(x):
    return {
        "BULLISH": "🟢 БЫЧИЙ",
        "BEARISH": "🔴 МЕДВЕЖИЙ",
        "NEUTRAL": "🟡 НЕЙТРАЛЬНЫЙ",
        "UNKNOWN": "⚪ НЕТ ДАННЫХ"
    }.get(x, x)


def format_full(result):

    if "error" in result:
        return (
            "❌ <b>GOLD SMART V3.1</b>\n\n"
            f"{result['error']}"
        )

    price = result["price"]

    fvg_text = "❌ Нет"

    if result["fvg"]:
        fvg_text = (
            f"✅ {result['fvg']['type']} "
            f"(CE {fmt_price(result['fvg']['ce'])})"
        )

    news_icon = {
        "LOW": "🟢",
        "MEDIUM": "🟠",
        "HIGH": "🔴"
    }.get(result["news"], "⚪")

    direction = result["direction"]

    if direction == "BUY":
        decision = "🟢 <b>BUY</b>"

    elif direction == "SELL":
        decision = "🔴 <b>SELL</b>"

    elif direction == "NO TRADE":
        decision = "⛔ <b>NO TRADE</b>"

    else:
        decision = "🟡 <b>WAIT</b> ⏳"

    msg = f"""
🧠 <b>GOLD SMART V3.1</b>

💰 <b>XAU/USD:</b> {fmt_price(price)}
⏰ <b>Сессия:</b> {result["session"]}
🕐 {result["timestamp"]}

━━━━━━━━━━━━━━━━
📊 <b>ТЕХНИЧЕСКИЙ АНАЛИЗ</b>
━━━━━━━━━━━━━━━━

🏦 H4: {trend_ru(result["h4"])}
📈 H1: {trend_ru(result["h1"])}
⏱ M15: {trend_ru(result["m15"])}

🧱 Структура: <b>{result["structure"]}</b>
💧 Liquidity Sweep: <b>{result["sweep"]}</b>
🧱 BOS: <b>{result["bos"]}</b>
📦 FVG: {fvg_text}

📈 RSI: <b>{result["rsi"]:.1f}</b>
📏 ATR: <b>{result["atr"]:.2f}</b>

━━━━━━━━━━━━━━━━
💧 <b>КЛЮЧЕВЫЕ УРОВНИ</b>
━━━━━━━━━━━━━━━━

🌏 Asia High: {fmt_price(result["asia_high"])}
🌏 Asia Low: {fmt_price(result["asia_low"])}

📅 PDH: {fmt_price(result["pdh"])}
📅 PDL: {fmt_price(result["pdl"])}

━━━━━━━━━━━━━━━━
🌎 <b>МАКРОФОН</b>
━━━━━━━━━━━━━━━━

💵 DXY: {fmt_price(result["dxy"])}
📊 DXY изменение: {fmt_price(result["dxy_change"])}%

🇺🇸 US10Y: {fmt_price(result["us10y"])}
📊 US10Y изменение: {fmt_price(result["us10y_change"])}%

━━━━━━━━━━━━━━━━
📰 <b>ЭКОНОМИЧЕСКИЙ КАЛЕНДАРЬ</b>
━━━━━━━━━━━━━━━━

{news_icon} Риск: <b>{result["news"]}</b>
"""

    if result["events"]:
        msg += "\n📌 События: "
        msg += ", ".join(result["events"][:8])
        msg += "\n"
    else:
        msg += "\n⚪ Ключевые события не обнаружены.\n"

    msg += f"""
━━━━━━━━━━━━━━━━
🧠 <b>SCORE</b>
━━━━━━━━━━━━━━━━

🟢 BUY: <b>{result["buy_score"]}</b>
🔴 SELL: <b>{result["sell_score"]}</b>

━━━━━━━━━━━━━━━━
🚦 <b>РЕШЕНИЕ</b>
━━━━━━━━━━━━━━━━

{decision}
"""

    if result["reason"]:
        for r in result["reason"]:
            msg += f"\n{r}"

    if result["plan"]:

        p = result["plan"]

        msg += f"""

━━━━━━━━━━━━━━━━
🎯 <b>ТОРГОВЫЙ ПЛАН</b>
━━━━━━━━━━━━━━━━

📍 Entry: <b>{fmt_price(p["entry_low"])} — {fmt_price(p["entry_high"])}</b>

🛑 SL: <b>{fmt_price(p["sl"])}</b>

🎯 TP1: <b>{fmt_price(p["tp1"])}</b>
🎯 TP2: <b>{fmt_price(p["tp2"])}</b>

⚖️ RR1: <b>1:{p["rr1"]:.1f}</b>
⚖️ RR2: <b>1:{p["rr2"]:.1f}</b>

💰 При балансе ${DEFAULT_BALANCE:.0f}
📊 риск {RISK_PERCENT:.1f}%
📦 ориентировочный лот: <b>{
    calculate_lot(
        DEFAULT_BALANCE,
        RISK_PERCENT,
        p["risk"]
    )
:.2f}</b>

⚠️ Точный lot зависит от contract size/tick value брокера.
"""

    msg += """

━━━━━━━━━━━━━━━━
🛡️ <b>ЗАЩИТА</b>
━━━━━━━━━━━━━━━━

🚫 Мартингейл: НЕТ
🚫 Усреднение: НЕТ
🚫 Автоторговля: НЕТ
✅ Только закрытые свечи
✅ Фильтр новостей
✅ Анти-CHASE
"""

    msg += """
⚠️ <i>Сигнал является аналитическим сценарием, а не гарантией результата.</i>
"""

    return msg


# ============================================================
# QUICK
# ============================================================

def format_quick(result):

    if "error" in result:
        return f"❌ {result['error']}"

    direction = result["direction"]

    if direction == "BUY":
        action = "🟢 BUY"

    elif direction == "SELL":
        action = "🔴 SELL"

    elif direction == "NO TRADE":
        action = "⛔ NO TRADE"

    else:
        action = "🟡 WAIT"

    return f"""
🧠 <b>GOLD SMART V3.1</b>

💰 {fmt_price(result["price"])}

🏦 H4: {trend_ru(result["h4"])}
📈 H1: {trend_ru(result["h1"])}
⏱ M15: {trend_ru(result["m15"])}

📈 RSI: {result["rsi"]:.1f}

🟢 BUY: {result["buy_score"]}
🔴 SELL: {result["sell_score"]}

📰 News: {result["news"]}

🚦 <b>{action}</b>
"""


# ============================================================
# NEWS COMMAND
# ============================================================

@bot.message_handler(commands=["news"])
def news_command(message):

    risk, events = news_risk()

    icon = {
        "LOW": "🟢",
        "MEDIUM": "🟠",
        "HIGH": "🔴"
    }.get(risk, "⚪")

    text = f"""
📰 <b>ЭКОНОМИЧЕСКИЙ КАЛЕНДАРЬ</b>

{icon} Текущий риск: <b>{risk}</b>

"""

    if events:
        text += "📌 Найденные события:\n"

        for event in events[:15]:
            text += f"• 🇺🇸 {event}\n"

    else:
        text += (
            "⚪ В текущем источнике не обнаружены "
            "ключевые USD/Fed события."
        )

    text += """

💡 Перед сильными новостями бот может перевести рынок
в режим ⛔ NO TRADE.
"""

    bot.reply_to(message, text)


# ============================================================
# GOLD
# ============================================================

@bot.message_handler(commands=["gold"])
def gold_command(message):

    bot.send_chat_action(
        message.chat.id,
        "typing"
    )

    try:
        result = analyze_gold()

        bot.send_message(
            message.chat.id,
            format_full(result)
        )

    except Exception as e:

        print("GOLD ERROR:", e)

        bot.send_message(
            message.chat.id,
            "❌ Ошибка анализа.\n"
            "Попробуй /gold ещё раз через несколько секунд."
        )


# ============================================================
# QUICK
# ============================================================

@bot.message_handler(commands=["quick"])
def quick_command(message):

    try:

        result = analyze_gold()

        bot.send_message(
            message.chat.id,
            format_quick(result)
        )

    except Exception as e:

        print("QUICK ERROR:", e)

        bot.send_message(
            message.chat.id,
            "❌ Не удалось получить анализ."
        )


# ============================================================
# LEVELS
# ============================================================

@bot.message_handler(commands=["levels"])
def levels_command(message):

    try:

        result = analyze_gold()

        if "error" in result:
            bot.send_message(
                message.chat.id,
                f"❌ {result['error']}"
            )
            return

        text = f"""
📍 <b>GOLD LEVELS</b>

💰 Цена: {fmt_price(result["price"])}

🌏 Asia High:
<b>{fmt_price(result["asia_high"])}</b>

🌏 Asia Low:
<b>{fmt_price(result["asia_low"])}</b>

📅 PDH:
<b>{fmt_price(result["pdh"])}</b>

📅 PDL:
<b>{fmt_price(result["pdl"])}</b>

📏 ATR:
<b>{fmt_price(result["atr"])}</b>
"""

        bot.send_message(
            message.chat.id,
            text
        )

    except Exception as e:

        print("LEVEL ERROR:", e)

        bot.send_message(
            message.chat.id,
            "❌ Ошибка получения уровней."
        )


# ============================================================
# STATS
# ============================================================

@bot.message_handler(commands=["stats"])
def stats_command(message):

    bot.send_message(
        message.chat.id,
        """
📊 <b>GOLD SMART V3.1 — СТАТИСТИКА</b>

⚠️ Полный backtest требует исторических
M15/H1/H4 данных и симуляции Entry → SL/TP.

В этой версии бот не показывает
искусственный WinRate.

🧠 Следующий этап:
• 100+ исторических сделок
• одинаковые правила входа
• без look-ahead
• SL/TP по фактическому движению
• WinRate
• Profit Factor
• Expectancy
• Max Drawdown
• BUY/SELL отдельно
• London/New York отдельно
"""
    )


# ============================================================
# STATUS
# ============================================================

@bot.message_handler(commands=["status"])
def status_command(message):

    bot.send_message(
        message.chat.id,
        """
🧠 <b>GOLD SMART V3.1</b>

🟢 Бот работает

📊 XAU/USD анализ: ON
🏦 H4: ON
📈 H1: ON
⏱ M15: ON

💧 Liquidity Sweep: ON
🧱 BOS/CHoCH: ON
📦 FVG: ON
📈 RSI: ON
📏 ATR: ON

💵 DXY: ON
🇺🇸 US10Y: ON
📰 Economic Calendar: ON

🛡️ Auto Trading: OFF
🚫 Martingale: OFF
🚫 Averaging: OFF
"""
    )


# ============================================================
# START
# ============================================================

@bot.message_handler(commands=["start"])
def start_command(message):

    bot.send_message(
        message.chat.id,
        """
🧠 <b>GOLD SMART V3.1</b>

🇷🇺 Умный анализатор золота XAU/USD

📊 /gold — полный анализ
⚡ /quick — краткий сигнал
📰 /news — экономический календарь
📍 /levels — ключевые уровни
📊 /stats — статистика
🟢 /status — статус

💡 Бот не торгует автоматически.
Он анализирует рынок и выдаёт:
🟢 BUY
🔴 SELL
🟡 WAIT
⛔ NO TRADE
"""
    )


# ============================================================
# ОБЫЧНЫЙ ТЕКСТ
# ============================================================

@bot.message_handler(
    func=lambda message: message.text is not None
    and message.text.lower().strip() in [
        "gold",
        "золото",
        "анализ золота"
    ]
)
def gold_text(message):

    try:

        result = analyze_gold()

        bot.send_message(
            message.chat.id,
            format_full(result)
        )

    except Exception as e:

        print("TEXT GOLD ERROR:", e)

        bot.send_message(
            message.chat.id,
            "❌ Ошибка анализа." 
        )


# ============================================================
# ЗАПУСК
# ============================================================

print("===================================")
print("🧠 GOLD SMART V3.1")
print("🇷🇺 Russian XAU/USD Analyzer")
print("🟢 Signal Only")
print("🚫 Auto Trading OFF")
print("===================================")

while True:

    try:

        print(
            "BOT RUNNING:",
            now_local().strftime(
                "%d.%m.%Y %H:%M:%S"
            )
        )

        bot.infinity_polling(
            timeout=60,
            long_polling_timeout=60
        )

    except Exception as e:

        print("BOT ERROR:", e)

        time.sleep(10)
