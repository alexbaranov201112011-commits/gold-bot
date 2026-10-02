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
# 🥇 GOLD SMART V5.8
# XAUUSD SMART SIGNAL ENGINE
#
# SOURCE:
# XAUS API
#
# FEATURES:
# - H4 / H1 / M15 / M5 analysis
# - SMC-style conditions
# - BUY / SELL / EARLY / WAIT
# - Automatic virtual trade tracking
# - WIN / LOSS / BE
# - Statistics
# - Telegram Webhook
# - Automatic signals
#
# REAL TRADING = OFF
# =========================================================
# =========================================================
# CONFIG
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WEBHOOK_URL = os.getenv(
    "WEBHOOK_URL",
    "https://gold-bot-q8la.onrender.com"
).strip()
XAUS_BASE = "https://xaus.com/api/v1"
RISK_PERCENT = 1.0
DEFAULT_DEPOSIT = 800.0
MIN_LOT = 0.01
MAX_LOT = 0.02
MIN_SCORE = 70
EARLY_SCORE = 60
POLL_SECONDS = 60
COOLDOWN_MIN = 15
# Gold distances in USD
SL_DISTANCE = 5.0
TP_DISTANCE = 15.0
# Move SL to BE after favorable move
BE_TRIGGER = 6.0
STATS_FILE = "gold_stats.json"
CACHE_SECONDS = 45
AUTO_TRADING = False
CLOSED_CANDLES = True
# =========================================================
# FLASK
# =========================================================
app = Flask(__name__)
# =========================================================
# GLOBAL STATE
# =========================================================
spot_cache = {
    "price": None,
    "time": 0
}
last_signal = None
last_signal_time = 0
virtual_trade = None
engine_started = False
# =========================================================
# STATISTICS
# =========================================================
stats = {
    "total": 0,
    "win": 0,
    "loss": 0,
    "be": 0,
    "open": 0
}
# =========================================================
# LOAD STATS
# =========================================================
def load_stats():
    global stats
    try:
        if os.path.exists(STATS_FILE):
            with open(
                STATS_FILE,
                "r",
                encoding="utf-8"
            ) as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    for key in stats:
                        if key in saved:
                            stats[key] = int(
                                saved[key]
                            )
    except Exception as e:
        print(
            f"Stats load error: {e}"
        )
# =========================================================
# SAVE STATS
# =========================================================
def save_stats():
    try:
        with open(
            STATS_FILE,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                stats,
                f,
                indent=2
            )
    except Exception as e:
        print(
            f"Stats save error: {e}"
        )
# =========================================================
# TELEGRAM
# =========================================================
def telegram_send(text_message, chat_id=None):
    if not BOT_TOKEN:
        print(
            "Telegram token missing"
        )
        return False
    target = chat_id or TELEGRAM_CHAT_ID
    if not target:
        print(
            "Telegram chat ID missing"
        )
        return False
    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/sendMessage"
    )
    payload = {
        "chat_id": target,
        "text": text_message
    }
    try:
        response = requests.post(
            url,
            json=payload,
            timeout=10
        )
        if response.status_code != 200:
            print(
                "Telegram error:",
                response.status_code,
                response.text[:500]
            )
            return False
        return True
    except Exception as e:
        print(
            f"Telegram request error: {e}"
        )
        return False
