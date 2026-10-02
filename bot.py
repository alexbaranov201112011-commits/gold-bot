
import os
import json
import time
import threading
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd
from flask import Flask, request

APP_NAME = "GOLD SMART V5.9"

XAUS_SPOT_URL = "https://xaus.com/api/v1/spot"
XAUS_INTRADAY_URL = "https://xaus.com/api/v1/intraday"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()

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

# XAUS intraday supports at most 48 hours and samples every ~2 minutes.
INTRADAY_HOURS = 48

# With 48h of intraday history, H4 can only have ~11 closed candles.
# Therefore the old global "20 candles for every TF" rule was impossible.
MIN_CANDLES = {"M5": 30, "M15": 16, "H1": 12, "H4": 8}

app = Flask(__name__)

price_cache = {"price": None, "time": 0}
intraday_cache = {"df": None, "time": 0}
state_lock = threading.Lock()

virtual_trade = None
last_signal_time = 0
last_signal_key = None
last_early_signal_time = 0
engine_started = False


def telegram_url(method):
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"


def send_telegram(text_message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram credentials missing")
        return False
    try:
        r = requests.post(
            telegram_url("sendMessage"),
            json={"chat_id": CHAT_ID, "text": text_message},
            timeout=15,
        )
        if r.status_code != 200:
            print("Telegram error:", r.text)
            return False
        return True
    except Exception as e:
        print("Telegram exception:", repr(e))
        return False


def configure_webhook():
    if not BOT_TOKEN or not RENDER_EXTERNAL_URL:
        print("Webhook setup skipped: BOT_TOKEN or RENDER_EXTERNAL_URL missing")
        return False

    base = RENDER_EXTERNAL_URL.rstrip("/")
    webhook_url = f"{base}/telegram"
    try:
        r = requests.post(
            telegram_url("setWebhook"),
            json={"url": webhook_url, "drop_pending_updates": False},
            timeout=15,
        )
        print("Webhook:", r.text)
        return r.status_code == 200
    except Exception as e:
        print("Webhook setup error:", repr(e))
        return False


def default_stats():
    return {
        "total_signals": 0, "win": 0, "loss": 0, "be": 0, "open": 0,
        "full_signals": 0, "full_win": 0, "full_loss": 0,
        "early_signals": 0, "early_win": 0, "early_loss": 0, "trades": []
    }


def load_stats():
    if not os.path.exists(STATS_FILE):
        return default_stats()
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        base = default_stats()
        if isinstance(data, dict):
            base.update(data)
        return base
    except Exception as e:
        print("Stats load error:", repr(e))
        return default_stats()


def save_stats(data=None):
    if data is None:
        data = stats
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Stats save error:", repr(e))


stats = load_stats()


def fetch_spot(force=False):
    now = time.time()
    if not force and price_cache["price"] is not None and now - price_cache["time"] < CACHE_SECONDS:
        return price_cache["price"]
    try:
        r = requests.get(XAUS_SPOT_URL, params={"fresh": int(now)}, timeout=15)
        r.raise_for_status()
        data = r.json()
        price = None
        if isinstance(data, dict):
            xau = data.get("xau")
            if isinstance(xau, dict):
                price = xau.get("price")
            if price is None:
                price = data.get("spot_usd_oz")
        if price is None:
            raise ValueError(f"Unknown spot response: {data}")
        price = float(price)
        price_cache.update({"price": price, "time": now})
        return price
    except Exception as e:
        print("Spot error:", repr(e))
        return price_cache["price"]


def fetch_intraday(force=False):
    now = time.time()
    if not force and intraday_cache["df"] is not None and now - intraday_cache["time"] < CACHE_SECONDS:
        return intraday_cache["df"].copy()

    try:
        r = requests.get(
            XAUS_INTRADAY_URL,
            params={"symbol": "xau", "hours": INTRADAY_HOURS, "fresh": int(now)},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()

        points = None
        if isinstance(data, dict):
            for key in ("points", "data", "series"):
                if isinstance(data.get(key), list):
                    points = data[key]
                    break
        elif isinstance(data, list):
            points = data

        if not points:
            raise ValueError("No intraday data")

        rows = []
        for item in points:
            if not isinstance(item, dict):
                continue
            timestamp = item.get("t") or item.get("timestamp") or item.get("time") or item.get("date")
            price = item.get("p") or item.get("price") or item.get("close")
            if timestamp is None or price is None:
                continue
            try:
                if isinstance(timestamp, (int, float)):
                    unit = "ms" if timestamp > 100000000000 else "s"
                    dt = pd.to_datetime(timestamp, unit=unit, utc=True)
                else:
                    dt = pd.to_datetime(timestamp, utc=True)
                rows.append({"time": dt, "price": float(price)})
            except Exception:
                continue

        if len(rows) < 20:
            raise ValueError(f"Not enough raw intraday points: {len(rows)}")

        df = pd.DataFrame(rows).sort_values("time").drop_duplicates("time").set_index("time")
        # XAUS records about one point every 2 minutes. Keep the source cadence;
        # resampling directly to 5m/15m/1h/4h is more honest than fabricating 1m bars.
        ohlc = df["price"].resample("2min").ohlc().dropna()

        if CLOSED_CANDLES and len(ohlc) > 2:
            ohlc = ohlc.iloc[:-1]

        if len(ohlc) < 20:
            raise ValueError(f"Not enough normalized intraday candles: {len(ohlc)}")

        intraday_cache.update({"df": ohlc.copy(), "time": now})
        return ohlc
    except Exception as e:
        print("Intraday error:", repr(e))
        if intraday_cache["df"] is not None:
            return intraday_cache["df"].copy()
        return None


def resample_ohlc(df, timeframe):
    if df is None or len(df) < 5:
        return None
    rules = {"M5": "5min", "M15": "15min", "H1": "1h", "H4": "4h"}
    if timeframe not in rules:
        return None
    out = df.resample(rules[timeframe]).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    if CLOSED_CANDLES and len(out) > 2:
        out = out.iloc[:-1]
    return out


def add_indicators(df):
    if df is None or len(df) < 5:
        return None
    x = df.copy()
    x["ema20"] = x["close"].ewm(span=20, adjust=False).mean()
    x["ema50"] = x["close"].ewm(span=50, adjust=False).mean()
    x["ema200"] = x["close"].ewm(span=200, adjust=False).mean()
    delta = x["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(14, min_periods=1).mean()
    avg_loss = loss.rolling(14, min_periods=1).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    x["rsi"] = (100 - 100 / (1 + rs)).fillna(50)
    return x


def structure_direction(df, lookback=30):
    if df is None or len(df) < 7:
        return "MIXED"
    x = df.tail(lookback)
    highs, lows = [], []
    h, l = x["high"].values, x["low"].values
    if len(x) < 5:
        return "MIXED"
    for i in range(2, len(x) - 2):
        if h[i] > h[i-1] and h[i] > h[i-2] and h[i] > h[i+1] and h[i] > h[i+2]:
            highs.append(h[i])
        if l[i] < l[i-1] and l[i] < l[i-2] and l[i] < l[i+1] and l[i] < l[i+2]:
            lows.append(l[i])
    if len(highs) < 2 or len(lows) < 2:
        return "MIXED"
    hh, ph = highs[-1], highs[-2]
    ll, pl = lows[-1], lows[-2]
    if hh > ph and ll > pl:
        return "BULLISH"
    if hh < ph and ll < pl:
        return "BEARISH"
    return "MIXED"


def momentum_from_df(df):
    if df is None or len(df) < 6:
        return "MIXED"
    a, b = float(df["close"].iloc[-1]), float(df["close"].iloc[-5])
    return "BULLISH" if a > b else "BEARISH" if a < b else "MIXED"


def detect_bos(df, lookback=8):
    if df is None or len(df) < lookback + 2:
        return "NONE"
    recent, last = df.iloc[-lookback-1:-1], df.iloc[-1]
    if last["close"] > recent["high"].max():
        return "BULLISH BOS"
    if last["close"] < recent["low"].min():
        return "BEARISH BOS"
    return "NONE"


def detect_liquidity(df, lookback=12):
    if df is None or len(df) < lookback + 2:
        return "NONE"
    previous, last = df.iloc[-lookback-1:-1], df.iloc[-1]
    ph, pl = previous["high"].max(), previous["low"].min()
    if last["high"] > ph and last["close"] < ph:
        return "BSL SWEEP"
    if last["low"] < pl and last["close"] > pl:
        return "SSL SWEEP"
    return "NONE"


def detect_fvg(df):
    if df is None or len(df) < 5:
        return "NONE"
    a, c = df.iloc[-3], df.iloc[-1]
    if c["low"] > a["high"]:
        return "BULLISH FVG"
    if c["high"] < a["low"]:
        return "BEARISH FVG"
    return "NONE"


def detect_displacement(df):
    if df is None or len(df) < 12:
        return "NONE"
    bodies = (df["close"] - df["open"]).abs()
    avg = bodies.iloc[-11:-1].mean()
    body = bodies.iloc[-1]
    if avg > 0 and body >= avg * 1.8:
        return "BULLISH" if df["close"].iloc[-1] > df["open"].iloc[-1] else "BEARISH"
    return "NONE"


def detect_zone(df):
    if df is None or len(df) < 10:
        return "MID"
    recent = df.tail(20)
    hi, lo, price = recent["high"].max(), recent["low"].min(), df["close"].iloc[-1]
    mid = (hi + lo) / 2
    return "DISCOUNT" if price < mid else "PREMIUM" if price > mid else "MID"


def trend_score(df):
    if df is None or len(df) < 7:
        return {"direction": "MIXED", "score": 50, "structure": "MIXED", "momentum": "MIXED", "bos": "NONE"}

    x = add_indicators(df)
    if x is None:
        return {"direction": "MIXED", "score": 50, "structure": "MIXED", "momentum": "MIXED", "bos": "NONE"}

    last = x.iloc[-1]
    structure, momentum, bos = structure_direction(x), momentum_from_df(x), detect_bos(x)
    bull = bear = 0

    if structure == "BULLISH": bull += 40
    elif structure == "BEARISH": bear += 40

    close, e20, e50, e200 = map(float, (last["close"], last["ema20"], last["ema50"], last["ema200"]))
    if close > e20 > e50 > e200: bull += 25
    elif close < e20 < e50 < e200: bear += 25
    elif close > e50 and e20 > e50: bull += 15
    elif close < e50 and e20 < e50: bear += 15

    if momentum == "BULLISH": bull += 15
    elif momentum == "BEARISH": bear += 15
    if bos == "BULLISH BOS": bull += 20
    elif bos == "BEARISH BOS": bear += 20

    if bull == bear:
        return {"direction": "MIXED", "score": 50, "structure": structure, "momentum": momentum, "bos": bos}

    side = bull if bull > bear else bear
    other = bear if bull > bear else bull
    score = max(0, min(100, round(50 + (side - other) / 2)))
    direction = "BULLISH" if bull > bear and score >= 60 else "BEARISH" if bear > bull and score >= 60 else "MIXED"
    return {"direction": direction, "score": score, "structure": structure, "momentum": momentum, "bos": bos}


def build_market_data():
    raw = fetch_intraday()
    if raw is None:
        return None
    result = {}
    for tf in ("M5", "M15", "H1", "H4"):
        tf_df = resample_ohlc(raw, tf)
        minimum = MIN_CANDLES[tf]
        if tf_df is None or len(tf_df) < minimum:
            print(f"Market data: {tf} has {0 if tf_df is None else len(tf_df)} candles; need {minimum}")
            return None
        result[tf] = add_indicators(tf_df)
    return result


def score_signal(market):
    h4, h1, m15, m5 = (market[k] for k in ("H4", "H1", "M15", "M5"))
    trend_data = {tf: trend_score(market[tf]) for tf in ("H4", "H1", "M15", "M5")}
    buy_score = sell_score = 0.0

    weights = {"H4": 20, "H1": 15, "M15": 15, "M5": 10}
    for tf, data in trend_data.items():
        contribution = data["score"] / 100 * weights[tf]
        if data["direction"] == "BULLISH": buy_score += contribution
        elif data["direction"] == "BEARISH": sell_score += contribution

    liquidity = detect_liquidity(m5)
    bos = detect_bos(m5)
    fvg = detect_fvg(m5)
    displacement = detect_displacement(m5)
    momentum = momentum_from_df(m5)
    zone = detect_zone(m5)
    rsi = float(m5["rsi"].iloc[-1])

    if liquidity == "SSL SWEEP": buy_score += 10
    elif liquidity == "BSL SWEEP": sell_score += 10
    if bos == "BULLISH BOS": buy_score += 15
    elif bos == "BEARISH BOS": sell_score += 15
    if fvg == "BULLISH FVG": buy_score += 5
    elif fvg == "BEARISH FVG": sell_score += 5
    if displacement == "BULLISH": buy_score += 10
    elif displacement == "BEARISH": sell_score += 10
    if momentum == "BULLISH": buy_score += 5
    elif momentum == "BEARISH": sell_score += 5
    if rsi < 35: buy_score += 5
    elif rsi > 65: sell_score += 5
    if zone == "DISCOUNT": buy_score += 3
    elif zone == "PREMIUM": sell_score += 3

    h4d, h1d = trend_data["H4"]["direction"], trend_data["H1"]["direction"]
    higher_bias = "BULLISH" if h4d == "BULLISH" and h1d == "BULLISH" else "BEARISH" if h4d == "BEARISH" and h1d == "BEARISH" else "MIXED"

    if buy_score > sell_score:
        dominant, dominant_score = "BUY", buy_score
    elif sell_score > buy_score:
        dominant, dominant_score = "SELL", sell_score
    else:
        dominant, dominant_score = "NONE", 0

    signal = "WAIT"
    if dominant == "BUY" and buy_score >= MIN_SCORE and higher_bias == "BULLISH" and (liquidity == "SSL SWEEP" or bos == "BULLISH BOS" or displacement == "BULLISH"):
        signal = "BUY"
    elif dominant == "SELL" and sell_score >= MIN_SCORE and higher_bias == "BEARISH" and (liquidity == "BSL SWEEP" or bos == "BEARISH BOS" or displacement == "BEARISH"):
        signal = "SELL"
    elif dominant == "BUY" and buy_score >= EARLY_SCORE and higher_bias != "BEARISH" and (liquidity == "SSL SWEEP" or bos == "BULLISH BOS"):
        signal = "EARLY BUY"
    elif dominant == "SELL" and sell_score >= EARLY_SCORE and higher_bias != "BULLISH" and (liquidity == "BSL SWEEP" or bos == "BEARISH BOS"):
        signal = "EARLY SELL"

    return {
        "signal": signal, "buy_score": round(buy_score, 1), "sell_score": round(sell_score, 1),
        "dominant": dominant, "dominant_score": round(dominant_score, 1),
        "higher_bias": higher_bias, "trend_data": trend_data,
        "liquidity": liquidity, "bos": bos, "fvg": fvg,
        "displacement": displacement, "momentum": momentum, "zone": zone, "rsi": round(rsi, 1)
    }


def trend_line(tf, data):
    return f"📊 {tf}: {data['direction']} ({data['score']}/100)"


def build_signal_message(price, analysis):
    td, signal = analysis["trend_data"], analysis["signal"]
    emoji = {"BUY":"🟢","SELL":"🔴","EARLY BUY":"🟡","EARLY SELL":"🟠"}.get(signal, "⚪")
    return f"""🥇 {APP_NAME}

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

🛡 AUTO TRADING: {"ON" if AUTO_TRADING else "OFF"}
📉 RISK: {RISK_PERCENT:.1f}%

💼 DEMO DEPOSIT: ${DEFAULT_DEPOSIT:.2f}"""


def open_virtual_trade(direction, price, signal_type):
    global virtual_trade
    if direction not in ("BUY","SELL") or signal_type not in ("BUY","SELL") or virtual_trade is not None:
        return
    sl, tp = (price - SL_DISTANCE, price + TP_DISTANCE) if direction == "BUY" else (price + SL_DISTANCE, price - TP_DISTANCE)
    virtual_trade = {
        "direction": direction, "signal_type": signal_type,
        "entry": round(price,2), "sl": round(sl,2), "tp": round(tp,2),
        "original_sl": round(sl,2), "be": False,
        "opened_at": datetime.now(timezone.utc).isoformat()
    }
    stats["open"] = 1
    save_stats()


def close_virtual_trade(result):
    global virtual_trade
    if virtual_trade is None:
        return
    direction, signal_type = virtual_trade["direction"], virtual_trade["signal_type"]
    stats["open"] = 0
    if result == "WIN": stats["win"] += 1
    elif result == "LOSS": stats["loss"] += 1
    elif result == "BE": stats["be"] += 1
    stats["full_signals"] += 1
    if result == "WIN": stats["full_win"] += 1
    elif result == "LOSS": stats["full_loss"] += 1
    stats["trades"].append({
        "direction": direction, "signal_type": signal_type,
        "entry": virtual_trade["entry"], "sl": virtual_trade["sl"],
        "tp": virtual_trade["tp"], "result": result,
        "closed_at": datetime.now(timezone.utc).isoformat()
    })
    virtual_trade = None
    save_stats()


def update_virtual_trade(price):
    global virtual_trade
    if virtual_trade is None:
        return None
    direction, entry, tp = virtual_trade["direction"], virtual_trade["entry"], virtual_trade["tp"]
    if direction == "BUY":
        move = price - entry
        if not virtual_trade["be"] and move >= BE_TRIGGER:
            virtual_trade["sl"], virtual_trade["be"] = entry, True
            save_stats()
            send_telegram(f"🛡 {APP_NAME}\n\n🔒 BREAK EVEN\n\n🟢 BUY\n\n💰 Entry: {entry:.2f}\n📍 Current: {price:.2f}\n➡️ SL moved to BE: {entry:.2f}\n\nAUTO TRADING: OFF")
        if price <= virtual_trade["sl"]:
            result = "BE" if virtual_trade["be"] else "LOSS"
            close_virtual_trade(result)
            return result
        if price >= tp:
            close_virtual_trade("WIN")
            return "WIN"
    else:
        move = entry - price
        if not virtual_trade["be"] and move >= BE_TRIGGER:
            virtual_trade["sl"], virtual_trade["be"] = entry, True
            save_stats()
            send_telegram(f"🛡 {APP_NAME}\n\n🔒 BREAK EVEN\n\n🔴 SELL\n\n💰 Entry: {entry:.2f}\n📍 Current: {price:.2f}\n➡️ SL moved to BE: {entry:.2f}\n\nAUTO TRADING: OFF")
        if price >= virtual_trade["sl"]:
            result = "BE" if virtual_trade["be"] else "LOSS"
            close_virtual_trade(result)
            return result
        if price <= tp:
            close_virtual_trade("WIN")
            return "WIN"
    return None


def stats_message():
    total = stats.get("total_signals",0)
    win, loss, be = stats.get("win",0), stats.get("loss",0), stats.get("be",0)
    closed = win + loss + be
    wr = win / closed * 100 if closed else 0
    full, fw, fl = stats.get("full_signals",0), stats.get("full_win",0), stats.get("full_loss",0)
    early, ew, el = stats.get("early_signals",0), stats.get("early_win",0), stats.get("early_loss",0)
    fwr = fw / (fw+fl) * 100 if fw+fl else 0
    ewr = ew / (ew+el) * 100 if ew+el else 0
    vt = "⚪ No virtual trade" if not virtual_trade else f"""🟡 OPEN
{virtual_trade["direction"]}
Entry: {virtual_trade["entry"]:.2f}
SL: {virtual_trade["sl"]:.2f}
TP: {virtual_trade["tp"]:.2f}
BE: {"ON" if virtual_trade["be"] else "OFF"}"""
    return f"""📊 {APP_NAME} — STATISTICS

━━━━━━━━━━━━━━━━━━

📌 TOTAL SIGNALS: {total}
🟢 WIN: {win}
🔴 LOSS: {loss}
⚪ BE: {be}
🟡 OPEN: {stats.get("open",0)}

━━━━━━━━━━━━━━━━━━

🎯 CLOSED: {closed}
📈 WIN RATE: {wr:.1f}%

━━━━━━━━━━━━━━━━━━

🔥 FULL SIGNALS: {full}
🟢 FULL WIN: {fw}
🔴 FULL LOSS: {fl}
🎯 FULL WIN RATE: {fwr:.1f}%

━━━━━━━━━━━━━━━━━━

🟡 EARLY SIGNALS: {early}
🟢 EARLY WIN: {ew}
🔴 EARLY LOSS: {el}
🎯 EARLY WIN RATE: {ewr:.1f}%

━━━━━━━━━━━━━━━━━━

{vt}

━━━━━━━━━━━━━━━━━━

🛡 AUTO TRADING: OFF
📉 RISK: {RISK_PERCENT:.1f}%"""


def status_message():
    price = fetch_spot()
    p = "N/A" if price is None else f"{price:.2f}"
    vt = "⚪ No virtual trade"
    if virtual_trade:
        vt = f"""🟡 Virtual trade:
{virtual_trade["direction"]}
Entry: {virtual_trade["entry"]:.2f}
SL: {virtual_trade["sl"]:.2f}
TP: {virtual_trade["tp"]:.2f}
BE: {"ON" if virtual_trade["be"] else "OFF"}"""
    return f"""🥇 {APP_NAME}

🟢 Engine: READY
🟢 Telegram: READY
🟢 XAUS API: READY

💰 XAUUSD: {p}

{vt}

━━━━━━━━━━━━━━━━━━

🛡 AUTO TRADING: OFF
📉 Risk: {RISK_PERCENT:.1f}%
🎯 Min Score: {MIN_SCORE}
🕯 Closed candles: {"ON" if CLOSED_CANDLES else "OFF"}
⏱ Poll: {POLL_SECONDS}s
⏳ Cooldown: {COOLDOWN_MIN} min"""


def test_message():
    price = fetch_spot(force=True)
    return f"""🧪 {APP_NAME} TEST

🟢 Engine: READY
🟢 Telegram: READY
{"🟢" if price is not None else "🔴"} XAUS API: {"READY" if price is not None else "UNAVAILABLE"}

💰 XAUUSD: {"N/A" if price is None else f"{price:.2f}"}

🛡 AUTO TRADING: OFF
📉 Risk: {RISK_PERCENT:.1f}%"""


def generate_signal():
    price = fetch_spot(force=True)
    market = build_market_data()
    if price is None or market is None:
        return f"""⚠️ {APP_NAME}

❌ Не удалось получить достаточно данных XAU/USD.

В логах Render будет указано, на каком таймфрейме не хватает свечей."""
    return build_signal_message(price, score_signal(market))


def handle_command(command):
    command = command.strip().lower().split()[0] if command.strip() else ""
    if command == "/start":
        return f"""🥇 {APP_NAME}

🟢 GOLD SMART запущен.

H4 → H1 → M15 → M5

🧠 Smart Trend
💧 Liquidity
🔨 BOS
🧩 FVG
💥 Displacement
📈 Momentum
📊 RSI
🎯 Premium / Discount

Команды:
/signal
/status
/stats
/test
/help

🛡 AUTO TRADING: OFF
📉 RISK: 1%"""
    if command == "/help":
        return f"""🥇 {APP_NAME}

📌 КОМАНДЫ

/signal — текущий анализ XAU/USD
/status — состояние бота
/stats — статистика
/test — Telegram + XAUS
/help — помощь

🧭 H4 + H1 = Higher TF Bias
🎯 M5 = основной trigger

🛡 AUTO TRADING: OFF
📉 RISK: 1%"""
    if command == "/status": return status_message()
    if command == "/stats": return stats_message()
    if command == "/test": return test_message()
    if command == "/signal": return generate_signal()
    return "🥇 GOLD SMART V5.9\n\nНеизвестная команда.\nИспользуй /help"


def engine_loop():
    global last_signal_time, last_signal_key, last_early_signal_time
    print(f"{APP_NAME} engine started")
    while True:
        try:
            price = fetch_spot()
            if price is None:
                print("Price unavailable")
                time.sleep(POLL_SECONDS)
                continue

            if virtual_trade is not None:
                result = update_virtual_trade(price)
                if result:
                    print("Virtual trade closed:", result)

            market = build_market_data()
            if market is None:
                time.sleep(POLL_SECONDS)
                continue

            analysis = score_signal(market)
            signal = analysis["signal"]
            print(f"[{datetime.now(timezone.utc)}] {signal} | XAUUSD {price:.2f} | BUY {analysis['buy_score']} | SELL {analysis['sell_score']} | BIAS {analysis['higher_bias']}")

            now = time.time()
            cooldown_ok = now - last_signal_time >= COOLDOWN_MIN * 60

            # A signal is identified by side + current M5 setup. This prevents
            # the same Early setup from being sent again every 15 minutes.
            setup_key = (
                signal, analysis["liquidity"], analysis["bos"],
                analysis["fvg"], analysis["displacement"], analysis["higher_bias"]
            )

            if signal in ("BUY","SELL") and cooldown_ok and virtual_trade is None:
                if send_telegram(build_signal_message(price, analysis)):
                    last_signal_time = now
                    last_signal_key = setup_key
                    last_early_signal_time = 0
                    stats["total_signals"] += 1
                    stats["full_signals"] += 1
                    save_stats()
                    open_virtual_trade(signal, price, signal)

                    if virtual_trade:
                        send_telegram(f"""📌 VIRTUAL TRADE OPENED

{"🟢 BUY" if signal == "BUY" else "🔴 SELL"}

💰 Entry: {price:.2f}
🛑 SL: {virtual_trade["sl"]:.2f}
🎯 TP: {virtual_trade["tp"]:.2f}
📐 RR: 1:3
📉 Risk: {RISK_PERCENT:.1f}%
🛡 AUTO TRADING: OFF""")

            elif signal in ("EARLY BUY","EARLY SELL") and cooldown_ok:
                # Early alerts are informational and do not open a virtual trade.
                # Re-send only after the setup has changed or 60 minutes have passed.
                early_repeat_ok = (
                    setup_key != last_signal_key
                    or now - last_early_signal_time >= 60 * 60
                )
                if early_repeat_ok and send_telegram(build_signal_message(price, analysis)):
                    last_signal_time = now
                    last_signal_key = setup_key
                    last_early_signal_time = now
                    stats["total_signals"] += 1
                    stats["early_signals"] += 1
                    save_stats()

            elif signal == "WAIT":
                # Once the market leaves the previous setup, allow a fresh
                # Early signal immediately when a new setup appears.
                if last_signal_key is not None:
                    wait_key = (
                        signal, analysis["liquidity"], analysis["bos"],
                        analysis["fvg"], analysis["displacement"], analysis["higher_bias"]
                    )
                    if wait_key != last_signal_key:
                        last_early_signal_time = 0

        except Exception as e:
            print("ENGINE ERROR:", repr(e))
        time.sleep(POLL_SECONDS)


@app.route("/", methods=["GET"])
def home():
    return {"app": APP_NAME, "status": "READY", "auto_trading": AUTO_TRADING, "risk_percent": RISK_PERCENT}


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "app": APP_NAME, "time": datetime.now(timezone.utc).isoformat()}


@app.route("/test", methods=["GET"])
def http_test():
    return test_message()


@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    try:
        update = request.get_json(silent=True) or {}
        message = update.get("message") or {}
        text_message = message.get("text", "")
        if not text_message:
            return "OK"
        incoming_chat_id = str((message.get("chat") or {}).get("id", ""))
        if CHAT_ID and incoming_chat_id != CHAT_ID:
            return "OK"
        response = handle_command(text_message)
        send_telegram(response)
        return "OK"
    except Exception as e:
        print("Webhook error:", repr(e))
        return "OK"


def start_engine():
    global engine_started
    if engine_started:
        return
    engine_started = True
    threading.Thread(target=engine_loop, daemon=True, name="gold-smart-engine").start()


# IMPORTANT: Gunicorn imports bot:app, so __main__ is NOT executed.
# Start the engine and configure Telegram when the module is imported.
start_engine()
configure_webhook()

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
