import os
import json
import time
import hmac
import hashlib
import tempfile
import threading
import traceback
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd
from flask import Flask, request, jsonify

# ============================================================
# GOLD SMART V6.2.0 — SMC SIGNAL BOT
# XAUS spot/intraday | H4 -> H1 -> M15 -> M5
# Virtual trade tracking only. No real orders are ever placed.
# ============================================================

APP_NAME = "GOLD SMART V6.2.0"
AUTO_TRADING = False  # Hard-disabled: this file has no order-placement code.

DEFAULT_DEPOSIT = float(os.getenv("DEFAULT_DEPOSIT", "800"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "1"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "75"))
POLL_SECONDS = max(30, int(os.getenv("POLL_SECONDS", "60")))
SIGNAL_COOLDOWN_MIN = max(1, int(os.getenv("SIGNAL_COOLDOWN_MIN", "15")))
INTRADAY_HOURS = max(24, int(os.getenv("INTRADAY_HOURS", "72")))
CLOSED_CANDLES = True

SL_DISTANCE = float(os.getenv("SL_DISTANCE", "5.0"))
TP1_DISTANCE = float(os.getenv("TP1_DISTANCE", "7.5"))
TP2_DISTANCE = float(os.getenv("TP2_DISTANCE", "15.0"))
BE_TRIGGER = float(os.getenv("BE_TRIGGER", "6.0"))
TP1_PARTIAL_PERCENT = 50.0

XAUS_SPOT_URL = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY_URL = "https://xaus.com/api/v1/intraday"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL", "https://gold-bot-q8la.onrender.com"
).strip().rstrip("/")
STATS_FILE = os.getenv("STATS_FILE", "gold_stats.json")
TRADE_FILE = os.getenv("TRADE_FILE", "gold_trade.json")
KEEPALIVE = os.getenv("KEEPALIVE", "0") == "1"
BOOT_NOTICE = os.getenv("BOOT_NOTICE", "1") == "1"

WEBHOOK_SECRET = hashlib.sha256(
    ("gold-smart:" + BOT_TOKEN).encode("utf-8")
).hexdigest()[:48]

app = Flask(__name__)
ENGINE_STARTED = False
STARTUP_DONE = False
KEEPALIVE_STARTED = False
ENGINE_LOCK = threading.Lock()
STATE_LOCK = threading.RLock()
last_cycle = None
last_cycle_attempt = None
last_error = None
last_price = None
last_data_counts = {}
last_signal_time = 0.0
active_trade = None


def utc_now():
    return datetime.now(timezone.utc)


