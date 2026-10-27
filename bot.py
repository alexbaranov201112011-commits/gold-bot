"""
GOLD SMART V8.1 — Telegram XAUUSD SMC analysis bot
Virtual/manual only. No broker connection and no order placement.
"""
import os, time, json, threading, logging
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests
from flask import Flask, jsonify

VERSION = "8.1"
AUTO_TRADING = False
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
CHART_URL = os.getenv("XAUS_CHART_URL", "https://xaus.com/api/v1/chart").strip()
SPOT_URL = os.getenv("XAUS_SPOT_URL", "https://xaus.com/api/v1/spot").strip()
POLL_SECONDS = max(30, int(os.getenv("POLL_SECONDS", "60")))
DATA_MAX_AGE = max(300, int(os.getenv("DATA_MAX_AGE", "1800")))
SPOT_MAX_AGE = max(60, int(os.getenv("SPOT_MAX_AGE", "300")))
MIN_SCORE = max(55, min(90, int(os.getenv("MIN_SCORE", "65"))))
STATE_FILE = os.getenv("STATE_FILE", "/tmp/gold_smart_state.json")
HTTP_TIMEOUT = 15

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("gold-smart")
app = Flask(__name__)
lock = threading.RLock()
stop_event = threading.Event()
session = requests.Session()
session.headers.update({"User-Agent": "GOLD-SMART/8.1"})


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat()


def parse_time(value):
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            # accept epoch seconds or milliseconds
            n = float(value)
            if n > 10_000_000_000:
                n /= 1000
            return datetime.fromtimestamp(n, tz=timezone.utc)
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def age_seconds(value):
    dt = parse_time(value)
    return None if dt is None else max(0, (utc_now() - dt).total_seconds())


def load_state():
    default = {
        "bot": "GOLD SMART", "version": VERSION, "engine": "ON",
        "auto_trading": False, "mode": "virtual_only", "heartbeat": None,
        "price": None, "price_source": None, "price_time": None,
        "chart_close": None, "source": None, "data_time": None,
        "real_ohlc": False, "last_analysis": {}, "last_error": None,
        "last_success": None, "signals_today": 0, "last_signal_key": None,
        "last_signal": None, "updated_at": None
    }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            old = json.load(f)
        default.update(old if isinstance(old, dict) else {})
    except Exception:
        pass
    default["version"] = VERSION
    default["auto_trading"] = False
    default["mode"] = "virtual_only"
    return default


state = load_state()


def save_state():
    with lock:
        state["heartbeat"] = iso_now()
        state["updated_at"] = state["heartbeat"]
        state["auto_trading"] = False
        state["mode"] = "virtual_only"
        tmp = STATE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp, STATE_FILE)
        except Exception as exc:
            log.warning("state save failed: %s", exc)


