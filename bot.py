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
# XAUS API
# Telegram Webhook
# AUTO TRADING = OFF
#
# NEW:
# - Automatic virtual trade tracking
# - ENTRY / SL / TP
# - WIN / LOSS / BE
# - Automatic statistics
# - No UP/DOWN percentages
# =========================================================
# =========================================================
# CONFIG
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
WEBHOOK_URL = os.getenv(
    "WEBHOOK_URL",
    "https://gold-bot-q8la.onrender.com/telegram"
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
AUTO_TRADING = False
CLOSED_CANDLES = True
# Virtual trade parameters
SL_DISTANCE = 5.0
TP_DISTANCE = 15.0
# Move SL to entry after +6 gold points
BE_TRIGGER = 6.0
STATS_FILE = "gold_stats.json"
CACHE_SECONDS = 45
# =========================================================
# FLASK
# =========================================================
app = Flask(__name__)
# =========================================================
# GLOBAL STATE
# =========================================================
state_lock = threading.Lock()
last_signal = None
last_signal_time = 0
price_cache = {
    "price": None,
    "time": 0
}
data_cache = {}
open_trade = None
engine_started = False
# =========================================================
# BASIC HELPERS
# =========================================================
def now_utc():
    return datetime.now(timezone.utc)
def now_string():
    return now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")
def safe_float(value, default=np.nan):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default
# =========================================================
# TELEGRAM
# =========================================================
def telegram_send(text_message):
    if not BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials are missing")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text_message
    }
    try:
        response = requests.post(
            url,
            json=payload,
            timeout=10
        )
        if response.ok:
            return True
        print("Telegram error:", response.status_code, response.text)
        return False
    except Exception as e:
        print("Telegram exception:", e)
        return False
# =========================================================
# XAUS SPOT
# =========================================================
def fetch_spot(force=False):
    current_time = time.time()
    if (
        not force
        and price_cache["price"] is not None
        and current_time - price_cache["time"] < CACHE_SECONDS
    ):
        return price_cache["price"]
    url = f"{XAUS_BASE}/spot"
    params = {
        "currency": "USD",
        "unit": "oz",
        "compact": 1
    }
    try:
        response = requests.get(
            url,
            params=params,
            timeout=15
        )
        response.raise_for_status()
        data = response.json()
        price = None
        if isinstance(data, dict):
            # Common schemas
            for key in [
                "price",
                "spot",
                "value",
                "rate",
                "mid",
                "xau"
            ]:
                if key in data:
                    candidate = safe_float(data[key], None)
                    if candidate is not None:
                        price = candidate
                        break
            # Nested result/data
            if price is None:
                for container_key in ["data", "result", "spot"]:
                    container = data.get(container_key)
                    if isinstance(container, dict):
                        for key in [
                            "price",
                            "spot",
                            "value",
                            "rate",
                            "mid"
                        ]:
                            if key in container:
                                candidate = safe_float(
                                    container[key],
                                    None
                                )
                                if candidate is not None:
                                    price = candidate
                                    break
                    if price is not None:
                        break
        elif isinstance(data, (int, float)):
            price = float(data)
        if price is None or price <= 0:
            print("XAUS spot parsing failed:", data)
            return None
        price_cache["price"] = price
        price_cache["time"] = current_time
        return price
    except Exception as e:
        print("XAUS spot error:", e)
        return None
