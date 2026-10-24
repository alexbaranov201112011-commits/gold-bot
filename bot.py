"""GOLD SMART V8.0 — XAUUSD SMC analysis bot. Virtual/manual only.
Render: gunicorn bot:app --workers 1 --threads 4 --timeout 120
Required env: BOT_TOKEN, TELEGRAM_CHAT_ID (for Telegram)
AUTO_TRADING is permanently disabled; this code never sends broker orders.
"""
from __future__ import annotations
import os, json, time, threading
from datetime import datetime, timezone
from typing import Any
import requests
import pandas as pd
from flask import Flask, jsonify

VERSION = "8.0"
AUTO_TRADING = False
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
CHART_URL = os.getenv("XAUS_CHART_URL", "https://xaus.com/api/v1/chart").strip()
SPOT_URL = os.getenv("XAUS_SPOT_URL", "https://xaus.com/api/v1/spot").strip()
INTRADAY_URL = os.getenv("XAUS_INTRADAY_URL", "https://xaus.com/api/v1/intraday").strip()
POLL_SECONDS = max(30, int(os.getenv("POLL_SECONDS", "60")))
DATA_MAX_AGE = max(300, int(os.getenv("DATA_MAX_AGE", "1800")))
MIN_SCORE = max(55, min(90, int(os.getenv("MIN_SCORE", "65"))))
STATE_FILE = os.getenv("STATE_FILE", "/tmp/gold_smart_state_v8.json")
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "GOLD-SMART/8.0", "Accept": "application/json"})
app = Flask(__name__)
lock = threading.RLock()
state: dict[str, Any] = {
    "engine": "STARTING", "version": VERSION, "auto_trading": False,
    "last_error": None, "last_success": None, "heartbeat": None,
    "price": None, "data_time": None, "source": None, "real_ohlc": False,
    "last_analysis": {}, "signals_today": 0, "telegram_offset": 0,
    "day": None, "virtual_trades": []
}

def now() -> datetime:
    return datetime.now(timezone.utc)

def iso() -> str:
    return now().isoformat()

def load_state() -> None:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            old = json.load(f)
        if isinstance(old, dict):
            state.update(old)
    except (OSError, ValueError, TypeError):
        pass
    state.update(engine="ON", version=VERSION, auto_trading=False)

def save_state() -> None:
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[GOLD SMART] state save failed: {e}")

def tg(method: str, payload: dict | None = None, timeout: int = 20):
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в Render Environment")
    r = HTTP.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload or {}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data.get("result")

def send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        tg("sendMessage", {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True})
    except Exception as e:
        with lock:
            state["last_error"] = f"Telegram send: {type(e).__name__}: {e}"
            save_state()

def get_json(url: str, params: dict, timeout: int = 15) -> dict:
    r = HTTP.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, dict):
        raise ValueError(f"{url}: ожидался JSON-объект")
    return payload

def parse_time(v: Any) -> pd.Timestamp:
    if isinstance(v, (int, float)):
        return pd.to_datetime(v, unit="ms" if v > 10_000_000_000 else "s", utc=True)
    return pd.to_datetime(v, utc=True, errors="coerce")

def points_from(payload: dict) -> list:
    for key in ("points", "data", "candles", "results", "items"):
        val = payload.get(key)
        if isinstance(val, list): return val
        if isinstance(val, dict):
            for k in ("points", "data", "candles", "items"):
                if isinstance(val.get(k), list): return val[k]
    return []

def price_from_spot(payload: dict) -> tuple[float | None, str | None, bool]:
    # XAUS documents spot_usd_oz; retain common aliases for compatible responses.
    candidates = [payload.get("spot_usd_oz"), payload.get("price"), payload.get("p"), payload.get("last")]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates += [data.get("spot_usd_oz"), data.get("price"), data.get("p")]
    value = next((x for x in candidates if x is not None), None)
    try: value = float(value) if value is not None else None
    except (ValueError, TypeError): value = None
    stamp = payload.get("price_as_of") or payload.get("updated_at")
    ds = payload.get("data_state")
    status = ds.get("status") if isinstance(ds, dict) else None
    stale = bool(payload.get("stale", False)) or status in ("stale", "unavailable")
    return value, str(stamp) if stamp else None, stale

