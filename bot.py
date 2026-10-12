"""
GOLD SMART V7 - XAUUSD smart-money signal bot.
Signals + virtual trade tracking only. This file contains NO order-placement code.

Pipeline: H4/H1 bias -> liquidity pools (PDH/PDL, Asia range, H1/M15 swings)
          -> M5 liquidity sweep -> M5 market-structure shift -> structural SL, R-based TPs.

ENV (all optional except the first two)
  BOT_TOKEN, TELEGRAM_CHAT_ID     send /start to the bot to get your chat id
  RENDER_EXTERNAL_URL             set -> webhook mode, empty -> long polling (local/VPS)
  DEPOSIT=800 RISK_PERCENT=1 LOT_OZ=100
  MIN_SCORE=70 MIN_RR=1.5 TP1_R=1.0 TP2_R=2.5
  MIN_SL=3 MAX_SL=12 SL_BUFFER_ATR=0.25 MIN_ATR_M15=1.5
  SESSION_FILTER=1 SESSION_START=7 SESSION_END=20      (UTC hours)
  COOLDOWN_MIN=30 MAX_SIGNALS_DAY=4 MAX_DAILY_LOSS_R=2 MAX_TRADE_HOURS=12
  YAHOO_SYMBOL=GC=F (or XAUUSD=X)  STATE_FILE=gold_state.json  POLL_SECONDS=60

RUN
  pip install -r requirements.txt && python bot.py
  Render: gunicorn bot:app --workers 1 --threads 4 --timeout 120   (ONE worker, state is in-process)
"""
import copy
import hashlib
import hmac
import json
import math
import os
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify, request

APP = "GOLD SMART V7"