def utc_string():
    return utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_float(value, default=None):
    try:
        if value is None or isinstance(value, bool):
            return default
        result = float(str(value).replace(",", "").strip())
        return result if np.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def atomic_json_save(filename, data):
    directory = os.path.dirname(filename) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".goldsmart_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, filename)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def load_json(filename, default):
    try:
        with open(filename, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return default


# ----------------------------- Telegram -----------------------------

def telegram_url(method):
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"


def send_telegram(message, chat_id=None):
    if not BOT_TOKEN:
        print("GOLD SMART: BOT_TOKEN missing")
        return False
    target = str(chat_id or TELEGRAM_CHAT_ID).strip()
    if not target:
        print("GOLD SMART: TELEGRAM_CHAT_ID missing")
        return False
    try:
        response = requests.post(
            telegram_url("sendMessage"),
            json={"chat_id": target, "text": str(message)},
            timeout=20,
        )
        if response.status_code != 200:
            print("GOLD SMART Telegram HTTP:", response.status_code, response.text[:500])
            return False
        return bool(response.json().get("ok", False))
    except Exception as exc:
        print("GOLD SMART Telegram error:", repr(exc))
        return False


def configure_webhook():
    if not BOT_TOKEN or not RENDER_EXTERNAL_URL:
        print("GOLD SMART: webhook skipped; token or Render URL missing")
        return False
    try:
        response = requests.post(
            telegram_url("setWebhook"),
            json={
                "url": RENDER_EXTERNAL_URL + "/telegram",
                "secret_token": WEBHOOK_SECRET,
                "drop_pending_updates": False,
            },
            timeout=20,
        )
        print("GOLD SMART setWebhook:", response.status_code, response.text[:500])
        return response.status_code == 200 and response.json().get("ok", False)
    except Exception as exc:
        print("GOLD SMART webhook error:", repr(exc))
        return False


# ----------------------------- XAUS data -----------------------------

def recursive_find_price(obj):
    if isinstance(obj, dict):
        for key in ("price", "spot_usd_oz", "close", "value", "last", "rate", "p"):
            value = safe_float(obj.get(key))
            if value is not None and value > 100:
                return value
        for key in ("xau", "gold", "data", "result", "quote"):
            if key in obj:
                found = recursive_find_price(obj[key])
                if found is not None:
                    return found
    elif isinstance(obj, list):
        for item in obj:
            found = recursive_find_price(item)
            if found is not None:
                return found
    else:
        return safe_float(obj)
    return None


def fetch_spot():
    global last_price
    response = requests.get(XAUS_SPOT_URL, params={"symbol": "xau"}, timeout=20)
    response.raise_for_status()
    price = recursive_find_price(response.json())
    if price is None or price < 100:
        raise ValueError("XAUS spot price missing or implausible")
    last_price = price
    return price


def extract_timestamp(item):
    if not isinstance(item, dict):
        return None
    for key in ("t", "timestamp", "time", "datetime", "date", "created_at"):
        value = item.get(key)
        if value is None:
            continue
        try:
            if isinstance(value, (int, float, np.integer, np.floating)):
                value = float(value)
                if value > 100_000_000_000:
                    value /= 1000
                result = pd.to_datetime(value, unit="s", utc=True, errors="coerce")
            else:
                result = pd.to_datetime(value, utc=True, errors="coerce")
            if pd.notna(result):
                return result
        except Exception:
            continue
    return None


def extract_candle_price(item):
    if not isinstance(item, dict):
        return None
    for key in ("p", "price", "close", "spot_usd_oz", "value", "last"):
        value = safe_float(item.get(key))
        if value is not None and value > 100:
            return value
    return None


def extract_intraday_items(data):
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    keys = ("points", "data", "prices", "intraday", "history", "candles", "results")
    for key in keys:
        if isinstance(data.get(key), list):
            return data[key]
    nested = data.get("xau")
    if isinstance(nested, list):
        return nested
    if isinstance(nested, dict):
        for key in keys:
            if isinstance(nested.get(key), list):
                return nested[key]
    return []


def fetch_intraday():
    response = requests.get(
        XAUS_INTRADAY_URL,
        params={"symbol": "xau", "hours": INTRADAY_HOURS, "fresh": int(time.time())},
        timeout=25,
    )
    response.raise_for_status()
    data = response.json()
    items = extract_intraday_items(data)
    rows = []
    for item in items:
        stamp, price = extract_timestamp(item), extract_candle_price(item)
        if stamp is not None and price is not None:
            rows.append({"time": stamp, "price": price})
    print(f"GOLD SMART XAUS: raw={len(items)} usable={len(rows)}")
    if not rows:
        keys = list(data.keys()) if isinstance(data, dict) else str(type(data))
        print("GOLD SMART XAUS response:", keys)
        if items:
            print("GOLD SMART first item:", repr(items[0])[:500])
        raise ValueError("XAUS intraday returned no usable points")
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df.dropna().sort_values("time").drop_duplicates("time", keep="last")
    return df.set_index("time")


# ----------------------------- Candles / indicators -----------------------------

def build_ohlc(intraday, rule):
    if intraday.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    series = intraday["price"].astype(float).sort_index()
    ohlc = series.resample(rule, label="left", closed="left").ohlc().dropna()
    if CLOSED_CANDLES and len(ohlc) > 1:
        now = pd.Timestamp.now(tz="UTC")
        duration = pd.Timedelta(rule)
        if ohlc.index[-1] + duration > now:
            ohlc = ohlc.iloc[:-1]
    return ohlc


def build_timeframes(intraday):
    return {
        "M5": build_ohlc(intraday, "5min"),
        "M15": build_ohlc(intraday, "15min"),
        "H1": build_ohlc(intraday, "1h"),
        "H4": build_ohlc(intraday, "4h"),
    }


def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(frame, period=14):
    prev = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev).abs(),
            (frame["low"] - prev).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(period, min_periods=1).mean()