def fetch_real_ohlc() -> tuple[pd.DataFrame, str]:
    payload = get_json(CHART_URL, {"symbol":"xau", "range":"5d", "interval":"15m"}, 20)
    ds = payload.get("data_state")
    if isinstance(ds, dict) and ds.get("status") in ("stale", "unavailable"):
        raise RuntimeError(f"chart data_state={ds.get('status')}")
    pts = points_from(payload)
    rows = []
    for p in pts:
        if not isinstance(p, dict): continue
        t = p.get("t", p.get("time", p.get("timestamp", p.get("datetime"))))
        o, h, l, c = (p.get(k, p.get(alias)) for k, alias in (("o","open"),("h","high"),("l","low"),("c","close")))
        if t is None or any(v is None for v in (o,h,l,c)): continue
        ts = parse_time(t)
        if pd.isna(ts): continue
        try:
            o,h,l,c = map(float, (o,h,l,c))
            if min(o,h,l,c) <= 0 or h < max(o,l,c) or l > min(o,h,c): continue
        except (ValueError, TypeError): continue
        rows.append({"time":ts,"open":o,"high":h,"low":l,"close":c,"volume":float(p.get("v",p.get("volume",0)) or 0)})
    if len(rows) < 20:
        raise RuntimeError(f"chart endpoint не вернул минимум 20 настоящих OHLC-свечей (получено {len(rows)})")
    df = pd.DataFrame(rows).drop_duplicates("time").sort_values("time").set_index("time")
    if len(df) < 20: raise RuntimeError("После очистки осталось меньше 20 OHLC-свечей")
    age = max(0, (now() - df.index[-1].to_pydatetime()).total_seconds())
    if age > DATA_MAX_AGE: raise RuntimeError(f"OHLC устарели: {int(age/60)} мин")
    return df, CHART_URL

def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    return df.resample(rule, label="right", closed="right").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna(subset=["open","high","low","close"])

def trend(df: pd.DataFrame) -> str:
    if len(df) < 8: return "NEUTRAL"
    c = df.close
    fast, slow = c.ewm(span=5, adjust=False).mean().iloc[-1], c.ewm(span=13, adjust=False).mean().iloc[-1]
    change = float(c.iloc[-1] - c.iloc[-min(5,len(c))])
    if fast > slow and change > 0: return "BUY"
    if fast < slow and change < 0: return "SELL"
    return "NEUTRAL"

def structure(df: pd.DataFrame) -> dict:
    out = {"sweep":None,"mss":False,"direction":None,"reason":"Недостаточно закрытых свечей"}
    closed = df.iloc[:-1]
    if len(closed) < 12: return out
    recent, prior = closed.iloc[-1], closed.iloc[-7:-1]
    ph, pl = float(prior.high.max()), float(prior.low.min())
    if float(recent.low) < pl and float(recent.close) > pl:
        out.update(sweep="SSL", direction="BUY", reason="SSL sweep; ожидаем подтверждение MSS")
        if float(closed.iloc[-1].close) > float(closed.iloc[-5:-1].high.max()): out.update(mss=True, reason="SSL sweep + bullish MSS")
    elif float(recent.high) > ph and float(recent.close) < ph:
        out.update(sweep="BSL", direction="SELL", reason="BSL sweep; ожидаем подтверждение MSS")
        if float(closed.iloc[-1].close) < float(closed.iloc[-5:-1].low.min()): out.update(mss=True, reason="BSL sweep + bearish MSS")
    else: out["reason"] = "Нет подтверждённого sweep на закрытых свечах"
    return out

def analyse(df: pd.DataFrame, source: str) -> dict:
    last = df.index[-1]
    age = max(0, int((now() - last.to_pydatetime()).total_seconds()))
    frames = {"M15":resample(df,"15min"),"H1":resample(df,"1h"),"H4":resample(df,"4h"),"D1":resample(df,"1D")}
    bias = {k:trend(v) for k,v in (("D1",frames["D1"]),("H4",frames["H4"]),("H1",frames["H1"]),("M15",frames["M15"]))}
    s = structure(frames["M15"])
    direction = s.get("direction")
    aligned = sum(v == direction for v in bias.values()) if direction else 0
    opposed = sum(v not in (direction,"NEUTRAL") for v in bias.values()) if direction else 0
    score = min(95, max(50, 55 + aligned*8 - opposed*8 + (12 if s.get("mss") else 0)))
    full = bool(s.get("sweep") and s.get("mss") and direction and score >= MIN_SCORE and age <= DATA_MAX_AGE)
    reason = "Подтверждённый SMC-сетап" if full else s.get("reason", "Сетап не подтверждён")
    if age > DATA_MAX_AGE: reason = f"Данные устарели: {age//60} мин; сигнал заблокирован"
    elif not full and s.get("sweep") and not s.get("mss"): reason = "Sweep есть, но MSS не подтверждён"
    elif not full and s.get("mss") and score < MIN_SCORE: reason = f"Score {score} ниже MIN_SCORE={MIN_SCORE}"
    return {"symbol":"XAUUSD","price":round(float(df.close.iloc[-1]),2),"source":source,"data_time":last.isoformat(),"data_age_seconds":age,"fresh":age<=DATA_MAX_AGE,"real_ohlc":True,"bias":bias,"setup":s,"direction":direction,"score":int(score),"full_signal":full,"reason":reason,"analysed_at":iso(),"candles":len(df)}