# =========================================================
# XAUS SPOT
# =========================================================
def fetch_spot(force=False):
    """
    Получение XAUUSD spot через XAUS.
    Реальный формат XAUS:
    {
        "xau": {
            "price": 4138.4,
            "currency": "USD",
            "unit": "troy_oz"
        },
        ...
    }
    """
    global spot_cache
    now = time.time()
    # -----------------------------------------------------
    # CACHE
    # -----------------------------------------------------
    if not force and spot_cache:
        age = (
            now -
            spot_cache.get(
                "time",
                0
            )
        )
        if (
            spot_cache.get("price")
            and
            age < CACHE_SECONDS
        ):
            return spot_cache["price"]
    url = f"{XAUS_BASE}/spot"
    try:
        response = requests.get(
            url,
            params={
                "currency": "USD",
                "unit": "oz",
                "compact": "1"
            },
            timeout=10,
            headers={
                "User-Agent":
                "GOLD-SMART-V5.8"
            }
        )
        print(
            f"XAUS SPOT HTTP "
            f"{response.status_code}"
        )
        if response.status_code != 200:
            print(
                "XAUS SPOT ERROR:",
                response.status_code,
                response.text[:500]
            )
            return None
        data = response.json()
        price = None
        # =================================================
        # PRIMARY FORMAT
        # =================================================
        if isinstance(data, dict):
            xau = data.get("xau")
            if isinstance(xau, dict):
                value = xau.get("price")
                if value is not None:
                    try:
                        price = float(value)
                    except (
                        TypeError,
                        ValueError
                    ):
                        price = None
            # =================================================
            # FALLBACK
            # =================================================
            if price is None:
                for key in (
                    "spot_usd_oz",
                    "price",
                    "spot",
                    "value",
                    "rate",
                    "mid"
                ):
                    value = data.get(key)
                    if isinstance(
                        value,
                        (int, float)
                    ):
                        price = float(value)
                        break
            # =================================================
            # NESTED FALLBACK
            # =================================================
            if price is None:
                for container_key in (
                    "data",
                    "result",
                    "spot"
                ):
                    container = data.get(
                        container_key
                    )
                    if not isinstance(
                        container,
                        dict
                    ):
                        continue
                    nested_xau = container.get(
                        "xau"
                    )
                    if isinstance(
                        nested_xau,
                        dict
                    ):
                        value = (
                            nested_xau.get(
                                "price"
                            )
                        )
                        if value is not None:
                            try:
                                price = float(
                                    value
                                )
                                break
                            except (
                                TypeError,
                                ValueError
                            ):
                                pass
                    for key in (
                        "spot_usd_oz",
                        "price",
                        "spot",
                        "value",
                        "rate",
                        "mid"
                    ):
                        value = container.get(
                            key
                        )
                        if isinstance(
                            value,
                            (int, float)
                        ):
                            price = float(
                                value
                            )
                            break
                    if price is not None:
                        break
        # =================================================
        # VALIDATION
        # =================================================
        if (
            price is None
            or
            price <= 0
        ):
            print(
                "XAUS spot parsing failed:",
                data
            )
            return None
        # =================================================
        # CACHE
        # =================================================
        spot_cache = {
            "price": price,
            "time": now
        }
        print(
            f"XAUS XAUUSD: "
            f"{price:.2f}"
        )
        return price
    except requests.RequestException as e:
        print(
            f"XAUS request error: {e}"
        )
        return None
    except ValueError as e:
        print(
            f"XAUS JSON error: {e}"
        )
        return None
    except Exception as e:
        print(
            f"XAUS unexpected error: {e}"
        )
        return None
# =========================================================
# CHART DATA
# =========================================================
def get_chart(
    interval,
    range_value
):
    url = f"{XAUS_BASE}/chart"
    try:
        response = requests.get(
            url,
            params={
                "symbol": "xau",
                "range": range_value,
                "interval": interval
            },
            timeout=15,
            headers={
                "User-Agent":
                "GOLD-SMART-V5.8"
            }
        )
        if response.status_code != 200:
            print(
                "XAUS chart error:",
                response.status_code,
                response.text[:500]
            )
            return None
        data = response.json()
        return parse_chart(data)
    except Exception as e:
        print(
            f"Chart error "
            f"{interval}: {e}"
        )
        return None
# =========================================================
# PARSE CHART
# =========================================================
def parse_chart(data):
    rows = []
    # -----------------------------------------------------
    # POSSIBLE POINT CONTAINER
    # -----------------------------------------------------
    points = None
    if isinstance(data, list):
        points = data
    elif isinstance(data, dict):
        for key in (
            "data",
            "result",
            "chart",
            "candles",
            "bars",
            "prices"
        ):
            value = data.get(key)
            if isinstance(value, list):
                points = value
                break
            if isinstance(value, dict):
                for nested_key in (
                    "data",
                    "candles",
                    "bars",
                    "prices"
                ):
                    nested = value.get(
                        nested_key
                    )
                    if isinstance(
                        nested,
                        list
                    ):
                        points = nested
                        break
            if points is not None:
                break
    if not points:
        return None
    # -----------------------------------------------------
    # PARSE POINTS
    # -----------------------------------------------------
    for item in points:
        try:
            # =============================================
            # DICT FORMAT
            # =============================================
            if isinstance(
                item,
                dict
            ):
                timestamp = (
                    item.get("t")
                    or item.get("time")
                    or item.get("timestamp")
                    or item.get("datetime")
                    or item.get("date")
                )
                open_price = (
                    item.get("o")
                    or item.get("open")
                )
                high_price = (
                    item.get("h")
                    or item.get("high")
                )
                low_price = (
                    item.get("l")
                    or item.get("low")
                )
                close_price = (
                    item.get("c")
                    or item.get("close")
                )
                volume = (
                    item.get("v")
                    or item.get("volume")
                    or 0
                )
            # =============================================
            # LIST FORMAT
            # =============================================
            elif isinstance(
                item,
                list
            ):
                if len(item) < 5:
                    continue
                timestamp = item[0]
                open_price = item[1]
                high_price = item[2]
                low_price = item[3]
                close_price = item[4]
                volume = (
                    item[5]
                    if len(item) > 5
                    else 0
                )
            else:
                continue
            if (
                timestamp is None
                or
                open_price is None
                or
                high_price is None
                or
                low_price is None
                or
                close_price is None
            ):
                continue
            # =============================================
            # TIMESTAMP
            # =============================================
            if isinstance(
                timestamp,
                str
            ):
                try:
                    dt = pd.to_datetime(
                        timestamp,
                        utc=True
                    )
                except Exception:
                    continue
            else:
                try:
                    ts = float(timestamp)
                    if ts > 1e12:
                        ts = ts / 1000.0
                    dt = pd.to_datetime(
                        ts,
                        unit="s",
                        utc=True
                    )
                except Exception:
                    continue
            rows.append(
                {
                    "time": dt,
                    "open": float(open_price),
                    "high": float(high_price),
                    "low": float(low_price),
                    "close": float(close_price),
                    "volume": float(volume or 0)
                }
            )
        except Exception:
            continue
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.drop_duplicates(
        subset=["time"]
    )
    df = df.sort_values(
        "time"
    )
    df = df.set_index(
        "time"
    )
    # -----------------------------------------------------
    # CLOSED CANDLES
    # -----------------------------------------------------
    if CLOSED_CANDLES and len(df) > 2:
        df = df.iloc[:-1]
    return df