# =========================================================
# XAUS CHART
# =========================================================
def parse_chart_points(data):
    points = None
    if isinstance(data, dict):
        for key in [
            "points",
            "data",
            "chart",
            "candles",
            "bars",
            "result"
        ]:
            if key in data:
                candidate = data[key]
                if isinstance(candidate, list):
                    points = candidate
                    break
                if isinstance(candidate, dict):
                    for nested_key in [
                        "points",
                        "data",
                        "candles",
                        "bars"
                    ]:
                        if nested_key in candidate:
                            if isinstance(candidate[nested_key], list):
                                points = candidate[nested_key]
                                break
                    if points is not None:
                        break
    elif isinstance(data, list):
        points = data
    if not points:
        return None
    rows = []
    for item in points:
        # -------------------------------------------------
        # Dictionary format
        # -------------------------------------------------
        if isinstance(item, dict):
            timestamp = (
                item.get("t")
                or item.get("time")
                or item.get("timestamp")
                or item.get("datetime")
                or item.get("date")
            )
            o = item.get("o", item.get("open"))
            h = item.get("h", item.get("high"))
            l = item.get("l", item.get("low"))
            c = item.get("c", item.get("close"))
            v = item.get("v", item.get("volume", 0))
            rows.append({
                "timestamp": timestamp,
                "open": safe_float(o),
                "high": safe_float(h),
                "low": safe_float(l),
                "close": safe_float(c),
                "volume": safe_float(v, 0)
            })
        # -------------------------------------------------
        # List format
        # -------------------------------------------------
        elif isinstance(item, (list, tuple)) and len(item) >= 5:
            rows.append({
                "timestamp": item[0],
                "open": safe_float(item[1]),
                "high": safe_float(item[2]),
                "low": safe_float(item[3]),
                "close": safe_float(item[4]),
                "volume": safe_float(
                    item[5] if len(item) > 5 else 0,
                    0
                )
            })
    if not rows:
        return None
    df = pd.DataFrame(rows)
    if "timestamp" not in df.columns:
        return None
    # -----------------------------------------------------
    # Timestamp conversion
    # -----------------------------------------------------
    ts_numeric = pd.to_numeric(
        df["timestamp"],
        errors="coerce"
    )
    if ts_numeric.notna().sum() > 0:
        median_ts = ts_numeric.dropna().median()
        if median_ts > 1e12:
            unit = "ms"
        elif median_ts > 1e9:
            unit = "s"
        else:
            unit = None
        if unit:
            index = pd.to_datetime(
                ts_numeric,
                unit=unit,
                utc=True,
                errors="coerce"
            )
        else:
            index = pd.to_datetime(
                df["timestamp"],
                utc=True,
                errors="coerce"
            )
    else:
        index = pd.to_datetime(
            df["timestamp"],
            utc=True,
            errors="coerce"
        )
    df.index = index
    df = df[
        ["open", "high", "low", "close", "volume"]
    ]
    df = df.dropna(
        subset=["open", "high", "low", "close"]
    )
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    if len(df) < 30:
        return None
    # -----------------------------------------------------
    # Closed candles only
    # Conservative: remove last candle
    # -----------------------------------------------------
    if CLOSED_CANDLES and len(df) > 30:
        df = df.iloc[:-1]
    return df
def get_chart(interval, range_value):
    cache_key = f"{interval}_{range_value}"
    current_time = time.time()
    cached = data_cache.get(cache_key)
    if cached:
        if current_time - cached["time"] < CACHE_SECONDS:
            return cached["df"].copy()
    url = f"{XAUS_BASE}/chart"
    params = {
        "symbol": "xau",
        "range": range_value,
        "interval": interval
    }
    try:
        response = requests.get(
            url,
            params=params,
            timeout=20
        )
        response.raise_for_status()
        data = response.json()
        df = parse_chart_points(data)
        if df is None:
            print(
                "Chart parse failed:",
                interval,
                str(data)[:500]
            )
            return None
        data_cache[cache_key] = {
            "df": df.copy(),
            "time": current_time
        }
        return df
    except Exception as e:
        print(
            f"XAUS chart error {interval}:",
            e
        )
        return None
# =========================================================
# INDICATORS
# =========================================================
def ema(series, period):
    return series.ewm(
        span=period,
        adjust=False
    ).mean()
def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()
    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False
    ).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi
def trend_from_df(df):
    if df is None or len(df) < 20:
        return "MIXED"
    close = df["close"]
    ema50 = ema(close, 50).iloc[-1]
    ema200 = (
        ema(close, 200).iloc[-1]
        if len(df) >= 200
        else np.nan
    )
    last = close.iloc[-1]
    if not np.isnan(ema200):
        if last > ema50 and ema50 > ema200:
            return "BULLISH"
        if last < ema50 and ema50 < ema200:
            return "BEARISH"
    # Fallback if 200 EMA unavailable
    if last > ema50:
        return "BULLISH"
    if last < ema50:
        return "BEARISH"
    return "MIXED"
