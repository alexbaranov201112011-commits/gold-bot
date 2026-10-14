"""
GOLD SMART V7.5 — XAUS spot/intraday SMC signals + Telegram chart.
Virtual tracking only. AUTO_TRADING is permanently OFF: no broker connection or order placement.
Render start: gunicorn bot:app --workers 1 --threads 4 --timeout 120
Required env: BOT_TOKEN, TELEGRAM_CHAT_ID. Optional: DEPOSIT, RISK_PERCENT, MIN_SCORE,
POLL_SECONDS, STATE_FILE, RENDER_EXTERNAL_URL, SESSION_FILTER.
"""
import copy, hashlib, hmac, io, json, math, os, tempfile, threading, time, traceback
from datetime import datetime, timezone
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify, request

APP = "GOLD SMART V7.5"
XAUS = "https://xaus.com/api/v1"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
STATE_FILE = os.getenv("STATE_FILE", "gold_state.json")
def env_float(name, default, low=None, high=None):
    try: value = float(os.getenv(name, str(default)))
    except (ValueError, TypeError): value = float(default)
    if not math.isfinite(value): value = float(default)
    if low is not None: value = max(low, value)
    if high is not None: value = min(high, value)
    return value
def env_int(name, default, low=None, high=None):
    return int(env_float(name, default, low, high))
DEPOSIT = env_float("DEPOSIT", 800, 1)
RISK_PCT = env_float("RISK_PERCENT", 1, .1, 2)
LOT_OZ = env_float("LOT_OZ", 100, .01)
MIN_SCORE = env_int("MIN_SCORE", 75, 75, 100)
TP1_R = env_float("TP1_R", 1, .25, 5)
TP2_R = env_float("TP2_R", 2.5, 1, 10)
MIN_SL = env_float("MIN_SL", 3, .1)
MAX_SL = env_float("MAX_SL", 12, MIN_SL)
POLL = env_int("POLL_SECONDS", 60, 30, 3600)
COOLDOWN = env_float("COOLDOWN_MIN", 30, 0) * 60
MAX_SIG_DAY = env_int("MAX_SIGNALS_DAY", 4, 1)
MAX_LOSS_R = env_float("MAX_DAILY_LOSS_R", 2, .1)
MAX_TRADE_H = env_float("MAX_TRADE_HOURS", 12, 1)
SESSION_FILTER = os.getenv("SESSION_FILTER", "1") == "1"
SESSION_START = env_int("SESSION_START", 7, 0, 23)
SESSION_END = env_int("SESSION_END", 20, 1, 24)
KEEPALIVE = os.getenv("KEEPALIVE", "1") == "1"
AUTOSTART = not bool(os.getenv("GOLD_NO_START"))
START_DELAY = env_float("START_DELAY", 8, 0)
SECRET = hashlib.sha256(("gold7:" + BOT_TOKEN).encode()).hexdigest()[:48]
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "GOLD-SMART/7.5 (+XAUS API client)"})
app = Flask(__name__)
DEFAULT = {"trade": None, "muted": False, "last_signal_ts": 0.0, "last_setup": "",
           "stats": {"total": 0, "wins": 0, "losses": 0, "be": 0, "r": 0.0},
           "day": {"date": "", "signals": 0, "r": 0.0}, "history": []}
S = copy.deepcopy(DEFAULT)
STATE_LOCK, RUN_LOCK, STOP = threading.RLock(), threading.Lock(), threading.Event()
META = {"started": False, "pid": None, "ok": None, "try": None, "err": None,
        "price": None, "price_as_of": None, "data_state": None, "fails": 0}
CACHE = {"intraday": None, "spot": None, "history": None}

def utcnow(): return datetime.now(timezone.utc)
def stamp(dt=None): return (dt or utcnow()).strftime("%Y-%m-%d %H:%M UTC")
def api_get(path, params=None, timeout=20):
    r = HTTP.get(f"{XAUS}/{path.lstrip('/')}", params=params, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    if not isinstance(d, dict): raise ValueError(f"XAUS {path}: unexpected response")
    return d
def send(message, chat=None):
    target = str(chat or CHAT_ID)
    if not BOT_TOKEN or not target: return False
    try:
        r = HTTP.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                      json={"chat_id": target, "text": str(message)[:4000]}, timeout=20)
        if not r.ok: print("Telegram error", r.status_code, r.text[:250])
        return r.ok
    except requests.RequestException as e:
        print("Telegram exception", repr(e)); return False
def send_photo(photo_bytes, caption="", chat=None):
    target = str(chat or CHAT_ID)
    if not BOT_TOKEN or not target: return False
    try:
        r = HTTP.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                      data={"chat_id": target, "caption": caption[:1000]},
                      files={"photo": ("gold_smart_chart.png", photo_bytes, "image/png")}, timeout=35)
        if not r.ok: print("Telegram sendPhoto error", r.status_code, r.text[:250])
        return r.ok
    except requests.RequestException as e:
        print("Telegram sendPhoto exception", repr(e)); return False
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
        # Repair malformed/older state rather than letting a cycle crash.
        if not isinstance(S.get("stats"), dict): S["stats"] = copy.deepcopy(DEFAULT["stats"])
        if not isinstance(S.get("day"), dict): S["day"] = copy.deepcopy(DEFAULT["day"])
        for k, v in DEFAULT["stats"].items(): S["stats"].setdefault(k, v)
        for k, v in DEFAULT["day"].items(): S["day"].setdefault(k, v)
        if not isinstance(S.get("history"), list): S["history"] = []
        if not isinstance(S.get("trade"), (dict, type(None))): S["trade"] = None
