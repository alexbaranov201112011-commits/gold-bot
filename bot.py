"""
GOLD SMART V7.6 — XAUUSD SMC Telegram signal bot
XAUS V7.5-compatible spot/intraday feed; multi-timeframe context; Telegram chart; virtual trades only.

Render start:
    gunicorn bot:app --workers 1 --threads 4 --timeout 120

Required environment:
    BOT_TOKEN
    TELEGRAM_CHAT_ID

Optional:
    XAUS_SPOT_URL       Full URL for XAUS spot endpoint
    XAUS_INTRADAY_URL   Full URL for XAUS intraday endpoint
    XAUS_HISTORY_URL    Full URL for XAUS history endpoint (optional)
    DEPOSIT=800
    RISK_PERCENT=1
    MIN_SCORE=75
    POLL_SECONDS=60
    STATE_FILE=gold_smart_state.json

IMPORTANT:
    AUTO_TRADING is permanently disabled. This bot never connects to a broker
    and never places real orders. All trades are virtual simulations.
"""
from __future__ import annotations

import os
import json
import time
import math
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from flask import Flask, jsonify

VERSION = "7.6"
AUTO_TRADING = False
SYMBOL = "XAUUSD"
DEFAULT_TIMEOUT = 15

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
XAUS_BASE_URL = os.getenv("XAUS_BASE_URL", "https://xaus.com/api/v1").strip().rstrip("/")
XAUS_SPOT_URL = os.getenv("XAUS_SPOT_URL", f"{XAUS_BASE_URL}/spot").strip()
XAUS_INTRADAY_URL = os.getenv("XAUS_INTRADAY_URL", f"{XAUS_BASE_URL}/intraday").strip()

DEPOSIT = max(1.0, float(os.getenv("DEPOSIT", "800")))
RISK_PERCENT = min(2.0, max(0.1, float(os.getenv("RISK_PERCENT", "1"))))
MIN_SCORE = max(60, min(95, int(os.getenv("MIN_SCORE", "75"))))
POLL_SECONDS = max(30, int(os.getenv("POLL_SECONDS", "60")))
STATE_FILE = os.getenv("STATE_FILE", "gold_smart_state.json")
MAX_SIGNALS_PER_DAY = 4
MAX_LOSSES_R_PER_DAY = 2.0
MIN_SL = 3.0
MAX_SL = 12.0
SL_BUFFER_ATR = 0.25
TP1_R = 1.0
TP2_R = 2.5
SIGNAL_COOLDOWN_MINUTES = 15
DATA_MAX_AGE_SECONDS = 1800

app = Flask(__name__)
session = requests.Session()
session.headers.update({"User-Agent": f"GOLD-SMART/{VERSION}", "Accept": "application/json"})
lock = threading.RLock()
stop_event = threading.Event()

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def iso_now() -> str:
    return utc_now().isoformat()

def parse_time(value: Any) -> Optional[datetime]:
    try:
        if isinstance(value, (int, float)):
            # Accept seconds or milliseconds.
            ts = float(value)
            if ts > 10_000_000_000:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, timezone.utc)
        s = str(value).strip()
        if not s:
            return None
        # XAUS may encode epoch seconds/milliseconds as a numeric string.
        try:
            numeric = float(s)
            if math.isfinite(numeric):
                if numeric > 100_000_000_000:
                    numeric /= 1000.0
                if numeric > 100_000_000:
                    return datetime.fromtimestamp(numeric, timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def load_state() -> dict:
    initial = {
        "version": VERSION,
        "started_at": iso_now(),
        "last_cycle": None,
        "last_success": None,
        "last_error": None,
        "last_price": None,
        "last_data_time": None,
        "last_signal_time": None,
        "last_signal_key": None,
        "signals_today": 0,
        "day_key": utc_now().date().isoformat(),
        "closed": [],
        "open_trade": None,
        "notifications": [],
        "signal_count": 0,
    }
    try:
        p = Path(STATE_FILE)
        if p.exists():
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                initial.update(loaded)
    except Exception:
        pass
    if initial.get("day_key") != utc_now().date().isoformat():
        initial["day_key"] = utc_now().date().isoformat()
        initial["signals_today"] = 0
    if not isinstance(initial.get("closed"), list):
        initial["closed"] = []
    return initial

state = load_state()

def save_state() -> None:
    try:
        path = Path(STATE_FILE)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        state["last_error"] = f"State save error: {exc}"

def telegram_api(method: str) -> str:
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

def tg_send(text: str, parse_mode: Optional[str] = "HTML", disable_preview: bool = True) -> bool:
    if not BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": disable_preview}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        r = session.post(telegram_api("sendMessage"), json=payload, timeout=DEFAULT_TIMEOUT)
        return r.ok and bool(r.json().get("ok"))
    except Exception:
        return False

def tg_photo(path: str, caption: str = "") -> bool:
    if not BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        with open(path, "rb") as fh:
            r = session.post(
                telegram_api("sendPhoto"),
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1000], "parse_mode": "HTML"},
                files={"photo": fh},
                timeout=30,
            )
        return r.ok and bool(r.json().get("ok"))
    except Exception:
        return False