def detect_bos(df):
    if df is None or len(df) < 15:
        return "NONE"
    current = df.iloc[-1]
    previous_high = df["high"].iloc[-13:-1].max()
    previous_low = df["low"].iloc[-13:-1].min()
    if current["close"] > previous_high:
        return "BULLISH"
    if current["close"] < previous_low:
        return "BEARISH"
    return "NONE"
def detect_liquidity(df):
    if df is None or len(df) < 12:
        return "NONE"
    current = df.iloc[-1]
    recent_high = df["high"].iloc[-11:-1].max()
    recent_low = df["low"].iloc[-11:-1].min()
    # Buy-side liquidity sweep
    if (
        current["high"] > recent_high
        and current["close"] < recent_high
    ):
        return "BSL SWEEP"
    # Sell-side liquidity sweep
    if (
        current["low"] < recent_low
        and current["close"] > recent_low
    ):
        return "SSL SWEEP"
    return "NONE"
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
def detect_displacement(df):
    if df is None or len(df) < 25:
        return "NONE"
    current = df.iloc[-1]
    current_range = (
        current["high"] -
        current["low"]
    )
    previous_ranges = (
        df["high"] -
        df["low"]
    ).iloc[-21:-1]
    mean_range = previous_ranges.mean()
    if mean_range <= 0:
        return "NONE"
    if current_range >= mean_range * 1.5:
        if current["close"] > current["open"]:
            return "BULLISH"
        if current["close"] < current["open"]:
            return "BEARISH"
    return "NONE"
def detect_momentum(df):
    if df is None or len(df) < 50:
        return "MIXED"
    close = df["close"]
    ema20 = ema(close, 20).iloc[-1]
    ema50 = ema(close, 50).iloc[-1]
    if ema20 > ema50:
        return "BULLISH"
    if ema20 < ema50:
        return "BEARISH"
    return "MIXED"
def detect_zone(df):
    if df is None or len(df) < 20:
        return "EQUILIBRIUM"
    recent = df.iloc[-50:]
    highest = recent["high"].max()
    lowest = recent["low"].min()
    midpoint = (
        highest + lowest
    ) / 2
    price = df["close"].iloc[-1]
    if price > midpoint:
        return "PREMIUM"
    if price < midpoint:
        return "DISCOUNT"
    return "EQUILIBRIUM"
