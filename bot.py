"""GOLD SMART V7.1 — XAUS-only spot/intraday SMC signal bot.
Signals and virtual trades only. No broker connection and no order-placement code.
Render: gunicorn bot:app --workers 1 --threads 4 --timeout 120
Required env: BOT_TOKEN, TELEGRAM_CHAT_ID. Optional: DEPOSIT=800, RISK_PERCENT=1,
MIN_SCORE=75, POLL_SECONDS=60, STATE_FILE=gold_state.json, RENDER_EXTERNAL_URL.
"""
import copy, hashlib, hmac, json, math, os, tempfile, threading, time, traceback
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify, request

APP = "GOLD SMART V7.1"
XAUS = "https://xaus.com/api/v1"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
STATE_FILE = os.getenv("STATE_FILE", "gold_state.json")
DEPOSIT = float(os.getenv("DEPOSIT", "800"))
RISK_PCT = float(os.getenv("RISK_PERCENT", "1"))
LOT_OZ = float(os.getenv("LOT_OZ", "100"))
MIN_SCORE = int(float(os.getenv("MIN_SCORE", "75")))
TP1_R = float(os.getenv("TP1_R", "1.0"))
TP2_R = float(os.getenv("TP2_R", "2.5"))
MIN_SL = float(os.getenv("MIN_SL", "3"))
MAX_SL = float(os.getenv("MAX_SL", "12"))
SL_BUFFER_ATR = float(os.getenv("SL_BUFFER_ATR", "0.25"))
POLL = max(30, int(float(os.getenv("POLL_SECONDS", "60"))))
COOLDOWN = max(0, float(os.getenv("COOLDOWN_MIN", "30"))) * 60
MAX_SIG_DAY = int(float(os.getenv("MAX_SIGNALS_DAY", "4")))
MAX_LOSS_R = float(os.getenv("MAX_DAILY_LOSS_R", "2"))
MAX_TRADE_H = float(os.getenv("MAX_TRADE_HOURS", "12"))
SESSION_FILTER = os.getenv("SESSION_FILTER", "1") == "1"
SESSION_START = int(float(os.getenv("SESSION_START", "7")))
SESSION_END = int(float(os.getenv("SESSION_END", "20")))
KEEPALIVE = os.getenv("KEEPALIVE", "1") == "1"
AUTOSTART = not bool(os.getenv("GOLD_NO_START"))
START_DELAY = float(os.getenv("START_DELAY", "8"))
SECRET = hashlib.sha256(("gold7:" + BOT_TOKEN).encode()).hexdigest()[:48]
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "GOLD-SMART/7.1 (+XAUS API client)"})
app = Flask(__name__)

DEFAULT = {"trade": None, "muted": False, "last_signal_ts": 0.0, "last_setup": "",
          "stats": {"total": 0, "wins": 0, "losses": 0, "be": 0, "r": 0.0},
          "day": {"date": "", "signals": 0, "r": 0.0}, "history": []}
S = copy.deepcopy(DEFAULT)
STATE_LOCK = threading.RLock(); RUN_LOCK = threading.Lock(); STOP = threading.Event()
META = {"started": False, "pid": None, "ok": None, "try": None, "err": None,
        "price": None, "price_as_of": None, "data_state": None, "fails": 0}
CACHE = {"intraday": None, "spot": None, "history": None}


def utcnow(): return datetime.now(timezone.utc)
def stamp(dt=None): return (dt or utcnow()).strftime("%Y-%m-%d %H:%M UTC")
def api_get(path, params=None, timeout=20):
    r = HTTP.get(f"{XAUS}/{path.lstrip('/')}", params=params, timeout=timeout)
    r.raise_for_status(); data = r.json()
    if not isinstance(data, dict): raise ValueError(f"XAUS {path}: unexpected response")
    return data

def send(message, chat=None):
    target = str(chat or CHAT_ID)
    if not BOT_TOKEN or not target:
        print("Telegram not configured: BOT_TOKEN / TELEGRAM_CHAT_ID missing"); return False
    try:
        r = HTTP.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": target, "text": str(message)[:4000]}, timeout=20)
        if not r.ok: print("Telegram error", r.status_code, r.text[:250])
        return r.ok
    except requests.RequestException as e: print("Telegram exception", repr(e)); return False

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f: data = json.load(f)
    except (OSError, ValueError): data = {}
    with STATE_LOCK:
        S.clear(); S.update(copy.deepcopy(DEFAULT))
        if isinstance(data, dict):
            for k, v in data.items():
                if k in ("stats", "day") and isinstance(v, dict): S[k].update(v)
                elif k in DEFAULT: S[k] = v