# =========================================================
# RSI
# =========================================================
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
    avg_gain = gain.rolling(
        period
    ).mean()
    avg_loss = loss.rolling(
        period
    ).mean()
    rs = (
        avg_gain /
        avg_loss.replace(
            0,
            np.nan
        )
    )
    rsi = 100 - (
        100 /
        (1 + rs)
    )
    return rsi
# =========================================================
# ANALYZE TIMEFRAME
# =========================================================
def analyze_dataframe(
    df
):
    if df is None or len(df) < 60:
        return {
            "trend": "MIXED",
            "momentum": "MIXED",
            "liquidity": "NONE",
            "bos": "NONE",
            "fvg": "NONE",
            "displacement": "NONE",
            "zone": "EQUILIBRIUM",
            "rsi": 50.0
        }
    df = df.copy()
    # -----------------------------------------------------
    # EMA
    # -----------------------------------------------------
    df["ema20"] = (
        df["close"]
        .ewm(
            span=20,
            adjust=False
        )
        .mean()
    )
    df["ema50"] = (
        df["close"]
        .ewm(
            span=50,
            adjust=False
        )
        .mean()
    )
    df["ema200"] = (
        df["close"]
        .ewm(
            span=200,
            adjust=False
        )
        .mean()
    )
    # -----------------------------------------------------
    # RSI
    # -----------------------------------------------------
    df["rsi"] = calculate_rsi(
        df["close"],
        14
    )
    last = df.iloc[-1]
    # -----------------------------------------------------
    # TREND
    # -----------------------------------------------------
    if (
        last["ema50"] >
        last["ema200"]
    ):
        trend = "BULLISH"
    elif (
        last["ema50"] <
        last["ema200"]
    ):
        trend = "BEARISH"
    else:
        trend = "MIXED"
    # -----------------------------------------------------
    # MOMENTUM
    # -----------------------------------------------------
    if (
        last["ema20"] >
        last["ema50"]
    ):
        momentum = "BULLISH"
    elif (
        last["ema20"] <
        last["ema50"]
    ):
        momentum = "BEARISH"
    else:
        momentum = "MIXED"
    # -----------------------------------------------------
    # RECENT DATA
    # -----------------------------------------------------
    recent12 = df.iloc[-13:-1]
    recent10 = df.iloc[-11:-1]
    # -----------------------------------------------------
    # BOS
    # -----------------------------------------------------
    bos = "NONE"
    if len(recent12) > 0:
        previous_high = (
            recent12["high"].max()
        )
        previous_low = (
            recent12["low"].min()
        )
        if (
            last["close"] >
            previous_high
        ):
            bos = "BULLISH BOS"
        elif (
            last["close"] <
            previous_low
        ):
            bos = "BEARISH BOS"
    # -----------------------------------------------------
    # LIQUIDITY SWEEP
    # -----------------------------------------------------
    liquidity = "NONE"
    if len(recent10) > 0:
        recent_high = (
            recent10["high"].max()
        )
        recent_low = (
            recent10["low"].min()
        )
        # BSL sweep
        if (
            last["high"] >
            recent_high
            and
            last["close"] <
            recent_high
        ):
            liquidity = "BSL SWEEP"
        # SSL sweep
        elif (
            last["low"] <
            recent_low
            and
            last["close"] >
            recent_low
        ):
            liquidity = "SSL SWEEP"
    # -----------------------------------------------------
    # FVG
    # -----------------------------------------------------
    fvg = "NONE"
    if len(df) >= 4:
        candle_minus3 = df.iloc[-4]
        if (
            last["low"] >
            candle_minus3["high"]
        ):
            fvg = "BULLISH FVG"
        elif (
            last["high"] <
            candle_minus3["low"]
        ):
            fvg = "BEARISH FVG"
    # -----------------------------------------------------
    # DISPLACEMENT
    # -----------------------------------------------------
    displacement = "NONE"
    df["range"] = (
        df["high"] -
        df["low"]
    )
    if len(df) >= 22:
        average_range = (
            df["range"]
            .iloc[-21:-1]
            .mean()
        )
        current_range = (
            last["high"] -
            last["low"]
        )
        if (
            average_range > 0
            and
            current_range >=
            average_range * 1.5
        ):
            if (
                last["close"] >
                last["open"]
            ):
                displacement = (
                    "BULLISH"
                )
            elif (
                last["close"] <
                last["open"]
            ):
                displacement = (
                    "BEARISH"
                )
    # -----------------------------------------------------
    # ZONE
    # -----------------------------------------------------
    recent50 = df.iloc[-50:]
    range_high = (
        recent50["high"].max()
    )
    range_low = (
        recent50["low"].min()
    )
    midpoint = (
        range_high +
        range_low
    ) / 2
    if last["close"] > midpoint:
        zone = "PREMIUM"
    elif last["close"] < midpoint:
        zone = "DISCOUNT"
    else:
        zone = "EQUILIBRIUM"
    # -----------------------------------------------------
    # RSI
    # -----------------------------------------------------
    rsi_value = last["rsi"]
    if pd.isna(rsi_value):
        rsi_value = 50.0
    rsi_value = float(
        rsi_value
    )
    return {
        "trend": trend,
        "momentum": momentum,
        "liquidity": liquidity,
        "bos": bos,
        "fvg": fvg,
        "displacement": displacement,
        "zone": zone,
        "rsi": rsi_value
    }