# =========================================================
# ANALYSIS
# =========================================================
def analyze_market():
    m5 = get_chart("5m", "1d")
    m15 = get_chart("15m", "5d")
    h1 = get_chart("60m", "1mo")
    if (
        m5 is None
        or m15 is None
        or h1 is None
    ):
        return None
    # -----------------------------------------------------
    # H4 from H1
    # -----------------------------------------------------
    h1_for_h4 = h1.copy()
    h4 = (
        h1_for_h4
        .resample("4h")
        .agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum"
        })
        .dropna()
    )
    if len(h4) < 20:
        return None
    price = fetch_spot()
    if price is None:
        return None
    # -----------------------------------------------------
    # Trends
    # -----------------------------------------------------
    h4_trend = trend_from_df(h4)
    h1_trend = trend_from_df(h1)
    m15_trend = trend_from_df(m15)
    m5_trend = trend_from_df(m5)
    # -----------------------------------------------------
    # Structure
    # -----------------------------------------------------
    liquidity = detect_liquidity(m5)
    bos = detect_bos(m5)
    fvg = detect_fvg(m5)
    displacement = detect_displacement(m5)
    momentum = detect_momentum(m5)
    zone = detect_zone(m5)
    rsi_value = calculate_rsi(
        m5["close"],
        14
    ).iloc[-1]
    rsi_value = safe_float(
        rsi_value,
        50
    )
    # =====================================================
    # SCORE
    # =====================================================
    buy_score = 0
    sell_score = 0
    # H4
    if h4_trend == "BULLISH":
        buy_score += 20
    elif h4_trend == "BEARISH":
        sell_score += 20
    # H1
    if h1_trend == "BULLISH":
        buy_score += 15
    elif h1_trend == "BEARISH":
        sell_score += 15
    # M15
    if m15_trend == "BULLISH":
        buy_score += 15
    elif m15_trend == "BEARISH":
        sell_score += 15
    # M5
    if m5_trend == "BULLISH":
        buy_score += 10
    elif m5_trend == "BEARISH":
        sell_score += 10
    # Liquidity
    if liquidity == "SSL SWEEP":
        buy_score += 10
    elif liquidity == "BSL SWEEP":
        sell_score += 10
    # BOS
    if bos == "BULLISH":
        buy_score += 15
    elif bos == "BEARISH":
        sell_score += 15
    # FVG
    if fvg == "BULLISH FVG":
        buy_score += 5
    elif fvg == "BEARISH FVG":
        sell_score += 5
    # Displacement
    if displacement == "BULLISH":
        buy_score += 10
    elif displacement == "BEARISH":
        sell_score += 10
    # Momentum
    if momentum == "BULLISH":
        buy_score += 5
    elif momentum == "BEARISH":
        sell_score += 5
    # RSI
    # Avoid blindly treating oversold as SELL.
    # RSI contributes only when direction is not extreme.
    if 50 <= rsi_value <= 70:
        buy_score += 5
    elif 30 <= rsi_value <= 50:
        sell_score += 5
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
    # SIGNAL
    # -----------------------------------------------------
    signal = "WAIT"
    if (
        buy_score >= MIN_SCORE
        and buy_score > sell_score
    ):
        signal = "BUY"
    elif (
        sell_score >= MIN_SCORE
        and sell_score > buy_score
    ):
        signal = "SELL"
    elif (
        buy_score >= EARLY_SCORE
        and buy_score > sell_score
    ):
        signal = "EARLY BUY"
    elif (
        sell_score >= EARLY_SCORE
        and sell_score > buy_score
    ):
        signal = "EARLY SELL"
    return {
        "price": price,
        "h4": h4_trend,
        "h1": h1_trend,
        "m15": m15_trend,
        "m5": m5_trend,
        "liquidity": liquidity,
        "bos": bos,
        "fvg": fvg,
        "displacement": displacement,
        "momentum": momentum,
        "zone": zone,
        "rsi": rsi_value,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "bias": bias,
        "signal": signal,
        "time": now_string()
    }
# =========================================================
# STATS
# =========================================================
def default_stats():
    return {
        "total_signals": 0,
        "wins": 0,
        "losses": 0,
        "be": 0,
        "open": 0,
        "trades": []
    }
def load_stats():
    if not os.path.exists(STATS_FILE):
        return default_stats()
    try:
        with open(
            STATS_FILE,
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)
        base = default_stats()
        base.update(data)
        return base
    except Exception as e:
        print("Stats load error:", e)
        return default_stats()
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
    except Exception as e:
        print("Stats save error:", e)
# =========================================================
# VIRTUAL TRADE
# =========================================================
def create_virtual_trade(analysis):
    global open_trade
    signal = analysis["signal"]
    if signal not in [
        "BUY",
        "SELL"
    ]:
        return None
    # Do not open another virtual trade
    if open_trade is not None:
        return None
    entry = float(analysis["price"])
    if signal == "BUY":
        sl = entry - SL_DISTANCE
        tp = entry + TP_DISTANCE
    else:
        sl = entry + SL_DISTANCE
        tp = entry - TP_DISTANCE
    risk_money = (
        DEFAULT_DEPOSIT *
        RISK_PERCENT /
        100
    )
    # Conservative lot model
    lot = MIN_LOT
    if risk_money >= 10:
        lot = min(0.02, MAX_LOT)
    trade = {
        "id": int(time.time()),
        "signal": signal,
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "initial_sl": round(sl, 2),
        "tp": round(tp, 2),
        "lot": lot,
        "risk_percent": RISK_PERCENT,
        "risk_money": round(risk_money, 2),
        "be_active": False,
        "opened_at": now_string(),
        "status": "OPEN",
        "close_price": None,
        "closed_at": None,
        "result": None
    }
    open_trade = trade
    stats = load_stats()
    stats["total_signals"] += 1
    stats["open"] = 1
    stats["trades"].append(trade.copy())
    # Keep history manageable
    stats["trades"] = stats["trades"][-200:]
    save_stats(stats)
    return trade