def _f(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
PUBLIC_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
DEPOSIT, RISK_PCT, LOT_OZ = _f("DEPOSIT", 800), _f("RISK_PERCENT", 1), _f("LOT_OZ", 100)
MIN_SCORE, MIN_RR = _f("MIN_SCORE", 70), _f("MIN_RR", 1.5)
TP1_R, TP2_R = _f("TP1_R", 1.0), _f("TP2_R", 2.5)
MIN_SL, MAX_SL = _f("MIN_SL", 3.0), _f("MAX_SL", 12.0)
SL_BUF_ATR, MIN_ATR15 = _f("SL_BUFFER_ATR", 0.25), _f("MIN_ATR_M15", 1.5)
SES_START, SES_END = int(_f("SESSION_START", 7)), int(_f("SESSION_END", 20))
SES_FILTER = os.getenv("SESSION_FILTER", "1") == "1"
COOLDOWN = _f("COOLDOWN_MIN", 30) * 60
MAX_SIG_DAY, MAX_LOSS_R = int(_f("MAX_SIGNALS_DAY", 4)), _f("MAX_DAILY_LOSS_R", 2.0)
MAX_TRADE_H = _f("MAX_TRADE_HOURS", 12)
SYMBOL = os.getenv("YAHOO_SYMBOL", "GC=F")
STATE_FILE = os.getenv("STATE_FILE", "gold_state.json")
POLL = max(30, int(_f("POLL_SECONDS", 60)))
KEEPALIVE = os.getenv("KEEPALIVE", "1") == "1"  # self-ping of the public URL, only used when RENDER_EXTERNAL_URL is set
AUTOSTART = not os.getenv("GOLD_NO_START")      # tests set GOLD_NO_START=1
START_DELAY = _f("START_DELAY", 10)             # engine start delay when the import already happens inside the worker
SECRET = hashlib.sha256(("gold7:" + BOT_TOKEN).encode()).hexdigest()[:48]

app = Flask(__name__)

# ------------------------------ state ------------------------------
DEFAULT = {
    "trade": None, "muted": False, "last_signal_ts": 0.0, "last_setup": "",
    "stats": {"total": 0, "wins": 0, "losses": 0, "be": 0, "r": 0.0},
    "day": {"date": "", "signals": 0, "r": 0.0},
    "history": [],
}
S = copy.deepcopy(DEFAULT)
STATE_LOCK = threading.RLock()
RUN_LOCK = threading.Lock()
META = {"started": False, "pid": None, "ok": None, "try": None, "err": None, "price": None, "fails": 0}
STOP = threading.Event()  # background threads of a process exit when its Event is set (see fork hooks)


def utcnow():
    return datetime.now(timezone.utc)


def stamp(dt=None):
    return (dt or utcnow()).strftime("%Y-%m-%d %H:%M UTC")


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as h:
            data = json.load(h)
    except (OSError, ValueError):
        data = {}
    with STATE_LOCK:
        S.clear()
        S.update(copy.deepcopy(DEFAULT))
        if isinstance(data, dict):
            for k, v in data.items():
                if k in ("stats", "day") and isinstance(v, dict):
                    S[k].update(v)
                elif k in DEFAULT:
                    S[k] = v


def save_state():
    with STATE_LOCK:
        d = os.path.dirname(os.path.abspath(STATE_FILE))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".gs_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as h:
                json.dump(S, h, ensure_ascii=False, indent=1)
            os.replace(tmp, STATE_FILE)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


def roll_day(now):
    d = now.strftime("%Y-%m-%d")
    if S["day"].get("date") != d:
        S["day"] = {"date": d, "signals": 0, "r": 0.0}


# ------------------------------ telegram ------------------------------
def tg(method):
    return f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"


def send(text, chat=None):
    target = str(chat or CHAT_ID)
    if not BOT_TOKEN or not target:
        print("telegram: BOT_TOKEN / TELEGRAM_CHAT_ID missing")
        return False
    try:
        r = requests.post(tg("sendMessage"), json={"chat_id": target, "text": text}, timeout=20)
        if not r.ok:
            print("telegram http", r.status_code, r.text[:300])
        return r.ok
    except requests.RequestException as exc:
        print("telegram error", repr(exc))
        return False


# ------------------------------ data (Yahoo chart API, real OHLC) ------------------------------
YF_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"
SPEC = {"M5": ("5m", "5d", 30), "M15": ("15m", "10d", 120), "H1": ("60m", "60d", 600)}  # interval, range, ttl s
_cache = {}


def fetch_ohlc(interval, rng):
    """Swap this one function to change data provider; must return UTC-indexed open/high/low/close."""
    r = requests.get(
        YF_URL + SYMBOL, params={"interval": interval, "range": rng},
        headers={"User-Agent": "Mozilla/5.0"}, timeout=20,
    )
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame(
        {k: q[k] for k in ("open", "high", "low", "close")},
        index=pd.to_datetime(res["timestamp"], unit="s", utc=True),
    ).apply(pd.to_numeric, errors="coerce").dropna()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if len(df) < 20:
        raise ValueError(f"{interval}: only {len(df)} candles")
    return df


def get_raw(name, now_ts):
    interval, rng, ttl = SPEC[name]
    c = _cache.get(name)
    if c and now_ts - c[0] < ttl:
        return c[1]
    try:
        df = fetch_ohlc(interval, rng)
        _cache[name] = (now_ts, df)
        return df
    except Exception:
        if c and now_ts - c[0] < 1800:  # tolerate a short provider hiccup
            return c[1]
        raise


def closed(df, minutes, now):
    return df[df.index + pd.Timedelta(minutes=minutes) <= pd.Timestamp(now)]


def to_h4(h1):
    o = h1.resample("4h", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    end = h1.index[-1] + pd.Timedelta(hours=1)
    return o[o.index + pd.Timedelta(hours=4) <= end]


def build_frames(now):
    ts = now.timestamp()
    m5_all, m15_all, h1_all = get_raw("M5", ts), get_raw("M15", ts), get_raw("H1", ts)
    fr = {"M5": closed(m5_all, 5, now), "M15": closed(m15_all, 15, now), "H1": closed(h1_all, 60, now)}
    fr["H4"] = to_h4(fr["H1"])
    return fr, m5_all, float(m5_all["close"].iloc[-1])


# ------------------------------ indicators / structure ------------------------------
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def atr(df, n=14):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50)


def swings(df, n=2):
    """Fractal pivots, positional index into df. Last n candles can't be pivots yet (unconfirmed)."""
    hi, lo = df["high"].to_numpy(), df["low"].to_numpy()
    sh, sl = [], []
    for i in range(n, len(df) - n):
        if hi[i] > hi[i - n:i].max() and hi[i] >= hi[i + 1:i + n + 1].max():
            sh.append((i, float(hi[i])))
        if lo[i] < lo[i - n:i].min() and lo[i] <= lo[i + 1:i + n + 1].min():
            sl.append((i, float(lo[i])))
    return sh, sl


def bias(df):
    """-2..+2: EMA20/50 stack + last two swing pivots (HH/HL or LH/LL)."""
    if len(df) < 55:
        return 0
    c = df["close"]
    e20, e50, last = float(ema(c, 20).iloc[-1]), float(ema(c, 50).iloc[-1]), float(c.iloc[-1])
    s = 1 if last > e20 > e50 else (-1 if last < e20 < e50 else 0)
    sh, sl = swings(df.tail(300), 2)
    if len(sh) >= 2 and len(sl) >= 2:
        if sh[-1][1] > sh[-2][1] and sl[-1][1] > sl[-2][1]:
            s += 1
        elif sh[-1][1] < sh[-2][1] and sl[-1][1] < sl[-2][1]:
            s -= 1
    return s


def liquidity_pools(m15, h1, now):
    """(name, price, weight). weight: PDH/PDL 3, Asia range / H1 swings 2, M15 swings 1."""
    lows, highs = [], []
    today = pd.Timestamp(now).floor("D")
    by_day = {d: g for d, g in m15.groupby(m15.index.floor("D"))}
    prev = [d for d in sorted(by_day) if d < today and len(by_day[d]) >= 20]
    if prev:
        g = by_day[prev[-1]]
        highs.append(("PDH", float(g["high"].max()), 3))
        lows.append(("PDL", float(g["low"].min()), 3))
    if now.hour >= 7 and today in by_day:
        a = by_day[today].between_time("00:00", "06:59")
        if len(a) >= 8:
            highs.append(("Asia H", float(a["high"].max()), 2))
            lows.append(("Asia L", float(a["low"].min()), 2))
    for df, n, name, w, k in ((h1.tail(300), 3, "H1 swing", 2, 4), (m15.tail(200), 3, "M15 swing", 1, 3)):
        sh, sl = swings(df, n)
        highs += [(name, p, w) for _, p in sh[-k:]]
        lows += [(name, p, w) for _, p in sl[-k:]]
    return {"low": lows, "high": highs}


def find_sweep(m5, pools, side, window=18):
    """Wick through a pool + close back inside within 3 candles, and the wick is a fresh 20-candle extreme."""
    lo, hi, cl = (m5[c].to_numpy() for c in ("low", "high", "close"))
    n, best = len(m5), None
    for i in range(max(21, n - window), n):
        if side == "BUY":
            if lo[i] > lo[i - 20:i].min():
                continue
            for name, lvl, w in pools["low"]:
                if lo[i] < lvl and cl[i:i + 3].max() > lvl and (best is None or (w, i) > (best[3], best[0])):
                    best = (i, name, lvl, w)
        else:
            if hi[i] < hi[i - 20:i].max():
                continue
            for name, lvl, w in pools["high"]:
                if hi[i] > lvl and cl[i:i + 3].min() < lvl and (best is None or (w, i) > (best[3], best[0])):
                    best = (i, name, lvl, w)
    if best is None:
        return None
    i, name, lvl, w = best
    extreme = float(lo[i:].min() if side == "BUY" else hi[i:].max())
    return {"i": int(i), "name": name, "level": float(lvl), "w": int(w),
            "ts": m5.index[i].isoformat(), "extreme": extreme}


def find_mss(m5, side, i_sw):
    """Close beyond the last opposing swing that formed before the sweep."""
    sh, sl = swings(m5, 2)
    cl = m5["close"].to_numpy()
    if side == "BUY":
        prior = [p for i, p in sh if i_sw - 25 <= i < i_sw]
        if prior:
            for j in range(i_sw + 1, len(cl)):
                if cl[j] > prior[-1]:
                    return {"level": float(prior[-1]), "j": int(j)}
    else:
        prior = [p for i, p in sl if i_sw - 25 <= i < i_sw]
        if prior:
            for j in range(i_sw + 1, len(cl)):
                if cl[j] < prior[-1]:
                    return {"level": float(prior[-1]), "j": int(j)}
    return None


# ------------------------------ analysis ------------------------------
def lot_size(risk_dist):
    risk_usd = DEPOSIT * RISK_PCT / 100.0
    lots = max(math.floor(risk_usd / (risk_dist * LOT_OZ) * 100) / 100, 0.01)
    return lots, lots * LOT_OZ * risk_dist


def analyze(fr, price, now):
    m5, m15, h1, h4 = fr["M5"], fr["M15"], fr["H1"], fr["H4"]
    out = {"price": price, "why": [], "entry": None, "ok": False}
    for name, need in (("M5", 60), ("M15", 60), ("H1", 60), ("H4", 55)):
        if len(fr[name]) < need:
            out["why"].append(f"мало данных {name}: {len(fr[name])}/{need}")
            return out
    age_min = (now - (m5.index[-1] + pd.Timedelta(minutes=5))).total_seconds() / 60
    if age_min > 25:
        out["why"].append("рынок закрыт или лента устарела")
        return out

    b4, b1, b15 = bias(h4), bias(h1), bias(m15)
    out["bias"] = {"H4": b4, "H1": b1, "M15": b15}
    if b4 >= 0 and b1 >= 1 and b4 + b1 >= 2:
        side = "BUY"
    elif b4 <= 0 and b1 <= -1 and b4 + b1 <= -2:
        side = "SELL"
    else:
        out["why"].append("H4/H1 не согласованы")
        return out
    sgn = 1 if side == "BUY" else -1
    if SES_FILTER and not (SES_START <= now.hour < SES_END):
        out["why"].append(f"вне сессии {SES_START}-{SES_END} UTC")
        return out
    a15 = float(atr(m15).iloc[-1])
    out["atr15"] = a15
    if a15 < MIN_ATR15:
        out["why"].append(f"низкая волатильность (ATR M15 {a15:.2f} < {MIN_ATR15:g})")
        return out

    pools = liquidity_pools(m15, h1, now)
    sw = find_sweep(m5, pools, side)
    if not sw:
        out["why"].append("нет sweep ликвидности за последние ~90 мин")
        return out
    mss = find_mss(m5, side, sw["i"])
    if not mss:
        out["why"].append(f"sweep {sw['name']} есть, слома структуры ещё нет")
        return out
    n = len(m5)
    if mss["j"] < n - 1 - 3:
        out["why"].append("MSS устарел, вход упущен")
        return out

    entry = price
    sl = sw["extreme"] - sgn * SL_BUF_ATR * a15
    risk = (entry - sl) * sgn
    if risk <= 0:
        out["why"].append("цена уже за уровнем SL")
        return out
    if risk < MIN_SL:
        sl, risk = entry - sgn * MIN_SL, MIN_SL
    if risk > MAX_SL:
        out["why"].append(f"SL слишком широкий ({risk:.2f}$ > {MAX_SL:g}$)")
        return out
    tp1, tp2 = entry + sgn * TP1_R * risk, entry + sgn * TP2_R * risk

    tgt = [p for p in (pools["high"] if side == "BUY" else pools["low"]) if p[2] >= 2 and (p[1] - entry) * sgn > 0]
    target = min(tgt, key=lambda p: abs(p[1] - entry), default=None)
    if target and abs(target[1] - entry) < MIN_RR * risk:
        out["why"].append(f"ликвидность-цель {target[0]} {target[1]:.2f} ближе {MIN_RR:g}R")
        return out

    o, c, hi, lo = (m5[k].to_numpy() for k in ("open", "close", "high", "low"))
    j = mss["j"]
    bodies = np.abs(c - o)
    avg = float(bodies[max(0, j - 20):j].mean()) if j > 0 else 0.0
    disp = bool(avg > 0 and bodies[j] >= 1.3 * avg and (c[j] - o[j]) * sgn > 0)
    fvg = False
    for k in (j - 1, j, j + 1):
        if 2 <= k < n:
            fvg = fvg or (lo[k] > hi[k - 2] if side == "BUY" else hi[k] < lo[k - 2])
    hh = h1.tail(48)
    mid = (float(hh["high"].max()) + float(hh["low"].min())) / 2
    zone = "DISCOUNT" if price < mid else "PREMIUM"
    zone_ok = (zone == "DISCOUNT") == (side == "BUY")
    r_now = float(rsi(m5["close"]).iloc[-1])
    rsi_ok = r_now > 52 if side == "BUY" else r_now < 48
    kz = 7 <= now.hour < 10 or 12 <= now.hour < 16
    b15ok = b15 * sgn >= 1

    score = 7.5 * (abs(b4) + abs(b1))                         # HTF alignment, max 30
    score += 10 if b15ok else (5 if b15 == 0 else 0)          # M15 agreement
    score += {3: 20, 2: 15, 1: 10}[sw["w"]] + 15              # sweep quality + MSS
    score += (8 if disp else 0) + (5 if fvg else 0) + (7 if zone_ok else 0) + (5 if rsi_ok else 0) + (5 if kz else 0)
    score = int(min(100, round(score)))

    out.update(side=side, score=score, entry=entry, sl=sl, tp1=tp1, tp2=tp2, risk=risk, sweep=sw, mss=mss,
               disp=disp, fvg=fvg, zone=zone, zone_ok=zone_ok, kz=kz, rsi_ok=rsi_ok, b15ok=b15ok,
               target=target, ok=score >= MIN_SCORE)
    if not out["ok"]:
        out["why"].append(f"score {score} < {MIN_SCORE:g}")
    return out


def gate(a):
    if S["muted"]:
        return "сигналы выключены (/unmute)"
    if S["trade"]:
        return "уже есть открытая виртуальная сделка"
    if S["day"]["signals"] >= MAX_SIG_DAY:
        return "лимит сигналов на день"
    if S["day"]["r"] <= -MAX_LOSS_R:
        return "дневной лимит убытка"
    if time.time() - S["last_signal_ts"] < COOLDOWN:
        return "cooldown"
    if S["last_setup"] == a["sweep"]["ts"]:
        return "этот сетап уже отработан"
    return None


# ------------------------------ virtual trades (SL checked before TP inside a candle) ------------------------------
def new_trade(a, now):
    lots, _ = lot_size(a["risk"])
    return {
        "side": a["side"], "entry": a["entry"], "sl": a["sl"], "sl0": a["sl"], "tp1": a["tp1"], "tp2": a["tp2"],
        "risk": a["risk"], "score": a["score"], "lots": lots, "opened": now.isoformat(),
        "cursor": pd.Timestamp(now).floor("5min").isoformat(),  # candles with start > cursor are evaluated
        "tp1_hit": False, "realized_r": 0.0, "remaining": 1.0,
    }


def _hit(level, p, sgn):
    return (p - level) * sgn >= 0


def step(t, hi, lo, notes):
    """Advance one candle (or a tick when hi == lo). Returns final R if the trade closed, else None."""
    sgn = 1 if t["side"] == "BUY" else -1
    fav, adv = (hi, lo) if sgn == 1 else (lo, hi)

    def stopped():
        return (t["sl"] - adv) * sgn >= 0

    if stopped():
        return t["realized_r"] + t["remaining"] * (t["sl"] - t["entry"]) * sgn / t["risk"]
    if not t["tp1_hit"] and _hit(t["tp1"], fav, sgn):
        t["tp1_hit"] = True
        t["realized_r"] += 0.5 * TP1_R
        t["remaining"] = 0.5
        t["sl"] = t["entry"]
        notes.append(f"🎯 {t['side']} TP1 {t['tp1']:.2f} hit\n50% зафиксировано (+{0.5 * TP1_R:.2f}R), SL → безубыток")
        if stopped():  # candle also came back to entry: assume the worst order
            return t["realized_r"]
    if t["tp1_hit"] and _hit(t["tp2"], fav, sgn):
        return t["realized_r"] + 0.5 * TP2_R
    return None


def finish_trade(t, r, reason=None):
    r = round(float(r), 2)
    result = "WIN" if r > 0.05 else ("LOSS" if r < -0.05 else "BE")
    st = S["stats"]
    st["total"] += 1
    st[{"WIN": "wins", "LOSS": "losses", "BE": "be"}[result]] += 1
    st["r"] = round(st["r"] + r, 2)
    S["day"]["r"] = round(S["day"]["r"] + r, 2)
    S["history"] = (S["history"] + [{"t": t["opened"], "side": t["side"], "r": r, "result": result}])[-50:]
    S["trade"] = None
    icon = {"WIN": "🏆", "LOSS": "📉", "BE": "🟡"}[result]
    return (f"{icon} {t['side']} закрыта: {result} {r:+.2f}R" + (f" ({reason})" if reason else "") +
            f"\nEntry {t['entry']:.2f}\nДень: {S['day']['r']:+.2f}R")


def manage_trade(m5_all, price, now):
    notes = []
    with STATE_LOCK:
        t = S["trade"]
        if not t:
            return notes
        res, reason = None, None
        cc = closed(m5_all, 5, now)
        for ts, row in cc[cc.index > pd.Timestamp(t["cursor"])].iterrows():
            t["cursor"] = ts.isoformat()
            res = step(t, float(row["high"]), float(row["low"]), notes)
            if res is not None:
                break
        if res is None:
            res = step(t, price, price, notes)  # forming candle: live price only, its high/low is replayed once closed
        if res is None and (now - datetime.fromisoformat(t["opened"])).total_seconds() > MAX_TRADE_H * 3600:
            sgn = 1 if t["side"] == "BUY" else -1
            res = t["realized_r"] + t["remaining"] * (price - t["entry"]) * sgn / t["risk"]
            reason = "TIMEOUT"
        if res is not None:
            notes.append(finish_trade(t, res, reason))
        save_state()
    return notes


# ------------------------------ messages ------------------------------
def arrow(b):
    return "▲" if b > 0 else ("▼" if b < 0 else "•")


def bias_str(a):
    b = a.get("bias")
    return f"H4 {arrow(b['H4'])}  H1 {arrow(b['H1'])}  M15 {arrow(b['M15'])}" if b else "n/a"


def build_signal(a):
    side = a["side"]
    lots, risk_usd = lot_size(a["risk"])
    tags = ["M15 aligned"] if a["b15ok"] else []
    tags += (["displacement"] if a["disp"] else []) + (["FVG"] if a["fvg"] else [])
    tags += [a["zone"].lower()] if a["zone_ok"] else []
    tags += (["momentum"] if a["rsi_ok"] else []) + (["killzone"] if a["kz"] else [])
    sw, mss = a["sweep"], a["mss"]
    over = risk_usd > DEPOSIT * RISK_PCT / 100.0 * 1.05  # min lot 0.01 can exceed the risk budget on small deposits
    lines = [
        f"🥇 {APP}", "",
        f"{'🟢' if side == 'BUY' else '🔴'} {side} XAUUSD · score {a['score']}/100", "",
        f"Entry {a['entry']:.2f}",
        f"SL    {a['sl']:.2f}  ({a['risk']:.2f}$)",
        f"TP1   {a['tp1']:.2f}  ({TP1_R:g}R · 50% + SL→BE)",
        f"TP2   {a['tp2']:.2f}  ({TP2_R:g}R)", "",
        f"Lot ≈ {lots:.2f} (risk ≈ ${risk_usd:.2f} из ${DEPOSIT:.0f})",
        *([f"⚠️ мин. лот 0.01 даёт риск выше {RISK_PCT:g}% — пропусти или бери меньше"] if over else []), "",
        f"Bias  {bias_str(a)}",
        f"Sweep {sw['name']} {sw['level']:.2f}",
        f"MSS   close {'выше' if side == 'BUY' else 'ниже'} {mss['level']:.2f}",
        "Конфлюенс: " + (", ".join(tags) or "—"),
    ]
    if a.get("target"):
        lines.append(f"Цель-ликвидность: {a['target'][0]} {a['target'][1]:.2f}")
    lines += ["", "Только сигналы, ордеров бот не ставит."]
    return "\n".join(lines)


def build_report(a):
    lines = [f"🥇 {APP} · анализ", f"XAUUSD {a['price']:.2f}", f"Bias {bias_str(a)}"]
    if a.get("atr15") is not None:
        lines.append(f"ATR M15 {a['atr15']:.2f}")
    if a.get("entry") is not None:
        lines += ["", build_signal(a), ""]
        if a["ok"] and not a.get("blocked"):
            lines.append("✅ сетап валиден — в рабочем цикле уйдёт как сигнал")
        else:
            lines.append("⛔ не отправлен: " + (a.get("blocked") or "; ".join(a["why"])))
    else:
        lines += ["", "Сетапа нет: " + "; ".join(a["why"])]
    return "\n".join(lines)


def cmd_start():
    return (f"🥇 {APP}\nXAUUSD · H4/H1 bias → sweep → MSS на M5\n\n"
            "/signal — анализ сейчас\n/trade — открытая виртуальная сделка\n/stats — статистика\n"
            "/status — состояние бота\n/mute /unmute — пауза сигналов\n\nОрдеров бот не ставит.")


def cmd_status():
    with STATE_LOCK:
        return (f"🥇 {APP}\nПоследний успешный цикл: {META['ok'] or 'нет'}\nПоследняя попытка: {META['try'] or 'нет'}\n"
                f"Цена: {META['price'] if META['price'] is not None else 'n/a'}\nОшибка: {META['err'] or 'нет'}\n"
                f"Сигналы: {'ВЫКЛ' if S['muted'] else 'вкл'}\nСегодня: {S['day']['signals']} сигн., {S['day']['r']:+.2f}R\n"
                f"Сделка открыта: {'да' if S['trade'] else 'нет'}")


def cmd_stats():
    with STATE_LOCK:
        st = S["stats"]
        n = st["total"]
        return (f"📊 {APP}\nЗакрыто: {n}\nWin {st['wins']} / Loss {st['losses']} / BE {st['be']}\n"
                f"Winrate: {(st['wins'] / n * 100) if n else 0:.1f}%\nСумма: {st['r']:+.2f}R\n"
                f"Средняя: {(st['r'] / n) if n else 0:+.2f}R\nСегодня: {S['day']['r']:+.2f}R")


def cmd_trade():
    with STATE_LOCK:
        t = S["trade"]
        if not t:
            return "Открытой виртуальной сделки нет."
        price = META["price"]
        sgn = 1 if t["side"] == "BUY" else -1
        live = ""
        if price is not None:
            live = f"\nСейчас {price:.2f} ({(price - t['entry']) * sgn / t['risk']:+.2f}R на остаток)"
        return (f"{t['side']} {t['lots']:.2f} lot · score {t['score']}\nEntry {t['entry']:.2f}\nSL {t['sl']:.2f}\n"
                f"TP1 {t['tp1']:.2f}{' ✅' if t['tp1_hit'] else ''}\nTP2 {t['tp2']:.2f}{live}\nОткрыта: {t['opened'][:16]} UTC")


def cmd_signal():
    a = market_cycle(open_new=False)
    return build_report(a) if a else f"⚠️ нет данных: {META['err']}"


def cmd_mute(flag):
    with STATE_LOCK:
        S["muted"] = flag
        save_state()
    return "🔕 сигналы выключены, сопровождение сделки продолжается." if flag else "🔔 сигналы включены."


def handle_update(u):
    m = u.get("message") or u.get("edited_message") or {}
    chat, txt = (m.get("chat") or {}).get("id"), (m.get("text") or "").strip()
    if chat is None or not txt:
        return
    cmd = txt.split()[0].split("@")[0].lower()
    if not CHAT_ID:
        if cmd in ("/start", "/help"):
            send(f"chat id: {chat}\nпропиши его в TELEGRAM_CHAT_ID и перезапусти бота.", chat)
        return
    if str(chat) != CHAT_ID:
        return
    table = {"/start": cmd_start, "/help": cmd_start, "/status": cmd_status, "/signal": cmd_signal,
             "/stats": cmd_stats, "/trade": cmd_trade, "/mute": lambda: cmd_mute(True),
             "/unmute": lambda: cmd_mute(False)}
    try:
        reply = table.get(cmd, lambda: "Неизвестная команда. /help")()
    except Exception as exc:
        traceback.print_exc()
        reply = f"⚠️ ошибка команды: {type(exc).__name__}"
    send(reply, chat)


# ------------------------------ engine ------------------------------
def market_cycle(open_new=True):
    if not RUN_LOCK.acquire(blocking=not open_new):  # engine skips if busy, commands wait
        return None
    try:
        now = utcnow()
        META["try"] = stamp(now)
        fr, m5_all, price = build_frames(now)
        META["price"] = price
        with STATE_LOCK:
            roll_day(now)
        for note in manage_trade(m5_all, price, now):
            send(note)
        a = analyze(fr, price, now)
        a["blocked"] = None
        if a["entry"] is not None and a["ok"]:
            with STATE_LOCK:
                a["blocked"] = gate(a)
        if open_new and a["ok"] and not a["blocked"]:
            if send(build_signal(a)):
                with STATE_LOCK:
                    S["trade"] = new_trade(a, now)
                    S["day"]["signals"] += 1
                    S["last_signal_ts"], S["last_setup"] = time.time(), a["sweep"]["ts"]
                    save_state()
            else:
                print("signal not delivered, trade not opened")
        META["ok"], META["err"], META["fails"] = stamp(now), None, 0
        return a
    except Exception as exc:
        META["err"] = f"{type(exc).__name__}: {exc}"
        META["fails"] += 1
        traceback.print_exc()
        if META["fails"] == 5:
            send(f"⚠️ {APP}: лента данных недоступна 5 циклов подряд\n{META['err']}")
        return None
    finally:
        RUN_LOCK.release()


def engine_loop(stop):
    print(f"{APP}: engine started (pid {os.getpid()})")
    while not stop.is_set():
        try:
            market_cycle(open_new=True)
        except Exception:
            traceback.print_exc()
        stop.wait(POLL)


def poll_loop(stop):
    try:
        requests.post(tg("deleteWebhook"), json={"drop_pending_updates": False}, timeout=20)
    except requests.RequestException:
        pass
    offset = None
    while not stop.is_set():
        try:
            r = requests.get(tg("getUpdates"), params={"timeout": 30, "offset": offset}, timeout=40)
            if not r.ok:
                stop.wait(5)
                continue
            for u in r.json().get("result", []):
                offset = u["update_id"] + 1
                handle_update(u)
        except Exception:
            stop.wait(5)


def keepalive_loop(stop):
    """Render free tier sleeps without inbound traffic; pinging our own public URL keeps the engine awake."""
    while not stop.wait(240):
        try:
            requests.get(PUBLIC_URL + "/health", timeout=10)
        except requests.RequestException as exc:
            print("keepalive:", repr(exc))


def configure_webhook():
    try:
        r = requests.post(tg("setWebhook"), json={"url": PUBLIC_URL + "/telegram", "secret_token": SECRET}, timeout=20)
        print("setWebhook:", r.status_code, r.text[:200])
    except requests.RequestException as exc:
        print("setWebhook error:", repr(exc))


def _boot():
    if BOT_TOKEN and PUBLIC_URL:
        configure_webhook()
    if BOT_TOKEN and CHAT_ID:
        send(f"🟢 {APP} online · {'webhook' if PUBLIC_URL else 'polling'}\n"
             f"score ≥ {MIN_SCORE:g} · сессия {SES_START}-{SES_END} UTC · {SYMBOL}\nТолько сигналы, ордеров нет.")


def start():
    """Idempotent, non-blocking. Never run this in a process that is going to fork() afterwards (see below)."""
    with STATE_LOCK:
        if META["started"]:
            return
        META["started"] = True
    META["pid"] = os.getpid()
    load_state()
    if not BOT_TOKEN:
        print("BOT_TOKEN missing: engine runs, nothing can be delivered")
    stop = STOP
    threading.Thread(target=engine_loop, args=(stop,), daemon=True, name="engine").start()
    if BOT_TOKEN and not PUBLIC_URL:
        threading.Thread(target=poll_loop, args=(stop,), daemon=True, name="poll").start()
    if PUBLIC_URL and KEEPALIVE:
        threading.Thread(target=keepalive_loop, args=(stop,), daemon=True, name="keepalive").start()
    threading.Thread(target=_boot, daemon=True, name="boot").start()


# Process model. `gunicorn --preload` imports this module in the MASTER and then fork()s the worker that serves
# requests. Threads don't survive fork, and forking while a thread is mid network call leaves every network call
# in the child hanging. So nothing starts at import time: a forked worker starts its own engine (hook below);
# when the import already happens inside the worker, the engine starts after START_DELAY or on the first request.
IS_MASTER = False


def _later(delay, fn):
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()


def _start_unless_master():
    if AUTOSTART and not IS_MASTER:
        start()


def _fork_parent():
    global IS_MASTER
    IS_MASTER = True  # this process forks workers, it must never run the engine itself
    STOP.set()


def _fork_child():
    global STATE_LOCK, RUN_LOCK, STOP, IS_MASTER
    STATE_LOCK, RUN_LOCK, STOP = threading.RLock(), threading.Lock(), threading.Event()  # may be held at fork
    META["started"], IS_MASTER = False, False
    if AUTOSTART:
        _later(1.0, start)  # let gunicorn finish initialising the worker first


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_parent=_fork_parent, after_in_child=_fork_child)


# ------------------------------ flask ------------------------------
@app.get("/")
def home():
    return jsonify({"app": APP, "status": "online", "last_ok": META["ok"], "price": META["price"], "error": META["err"]})


@app.get("/health")
def health():
    return jsonify({"status": "ok", "time": stamp()})


@app.get("/status")
def http_status():
    with STATE_LOCK:
        return jsonify({"app": APP, "meta": META, "trade": S["trade"], "stats": S["stats"], "day": S["day"],
                        "pid": os.getpid(), "threads": sorted(t.name for t in threading.enumerate())})


@app.post("/telegram")
def telegram_webhook():
    if not hmac.compare_digest(request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), SECRET):
        return jsonify({"ok": False}), 401
    threading.Thread(target=handle_update, args=(request.get_json(silent=True) or {},), daemon=True).start()
    return jsonify({"ok": True})


@app.before_request
def _ensure_started():
    _start_unless_master()  # safety net: the first request in a worker starts the engine if nothing did


if AUTOSTART:
    _later(START_DELAY, _start_unless_master)

if __name__ == "__main__":
    start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

# ---- end of bot.py (if this line is missing in your deployed copy, the file got truncated) ----