def trend_analysis(frame):
    if len(frame) < 3:
        return {"side": "NEUTRAL", "score": 0}
    close = frame["close"]
    fast, slow = float(ema(close, 20).iloc[-1]), float(ema(close, 50).iloc[-1])
    current, prior = float(close.iloc[-1]), float(close.iloc[-3])
    atr_now = max(float(atr(frame).iloc[-1]), 0.01)
    strength = min(100, int(abs(current - prior) / atr_now * 35))
    if current > fast > slow and current >= prior:
        side, score = "BULLISH", max(55, strength)
    elif current < fast < slow and current <= prior:
        side, score = "BEARISH", max(55, strength)
    elif fast > slow:
        side, score = "BULLISH", max(35, strength)
    elif fast < slow:
        side, score = "BEARISH", max(35, strength)
    else:
        side, score = "NEUTRAL", strength
    return {"side": side, "score": min(100, score)}


def detect_bos(frame, lookback=12):
    if len(frame) < lookback + 2:
        return "NONE"
    previous, last = frame.iloc[-lookback-1:-1], frame.iloc[-1]
    if last["close"] > previous["high"].max():
        return "BULLISH"
    if last["close"] < previous["low"].min():
        return "BEARISH"
    return "NONE"


def detect_choch(frame, lookback=12):
    # Approximate CHoCH: close breaks the recent range against the prior EMA trend.
    if len(frame) < lookback + 5:
        return "NONE"
    prior_trend = trend_analysis(frame.iloc[:-1])["side"]
    last = frame.iloc[-1]
    previous = frame.iloc[-lookback-1:-1]
    if prior_trend == "BEARISH" and last["close"] > previous["high"].max():
        return "BULLISH CHoCH"
    if prior_trend == "BULLISH" and last["close"] < previous["low"].min():
        return "BEARISH CHoCH"
    return "NONE"


def detect_liquidity_sweep(frame, lookback=12):
    if len(frame) < lookback + 2:
        return "NONE"
    previous, last = frame.iloc[-lookback-1:-1], frame.iloc[-1]
    low_level, high_level = float(previous["low"].min()), float(previous["high"].max())
    if last["low"] < low_level and last["close"] > low_level:
        return "BULLISH SWEEP"
    if last["high"] > high_level and last["close"] < high_level:
        return "BEARISH SWEEP"
    return "NONE"


def detect_fvg(frame):
    if len(frame) < 3:
        return {"side": "NONE", "low": None, "high": None}
    a, c = frame.iloc[-3], frame.iloc[-1]
    if c["low"] > a["high"]:
        return {"side": "BULLISH", "low": float(a["high"]), "high": float(c["low"])}
    if c["high"] < a["low"]:
        return {"side": "BEARISH", "low": float(c["high"]), "high": float(a["low"])}
    return {"side": "NONE", "low": None, "high": None}


def detect_displacement(frame):
    if len(frame) < 15:
        return "NONE"
    last = frame.iloc[-1]
    body = abs(float(last["close"] - last["open"]))
    average = abs(frame["close"] - frame["open"]).iloc[-15:-1].mean()
    if not np.isfinite(average) or average <= 0 or body < average * 1.5:
        return "NONE"
    if last["close"] > last["open"]:
        return "BULLISH"
    if last["close"] < last["open"]:
        return "BEARISH"
    return "NONE"


def detect_momentum(frame):
    value = float(rsi(frame["close"]).iloc[-1])
    if value >= 57:
        return "BULLISH"
    if value <= 43:
        return "BEARISH"
    return "NEUTRAL"