def get_json(url, params=None):
    r = session.get(url, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise ValueError("Источник вернул неожиданный формат JSON")
    return data


def fetch_real_ohlc():
    data = get_json(CHART_URL, {"symbol": "xau", "range": "5d", "interval": "15m"})
    points = data.get("points") or data.get("candles") or data.get("data")
    if isinstance(points, dict):
        points = points.get("points") or points.get("candles") or []
    if not isinstance(points, list) or len(points) < 20:
        raise ValueError("В источнике нет достаточного количества OHLC-свечей")
    rows = []
    for p in points:
        if not isinstance(p, dict):
            continue
        try:
            t = p.get("t", p.get("timestamp", p.get("time")))
            o = p.get("o", p.get("open"))
            h = p.get("h", p.get("high"))
            l = p.get("l", p.get("low"))
            c = p.get("c", p.get("close"))
            dt = parse_time(t)
            if dt is None and isinstance(t, (int, float)):
                dt = parse_time(t)
            if dt is None:
                continue
            o, h, l, c = float(o), float(h), float(l), float(c)
            if min(o, h, l, c) <= 0 or h < max(o, c) or l > min(o, c) or h < l:
                continue
            rows.append({"time": dt, "open": o, "high": h, "low": l, "close": c})
        except (TypeError, ValueError):
            continue
    rows.sort(key=lambda x: x["time"])
    # Remove duplicate timestamps
    uniq = {x["time"]: x for x in rows}
    rows = [uniq[k] for k in sorted(uniq)]
    if len(rows) < 20:
        raise ValueError("После проверки осталось меньше 20 корректных свечей")
    candle_age = (utc_now() - rows[-1]["time"]).total_seconds()
    if candle_age < -120 or candle_age > DATA_MAX_AGE:
        raise ValueError(f"OHLC-свечи устарели: возраст {int(candle_age)} сек.")
    return rows, rows[-1]["time"].isoformat()


def fetch_spot():
    # XAUS caches spot responses briefly. A unique query parameter requests a fresh response.
    data = get_json(SPOT_URL, {"compact": "1", "fresh": str(int(time.time()))})
    price = None
    for key in ("spot_usd_oz", "price", "spot", "price_usd_oz", "xau_usd"):
        val = data.get(key)
        if isinstance(val, (int, float)) and val > 0:
            price = float(val)
            break
        if isinstance(val, dict):
            for nested in ("price", "value", "usd_oz"):
                v = val.get(nested)
                if isinstance(v, (int, float)) and v > 0:
                    price = float(v)
                    break
        if price:
            break
    if not price:
        raise ValueError("В ответе spot нет распознанной цены")
    status = data.get("data_state")
    if isinstance(status, dict):
        status = status.get("status")
    if str(status or "").lower() in ("stale", "unavailable", "error"):
        raise ValueError(f"Spot источник сообщает состояние: {status}")
    if data.get("stale") is True:
        raise ValueError("Spot источник пометил котировку как устаревшую")
    stamp = data.get("price_as_of") or data.get("updated_at") or data.get("timestamp")
    age = data.get("age_seconds")
    if isinstance(age, (int, float)) and age > SPOT_MAX_AGE:
        raise ValueError(f"Spot котировка устарела: {int(age)} сек.")
    actual_age = age_seconds(stamp) if stamp else None
    if stamp and actual_age is not None and actual_age > SPOT_MAX_AGE:
        raise ValueError(f"Spot котировка устарела: {int(actual_age)} сек.")
    if not stamp and age is None:
        raise ValueError("У spot котировки нет времени обновления — свежесть проверить нельзя")
    return round(price, 2), stamp or iso_now(), data


def bias_from_closes(rows, n=8):
    closes = [x["close"] for x in rows[-n:]]
    if len(closes) < 3:
        return "•", 0
    delta = closes[-1] - closes[0]
    threshold = max(closes[0] * 0.0003, 0.15)
    if delta > threshold:
        return "▲", 1
    if delta < -threshold:
        return "▼", -1
    return "•", 0


def analyse_smc(rows, spot_price, spot_time):
    # Work only with closed historical candles. The final candle is treated as the latest
    # completed candle returned by the source; the spot quote is display-only.
    if len(rows) < 25:
        raise ValueError("Недостаточно свечей для SMC")
    last = rows[-1]
    prior = rows[-7:-1]
    prev = rows[-2]
    recent_high = max(x["high"] for x in prior)
    recent_low = min(x["low"] for x in prior)

    sweep = "нет"
    sweep_idx = None
    sweep_side = None
    # Search for a recent liquidity sweep followed by a later market-structure shift.
    start = max(1, len(rows) - 7)
    for i in range(start, len(rows)):
        candle = rows[i]
        lookback = rows[max(0, i-6):i]
        if len(lookback) < 4:
            continue
        old_high = max(x["high"] for x in lookback)
        old_low = min(x["low"] for x in lookback)
        if candle["high"] > old_high and candle["close"] < old_high:
            sweep, sweep_idx, sweep_side = "BSL", i, "sell"
        elif candle["low"] < old_low and candle["close"] > old_low:
            sweep, sweep_idx, sweep_side = "SSL", i, "buy"

    mss = False
    direction = None
    if sweep_idx is not None and sweep_idx < len(rows) - 1:
        after = rows[sweep_idx + 1:]
        if sweep_side == "buy":
            level = max(x["high"] for x in rows[max(0, sweep_idx-5):sweep_idx])
            mss = any(x["close"] > level for x in after)
            direction = "BUY" if mss else None
        elif sweep_side == "sell":
            level = min(x["low"] for x in rows[max(0, sweep_idx-5):sweep_idx])
            mss = any(x["close"] < level for x in after)
            direction = "SELL" if mss else None

    d1, _ = bias_from_closes(rows, min(40, len(rows)))
    h4, _ = bias_from_closes(rows, min(16, len(rows)))
    h1, h1v = bias_from_closes(rows, min(8, len(rows)))
    m15, m15v = bias_from_closes(rows, min(4, len(rows)))
    score = 0
    if sweep != "нет":
        score += 25
    if mss:
        score += 30
    if h1v and m15v and h1v == m15v:
        score += 15
    if h4 != "•" and h4 == h1:
        score += 15
    if d1 != "•" and d1 == h4:
        score += 15
    score = min(score, 100)
    full_signal = bool(direction and mss and score >= MIN_SCORE)
    reason = "Подтверждённый сигнал" if full_signal else (
        "Sweep есть, но MSS не подтверждён" if sweep != "нет" and not mss else
        f"Score ниже порога {MIN_SCORE}/100" if direction and score < MIN_SCORE else
        "Подтверждённого сигнала нет"
    )
    key = f"{direction}:{sweep}:{rows[-1]['time'].isoformat()}:{round(last['close'],2)}" if full_signal else None
    return {
        "price": spot_price, "price_time": spot_time, "price_source": SPOT_URL,
        "chart_close": round(last["close"], 2), "source": CHART_URL,
        "data_time": last["time"].isoformat(), "real_ohlc": True,
        "bias": {"D1": d1, "H4": h4, "H1": h1, "M15": m15},
        "sweep": sweep, "mss": bool(mss), "score": score,
        "direction": direction if full_signal else None,
        "full_signal": full_signal, "reason": reason, "signal_key": key,
        "candle_age_seconds": int(max(0, (utc_now() - last["time"]).total_seconds()))
    }


def telegram_send(text):
    if not BOT_TOKEN or not CHAT_ID:
        log.warning("Telegram not configured: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        return False
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        r = session.post(url, json={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        result = r.json()
        if not result.get("ok"):
            raise RuntimeError(str(result))
        return True
    except Exception as exc:
        log.error("Telegram send failed: %s", exc)
        return False


def format_analysis(a):
    bias = a.get("bias", {})
    lines = [
        f"🥇 GOLD SMART V{VERSION} · анализ",
        f"XAUUSD spot (справочная): {a.get('price') if a.get('price') is not None else 'НЕТ ДАННЫХ'}",
        f"Последний close OHLC: {a.get('chart_close', '—')}",
        f"Bias: D1 {bias.get('D1','•')}  H4 {bias.get('H4','•')}  H1 {bias.get('H1','•')}  M15 {bias.get('M15','•')}",
        f"Источник OHLC: {a.get('source', CHART_URL)}",
        f"Источник spot: {a.get('price_source', SPOT_URL)}",
        f"Время spot: {a.get('price_time', '—')}",
        f"Время свечи: {a.get('data_time', '—')}",
        f"OHLC: {'реальные' if a.get('real_ohlc') else 'НЕТ — сигнал заблокирован'}",
        f"Sweep: {a.get('sweep', 'нет')} · MSS: {'да' if a.get('mss') else 'нет'}",
        f"Score: {a.get('score', 0)}/100",
        f"Результат: {a.get('reason', 'Нет анализа')}"
    ]
    if a.get("full_signal"):
        lines += ["", f"🚨 СИГНАЛ {a['direction']} XAUUSD", f"Ориентир цены: {a.get('price')}", "Проверь цену и спред у своего брокера перед ручным входом."]
    else:
        lines += ["⏳ Подтверждённого сигнала нет."]
    lines += ["Режим: виртуальный · AUTO_TRADING=OFF"]
    return "\n".join(lines)


def status_text():
    with lock:
        s = dict(state)
    return (
        f"🥇 GOLD SMART V{VERSION}\n"
        f"Engine: {s.get('engine')}\n"
        f"Режим: {s.get('mode')} · AUTO_TRADING={s.get('auto_trading')}\n"
        f"Spot цена: {s.get('price')}\n"
        f"Spot время: {s.get('price_time')}\n"
        f"Последний close OHLC: {s.get('chart_close')}\n"
        f"Реальные OHLC: {s.get('real_ohlc')}\n"
        f"Источник OHLC: {s.get('source')}\n"
        f"Последний анализ: {s.get('last_success')}\n"
        f"Сигналов сегодня: {s.get('signals_today', 0)}\n"
        f"Ошибка: {s.get('last_error') or 'нет'}"
    )


def do_cycle(notify=False):
    try:
        rows, candle_time = fetch_real_ohlc()
        spot, spot_time, spot_data = fetch_spot()
        a = analyse_smc(rows, spot, spot_time)
        a["data_time"] = candle_time
        with lock:
            old_key = state.get("last_signal_key")
            state.update({
                "price": spot, "price_source": SPOT_URL, "price_time": spot_time,
                "chart_close": a["chart_close"], "source": CHART_URL,
                "data_time": candle_time, "real_ohlc": True,
                "last_analysis": a, "last_error": None, "last_success": iso_now()
            })
            if a["full_signal"] and a.get("signal_key") != old_key:
                state["last_signal_key"] = a["signal_key"]
                state["last_signal"] = a
                state["signals_today"] = int(state.get("signals_today", 0)) + 1
                should_send = bool(notify)
            else:
                should_send = False
        save_state()
        if should_send:
            telegram_send(format_analysis(a))
        return a
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        log.warning("analysis failed: %s", msg)
        try:
            spot, stamp, _ = fetch_spot()
            with lock:
                state.update({"price": spot, "price_source": SPOT_URL, "price_time": stamp})
        except Exception as spot_exc:
            log.warning("spot fetch failed: %s", spot_exc)
            with lock:
                state.update({"price": None, "price_source": None, "price_time": None})
        with lock:
            state.update({
                "real_ohlc": False, "source": CHART_URL, "data_time": None,
                "last_error": msg, "last_analysis": {
                    "price": state.get("price"), "price_source": state.get("price_source"),
                    "price_time": state.get("price_time"), "real_ohlc": False,
                    "score": 0, "sweep": "нет", "mss": False, "full_signal": False,
                    "reason": "Нет свежих реальных OHLC — сигнал заблокирован"
                }
            })
        save_state()
        return state["last_analysis"]


def telegram_loop():
    if not BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN is missing; Telegram polling disabled")
        return
    offset = None
    while not stop_event.is_set():
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            r = session.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", params=params, timeout=35)
            if r.status_code == 409:
                log.error("Telegram 409 conflict: another process is polling this bot token")
                time.sleep(10)
                continue
            r.raise_for_status()
            payload = r.json()
            for update in payload.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message") or {}
                chat = message.get("chat", {})
                text_in = (message.get("text") or "").strip().split()
                if not text_in:
                    continue
                cmd = text_in[0].split("@")[0].lower()
                reply_chat = str(chat.get("id", ""))
                # Restrict responses to configured chat if CHAT_ID is set.
                if CHAT_ID and reply_chat != CHAT_ID:
                    continue
                if cmd in ("/start", "/help"):
                    telegram_send("🥇 GOLD SMART V8.1\nКоманды: /status /signal /test /stats /help\nАвтоуведомления: только при новом подтверждённом BUY/SELL.\nАвтоторговля выключена.")
                elif cmd == "/status":
                    telegram_send(status_text())
                elif cmd == "/signal":
                    a = do_cycle(False)
                    telegram_send(format_analysis(a))
                elif cmd == "/test":
                    telegram_send("✅ Telegram-связь работает. Автоторговля выключена.")
                elif cmd == "/stats":
                    with lock:
                        n = state.get("signals_today", 0)
                        last = state.get("last_signal") or {}
                    telegram_send(f"📊 GOLD SMART V{VERSION}\nСигналов за время работы: {n}\nПоследний сигнал: {last.get('direction', 'нет')}")
        except Exception as exc:
            log.warning("telegram polling error: %s", exc)
            time.sleep(5)


def worker_loop():
    if BOT_TOKEN and CHAT_ID:
        telegram_send(f"🥇 GOLD SMART V{VERSION} запущен.\nАвтоуведомления включены для новых подтверждённых сигналов.\nAUTO_TRADING=OFF")
    while not stop_event.is_set():
        do_cycle(True)
        stop_event.wait(POLL_SECONDS)


@app.get("/")
def root():
    return jsonify({"bot": "GOLD SMART", "version": VERSION, "engine": "ON",
                    "mode": "virtual_only", "auto_trading": False,
                    "endpoints": ["/health", "/status", "/signal"]})


@app.get("/health")
def health():
    return jsonify({"ok": True, "version": VERSION, "heartbeat": state.get("heartbeat")})


@app.get("/status")
def status():
    with lock:
        result = dict(state)
    result["auto_trading"] = False
    result["mode"] = "virtual_only"
    return jsonify(result)


@app.get("/signal")
def signal():
    a = do_cycle(False)
    return jsonify(a)


def start_background():
    if os.getenv("DISABLE_BACKGROUND_WORKERS", "0") == "1":
        return
    threading.Thread(target=worker_loop, name="gold-worker", daemon=True).start()
    threading.Thread(target=telegram_loop, name="telegram-polling", daemon=True).start()


start_background()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
