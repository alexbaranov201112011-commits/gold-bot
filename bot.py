from flask import Flask, request
import requests
import os
from datetime import datetime, timezone

app = Flask(__name__)
BOT_TOKEN = os.getenv("BOT_TOKEN")

def get_price():
    for url in ["https://api.binance.com/api/v3/ticker/price?symbol=PAXGUSDT","https://data-api.binance.vision/api/v3/ticker/price?symbol=PAXGUSDT"]:
        try:
            r = requests.get(url, timeout=5).json()
            if "price" in r: return float(r["price"])
        except: continue
    return 4340.0

def get_klines():
    for url in ["https://api.binance.com/api/v3/klines?symbol=PAXGUSDT&interval=15m&limit=100","https://data-api.binance.vision/api/v3/klines?symbol=PAXGUSDT&interval=15m&limit=100"]:
        try:
            d = requests.get(url, timeout=8, headers={"User-Agent":"Mozilla"}).json()
            if isinstance(d, list) and len(d)>60:
                closes = [float(c[4]) for c in d]
                highs = [float(c[2]) for c in d]
                lows = [float(c[3]) for c in d]
                return closes, highs, lows
        except: continue
    return None, None, None

def get_news():
    try:
        data = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=8).json()
        now = datetime.now(timezone.utc)
        w=[]
        for ev in data:
            if ev.get("country")!="USD": continue
            if int(ev.get("impact",0))<3: continue
            try: dt=datetime.fromisoformat(ev["date"].replace("Z","+00:00"))
            except: continue
            diff=(dt-now).total_seconds()/60
            if -30 < diff < 60: w.append(ev.get("title","News"))
        return " | ".join(w[:2]) if w else None
    except: return None

def calc_ema(arr, p):
    k=2/(p+1); e=arr[0]
    for v in arr[1:]: e=v*k+e*(1-k)
    return e

def get_levels(closes, highs, lows):
    # поддержка / сопротивление - ищем пивоты за 50 свечей
    last50_c = closes[-50:]
    last50_h = highs[-50:]
    last50_l = lows[-50:]
    support = min(last50_l)
    resistance = max(last50_h)
    # ближайший уровень
    cur = closes[-1]
    # поддержка ниже цены
    below = [x for x in last50_l if x < cur]
    above = [x for x in last50_h if x > cur]
    s1 = max(below) if below else support
    r1 = min(above) if above else resistance
    return round(s1,2), round(support,2), round(r1,2), round(resistance,2)

def get_winrate(closes):
    wins=0; total=0
    if len(closes)<70: return 0
    for i in range(50, len(closes)-10):
        hist = closes[:i]
        ema = calc_ema(hist[-70:], 50)
        # rsi 14
        g=l=0
        for k in range(1,15):
            d=hist[-k]-hist[-k-1]
            if d>0: g+=d
            else: l+=abs(d)
        ag=g/14 if g else 0.5; al=l/14 if l else 0.5
        rsi=100-(100/(1+ag/al)) if al else 50
        price=hist[-1]
        # сигнал
        is_buy=None
        if abs(price-ema)>3:
            if price>ema and rsi>=52: is_buy=True
            if price<ema and rsi<=53: is_buy=False
        if is_buy is None: continue
        total+=1
        # смотрим 10 свечей вперед - сработал ли TP25 раньше SL20
        future = closes[i:i+10]
        if not future: continue
        tp = price+25 if is_buy else price-25
        sl = price-20 if is_buy else price+20
        for fp in future:
            if is_buy:
                if fp>=tp: wins+=1; break
                if fp<=sl: break
            else:
                if fp<=tp: wins+=1; break
                if fp>=sl: break
    return int(wins/total*100) if total>0 else 0

def analyze(price, ema50, rsi, news):
    if news: return None, "NOVOSTI " + news
    dist=abs(price-ema50)
    if dist<3.0: return None, "Bokovik u EMA %.1f" % ema50
    if 48<rsi<52 and dist<7: return None, "Net impulsa RSI %.0f" % rsi
    if price>ema50 and rsi>=52: return True, "Vyshe EMA %.1f RSI %.0f byki" % (ema50, rsi)
    if price<ema50 and rsi<=53: return False, "Nizhe EMA %.1f RSI %.0f medvedi" % (ema50, rsi)
    return None, "Rynok nechistiy"

def format_msg(price, ema50, rsi, is_buy, reason, has_data, news, s1, s2, r1, r2, winrate):
    status="REAL M15" if has_data else "ZONE"
    nline=news if news else "Net novostey"
    lvl="PODDERZHKA: %.1f$ (bliz) / %.1f$ (siln)\nSOPROTIVLENIE: %.1f$ (bliz) / %.1f$ (siln)" % (s1,s2,r1,r2)
    wr="WINRATE strategii za 100 svechey: %s%%" % winrate
    if is_buy is None:
        return "STAR V6 GOLD PRO\n\nCena: %.2f$\nEMA50: %.2f$ [%s]\nRSI: %.1f\n%s\n%s\n%s\n\nSIGNAL: ZHDI\n%s\n" % (price,ema50,status,rsi,lvl,nline,wr,reason)
    sig="LONG BUY" if is_buy else "SHORT SELL"
    sl=price-20 if is_buy else price+20
    tp1=price+25 if is_buy else price-25
    tp2=price+50 if is_buy else price-50
    return "STAR V6 GOLD PRO\n\nCena: %.2f$\nEMA50: %.2f$ [%s]\nRSI: %.1f\n%s\n%s\n%s\n\nSIGNAL: %s\n%s\nVhod: %.1f-%.1f\nSL: %.1f\nTP1: %.1f\nTP2: %.1f\n" % (price,ema50,status,rsi,lvl,nline,wr,sig,reason,price-2,price+2,sl,tp1,tp2)

def send_tg(cid, txt):
    try: requests.post("https://api.telegram.org/bot%s/sendMessage" % BOT_TOKEN, json={"chat_id":cid,"text":txt}, timeout=10)
    except: pass

@app.route("/")
def home():
    c,h,l=get_klines()
    p=get_price()
    return "V6 OK %s" % p, 200

@app.route("/webhook", methods=["POST"])
def webhook():
    data=request.get_json()
    if data and "message" in data:
        cid=data["message"]["chat"]["id"]
        txt=data["message"].get("text","")
        if txt in ["/gold","/check","/start"]:
            closes, highs, lows = get_klines()
            price = get_price()
            news = get_news()
            if closes:
                ema50=calc_ema(closes[-70:],50)
                g=l=0
                for k in range(1,15):
                    d=closes[-k]-closes[-k-1]
                    if d>0: g+=d
                    else: l+=abs(d)
                ag=g/14 if g else 0.5; al=l/14 if l else 0.5
                rsi=100-(100/(1+ag/al)) if al else 50
                s1,s2,r1,r2=get_levels(closes,highs,lows)
                winrate=get_winrate(closes)
                has=True
            else:
                ema50=price-5; rsi=50; s1=s2=r1=r2=price; winrate=0; has=False
            ib, rs = analyze(price,ema50,rsi,news)
            send_tg(cid, format_msg(price,ema50,rsi,ib,rs,has,news,s1,s2,r1,r2,winrate))
    return "ok",200

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))