def premium_discount(frame, lookback=20):
    recent = frame.tail(lookback)
    high, low = float(recent["high"].max()), float(recent["low"].min())
    midpoint, price = (high + low) / 2, float(frame["close"].iloc[-1])
    if price < midpoint:
        return "DISCOUNT"
    if price > midpoint:
        return "PREMIUM"
    return "EQUILIBRIUM"


def fibonacci_zone(frame, lookback=30):
    recent = frame.tail(lookback)
    high, low = float(recent["high"].max()), float(recent["low"].min())
    price = float(frame["close"].iloc[-1])
    span = high - low
    if span <= 0:
        return {"zone": "UNKNOWN", "fib_382": None, "fib_50": None, "fib_618": None}
    # Retracement levels measured from the recent swing high downwards.
    fib_382 = high - span * 0.382
    fib_50 = high - span * 0.500
    fib_618 = high - span * 0.618
    if fib_618 <= price <= fib_382:
        zone = "GOLDEN POCKET"
    elif price < (high + low) / 2:
        zone = "LOWER HALF"
    else:
        zone = "UPPER HALF"
    return {
        "zone": zone, "fib_382": round(fib_382, 2),
        "fib_50": round(fib_50, 2), "fib_618": round(fib_618, 2),
    }


def analyze_market(frames):
    trends = {name.lower(): trend_analysis(frames[name]) for name in ("H4", "H1", "M15", "M5")}
    m5 = frames["M5"]
    bos = detect_bos(m5)
    choch = detect_choch(m5)
    sweep = detect_liquidity_sweep(m5)
    fvg = detect_fvg(m5)
    displacement = detect_displacement(m5)
    momentum = detect_momentum(m5)
    zone = premium_discount(m5)
    fib = fibonacci_zone(m5)

    buy = sell = 0
    for name, weight in (("h4", 20), ("h1", 20), ("m15", 15), ("m5", 10)):
        if trends[name]["side"] == "BULLISH":
            buy += weight
        elif trends[name]["side"] == "BEARISH":
            sell += weight
    for value, weight in ((bos, 12), (sweep, 12), (fvg["side"], 4),
                          (displacement, 4), (momentum, 3)):
        if value in ("BULLISH", "BULLISH SWEEP"):
            buy += weight
        elif value in ("BEARISH", "BEARISH SWEEP"):
            sell += weight
    if zone == "DISCOUNT":
        buy += 5
    elif zone == "PREMIUM":
        sell += 5
    # Fibonacci is a context bonus only; never a standalone entry trigger.
    if fib["zone"] == "GOLDEN POCKET":
        if trends["h1"]["side"] == "BULLISH":
            buy += 3
        elif trends["h1"]["side"] == "BEARISH":
            sell += 3

    buy, sell = min(100, buy), min(100, sell)
    side, score = ("BUY", buy) if buy > sell else (("SELL", sell) if sell > buy else ("WAIT", max(buy, sell)))
    reasons = []
    if side in ("BUY", "SELL"):
        expected = "BULLISH" if side == "BUY" else "BEARISH"
        opposite = "BEARISH" if side == "BUY" else "BULLISH"
        expected_sweep = "BULLISH SWEEP" if side == "BUY" else "BEARISH SWEEP"
        if trends["h4"]["side"] != expected or trends["h1"]["side"] != expected:
            reasons.append("H4/H1 не согласованы")
        if trends["m15"]["side"] != expected:
            reasons.append("M15 не подтверждает направление")
        if sweep != expected_sweep:
            reasons.append("нет соответствующего снятия ликвидности")
        if bos != expected:
            reasons.append("нет подтверждённого BOS")
        if momentum == opposite:
            reasons.append("импульс против направления")
        if fvg["side"] == opposite or displacement == opposite:
            reasons.append("FVG/импульс против направления")
        if reasons:
            side = "WAIT"

    signal_type = "FULL" if side in ("BUY", "SELL") and score >= MIN_SCORE else "WAIT"
    if side == "WAIT":
        signal_type = "WAIT"
    return {
        "side": side, "score": int(score), "signal_type": signal_type,
        "setup_reasons": reasons, "h4": trends["h4"], "h1": trends["h1"],
        "m15": trends["m15"], "m5": trends["m5"], "liquidity": sweep,
        "bos": bos, "choch": choch, "fvg": fvg, "displacement": displacement,
        "momentum": momentum, "premium_discount": zone, "fibonacci": fib,
        "htf_agreement": trends["h4"]["side"] == trends["h1"]["side"],
    }