def spot_fallback() -> tuple[float | None, str | None, str | None]:
    payload = get_json(SPOT_URL, {"compact":"1"}, 15)
    price, stamp, stale = price_from_spot(payload)
    if price is None: raise RuntimeError("spot endpoint не содержит spot_usd_oz/price")
    if stale: raise RuntimeError(f"spot endpoint сообщает stale/unavailable; price={price}, timestamp={stamp}")
    return price, stamp, SPOT_URL

def do_cycle(notify: bool = False) -> dict:
    with lock:
        state["heartbeat"] = iso(); state["engine"] = "ON"; save_state()
    try:
        df, source = fetch_real_ohlc()
        a = analyse(df, source)
        with lock:
            day = now().date().isoformat()
            if state.get("day") != day: state.update(day=day, signals_today=0)
            oldkey = state.get("last_signal_key")
            key = f"{a['data_time']}:{a['direction']}:{a['score']}"
            send_signal = bool(a["full_signal"] and key != oldkey)
            if send_signal:
                state["last_signal_key"] = key; state["signals_today"] = int(state.get("signals_today",0))+1
            state.update(engine="ON", last_success=iso(), last_error=None, heartbeat=iso(), price=a["price"], data_time=a["data_time"], source=source, real_ohlc=True, last_analysis=a)
            save_state()
        if notify and send_signal: send(format_analysis(a))
        return a
    except Exception as chart_error:
        # Spot can keep the status informative, but it is never treated as candles or a signal.
        spot_err = None
        try:
            price, stamp, source = spot_fallback()
            info = {"symbol":"XAUUSD","price":round(price,2),"source":source,"data_time":stamp,"real_ohlc":False,"full_signal":False,"direction":None,"score":0,"reason":"Доступна только справочная spot-цена; нет реальных OHLC-свечей, сигналы заблокированы","analysed_at":iso()}
            with lock:
                state.update(engine="ON", heartbeat=iso(), price=round(price,2), data_time=stamp, source=source, real_ohlc=False, last_analysis=info, last_error=f"OHLC недоступны: {chart_error}", last_success=state.get("last_success"))
                save_state()
            return info
        except Exception as e: spot_err = f"spot fallback: {type(e).__name__}: {e}"
        message = f"OHLC: {type(chart_error).__name__}: {chart_error}; {spot_err}"
        with lock:
            state.update(engine="ON", heartbeat=iso(), real_ohlc=False, last_error=message)
            state["last_analysis"] = {"symbol":"XAUUSD","real_ohlc":False,"full_signal":False,"reason":"Нет доступных свежих OHLC и spot-данных","analysed_at":iso()}
            save_state()
        raise RuntimeError(message)

def format_analysis(a: dict) -> str:
    arrows = {"BUY":"▲","SELL":"▼","NEUTRAL":"•"}
    bias = a.get("bias",{})
    s = a.get("setup",{})
    lines = [f"🥇 GOLD SMART V{VERSION} · анализ",f"XAUUSD: {a.get('price','—')}"]
    if bias: lines.append("Bias: " + "  ".join(f"{k} {arrows.get(v,'•')}" for k,v in bias.items()))
    lines += [f"Источник: {a.get('source','—')}",f"OHLC: {'реальные' if a.get('real_ohlc') else 'НЕТ — сигнал заблокирован'}",f"Время данных: {a.get('data_time','—')}",f"Sweep: {s.get('sweep') or 'нет'} · MSS: {'да' if s.get('mss') else 'нет'}",f"Score: {a.get('score',0)}/100",f"Результат: {a.get('reason','—')}"]
    lines += ([f"🟢 FULL SIGNAL: {a['direction']}"] if a.get("full_signal") else ["⏳ Подтверждённого сигнала нет."])
    lines.append("Режим: виртуальный · AUTO_TRADING=OFF")
    return "\n".join(lines)