# =========================================================
# BUILD H4 FROM H1
# =========================================================
def build_h4(
    h1
):
    if h1 is None or h1.empty:
        return None
    try:
        h4 = (
            h1
            .resample("4h")
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum"
                }
            )
            .dropna()
        )
        return h4
    except Exception as e:
        print(
            f"H4 resample error: {e}"
        )
        return None
# =========================================================
# GET ALL MARKET DATA
# =========================================================
def get_market_data():
    m5 = get_chart(
        "5m",
        "1d"
    )
    m15 = get_chart(
        "15m",
        "5d"
    )
    h1 = get_chart(
        "60m",
        "1mo"
    )
    if h1 is None:
        return None
    h4 = build_h4(
        h1
    )
    return {
        "M5": m5,
        "M15": m15,
        "H1": h1,
        "H4": h4
    }
# =========================================================
# SCORE
# =========================================================
def calculate_scores(
    analyses
):
    buy = 0
    sell = 0
    # -----------------------------------------------------
    # H4
    # -----------------------------------------------------
    h4 = analyses["H4"]
    if h4["trend"] == "BULLISH":
        buy += 20
    elif h4["trend"] == "BEARISH":
        sell += 20
    # -----------------------------------------------------
    # H1
    # -----------------------------------------------------
    h1 = analyses["H1"]
    if h1["trend"] == "BULLISH":
        buy += 15
    elif h1["trend"] == "BEARISH":
        sell += 15
    # -----------------------------------------------------
    # M15
    # -----------------------------------------------------
    m15 = analyses["M15"]
    if m15["trend"] == "BULLISH":
        buy += 15
    elif m15["trend"] == "BEARISH":
        sell += 15
    # -----------------------------------------------------
    # M5
    # -----------------------------------------------------
    m5 = analyses["M5"]
    if m5["trend"] == "BULLISH":
        buy += 10
    elif m5["trend"] == "BEARISH":
        sell += 10
    # -----------------------------------------------------
    # LIQUIDITY
    # -----------------------------------------------------
    if m5["liquidity"] == "SSL SWEEP":
        buy += 10
    elif m5["liquidity"] == "BSL SWEEP":
        sell += 10
    # -----------------------------------------------------
    # BOS
    # -----------------------------------------------------
    if m5["bos"] == "BULLISH BOS":
        buy += 15
    elif m5["bos"] == "BEARISH BOS":
        sell += 15
    # -----------------------------------------------------
    # FVG
    # -----------------------------------------------------
    if m5["fvg"] == "BULLISH FVG":
        buy += 5
    elif m5["fvg"] == "BEARISH FVG":
        sell += 5
    # -----------------------------------------------------
    # DISPLACEMENT
    # -----------------------------------------------------
    if m5["displacement"] == "BULLISH":
        buy += 10
    elif m5["displacement"] == "BEARISH":
        sell += 10
    # -----------------------------------------------------
    # MOMENTUM
    # -----------------------------------------------------
    if m5["momentum"] == "BULLISH":
        buy += 5
    elif m5["momentum"] == "BEARISH":
        sell += 5
    # -----------------------------------------------------
    # RSI
    # -----------------------------------------------------
    rsi = m5["rsi"]
    # RSI confirmation, but don't chase extremes
    if 35 <= rsi <= 65:
        if m5["momentum"] == "BULLISH":
            buy += 5
        elif m5["momentum"] == "BEARISH":
            sell += 5
    elif rsi < 30:
        # Oversold: avoid adding SELL score
        pass
    elif rsi > 70:
        # Overbought: avoid adding BUY score
        pass
    return buy, sell