# ----------------------------- Virtual trade & statistics -----------------------------

def load_stats():
    default = {"total": 0, "wins": 0, "losses": 0, "breakeven": 0, "points": 0.0}
    stats = load_json(STATS_FILE, default.copy())
    if not isinstance(stats, dict):
        stats = default.copy()
    for key, value in default.items():
        stats.setdefault(key, value)
    return stats


def save_stats(stats):
    atomic_json_save(STATS_FILE, stats)


def save_trade():
    atomic_json_save(TRADE_FILE, active_trade)


def load_trade():
    global active_trade
    trade = load_json(TRADE_FILE, None)
    active_trade = trade if isinstance(trade, dict) else None


def close_virtual_trade(result, points):
    global active_trade
    with STATE_LOCK:
        stats = load_stats()
        stats["total"] = int(stats.get("total", 0)) + 1
        key = {"WIN": "wins", "LOSS": "losses", "BREAKEVEN": "breakeven"}.get(result, "breakeven")
        stats[key] = int(stats.get(key, 0)) + 1
        stats["points"] = round(float(stats.get("points", 0)) + float(points), 2)
        save_stats(stats)
        active_trade = None
        save_trade()


def open_virtual_trade(side, entry, analysis):
    global active_trade
    if AUTO_TRADING or side not in ("BUY", "SELL"):
        return False
    with STATE_LOCK:
        if active_trade:
            return False
        if side == "BUY":
            sl, tp1, tp2 = entry - SL_DISTANCE, entry + TP1_DISTANCE, entry + TP2_DISTANCE
        else:
            sl, tp1, tp2 = entry + SL_DISTANCE, entry - TP1_DISTANCE, entry - TP2_DISTANCE
        active_trade = {
            "side": side, "entry": round(entry, 2), "sl": round(sl, 2),
            "original_sl": round(sl, 2), "tp1": round(tp1, 2), "tp2": round(tp2, 2),
            "score": int(analysis["score"]), "opened_at": utc_string(),
            "be": False, "tp1_hit": False, "tp1_partial_percent": TP1_PARTIAL_PERCENT,
            "realized_points": 0.0, "status": "OPEN",
        }
        save_trade()
    return True