def save_state():
    with STATE_LOCK:
        directory = os.path.dirname(os.path.abspath(STATE_FILE)); os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".goldsmart_", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(S, f, ensure_ascii=False, indent=2, allow_nan=False)
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
            dt = dt.tz_localize("UTC") if dt.tzinfo is None else dt.tz_convert("UTC")
            age = (pd.Timestamp.now(tz="UTC") - dt).total_seconds()
            if age > max_age or age < -60: return False, f"XAUS data age {age:.0f}s"
        except Exception: return False, "bad XAUS timestamp"
    return True, None
def fetch_spot():
    d = api_get("spot", {"compact": "1", "fresh": int(time.time())})
    ok, why = freshness_ok(d, 180); p = d.get("spot_usd_oz")
    if p is None and isinstance(d.get("xau"), dict): p = d["xau"].get("price")
    if not ok: raise ValueError(why)
    if not isinstance(p, (int, float)) or not math.isfinite(float(p)) or float(p) <= 0:
        raise ValueError("XAUS spot price missing/invalid")
    return float(p), d
def parse_time(raw):
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().replace(".", "", 1).isdigit()):
        v = float(raw)
        return pd.to_datetime(v, unit="ms" if v > 100_000_000_000 else "s", utc=True, errors="raise")
    return pd.to_datetime(raw, utc=True, errors="raise")
def fetch_intraday():
    d = api_get("intraday", {"symbol": "xau", "hours": 48})
    ok, why = freshness_ok(d, 300)
    if not ok: raise ValueError(why)
    rows = []
    for p in d.get("points") or []:
        try:
            t, v = parse_time(p.get("t")), float(p.get("p"))
            if math.isfinite(v) and v > 0: rows.append((t, v))
        except (ValueError, TypeError, OverflowError): continue
    df = pd.DataFrame(rows, columns=["time", "price"]).drop_duplicates("time").set_index("time").sort_index()
    if len(df) < 100: raise ValueError(f"XAUS intraday points too few: {len(df)}/100")
    ohlc = df.price.resample("5min").ohlc().dropna()
    ohlc.columns = ["open", "high", "low", "close"]
    counts = df.price.resample("5min").count()
    return ohlc[counts.reindex(ohlc.index).fillna(0) >= 1], d
def fetch_daily_history():
    d = api_get("history"); rows = []
    for p in d.get("points") or []:
        try:
            day = pd.to_datetime(p.get("d"), utc=True, errors="raise")
            c = float(p.get("c")); h = float(p.get("h", c)); l = float(p.get("l", c))
            if all(math.isfinite(x) for x in (c, h, l)) and c > 0 and h >= l > 0:
                rows.append((day, h, l, c))
        except (ValueError, TypeError): continue
    if len(rows) < 20: raise ValueError(f"XAUS daily history too short: {len(rows)}")
    df = pd.DataFrame(rows, columns=["time", "high", "low", "close"]).drop_duplicates("time").set_index("time").sort_index()
    df["open"] = df.close.shift(1).fillna(df.close)
    return df[["open", "high", "low", "close"]], d
def closed(df, minutes, now):
    if df.empty: return df.copy()
    return df[df.index + pd.Timedelta(minutes=minutes) <= pd.Timestamp(now)]
def resample_ohlc(df, rule):
    return df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