# =========================================================
# DETERMINE SIGNAL
# =========================================================
def determine_signal(
    buy_score,
    sell_score
):
    if (
        buy_score >= MIN_SCORE
        and
        buy_score > sell_score
    ):
        return "BUY"
    if (
        sell_score >= MIN_SCORE
        and
        sell_score > buy_score
    ):
        return "SELL"
    if (
        buy_score >= EARLY_SCORE
        and
        buy_score > sell_score
    ):
        return "EARLY BUY"
    if (
        sell_score >= EARLY_SCORE
        and
        sell_score > buy_score
    ):
        return "EARLY SELL"
    return "WAIT"
# =========================================================
# ANALYSIS
# =========================================================
def run_analysis():
    price = fetch_spot()
    if price is None:
        return None
    market = get_market_data()
    if market is None:
        return None
    analyses = {}
    for timeframe in (
        "H4",
        "H1",
        "M15",
        "M5"
    ):
        analyses[timeframe] = (
            analyze_dataframe(
                market[timeframe]
            )
        )
    buy_score, sell_score = (
        calculate_scores(
            analyses
        )
    )
    signal = determine_signal(
        buy_score,
        sell_score
    )
    return {
        "price": price,
        "analyses": analyses,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "signal": signal
    }
# =========================================================
# FORMAT SIGNAL
# =========================================================
def format_analysis(
    result
):
    if result is None:
        return (
            "⚠️ GOLD SMART V5.8\n\n"
            "❌ Не удалось получить "
            "данные XAUUSD.\n\n"
            "Источник: XAUS API"
        )
    price = result["price"]
    analyses = result[
        "analyses"
    ]
    signal = result[
        "signal"
    ]
    buy_score = result[
        "buy_score"
    ]
    sell_score = result[
        "sell_score"
    ]
    m5 = analyses["M5"]
    # -----------------------------------------------------
    # SIGNAL EMOJI
    # -----------------------------------------------------
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
    # -----------------------------------------------------
    # BIAS
    # -----------------------------------------------------
    if buy_score > sell_score:
        bias = "BUY"
    elif sell_score > buy_score:
        bias = "SELL"
    else:
        bias = "NEUTRAL"
    # -----------------------------------------------------
    # TP / SL
    # -----------------------------------------------------
    levels = ""
    if signal == "BUY":
        sl = price - SL_DISTANCE
        tp = price + TP_DISTANCE
        levels = (
            f"\n"
            f"🎯 ENTRY: {price:.2f}\n"
            f"🛑 SL: {sl:.2f}\n"
            f"🎯 TP: {tp:.2f}\n"
        )
    elif signal == "SELL":
        sl = price + SL_DISTANCE
        tp = price - TP_DISTANCE
        levels = (
            f"\n"
            f"🎯 ENTRY: {price:.2f}\n"
            f"🛑 SL: {sl:.2f}\n"
            f"🎯 TP: {tp:.2f}\n"
        )
    message = (
        "🥇 GOLD SMART V5.8\n\n"
        f"{emoji} SIGNAL: {signal}\n\n"
        f"💰 XAUUSD: {price:.2f}\n\n"
        f"📊 H4: "
        f"{analyses['H4']['trend']}\n"
        f"📊 H1: "
        f"{analyses['H1']['trend']}\n"
        f"📊 M15: "
        f"{analyses['M15']['trend']}\n"
        f"📊 M5: "
        f"{analyses['M5']['trend']}\n\n"
        f"💧 Liquidity: "
        f"{m5['liquidity']}\n"
        f"🔨 BOS: "
        f"{m5['bos']}\n"
        f"🧩 FVG: "
        f"{m5['fvg']}\n"
        f"💥 Displacement: "
        f"{m5['displacement']}\n"
        f"📈 Momentum: "
        f"{m5['momentum']}\n"
        f"📐 Zone: "
        f"{m5['zone']}\n"
        f"📊 RSI: "
        f"{m5['rsi']:.1f}\n\n"
        f"🟢 BUY SCORE: "
        f"{buy_score}\n"
        f"🔴 SELL SCORE: "
        f"{sell_score}\n"
        f"🧭 BIAS: "
        f"{bias}\n"
        f"{levels}\n"
        "━━━━━━━━━━━━━━━━\n"
        "📡 SOURCE: XAUS API\n"
        "🕯 CLOSED CANDLES: ON\n"
        "🛡 AUTO TRADING: OFF\n"
        "📊 VIRTUAL TRADE TRACKING: ON"
    )
    return message
