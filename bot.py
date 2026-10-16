"""GOLD SMART V7.8 — XAUUSD SMC Telegram signal bot.
XAUS spot/intraday feed, multi-timeframe context, chart, and virtual-only trades.
AUTO_TRADING is permanently disabled; this program never places real orders.
Render: gunicorn bot:app --workers 1 --threads 4 --timeout 120
Required env: BOT_TOKEN, TELEGRAM_CHAT_ID
"""
from __future__ import annotations
import os, json, time, math, threading, html
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

VERSION = "7.8"
AUTO_TRADING = False
SYMBOL = "XAUUSD"
TIMEOUT = 15
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
BASE = os.getenv("XAUS_BASE_URL", "https://xaus.com/api/v1").strip().rstrip("/")
SPOT_URL = os.getenv("XAUS_SPOT_URL", f"{BASE}/spot").strip()
INTRADAY_URL = os.getenv("XAUS_INTRADAY_URL", f"{BASE}/intraday").strip()
DEPOSIT = max(1.0, float(os.getenv("DEPOSIT", "800")))
RISK_PERCENT = min(2.0, max(0.1, float(os.getenv("RISK_PERCENT", "1"))))
MIN_SCORE = max(55, min(95, int(os.getenv("MIN_SCORE", "65"))))
POLL_SECONDS = max(30, int(os.getenv("POLL_SECONDS", "60")))
STATE_FILE = os.getenv("STATE_FILE", "gold_smart_state.json")
MAX_SIGNALS_DAY = 4
MAX_LOSS_R_DAY = 2.0
MIN_SL, MAX_SL, SL_BUFFER_ATR = 3.0, 12.0, 0.25
TP1_R, TP2_R = 1.0, 2.5
FULL_COOLDOWN_MIN = 15
EARLY_COOLDOWN_MIN = 30
DATA_MAX_AGE = 1800
app = Flask(__name__)
session = requests.Session()
session.headers.update({"User-Agent": f"GOLD-SMART/{VERSION}", "Accept": "application/json"})
lock = threading.RLock()
stop_event = threading.Event()


def now() -> datetime: return datetime.now(timezone.utc)
def iso_now() -> str: return now().isoformat()
def parse_time(v: Any) -> Optional[datetime]:
    try:
        if isinstance(v, (int, float)):
            x = float(v)
            if x > 10_000_000_000: x /= 1000
            return datetime.fromtimestamp(x, timezone.utc)
        s = str(v or "").strip()
        if not s: return None
        try:
            x = float(s)
            if x > 100_000_000:
                if x > 100_000_000_000: x /= 1000
                return datetime.fromtimestamp(x, timezone.utc)
        except ValueError: pass
        if s.endswith("Z"): s = s[:-1] + "+00:00"
        d = datetime.fromisoformat(s)
        if d.tzinfo is None: d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception: return None


def fresh_state() -> dict:
    return {"version": VERSION, "started_at": iso_now(), "last_cycle": None,
        "last_success": None, "last_error": None, "last_price": None,
        "last_data_time": None, "last_signal_time": None, "last_signal_key": None,
        "last_early_time": None, "last_early_key": None, "signals_today": 0,
        "early_today": 0, "day_key": now().date().isoformat(), "closed": [],
        "open_trade": None, "signal_count": 0, "active_setup": None,
        "last_analysis": None}

def load_state() -> dict:
    s = fresh_state()
    try:
        p = Path(STATE_FILE)
        if p.exists():
            v = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(v, dict): s.update(v)
    except Exception: pass
    if s.get("day_key") != now().date().isoformat():
        s["day_key"] = now().date().isoformat(); s["signals_today"] = 0; s["early_today"] = 0
    if not isinstance(s.get("closed"), list): s["closed"] = []
    return s
state = load_state()

def save_state() -> None:
    try:
        p = Path(STATE_FILE); tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(p)
    except Exception as e: state["last_error"] = f"State save error: {e}"

def tg_send(message: str, parse_mode: Optional[str] = "HTML") -> bool:
    if not BOT_TOKEN or not TELEGRAM_CHAT_ID: return False
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": True}
    if parse_mode: payload["parse_mode"] = parse_mode
    try:
        r = session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=TIMEOUT)
        return r.ok and bool(r.json().get("ok"))
    except Exception: return False