def manage_virtual_trade(price):
    global active_trade
    with STATE_LOCK:
        if not active_trade:
            return
        try:
            trade = active_trade
            side, entry = trade["side"], float(trade["entry"])
            sl, tp1, tp2 = float(trade["sl"]), float(trade["tp1"]), float(trade["tp2"])
            favorable = price - entry if side == "BUY" else entry - price

            if favorable >= BE_TRIGGER and not trade.get("be"):
                trade["sl"] = entry
                trade["be"] = True
                save_trade()
                send_telegram(
                    f"🛡 {APP_NAME}\nBREAK EVEN: {side}\nEntry: {entry:.2f}\n"
                    f"Current: {price:.2f}\nВиртуальный SL перенесён в безубыток.\nAUTO TRADING: OFF"
                )
                sl = entry

            tp1_hit = price >= tp1 if side == "BUY" else price <= tp1
            if tp1_hit and not trade.get("tp1_hit"):
                # Model a 50% partial close at TP1; remaining half is tracked to TP2/SL.
                partial_points = TP1_DISTANCE * (TP1_PARTIAL_PERCENT / 100.0)
                trade["realized_points"] = round(float(trade.get("realized_points", 0)) + partial_points, 2)
                trade["tp1_hit"] = True
                save_trade()
                send_telegram(
                    f"🎯 {APP_NAME}\nTP1 HIT — {side}\nEntry: {entry:.2f}\nTP1: {tp1:.2f}\n"
                    f"Виртуально зафиксировано {TP1_PARTIAL_PERCENT:.0f}% позиции.\nAUTO TRADING: OFF"
                )

            stop_hit = price <= sl if side == "BUY" else price >= sl
            if stop_hit:
                remaining = 1.0 - (TP1_PARTIAL_PERCENT / 100.0 if trade.get("tp1_hit") else 0.0)
                move = price - entry if side == "BUY" else entry - price
                points = float(trade.get("realized_points", 0)) + move * remaining
                result = "BREAKEVEN" if trade.get("be") and abs(points) < 0.15 else ("WIN" if points > 0 else "LOSS")
                close_virtual_trade(result, points)
                send_telegram(
                    f"📊 {APP_NAME}\n{'🟡 BREAK EVEN' if result == 'BREAKEVEN' else '📉 VIRTUAL SL HIT'}\n"
                    f"Side: {side}\nEntry: {entry:.2f}\nClose: {price:.2f}\n"
                    f"Weighted points: {points:+.2f}\nAUTO TRADING: OFF"
                )
                return

            tp2_hit = price >= tp2 if side == "BUY" else price <= tp2
            if tp2_hit:
                remaining = 1.0 - (TP1_PARTIAL_PERCENT / 100.0 if trade.get("tp1_hit") else 0.0)
                points = float(trade.get("realized_points", 0)) + TP2_DISTANCE * remaining
                close_virtual_trade("WIN", points)
                send_telegram(
                    f"🏆 {APP_NAME}\nTP2 HIT — {side}\nEntry: {entry:.2f}\nTP2: {tp2:.2f}\n"
                    f"Weighted result: {points:+.2f}\nAUTO TRADING: OFF"
                )
        except Exception as exc:
            print("GOLD SMART virtual trade error:", repr(exc))
            traceback.print_exc()


# ----------------------------- Messages / commands -----------------------------

def build_signal_message(price, analysis):
    side, score = analysis["side"], analysis["score"]
    emoji = "🟢" if side == "BUY" else ("🔴" if side == "SELL" else "⚪")
    lines = [
        f"🥇 {APP_NAME}", "", f"{emoji} SIGNAL: {analysis['signal_type']} {side}",
        f"💰 XAUUSD: {price:.2f}", "",
    ]
    for name in ("h4", "h1", "m15", "m5"):
        item = analysis[name]
        lines.append(f"📊 {name.upper()}: {item['side']} ({item['score']}/100)")
    fib = analysis.get("fibonacci", {})
    fvg = analysis.get("fvg", {})
    lines += [
        "", f"💧 Liquidity: {analysis['liquidity']}",
        f"🔨 BOS: {analysis['bos']}", f"🔀 CHoCH: {analysis['choch']}",
        f"🧩 FVG: {fvg.get('side', 'NONE')}",
        f"🚀 Displacement: {analysis['displacement']}",
        f"⚡ Momentum: {analysis['momentum']}",
        f"📦 Zone: {analysis['premium_discount']}",
        f"📐 Fibonacci: {fib.get('zone', 'UNKNOWN')}",
    ]
    if fib.get("fib_382") is not None:
        lines.append(f"Fib 38.2/50/61.8: {fib['fib_382']:.2f} / {fib['fib_50']:.2f} / {fib['fib_618']:.2f}")
    lines.append(f"📈 Score: {score}/100")
    if analysis.get("setup_reasons"):
        lines.append("⚠️ Фильтр: " + "; ".join(analysis["setup_reasons"]))
    if side in ("BUY", "SELL") and analysis["signal_type"] == "FULL":
        if side == "BUY":
            sl, tp1, tp2 = price - SL_DISTANCE, price + TP1_DISTANCE, price + TP2_DISTANCE
        else:
            sl, tp1, tp2 = price + SL_DISTANCE, price - TP1_DISTANCE, price - TP2_DISTANCE
        lines += [
            "", f"🎯 Entry: {price:.2f}", f"🛑 Virtual SL: {sl:.2f}",
            f"🎯 TP1: {tp1:.2f} (частичная фиксация {TP1_PARTIAL_PERCENT:.0f}%)",
            f"🏆 TP2: {tp2:.2f}",
            f"⚖️ Planned RR to TP2: 1:{TP2_DISTANCE / SL_DISTANCE:.1f}",
        ]
    else:
        lines += ["", "⏳ Ждём подтверждения. Не входить только по одному score."]
    lines += ["", "🤖 AUTO TRADING: OFF", "✋ Только сигналы и виртуальный учёт"]
    return "\n".join(lines)


