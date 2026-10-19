"""
GOLD SMART V7.9 — XAUUSD SMC Telegram signal bot.
Real OHLCV preferred; proxy {t,p} data is analysis-only and NEVER creates signals.
Virtual/manual tracking only. AUTO_TRADING is permanently disabled.
Render start command:
gunicorn bot:app --workers 1 --threads 4 --timeout 120
Required env: BOT_TOKEN, TELEGRAM_CHAT_ID
Recommended env: XAUS_CHART_URL=https://xaus.com/api/v1/chart
Optional: XAUS_SPOT_URL, XAUS_INTRADAY_URL, POLL_SECONDS, MIN_SCORE, STATE_FILE
"""
from __future__ import annotations

import os
import json
import time
import threading
import traceback
from datetime import datetime, timezone
from typing import Any

import requests
import numpy as np
import pandas as pd
from flask import Flask, jsonify

VERSION = "7.9"
AUTO_TRADING = False
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
XAUS_CHART_URL = os.getenv("XAUS_CHART_URL", "https://xaus.com/api/v1/chart").strip()
XAUS_SPOT_URL = os.getenv("XAUS_SPOT_URL", "").strip()
XAUS_INTRADAY_URL = os.getenv("XAUS_INTRADAY_URL", "").strip()
POLL_SECONDS = max(30, int(os.getenv("POLL_SECONDS", "60")))
MIN_SCORE = max(55, min(90, int(os.getenv("MIN_SCORE", "65"))))
STATE_FILE = os.getenv("STATE_FILE", "/tmp/gold_smart_state.json")
DATA_MAX_AGE = max(300, int(os.getenv("DATA_MAX_AGE", "1800")))
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "GOLD-SMART/7.9", "Accept": "application/json"})
app = Flask(__name__)

lock = threading.RLock()
state: dict[str, Any] = {
    "engine": "STARTING", "version": VERSION, "auto_trading": False,
    "last_error": None, "last_success": None, "heartbeat": None,
    "price": None, "data_time": None, "source": None, "real_ohlc": False,
    "last_analysis": {}, "signals_today": 0, "early_today": 0,
    "virtual_trades": [], "telegram_offset": 0, "day": None,
    "last_full_key": None, "last_early_key": None
}

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def iso_now() -> str:
    return utc_now().isoformat()

def load_state() -> None:
    global state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            old = json.load(f)
        if isinstance(old, dict):
            state.update(old)
    except (OSError, ValueError, TypeError):
        pass
    state["engine"] = "ON"
    state["version"] = VERSION
    state["auto_trading"] = False

def save_state() -> None:
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass

def telegram(method: str, payload: dict[str, Any] | None = None, timeout: int = 20) -> Any:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в Render Environment")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    r = SESSION.post(url, json=payload or {}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API: {data}")
    return data.get("result")