def tg_photo(path: str, caption: str = "") -> bool:
    if not BOT_TOKEN or not TELEGRAM_CHAT_ID: return False
    try:
        with open(path, "rb") as f:
            r = session.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1000], "parse_mode": "HTML"},
                files={"photo": f}, timeout=30)
        return r.ok and bool(r.json().get("ok"))
    except Exception: return False

def http_json(url: str, params: Optional[dict] = None) -> Any:
    if not url: raise RuntimeError("Не задан URL XAUS API")
    r = session.get(url, params=params, timeout=TIMEOUT); r.raise_for_status(); data = r.json()
    if not isinstance(data, (dict, list)): raise RuntimeError("XAUS вернул неожиданный JSON")
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(str(data.get("message") or data.get("error") or "XAUS API error"))
    return data

def check_fresh(payload: Any, max_age: int, label: str) -> Optional[datetime]:
    if not isinstance(payload, dict): raise RuntimeError(f"XAUS {label}: ответ не объект JSON")
    ds = payload.get("data_state") or {}; status = ds.get("status") if isinstance(ds, dict) else None
    if status not in (None, "fresh"): raise RuntimeError(f"XAUS status={status}")
    raw = payload.get("price_as_of") or (ds.get("as_of") if isinstance(ds, dict) else None) or payload.get("updated_at")
    if raw is None and isinstance(payload.get("spot"), dict):
        sp = payload["spot"]; raw = sp.get("price_as_of") or sp.get("updated_at") or sp.get("t")
    dt = parse_time(raw) if raw is not None else None
    if raw is not None and dt is None: raise RuntimeError(f"XAUS {label}: некорректное время данных")
    if dt:
        age = (now()-dt).total_seconds()
        if age > max_age or age < -60: raise RuntimeError(f"XAUS {label}: данные устарели ({int(age)} сек.)")
    return dt

def extract_price(payload: Any) -> tuple[float, Optional[datetime]]:
    objects = [payload]
    if isinstance(payload, dict):
        objects += [payload[k] for k in ("data", "result", "spot", "quote", "ticker", "xau") if k in payload]
    for o in objects:
        if isinstance(o, (int, float, str)):
            try:
                p = float(o)
                if math.isfinite(p) and p > 0: return p, None
            except Exception: pass
        if isinstance(o, dict):
            for k in ("price", "p", "last", "last_price", "spot_usd_oz", "value", "close", "rate", "ask"):
                if o.get(k) is not None:
                    try:
                        p = float(o[k])
                        if math.isfinite(p) and p > 0:
                            ts = next((parse_time(o.get(t)) for t in ("t","time","timestamp","datetime","updated_at","date","price_as_of") if o.get(t) is not None and parse_time(o.get(t))), None)
                            return p, ts
                    except Exception: pass
    raise RuntimeError("Не удалось распознать цену XAUS spot")

def extract_points(payload: Any) -> tuple[pd.DataFrame, bool]:
    rows = payload if isinstance(payload, list) else None
    if isinstance(payload, dict):
        rows = next((payload[k] for k in ("points","data","history","candles","result","prices") if isinstance(payload.get(k), list)), None)
    if not rows: raise RuntimeError("XAUS intraday не вернул список точек")
    parsed = []; has_ohlc = True
    for item in rows:
        try:
            if isinstance(item, dict):
                ts = next((parse_time(item.get(k)) for k in ("t","time","timestamp","datetime","date") if item.get(k) is not None and parse_time(item.get(k))), None)
                cv = next((item[k] for k in ("close","c","p","price","value","last") if item.get(k) is not None), None)
                close = float(cv)
                real = all(item.get(k) is not None for k in ("open","high","low")) or all(item.get(k) is not None for k in ("o","h","l"))
                op = float(item.get("open", item.get("o", close))); hi = float(item.get("high", item.get("h", close))); lo = float(item.get("low", item.get("l", close)))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                ts = parse_time(item[0]); close = float(item[-1]); op=hi=lo=close; real=False
            else: continue
            if not ts: continue
            if close <= 0 or not math.isfinite(close): continue
            if not (isinstance(item, dict) and real): has_ohlc = False
            parsed.append({"time":ts,"open":op,"high":max(hi,op,close),"low":min(lo,op,close),"close":close})
        except Exception: continue
    df = pd.DataFrame(parsed)
    if df.empty: raise RuntimeError("В XAUS intraday нет точек с корректным временем и ценой")
    df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
    if not has_ohlc:
        # Honest proxy candles for visual context only; not true exchange OHLC.
        prev = df["close"].shift(1).fillna(df["close"])
        df["open"] = prev
        wiggle = (df["close"]-df["open"]).abs()*0.1
        df["high"] = np.maximum(df["close"], df["open"])+wiggle
        df["low"] = np.minimum(df["close"], df["open"])-wiggle
    return df, has_ohlc