def html_escape(value: Any) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def http_json(url: str, params: Optional[dict] = None) -> Any:
    if not url:
        raise RuntimeError("Не задан URL XAUS API.")
    r = session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, (dict, list)):
        raise RuntimeError("XAUS API вернул неожиданный формат ответа.")
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(str(data.get("message") or data.get("error") or "XAUS API error"))
    return data

def check_xaus_freshness(payload: Any, max_age_seconds: int, label: str) -> Optional[datetime]:
    """Validate the XAUS V7.5 data_state/as_of fields when the provider supplies them."""
    if not isinstance(payload, dict):
        raise RuntimeError(f"XAUS {label}: ответ не является объектом JSON.")
    data_state = payload.get("data_state") or {}
    if isinstance(data_state, dict):
        status = data_state.get("status")
        if status not in (None, "fresh"):
            raise RuntimeError(f"XAUS status={status}")
    raw_time = payload.get("price_as_of") or (data_state.get("as_of") if isinstance(data_state, dict) else None) or payload.get("updated_at")
    if raw_time is None and isinstance(payload.get("spot"), dict):
        raw_time = payload["spot"].get("price_as_of") or payload["spot"].get("updated_at") or payload["spot"].get("t")
    dt = parse_time(raw_time) if raw_time is not None else None
    if raw_time is not None and dt is None:
        raise RuntimeError(f"XAUS {label}: некорректное время данных.")
    if dt is not None:
        age = (utc_now() - dt).total_seconds()
        if age > max_age_seconds or age < -60:
            raise RuntimeError(f"XAUS {label}: данные устарели ({int(age)} сек.).")
    return dt

def extract_price(payload: Any) -> tuple[float, Optional[datetime]]:
    """Accept common XAUS response shapes: {price}, {spot}, {data:{price}}, {p}."""
    candidates = [payload]
    if isinstance(payload, dict):
        for key in ("data", "result", "spot", "quote", "ticker"):
            if key in payload:
                candidates.append(payload[key])
    if isinstance(payload, dict) and payload.get("spot_usd_oz") is not None:
        try:
            price = float(payload["spot_usd_oz"])
            if math.isfinite(price) and price > 0:
                ts = parse_time(payload.get("price_as_of") or payload.get("updated_at"))
                return price, ts
        except (TypeError, ValueError):
            pass
    if isinstance(payload, dict) and isinstance(payload.get("xau"), dict):
        try:
            price = float(payload["xau"].get("price"))
            if math.isfinite(price) and price > 0:
                ts = parse_time(payload.get("price_as_of") or payload.get("updated_at") or payload["xau"].get("t"))
                return price, ts
        except (TypeError, ValueError):
            pass
    for obj in candidates:
        if isinstance(obj, (int, float, str)):
            try:
                price = float(obj)
                if price > 0:
                    return price, None
            except Exception:
                pass
        if isinstance(obj, dict):
            price_value = None
            for k in ("price", "p", "last", "last_price", "spot_usd_oz", "spot", "value", "close", "rate", "ask"):
                if k in obj:
                    price_value = obj[k]
                    break
            try:
                price = float(price_value)
                if price > 0:
                    ts = None
                    for k in ("t", "time", "timestamp", "datetime", "updated_at", "date"):
                        if k in obj:
                            ts = parse_time(obj[k])
                            if ts:
                                break
                    return price, ts
            except Exception:
                continue
    raise RuntimeError("Не удалось распознать цену в ответе XAUS spot API.")

