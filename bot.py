import os
import json
import time
import threading
from datetime import datetime, timezone
import requests
import pandas as pd
import numpy as np
from flask import Flask, request, jsonify
# ============================================================
# 🥇 GOLD SMART V5.7
# XAUUSD SMART SMC SIGNAL BOT
#
# DATA SOURCE:
# XAUS API
#
# AUTO TRADING = OFF
# RISK = 1%
# CLOSED CANDLES = ON
#
# Telegram:
# /start
# /status
# /signal
# /test
# /stats
# /help
# ============================================================
# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
PORT = int(os.getenv("PORT", "10000"))
XAUS_BASE = "https://xaus.com/api/v1"
RISK_PERCENT = 1.0
DEFAULT_DEPOSIT = 800.0
MIN_LOT = 0.01
MAX_LOT = 0.02
MIN_SCORE = 70
EARLY_SCORE = 60
TARGET_RR = 3.0
POLL_SECONDS = 60
COOLDOWN_MIN = 15
# Maximum age of chart data in seconds.
MAX_DATA_AGE = 180
# Cache
CACHE_SECONDS = 45
# Statistics file
STATS_FILE = "gold_stats.json"
# Auto trading intentionally disabled
AUTO_TRADING = False
# ============================================================
# FLASK
# ============================================================
app = Flask(__name__)
# ============================================================
# GLOBAL STATE
# ============================================================
state_lock = threading.Lock()
last_analysis = None
last_signal = None
last_signal_time = 0
cache = {}
stats = {
    "total_signals": 0,
    "win": 0,
    "loss": 0,
    "be": 0,
    "open": 0,
    "early": 0,
    "full": 0,
    "last_signal": None,
    "updated": None
}
# ============================================================
# LOGGING
# ============================================================
def log(message):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {message}", flush=True)
# ============================================================
# STATS
# ============================================================
def load_stats():
    global stats
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                stats.update(saved)
    except Exception as e:
        log(f"Stats load error: {e}")
def save_stats():
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"Stats save error: {e}")
# ============================================================
# TELEGRAM
# ============================================================
def telegram_url(method):
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
def send_telegram(text_message, chat_id=None):
    if not BOT_TOKEN:
        log("Telegram token missing")
        return False
    target = chat_id or CHAT_ID
    if not target:
        log("Telegram chat id missing")
        return False
    try:
        response = requests.post(
            telegram_url("sendMessage"),
            json={
                "chat_id": target,
                "text": text_message,
                "disable_web_page_preview": True
            },
            timeout=10
        )
        if response.status_code != 200:
            log(
                f"Telegram error {response.status_code}: "
                f"{response.text[:300]}"
            )
            return False
        return True
    except Exception as e:
        log(f"Telegram exception: {e}")
        return False
# ============================================================
# TELEGRAM COMMANDS
# ============================================================
def help_message():
    return (
        "🥇 GOLD SMART V5.7\n\n"
        "📌 COMMANDS\n\n"
        "/start — запуск\n"
        "/status — состояние системы\n"
        "/signal — текущий анализ XAUUSD\n"
        "/test — тест Telegram/API\n"
        "/stats — статистика сигналов\n"
        "/help — команды\n\n"
        "🛡 AUTO TRADING: OFF\n"
        "⚠️ Сигналы не являются исполнением сделок."
    )
def status_message():
    data = get_cached_analysis()
    if not data:
        return (
            "🥇 GOLD SMART V5.7\n\n"
            "🟡 STATUS: READY\n"
            "🟠 DATA: WAIT\n\n"
            "Источник XAUUSD: XAUS API\n"
            "AUTO TRADING: OFF"
        )
    price = data.get("price")
    return (
        "🥇 GOLD SMART V5.7\n\n"
        "🟢 ENGINE: READY\n"
        "🟢 TELEGRAM: READY\n"
        "🟢 XAUS API: READY\n\n"
        f"💰 XAUUSD: {price:.2f}\n"
        f"📊 SIGNAL: {data.get('signal', 'WAIT')}\n"
        f"📈 BUY SCORE: {data.get('buy_score', 0)}\n"
        f"📉 SELL SCORE: {data.get('sell_score', 0)}\n\n"
        "🛡 AUTO TRADING: OFF\n"
        "⚠️ CLOSED CANDLES: ON"
    )