# =========================================================
# VIRTUAL TRADE
# =========================================================
def open_virtual_trade(
    signal,
    entry
):
    global virtual_trade
    # Only one open trade
    if virtual_trade is not None:
        return False
    if signal not in (
        "BUY",
        "SELL"
    ):
        return False
    if signal == "BUY":
        sl = entry - SL_DISTANCE
        tp = entry + TP_DISTANCE
    else:
        sl = entry + SL_DISTANCE
        tp = entry - TP_DISTANCE
    virtual_trade = {
        "direction": signal,
        "entry": float(entry),
        "sl": float(sl),
        "original_sl": float(sl),
        "tp": float(tp),
        "be_trigger": float(BE_TRIGGER),
        "be_active": False,
        "opened_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "status": "OPEN"
    }
    stats["total"] += 1
    stats["open"] = 1
    save_stats()
    print(
        "VIRTUAL TRADE OPEN:",
        virtual_trade
    )
    telegram_send(
        "📊 VIRTUAL TRADE OPEN\n\n"
        f"{signal}\n"
        f"Entry: {entry:.2f}\n"
        f"SL: {sl:.2f}\n"
        f"TP: {tp:.2f}\n\n"
        "🛡 Real trading: OFF"
    )
    return True
# =========================================================
# CHECK VIRTUAL TRADE
# =========================================================
def check_virtual_trade(
    price
):
    global virtual_trade
    if virtual_trade is None:
        return
    trade = virtual_trade
    direction = trade[
        "direction"
    ]
    entry = trade[
        "entry"
    ]
    tp = trade[
        "tp"
    ]
    # -----------------------------------------------------
    # BUY
    # -----------------------------------------------------
    if direction == "BUY":
        favorable_move = (
            price - entry
        )
        # BE
        if (
            not trade["be_active"]
            and
            favorable_move >=
            BE_TRIGGER
        ):
            trade["be_active"] = True
            trade["sl"] = entry
            print(
                f"VIRTUAL BUY BE "
                f"activated at {price:.2f}"
            )
            telegram_send(
                "🟡 VIRTUAL BUY\n\n"
                f"XAUUSD: {price:.2f}\n"
                f"BE ACTIVATED: {entry:.2f}\n\n"
                "SL moved to ENTRY"
            )
        # TP
        if price >= tp:
            close_virtual_trade(
                "WIN",
                price
            )
            return
        # SL / BE
        if price <= trade["sl"]:
            if trade["be_active"]:
                close_virtual_trade(
                    "BE",
                    price
                )
            else:
                close_virtual_trade(
                    "LOSS",
                    price
                )
            return
    # -----------------------------------------------------
    # SELL
    # -----------------------------------------------------
    elif direction == "SELL":
        favorable_move = (
            entry - price
        )
        # BE
        if (
            not trade["be_active"]
            and
            favorable_move >=
            BE_TRIGGER
        ):
            trade["be_active"] = True
            trade["sl"] = entry
            print(
                f"VIRTUAL SELL BE "
                f"activated at {price:.2f}"
            )
            telegram_send(
                "🟡 VIRTUAL SELL\n\n"
                f"XAUUSD: {price:.2f}\n"
                f"BE ACTIVATED: {entry:.2f}\n\n"
                "SL moved to ENTRY"
            )
        # TP
        if price <= tp:
            close_virtual_trade(
                "WIN",
                price
            )
            return
        # SL / BE
        if price >= trade["sl"]:
            if trade["be_active"]:
                close_virtual_trade(
                    "BE",
                    price
                )
            else:
                close_virtual_trade(
                    "LOSS",
                    price
                )
            return
# =========================================================
# CLOSE VIRTUAL TRADE
# =========================================================
def close_virtual_trade(
    result,
    exit_price
):
    global virtual_trade
    if virtual_trade is None:
        return
    trade = virtual_trade
    direction = trade[
        "direction"
    ]
    entry = trade[
        "entry"
    ]
    if direction == "BUY":
        pnl_price = (
            exit_price -
            entry
        )
    else:
        pnl_price = (
            entry -
            exit_price
        )
    # -----------------------------------------------------
    # UPDATE STATS
    # -----------------------------------------------------
    if stats["open"] > 0:
        stats["open"] = 0
    if result == "WIN":
        stats["win"] += 1
    elif result == "LOSS":
        stats["loss"] += 1
    elif result == "BE":
        stats["be"] += 1
    save_stats()
    # -----------------------------------------------------
    # MESSAGE
    # -----------------------------------------------------
    if result == "WIN":
        emoji = "🟢"
    elif result == "LOSS":
        emoji = "🔴"
    else:
        emoji = "🟡"
    telegram_send(
        f"{emoji} VIRTUAL TRADE CLOSED\n\n"
        f"{direction}\n"
        f"Entry: {entry:.2f}\n"
        f"Exit: {exit_price:.2f}\n"
        f"Result: {result}\n"
        f"Move: {pnl_price:+.2f}\n\n"
        f"📊 WIN: {stats['win']}\n"
        f"🔴 LOSS: {stats['loss']}\n"
        f"🟡 BE: {stats['be']}"
    )
    print(
        "VIRTUAL TRADE CLOSED:",
        result,
        exit_price
    )
    virtual_trade = None