def extract_points(payload: Any) -> pd.DataFrame:
    """Normalizes {points:[{t,p}]}, {data:[...]}, candles, or OHLC arrays."""
    rows = None
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for k in ("points", "data", "history", "candles", "result", "prices"):
            if isinstance(payload.get(k), list):
                rows = payload[k]
                break
    if not rows:
        raise RuntimeError("XAUS intraday не вернул список точек.")
    parsed = []
    for item in rows:
        if isinstance(item, dict):
            ts = None
            for k in ("t", "time", "timestamp", "datetime", "date"):
                if k in item:
                    ts = parse_time(item[k])
                    if ts:
                        break
            close = None
            for k in ("p", "close", "price", "c", "value", "last"):
                if k in item:
                    close = item[k]
                    break
            try:
                close = float(close)
                if close > 0:
                    parsed.append({"time": ts or utc_now(), "close": close,
                                   "open": float(item.get("open", item.get("o", close))),
                                   "high": float(item.get("high", item.get("h", close))),
                                   "low": float(item.get("low", item.get("l", close)))})
            except Exception:
                continue
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            ts = parse_time(item[0])
            try:
                close = float(item[-1])
                if close > 0:
                    parsed.append({"time": ts or utc_now(), "close": close,
                                   "open": close, "high": close, "low": close})
            except Exception:
                continue
    df = pd.DataFrame(parsed)
    if df.empty:
        raise RuntimeError("В XAUS intraday нет корректных ценовых точек.")
    df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    # Infer approximate OHLC from adjacent sampled prices if API provides only {t,p}.
    if len(df) >= 2:
        prev = df["close"].shift(1)
        df["open"] = df["open"].where(df["open"] != df["close"], prev.fillna(df["close"]))
        spread = (df["close"] - df["open"]).abs() * 0.25
        df["high"] = np.maximum(df["high"], df[["open", "close"]].max(axis=1) + spread)
        df["low"] = np.minimum(df["low"], df[["open", "close"]].min(axis=1) - spread)
    return df

def fetch_market() -> tuple[float, pd.DataFrame, Optional[datetime]]:
    # Match the known-good V7.5 XAUS requests and response fields.
    spot_payload = http_json(XAUS_SPOT_URL, {"compact": "1", "fresh": int(time.time())})
    spot_data_time = check_xaus_freshness(spot_payload, 180, "spot")
    price, parsed_spot_time = extract_price(spot_payload)
    spot_time = spot_data_time or parsed_spot_time

    intraday_payload = http_json(XAUS_INTRADAY_URL, {"symbol": "xau", "hours": 48})
    intraday_time = check_xaus_freshness(intraday_payload, 300, "intraday")
    df = extract_points(intraday_payload)
    if len(df) < 100:
        raise RuntimeError(f"Мало точек XAUS intraday: {len(df)}; нужно минимум 100, как в V7.5.")
    data_time = spot_time or intraday_time or df["time"].iloc[-1]
    age = (utc_now() - data_time).total_seconds()
    if age > DATA_MAX_AGE_SECONDS or age < -60:
        raise RuntimeError(f"XAUS status=stale: данные старше {int(age)} сек.")
    # Anchor the latest sample to current spot, preserving the V7.6 frame builder.
    df.loc[df.index[-1], "close"] = price
    df.loc[df.index[-1], "high"] = max(float(df.iloc[-1]["high"]), price)
    df.loc[df.index[-1], "low"] = min(float(df.iloc[-1]["low"]), price)
    return price, df, data_time