def test_message():
    try:
        price_data = fetch_spot()
        if not price_data:
            return (
                "⚠️ GOLD SMART V5.7 TEST\n\n"
                "🟢 Telegram: READY\n"
                "🔴 XAUS API: ERROR"
            )
        price = price_data["price"]
        return (
            "✅ GOLD SMART V5.7 TEST\n\n"
            "🟢 Engine: READY\n"
            "🟢 Telegram: READY\n"
            "🟢 XAUS API: READY\n"
            f"💰 XAUUSD: {price:.2f}\n\n"
            "🛡 AUTO TRADING: OFF"
        )
    except Exception as e:
        return (
            "⚠️ GOLD SMART V5.7 TEST\n\n"
            f"❌ ERROR: {str(e)[:250]}"
        )
def stats_message():
    with state_lock:
        total = int(stats.get("total_signals", 0))
        win = int(stats.get("win", 0))
        loss = int(stats.get("loss", 0))
        be = int(stats.get("be", 0))
        early = int(stats.get("early", 0))
        full = int(stats.get("full", 0))
        opened = int(stats.get("open", 0))
    closed = win + loss + be
    if closed > 0:
        win_rate = win / closed * 100
    else:
        win_rate = 0
    return (
        "📊 GOLD SMART V5.7 — STATISTICS\n\n"
        f"📌 TOTAL SIGNALS: {total}\n\n"
        f"🟢 WIN: {win}\n"
        f"🔴 LOSS: {loss}\n"
        f"⚪ BE: {be}\n"
        f"🟡 OPEN: {opened}\n\n"
        f"🎯 CLOSED: {closed}\n"
        f"📈 WIN RATE: {win_rate:.1f}%\n\n"
        f"🔥 FULL SIGNALS: {full}\n"
        f"🟡 EARLY SIGNALS: {early}"
    )
def process_telegram_update(update):
    try:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        text = (message.get("text") or "").strip()
        chat_id = str(chat.get("id", ""))
        if not text:
            return
        command = text.split()[0].lower()
        if command.startswith("/start"):
            send_telegram(
                "🥇 GOLD SMART V5.7\n\n"
                "Бот запущен.\n\n"
                "Используй /help для списка команд.",
                chat_id
            )
        elif command.startswith("/help"):
            send_telegram(help_message(), chat_id)
        elif command.startswith("/status"):
            send_telegram(status_message(), chat_id)
        elif command.startswith("/test"):
            send_telegram(test_message(), chat_id)
        elif command.startswith("/stats"):
            send_telegram(stats_message(), chat_id)
        elif command.startswith("/signal"):
            send_telegram(
                "⏳ GOLD SMART V5.7\n\n"
                "Получаю данные XAUUSD...",
                chat_id
            )
            result = analyze_market()
            send_telegram(format_signal(result), chat_id)
        else:
            send_telegram(
                "Неизвестная команда.\n\nИспользуй /help",
                chat_id
            )
    except Exception as e:
        log(f"Telegram update error: {e}")
# ============================================================
# WEBHOOK
# ============================================================
@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "status": "ok",
        "bot": "GOLD SMART V5.7",
        "source": "XAUS",
        "auto_trading": False
    })
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "bot": "GOLD SMART V5.7"
    })
@app.route("/telegram", methods=["POST"])
def telegram_webhook():
    update = request.get_json(silent=True) or {}
    # Respond immediately.
    threading.Thread(
        target=process_telegram_update,
        args=(update,),
        daemon=True
    ).start()
    return jsonify({"ok": True})
# ============================================================
# XAUS API
# ============================================================
def fetch_json(url, params=None, timeout=12):
    try:
        response = requests.get(
            url,
            params=params,
            timeout=timeout,
            headers={
                "User-Agent": "GOLD-SMART-V5.7"
            }
        )
        if response.status_code != 200:
            log(
                f"XAUS HTTP {response.status_code}: "
                f"{response.text[:250]}"
            )
            return None
        return response.json()
    except Exception as e:
        log(f"XAUS request error: {e}")
        return None