def fetch_market() -> tuple[float, pd.DataFrame, Optional[datetime], bool]:
    sp = http_json(SPOT_URL, {"compact":"1", "fresh":int(time.time())})
    st = check_fresh(sp, 180, "spot"); price, pt = extract_price(sp); st = st or pt
    ip = http_json(INTRADAY_URL, {"symbol":"xau", "hours":48})
    it = check_fresh(ip, 300, "intraday"); df, real_ohlc = extract_points(ip)
    if len(df) < 100: raise RuntimeError(f"Мало точек XAUS intraday: {len(df)}; нужно минимум 100")
    dt = st or it or df["time"].iloc[-1]; age=(now()-dt).total_seconds()
    if age > DATA_MAX_AGE or age < -60: raise RuntimeError(f"XAUS status=stale: данные старше {int(age)} сек.")
    # Update latest sample's close to spot, but never claim that this creates real OHLC.
    df.loc[df.index[-1], "close"] = price
    df.loc[df.index[-1], "high"] = max(float(df.iloc[-1]["high"]), price)
    df.loc[df.index[-1], "low"] = min(float(df.iloc[-1]["low"]), price)
    return price, df, dt, real_ohlc

def resample_ohlc(df: pd.DataFrame, mins: int) -> pd.DataFrame:
    x=df.copy().set_index("time").sort_index()
    return x[["open","high","low","close"]].resample(f"{mins}min").agg({"open":"first","high":"max","low":"min","close":"last"}).dropna().reset_index()