def update_trade_in_stats(trade):
    stats = load_stats()
    for item in reversed(stats["trades"]):
        if item.get("id") == trade.get("id"):
            item.update(trade)
            break
    save_stats(stats)
def close_virtual_trade(
    trade,
    result,
    close_price
):
    global open_trade
    trade["status"] = "CLOSED"
    trade["result"] = result
    trade["close_price"] = round(
        close_price,
        2
    )
    trade["closed_at"] = now_string()
    stats = load_stats()
    if result == "WIN":
        stats["wins"] += 1
    elif result == "LOSS":
        stats["losses"] += 1
    elif result == "BE":
        stats["be"] += 1
    stats["open"] = 0
    # Update existing trade
    for item in reversed(stats["trades"]):
        if item.get("id") == trade.get("id"):
            item.update(trade)
            break
    save_stats(stats)
    open_trade = None
    # -----------------------------------------------------
    # Telegram result
    # -----------------------------------------------------
    if result == "WIN":
        emoji = "🟢"
    elif result == "LOSS":
        emoji = "🔴"
    else:
        emoji = "⚪"
    message = (
        f"{emoji} GOLD SMART V5.8\n\n"
        f"📊 VIRTUAL TRADE CLOSED\n\n"
        f"TYPE: {trade['signal']}\n"
        f"ENTRY: {trade['entry']:.2f}\n"
        f"CLOSE: {trade['close_price']:.2f}\n"
        f"RESULT: {result}\n\n"
        f"📈 TP: {trade['tp']:.2f}\n"
        f"🛑 SL: {trade['sl']:.2f}\n"
        f"⚖️ BE: {'ON' if trade['be_active'] else 'OFF'}\n\n"
        f"📡 SOURCE: XAUS API\n"
        f"🛡 AUTO TRADING: OFF"
    )
    telegram_send(message)
def check_virtual_trade():
    global open_trade
    if open_trade is None:
        return
    price = fetch_spot(force=True)
    if price is None:
        return
    trade = open_trade
    direction = trade["signal"]
    # -----------------------------------------------------
    # BUY
    # -----------------------------------------------------
    if direction == "BUY":
        # TP first
        if price >= trade["tp"]:
            close_virtual_trade(
                trade,
                "WIN",
                price
            )
            return
        # Activate BE
        if (
            not trade["be_active"]
            and price >= trade["entry"] + BE_TRIGGER
        ):
            trade["be_active"] = True
            trade["sl"] = trade["entry"]
            update_trade_in_stats(trade)
            telegram_send(
                "⚖️ GOLD SMART V5.8\n\n"
                "🔒 BREAK-EVEN ACTIVATED\n\n"
                f"BUY ENTRY: {trade['entry']:.2f}\n"
                f"CURRENT: {price:.2f}\n"
                f"NEW SL: {trade['entry']:.2f}\n\n"
                "🛡 AUTO TRADING: OFF"
            )
        # SL / BE
        if price <= trade["sl"]:
            if trade["be_active"]:
                close_virtual_trade(
                    trade,
                    "BE",
                    price
                )
            else:
                close_virtual_trade(
                    trade,
                    "LOSS",
                    price
                )
            return
    # -----------------------------------------------------
    # SELL
    # -----------------------------------------------------
    elif direction == "SELL":
        # TP first
        if price <= trade["tp"]:
            close_virtual_trade(
                trade,
                "WIN",
                price
            )
            return
        # Activate BE
        if (
            not trade["be_active"]
            and price <= trade["entry"] - BE_TRIGGER
        ):
            trade["be_active"] = True
            trade["sl"] = trade["entry"]
            update_trade_in_stats(trade)
            telegram_send(
                "⚖️ GOLD SMART V5.8\n\n"
                "🔒 BREAK-EVEN ACTIVATED\n\n"
                f"SELL ENTRY: {trade['entry']:.2f}\n"
                f"CURRENT: {price:.2f}\n"
                f"NEW SL: {trade['entry']:.2f}\n\n"
                "🛡 AUTO TRADING: OFF"
            )
        # SL / BE
        if price >= trade["sl"]:
            if trade["be_active"]:
                close_virtual_trade(
                    trade,
                    "BE",
                    price
                )
            else:
                close_virtual_trade(
                    trade,
                    "LOSS",
                    price
                )
            return