def status_text() -> str:
    with lock:
        return (f"🥇 GOLD SMART V{VERSION}\nEngine: {state.get('engine')}\nЦена: {state.get('price')}\nИсточник: {state.get('source')}\nВремя данных: {state.get('data_time')}\nПоследний успех: {state.get('last_success')}\nHeartbeat: {state.get('heartbeat')}\nРеальные OHLC: {state.get('real_ohlc')}\nFULL сегодня: {state.get('signals_today',0)}\nОшибка: {state.get('last_error')}\nAUTO_TRADING: OFF")

def telegram_loop() -> None:
    while True:
        try:
            with lock: offset = int(state.get("telegram_offset",0))
            updates = tg("getUpdates", {"timeout":15,"offset":offset,"allowed_updates":["message"]}, timeout=20)
            for upd in updates or []:
                with lock:
                    state["telegram_offset"] = max(int(state.get("telegram_offset",0)),int(upd.get("update_id",0))+1); save_state()
                msg = upd.get("message") or {}; chat = str((msg.get("chat") or {}).get("id",""))
                if CHAT_ID and chat != str(CHAT_ID): continue
                cmd = (msg.get("text") or "").strip().lower().split()[0] if (msg.get("text") or "").strip() else ""
                if cmd in ("/start","/help"):
                    send(f"🥇 GOLD SMART V{VERSION}\n/status — статус\n/signal — свежий анализ\n/test — тест Telegram\n/stats — статистика\n\nТолько реальные OHLC дают право на SMC-сигнал. Автоторговля выключена.")
                elif cmd == "/status": send(status_text())
                elif cmd == "/signal":
                    try: send(format_analysis(do_cycle(False)))
                    except Exception as e: send(f"⚠️ Анализ не выполнен: {e}")
                elif cmd == "/test": send(f"✅ GOLD SMART V{VERSION}: Telegram работает. AUTO_TRADING=OFF.")
                elif cmd == "/stats":
                    with lock: send(f"📊 GOLD SMART V{VERSION}\nFULL сегодня: {state.get('signals_today',0)}\nВиртуальных сделок: {len(state.get('virtual_trades',[]))}\nРеальные ордера: 0")
        except Exception as e:
            with lock: state["last_error"] = f"Telegram polling: {type(e).__name__}: {e}"; save_state()
            time.sleep(5)

def worker_loop() -> None:
    with lock: state.update(engine="ON",heartbeat=iso()); save_state()
    send(f"🟢 GOLD SMART V{VERSION}: цикл запущен. Опрос каждые {POLL_SECONDS} сек. Режим виртуальный; AUTO_TRADING=OFF.")
    while True:
        try: do_cycle(True)
        except Exception as e: print(f"[GOLD SMART] cycle error: {e}")
        time.sleep(POLL_SECONDS)

@app.get("/")
def home():
    return jsonify({"bot":"GOLD SMART","version":VERSION,"engine":state.get("engine"),"auto_trading":False,"mode":"virtual_only","endpoints":["/health","/status","/signal"]})
@app.get("/health")
def health():
    return jsonify({"ok":True,"bot":"GOLD SMART","version":VERSION,"engine":state.get("engine"),"auto_trading":False})
@app.get("/status")
def status():
    with lock:
        return jsonify({"bot":"GOLD SMART","version":VERSION,"engine":state.get("engine"),"auto_trading":False,"mode":"virtual_only","last_error":state.get("last_error"),"last_success":state.get("last_success"),"heartbeat":state.get("heartbeat"),"price":state.get("price"),"data_time":state.get("data_time"),"source":state.get("source"),"real_ohlc":state.get("real_ohlc"),"signals_today":state.get("signals_today",0),"last_analysis":state.get("last_analysis")})
@app.get("/signal")
def signal():
    with lock: return jsonify({"bot":"GOLD SMART","version":VERSION,"auto_trading":False,"last_analysis":state.get("last_analysis") or {},"last_error":state.get("last_error")})

load_state()
# Flask/Gunicorn starts one web worker; Render command must use --workers 1 to avoid duplicate polling loops.
if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not os.environ.get("WERKZEUG_RUN_MAIN"):
    threading.Thread(target=worker_loop,daemon=True,name="gold-smart-worker").start()
    if BOT_TOKEN: threading.Thread(target=telegram_loop,daemon=True,name="telegram-commands").start()