def fetch_spot():
    data = fetch_json(
        f"{XAUS_BASE}/spot",
        params={
            "currency": "USD",
            "unit": "oz",
            "compact": "1"
        }
    )
    if not data:
        return None
    price = data.get("spot_usd_oz")
    if price is None:
        return None
    data_state = data.get("data_state") or {}
    status = data_state.get("status")
    # Do not use unavailable data.
    if status == "unavailable":
        return None
    return {
        "price": float(price),
        "updated_at": data.get("updated_at"),
        "price_as_of": data.get("price_as_of"),
        "data_state": data_state,
        "stale": bool(data.get("stale", False))
    }
# ============================================================
# CHART DATA
# ============================================================
def get_chart(interval, range_value):
    cache_key = f"{interval}_{range_value}"
    now = time.time()
    cached = cache.get(cache_key)
    if cached:
        age = now - cached["time"]
        if age < CACHE_SECONDS:
            return cached["data"]
    data = fetch_json(
        f"{XAUS_BASE}/chart",
        params={
            "symbol": "xau",
            "range": range_value,
            "interval": interval
        }
    )
    if not data:
        return None
    cache[cache_key] = {
        "time": now,
        "data": data
    }
    return data
def parse_chart(data):
    if not data:
        return None
    points = (
        data.get("points")
        or data.get("data")
        or data.get("chart")
        or []
    )
    if not isinstance(points, list) or len(points) < 30:
        return None
    rows = []
    for p in points:
        try:
            if isinstance(p, dict):
                timestamp = (
                    p.get("t")
                    or p.get("time")
                    or p.get("timestamp")
                )
                o = p.get("o")
                h = p.get("h")
                l = p.get("l")
                c = p.get("c")
                v = p.get("v", 0)
            elif isinstance(p, (list, tuple)) and len(p) >= 5:
                timestamp = p[0]
                o, h, l, c, v = p[1:6]
            else:
                continue
            if timestamp is None:
                continue
            if o is None or h is None or l is None or c is None:
                continue
            rows.append({
                "time": pd.to_datetime(
                    timestamp,
                    unit="s",
                    utc=True
                ),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
                "volume": float(v or 0)
            })
        except Exception:
            continue
    if len(rows) < 30:
        return None
    df = pd.DataFrame(rows)
    df = df.drop_duplicates("time")
    df = df.sort_values("time")
    df = df.set_index("time")
    return df
# ============================================================
# CLOSED CANDLES
# ============================================================
def remove_current_candle(df, minutes):
    if df is None or len(df) < 3:
        return df
    now = pd.Timestamp.now(tz="UTC")
    last_time = df.index[-1]
    # Conservative: discard the last candle.
    # This avoids using a candle that may still be forming.
    if last_time + pd.Timedelta(minutes=minutes) > now:
        return df.iloc[:-1].copy()
    return df.iloc[:-1].copy()
# ============================================================
# RESAMPLE H4
# ============================================================
def resample_h4(df):
    if df is None or len(df) < 20:
        return None
    result = df.resample("4h", origin="epoch").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    })
    result = result.dropna()
    return result
# ============================================================
# INDICATORS
# ============================================================
def ema(series, period):
    return series.ewm(
        span=period,
        adjust=False
    ).mean()
def rsi(series, period=14):
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
    result = 100 - (100 / (1 + rs))
    return result.fillna(50)
# ============================================================
# TREND
# ============================================================
def get_trend(df):
    if df is None or len(df) < 210:
        return "MIXED"
    close = df["close"]
    e50 = ema(close, 50).iloc[-1]
    e200 = ema(close, 200).iloc[-1]
    price = close.iloc[-1]
    if price > e50 > e200:
        return "BULLISH"
    if price < e50 < e200:
        return "BEARISH"
    return "MIXED"
# ============================================================
# BOS
# ============================================================
def get_bos(df):
    if df is None or len(df) < 20:
        return "NONE"
    previous = df.iloc[-13:-1]
    current = df.iloc[-1]
    prev_high = previous["high"].max()
    prev_low = previous["low"].min()
    if current["close"] > prev_high:
        return "BULLISH BOS"
    if current["close"] < prev_low:
        return "BEARISH BOS"
    return "NONE"