# =========================================================
# FORMAT ANALYSIS
# =========================================================
def format_analysis(a):
    return (
        "🥇 GOLD SMART V5.8\n\n"
        f"{'🟢' if a['signal'] == 'BUY' else '🔴' if a['signal'] == 'SELL' else '🟡' if 'EARLY' in a['signal'] else '⚪'} "
        f"SIGNAL: {a['signal']}\n\n"
        f"💰 XAUUSD: {a['price']:.2f}\n\n"
        f"📊 H4: {a['h4']}\n"
        f"📊 H1: {a['h1']}\n"
        f"📊 M15: {a['m15']}\n"
        f"📊 M5: {a['m5']}\n\n"
        f"💧 Liquidity: {a['liquidity']}\n"
        f"🔨 BOS: {a['bos']}\n"
        f"🧩 FVG: {a['fvg']}\n"
        f"💥 Displacement: {a['displacement']}\n"
        f"📈 Momentum: {a['momentum']}\n"
        f"📐 Zone: {a['zone']}\n"
        f"📊 RSI: {a['rsi']:.1f}\n\n"
        f"🧭 BIAS: {a['bias']}\n\n"
        f"🟢 BUY SCORE: {a['buy_score']}\n"
        f"🔴 SELL SCORE: {a['sell_score']}\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "📡 SOURCE: XAUS API\n"
        f"🕯 CLOSED CANDLES: {'ON' if CLOSED_CANDLES else 'OFF'}\n"
        "🛡 AUTO TRADING: OFF"
    )
# =========================================================
# SIGNAL COOLDOWN
# =========================================================
def can_send_signal(signal):
    global last_signal
    global last_signal_time
    if signal not in [
        "BUY",
        "SELL",
        "EARLY BUY",
        "EARLY SELL"
    ]:
        return False
    current = time.time()
    if (
        last_signal == signal
        and current - last_signal_time
        < COOLDOWN_MIN * 60
    ):
        return False
    last_signal = signal
    last_signal_time = current
    return True
# =========================================================
# ENGINE
# =========================================================
def engine_loop():
    print("GOLD SMART V5.8 engine started")
    while True:
        try:
            # -------------------------------------------------
            # First check existing virtual trade
            # -------------------------------------------------
            if open_trade is not None:
                check_virtual_trade()
            # -------------------------------------------------
            # Analyze market
            # -------------------------------------------------
            analysis = analyze_market()
            if analysis is None:
                print(
                    f"[{now_string()}] "
                    "WAIT | Data unavailable"
                )
                time.sleep(POLL_SECONDS)
                continue
            signal = analysis["signal"]
            print(
                f"[{now_string()}] "
                f"{signal} | "
                f"XAUUSD {analysis['price']:.2f} | "
                f"BUY {analysis['buy_score']} | "
                f"SELL {analysis['sell_score']}"
            )
            # -------------------------------------------------
            # Automatic signal
            # -------------------------------------------------
            if signal in [
                "BUY",
                "SELL"
            ]:
                # Only one virtual trade at a time
                if open_trade is None:
                    if can_send_signal(signal):
                        trade = create_virtual_trade(
                            analysis
                        )
                        if trade:
                            message = (
                                format_analysis(
                                    analysis
                                )
                                + "\n\n"
                                "━━━━━━━━━━━━━━━━\n"
                                "📌 VIRTUAL TRADE\n"
                                f"ENTRY: {trade['entry']:.2f}\n"
                                f"🛑 SL: {trade['sl']:.2f}\n"
                                f"🎯 TP: {trade['tp']:.2f}\n"
                                f"⚖️ BE: +{BE_TRIGGER:.2f}\n"
                                f"📦 LOT: {trade['lot']:.2f}\n"
                                "\n"
                                "📊 RESULT TRACKING: ON"
                            )
                            telegram_send(
                                message
                            )
            time.sleep(POLL_SECONDS)
        except Exception as e:
            print(
                "ENGINE ERROR:",
                repr(e)
            )
            time.sleep(POLL_SECONDS)