def save_state():
    with STATE_LOCK:
        directory = os.path.dirname(os.path.abspath(STATE_FILE)); os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".goldsmart_", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f: json.dump(S, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception:
            try: os.remove(tmp)
            except OSError: pass
            raise

def roll_day(now):
    day = now.strftime("%Y-%m-%d")
    if S["day"].get("date") != day: S["day"] = {"date": day, "signals": 0, "r": 0.0}

def freshness_ok(payload, max_age=300):
    state = payload.get("data_state") or {}
    if state.get("status") not in (None, "fresh"): return False, f"XAUS status={state.get('status')}"
    asof = payload.get("price_as_of") or state.get("as_of") or payload.get("updated_at")
    if asof:
        try:
            dt = pd.Timestamp(asof)
            if dt.tzinfo is None: dt = dt.tz_localize("UTC")
            age = (pd.Timestamp.now(tz="UTC") - dt).total_seconds()
            if age > max_age or age < -60: return False, f"XAUS data age {age:.0f}s"
        except Exception: return False, "bad XAUS timestamp"
    return True, None

def fetch_spot():
    d = api_get("spot", {"compact": "1", "fresh": int(time.time())})
    ok, why = freshness_ok(d, 180)
    price = d.get("spot_usd_oz")
    if price is None and isinstance(d.get("xau"), dict): price = d["xau"].get("price")
    if not ok: raise ValueError(why)
    if not isinstance(price, (int, float)) or not math.isfinite(float(price)) or float(price) <= 0:
        raise ValueError("XAUS spot price missing/invalid")
    return float(price), d

def fetch_intraday():
    d = api_get("intraday", {"symbol": "xau", "hours": 48})
    ok, why = freshness_ok(d, 300)
    if not ok: raise ValueError(why)
    points = d.get("points") or []
    rows = []
    for p in points:
        try:
            raw_t = p.get("t")
            if isinstance(raw_t, (int, float)) or (isinstance(raw_t, str) and raw_t.strip().replace(".", "", 1).isdigit()):
                numeric_t = float(raw_t)
                # Accept Unix seconds or milliseconds; XAUS may serialize timestamps either way.
                unit = "ms" if numeric_t > 100_000_000_000 else "s"
                t = pd.to_datetime(numeric_t, unit=unit, utc=True, errors="raise")
            else:
                t = pd.to_datetime(raw_t, utc=True, errors="raise")
            v = float(p.get("p"))
            if math.isfinite(v) and v > 0: rows.append((t, v))
        except (ValueError, TypeError): continue
    if len(rows) < 100: raise ValueError(f"XAUS intraday points too few: {len(rows)}/100")
    df = pd.DataFrame(rows, columns=["time", "price"]).drop_duplicates("time").set_index("time").sort_index()
    # XAUS records spot samples, not exchange OHLC candles. OHLC below is sampled-price aggregation.
    ohlc = df["price"].resample("5min").ohlc().dropna()
    ohlc.columns = ["open", "high", "low", "close"]
    # Require a candle to have a real sample in its interval; never forward-fill missing candles.
    counts = df["price"].resample("5min").count()
    ohlc = ohlc[counts.reindex(ohlc.index).fillna(0) >= 1]
    return ohlc, d

def fetch_daily_history():
    d = api_get("history")
    pts = d.get("points") or []
    rows = []
    for p in pts:
        try:
            day = pd.to_datetime(p.get("d"), utc=True, errors="raise")
            c = float(p.get("c")); h = float(p.get("h", c)); l = float(p.get("l", c))
            if c > 0 and h >= l > 0: rows.append((day, h, l, c))
        except (ValueError, TypeError): continue
    if len(rows) < 20: raise ValueError(f"XAUS daily history too short: {len(rows)}")
    df = pd.DataFrame(rows, columns=["time", "high", "low", "close"]).drop_duplicates("time").set_index("time").sort_index()
    # Daily API documents H/L/C but not open. Previous close is used as a proxy open only for broad D1 context.
    df["open"] = df["close"].shift(1).fillna(df["close"])
    return df[["open", "high", "low", "close"]], d

def closed(df, minutes, now): return df[df.index + pd.Timedelta(minutes=minutes) <= pd.Timestamp(now)]
def resample_ohlc(df, rule):
    x = df.resample(rule, label="left", closed="left").agg({"open":"first","high":"max","low":"min","close":"last"}).dropna()
    return x

def build_frames(now):
    now_ts = time.time()
    if CACHE["intraday"] and now_ts - CACHE["intraday"][0] < 45:
        m5_all, intraday_payload = CACHE["intraday"][1], CACHE["intraday"][2]
    else:
        m5_all, intraday_payload = fetch_intraday(); CACHE["intraday"] = (now_ts, m5_all, intraday_payload)
    if CACHE["spot"] and now_ts - CACHE["spot"][0] < 20:
        price, spot_payload = CACHE["spot"][1], CACHE["spot"][2]
    else:
        price, spot_payload = fetch_spot(); CACHE["spot"] = (now_ts, price, spot_payload)
    m5 = closed(m5_all, 5, now)
    m15 = closed(resample_ohlc(m5_all, "15min"), 15, now)
    h1 = closed(resample_ohlc(m5_all, "1h"), 60, now)
    h4 = closed(resample_ohlc(m5_all, "4h"), 240, now)
    # D1 is independent XAUS daily context, but its synthetic open is explicitly only a proxy.
    if CACHE["history"] and now_ts - CACHE["history"][0] < 1800:
        d1, hist_payload = CACHE["history"][1], CACHE["history"][2]
    else:
        d1, hist_payload = fetch_daily_history(); CACHE["history"] = (now_ts, d1, hist_payload)
    return {"M5":m5, "M15":m15, "H1":h1, "H4":h4, "D1":d1}, m5_all, price, spot_payload, intraday_payload

def ema(series, n): return series.ewm(span=n, adjust=False).mean()
def atr(df, n=14):
    pc = df.close.shift(1)
    tr = pd.concat([df.high-df.low, (df.high-pc).abs(), (df.low-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()
def rsi(series, n=14):
    d=series.diff(); up=d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean(); dn=(-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return (100-100/(1+up/dn.replace(0,np.nan))).fillna(50)
def swings(df, n=2):
    hi, lo = df.high.to_numpy(), df.low.to_numpy(); sh=[]; sl=[]
    for i in range(n, len(df)-n):
        if hi[i] > hi[i-n:i].max() and hi[i] >= hi[i+1:i+n+1].max(): sh.append((i,float(hi[i])))
        if lo[i] < lo[i-n:i].min() and lo[i] <= lo[i+1:i+n+1].min(): sl.append((i,float(lo[i])))
    return sh,sl
def bias(df, min_bars=8):
    if len(df) < min_bars: return 0
    c=df.close; short=min(8,max(3,len(df)//3)); long_n=min(21,max(short+2,len(df)//2))
    e1,e2,last=float(ema(c,short).iloc[-1]),float(ema(c,long_n).iloc[-1]),float(c.iloc[-1])
    score = 1 if last>e1>e2 else (-1 if last<e1<e2 else 0)
    sh,sl=swings(df.tail(120),2)
    if len(sh)>=2 and len(sl)>=2:
        if sh[-1][1]>sh[-2][1] and sl[-1][1]>sl[-2][1]: score+=1
        elif sh[-1][1]<sh[-2][1] and sl[-1][1]<sl[-2][1]: score-=1
    return max(-2,min(2,score))
def liquidity_pools(m15,h1,now):
    lows=[]; highs=[]; today=pd.Timestamp(now).floor("D")
    if len(m15):
        by_day={d:g for d,g in m15.groupby(m15.index.floor("D"))}; prev=[d for d in sorted(by_day) if d<today and len(by_day[d])>=10]
        if prev:
            g=by_day[prev[-1]]; highs.append(("PDH",float(g.high.max()),3)); lows.append(("PDL",float(g.low.min()),3))
        if now.hour>=7 and today in by_day:
            a=by_day[today].between_time("00:00","06:59")
            if len(a)>=5: highs.append(("Asia H",float(a.high.max()),2)); lows.append(("Asia L",float(a.low.min()),2))
    for df,name,w,k in ((h1.tail(120),"H1 swing",2,3),(m15.tail(160),"M15 swing",1,3)):
        sh,sl=swings(df,2); highs += [(name,p,w) for _,p in sh[-k:]]; lows += [(name,p,w) for _,p in sl[-k:]]
    return {"low":lows,"high":highs}
def find_sweep(m5,pools,side,window=18):
    if len(m5)<25: return None
    lo,hi,cl=(m5[c].to_numpy() for c in ("low","high","close")); n=len(m5); best=None
    for i in range(max(20,n-window),n-1):
        if side=="BUY":
            if lo[i]>lo[max(0,i-20):i].min(): continue
            for name,lvl,w in pools["low"]:
                if lo[i]<lvl and cl[i+1:min(n,i+4)].max()>lvl and (best is None or (w,i)>(best[3],best[0])): best=(i,name,lvl,w)
        else:
            if hi[i]<hi[max(0,i-20):i].max(): continue
            for name,lvl,w in pools["high"]:
                if hi[i]>lvl and cl[i+1:min(n,i+4)].min()<lvl and (best is None or (w,i)>(best[3],best[0])): best=(i,name,lvl,w)
    if not best: return None
    i,name,lvl,w=best; extreme=float(lo[i:].min() if side=="BUY" else hi[i:].max())
    return {"i":i,"name":name,"level":float(lvl),"w":w,"ts":m5.index[i].isoformat(),"extreme":extreme}
def find_mss(m5,side,i_sweep):
    sh,sl=swings(m5,2); close=m5.close.to_numpy()
    pivots=[p for i,p in (sh if side=="BUY" else sl) if i_sweep-20<=i<i_sweep]
    if not pivots:return None
    level=pivots[-1]
    for j in range(i_sweep+1,len(close)):
        if (close[j]>level if side=="BUY" else close[j]<level): return {"level":float(level),"j":j}
    return None
def lot_size(risk_dist):
    budget=DEPOSIT*RISK_PCT/100
    lots=max(math.floor(budget/(risk_dist*LOT_OZ)*100)/100,0.01)
    return lots,lots*LOT_OZ*risk_dist

def analyze(fr,price,now):
    m5,m15,h1,h4,d1=(fr[k] for k in ("M5","M15","H1","H4","D1"))
    out={"price":price,"why":[],"entry":None,"ok":False,"bias":{}}
    # 48h XAUS samples can provide about 12 H4 candles. Do not pretend this is a long H4 history.
    needs={"M5":80,"M15":30,"H1":12,"H4":6,"D1":20}
    for k,n in needs.items():
        if len(fr[k])<n: out["why"].append(f"недостаточно данных {k}: {len(fr[k])}/{n}"); return out
    age=(now-(m5.index[-1]+pd.Timedelta(minutes=5))).total_seconds()/60
    if age>15: out["why"].append("свечи XAUS устарели"); return out
    b4,b1,b15,bd=bias(h4,6),bias(h1,8),bias(m15,12),bias(d1,20)
    out["bias"]={"H4":b4,"H1":b1,"M15":b15,"D1":bd}
    # D1 uses documented daily H/L/C and a proxy open; it is contextual only, not treated as exact OHLC.
    if b4>=1 and b1>=1 and bd>=0: side="BUY"
    elif b4<=-1 and b1<=-1 and bd<=0: side="SELL"
    else: out["why"].append("H4/H1/D1 не согласованы"); return out
    sgn=1 if side=="BUY" else -1
    if SESSION_FILTER and not (SESSION_START<=now.hour<SESSION_END): out["why"].append(f"вне сессии {SESSION_START}-{SESSION_END} UTC"); return out
    a15=float(atr(m15).iloc[-1]); out["atr15"]=a15
    if not math.isfinite(a15) or a15<=0: out["why"].append("ATR M15 недоступен"); return out
    pools=liquidity_pools(m15,h1,now); sw=find_sweep(m5,pools,side)
    if not sw: out["why"].append("нет подтверждённого sweep ликвидности"); return out
    mss=find_mss(m5,side,sw["i"])
    if not mss: out["why"].append(f"sweep {sw['name']} есть, MSS не подтверждён"); return out
    if mss["j"]<len(m5)-1-2: out["why"].append("MSS устарел — вход упущен"); return out
    entry=float(price); sl=sw["extreme"]-sgn*SL_BUFFER_ATR*a15; risk=(entry-sl)*sgn
    if risk<=0: out["why"].append("цена уже за SL"); return out
    if risk<MIN_SL: sl=entry-sgn*MIN_SL; risk=MIN_SL
    if risk>MAX_SL: out["why"].append(f"SL слишком широкий: {risk:.2f}$"); return out
    tp1=entry+sgn*TP1_R*risk; tp2=entry+sgn*TP2_R*risk
    target_pool=pools["high"] if side=="BUY" else pools["low"]
    targets=[p for p in target_pool if p[2]>=2 and (p[1]-entry)*sgn>0]
    target=min(targets,key=lambda p:abs(p[1]-entry),default=None)
    if target and abs(target[1]-entry)<1.5*risk: out["why"].append(f"цель {target[0]} ближе 1.5R"); return out
    o,c,hi,lo=(m5[k].to_numpy() for k in ("open","close","high","low")); j=mss["j"]
    bodies=np.abs(c-o); avg=float(bodies[max(0,j-20):j].mean()) if j>0 else 0
    disp=bool(avg>0 and bodies[j]>=1.3*avg and (c[j]-o[j])*sgn>0)
    fvg=any((lo[k]>hi[k-2] if side=="BUY" else hi[k]<lo[k-2]) for k in range(max(2,j-1),min(len(m5),j+2)))
    recent=h1.tail(36); midpoint=(float(recent.high.max())+float(recent.low.min()))/2
    zone="DISCOUNT" if price<midpoint else "PREMIUM"; zone_ok=(zone=="DISCOUNT")== (side=="BUY")
    r=float(rsi(m5.close).iloc[-1]); rsi_ok=(r>52 if side=="BUY" else r<48)
    b15ok=b15*sgn>=1; killzone=(7<=now.hour<10 or 12<=now.hour<16)
    score=10*(abs(b4)+abs(b1)) + (8 if bd*sgn>=0 else 0) + (10 if b15ok else 0)
    score += {3:20,2:15,1:10}.get(sw["w"],5)+15+(8 if disp else 0)+(5 if fvg else 0)+(7 if zone_ok else 0)+(5 if rsi_ok else 0)+(5 if killzone else 0)
    score=int(min(100,score))
    out.update(side=side,score=score,entry=entry,sl=sl,tp1=tp1,tp2=tp2,risk=risk,sweep=sw,mss=mss,disp=disp,fvg=fvg,zone=zone,zone_ok=zone_ok,kz=killzone,rsi_ok=rsi_ok,b15ok=b15ok,target=target,ok=score>=MIN_SCORE)
    if not out["ok"]:out["why"].append(f"score {score} < {MIN_SCORE}")
    return out

def gate(a):
    if S["muted"]:return "сигналы выключены (/unmute)"
    if S["trade"]:return "уже есть открытая виртуальная сделка"
    if S["day"]["signals"]>=MAX_SIG_DAY:return "лимит сигналов на день"
    if S["day"]["r"]<=-MAX_LOSS_R:return "дневной лимит убытка"
    if time.time()-S["last_signal_ts"]<COOLDOWN:return "cooldown"
    if S["last_setup"]==a["sweep"]["ts"]:return "сетап уже обработан"
    return None

def new_trade(a,now):
    lots,_=lot_size(a["risk"])
    return {"side":a["side"],"entry":a["entry"],"sl":a["sl"],"sl0":a["sl"],"tp1":a["tp1"],"tp2":a["tp2"],"risk":a["risk"],"score":a["score"],"lots":lots,"opened":now.isoformat(),"cursor":pd.Timestamp(now).floor("5min").isoformat(),"tp1_hit":False,"realized_r":0.0,"remaining":1.0}
def step_trade(t,hi,lo,notes):
    sgn=1 if t["side"]=="BUY" else -1; fav,adv=(hi,lo) if sgn==1 else (lo,hi)
    if (t["sl"]-adv)*sgn>=0:return t["realized_r"]+t["remaining"]*(t["sl"]-t["entry"])*sgn/t["risk"]
    if not t["tp1_hit"] and (fav-t["tp1"])*sgn>=0:
        t["tp1_hit"]=True;t["realized_r"]+=.5*TP1_R;t["remaining"]=.5;t["sl"]=t["entry"]
        notes.append(f"🎯 {t['side']} TP1 {t['tp1']:.2f}: закрыто 50%, SL → безубыток")
    if t["tp1_hit"] and (fav-t["tp2"])*sgn>=0:return t["realized_r"]+.5*TP2_R
    return None
def finish_trade(t,r,reason=None):
    r=round(float(r),2); result="WIN" if r>.05 else ("LOSS" if r<-.05 else "BE"); st=S["stats"]
    st["total"]+=1;st[{"WIN":"wins","LOSS":"losses","BE":"be"}[result]]+=1;st["r"]=round(st["r"]+r,2);S["day"]["r"]=round(S["day"]["r"]+r,2)
    S["history"]=(S["history"]+[{"t":t["opened"],"side":t["side"],"r":r,"result":result}])[-50:];S["trade"]=None
    return f"{'🏆' if result=='WIN' else '📉' if result=='LOSS' else '🟡'} {t['side']} закрыта: {result} {r:+.2f}R"+(f" ({reason})" if reason else "")+f"\nEntry {t['entry']:.2f}\nДень: {S['day']['r']:+.2f}R"
def manage_trade(m5_all,price,now):
    notes=[]
    with STATE_LOCK:
        t=S["trade"]
        if not t:return notes
        res=None; cc=closed(m5_all,5,now)
        for ts,row in cc[cc.index>pd.Timestamp(t["cursor"])].iterrows():
            t["cursor"]=ts.isoformat();res=step_trade(t,float(row.high),float(row.low),notes)
            if res is not None:break
        if res is None:res=step_trade(t,price,price,notes)
        if res is None and (now-datetime.fromisoformat(t["opened"])).total_seconds()>MAX_TRADE_H*3600:
            sgn=1 if t["side"]=="BUY" else -1;res=t["realized_r"]+t["remaining"]*(price-t["entry"])*sgn/t["risk"]
            reason="TIMEOUT"
        else:reason=None
        if res is not None:notes.append(finish_trade(t,res,reason))
        save_state()
    return notes

def arrow(v):return "▲" if v>0 else ("▼" if v<0 else "•")
def bias_str(a):
    b=a.get("bias") or {};return "  ".join(f"{k} {arrow(b.get(k,0))}" for k in ("D1","H4","H1","M15")) or "n/a"
def build_signal(a):
    side=a["side"];lots,risk_usd=lot_size(a["risk"]);sw=a["sweep"];mss=a["mss"]
    tags=((["M15 aligned"] if a["b15ok"] else [])+(["displacement"] if a["disp"] else [])+(["FVG"] if a["fvg"] else [])+([a["zone"].lower()] if a["zone_ok"] else [])+(["momentum"] if a["rsi_ok"] else [])+(["killzone"] if a["kz"] else []))
    lines=[f"🥇 {APP}","",f"{'🟢' if side=='BUY' else '🔴'} FULL {side} XAUUSD · score {a['score']}/100","",f"Entry {a['entry']:.2f}",f"SL    {a['sl']:.2f} ({a['risk']:.2f}$)",f"TP1   {a['tp1']:.2f} ({TP1_R:g}R · 50%, затем SL → BE)",f"TP2   {a['tp2']:.2f} ({TP2_R:g}R)","",f"Lot ≈ {lots:.2f} · риск ≈ ${risk_usd:.2f} при депозите ${DEPOSIT:.0f}",f"Bias {bias_str(a)}",f"Sweep {sw['name']} {sw['level']:.2f}",f"MSS close {'выше' if side=='BUY' else 'ниже'} {mss['level']:.2f}","Подтверждения: "+(", ".join(tags) or "—")]
    if a.get("target"):lines.append(f"Цель ликвидности: {a['target'][0]} {a['target'][1]:.2f}")
    if risk_usd>DEPOSIT*RISK_PCT/100*1.05:lines.append("⚠️ Минимальный лот даёт риск выше бюджета — проверь объём у брокера.")
    lines += ["","Источник: XAUS Spot + XAUS Intraday samples.","⚠️ Это ориентировочные spot-данные, не котировка брокера.","AUTO TRADING: OFF — реальные ордера не выставляются."]
    return "\n".join(lines)
def build_report(a):
    lines=[f"🥇 {APP} · анализ",f"XAUUSD spot {a['price']:.2f}",f"Bias {bias_str(a)}"]
    if a.get("atr15") is not None:lines.append(f"ATR M15 (sampled) {a['atr15']:.2f}")
    if a.get("entry") is not None:
        lines += ["",build_signal(a),""]
        lines.append("✅ сетап прошёл фильтр" if a["ok"] else "⛔ не отправлен: "+"; ".join(a["why"]))
    else:lines += ["","Сетапа нет: "+"; ".join(a["why"])]
    return "\n".join(lines)
def cmd_start():return f"🥇 {APP}\nXAUS spot · D1 context → H4/H1 → M15/M5 SMC\n\n/start /help — помощь\n/status — статус\n/signal — анализ сейчас\n/test — проверка XAUS\n/trade — виртуальная сделка\n/stats — статистика\n/mute /unmute — пауза сигналов\n\nТолько FULL-сигналы. AUTO TRADING: OFF."
def cmd_status():
    with STATE_LOCK:return f"🥇 {APP}\nEngine: {'ON' if META['started'] else 'starting'}\nПоследний успешный цикл: {META['ok'] or 'нет'}\nПоследняя попытка: {META['try'] or 'нет'}\nЦена XAUS: {META['price'] if META['price'] is not None else 'n/a'}\nPrice as of: {META['price_as_of'] or 'n/a'}\nData state: {META['data_state'] or 'n/a'}\nОшибка: {META['err'] or 'нет'}\nСигналы: {'ВЫКЛ' if S['muted'] else 'вкл'}\nСегодня: {S['day']['signals']} сигналов, {S['day']['r']:+.2f}R\nВиртуальная сделка: {'да' if S['trade'] else 'нет'}\nAUTO TRADING: OFF"
def cmd_stats():
    with STATE_LOCK:
        st=S['stats'];n=st['total'];return f"📊 {APP}\nЗакрыто: {n}\nWin {st['wins']} / Loss {st['losses']} / BE {st['be']}\nWinrate: {st['wins']/n*100 if n else 0:.1f}%\nСумма: {st['r']:+.2f}R\nСредняя: {st['r']/n if n else 0:+.2f}R\nСегодня: {S['day']['r']:+.2f}R"
def cmd_trade():
    with STATE_LOCK:
        t=S['trade']
        if not t:return "Открытой виртуальной сделки нет."
        p=META['price'];sgn=1 if t['side']=='BUY' else -1;live=f"\nТекущая цена {p:.2f}" if p is not None else ""
        return f"{t['side']} {t['lots']:.2f} lot · score {t['score']}\nEntry {t['entry']:.2f}\nSL {t['sl']:.2f}\nTP1 {t['tp1']:.2f} {'✅' if t['tp1_hit'] else ''}\nTP2 {t['tp2']:.2f}{live}\nОткрыта {t['opened'][:16]} UTC"
def cmd_test():
    p,d=fetch_spot(); intr=api_get("intraday",{"symbol":"xau","hours":48}); pts=len(intr.get("points") or []); state=(d.get("data_state") or {}).get("status","unknown")
    return f"✅ XAUS API доступен\nSpot: {p:.2f}\nData state: {state}\nIntraday samples: {pts}\nAUTO TRADING: OFF"
def cmd_signal():
    a=market_cycle(open_new=False);return build_report(a) if a else f"⚠️ нет данных XAUS: {META['err'] or 'попробуй позже'}"
def cmd_mute(flag):
    with STATE_LOCK:S['muted']=flag;save_state()
    return "🔕 сигналы выключены, сопровождение сделки продолжается." if flag else "🔔 сигналы включены."
def handle_update(update):
    m=update.get('message') or update.get('edited_message') or {};chat=(m.get('chat') or {}).get('id');txt=(m.get('text') or '').strip()
    if chat is None or not txt:return
    cmd=txt.split()[0].split('@')[0].lower()
    if not CHAT_ID:
        if cmd in ('/start','/help'):send(f"Твой chat id: {chat}\nУкажи его в TELEGRAM_CHAT_ID и перезапусти.",chat)
        return
    if str(chat)!=CHAT_ID:return
    table={'/start':cmd_start,'/help':cmd_start,'/status':cmd_status,'/signal':cmd_signal,'/test':cmd_test,'/stats':cmd_stats,'/trade':cmd_trade,'/mute':lambda:cmd_mute(True),'/unmute':lambda:cmd_mute(False)}
    try:reply=table.get(cmd,lambda:"Неизвестная команда. /help")()
    except Exception as e:traceback.print_exc();reply=f"⚠️ ошибка команды: {type(e).__name__}: {e}"
    send(reply,chat)

def market_cycle(open_new=True):
    if not RUN_LOCK.acquire(blocking=not open_new):return None
    try:
        now=utcnow();META['try']=stamp(now)
        fr,m5_all,price,spot_payload,intra_payload=build_frames(now)
        META['price']=price;META['price_as_of']=spot_payload.get('price_as_of') or (spot_payload.get('data_state') or {}).get('as_of');META['data_state']=(spot_payload.get('data_state') or {}).get('status','unknown')
        with STATE_LOCK:roll_day(now)
        for note in manage_trade(m5_all,price,now):send(note)
        a=analyze(fr,price,now);a['blocked']=None
        if a.get('entry') is not None and a.get('ok'):
            with STATE_LOCK:a['blocked']=gate(a)
        if open_new and a.get('ok') and not a.get('blocked'):
            if send(build_signal(a)):
                with STATE_LOCK:S['trade']=new_trade(a,now);S['day']['signals']+=1;S['last_signal_ts']=time.time();S['last_setup']=a['sweep']['ts'];save_state()
            else:print('signal not delivered; virtual trade not opened')
        META['ok']=stamp(now);META['err']=None;META['fails']=0
        return a
    except Exception as e:
        META['err']=f"{type(e).__name__}: {e}";META['fails']+=1;traceback.print_exc()
        if META['fails'] in (3,5,10):send(f"⚠️ {APP}: XAUS/engine failure {META['fails']} cycles\n{META['err']}")
        return None
    finally:RUN_LOCK.release()
def engine_loop(stop):
    print(f"{APP}: engine started pid={os.getpid()}")
    while not stop.is_set():
        try:market_cycle(True)
        except Exception:traceback.print_exc()
        stop.wait(POLL)
def poll_loop(stop):
    try:HTTP.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook",json={"drop_pending_updates":False},timeout=15)
    except requests.RequestException:pass
    offset=None
    while not stop.is_set():
        try:
            r=HTTP.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",params={"timeout":25,"offset":offset},timeout=35)
            if not r.ok:stop.wait(4);continue
            for u in r.json().get('result',[]):offset=u['update_id']+1;handle_update(u)
        except Exception:stop.wait(4)
def keepalive_loop(stop):
    while not stop.wait(240):
        try:HTTP.get(PUBLIC_URL+"/health",timeout=10)
        except requests.RequestException as e:print('keepalive',repr(e))
def configure_webhook():
    try:
        r=HTTP.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",json={"url":PUBLIC_URL+"/telegram","secret_token":SECRET},timeout=20)
        print('setWebhook',r.status_code,r.text[:200])
    except requests.RequestException as e:print('setWebhook error',repr(e))
def start():
    with STATE_LOCK:
        if META['started']:return
        META['started']=True
    META['pid']=os.getpid();load_state()
    if BOT_TOKEN and PUBLIC_URL:configure_webhook()
    threading.Thread(target=engine_loop,args=(STOP,),daemon=True,name='engine').start()
    if BOT_TOKEN and not PUBLIC_URL:threading.Thread(target=poll_loop,args=(STOP,),daemon=True,name='poll').start()
    if PUBLIC_URL and KEEPALIVE:threading.Thread(target=keepalive_loop,args=(STOP,),daemon=True,name='keepalive').start()
    if BOT_TOKEN and CHAT_ID:send(f"🟢 {APP} online\nИсточник: XAUS Spot + Intraday\nFULL score ≥ {MIN_SCORE} · AUTO TRADING OFF")
IS_MASTER=False
def later(delay,fn):
    t=threading.Timer(delay,fn);t.daemon=True;t.start()
def start_unless_master():
    if AUTOSTART and not IS_MASTER:start()
def fork_parent():
    global IS_MASTER;IS_MASTER=True;STOP.set()
def fork_child():
    global STATE_LOCK,RUN_LOCK,STOP,IS_MASTER
    STATE_LOCK=threading.RLock();RUN_LOCK=threading.Lock();STOP=threading.Event();META['started']=False;IS_MASTER=False
    if AUTOSTART:later(1,start)
if hasattr(os,'register_at_fork'):os.register_at_fork(after_in_parent=fork_parent,after_in_child=fork_child)
@app.get('/')
def home():return jsonify({"app":APP,"status":"online","last_ok":META['ok'],"price":META['price'],"data_state":META['data_state'],"error":META['err']})
@app.get('/health')
def health():return jsonify({"status":"ok","time":stamp()})
@app.get('/status')
def http_status():
    with STATE_LOCK:return jsonify({"app":APP,"meta":META,"trade":S['trade'],"stats":S['stats'],"day":S['day'],"pid":os.getpid(),"threads":sorted(t.name for t in threading.enumerate())})
@app.post('/telegram')
def telegram_webhook():
    if not hmac.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token',''),SECRET):return jsonify({"ok":False}),401
    threading.Thread(target=handle_update,args=(request.get_json(silent=True) or {},),daemon=True).start();return jsonify({"ok":True})
@app.before_request
def ensure_started():start_unless_master()
if AUTOSTART:later(START_DELAY,start_unless_master)
if __name__=='__main__':
    start();app.run(host='0.0.0.0',port=int(os.getenv('PORT','10000')))