# ============================================================
# LIQUIDITY SWEEP
# ============================================================
def get_liquidity(df):
    if df is None or len(df) < 15:
        return "NONE"
    previous = df.iloc[-11:-1]
    current = df.iloc[-1]
    recent_high = previous["high"].max()
    recent_low = previous["low"].min()
    # Buy-side liquidity sweep:
    # price takes previous high but closes below it.
    if (
        current["high"] > recent_high
        and current["close"] < recent_high
    ):
        return "BSL SWEEP"
    # Sell-side liquidity sweep:
    # price takes previous low but closes above it.
    if (
        current["low"] < recent_low
        and current["close"] > recent_low
    ):
        return "SSL SWEEP"
    return "NONE"
# ============================================================
# FVG
# ============================================================
def get_fvg(df):
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
# ============================================================
# DISPLACEMENT
# ============================================================
def get_displacement(df):
    if df is None or len(df) < 25:
        return "NONE"
    ranges = df["high"] - df["low"]
    average_range = ranges.iloc[-21:-1].mean()
    current = df.iloc[-1]
    body = abs(
        current["close"] - current["open"]
    )
    if average_range <= 0:
        return "NONE"
    if body >= average_range * 1.5:
        if current["close"] > current["open"]:
            return "BULLISH"
        if current["close"] < current["open"]:
            return "BEARISH"
    return "NONE"
# ============================================================
# MOMENTUM
# ============================================================
def get_momentum(df):
    if df is None or len(df) < 60:
        return "MIXED"
    e20 = ema(df["close"], 20).iloc[-1]
    e50 = ema(df["close"], 50).iloc[-1]
    if e20 > e50:
        return "BULLISH"
    if e20 < e50:
        return "BEARISH"
    return "MIXED"
# ============================================================
# ZONE
# ============================================================
def get_zone(df):
    if df is None or len(df) < 55:
        return "MID"
    recent = df.iloc[-50:]
    high = recent["high"].max()
    low = recent["low"].min()
    midpoint = (high + low) / 2
    price = df["close"].iloc[-1]
    if price > midpoint:
        return "PREMIUM"
    if price < midpoint:
        return "DISCOUNT"
    return "MID"
# ============================================================
# RSI
# ============================================================
def get_rsi(df):
    if df is None or len(df) < 20:
        return 50.0
    return float(
        rsi(df["close"], 14).iloc[-1]
    )
# ============================================================
# SCORE
# ============================================================
def calculate_score(
    h4_trend,
    h1_trend,
    m15_trend,
    m5_trend,
    liquidity,
    bos,
    fvg,
    displacement,
    momentum,
    rsi_value
):
    buy = 0
    sell = 0
    # --------------------------------------------------------
    # H4 = 20
    # --------------------------------------------------------
    if h4_trend == "BULLISH":
        buy += 20
    elif h4_trend == "BEARISH":
        sell += 20
    # --------------------------------------------------------
    # H1 = 15
    # --------------------------------------------------------
    if h1_trend == "BULLISH":
        buy += 15
    elif h1_trend == "BEARISH":
        sell += 15
    # --------------------------------------------------------
    # M15 = 15
    # --------------------------------------------------------
    if m15_trend == "BULLISH":
        buy += 15
    elif m15_trend == "BEARISH":
        sell += 15
    # --------------------------------------------------------
    # M5 = 10
    # --------------------------------------------------------
    if m5_trend == "BULLISH":
        buy += 10
    elif m5_trend == "BEARISH":
        sell += 10
    # --------------------------------------------------------
    # LIQUIDITY = 10
    # --------------------------------------------------------
    if liquidity == "SSL SWEEP":
        buy += 10
    elif liquidity == "BSL SWEEP":
        sell += 10
    # --------------------------------------------------------
    # BOS = 15
    # --------------------------------------------------------
    if bos == "BULLISH BOS":
        buy += 15
    elif bos == "BEARISH BOS":
        sell += 15
    # --------------------------------------------------------
    # FVG = 5
    # --------------------------------------------------------
    if fvg == "BULLISH FVG":
        buy += 5
    elif fvg == "BEARISH FVG":
        sell += 5
    # --------------------------------------------------------
    # DISPLACEMENT = 10
    # --------------------------------------------------------
    if displacement == "BULLISH":
        buy += 10
    elif displacement == "BEARISH":
        sell += 10
    # --------------------------------------------------------
    # MOMENTUM = 5
    # --------------------------------------------------------
    if momentum == "BULLISH":
        buy += 5
    elif momentum == "BEARISH":
        sell += 5
    # --------------------------------------------------------
    # RSI = 5
    # --------------------------------------------------------
    if rsi_value >= 55:
        buy += 5
    elif rsi_value <= 45:
        sell += 5
    return buy, sell