def send_message(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        telegram("sendMessage", {
            "chat_id": CHAT_ID, "text": text,
            "disable_web_page_preview": True
        })
    except Exception as e:
        with lock:
            state["last_error"] = f"Telegram send: {e}"
            save_state()

def parse_epoch(value: Any) -> pd.Timestamp:
    if isinstance(value, (int, float)):
        unit = "ms" if value > 10_000_000_000 else "s"
        return pd.to_datetime(value, unit=unit, utc=True)
    return pd.to_datetime(value, utc=True, errors="coerce")

def extract_points(payload: Any) -> list[dict[str, Any]]:
    """Accept common XAUS response shapes without fabricating OHLC."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("points", "data", "candles", "results", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for nested in ("points", "data", "candles", "items"):
                if isinstance(value.get(nested), list):
                    return value[nested]
    return []

def fetch_chart() -> tuple[pd.DataFrame, str, bool, str | None]:
    errors = []
    urls = []
    if XAUS_CHART_URL:
        urls.append((XAUS_CHART_URL, {"symbol": "xau", "range": "5d", "interval": "15m"}))
    if XAUS_INTRADAY_URL:
        urls.append((XAUS_INTRADAY_URL, {"symbol": "xau", "hours": "120"}))
    for url, params in urls:
        try:
            r = SESSION.get(url, params=params, timeout=20)
            r.raise_for_status()
            payload = r.json()
            points = extract_points(payload)
            if not points:
                errors.append(f"{url}: JSON без массива points/data/candles")
                continue
            rows = []
            real = True
            for p in points:
                if not isinstance(p, dict):
                    continue
                t = p.get("t", p.get("time", p.get("timestamp", p.get("datetime"))))
                close = p.get("c", p.get("close", p.get("p", p.get("price"))))
                if t is None or close is None:
                    continue
                op = p.get("o", p.get("open"))
                hi = p.get("h", p.get("high"))
                lo = p.get("l", p.get("low"))
                if op is None or hi is None or lo is None:
                    real = False
                    # Preserve close-only points for diagnostics, not for SMC signals.
                    op = hi = lo = close
                ts = parse_epoch(t)
                if pd.isna(ts):
                    continue
                rows.append({
                    "time": ts, "open": float(op), "high": float(hi),
                    "low": float(lo), "close": float(close),
                    "volume": float(p.get("v", p.get("volume", 0)) or 0)
                })
            if len(rows) < 20:
                errors.append(f"{url}: мало корректных точек ({len(rows)})")
                continue
            df = pd.DataFrame(rows).drop_duplicates("time").sort_values("time").set_index("time")
            df = df[~df.index.isna()]
            # A candle endpoint should return OHLC. Never pretend proxy candles are real.
            real = real and len(rows) >= 20
            return df, url, real, None
        except Exception as e:
            errors.append(f"{url}: {type(e).__name__}: {e}")
    # Optional fallback only for display; these points cannot generate trade signals.
    if XAUS_SPOT_URL:
        try:
            r = SESSION.get(XAUS_SPOT_URL, params={"symbol": "xau"}, timeout=15)
            r.raise_for_status()
            payload = r.json()
            price = None
            if isinstance(payload, dict):
                price = payload.get("price", payload.get("p", payload.get("last")))
                if price is None and isinstance(payload.get("data"), dict):
                    price = payload["data"].get("price", payload["data"].get("p"))
            if price is not None:
                ts = utc_now()
                df = pd.DataFrame([{"open": float(price), "high": float(price),
                                    "low": float(price), "close": float(price), "volume": 0}],
                                  index=pd.DatetimeIndex([ts], name="time"))
                return df, XAUS_SPOT_URL, False, "Только spot-цена; OHLC отсутствует"
        except Exception as e:
            errors.append(f"{XAUS_SPOT_URL}: {e}")
    raise RuntimeError("Не удалось получить данные XAUS. " + " | ".join(errors[-3:]))

def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return df.resample(rule, label="right", closed="right").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
    }).dropna(subset=["open", "high", "low", "close"])

def trend(df: pd.DataFrame) -> str:
    if len(df) < 8:
        return "NEUTRAL"
    # EMA structure plus recent price displacement; conservative bias only.
    c = df["close"]
    fast = c.ewm(span=5, adjust=False).mean().iloc[-1]
    slow = c.ewm(span=13, adjust=False).mean().iloc[-1]
    change = c.iloc[-1] - c.iloc[-min(5, len(c))]
    if fast > slow and change > 0:
        return "BUY"
    if fast < slow and change < 0:
        return "SELL"
    return "NEUTRAL"

def atr(df: pd.DataFrame, period: int = 14) -> float:
    if len(df) < 2:
        return 0.0
    prev = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs()
    ], axis=1).max(axis=1)
    val = tr.rolling(period, min_periods=2).mean().iloc[-1]
    return float(val) if pd.notna(val) else 0.0

def structure_setup(df: pd.DataFrame) -> dict[str, Any]:
    """Closed-candle sweep then MSS; conservative, real OHLC only."""
    result = {"sweep": None, "mss": False, "direction": None, "reason": "Недостаточно свечей"}
    if len(df) < 12:
        return result
    # Exclude the current/incomplete candle when checking the preceding range.
    closed = df.iloc[:-1]
    if len(closed) < 10:
        return result
    recent = closed.iloc[-1]
    prior = closed.iloc[-7:-1]
    prior_high = float(prior["high"].max())
    prior_low = float(prior["low"].min())
    sweep_down = float(recent["low"]) < prior_low and float(recent["close"]) > prior_low
    sweep_up = float(recent["high"]) > prior_high and float(recent["close"]) < prior_high
    if sweep_down:
        result.update(sweep="SSL", direction="BUY", reason="SSL sweep: цена вернулась выше предыдущего минимума")
        # MSS must be confirmed by a later closed candle; avoid same-candle sweep=MSS.
        later = closed.iloc[-3:]
        if len(later) >= 2 and float(later.iloc[-1]["close"]) > float(closed.iloc[-5:-1]["high"].max()):
            result.update(mss=True, reason="SSL sweep + bullish MSS")
    elif sweep_up:
        result.update(sweep="BSL", direction="SELL", reason="BSL sweep: цена вернулась ниже предыдущего максимума")
        later = closed.iloc[-3:]
        if len(later) >= 2 and float(later.iloc[-1]["close"]) < float(closed.iloc[-5:-1]["low"].min()):
            result.update(mss=True, reason="BSL sweep + bearish MSS")
    else:
        result["reason"] = "Нет подтверждённого sweep на закрытых свечах"
    return result

def analyse(df: pd.DataFrame, source: str, real_ohlc: bool) -> dict[str, Any]:
    current = float(df["close"].iloc[-1])
    now = utc_now()
    last_ts = df.index[-1]
    age = max(0.0, (now - last_ts.to_pydatetime()).total_seconds())
    d15 = resample_ohlc(df, "15min")
    h1 = resample_ohlc(df, "1h")
    h4 = resample_ohlc(df, "4h")
    d1 = resample_ohlc(df, "1D")
    trends = {"D1": trend(d1), "H4": trend(h4), "H1": trend(h1), "M15": trend(d15)}
    atr15 = atr(d15)
    setup = structure_setup(d15) if real_ohlc else {
        "sweep": None, "mss": False, "direction": None,
        "reason": "Нет настоящих OHLC — сигналы заблокированы"
    }
    direction = setup.get("direction")
    score = 50
    if direction:
        aligned = sum(1 for v in trends.values() if v == direction)
        opposed = sum(1 for v in trends.values() if v not in (direction, "NEUTRAL"))
        score = min(95, 55 + aligned * 8 - opposed * 8 + (12 if setup.get("mss") else 0))
    fresh = age <= DATA_MAX_AGE
    full = bool(real_ohlc and fresh and setup.get("sweep") and setup.get("mss")
                and direction and score >= MIN_SCORE)
    if not fresh:
        reason = f"Данные устарели: {int(age/60)} мин. Сигналы заблокированы"
    elif not real_ohlc:
        reason = "Источник не дал настоящие OHLC (o/h/l/c); сигнал запрещён"
    elif not setup.get("sweep"):
        reason = setup["reason"]
    elif not setup.get("mss"):
        reason = "Sweep найден, но MSS ещё не подтверждён закрытой свечой"
    elif score < MIN_SCORE:
        reason = f"Score {score} ниже MIN_SCORE={MIN_SCORE}"
    else:
        reason = "Подтверждённый SMC-сетап"
    return {
        "symbol": "XAUUSD", "price": round(current, 2), "source": source,
        "data_time": last_ts.isoformat(), "data_age_seconds": int(age),
        "fresh": fresh, "real_ohlc": bool(real_ohlc), "bias": trends,
        "atr_m15": round(atr15, 3), "setup": setup, "direction": direction,
        "score": int(score), "full_signal": full, "reason": reason,
        "analysed_at": iso_now(), "candles": len(df)
    }

def format_analysis(a: dict[str, Any]) -> str:
    arrows = {"BUY": "▲", "SELL": "▼", "NEUTRAL": "•"}
    bias = a.get("bias", {})
    setup = a.get("setup", {})
    lines = [
        f"🥇 GOLD SMART V{VERSION} · анализ",
        f"XAUUSD {a.get('price', '—')}",
        "Bias: " + "  ".join(f"{k} {arrows.get(v, '•')}" for k, v in bias.items()),
        f"ATR M15: {a.get('atr_m15', 0)}",
        f"Источник: {a.get('source', '—')}",
        f"OHLC: {'реальные' if a.get('real_ohlc') else 'НЕТ — сигналы заблокированы'}",
        f"Свеча: {a.get('data_time', '—')} · возраст {a.get('data_age_seconds', '—')} сек",
        f"Sweep: {setup.get('sweep') or 'нет'} · MSS: {'да' if setup.get('mss') else 'нет'}",
        f"Score: {a.get('score', 0)}/100",
        f"Результат: {a.get('reason', '—')}"
    ]
    if a.get("full_signal"):
        lines += ["", f"🟢 FULL SIGNAL: {a['direction']}"]
    else:
        lines += ["", "⏳ Сигнала нет. Не входить вручную без подтверждения."]
    lines += ["", "Режим: виртуальный · AUTO_TRADING=OFF"]
    return "\n".join(lines)

def run_analysis(notify: bool = False) -> dict[str, Any]:
    with lock:
        state["heartbeat"] = iso_now()
    df, source, real_ohlc, warning = fetch_chart()
    a = analyse(df, source, real_ohlc)
    if warning:
        a["warning"] = warning
    today = utc_now().date().isoformat()
    with lock:
        if state.get("day") != today:
            state["day"] = today
            state["signals_today"] = 0
            state["early_today"] = 0
            state["last_full_key"] = None
            state["last_early_key"] = None
        state.update({
            "engine": "ON", "last_success": iso_now(), "last_error": None,
            "price": a["price"], "data_time": a["data_time"], "source": source,
            "real_ohlc": real_ohlc, "last_analysis": a,
            "heartbeat": iso_now()
        })
        key = f"{a.get('data_time')}:{a.get('direction')}:{a.get('score')}"
        should_send = bool(a.get("full_signal") and key != state.get("last_full_key"))
        if should_send:
            state["last_full_key"] = key
            state["signals_today"] = int(state.get("signals_today", 0)) + 1
        save_state()
    if notify:
        if should_send:
            send_message(format_analysis(a))
        else:
            # Keep analysis available via /signal; do not spam the chat every minute.
            pass
    return a

def status_text() -> str:
    with lock:
        a = state.get("last_analysis") or {}
        return (
            f"🥇 GOLD SMART V{VERSION}\n"
            f"Engine: {state.get('engine')}\n"
            f"Цена: {state.get('price')}\n"
            f"Последний успешный анализ: {state.get('last_success')}\n"
            f"Heartbeat: {state.get('heartbeat')}\n"
            f"Источник: {state.get('source')}\n"
            f"Реальные OHLC: {state.get('real_ohlc')}\n"
            f"FULL сигналов сегодня: {state.get('signals_today', 0)}\n"
            f"Ошибка: {state.get('last_error')}\n"
            f"AUTO_TRADING: OFF"
        )

def command_loop() -> None:
    while True:
        try:
            with lock:
                offset = int(state.get("telegram_offset", 0))
            updates = telegram("getUpdates", {"timeout": 15, "offset": offset, "allowed_updates": ["message"]}, timeout=20)
            for update in updates or []:
                with lock:
                    state["telegram_offset"] = max(int(state.get("telegram_offset", 0)), int(update.get("update_id", 0)) + 1)
                    save_state()
                msg = update.get("message") or {}
                chat = str((msg.get("chat") or {}).get("id", ""))
                if CHAT_ID and chat != str(CHAT_ID):
                    continue
                text = (msg.get("text") or "").strip().lower()
                if text in ("/start", "/help"):
                    send_message("🥇 GOLD SMART V7.9\n/start — помощь\n/status — статус\n/signal — свежий анализ\n/test — тест Telegram\n\nSMC: D1/H4/H1 → M15 sweep + MSS. Только реальные OHLC. Автоторговля выключена.")
                elif text == "/status":
                    send_message(status_text())
                elif text == "/signal":
                    try:
                        a = run_analysis(False)
                        send_message(format_analysis(a))
                    except Exception as e:
                        send_message(f"⚠️ Анализ не выполнен: {e}")
                elif text == "/test":
                    send_message("✅ GOLD SMART: Telegram работает. AUTO_TRADING=OFF.")
                elif text == "/stats":
                    with lock:
                        send_message(f"📊 GOLD SMART V{VERSION}\nFULL сегодня: {state.get('signals_today', 0)}\nВиртуальных сделок: {len(state.get('virtual_trades', []))}\nРеальные ордера: 0")
        except Exception as e:
            with lock:
                state["last_error"] = f"Telegram polling: {type(e).__name__}: {e}"
                save_state()
            time.sleep(5)

def worker_loop() -> None:
    with lock:
        state["engine"] = "ON"
        save_state()
    send_message(f"🟢 GOLD SMART V{VERSION}: цикл запущен\nОпрос каждые {POLL_SECONDS} сек.\nРежим: виртуальный · AUTO_TRADING=OFF")
    while True:
        try:
            run_analysis(notify=True)
        except Exception as e:
            with lock:
                state["last_error"] = f"{type(e).__name__}: {e}"
                state["heartbeat"] = iso_now()
                save_state()
            # Alert only on first/repeated outage in a coarse interval, avoid Telegram spam.
            print(f"[GOLD SMART] analysis error: {e}")
        time.sleep(POLL_SECONDS)

@app.get("/")
def home():
    return jsonify({"bot": "GOLD SMART", "version": VERSION, "engine": state.get("engine"),
                    "auto_trading": False, "mode": "virtual_only",
                    "endpoints": ["/health", "/status", "/signal"]})

@app.get("/health")
def health():
    return jsonify({"ok": True, "bot": "GOLD SMART", "version": VERSION,
                    "engine": state.get("engine"), "auto_trading": False})

@app.get("/status")
def status():
    with lock:
        return jsonify({
            "bot": "GOLD SMART", "version": VERSION, "engine": state.get("engine"),
            "auto_trading": False, "mode": "virtual_only",
            "last_error": state.get("last_error"), "last_success": state.get("last_success"),
            "heartbeat": state.get("heartbeat"), "price": state.get("price"),
            "data_time": state.get("data_time"), "source": state.get("source"),
            "real_ohlc": state.get("real_ohlc"), "signals_today": state.get("signals_today", 0),
            "last_analysis": state.get("last_analysis")
        })

@app.get("/signal")
def signal():
    with lock:
        a = dict(state.get("last_analysis") or {})
    return jsonify({"bot": "GOLD SMART", "version": VERSION, "auto_trading": False,
                    "last_analysis": a, "last_error": state.get("last_error")})

load_state()

# Start exactly one worker per Render process; use --workers 1.
if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not os.environ.get("WERKZEUG_RUN_MAIN"):
    threading.Thread(target=worker_loop, daemon=True, name="gold-smart-worker").start()
    if BOT_TOKEN:
        threading.Thread(target=command_loop, daemon=True, name="telegram-commands").start()