# =========================================================
# TELEGRAM COMMANDS
# =========================================================
def command_start():
    return (
        "🥇 GOLD SMART V5.8\n\n"
        "🤖 XAUUSD Smart Signal Engine\n\n"
        "📡 Source: XAUS API\n"
        "🕯 Closed Candles: ON\n"
        "🛡 Auto Trading: OFF\n"
        "📊 Virtual Trade Tracking: ON\n\n"
        "Команды:\n"
        "/status — статус системы\n"
        "/signal — текущий анализ\n"
        "/test — тест системы\n"
        "/stats — статистика\n"
        "/help — помощь"
    )
def command_status():
    price = fetch_spot()
    if price is None:
        price_text = "UNAVAILABLE"
    else:
        price_text = f"{price:.2f}"
    if open_trade:
        trade_text = (
            f"🟡 OPEN {open_trade['signal']}\n"
            f"ENTRY: {open_trade['entry']:.2f}\n"
            f"SL: {open_trade['sl']:.2f}\n"
            f"TP: {open_trade['tp']:.2f}\n"
            f"BE: {'ON' if open_trade['be_active'] else 'OFF'}"
        )
    else:
        trade_text = "⚪ NO OPEN VIRTUAL TRADE"
    return (
        "🥇 GOLD SMART V5.8\n\n"
        "🟢 Engine: READY\n"
        f"🟢 Telegram: {'READY' if BOT_TOKEN else 'NOT CONFIGURED'}\n"
        f"🟢 XAUS API: {'READY' if price is not None else 'ERROR'}\n\n"
        f"💰 XAUUSD: {price_text}\n\n"
        f"{trade_text}\n\n"
        "🛡 AUTO TRADING: OFF"
    )
def command_test():
    price = fetch_spot(force=True)
    if price is None:
        return (
            "⚠️ GOLD SMART V5.8 TEST\n\n"
            "🔴 XAUS API: ERROR\n"
            "❌ XAUUSD price unavailable"
        )
    return (
        "✅ GOLD SMART V5.8 TEST\n\n"
        "🟢 Engine: READY\n"
        "🟢 Telegram: READY\n"
        "🟢 XAUS API: READY\n"
        f"💰 XAUUSD: {price:.2f}\n\n"
        "📊 VIRTUAL TRADE TRACKING: ON\n"
        "🛡 AUTO TRADING: OFF"
    )
def command_signal():
    analysis = analyze_market()
    if analysis is None:
        return (
            "⚠️ GOLD SMART V5.8\n\n"
            "❌ Не удалось получить данные XAUUSD."
        )
    return format_analysis(analysis)
def command_stats():
    stats = load_stats()
    total = int(stats.get("total_signals", 0))
    wins = int(stats.get("wins", 0))
    losses = int(stats.get("losses", 0))
    be = int(stats.get("be", 0))
    open_count = 1 if open_trade else 0
    closed = wins + losses + be
    if closed > 0:
        win_rate = wins / closed * 100
    else:
        win_rate = 0
    if open_trade:
        open_text = (
            "\n🟡 OPEN TRADE\n"
            f"{open_trade['signal']} "
            f"{open_trade['entry']:.2f}\n"
        )
    else:
        open_text = ""
    return (
        "📊 GOLD SMART V5.8 — STATISTICS\n\n"
        f"📌 TOTAL SIGNALS: {total}\n\n"
        f"🟢 WIN: {wins}\n"
        f"🔴 LOSS: {losses}\n"
        f"⚪ BE: {be}\n"
        f"🟡 OPEN: {open_count}\n\n"
        f"🎯 CLOSED: {closed}\n"
        f"📈 WIN RATE: {win_rate:.1f}%\n"
        f"{open_text}\n"
        "━━━━━━━━━━━━━━━━\n"
        "📡 SOURCE: XAUS API\n"
        "📊 RESULT TRACKING: ON\n"
        "🛡 AUTO TRADING: OFF"
    )