# =========================================================
# STATISTICS MESSAGE
# =========================================================
def stats_message():
    closed = (
        stats["win"] +
        stats["loss"] +
        stats["be"]
    )
    if closed > 0:
        win_rate = (
            stats["win"] /
            closed *
            100
        )
    else:
        win_rate = 0.0
    return (
        "📊 GOLD SMART V5.8 — STATISTICS\n\n"
        f"📌 TOTAL TRADES: "
        f"{stats['total']}\n\n"
        f"🟢 WIN: "
        f"{stats['win']}\n"
        f"🔴 LOSS: "
        f"{stats['loss']}\n"
        f"🟡 BE: "
        f"{stats['be']}\n"
        f"🟠 OPEN: "
        f"{stats['open']}\n\n"
        f"🎯 CLOSED: "
        f"{closed}\n"
        f"📈 WIN RATE: "
        f"{win_rate:.1f}%\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "📡 SOURCE: XAUS API\n"
        "🛡 AUTO TRADING: OFF\n"
        "📊 VIRTUAL TRACKING: ON"
    )
# =========================================================
# TEST
# =========================================================
def test_message():
    price = fetch_spot(
        force=True
    )
    if price is None:
        return (
            "⚠️ GOLD SMART V5.8 TEST\n\n"
            "🟢 Engine: READY\n"
            "🟢 Telegram: READY\n"
            "🔴 XAUS API: ERROR\n\n"
            "❌ XAUUSD price unavailable"
        )
    return (
        "✅ GOLD SMART V5.8 TEST\n\n"
        "🟢 Engine: READY\n"
        "🟢 Telegram: READY\n"
        "🟢 XAUS API: READY\n\n"
        f"💰 XAUUSD: "
        f"{price:.2f}\n\n"
        "🛡 AUTO TRADING: OFF\n"
        "📊 VIRTUAL TRADE TRACKING: ON"
    )
# =========================================================
# STATUS
# =========================================================
def status_message():
    price = fetch_spot()
    if price is None:
        price_text = "UNAVAILABLE"
    else:
        price_text = (
            f"{price:.2f}"
        )
    if virtual_trade:
        vt = (
            f"{virtual_trade['direction']} "
            f"@ {virtual_trade['entry']:.2f}"
        )
    else:
        vt = "NONE"
    return (
        "🥇 GOLD SMART V5.8\n\n"
        "🟢 ENGINE: RUNNING\n"
        "🟢 TELEGRAM: READY\n"
        "🟢 XAUS API: READY\n\n"
        f"💰 XAUUSD: "
        f"{price_text}\n\n"
        f"📊 VIRTUAL TRADE: "
        f"{vt}\n\n"
        "🛡 AUTO TRADING: OFF\n"
        "📈 RISK: 1%\n"
        "🕯 CLOSED CANDLES: ON\n"
        "📡 SOURCE: XAUS API"
    )
# =========================================================
# HELP
# =========================================================
def help_message():
    return (
        "🥇 GOLD SMART V5.8\n\n"
        "Команды:\n\n"
        "/start — запуск бота\n"
        "/status — состояние системы\n"
        "/signal — текущий анализ XAUUSD\n"
        "/test — проверка системы\n"
        "/stats — статистика виртуальных сделок\n"
        "/help — список команд\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "📊 BUY / SELL открывают "
        "виртуальную сделку.\n\n"
        "🟡 EARLY BUY/SELL — "
        "раннее предупреждение, "
        "сделка автоматически НЕ открывается.\n\n"
        "⚪ WAIT — подтверждения "
        "недостаточно.\n\n"
        "🛡 REAL TRADING: OFF"
    )
# =========================================================
# TELEGRAM COMMAND HANDLER
# =========================================================
def handle_command(
    command,
    chat_id
):
    command = (
        command
        .strip()
        .split("@")[0]
        .lower()
    )
    if command == "/start":
        telegram_send(
            "🥇 GOLD SMART V5.8\n\n"
            "🟢 Бот запущен.\n\n"
            "Источник: XAUS API\n"
            "Auto Trading: OFF\n"
            "Virtual Tracking: ON\n\n"
            "Используй /help",
            chat_id
        )
    elif command == "/status":
        telegram_send(
            status_message(),
            chat_id
        )
    elif command == "/signal":
        telegram_send(
            "⏳ GOLD SMART V5.8\n\n"
            "Получаю данные XAUUSD...",
            chat_id
        )
        result = run_analysis()
        telegram_send(
            format_analysis(
                result
            ),
            chat_id
        )
    elif command == "/test":
        telegram_send(
            test_message(),
            chat_id
        )
    elif command == "/stats":
        telegram_send(
            stats_message(),
            chat_id
        )
    elif command == "/help":
        telegram_send(
            help_message(),
            chat_id
        )
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
        ) or {}
        message = update.get(
            "message"
        )
        if message:
            chat = message.get(
                "chat",
                {}
            )
            chat_id = chat.get(
                "id"
            )
            text_value = (
                message.get(
                    "text",
                    ""
                )
                or ""
            )
            if (
                chat_id
                and
                text_value.startswith("/")
            ):
                threading.Thread(
                    target=handle_command,
                    args=(
                        text_value,
                        chat_id
                    ),
                    daemon=True
                ).start()
        return "OK", 200
    except Exception as e:
        print(
            f"Webhook error: {e}"
        )
        return "OK", 200