# ============================================================
# SIGNAL
# ============================================================
def determine_signal(buy_score, sell_score):
    if (
        buy_score >= MIN_SCORE
        and buy_score > sell_score
    ):
        return "BUY"
    if (
        sell_score >= MIN_SCORE
        and sell_score > buy_score
    ):
        return "SELL"
    if (
        buy_score >= EARLY_SCORE
        and buy_score > sell_score
    ):
        return "EARLY BUY"
    if (
        sell_score >= EARLY_SCORE
        and sell_score > buy_score
    ):
        return "EARLY SELL"
    return "WAIT"
# ============================================================
# SETUP
# ============================================================
def build_setup(price, signal):
    if signal not in (
        "BUY",
        "SELL",
        "EARLY BUY",
        "EARLY SELL"
    ):
        return None
    # Conservative fixed distance.
    sl_distance = 5.0
    tp_distance = sl_distance * TARGET_RR
    if "BUY" in signal:
        sl = price - sl_distance
        tp = price + tp_distance
    else:
        sl = price + sl_distance
        tp = price - tp_distance
    risk_money = DEFAULT_DEPOSIT * (
        RISK_PERCENT / 100
    )
    # Conservative lot.
    lot = 0.01
    # Clamp.
    lot = max(
        MIN_LOT,
        min(MAX_LOT, lot)
    )
    return {
        "entry": price,
        "sl": sl,
        "tp": tp,
        "lot": lot,
        "risk_money": risk_money,
        "rr": TARGET_RR
    }
# ============================================================
# DATA AGE
# ============================================================
def parse_timestamp(value):
    if not value:
        return None
    try:
        ts = pd.to_datetime(
            value,
            utc=True
        )
        return ts.to_pydatetime()
    except Exception:
        return None
def is_fresh(data):
    if not data:
        return False
    data_state = data.get("data_state") or {}
    if data_state.get("status") == "unavailable":
        return False
    updated = parse_timestamp(
        data.get("updated_at")
    )
    if updated is None:
        # Chart endpoint may not always provide
        # a top-level timestamp.
        return True
    age = (
        datetime.now(timezone.utc) - updated
    ).total_seconds()
    if age > MAX_DATA_AGE:
        log(
            f"Data too old: {age:.0f}s"
        )
        return False
    return True