def command_help():
    return (
        "🥇 GOLD SMART V5.8 — HELP\n\n"
        "/start\n"
        "Запуск и информация о боте.\n\n"
        "/status\n"
        "Проверка Engine, Telegram, XAUS API и открытой виртуальной сделки.\n\n"
        "/signal\n"
        "Ручной анализ XAUUSD прямо сейчас.\n\n"
        "/test\n"
        "Тест соединения с XAUS API и Telegram.\n\n"
        "/stats\n"
        "Статистика виртуальных сделок WIN / LOSS / BE.\n\n"
        "/help\n"
        "Список команд.\n\n"
        "🤖 Автоматические сигналы работают самостоятельно.\n"
        "📊 Результат каждого BUY/SELL отслеживается автоматически.\n"
        "🛡 Реальные сделки НЕ открываются."
    )
def process_command(command):
    command = command.lower().strip()
    if command.startswith("/start"):
        return command_start()
    if command.startswith("/status"):
        return command_status()
    if command.startswith("/signal"):
        return command_signal()
    if command.startswith("/test"):
        return command_test()
    if command.startswith("/stats"):
        return command_stats()
    if command.startswith("/help"):
        return command_help()
    return (
        "Неизвестная команда.\n"
        "Используй /help"
    )
# =========================================================
# TELEGRAM WEBHOOK
# =========================================================
@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    try:
        update = request.get_json(
            silent=True
        )
        if not update:
            return "OK", 200
        message = update.get("message")
        if not message:
            return "OK", 200
        text_message = message.get(
            "text",
            ""
        ).strip()
        if not text_message:
            return "OK", 200
        if not text_message.startswith("/"):
            return "OK", 200
        # -------------------------------------------------
        # Respond in background
        # -------------------------------------------------
        def worker():
            try:
                response = process_command(
                    text_message
                )
                telegram_send(
                    response
                )
            except Exception as e:
                print(
                    "Command worker error:",
                    e
                )
        threading.Thread(
            target=worker,
            daemon=True
        ).start()
        return "OK", 200
    except Exception as e:
        print(
            "Webhook error:",
            repr(e)
        )
        return "OK", 200
# =========================================================
# ROOT / HEALTH
# =========================================================
@app.route("/", methods=["GET"])
def root():
    return (
        "🥇 GOLD SMART V5.8 — ONLINE\n"
        "AUTO TRADING: OFF\n"
        "XAUS API: ACTIVE"
    )
@app.route("/health", methods=["GET"])
def health():
    return {
        "status": "ok",
        "version": "V5.8",
        "auto_trading": False
    }
# =========================================================
# TELEGRAM WEBHOOK SETUP
# =========================================================
def setup_webhook():
    if not BOT_TOKEN:
        print(
            "BOT_TOKEN missing — webhook not configured"
        )
        return
    if not WEBHOOK_URL:
        print(
            "WEBHOOK_URL missing — webhook not configured"
        )
        return
    url = (
        f"https://api.telegram.org/"
        f"bot{BOT_TOKEN}/setWebhook"
    )
    payload = {
        "url": WEBHOOK_URL
    }
    try:
        response = requests.post(
            url,
            json=payload,
            timeout=15
        )
        print(
            "Telegram webhook:",
            response.text
        )
    except Exception as e:
        print(
            "Webhook setup error:",
            e
        )
# =========================================================
# STARTUP
# =========================================================
def startup():
    global engine_started
    if engine_started:
        return
    engine_started = True
    print("=" * 60)
    print("🥇 GOLD SMART V5.8")
    print("Engine starting...")
    print("Source: XAUS API")
    print("AUTO TRADING: OFF")
    print("Virtual Trade Tracking: ON")
    print("=" * 60)
    setup_webhook()
    thread = threading.Thread(
        target=engine_loop,
        daemon=True
    )
    thread.start()
# =========================================================
# START
# =========================================================
startup()
if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(
            os.getenv("PORT", "10000")
        )
    )