def build_frames(now):
    ts = time.time()
    if CACHE["intraday"] and ts - CACHE["intraday"][0] < 45: m5_all, intra = CACHE["intraday"][1:]
    else: m5_all, intra = fetch_intraday(); CACHE["intraday"] = (ts, m5_all, intra)
    if CACHE["spot"] and ts - CACHE["spot"][0] < 20: price, spot = CACHE["spot"][1:]
    else: price, spot = fetch_spot(); CACHE["spot"] = (ts, price, spot)
    if CACHE["history"] and ts - CACHE["history"][0] < 1800: d1, hist = CACHE["history"][1:]
    else: d1, hist = fetch_daily_history(); CACHE["history"] = (ts, d1, hist)
    fr = {"M5": closed(m5_all, 5, now), "M15": closed(resample_ohlc(m5_all, "15min"), 15, now),
          "H1": closed(resample_ohlc(m5_all, "1h"), 60, now),
          "H4": closed(resample_ohlc(m5_all, "4h"), 240, now), "D1": d1}
    return fr, m5_all, price, spot, intra
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def atr(df, n=14):
    pc = df.close.shift(1)
    tr = pd.concat([df.high-df.low, (df.high-pc).abs(), (df.low-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()
def rsi(s, n=14):
    d = s.diff(); up = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return (100-100/(1+up/dn.replace(0, np.nan))).fillna(50)
def swings(df, n=2):
    hi, lo = df.high.to_numpy(), df.low.to_numpy(); sh, sl = [], []
    for i in range(n, len(df)-n):
        if hi[i] > hi[i-n:i].max() and hi[i] >= hi[i+1:i+n+1].max(): sh.append((i, float(hi[i])))
        if lo[i] < lo[i-n:i].min() and lo[i] <= lo[i+1:i+n+1].min(): sl.append((i, float(lo[i])))
    return sh, sl
def bias(df, min_bars=8):
    if len(df) < min_bars: return 0
    c = df.close; short = min(8, max(3, len(df)//3)); long_n = min(21, max(short+2, len(df)//2))
    e1, e2, last = float(ema(c, short).iloc[-1]), float(ema(c, long_n).iloc[-1]), float(c.iloc[-1])
    score = 1 if last > e1 > e2 else (-1 if last < e1 < e2 else 0)
    sh, sl = swings(df.tail(120), 2)
    if len(sh) >= 2 and len(sl) >= 2:
        if sh[-1][1] > sh[-2][1] and sl[-1][1] > sl[-2][1]: score += 1
        elif sh[-1][1] < sh[-2][1] and sl[-1][1] < sl[-2][1]: score -= 1
    return max(-2, min(2, score))
def liquidity_pools(m15, h1, now):
    lows, highs = [], []; today = pd.Timestamp(now).floor("D")
    if len(m15):
        by_day = {d:g for d,g in m15.groupby(m15.index.floor("D"))}
        prev = [d for d in sorted(by_day) if d < today and len(by_day[d]) >= 10]
        if prev:
            g = by_day[prev[-1]]; highs.append(("PDH", float(g.high.max()), 3)); lows.append(("PDL", float(g.low.min()), 3))
        if now.hour >= 7 and today in by_day:
            a = by_day[today].between_time("00:00", "06:59")
            if len(a) >= 5: highs.append(("Asia H", float(a.high.max()), 2)); lows.append(("Asia L", float(a.low.min()), 2))
    for df, name, w, k in ((h1.tail(120), "H1 swing", 2, 3), (m15.tail(160), "M15 swing", 1, 3)):
        sh, sl = swings(df, 2); highs += [(name,p,w) for _,p in sh[-k:]]; lows += [(name,p,w) for _,p in sl[-k:]]
    return {"low": lows, "high": highs}
def find_sweep(m5, pools, side, window=24):
    if len(m5) < 25: return None
    lo, hi, cl = (m5[c].to_numpy(dtype=float) for c in ("low", "high", "close")); n = len(m5); best = None
    # Only completed bars are supplied. Require a later close back through the swept level.
    for i in range(max(20, n-window-3), n-1):
        if side == "BUY" and lo[i] >= lo[max(0,i-10):i].min(): continue
        if side == "SELL" and hi[i] <= hi[max(0,i-10):i].max(): continue
        pools_side = pools["low"] if side == "BUY" else pools["high"]
        for name, lvl, w in pools_side:
            end = min(n, i+4)
            returned = bool(len(cl[i+1:end]) and ((cl[i+1:end] > lvl).any() if side == "BUY" else (cl[i+1:end] < lvl).any()))
            swept = lo[i] < lvl if side == "BUY" else hi[i] > lvl
            if swept and returned and (best is None or (w,i) > (best[3],best[0])): best = (i,name,float(lvl),w)
    if not best: return None
    i,name,lvl,w = best
    return {"i":i,"name":name,"level":lvl,"w":w,"ts":m5.index[i].isoformat(),
            "extreme":float(lo[i] if side=="BUY" else hi[i])}
def find_mss(m5, side, i_sweep):
    sh, sl = swings(m5, 2); close = m5.close.to_numpy()
    pivots = [p for i,p in (sh if side=="BUY" else sl) if i_sweep-20 <= i < i_sweep]
    if not pivots: return None
    level = pivots[-1]
    for j in range(i_sweep+1, len(close)):
        if (close[j] > level if side=="BUY" else close[j] < level):
            return {"level":float(level),"j":j}
    return None
def structure_strength(df, side):
    if len(df) < 20: return 50, {"structure":"insufficient","pullback":None,"momentum":0}
    x=df.tail(80); sh,sl=swings(x,2); c=x.close.to_numpy(); o=x.open.to_numpy()
    hi=x.high.to_numpy(); lo=x.low.to_numpy(); score=50; structure="mixed"
    if len(sh)>=2 and len(sl)>=2:
        hh=sh[-1][1]>sh[-2][1]; hl=sl[-1][1]>sl[-2][1]
        lh=sh[-1][1]<sh[-2][1]; ll=sl[-1][1]<sl[-2][1]
        if side=="BUY":
            score += 18 if hh and hl else (-18 if lh and ll else (7 if hl else 0))
            structure = "HH/HL" if hh and hl else ("LH/LL" if lh and ll else "mixed")
        else:
            score += 18 if lh and ll else (-18 if hh and hl else (7 if lh else 0))
            structure = "LH/LL" if lh and ll else ("HH/HL" if hh and hl else "mixed")
    bodies=np.abs(c-o); recent=float(np.mean((c[-5:]-o[-5:])*(1 if side=="BUY" else -1)))
    base=float(np.mean(bodies[-25:-5])) if len(bodies)>25 else float(np.mean(bodies[:-5]))
    if recent>0: score+=10
    if base>0 and recent>=base*.7: score+=8
    elif base>0 and recent<0: score-=8
    rng=max(1e-9,float(np.max(hi[-12:])-np.min(lo[-12:])))
    pull=(float(np.max(hi[-12:])-c[-1]) if side=="BUY" else float(c[-1]-np.min(lo[-12:])))/rng
    if pull<.35: score+=7
    elif pull>.65: score-=8
    pos=(c[-1]-float(np.min(lo[-12:])))/rng
    if (side=="BUY" and pos>=.7) or (side=="SELL" and pos<=.3): score+=7
    elif (side=="BUY" and pos<=.3) or (side=="SELL" and pos>=.7): score-=7
    return int(max(0,min(100,score))), {"structure":structure,"pullback":round(pull,2),"momentum":round(recent,3)}
def fibonacci_context(df, price, side):
    sh,sl=swings(df.tail(100).reset_index(drop=True),2)
    if not sh or not sl: return {"available":False,"text":"недостаточно swing-точек","level":None}
    if sh[-1][0] > sl[-1][0]:
        hi_i,hi=sh[-1]; earlier=[z for z in sl if z[0]<hi_i]
        if not earlier: return {"available":False,"text":"нет завершённого импульса","level":None}
        lo_i,lo=earlier[-1]; direction="SELL"
    else:
        lo_i,lo=sl[-1]; later=[z for z in sh if z[0]>lo_i]
        if not later: return {"available":False,"text":"нет завершённого импульса","level":None}
        hi_i,hi=later[-1]; direction="BUY"
    if hi<=lo: return {"available":False,"text":"невалидный диапазон","level":None}
    levels={"38.2%":float(hi-(hi-lo)*.382),"50.0%":float(hi-(hi-lo)*.5),
            "61.8%":float(hi-(hi-lo)*.618),"78.6%":float(hi-(hi-lo)*.786)}
    name,val=min(levels.items(),key=lambda z:abs(price-z[1])); tol=max(float(atr(df).iloc[-1])*.25,.35)
    near=abs(price-val)<=tol
    return {"available":True,"direction":direction,"levels":levels,"nearest":name,"value":val,
            "near":near,"aligned":direction==side and near,
            "text":f"импульс {direction}; ближе к Fib {name} ({val:.2f})"+(" · рядом" if near else " · далеко"),
            "swing_low":lo,"swing_high":hi}
def lot_size(risk_dist):
    if not all(math.isfinite(x) and x>0 for x in (DEPOSIT,RISK_PCT,LOT_OZ,risk_dist)): return 0.0,0.0
    budget=DEPOSIT*RISK_PCT/100; raw=budget/(risk_dist*LOT_OZ)
    lots=math.floor((raw+1e-12)*100)/100
    if lots<.01: return 0.0,0.0
    return lots,lots*LOT_OZ*risk_dist
def analyze(fr, price, now):
    m5,m15,h1,h4,d1=(fr[k] for k in ("M5","M15","H1","H4","D1"))
    out={"price":price,"why":[],"entry":None,"ok":False,"bias":{}}
    for k,n in {"M5":80,"M15":30,"H1":12,"H4":6,"D1":20}.items():
        if len(fr[k])<n: out["why"].append(f"недостаточно данных {k}: {len(fr[k])}/{n}"); return out
    age=(now-(m5.index[-1]+pd.Timedelta(minutes=5)).to_pydatetime()).total_seconds()/60
    if age>15: out["why"].append("свечи XAUS устарели"); return out
    b4,b1,b15,bd=bias(h4,6),bias(h1,8),bias(m15,12),bias(d1,20)
    out["bias"]={"H4":b4,"H1":b1,"M15":b15,"D1":bd}
    buy_strength,buy_meta=structure_strength(m15,"BUY"); sell_strength,sell_meta=structure_strength(m15,"SELL")
    out.update(buy_strength=buy_strength,sell_strength=sell_strength,buy_meta=buy_meta,sell_meta=sell_meta,
               fib=fibonacci_context(h1,price,"BUY"))
    if b4>=1 and b1>=1 and bd>=0: side="BUY"
    elif b4<=-1 and b1<=-1 and bd<=0: side="SELL"
    else: out["why"].append("H4/H1/D1 не согласованы"); return out
    if SESSION_FILTER and not (SESSION_START<=now.hour<SESSION_END):
        out["why"].append(f"вне сессии {SESSION_START}-{SESSION_END} UTC"); return out
    a15=float(atr(m15).iloc[-1]); out["atr15"]=a15
    if not math.isfinite(a15) or a15<=0: out["why"].append("ATR M15 недоступен"); return out
    pools=liquidity_pools(m15,h1,now); out["pools"]=pools; sw=find_sweep(m5,pools,side)
    if not sw: out["why"].append("нет подтверждённого sweep ликвидности"); return out
    out["sweep"]=sw; mss=find_mss(m5,side,sw["i"])
    if not mss: out["why"].append(f"sweep {sw['name']} есть, MSS не подтверждён"); return out
    out["mss"]=mss; age_bars=len(m5)-1-mss["j"]
    if age_bars>2:
        out["why"].append(f"MSS устарел — подтверждение {age_bars} закрытых M5-свечей назад"); return out
    entry=float(price); sgn=1 if side=="BUY" else -1
    # Ensure the stop is beyond the sweep extreme and on the loss side of entry.
    sl=sw["extreme"]-sgn*.25*a15; risk=(entry-sl)*sgn
    if risk<=0: out["why"].append("цена уже за SL"); return out
    if risk<MIN_SL: sl=entry-sgn*MIN_SL; risk=MIN_SL
    if risk>MAX_SL: out["why"].append(f"SL слишком широкий: {risk:.2f}$"); return out
    lots,risk_usd=lot_size(risk)
    if lots<.01: out["why"].append(f"минимальный лот превышает бюджет риска ${DEPOSIT*RISK_PCT/100:.2f}; сигнал отменён"); return out
    tp1=entry+sgn*TP1_R*risk; tp2=entry+sgn*TP2_R*risk
    target_pool=pools["high"] if side=="BUY" else pools["low"]
    targets=[p for p in target_pool if p[2]>=2 and (p[1]-entry)*sgn>0]
    target=min(targets,key=lambda p:abs(p[1]-entry),default=None)
    if target and abs(target[1]-entry)<1.5*risk:
        out["why"].append(f"цель {target[0]} ближе 1.5R"); return out
    o,c,hi,lo=(m5[k].to_numpy() for k in ("open","close","high","low"))
    j=mss["j"]; bodies=np.abs(c-o)
    avg=float(bodies[max(0,j-20):j].mean()) if j>0 else 0
    disp=bool(avg>0 and bodies[j]>=1.3*avg and (c[j]-o[j])*sgn>0)
    fvg=any((lo[k]>hi[k-2] if side=="BUY" else hi[k]<lo[k-2])
            for k in range(max(2,j-1),min(len(m5),j+2)))
    recent=h1.tail(36); mid=(float(recent.high.max())+float(recent.low.min()))/2
    zone="DISCOUNT" if price<mid else "PREMIUM"; zone_ok=(zone=="DISCOUNT")== (side=="BUY")
    rv=float(rsi(m5.close).iloc[-1]); rsi_ok=(rv>52 if side=="BUY" else rv<48)
    b15ok=b15*sgn>=1; kz=(7<=now.hour<10 or 12<=now.hour<16)
    side_strength=buy_strength if side=="BUY" else sell_strength; missing=[]
    if not disp: missing.append("нет displacement")
    if not fvg: missing.append("нет FVG")
    if not b15ok: missing.append("M15 против направления")
    if not zone_ok: missing.append("неподходящая Premium/Discount зона")
    if side_strength<52: missing.append(f"сила {'покупателей' if side=='BUY' else 'продавцов'} слабая ({side_strength}/100)")
    score=10*(abs(b4)+abs(b1))+(8 if bd*sgn>=0 else 0)+(10 if b15ok else 0)+{3:20,2:15,1:10}.get(sw["w"],5)+15+(8 if disp else 0)+(5 if fvg else 0)+(7 if zone_ok else 0)+(5 if rsi_ok else 0)+(5 if kz else 0)+(5 if fibonacci_context(h1,price,side).get("aligned") else 0)+(4 if side_strength>=65 else (2 if side_strength>=58 else 0))
    score=int(min(100,score)); ok=score>=MIN_SCORE and not missing
    out.update(side=side,score=score,entry=entry,sl=sl,tp1=tp1,tp2=tp2,risk=risk,lots=lots,risk_usd=risk_usd,
               disp=disp,fvg=fvg,zone=zone,zone_ok=zone_ok,kz=kz,rsi_ok=rsi_ok,b15ok=b15ok,target=target,
               ok=ok,side_strength=side_strength,buy_meta=buy_meta,sell_meta=sell_meta,
               fib=fibonacci_context(h1,price,side))
    out["why"].extend(missing)
    if score<MIN_SCORE: out["why"].append(f"score {score} < {MIN_SCORE}")
    return out
def gate(a):
    if S["muted"]: return "сигналы выключены (/unmute)"
    if S["trade"]: return "уже есть открытая виртуальная сделка"
    if S["day"]["signals"]>=MAX_SIG_DAY: return "лимит сигналов на день"
    if S["day"]["r"]<=-MAX_LOSS_R: return "дневной лимит убытка"
    if time.time()-float(S.get("last_signal_ts",0))<COOLDOWN: return "cooldown"
    if S.get("last_setup")==a.get("sweep",{}).get("ts"): return "сетап уже обработан"
    return None
def new_trade(a,now):
    return {"side":a["side"],"entry":a["entry"],"sl":a["sl"],"sl0":a["sl"],"tp1":a["tp1"],"tp2":a["tp2"],
            "risk":a["risk"],"score":a["score"],"lots":a["lots"],"opened":now.isoformat(),
            "cursor":pd.Timestamp(now).floor("5min").isoformat(),"tp1_hit":False,"realized_r":0.0,"remaining":1.0}
def step_trade(t, hi, lo, notes):
    sgn=1 if t["side"]=="BUY" else -1
    fav,adv=(hi,lo) if sgn==1 else (lo,hi)
    stop_hit=(adv<=t["sl"] if sgn==1 else adv>=t["sl"])
    tp1_hit=(fav>=t["tp1"] if sgn==1 else fav<=t["tp1"])
    tp2_hit=(fav>=t["tp2"] if sgn==1 else fav<=t["tp2"])
    # OHLC cannot reveal which level was hit first. Conservatively assume stop first
    # whenever stop and a target are both inside the same bar.
    if stop_hit: return t["realized_r"]+t["remaining"]*(t["sl"]-t["entry"])*sgn/t["risk"]
    if not t["tp1_hit"] and tp1_hit:
        t["tp1_hit"]=True; t["realized_r"]+=.5*TP1_R; t["remaining"]=.5; t["sl"]=t["entry"]
        notes.append(f"🎯 {t['side']} TP1 {t['tp1']:.2f}: закрыто 50%, SL → безубыток")
    if t["tp1_hit"] and tp2_hit: return t["realized_r"]+.5*TP2_R
    return None
def finish_trade(t,r,reason=None):
    r=round(float(r),2); result="WIN" if r>.05 else ("LOSS" if r<-.05 else "BE")
    st=S["stats"]; st["total"]+=1; st[{"WIN":"wins","LOSS":"losses","BE":"be"}[result]]+=1
    st["r"]=round(st["r"]+r,2); S["day"]["r"]=round(S["day"]["r"]+r,2)
    S["history"]=(S["history"]+[{"t":t["opened"],"side":t["side"],"r":r,"result":result}])[-50:]
    S["trade"]=None
    return f"{'🏆' if result=='WIN' else '📉' if result=='LOSS' else '🟡'} {t['side']} закрыта: {result} {r:+.2f}R"+(f" ({reason})" if reason else "")+f"\nEntry {t['entry']:.2f}\nДень: {S['day']['r']:+.2f}R"
def manage_trade(m5_all,price,now):
    notes=[]
    with STATE_LOCK:
        t=S["trade"]
        if not t: return notes
        res=None; cc=closed(m5_all,5,now)
        try: cursor=pd.Timestamp(t["cursor"])
        except Exception: cursor=pd.Timestamp(t["opened"]).floor("5min")
        for ts,row in cc[cc.index>cursor].iterrows():
            t["cursor"]=ts.isoformat(); res=step_trade(t,float(row.high),float(row.low),notes)
            if res is not None: break
        if res is None: res=step_trade(t,float(price),float(price),notes)
        reason=None
        if res is None and (now-datetime.fromisoformat(t["opened"])).total_seconds()>MAX_TRADE_H*3600:
            sgn=1 if t["side"]=="BUY" else -1
            res=t["realized_r"]+t["remaining"]*(price-t["entry"])*sgn/t["risk"]; reason="TIMEOUT"
        if res is not None: notes.append(finish_trade(t,res,reason))
        save_state()
    return notes
def arrow(v): return "▲" if v>0 else ("▼" if v<0 else "•")
def bias_str(a):
    b=a.get("bias") or {}; return "  ".join(f"{k} {arrow(b.get(k,0))}" for k in ("D1","H4","H1","M15")) or "n/a"
def build_signal(a):
    side=a["side"]; sw=a["sweep"]; mss=a["mss"]; fib=a.get("fib",{})
    lines=[f"🥇 {APP}","",f"{'🟢' if side=='BUY' else '🔴'} FULL {side} XAUUSD · score {a['score']}/100",
           "",f"Entry {a['entry']:.2f}",f"SL    {a['sl']:.2f} ({a['risk']:.2f}$)",
           f"TP1   {a['tp1']:.2f} ({TP1_R:g}R · 50%, затем SL → BE)",f"TP2   {a['tp2']:.2f} ({TP2_R:g}R)","",
           f"Lot ≈ {a['lots']:.2f} · расчётный риск ≈ ${a['risk_usd']:.2f} при депозите ${DEPOSIT:.0f}",
           f"Bias {bias_str(a)}",f"Сила покупателей: {a['buy_strength']}/100",f"Сила продавцов: {a['sell_strength']}/100",
           f"Структура: BUY {a['buy_meta']['structure']} · SELL {a['sell_meta']['structure']}",
           f"Sweep {sw['name']} {sw['level']:.2f}",f"MSS close {'выше' if side=='BUY' else 'ниже'} {mss['level']:.2f}",
           f"Displacement: {'да' if a['disp'] else 'нет'} · FVG: {'да' if a['fvg'] else 'нет'}",
           f"Зона: {a['zone']} · RSI: {'OK' if a['rsi_ok'] else 'не подтверждает'}",
           f"Fibonacci H1: {fib.get('text','нет данных')}"]
    if fib.get("available"): lines.append("Fib: "+" · ".join(f"{k} {v:.2f}" for k,v in fib["levels"].items()))
    if a.get("target"): lines.append(f"Цель ликвидности: {a['target'][0]} {a['target'][1]:.2f}")
    lines += ["","Источник: XAUS Spot + Intraday samples (M5 OHLC агрегированы из ценовых отсчётов).",
              "⚠️ Spot-данные могут отличаться от котировки брокера.","AUTO TRADING: OFF — реальные ордера не выставляются."]
    return "\n".join(lines)
def build_report(a):
    lines=[f"🥇 {APP} · анализ",f"XAUUSD spot {a['price']:.2f}",f"Bias {bias_str(a)}"]
    if a.get("atr15") is not None: lines.append(f"ATR M15 (sampled) {a['atr15']:.2f}")
    if a.get("buy_strength") is not None:
        lines += [f"Сила покупателей: {a['buy_strength']}/100",f"Сила продавцов: {a['sell_strength']}/100",
                  f"Fibonacci H1: {a.get('fib',{}).get('text','нет данных')}"]
    if a.get("entry") is not None:
        lines += ["",build_signal(a),"",("✅ сетап прошёл фильтр" if a["ok"] else "⛔ не отправлен: "+("; ".join(a["why"]) or "обязательные условия не выполнены"))]
    else: lines += ["","Сетапа нет: "+("; ".join(a["why"]) or "обязательные условия не выполнены")]
    if a.get("blocked"): lines.append("Блокировка: "+a["blocked"])
    return "\n".join(lines)
def render_chart(fr,price,a,now):
    df=fr.get("M15",pd.DataFrame()).tail(72).copy()
    if len(df)<5: df=fr.get("M5",pd.DataFrame()).tail(96).copy()
    if len(df)<5: raise ValueError("Недостаточно баров для графика")
    fig,ax=plt.subplots(figsize=(12,7),dpi=140); fig.subplots_adjust(left=.09,right=.97,top=.83,bottom=.16)
    x=mdates.date2num(pd.to_datetime(df.index,utc=True).to_pydatetime())
    step=float(np.median(np.diff(x))) if len(x)>1 else 1/96; bodyw=step*.62
    for xi,(_,r) in zip(x,df.iterrows()):
        up=float(r.close)>=float(r.open); color="#16856b" if up else "#d94b4b"
        ax.vlines(xi,r.low,r.high,color=color,linewidth=1.0,zorder=2)
        bottom=min(float(r.open),float(r.close)); height=max(abs(float(r.close)-float(r.open)),.015)
        ax.add_patch(Rectangle((xi-bodyw/2,bottom),bodyw,height,facecolor=color,edgecolor=color,linewidth=.6,zorder=3))
    ax.axhline(price,color="#f0a202",linewidth=1.5,linestyle="--",label=f"Current {price:.2f}")
    fib=a.get("fib",{})
    if fib.get("available"):
        for name,val in fib.get("levels",{}).items(): ax.axhline(val,linewidth=.8,linestyle=":",alpha=.65,label=f"Fib {name} {val:.2f}")
    pools=a.get("pools") or {}
    for kind,col in (("high","#9b59b6"),("low","#2d8a68")):
        for name,level,weight in pools.get(kind,[])[:8]:
            ax.axhline(level,color=col,linewidth=.75,alpha=.55,linestyle="--")
            ax.text(x[-1],level,f" {name}",fontsize=7,color=col,va="bottom")
    for i in range(2,len(df)):
        if float(df.low.iloc[i])>float(df.high.iloc[i-2]): ax.axhspan(float(df.high.iloc[i-2]),float(df.low.iloc[i]),color="#16856b",alpha=.10)
        elif float(df.high.iloc[i])<float(df.low.iloc[i-2]): ax.axhspan(float(df.high.iloc[i]),float(df.low.iloc[i-2]),color="#d94b4b",alpha=.10)
    if a.get("entry") is not None:
        for key,col in (("entry","#f0a202"),("sl","#d62728"),("tp1","#1b9e77"),("tp2","#087f5b")):
            if a.get(key) is not None: ax.axhline(a[key],color=col,linewidth=1.1,linestyle="-.",label=f"{key.upper()} {a[key]:.2f}")
    if a.get("sweep"):
        sw=a["sweep"]; ax.axhline(sw["level"],color="#6c5ce7",linestyle="--",linewidth=1.0,label=f"Sweep {sw['name']} {sw['level']:.2f}")
    ax.set_title(f"{APP} · XAUUSD spot {price:.2f}\nBias {bias_str(a)} | Buyers {a.get('buy_strength','n/a')}/100 · Sellers {a.get('sell_strength','n/a')}/100",loc="left",fontsize=12,fontweight="bold")
    ax.set_ylabel("Price (USD/oz)"); ax.grid(True,alpha=.18)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %H:%M",tz=timezone.utc)); ax.tick_params(axis="x",rotation=20,labelsize=8)
    ax.set_xlim(x[0]-step,x[-1]+step*5); ax.legend(loc="upper left",bbox_to_anchor=(0,-.20),ncol=4,fontsize=7,frameon=False)
    side=a.get("side"); status=(f"FULL {side} · score {a.get('score')}" if a.get("ok") else "WAIT · подтверждённого FULL-сетапа нет")
    why="; ".join(a.get("why",[])[:2]); fig.text(.09,.04,f"{status}   {why}\nXAUS samples aggregated to OHLC · UTC · AUTO TRADING OFF",fontsize=8)
    out=io.BytesIO(); fig.savefig(out,format="png",bbox_inches="tight",facecolor="white"); plt.close(fig); out.seek(0); return out.read()
def cmd_start():
    return f"🥇 {APP}\nXAUS spot · D1/H4/H1 bias · M15/M5 SMC\nFibonacci + buyer/seller strength + chart\n\n/start /help — помощь\n/status — статус\n/signal — анализ + график сейчас\n/test — проверка XAUS\n/trade — виртуальная сделка\n/stats — статистика\n/mute /unmute — пауза сигналов\n\nТолько FULL-сигналы. AUTO TRADING: OFF."
def cmd_status():
    with STATE_LOCK:
        return f"🥇 {APP}\nEngine: {'ON' if META['started'] else 'starting'}\nПоследний успешный цикл: {META['ok'] or 'нет'}\nПоследняя попытка: {META['try'] or 'нет'}\nЦена XAUS: {META['price'] if META['price'] is not None else 'n/a'}\nPrice as of: {META['price_as_of'] or 'n/a'}\nData state: {META['data_state'] or 'n/a'}\nОшибка: {META['err'] or 'нет'}\nСигналы: {'ВЫКЛ' if S['muted'] else 'вкл'}\nСегодня: {S['day']['signals']} сигналов, {S['day']['r']:+.2f}R\nВиртуальная сделка: {'да' if S['trade'] else 'нет'}\nAUTO TRADING: OFF"
def cmd_stats():
    with STATE_LOCK:
        st=S["stats"]; n=st["total"]
        return f"📊 {APP}\nЗакрыто: {n}\nWin {st['wins']} / Loss {st['losses']} / BE {st['be']}\nWinrate: {st['wins']/n*100 if n else 0:.1f}%\nСумма: {st['r']:+.2f}R\nСредняя: {st['r']/n if n else 0:+.2f}R\nСегодня: {S['day']['r']:+.2f}R"
def cmd_trade():
    with STATE_LOCK:
        t=S["trade"]
        if not t: return "Открытой виртуальной сделки нет."
        p=META["price"]; live=f"\nТекущая цена {p:.2f}" if p is not None else ""
        return f"{t['side']} {t['lots']:.2f} lot · score {t['score']}\nEntry {t['entry']:.2f}\nSL {t['sl']:.2f}\nTP1 {t['tp1']:.2f} {'✅' if t['tp1_hit'] else ''}\nTP2 {t['tp2']:.2f}{live}\nОткрыта {t['opened'][:16]} UTC"
def cmd_test():
    p,d=fetch_spot(); intr=api_get("intraday",{"symbol":"xau","hours":48})
    pts=len(intr.get("points") or []); state=(d.get("data_state") or {}).get("status","unknown")
    return f"✅ XAUS API доступен\nSpot: {p:.2f}\nData state: {state}\nIntraday samples: {pts}\nAUTO TRADING: OFF"
def cmd_mute(flag):
    with STATE_LOCK: S["muted"]=flag; save_state()
    return "🔕 сигналы выключены, сопровождение сделки продолжается." if flag else "🔔 сигналы включены."
def cmd_signal():
    a=market_cycle(open_new=False,manage=False)
    if not a: return f"⚠️ нет данных XAUS: {META['err'] or 'попробуй позже'}"
    report=build_report(a); send(report)
    try:
        fr,_,price,_,_=build_frames(utcnow())
        send_photo(render_chart(fr,price,a,utcnow()),"📊 Диагностический график GOLD SMART · без открытия сделки")
    except Exception as e: print("diagnostic chart error",repr(e))
    return None
def handle_update(update):
    m=update.get("message") or update.get("edited_message") or {}
    chat=(m.get("chat") or {}).get("id"); txt=(m.get("text") or "").strip()
    if chat is None or not txt: return
    cmd=txt.split()[0].split("@")[0].lower()
    if not CHAT_ID:
        if cmd in ("/start","/help"): send(f"Твой chat id: {chat}\nУкажи его в TELEGRAM_CHAT_ID и перезапусти.",chat)
        return
    if str(chat)!=CHAT_ID: return
    table={"/start":cmd_start,"/help":cmd_start,"/status":cmd_status,"/signal":cmd_signal,"/test":cmd_test,
           "/stats":cmd_stats,"/trade":cmd_trade,"/mute":lambda:cmd_mute(True),"/unmute":lambda:cmd_mute(False)}
    try: reply=table.get(cmd,lambda:"Неизвестная команда. /help")()
    except Exception as e: traceback.print_exc(); reply=f"⚠️ ошибка команды: {type(e).__name__}: {e}"
    if reply: send(reply,chat)
def market_cycle(open_new=True,manage=True):
    if not RUN_LOCK.acquire(blocking=not open_new): return None
    try:
        now=utcnow(); META["try"]=stamp(now)
        fr,m5_all,price,spot_payload,intra_payload=build_frames(now)
        META["price"]=price; META["price_as_of"]=spot_payload.get("price_as_of") or (spot_payload.get("data_state") or {}).get("as_of")
        META["data_state"]=(spot_payload.get("data_state") or {}).get("status","unknown")
        with STATE_LOCK: roll_day(now)
        if manage:
            for note in manage_trade(m5_all,price,now): send(note)
        a=analyze(fr,price,now); a["blocked"]=None; a["frame_keys"]={k:len(v) for k,v in fr.items()}
        if a.get("entry") is not None and a.get("ok"):
            with STATE_LOCK: a["blocked"]=gate(a)
        if open_new and a.get("ok") and not a.get("blocked"):
            with STATE_LOCK: blocked=gate(a)
            if not blocked and send(build_signal(a)):
                try: send_photo(render_chart(fr,price,a,now),f"📊 {APP} · FULL {a['side']} · score {a['score']}/100")
                except Exception as e: print("signal chart error",repr(e))
                with STATE_LOCK:
                    S["trade"]=new_trade(a,now); S["day"]["signals"]+=1; S["last_signal_ts"]=time.time()
                    S["last_setup"]=a["sweep"]["ts"]; save_state()
            elif blocked: a["blocked"]=blocked
            else: print("signal not delivered; virtual trade not opened")
        META["ok"]=stamp(now); META["err"]=None; META["fails"]=0; return a
    except Exception as e:
        META["err"]=f"{type(e).__name__}: {e}"; META["fails"]+=1; traceback.print_exc()
        if META["fails"] in (3,5,10): send(f"⚠️ {APP}: XAUS/engine failure {META['fails']} cycles\n{META['err']}")
        return None
    finally: RUN_LOCK.release()
def engine_loop(stop):
    print(f"{APP}: engine started pid={os.getpid()}")
    while not stop.is_set():
        try: market_cycle(True,True)
        except Exception: traceback.print_exc()
        stop.wait(POLL)
def poll_loop(stop):
    try: HTTP.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook",json={"drop_pending_updates":False},timeout=15)
    except requests.RequestException: pass
    offset=None
    while not stop.is_set():
        try:
            r=HTTP.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",params={"timeout":25,"offset":offset},timeout=35)
            if not r.ok: stop.wait(4); continue
            for u in r.json().get("result",[]): offset=u["update_id"]+1; handle_update(u)
        except Exception: stop.wait(4)
def keepalive_loop(stop):
    while not stop.wait(240):
        try: HTTP.get(PUBLIC_URL+"/health",timeout=10)
        except requests.RequestException as e: print("keepalive",repr(e))
def configure_webhook():
    try:
        r=HTTP.post(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                    json={"url":PUBLIC_URL+"/telegram","secret_token":SECRET},timeout=20)
        print("setWebhook",r.status_code,r.text[:200])
    except requests.RequestException as e: print("setWebhook error",repr(e))
def start():
    with STATE_LOCK:
        if META["started"]: return
        META["started"]=True
    META["pid"]=os.getpid(); load_state()
    if BOT_TOKEN and PUBLIC_URL: configure_webhook()
    threading.Thread(target=engine_loop,args=(STOP,),daemon=True,name="engine").start()
    if BOT_TOKEN and not PUBLIC_URL: threading.Thread(target=poll_loop,args=(STOP,),daemon=True,name="poll").start()
    if PUBLIC_URL and KEEPALIVE: threading.Thread(target=keepalive_loop,args=(STOP,),daemon=True,name="keepalive").start()
    if BOT_TOKEN and CHAT_ID: send(f"🟢 {APP} online\nXAUS Spot + Intraday\nFULL score ≥ {MIN_SCORE} · chart enabled · AUTO TRADING OFF")
IS_MASTER=False
def later(delay,fn):
    t=threading.Timer(delay,fn); t.daemon=True; t.start()
def start_unless_master():
    if AUTOSTART and not IS_MASTER: start()
def fork_parent():
    global IS_MASTER; IS_MASTER=True; STOP.set()
def fork_child():
    global STATE_LOCK,RUN_LOCK,STOP,IS_MASTER
    STATE_LOCK=threading.RLock(); RUN_LOCK=threading.Lock(); STOP=threading.Event(); META["started"]=False; IS_MASTER=False
    if AUTOSTART: later(1,start)
if hasattr(os,"register_at_fork"): os.register_at_fork(after_in_parent=fork_parent,after_in_child=fork_child)
@app.get("/")
def home(): return jsonify({"app":APP,"status":"online","last_ok":META["ok"],"price":META["price"],"data_state":META["data_state"],"error":META["err"]})
@app.get("/health")
def health(): return jsonify({"status":"ok","time":stamp()})
@app.get("/status")
def http_status():
    with STATE_LOCK: return jsonify({"app":APP,"meta":META,"trade":S["trade"],"stats":S["stats"],"day":S["day"],"pid":os.getpid(),"threads":sorted(t.name for t in threading.enumerate())})
@app.post("/telegram")
def telegram_webhook():
    if not hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token",""),SECRET): return jsonify({"ok":False}),401
    threading.Thread(target=handle_update,args=(request.get_json(silent=True) or {},),daemon=True).start()
    return jsonify({"ok":True})
@app.before_request
def ensure_started(): start_unless_master()
if AUTOSTART: later(START_DELAY,start_unless_master)
if __name__=="__main__":
    start(); app.run(host="0.0.0.0",port=int(os.getenv("PORT","10000")))