def ema(s: pd.Series, n: int) -> pd.Series: return s.ewm(span=n, adjust=False).mean()
def atr(df: pd.DataFrame, n: int=14) -> pd.Series:
    pc=df["close"].shift(1); tr=pd.concat([df["high"]-df["low"],(df["high"]-pc).abs(),(df["low"]-pc).abs()],axis=1).max(axis=1)
    return tr.rolling(n,min_periods=max(3,n//2)).mean().bfill()
def bias(df: pd.DataFrame) -> str:
    if len(df)<10:return "NEUTRAL"
    c=df["close"]; e20=ema(c,min(20,max(3,len(c)//3))).iloc[-1]; e50=ema(c,min(50,max(5,len(c)//2))).iloc[-1]
    move=float(c.iloc[-1]-c.iloc[-min(6,len(c))]); threshold=max(float(atr(df).iloc[-1])*0.08,0.02)
    if c.iloc[-1]>e20>e50 and move>threshold:return "BUY"
    if c.iloc[-1]<e20<e50 and move < -threshold:return "SELL"
    return "NEUTRAL"
def strength(df: pd.DataFrame) -> tuple[int,int]:
    if len(df)<10:return 50,50
    c=df["close"].tail(12).to_numpy(); up=float(np.maximum(np.diff(c),0).sum()); dn=float(np.maximum(-np.diff(c),0).sum()); a=max(float(atr(df).iloc[-1]),0.01)
    du=max(0.,float(c[-1]-c[max(0,len(c)-5)])); dd=max(0.,float(c[max(0,len(c)-5)]-c[-1]))
    buy=50+28*(up-dn)/max(up+dn,a)+min(15,5*du/a)-min(15,5*dd/a); buy=int(max(0,min(100,round(buy))))
    return buy,100-buy

def find_sweep_mss(df15: pd.DataFrame, saved: Optional[dict]=None) -> dict:
    """Find a liquidity sweep and keep a valid candidate for up to 8 closed M15 bars."""
    out={"direction":None,"sweep":False,"mss":False,"level":None,"sweep_time":None,"reason":"нет подтверждённого снятия ликвидности (sweep)"}
    if len(df15)<25: out["reason"]="недостаточно свечей M15"; return out
    closed=df15.iloc[:-1].reset_index(drop=True) # current candle is still forming
    if len(closed)<20: out["reason"]="недостаточно закрытых свечей M15"; return out
    lookback=12; struct_n=6; candidates=[]
    for i in range(max(lookback,len(closed)-8),len(closed)):
        prior=closed.iloc[max(0,i-lookback):i]
        if len(prior)<8: continue
        c=closed.iloc[i]; low=float(prior.low.min()); high=float(prior.high.max())
        pre=closed.iloc[max(0,i-struct_n):i]
        if len(pre)<3: continue
        if float(c.low)<low and float(c.close)>low:
            candidates.append({"direction":"BUY","level":float(pre.high.max()),"sweep_time":pd.Timestamp(c.time).isoformat(),"sweep_index":i})
        if float(c.high)>high and float(c.close)<high:
            candidates.append({"direction":"SELL","level":float(pre.low.min()),"sweep_time":pd.Timestamp(c.time).isoformat(),"sweep_index":i})
    candidate=candidates[-1] if candidates else None
    # Persist previously detected sweep across cycles if it is still recent and not invalidated.
    if saved and saved.get("direction") in ("BUY","SELL") and saved.get("level") is not None:
        try:
            t=pd.Timestamp(saved.get("sweep_time")); age_bars=int(((pd.Timestamp(closed.time.iloc[-1])-t).total_seconds())//900)
            if 0 <= age_bars <= 8 and (candidate is None or pd.Timestamp(candidate["sweep_time"]) < t):
                candidate={"direction":saved["direction"],"level":float(saved["level"]),"sweep_time":saved.get("sweep_time"),"sweep_index":max(0,len(closed)-1-age_bars)}
        except Exception: pass
    if not candidate:return out
    direction=candidate["direction"]; level=float(candidate["level"]); sweep_time=candidate["sweep_time"]
    out.update({"direction":direction,"sweep":True,"level":level,"sweep_time":sweep_time})
    try: sweep_idx=next(i for i,t in enumerate(closed.time) if pd.Timestamp(t).isoformat()==pd.Timestamp(sweep_time).isoformat())
    except Exception: sweep_idx=max(0,len(closed)-2)
    # Need at least one later closed candle for MSS; a sweep candle alone is not confirmation.
    if sweep_idx >= len(closed)-1:
        out["reason"]=f"sweep {'SSL' if direction=='BUY' else 'BSL'} найден; ждём следующую закрытую M15-свечу"
        return out
    last=closed.iloc[-1]; close=float(last.close); op=float(last.open)
    if direction=="BUY" and close>level and close>op:
        out.update({"mss":True,"reason":"sweep SSL + последующее закрытие выше структуры"})
    elif direction=="SELL" and close<level and close<op:
        out.update({"mss":True,"reason":"sweep BSL + последующее закрытие ниже структуры"})
    else:
        out["reason"]=f"sweep {'SSL' if direction=='BUY' else 'BSL'} найден; ждём закрытие M15 {'выше' if direction=='BUY' else 'ниже'} {level:.2f}"
    return out

def fib_context(h1: pd.DataFrame, price: float) -> str:
    if len(h1)<20:return "нет завершённого импульса"
    w=h1.iloc[-20:-1]; li=int(w.low.idxmin()); hi_i=int(w.high.idxmax()); lo=float(h1.loc[li,"low"]); high=float(h1.loc[hi_i,"high"])
    if high<=lo:return "нет завершённого импульса"
    if li<hi_i: levels=[(r,high-(high-lo)*r) for r in (.382,.5,.618,.786)]; d="BUY"
    else: levels=[(r,lo+(high-lo)*r) for r in (.382,.5,.618,.786)]; d="SELL"
    ratio,lev=min(levels,key=lambda x:abs(x[1]-price)); near="рядом" if abs(lev-price)<=max(3.,abs(high-lo)*.15) else "далеко"
    return f"импульс {d}; Fib {ratio*100:.1f}% {lev:.2f} · {near}"

def analyze(price: float, raw: pd.DataFrame, real_ohlc: bool=True) -> dict:
    m5=resample_ohlc(raw,5); m15=resample_ohlc(raw,15); h1=resample_ohlc(raw,60); h4=resample_ohlc(raw,240); d1=resample_ohlc(raw,1440)
    b5,b15,b1,b4=(bias(x) for x in (m5,m15,h1,h4)); bd=bias(d1) if len(d1)>=3 else "NEUTRAL"
    buyers,sellers=strength(m15); a15=float(atr(m15).iloc[-1]) if len(m15)>=5 else float(atr(raw).iloc[-1])
    setup=find_sweep_mss(m15,state.get("active_setup")); fib=fib_context(h1,price)
    direction=setup["direction"]; score=0; reasons=[]
    core=b4 if b4!="NEUTRAL" else b1; conflict=bool(bd not in ("NEUTRAL",core) and core!="NEUTRAL")
    if direction:
        for tf,val,yes,no in (("H4",b4,20,12),("H1",b1,20,8),("M15",b15,12,5)):
            if val==direction: score+=yes; reasons.append(f"{tf} согласован")
            elif val!="NEUTRAL": score-=no; reasons.append(f"{tf} против сетапа")
        if setup["sweep"]: score+=18; reasons.append("liquidity sweep")
        if setup["mss"]: score+=20; reasons.append("MSS подтверждён закрытием")
        strength_value=buyers if direction=="BUY" else sellers; score+=int(max(0,min(10,(strength_value-50)/5)))
        if conflict: score-=10; reasons.append("D1 против направления (−10)")
        if b5==direction: score+=5; reasons.append("M5 согласован")
        score=max(0,min(100,score))
    full=bool(direction and setup["sweep"] and setup["mss"] and score>=MIN_SCORE)
    early=bool(direction and setup["sweep"] and not setup["mss"])
    if not setup["sweep"]: reason="нет подтверждённого снятия ликвидности (sweep)"
    elif not setup["mss"]: reason=setup["reason"]
    elif not full: reason=f"MSS есть, но score {score}/100 ниже порога {MIN_SCORE}"
    else: reason=""
    # Candidate is persisted only when sweep was detected; expired candidates are cleared.
    active=None
    if setup["sweep"] and setup.get("sweep_time"):
        try:
            age=(pd.Timestamp(m15.iloc[-2]["time"])-pd.Timestamp(setup["sweep_time"])).total_seconds()/900
            if age<=8 and not setup["mss"]: active={k:setup.get(k) for k in ("direction","level","sweep_time")}
        except Exception: pass
    return {"price":price,"bias":{"D1":bd,"H4":b4,"H1":b1,"M15":b15,"M5":b5},"buyers":buyers,"sellers":sellers,"atr":a15,"fib":fib,"setup":setup,"direction":direction,"score":score,"full":full,"early":early,"reasons":reasons,"no_setup":reason,"trend_conflict":conflict,"m5":m5,"m15":m15,"h1":h1,"real_ohlc":real_ohlc,"active_setup":active}

def arrow(v:str)->str:return {"BUY":"▲","SELL":"▼","NEUTRAL":"•"}.get(v,"•")
def analysis_text(a:dict)->str:
    b=a["bias"]
    lines=[f"🥇 <b>GOLD SMART V{VERSION} · анализ</b>",f"XAUUSD spot <b>{a['price']:.2f}</b>",f"Bias D1 {arrow(b['D1'])}  H4 {arrow(b['H4'])}  H1 {arrow(b['H1'])}  M15 {arrow(b['M15'])}",f"ATR M15 {a['atr']:.2f}",f"Сила покупателей: {a['buyers']}/100",f"Сила продавцов: {a['sellers']}/100",f"Fibonacci H1: {html.escape(a['fib'])}"]
    if not a["real_ohlc"]: lines.append("⚠️ XAUS прислал точки цены без настоящего OHLC; структура свечей приблизительная.")
    if a["full"]: lines += [f"✅ <b>FULL {a['direction']} · score {a['score']}/100</b>",f"Причины: {html.escape(', '.join(a['reasons']))}"]
    elif a["early"]: lines += [f"🟡 <b>EARLY {a['direction']} · предварительно</b>",f"Sweep найден, MSS ещё не подтверждён. Контрольный уровень: {a['setup']['level']:.2f}"]
    else: lines.append(f"Сетапа нет: {html.escape(a['no_setup'])}")
    if a["setup"].get("level") is not None: lines.append(f"Уровень MSS: {a['setup']['level']:.2f}")
    return "\n".join(lines)

def build_chart(a:dict)->str:
    df=a["m15"].tail(50).copy()
    if df.empty: raise RuntimeError("Недостаточно данных для графика")
    fig,ax=plt.subplots(figsize=(12,6),dpi=130); x=np.arange(len(df))
    for i,r in enumerate(df.itertuples()):
        color="#16a34a" if r.close>=r.open else "#dc2626"; ax.vlines(i,r.low,r.high,color=color,linewidth=1)
        ax.add_patch(plt.Rectangle((i-.28,min(r.open,r.close)),.56,max(abs(r.close-r.open),.02),color=color,alpha=.9))
    ax.plot(x,ema(df.close,20),linewidth=1.2,label="EMA 20"); ax.plot(x,ema(df.close,50),linewidth=1.2,label="EMA 50")
    ax.axhline(a["price"],linestyle="--",linewidth=1,label=f"Spot {a['price']:.2f}")
    if a["setup"].get("level") is not None: ax.axhline(a["setup"]["level"],linestyle=":",linewidth=1.5,label=f"MSS {a['setup']['level']:.2f}")
    ax.set_title(f"GOLD SMART V{VERSION} | XAUUSD M15 | score {a['score']}/100"); ax.set_ylabel("Price"); ax.grid(alpha=.2); ax.legend(loc="upper left",ncol=3,fontsize=8)
    ticks=np.linspace(0,len(df)-1,min(7,len(df))).astype(int); ax.set_xticks(ticks); ax.set_xticklabels([pd.Timestamp(df.iloc[i].time).strftime("%m-%d %H:%M") for i in ticks],rotation=25,ha="right")
    fig.tight_layout(); path="/tmp/gold_smart_chart.png"; fig.savefig(path); plt.close(fig); return path

def reset_day():
    day=now().date().isoformat()
    if state.get("day_key")!=day: state.update({"day_key":day,"signals_today":0,"early_today":0})
def open_trade(a:dict)->dict:
    e=float(a["price"]); d=a["direction"]; av=max(float(a["atr"]),.1); dist=min(MAX_SL,max(MIN_SL,av*.8))+av*SL_BUFFER_ATR
    sl=e-dist if d=="BUY" else e+dist; risk=abs(e-sl); tp1=e+risk*TP1_R if d=="BUY" else e-risk*TP1_R; tp2=e+risk*TP2_R if d=="BUY" else e-risk*TP2_R
    return {"id":f"{int(time.time())}-{d}","direction":d,"entry":e,"sl":sl,"tp1":tp1,"tp2":tp2,"risk_distance":risk,"risk_cash":DEPOSIT*RISK_PERCENT/100,"score":int(a["score"]),"opened_at":iso_now(),"tp1_hit":False,"be":False,"status":"OPEN","source":"VIRTUAL"}
def close_trade(t:dict,status:str,r:float,price:float,why:str):
    item=dict(t); item.update({"status":status,"result_r":float(r),"close_price":float(price),"closed_at":iso_now(),"close_reason":why}); state.setdefault("closed",[]).append(item); state["closed"]=state["closed"][-500:]; state["open_trade"]=None; save_state()
def update_trade(price:float)->Optional[str]:
    t=state.get("open_trade")
    if not t or t.get("status")!="OPEN": return None
    buy=t["direction"]=="BUY"
    if (buy and price<=t["sl"]) or (not buy and price>=t["sl"]):
        r=-1.0 if not t.get("tp1_hit") else 0.0; close_trade(t,"LOSS" if r<0 else "BE",r,price,"SL/BE")
        return f"🛑 <b>Виртуальная сделка закрыта</b>\n{t['direction']} · {price:.2f}\nРезультат {r:+.2f}R"
    if not t.get("tp1_hit") and ((buy and price>=t["tp1"]) or (not buy and price<=t["tp1"])):
        t["tp1_hit"]=True; t["be"]=True; t["sl"]=t["entry"]; save_state()
        return f"🎯 <b>TP1 достигнут · виртуально</b>\n{t['direction']} · {price:.2f}\nМодель: 50% зафиксировано, SL остатка в BE {t['entry']:.2f}."
    if t.get("tp1_hit") and ((buy and price>=t["tp2"]) or (not buy and price<=t["tp2"])):
        close_trade(t,"WIN",1.75,price,"TP2"); return f"🏁 <b>Виртуальная сделка закрыта по TP2</b>\n{t['direction']} · {price:.2f}\nРезультат +1.75R (модель)"
    save_state(); return None

def stats_text()->str:
    closed=state.get("closed",[]); w=sum(x.get("status")=="WIN" for x in closed); l=sum(x.get("status")=="LOSS" for x in closed); be=sum(x.get("status")=="BE" for x in closed); total=sum(float(x.get("result_r",0)) for x in closed); today=now().date().isoformat(); td=[x for x in closed if str(x.get("closed_at","")).startswith(today)]
    return f"📊 <b>GOLD SMART V{VERSION} · статистика</b>\nЗакрыто: {len(closed)}\nWin {w} / Loss {l} / BE {be}\nWinrate (без BE): {100*w/max(1,w+l):.1f}%\nСумма: {total:+.2f}R\nСегодня: {sum(float(x.get('result_r',0)) for x in td):+.2f}R\nFULL сегодня: {state.get('signals_today',0)} · EARLY сегодня: {state.get('early_today',0)}\nРежим: виртуальный · AUTO_TRADING=OFF"
def status_text()->str:
    p=state.get("last_price"); t=state.get("open_trade")
    return "\n".join([f"🥇 <b>GOLD SMART V{VERSION}</b>","Engine: ON",f"Последний цикл: {state.get('last_success') or 'ещё не было'}",f"Цена: {p:.2f}" if isinstance(p,(int,float)) else "Цена: нет данных",f"Данные: {state.get('last_data_time') or 'нет'}",f"Ошибка: {state.get('last_error') or 'нет'}",f"FULL сигналов всего: {state.get('signal_count',0)}",f"Виртуальная сделка: {t['direction']} @ {t['entry']:.2f}" if t else "Виртуальная сделка: нет","AUTO_TRADING: OFF"])
def help_text()->str:return "🤖 <b>Команды GOLD SMART</b>\n/start — описание\n/status — состояние\n/signal — текущий анализ + график\n/test — тест Telegram\n/stats — статистика\n/help — команды\n\nEARLY — предварительное предупреждение, не готовый вход. FULL требует sweep + MSS + score. Только виртуальный режим."

def process_command(text:str, chat_id:str):
    if TELEGRAM_CHAT_ID and str(chat_id)!=str(TELEGRAM_CHAT_ID): return
    cmd=(text or "").strip().split()[0].split("@")[0].lower() if text else ""
    if cmd=="/start": tg_send("🥇 <b>GOLD SMART запущен</b>\n"+help_text())
    elif cmd=="/help": tg_send(help_text())
    elif cmd=="/status": tg_send(status_text())
    elif cmd=="/stats": tg_send(stats_text())
    elif cmd=="/test": tg_send(f"✅ Telegram работает. GOLD SMART V{VERSION}\nAUTO_TRADING=OFF")
    elif cmd=="/signal":
        try:
            p,raw,dt,real=fetch_market(); a=analyze(p,raw,real); state["active_setup"]=a["active_setup"]; tg_send(analysis_text(a))
            try: tg_photo(build_chart(a),"📈 GOLD SMART · M15 technical chart")
            except Exception: pass
        except Exception as e: tg_send(f"⚠️ Не удалось получить анализ: {html.escape(str(e))}")

def poll_commands():
    if not BOT_TOKEN:return
    offset=None
    while not stop_event.is_set():
        try:
            params={"timeout":20}
            if offset is not None: params["offset"]=offset
            r=session.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",params=params,timeout=25)
            if r.ok:
                for u in r.json().get("result",[]):
                    offset=max(offset or 0,int(u["update_id"])+1); m=u.get("message") or u.get("edited_message")
                    if m and m.get("text"): process_command(m["text"],str((m.get("chat") or {}).get("id","")))
        except Exception: time.sleep(3)

def cycle_once():
    reset_day(); state["last_cycle"]=iso_now(); price,raw,dt,real=fetch_market(); a=analyze(price,raw,real)
    state.update({"last_price":float(price),"last_data_time":dt.isoformat() if dt else iso_now(),"last_success":iso_now(),"last_error":None,"active_setup":a["active_setup"],"last_analysis":{"price":price,"direction":a["direction"],"score":a["score"],"sweep":a["setup"]["sweep"],"mss":a["setup"]["mss"],"reason":a["no_setup"],"real_ohlc":real}})
    closed_msg=update_trade(price)
    if closed_msg: tg_send(closed_msg)
    # EARLY is a distinct heads-up and never opens a virtual trade.
    if a["early"]:
        key=f"EARLY:{a['direction']}:{a['setup'].get('sweep_time')}:{round(float(a['setup'].get('level') or 0),2)}"
        lt=parse_time(state.get("last_early_time")); cooldown=(not lt or (now()-lt).total_seconds()>=EARLY_COOLDOWN_MIN*60)
        if key!=state.get("last_early_key") and cooldown and int(state.get("early_today",0))<8:
            sent=tg_send(f"🟡 <b>GOLD SMART V{VERSION} · EARLY {a['direction']}</b>\nXAUUSD {price:.2f} · score {a['score']}/100\nSweep найден, MSS пока не подтверждён. Уровень: {float(a['setup']['level']):.2f}\n⚠️ Предупреждение для наблюдения, НЕ готовый вход. AUTO_TRADING=OFF.")
            if sent: state["last_early_time"]=iso_now(); state["last_early_key"]=key; state["early_today"]=int(state.get("early_today",0))+1
    if a["full"]:
        key=f"FULL:{a['direction']}:{a['setup'].get('sweep_time')}:{round(float(a['setup'].get('level') or 0),2)}"; lt=parse_time(state.get("last_signal_time")); cooldown=(not lt or (now()-lt).total_seconds()>=FULL_COOLDOWN_MIN*60)
        if key!=state.get("last_signal_key") and cooldown and int(state.get("signals_today",0))<MAX_SIGNALS_DAY and state.get("open_trade") is None:
            losses=sum(abs(float(x.get("result_r",0))) for x in state.get("closed",[]) if str(x.get("closed_at","")).startswith(now().date().isoformat()) and float(x.get("result_r",0))<0)
            if losses<MAX_LOSS_R_DAY:
                trade=open_trade(a); state.update({"open_trade":trade,"last_signal_time":iso_now(),"last_signal_key":key,"signals_today":int(state.get("signals_today",0))+1,"signal_count":int(state.get("signal_count",0))+1})
                tg_send(f"🚨 <b>GOLD SMART V{VERSION} · FULL {a['direction']}</b>\nXAUUSD: <b>{price:.2f}</b> · score {a['score']}/100\nEntry: {trade['entry']:.2f}\nSL: {trade['sl']:.2f}\nTP1: {trade['tp1']:.2f} (1R)\nTP2: {trade['tp2']:.2f} (2.5R)\nRisk model: {RISK_PERCENT:.2f}% of ${DEPOSIT:.0f}\nПричины: {html.escape(', '.join(a['reasons']))}\n⚠️ ВИРТУАЛЬНЫЙ сигнал, не ордер. AUTO_TRADING=OFF.")
                try: tg_photo(build_chart(a),"📈 Подтверждённый FULL setup · M15")
                except Exception: pass
    save_state()
def worker_loop():
    while not stop_event.is_set():
        try:
            with lock: cycle_once()
        except Exception as e:
            with lock: state["last_error"]=f"{type(e).__name__}: {e}"; state["last_cycle"]=iso_now(); save_state()
        stop_event.wait(POLL_SECONDS)
def start_background():
    if app.config.get("BACKGROUND_STARTED"):return
    app.config["BACKGROUND_STARTED"]=True
    threading.Thread(target=worker_loop,name="gold-smart-worker",daemon=True).start()
    threading.Thread(target=poll_commands,name="gold-smart-telegram",daemon=True).start()
@app.get("/")
def home():return jsonify({"bot":"GOLD SMART","version":VERSION,"engine":"ON","auto_trading":False,"mode":"virtual_only","last_success":state.get("last_success"),"last_error":state.get("last_error")})
@app.get("/health")
def health():return jsonify({"ok":True,"version":VERSION,"auto_trading":False})
@app.get("/status")
def web_status():return jsonify({"version":VERSION,"engine":"ON","auto_trading":False,"last_cycle":state.get("last_cycle"),"last_success":state.get("last_success"),"last_error":state.get("last_error"),"last_price":state.get("last_price"),"last_data_time":state.get("last_data_time"),"signals_today":state.get("signals_today"),"early_today":state.get("early_today"),"signal_count":state.get("signal_count"),"last_analysis":state.get("last_analysis"),"open_trade":state.get("open_trade"),"closed_trades":len(state.get("closed",[]))})
@app.get("/signal")
def web_signal():
    try:
        p,raw,dt,real=fetch_market(); a=analyze(p,raw,real); state["active_setup"]=a["active_setup"]; save_state()
        return jsonify({"version":VERSION,"symbol":SYMBOL,"price":p,"data_time":dt.isoformat() if dt else None,"bias":a["bias"],"buyers":a["buyers"],"sellers":a["sellers"],"atr_m15":a["atr"],"fibonacci":a["fib"],"setup":a["setup"],"score":a["score"],"early_signal":a["early"],"full_signal":a["full"],"direction":a["direction"],"reason":a["no_setup"],"real_ohlc":real,"auto_trading":False,"message":analysis_text(a).replace("<b>","").replace("</b>","")})
    except Exception as e:return jsonify({"ok":False,"error":str(e),"auto_trading":False}),503
@app.before_request
def ensure_background():start_background()
if __name__=="__main__":
    start_background(); app.run(host="0.0.0.0",port=int(os.getenv("PORT","8080")))