def command_start():
    return (
        f"🥇 {APP_NAME}\n\nБот запущен.\nAUTO TRADING: OFF\n"
        "Данные: XAUS\nТаймфреймы: H4 → H1 → M15 → M5\n\n"
        "/start — меню\n/status — состояние\n/signal — текущий анализ\n"
        "/test — тест Telegram\n/stats — статистика виртуальных сделок\n/help — помощь"
    )


def command_status():
    counts = ", ".join(f"{k}:{v}" for k, v in last_data_counts.items()) or "нет данных"
    price = f"{last_price:.2f}" if last_price is not None else "unavailable"
    return (
        f"🥇 {APP_NAME}\n\nENGINE: {'ON' if ENGINE_STARTED else 'OFF'}\n"
        f"AUTO TRADING: OFF\nXAUUSD: {price}\nСвечи: {counts}\n"
        f"Последний успешный цикл: {last_cycle or 'нет'}\n"
        f"Последняя попытка: {last_cycle_attempt or 'нет'}\nОшибка: {last_error or 'нет'}\n"
        f"Виртуальная сделка активна: {'ДА' if active_trade else 'НЕТ'}"
    )


def command_stats():
    stats = load_stats()
    closed = int(stats["wins"]) + int(stats["losses"]) + int(stats["breakeven"])
    winrate = (int(stats["wins"]) / closed * 100) if closed else 0
    return (
        f"📊 {APP_NAME} STATS\n\nВсего закрыто: {stats['total']}\n"
        f"Победы: {stats['wins']}\nУбытки: {stats['losses']}\n"
        f"Безубыток: {stats['breakeven']}\nWin rate: {winrate:.1f}%\n"
        f"Взвешенные пункты: {float(stats['points']):+.2f}\n\nAUTO TRADING: OFF"
    )


def command_signal():
    analysis = market_cycle(send_signal=False, force=True)
    if not analysis or last_price is None:
        return f"⚠️ {APP_NAME}\nНе удалось получить анализ XAU/USD.\nПроверь Render Logs."
    # This command only reports analysis; it does not open a virtual trade.
    return build_signal_message(last_price, analysis)


def handle_telegram_update(update):
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    text_message = (message.get("text") or "").strip()
    if chat_id is None or not text_message:
        return
    if TELEGRAM_CHAT_ID and str(chat_id) != TELEGRAM_CHAT_ID:
        print("GOLD SMART: ignored unauthorized chat", chat_id)
        return
    command = text_message.split()[0].split("@")[0].lower()
    commands = {
        "/start": command_start, "/help": command_start, "/status": command_status,
        "/signal": command_signal, "/stats": command_stats,
        "/test": lambda: f"🧪 {APP_NAME}\nTelegram connection works.\n{utc_string()}\nAUTO TRADING: OFF",
    }
    try:
        reply = commands.get(command, lambda: "❓ Неизвестная команда. Используй /help")()
    except Exception as exc:
        print("GOLD SMART command error:", repr(exc))
        reply = "⚠️ Ошибка выполнения команды. Проверь Render Logs."
    send_telegram(reply, chat_id=chat_id)


# ----------------------------- Market cycle / engine -----------------------------