def resample_ohlc(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    tmp = df.copy().set_index("time")
    out = tmp[["open", "high", "low", "close"]].resample(f"{minutes}min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    return out.reset_index()

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=max(3, period // 2)).mean().bfill()

def timeframe_bias(df: pd.DataFrame) -> str:
    if len(df) < 10:
        return "NEUTRAL"
    c = df["close"]
    e20 = ema(c, min(20, max(3, len(c)//3))).iloc[-1]
    e50 = ema(c, min(50, max(5, len(c)//2))).iloc[-1]
    recent = c.iloc[-1] - c.iloc[-min(6, len(c))]
    threshold = max(float(atr(df, 14).iloc[-1]) * 0.08, 0.02)
    if c.iloc[-1] > e20 and e20 > e50 and recent > threshold:
        return "BUY"
    if c.iloc[-1] < e20 and e20 < e50 and recent < -threshold:
        return "SELL"
    return "NEUTRAL"

def strength_scores(df15: pd.DataFrame) -> tuple[int, int]:
    if len(df15) < 10:
        return 50, 50
    tail = df15.tail(12)
    closes = tail["close"].to_numpy()
    highs = tail["high"].to_numpy()
    lows = tail["low"].to_numpy()
    up = float(np.maximum(np.diff(closes), 0).sum())
    down = float(np.maximum(-np.diff(closes), 0).sum())
    displacement_up = max(0.0, closes[-1] - closes[max(0, len(closes)-5)])
    displacement_down = max(0.0, closes[max(0, len(closes)-5)] - closes[-1])
    a = max(float(atr(df15, 14).iloc[-1]), 0.01)
    buy = 50 + 28 * (up - down) / max(up + down, a)
    buy += min(15, 5 * displacement_up / a) - min(15, 5 * displacement_down / a)
    buy = int(max(0, min(100, round(buy))))
    return buy, 100 - buy

def find_sweep_mss(df15: pd.DataFrame) -> dict:
    """Detect a recent liquidity sweep and close-confirmed market structure shift."""
    result = {"direction": None, "sweep": False, "mss": False, "level": None,
              "reason": "нет подтверждённого sweep"}
    if len(df15) < 25:
        result["reason"] = "недостаточно свечей M15"
        return result
    # Last candle is treated as forming; use previous closed candles for confirmation.
    closed = df15.iloc[:-1].reset_index(drop=True)
    if len(closed) < 20:
        return result
    lookback = 12
    prior = closed.iloc[-(lookback+1):-1]
    last = closed.iloc[-1]
    prev = closed.iloc[-2]
    swing_low = float(prior["low"].min())
    swing_high = float(prior["high"].max())
    recent_atr = max(float(atr(closed, 14).iloc[-1]), 0.01)
    sweep_down = float(last["low"]) < swing_low and float(last["close"]) > swing_low
    sweep_up = float(last["high"]) > swing_high and float(last["close"]) < swing_high
    # MSS requires a candle close beyond a local opposing structure level after a sweep.
    recent = closed.iloc[-7:-1]
    bullish_level = float(recent["high"].max())
    bearish_level = float(recent["low"].min())
    if sweep_down:
        result.update(direction="BUY", sweep=True, level=bullish_level)
        if float(last["close"]) > bullish_level and float(last["close"]) > float(last["open"]):
            result.update(mss=True, reason="sweep SSL + закрытие выше структуры")
        else:
            result["reason"] = f"sweep SSL есть; ждём закрытие M15 выше {bullish_level:.2f}"
    elif sweep_up:
        result.update(direction="SELL", sweep=True, level=bearish_level)
        if float(last["close"]) < bearish_level and float(last["close"]) < float(last["open"]):
            result.update(mss=True, reason="sweep BSL + закрытие ниже структуры")
        else:
            result["reason"] = f"sweep BSL есть; ждём закрытие M15 ниже {bearish_level:.2f}"
    else:
        # Also look for sweep in last three closed bars, then require latest close to confirm MSS.
        for idx in range(max(0, len(closed)-4), len(closed)-1):
            candle = closed.iloc[idx]
            earlier = closed.iloc[max(0, idx-lookback):idx]
            if len(earlier) < 5:
                continue
            lo, hi = float(earlier["low"].min()), float(earlier["high"].max())
            if float(candle["low"]) < lo and float(candle["close"]) > lo:
                level = float(closed.iloc[idx+1:]["high"].max())
                result.update(direction="BUY", sweep=True, level=level)
                if float(last["close"]) > level and float(last["close"]) > float(last["open"]):
                    result.update(mss=True, reason="после sweep SSL подтверждён bullish MSS")
                else:
                    result["reason"] = f"sweep SSL найден; ждём закрытие M15 выше {level:.2f}"
                break
            if float(candle["high"]) > hi and float(candle["close"]) < hi:
                level = float(closed.iloc[idx+1:]["low"].min())
                result.update(direction="SELL", sweep=True, level=level)
                if float(last["close"]) < level and float(last["close"]) < float(last["open"]):
                    result.update(mss=True, reason="после sweep BSL подтверждён bearish MSS")
                else:
                    result["reason"] = f"sweep BSL найден; ждём закрытие M15 ниже {level:.2f}"
                break
    return result

def fibonacci_context(df1h: pd.DataFrame, price: float) -> str:
    if len(df1h) < 20:
        return "нет завершённого импульса"
    window = df1h.iloc[-20:-1]
    lo_idx, hi_idx = int(window["low"].idxmin()), int(window["high"].idxmax())
    lo, hi = float(df1h.loc[lo_idx, "low"]), float(df1h.loc[hi_idx, "high"])
    if hi <= lo:
        return "нет завершённого импульса"
    if lo_idx < hi_idx:
        levels = [(0.382, hi-(hi-lo)*0.382), (0.5, hi-(hi-lo)*0.5), (0.618, hi-(hi-lo)*0.618), (0.786, hi-(hi-lo)*0.786)]
        direction = "BUY"
    else:
        levels = [(0.382, lo+(hi-lo)*0.382), (0.5, lo+(hi-lo)*0.5), (0.618, lo+(hi-lo)*0.618), (0.786, lo+(hi-lo)*0.786)]
        direction = "SELL"
    ratio, level = min(levels, key=lambda x: abs(x[1]-price))
    if abs(level-price) <= max(3.0, abs(hi-lo)*0.15):
        distance = "рядом"
    else:
        distance = "далеко"
    return f"импульс {direction}; Fib {ratio*100:.1f}% {level:.2f} · {distance}"

def analyze(price: float, raw: pd.DataFrame) -> dict:
    m5 = resample_ohlc(raw, 5)
    m15 = resample_ohlc(raw, 15)
    h1 = resample_ohlc(raw, 60)
    h4 = resample_ohlc(raw, 240)
    d1 = resample_ohlc(raw, 1440)
    # Intraday-only sample may not contain a full D1. Use coarser context conservatively.
    b5 = timeframe_bias(m5)
    b15 = timeframe_bias(m15)
    b1 = timeframe_bias(h1)
    b4 = timeframe_bias(h4)
    bd = timeframe_bias(d1) if len(d1) >= 3 else "NEUTRAL"
    buyers, sellers = strength_scores(m15)
    atr15 = float(atr(m15, 14).iloc[-1]) if len(m15) >= 5 else float(atr(raw, 14).iloc[-1])
    setup = find_sweep_mss(m15)
    fib = fibonacci_context(h1, price)
    # H4 + H1 establish direction. D1 is contextual only and cannot veto a valid setup.
    core = b4 if b4 != "NEUTRAL" else b1
    trend_conflict = bd not in ("NEUTRAL", core) and core != "NEUTRAL"
    direction = setup["direction"]
    score = 0
    reasons = []
    if direction:
        if b4 == direction:
            score += 20; reasons.append("H4 согласован")
        elif b4 != "NEUTRAL":
            score -= 12; reasons.append("H4 против сетапа")
        if b1 == direction:
            score += 20; reasons.append("H1 согласован")
        elif b1 != "NEUTRAL":
            score -= 8; reasons.append("H1 против сетапа")
        if b15 == direction:
            score += 12; reasons.append("M15 согласован")
        elif b15 != "NEUTRAL":
            score -= 5; reasons.append("M15 против сетапа")
        if setup["sweep"]:
            score += 18; reasons.append("liquidity sweep")
        if setup["mss"]:
            score += 20; reasons.append("MSS подтверждён закрытием")
        strength = buyers if direction == "BUY" else sellers
        score += int(max(0, min(10, (strength - 50) / 5)))
        if trend_conflict:
            score -= 10
            reasons.append("D1 против направления: штраф −10, но не запрет")
        if b5 == direction:
            score += 5; reasons.append("M5 согласован")
        score = max(0, min(100, score))
    else:
        strength = max(buyers, sellers)
    # A confirmed FULL signal needs sweep + MSS and score threshold.
    full = bool(direction and setup["sweep"] and setup["mss"] and score >= MIN_SCORE)
    if not setup["sweep"]:
        no_setup = "нет подтверждённого снятия ликвидности (sweep)"
    elif not setup["mss"]:
        no_setup = setup["reason"]
    elif not full:
        no_setup = f"MSS есть, но score {score}/100 ниже порога {MIN_SCORE}"
    else:
        no_setup = ""
    return {
        "price": price, "bias": {"D1": bd, "H4": b4, "H1": b1, "M15": b15, "M5": b5},
        "buyers": buyers, "sellers": sellers, "atr": atr15, "fib": fib,
        "setup": setup, "direction": direction, "score": score, "full": full,
        "reasons": reasons, "no_setup": no_setup, "trend_conflict": trend_conflict,
        "m5": m5, "m15": m15, "h1": h1,
    }

def bias_symbol(value: str) -> str:
    return {"BUY": "▲", "SELL": "▼", "NEUTRAL": "•"}.get(value, "•")

def analysis_text(a: dict) -> str:
    b = a["bias"]
    lines = [
        f"🥇 <b>GOLD SMART V{VERSION} · анализ</b>",
        f"XAUUSD spot <b>{a['price']:.2f}</b>",
        f"Bias D1 {bias_symbol(b['D1'])}  H4 {bias_symbol(b['H4'])}  H1 {bias_symbol(b['H1'])}  M15 {bias_symbol(b['M15'])}",
        f"ATR M15 (sampled) {a['atr']:.2f}",
        f"Сила покупателей: {a['buyers']}/100",
        f"Сила продавцов: {a['sellers']}/100",
        f"Fibonacci H1: {html_escape(a['fib'])}",
    ]
    if a["trend_conflict"]:
        lines.append("Контекст: D1 против направления H4/H1; D1 учитывается как контекст, а не жёсткий запрет.")
    if a["full"]:
        lines += [f"✅ <b>FULL {a['direction']} · score {a['score']}/100</b>",
                  f"Причины: {html_escape(', '.join(a['reasons']))}"]
    else:
        lines.append(f"Сетапа нет: {html_escape(a['no_setup'])}")
        if a["setup"].get("level") is not None and a["setup"].get("sweep"):
            lines.append(f"Контрольный уровень MSS: {a['setup']['level']:.2f}")
    return "\n".join(lines)

def build_chart(a: dict) -> str:
    df = a["m15"].tail(50).copy()
    if df.empty:
        raise RuntimeError("Недостаточно данных для графика.")
    fig, ax = plt.subplots(figsize=(12, 6), dpi=130)
    x = np.arange(len(df))
    for i, row in enumerate(df.itertuples()):
        up = row.close >= row.open
        candle_color = "#16a34a" if up else "#dc2626"
        ax.vlines(i, row.low, row.high, color=candle_color, linewidth=1)
        bottom = min(row.open, row.close)
        height = max(abs(row.close-row.open), 0.02)
        ax.add_patch(plt.Rectangle((i-0.28, bottom), 0.56, height, color=candle_color, alpha=0.9))
    close = df["close"]
    ax.plot(x, ema(close, 20), linewidth=1.2, label="EMA 20")
    ax.plot(x, ema(close, 50), linewidth=1.2, label="EMA 50")
    ax.axhline(a["price"], linestyle="--", linewidth=1, label=f"Spot {a['price']:.2f}")
    setup = a.get("setup", {})
    if setup.get("level") is not None:
        ax.axhline(setup["level"], linestyle=":", linewidth=1.5, label=f"MSS {setup['level']:.2f}")
    ax.set_title(f"GOLD SMART V{VERSION} | XAUUSD M15 | score {a['score']}/100")
    ax.set_ylabel("Price")
    ax.grid(alpha=0.2)
    ax.legend(loc="upper left", ncol=3, fontsize=8)
    ticks = np.linspace(0, len(df)-1, min(7, len(df))).astype(int)
    ax.set_xticks(ticks)
    ax.set_xticklabels([pd.Timestamp(df.iloc[i]["time"]).strftime("%m-%d %H:%M") for i in ticks], rotation=25, ha="right")
    fig.tight_layout()
    path = "/tmp/gold_smart_chart.png"
    fig.savefig(path)
    plt.close(fig)
    return path

def current_day_reset() -> None:
    today = utc_now().date().isoformat()
    if state.get("day_key") != today:
        state["day_key"] = today
        state["signals_today"] = 0

def open_virtual_trade(a: dict) -> dict:
    entry = float(a["price"])
    direction = a["direction"]
    atr_value = max(float(a["atr"]), 0.1)
    # Use ATR-based stop, clamped to a practical range for gold.
    sl_dist = min(MAX_SL, max(MIN_SL, atr_value * 0.8))
    sl_dist += atr_value * SL_BUFFER_ATR
    sl = entry - sl_dist if direction == "BUY" else entry + sl_dist
    risk_distance = abs(entry-sl)
    tp1 = entry + risk_distance*TP1_R if direction == "BUY" else entry-risk_distance*TP1_R
    tp2 = entry + risk_distance*TP2_R if direction == "BUY" else entry-risk_distance*TP2_R
    risk_cash = DEPOSIT * RISK_PERCENT / 100.0
    return {
        "id": f"{int(time.time())}-{direction}", "direction": direction, "entry": entry,
        "sl": sl, "tp1": tp1, "tp2": tp2, "risk_distance": risk_distance,
        "risk_cash": risk_cash, "score": int(a["score"]), "opened_at": iso_now(),
        "tp1_hit": False, "be": False, "status": "OPEN", "source": "VIRTUAL",
    }

def update_virtual_trade(price: float) -> Optional[str]:
    trade = state.get("open_trade")
    if not trade or trade.get("status") != "OPEN":
        return None
    buy = trade["direction"] == "BUY"
    # Conservative ambiguity rule: if SL and TP could both be hit between sampled quotes,
    # evaluate the stop first. Since only sampled spot prices are available, no intrabar claim.
    if (buy and price <= trade["sl"]) or (not buy and price >= trade["sl"]):
        r = -1.0 if not trade.get("tp1_hit") else 0.0
        close_virtual_trade(trade, "LOSS" if r < 0 else "BE", r, price, "SL/BE")
        return f"🛑 <b>Виртуальная сделка закрыта</b>\n{trade['direction']} · цена {price:.2f}\nРезультат {r:+.2f}R"
    if not trade.get("tp1_hit") and ((buy and price >= trade["tp1"]) or (not buy and price <= trade["tp1"])):
        trade["tp1_hit"] = True
        trade["be"] = True
        trade["sl"] = trade["entry"]
        save_state()
        return (f"🎯 <b>TP1 достигнут · виртуально</b>\n{trade['direction']} · {price:.2f}\n"
                f"50% зафиксировано в модели; остаток SL перенесён в BE {trade['entry']:.2f}.")
    if trade.get("tp1_hit") and ((buy and price >= trade["tp2"]) or (not buy and price <= trade["tp2"])):
        close_virtual_trade(trade, "WIN", 1.75, price, "TP2")
        return f"🏁 <b>Виртуальная сделка закрыта по TP2</b>\n{trade['direction']} · {price:.2f}\nРезультат +1.75R (модель)"
    save_state()
    return None

def close_virtual_trade(trade: dict, outcome: str, r: float, price: float, reason: str) -> None:
    item = dict(trade)
    item.update({"status": outcome, "result_r": float(r), "close_price": float(price),
                 "closed_at": iso_now(), "close_reason": reason})
    state.setdefault("closed", []).append(item)
    state["closed"] = state["closed"][-500:]
    state["open_trade"] = None
    save_state()

def stats_text() -> str:
    closed = state.get("closed", [])
    wins = sum(1 for x in closed if x.get("status") == "WIN")
    losses = sum(1 for x in closed if x.get("status") == "LOSS")
    be_count = sum(1 for x in closed if x.get("status") == "BE")
    total_r = sum(float(x.get("result_r", 0)) for x in closed)
    today = utc_now().date().isoformat()
    todays = [x for x in closed if str(x.get("closed_at", "")).startswith(today)]
    today_r = sum(float(x.get("result_r", 0)) for x in todays)
    n = len(closed)
    wr = 100 * wins / max(1, wins+losses)
    return (f"📊 <b>GOLD SMART V{VERSION} · статистика</b>\n"
            f"Закрыто: {n}\nWin {wins} / Loss {losses} / BE {be_count}\n"
            f"Winrate (без BE): {wr:.1f}%\nСумма: {total_r:+.2f}R\n"
            f"Сегодня: {today_r:+.2f}R\nСигналов сегодня: {state.get('signals_today', 0)}\n"
            f"Режим: только виртуальные сделки · AUTO_TRADING=OFF")

def status_text() -> str:
    price = state.get("last_price")
    trade = state.get("open_trade")
    data_time = state.get("last_data_time")
    return (f"🥇 <b>GOLD SMART V{VERSION}</b>\n"
            f"Engine: {'ON' if not stop_event.is_set() else 'OFF'}\n"
            f"Последний успешный цикл: {state.get('last_success') or 'ещё не было'}\n"
            f"Цена: {price:.2f}\n" if isinstance(price, (int, float)) else
            f"🥇 <b>GOLD SMART V{VERSION}</b>\nEngine: ON\nЦена: нет данных\n") + (
            f"Данные: {data_time or 'нет'}\n"
            f"Последняя ошибка: {state.get('last_error') or 'нет'}\n"
            f"Сигналов всего: {state.get('signal_count', 0)}\n"
            f"Виртуальная сделка: {trade['direction']} @ {trade['entry']:.2f}" if trade else
            f"Данные: {data_time or 'нет'}\nПоследняя ошибка: {state.get('last_error') or 'нет'}\n"
            f"Сигналов всего: {state.get('signal_count', 0)}\nВиртуальная сделка: нет") + "\nAUTO_TRADING: OFF"

def help_text() -> str:
    return ("🤖 <b>Команды GOLD SMART</b>\n"
            "/start — запуск и описание\n/status — состояние движка и данных\n"
            "/signal — текущий анализ + график\n/test — тест Telegram\n/stats — виртуальная статистика\n/help — команды\n\n"
            "Сигналы отправляются автоматически только при FULL score ≥ порога, подтверждённом sweep + MSS. "
            "Никаких реальных ордеров бот не размещает.")

def process_command(text: str, chat_id: str) -> None:
    if TELEGRAM_CHAT_ID and str(chat_id) != str(TELEGRAM_CHAT_ID):
        return
    cmd = (text or "").strip().split()[0].split("@")[0].lower() if text else ""
    if cmd == "/start":
        tg_send("🥇 <b>GOLD SMART запущен</b>\n" + help_text())
    elif cmd == "/help":
        tg_send(help_text())
    elif cmd == "/status":
        tg_send(status_text())
    elif cmd == "/stats":
        tg_send(stats_text())
    elif cmd == "/test":
        tg_send(f"✅ Telegram работает. GOLD SMART V{VERSION}\nAUTO_TRADING=OFF")
    elif cmd == "/signal":
        try:
            price, raw, dt = fetch_market()
            a = analyze(price, raw)
            tg_send(analysis_text(a))
            try:
                path = build_chart(a)
                tg_photo(path, "📈 GOLD SMART · M15 technical chart")
            except Exception:
                pass
        except Exception as exc:
            tg_send(f"⚠️ Не удалось получить анализ: {html_escape(exc)}")

def poll_telegram_commands() -> None:
    if not BOT_TOKEN:
        return
    offset = None
    while not stop_event.is_set():
        try:
            params = {"timeout": 20}
            if offset is not None:
                params["offset"] = offset
            r = session.get(telegram_api("getUpdates"), params=params, timeout=25)
            if r.ok:
                for update in r.json().get("result", []):
                    offset = max(offset or 0, int(update["update_id"])+1)
                    msg = update.get("message") or update.get("edited_message")
                    if msg and msg.get("text"):
                        process_command(msg["text"], str((msg.get("chat") or {}).get("id", "")))
        except Exception:
            time.sleep(3)

def cycle_once() -> None:
    current_day_reset()
    state["last_cycle"] = iso_now()
    price, raw, data_time = fetch_market()
    a = analyze(price, raw)
    state["last_price"] = float(price)
    state["last_data_time"] = data_time.isoformat() if data_time else iso_now()
    state["last_success"] = iso_now()
    state["last_error"] = None
    closed_msg = update_virtual_trade(price)
    if closed_msg:
        tg_send(closed_msg)
    if a["full"]:
        key = f"{a['direction']}:{round(price,1)}:{a['setup'].get('level')}"
        last_time = parse_time(state.get("last_signal_time"))
        cooldown_ok = not last_time or (utc_now()-last_time).total_seconds() >= SIGNAL_COOLDOWN_MINUTES*60
        unique = key != state.get("last_signal_key")
        within_limit = int(state.get("signals_today", 0)) < MAX_SIGNALS_PER_DAY
        if cooldown_ok and unique and within_limit and state.get("open_trade") is None:
            losses_today = sum(abs(float(x.get("result_r", 0))) for x in state.get("closed", [])
                               if str(x.get("closed_at", "")).startswith(utc_now().date().isoformat())
                               and float(x.get("result_r", 0)) < 0)
            if losses_today < MAX_LOSSES_R_PER_DAY:
                trade = open_virtual_trade(a)
                state["open_trade"] = trade
                state["last_signal_time"] = iso_now()
                state["last_signal_key"] = key
                state["signals_today"] = int(state.get("signals_today", 0)) + 1
                state["signal_count"] = int(state.get("signal_count", 0)) + 1
                msg = (f"🚨 <b>GOLD SMART V{VERSION} · FULL {a['direction']}</b>\n"
                       f"XAUUSD: <b>{price:.2f}</b> · score {a['score']}/100\n"
                       f"Entry: {trade['entry']:.2f}\nSL: {trade['sl']:.2f}\n"
                       f"TP1: {trade['tp1']:.2f} (1R)\nTP2: {trade['tp2']:.2f} (2.5R)\n"
                       f"Risk model: {RISK_PERCENT:.2f}% of ${DEPOSIT:.0f}\n"
                       f"Причины: {html_escape(', '.join(a['reasons']))}\n"
                       f"⚠️ ВИРТУАЛЬНЫЙ сигнал, не ордер. AUTO_TRADING=OFF.")
                tg_send(msg)
                try:
                    tg_photo(build_chart(a), "📈 Подтверждённый FULL setup · M15")
                except Exception:
                    pass
    save_state()

def worker_loop() -> None:
    # Avoid duplicate worker processes: Render command should use --workers 1.
    while not stop_event.is_set():
        try:
            with lock:
                cycle_once()
        except Exception as exc:
            with lock:
                state["last_error"] = f"{type(exc).__name__}: {exc}"
                state["last_cycle"] = iso_now()
                save_state()
        stop_event.wait(POLL_SECONDS)

def start_background() -> None:
    if app.config.get("BACKGROUND_STARTED"):
        return
    app.config["BACKGROUND_STARTED"] = True
    threading.Thread(target=worker_loop, name="gold-smart-worker", daemon=True).start()
    threading.Thread(target=poll_telegram_commands, name="gold-smart-telegram", daemon=True).start()

@app.get("/")
def home():
    return jsonify({"bot": "GOLD SMART", "version": VERSION, "engine": "ON",
                    "auto_trading": False, "mode": "virtual_only",
                    "last_success": state.get("last_success"),
                    "last_error": state.get("last_error")})

@app.get("/health")
def health():
    return jsonify({"ok": True, "version": VERSION, "auto_trading": False})

@app.get("/status")
def web_status():
    return jsonify({
        "version": VERSION, "engine": "ON", "auto_trading": False,
        "last_cycle": state.get("last_cycle"), "last_success": state.get("last_success"),
        "last_error": state.get("last_error"), "last_price": state.get("last_price"),
        "last_data_time": state.get("last_data_time"), "signals_today": state.get("signals_today"),
        "signal_count": state.get("signal_count"), "open_trade": state.get("open_trade"),
        "closed_trades": len(state.get("closed", [])),
    })

@app.get("/signal")
def web_signal():
    try:
        price, raw, dt = fetch_market()
        a = analyze(price, raw)
        return jsonify({
            "version": VERSION, "symbol": SYMBOL, "price": price, "data_time": dt.isoformat() if dt else None,
            "bias": a["bias"], "buyers": a["buyers"], "sellers": a["sellers"], "atr_m15": a["atr"],
            "fibonacci": a["fib"], "setup": {k: v for k, v in a["setup"].items()},
            "score": a["score"], "full_signal": a["full"], "direction": a["direction"],
            "reason": a["no_setup"], "auto_trading": False, "message": analysis_text(a).replace("<b>", "").replace("</b>", ""),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "auto_trading": False}), 503

@app.before_request
def ensure_background():
    start_background()

if __name__ == "__main__":
    start_background()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