# ============================================================
# MARKET ANALYSIS
# ============================================================
def analyze_market():
    global last_analysis
    try:
        # ----------------------------------------------------
        # SPOT
        # ----------------------------------------------------
        spot = fetch_spot()
        if not spot:
            return {
                "signal": "WAIT",
                "error": "XAUS spot unavailable"
            }
        price = spot["price"]
        # If API explicitly says stale, don't generate
        # a trading signal.
        if spot.get("stale"):
            return {
                "signal": "WAIT",
                "price": price,
                "error": "XAUS price is stale"
            }
        # ----------------------------------------------------
        # M5
        # ----------------------------------------------------
        m5_raw = get_chart("5m", "1d")
        m5 = parse_chart(m5_raw)
        if m5 is None:
            return {
                "signal": "WAIT",
                "price": price,
                "error": "M5 data unavailable"
            }
        m5 = remove_current_candle(
            m5,
            5
        )
        # ----------------------------------------------------
        # M15
        # ----------------------------------------------------
        m15_raw = get_chart("15m", "5d")
        m15 = parse_chart(m15_raw)
        if m15 is None:
            return {
                "signal": "WAIT",
                "price": price,
                "error": "M15 data unavailable"
            }
        m15 = remove_current_candle(
            m15,
            15
        )
        # ----------------------------------------------------
        # H1
        # ----------------------------------------------------
        h1_raw = get_chart("60m", "1mo")
        h1 = parse_chart(h1_raw)
        if h1 is None:
            return {
                "signal": "WAIT",
                "price": price,
                "error": "H1 data unavailable"
            }
        h1 = remove_current_candle(
            h1,
            60
        )
        # ----------------------------------------------------
        # H4
        # ----------------------------------------------------
        h4 = resample_h4(h1)
        if h4 is None or len(h4) < 20:
            return {
                "signal": "WAIT",
                "price": price,
                "error": "H4 data unavailable"
            }
        # ----------------------------------------------------
        # TRENDS
        # ----------------------------------------------------
        h4_trend = get_trend(h4)
        h1_trend = get_trend(h1)
        m15_trend = get_trend(m15)
        m5_trend = get_trend(m5)
        # ----------------------------------------------------
        # SMC
        # ----------------------------------------------------
        liquidity = get_liquidity(m5)
        bos = get_bos(m5)
        fvg = get_fvg(m5)
        displacement = get_displacement(m5)
        momentum = get_momentum(m5)
        zone = get_zone(m5)
        rsi_value = get_rsi(m5)
        # ----------------------------------------------------
        # SCORE
        # ----------------------------------------------------
        buy_score, sell_score = calculate_score(
            h4_trend,
            h1_trend,
            m15_trend,
            m5_trend,
            liquidity,
            bos,
            fvg,
            displacement,
            momentum,
            rsi_value
        )
        signal = determine_signal(
            buy_score,
            sell_score
        )
        setup = build_setup(
            price,
            signal
        )
        total = buy_score + sell_score
        if total > 0:
            up_percent = (
                buy_score / total * 100
            )
            down_percent = (
                sell_score / total * 100
            )
        else:
            up_percent = 50
            down_percent = 50
        result = {
            "signal": signal,
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
            "up_percent": up_percent,
            "down_percent": down_percent,
            "setup": setup,
            "source": "XAUS API",
            "closed_candles": True,
            "auto_trading": False,
            "timestamp": datetime.now(
                timezone.utc
            ).isoformat()
        }
        with state_lock:
            last_analysis = result
        return result
    except Exception as e:
        log(f"Analysis error: {e}")
        return {
            "signal": "WAIT",
            "error": str(e)
        }
# ============================================================
# FORMAT SIGNAL
# ============================================================
def format_signal(data):
    if not data:
        return (
            "⚠️ GOLD SMART V5.7\n\n"
            "❌ Нет данных XAUUSD."
        )
    signal = data.get("signal", "WAIT")
    if "error" in data:
        return (
            "⚠️ GOLD SMART V5.7\n\n"
            "⚪ SIGNAL: WAIT\n\n"
            f"Причина: {data['error']}\n\n"
            "🛡 AUTO TRADING: OFF"
        )
    price = data["price"]
    emoji = {
        "BUY": "🟢",
        "SELL": "🔴",
        "EARLY BUY": "🟡",
        "EARLY SELL": "🟠",
        "WAIT": "⚪"
    }.get(signal, "⚪")
    text = (
        "🥇 GOLD SMART V5.7\n\n"
        f"{emoji} SIGNAL: {signal}\n\n"
        f"💰 XAUUSD: {price:.2f}\n\n"
        f"📊 H4: {data['h4']}\n"
        f"📊 H1: {data['h1']}\n"
        f"📊 M15: {data['m15']}\n"
        f"📊 M5: {data['m5']}\n\n"
        f"💧 Liquidity: {data['liquidity']}\n"
        f"🔨 BOS: {data['bos']}\n"
        f"🧩 FVG: {data['fvg']}\n"
        f"💥 Displacement: {data['displacement']}\n"
        f"📈 Momentum: {data['momentum']}\n"
        f"📐 Zone: {data['zone']}\n"
        f"📊 RSI: {data['rsi']:.1f}\n\n"
        f"🟢 BUY SCORE: {data['buy_score']}\n"
        f"🔴 SELL SCORE: {data['sell_score']}\n\n"
        f"📈 UP: {data['up_percent']:.1f}%\n"
        f"📉 DOWN: {data['down_percent']:.1f}%\n"
    )
    setup = data.get("setup")
    if setup:
        text += (
            "\n━━━━━━━━━━━━━━━━\n"
            "🎯 TRADE SETUP\n\n"
            f"Entry: {setup['entry']:.2f}\n"
            f"SL: {setup['sl']:.2f}\n"
            f"TP: {setup['tp']:.2f}\n"
            f"Lot: {setup['lot']:.2f}\n"
            f"Risk: ${setup['risk_money']:.2f}\n"
            f"RR: 1:{setup['rr']:.0f}\n"
        )
    text += (
        "\n━━━━━━━━━━━━━━━━\n"
        "📡 SOURCE: XAUS API\n"
        "🕯 CLOSED CANDLES: ON\n"
        "🛡 AUTO TRADING: OFF"
    )
    return text