def market_cycle(send_signal=True, force=False):
    global last_cycle, last_cycle_attempt, last_error, last_data_counts, last_signal_time, active_trade
    acquired = ENGINE_LOCK.acquire(blocking=bool(force))
    if not acquired:
        return None
    try:
        last_cycle_attempt = utc_string()
        price = fetch_spot()
        intraday = fetch_intraday()
        frames = build_timeframes(intraday)
        last_data_counts = {name: len(frame) for name, frame in frames.items()}
        minimums = {"M5": 30, "M15": 20, "H1": 7, "H4": 8}
        for name, minimum in minimums.items():
            if len(frames[name]) < minimum:
                raise ValueError(f"Insufficient {name} candles: {len(frames[name])}/{minimum}")
        analysis = analyze_market(frames)
        last_cycle, last_error = utc_string(), None
        print("GOLD SMART:", analysis["side"], analysis["score"], analysis["signal_type"])
        manage_virtual_trade(price)

        if send_signal and analysis["side"] in ("BUY", "SELL") and analysis["signal_type"] == "FULL":
            elapsed = time.time() - last_signal_time
            if elapsed >= SIGNAL_COOLDOWN_MIN * 60 and not active_trade:
                if open_virtual_trade(analysis["side"], price, analysis):
                    sent = send_telegram(build_signal_message(price, analysis))
                    # Do not silently keep a trade if its opening alert failed.
                    if sent:
                        last_signal_time = time.time()
                    else:
                        with STATE_LOCK:
                            active_trade = None
                            save_trade()
                    print("GOLD SMART: FULL signal notification sent:", sent)
        return analysis
    except Exception as exc:
        last_error = f"{type(exc).__name__}: {exc}"
        print("GOLD SMART market_cycle error:", repr(exc))
        traceback.print_exc()
        return None
    finally:
        ENGINE_LOCK.release()


def engine_loop():
    global last_error
    print(f"{APP_NAME}: engine loop started")
    while True:
        try:
            market_cycle(send_signal=True, force=False)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            print("GOLD SMART engine error:", repr(exc))
            traceback.print_exc()
        time.sleep(POLL_SECONDS)


def start_background_services():
    global ENGINE_STARTED, STARTUP_DONE, KEEPALIVE_STARTED
    if STARTUP_DONE:
        return
    STARTUP_DONE = True
    load_stats()
    load_trade()
    if BOT_TOKEN:
        configure_webhook()
    ENGINE_STARTED = True
    threading.Thread(target=engine_loop, daemon=True, name="gold-engine").start()
    if KEEPALIVE and RENDER_EXTERNAL_URL and not KEEPALIVE_STARTED:
        KEEPALIVE_STARTED = True

        def ping_loop():
            while True:
                try:
                    requests.get(RENDER_EXTERNAL_URL + "/health", timeout=10)
                except Exception as exc:
                    print("GOLD SMART keepalive:", repr(exc))
                time.sleep(300)

        threading.Thread(target=ping_loop, daemon=True, name="gold-keepalive").start()
    if BOOT_NOTICE and BOT_TOKEN and TELEGRAM_CHAT_ID:
        send_telegram(
            f"🟢 {APP_NAME}\nБот запущен.\nXAUS data\nH4 → H1 → M15 → M5\n"
            "AUTO TRADING: OFF\nManual signals only"
        )


# ----------------------------- Flask routes -----------------------------

@app.get("/")
def home():
    return jsonify({
        "app": APP_NAME, "status": "online", "auto_trading": False,
        "engine": ENGINE_STARTED, "last_cycle": last_cycle,
        "last_price": last_price, "last_error": last_error,
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok", "app": APP_NAME, "engine": ENGINE_STARTED,
        "auto_trading": False, "time": utc_string(),
    })


@app.get("/status")
def http_status():
    return jsonify({
        "app": APP_NAME, "engine": ENGINE_STARTED, "auto_trading": False,
        "last_cycle": last_cycle, "last_cycle_attempt": last_cycle_attempt,
        "last_price": last_price, "last_error": last_error,
        "data_counts": last_data_counts, "active_trade": active_trade,
    })


@app.post("/telegram")
def telegram_webhook():
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(str(supplied), str(WEBHOOK_SECRET)):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    update = request.get_json(silent=True) or {}
    threading.Thread(target=handle_telegram_update, args=(update,), daemon=True).start()
    return jsonify({"ok": True})


@app.get("/telegram")
def telegram_get():
    return jsonify({"ok": True, "message": "Telegram webhook endpoint"})


# Startup on module import for Render/gunicorn. Use one worker on Render.
start_background_services()