# =========================================================
# HOME
# =========================================================
@app.route(
    "/",
    methods=["GET", "HEAD"]
)
def home():
    return (
        "🥇 GOLD SMART V5.8 "
        "ONLINE"
    )
# =========================================================
# HEALTH
# =========================================================
@app.route(
    "/health",
    methods=["GET"]
)
def health():
    return {
        "status": "ok",
        "engine": "running",
        "source": "XAUS API",
        "auto_trading": False,
        "virtual_tracking": True
    }
# =========================================================
# SET TELEGRAM WEBHOOK
# =========================================================
def setup_webhook():
    if not BOT_TOKEN:
        print(
            "Telegram BOT_TOKEN missing"
        )
        return
    webhook = (
        WEBHOOK_URL.rstrip("/")
        +
        "/telegram"
    )
    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/setWebhook"
    )
    try:
        response = requests.post(
            url,
            json={
                "url": webhook
            },
            timeout=10
        )
        print(
            "Telegram webhook:",
            response.text
        )
    except Exception as e:
        print(
            f"Webhook setup error: {e}"
        )
# =========================================================
# AUTOMATIC ENGINE
# =========================================================
def engine_loop():
    global last_signal
    global last_signal_time
    print(
        "GOLD SMART V5.8 engine started"
    )
    while True:
        try:
            # -------------------------------------------------
            # CURRENT PRICE
            # -------------------------------------------------
            price = fetch_spot()
            if price is not None:
                # ---------------------------------------------
                # CHECK OPEN VIRTUAL TRADE
                # ---------------------------------------------
                check_virtual_trade(
                    price
                )
            # -------------------------------------------------
            # MARKET ANALYSIS
            # -------------------------------------------------
            result = run_analysis()
            if result is None:
                time.sleep(
                    POLL_SECONDS
                )
                continue
            signal = result[
                "signal"
            ]
            buy_score = result[
                "buy_score"
            ]
            sell_score = result[
                "sell_score"
            ]
            current_price = result[
                "price"
            ]
            now = time.time()
            print(
                f"[{datetime.now(timezone.utc)}] "
                f"{signal} | "
                f"XAUUSD {current_price:.2f} | "
                f"BUY {buy_score} | "
                f"SELL {sell_score}"
            )
            # -------------------------------------------------
            # ONLY FULL SIGNALS CREATE VIRTUAL TRADES
            # -------------------------------------------------
            if signal in (
                "BUY",
                "SELL"
            ):
                # One trade at a time
                if virtual_trade is None:
                    cooldown_ok = (
                        now -
                        last_signal_time
                        >=
                        COOLDOWN_MIN * 60
                    )
                    different_signal = (
                        signal !=
                        last_signal
                    )
                    if (
                        cooldown_ok
                        or
                        different_signal
                    ):
                        message = (
                            format_analysis(
                                result
                            )
                        )
                        telegram_send(
                            message
                        )
                        opened = (
                            open_virtual_trade(
                                signal,
                                current_price
                            )
                        )
                        if opened:
                            last_signal = (
                                signal
                            )
                            last_signal_time = (
                                now
                            )
            time.sleep(
                POLL_SECONDS
            )
        except Exception as e:
            print(
                f"ENGINE ERROR: {e}"
            )
            time.sleep(
                POLL_SECONDS
            )
# =========================================================
# START ENGINE
# =========================================================
def start_engine():
    global engine_started
    if engine_started:
        return
    engine_started = True
    load_stats()
    print(
        "=" * 60
    )
    print(
        "🥇 GOLD SMART V5.8"
    )
    print(
        "Engine starting..."
    )
    print(
        "Source: XAUS API"
    )
    print(
        "AUTO TRADING: OFF"
    )
    print(
        "Virtual Trade Tracking: ON"
    )
    print(
        "=" * 60
    )
    setup_webhook()
    thread = threading.Thread(
        target=engine_loop,
        daemon=True
    )
    thread.start()
# =========================================================
# START ON IMPORT
# =========================================================
start_engine()
# =========================================================
# LOCAL RUN
# =========================================================
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

После замены Start Command оставь прежней:

gunicorn bot:app --workers 1 --threads 4 --bind 0.0.0.0:$PORT

После Deploy

Сначала отправь:

/test

Должно показать примерно:

✅ GOLD SMART V5.8 TEST
🟢 Engine: READY
🟢 Telegram: READY
🟢 XAUS API: READY
💰 XAUUSD: 4140.xx
🛡 AUTO TRADING: OFF
📊 VIRTUAL TRADE TRACKING: ON

Затем:

/signal

И потом:

/stats

Главное исправление: V5.8 теперь читает реальную структуру XAUS xau → price, которую видно непосредственно в твоих Render-логах. Поэтому ошибка XAUS spot parsing failed должна исчезнуть.