# ============================================================
# SIGNAL COOLDOWN
# ============================================================
def should_send_signal(result):
    global last_signal
    global last_signal_time
    signal = result.get("signal", "WAIT")
    if signal == "WAIT":
        return False
    now = time.time()
    with state_lock:
        previous_signal = last_signal
        previous_time = last_signal_time
        if (
            signal == previous_signal
            and now - previous_time
            < COOLDOWN_MIN * 60
        ):
            return False
        last_signal = signal
        last_signal_time = now
    return True
# ============================================================
# RECORD SIGNAL
# ============================================================
def record_signal(result):
    signal = result.get("signal", "WAIT")
    if signal == "WAIT":
        return
    with state_lock:
        stats["total_signals"] += 1
        stats["open"] += 1
        if signal.startswith("EARLY"):
            stats["early"] += 1
        else:
            stats["full"] += 1
        stats["last_signal"] = {
            "signal": signal,
            "price": result.get("price"),
            "time": datetime.now(
                timezone.utc
            ).isoformat()
        }
        stats["updated"] = datetime.now(
            timezone.utc
        ).isoformat()
    save_stats()
# ============================================================
# BACKGROUND ENGINE
# ============================================================
def engine_loop():
    log("GOLD SMART V5.7 engine started")
    while True:
        try:
            result = analyze_market()
            signal = result.get(
                "signal",
                "WAIT"
            )
            price = result.get(
                "price"
            )
            if price:
                log(
                    f"{signal} | "
                    f"XAUUSD {price:.2f} | "
                    f"BUY {result.get('buy_score', 0)} | "
                    f"SELL {result.get('sell_score', 0)}"
                )
            else:
                log(
                    f"{signal} | "
                    f"{result.get('error', 'no data')}"
                )
            # Only send actionable signals.
            if signal != "WAIT":
                if should_send_signal(result):
                    record_signal(result)
                    send_telegram(
                        format_signal(result)
                    )
        except Exception as e:
            log(f"Engine loop error: {e}")
        time.sleep(POLL_SECONDS)
# ============================================================
# TELEGRAM WEBHOOK SETUP
# ============================================================
def setup_webhook():
    if not BOT_TOKEN:
        log("BOT_TOKEN not configured")
        return False
    webhook_url = os.getenv(
        "WEBHOOK_URL",
        "https://gold-bot-q8la.onrender.com/telegram"
    ).strip()
    try:
        response = requests.post(
            telegram_url("setWebhook"),
            json={
                "url": webhook_url,
                "drop_pending_updates": True
            },
            timeout=10
        )
        log(
            f"Webhook setup: "
            f"{response.status_code} "
            f"{response.text[:300]}"
        )
        return response.status_code == 200
    except Exception as e:
        log(f"Webhook setup error: {e}")
        return False
# ============================================================
# STARTUP
# ============================================================
load_stats()
def startup():
    log("========================================")
    log("🥇 GOLD SMART V5.7")
    log("========================================")
    log("Source: XAUS API")
    log("Auto trading: OFF")
    log("Risk: 1%")
    log("Closed candles: ON")
    setup_webhook()
    # Small delay so Gunicorn/Flask is ready.
    time.sleep(2)
    thread = threading.Thread(
        target=engine_loop,
        daemon=True
    )
    thread.start()
    log("Background engine: READY")
# Gunicorn imports bot:app.
# Start background engine only once.
if os.getenv("WERKZEUG_RUN_MAIN") != "true":
    startup()
