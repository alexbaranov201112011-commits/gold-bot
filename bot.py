import os, time, threading
from datetime import datetime, timezone
import pandas as pd
import yfinance as yf
import requests
from flask import Flask, request

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TOKEN")
CHAT_ID_ENV = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID") or ""

SYMBOL = "GC=F"
MIN_SCORE = 5
RR = 2.0
SESSION_START = 12
SESSION_END = 22

app = Flask(__name__)
last_signal_key = None

def send_telegram(chat_id, text):
    if not BOT_TOKEN: return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15)
    except Exception as e:
        print("Telegram error:", e)

def get_data(interval="15m", period="10d"):
    df = yf.download(SYMBOL, period=period, interval=interval, auto_adjust=False, progress=False)
    if df.empty: return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)
    return df[["Open","High","Low","Close","Volume"]].dropna()

def ema(s,l): return s.ewm(span=l, adjust=False).mean()
def rsi(series,length=14):
    delta=series.diff()
    gain=delta.clip(lower=0); loss=-delta.clip(upper=0)
    avg_gain=gain.ewm(alpha=1/length, adjust=False).mean()
    avg_loss=loss.ewm(alpha=1/length, adjust=False).mean()
    rs=avg_gain/avg_loss.replace(0, pd.NA)
    return 100-(100/(1+rs))
def atr(df,length=14):
    hl=df["High"]-df["Low"]
    hc=(df["High"]-df["Close"].shift()).abs()
    lc=(df["Low"]-df["Close"].shift()).abs()
    tr=pd.concat([hl,hc,lc],axis=1).max(axis=1)
    return tr.ewm(alpha=1/length, adjust=False).mean()
def market_structure(df):
    if len(df)<8: return "NEUTRAL"
    recent=df.tail(8); highs=recent["High"].values; lows=recent["Low"].values
    if highs[-1]>highs[-3] and lows[-1]>lows[-3]: return "BULLISH"
    if highs[-1]<highs[-3] and lows[-1]<lows[-3]: return "BEARISH"
    return "NEUTRAL"
def liquidity_sweep(df):
    if len(df)<6: return "NONE"
    last=df.iloc[-1]; prev=df.iloc[-6:-1]
    ph=prev["High"].max(); pl=prev["Low"].min()
    if last["High"]>ph and last["Close"]<ph: return "SELL_SWEEP"
    if last["Low"]<pl and last["Close"]>pl: return "BUY_SWEEP"
    return "NONE"

def analyze():
    m15=get_data("15m","10d"); h1=get_data("60m","30d"); h1_raw=get_data("1h","60d")
    if m15.empty or h1.empty or h1_raw.empty: return None
    m15["EMA50"]=ema(m15["Close"],50); m15["EMA200"]=ema(m15["Close"],200); m15["RSI"]=rsi(m15["Close"]); m15["ATR"]=atr(m15)
    h1["EMA50"]=ema(h1["Close"],50); h1["EMA200"]=ema(h1["Close"],200)
    h4=h1_raw.resample("4h").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
    h4["EMA50"]=ema(h4["Close"],50); h4["EMA200"]=ema(h4["Close"],200)
    last=m15.iloc[-1]; h1_last=h1.iloc[-1]; h4_last=h4.iloc[-1]
    price=float(last["Close"]); atr_v=float(last["ATR"]); rsi_v=float(last["RSI"])
    sb=ss=0; rb=[]; rs=[]
    if h4_last["EMA50"]>h4_last["EMA200"]: sb+=2; rb.append("H4 EMA50 > EMA200")
    elif h4_last["EMA50"]<h4_last["EMA200"]: ss+=2; rs.append("H4 EMA50 < EMA200")
    if h1_last["EMA50"]>h1_last["EMA200"]: sb+=1; rb.append("H1 EMA50 > EMA200")
    elif h1_last["EMA50"]<h1_last["EMA200"]: ss+=1; rs.append("H1 EMA50 < EMA200")
    if last["EMA50"]>last["EMA200"]: sb+=1; rb.append("M15 bullish EMA")
    elif last["EMA50"]<last["EMA200"]: ss+=1; rs.append("M15 bearish EMA")
    if 50<=rsi_v<=68: sb+=1; rb.append(f"RSI {rsi_v:.1f}")
    elif 32<=rsi_v<=50: ss+=1; rs.append(f"RSI {rsi_v:.1f}")
    struct=market_structure(m15)
    if struct=="BULLISH": sb+=1; rb.append("M15 HH/HL")
    elif struct=="BEARISH": ss+=1; rs.append("M15 LH/LL")
    sweep=liquidity_sweep(m15)
    if sweep=="BUY_SWEEP": sb+=2; rb.append("Liquidity sweep low")
    elif sweep=="SELL_SWEEP": ss+=2; rs.append("Liquidity sweep high")

    if sb>=MIN_SCORE and sb>ss:
        sl=price-max(atr_v*1.2, price*0.0015); risk=price-sl; tp=price+risk*RR
        return {"direction":"BUY","price":price,"sl":sl,"tp":tp,"score":sb,"buy_score":sb,"sell_score":ss,"rsi":rsi_v,"atr":atr_v,"structure":struct,"sweep":sweep,"reasons":rb}
    elif ss>=MIN_SCORE and ss>sb:
        sl=price+max(atr_v*1.2, price*0.0015); risk=sl-price; tp=price-risk*RR
        return {"direction":"SELL","price":price,"sl":sl,"tp":tp,"score":ss,"buy_score":sb,"sell_score":ss,"rsi":rsi_v,"atr":atr_v,"structure":struct,"sweep":sweep,"reasons":rs}
    else:
        return {"direction":"WAIT","price":price,"sl":None,"tp":None,"score":max(sb,ss),"buy_score":sb,"sell_score":ss,"rsi":rsi_v,"atr":atr_v,"structure":struct,"sweep":sweep,"reasons":[]}

def format_signal(r):
    price=r["price"]
    if r["direction"]=="WAIT":
        return f"🟡 <b>STAR TRADER V2</b>\n\nЦена: <b>{price:.2f}</b>\nBUY: {r['buy_score']} SELL: {r['sell_score']}\nRSI: {r['rsi']:.1f}\nStruct: {r['structure']}\nSweep: {r['sweep']}\n\n⏳ Сильного сигнала нет."
    emoji="🟢" if r["direction"]=="BUY" else "🔴"
    reasons="\n".join(f"• {x}" for x in r["reasons"])
    return f"{emoji} <b>XAUUSD {r['direction']}</b>\n\nEntry: <b>{price:.2f}</b>\nSL: <b>{r['sl']:.2f}</b>\nTP: <b>{r['tp']:.2f}</b>\nRR: <b>1:{RR:.0f}</b>\n\nScore: <b>{r['score']}</b>\nRSI: {r['rsi']:.1f}\nStructure: {r['structure']}\nSweep: {r['sweep']}\n\n<b>Подтверждения:</b>\n{reasons}\n\n⚠️ Проверь спред и новости."

def is_session():
    hour=(datetime.now(timezone.utc).hour+5)%24
    return SESSION_START<=hour<SESSION_END

def scan_loop():
    global last_signal_key
    print("Scan loop started")
    while True:
        try:
            res=analyze()
            if res: print(datetime.now().isoformat(), res["direction"], res["price"])
            if res and res["direction"]!="WAIT" and is_session() and CHAT_ID_ENV:
                key=datetime.now().strftime("%Y-%m-%d-%H") + f"-{res['direction']}-{round(res['price'],1)}"
                if key!=last_signal_key:
                    last_signal_key=key
                    send_telegram(CHAT_ID_ENV, format_signal(res))
        except Exception as e: print("SCAN ERROR:", e)
        time.sleep(300)

@app.route("/")
def home(): return "STAR TRADER V2 WEBHOOK LIVE"

@app.route("/webhook", methods=["POST"])
def webhook():
    data=request.get_json()
    if not data or "message" not in data: return "ok",200
    chat_id=data["message"]["chat"]["id"]
    text=data["message"].get("text","").strip()
    print(f"Got: {text}")
    if text.startswith("/start"):
        send_telegram(chat_id, "🟢 <b>STAR TRADER V2</b> работает!\n\n/gold - анализ\n/status - статус\n/help - команды")
    elif text.startswith("/gold"):
        send_telegram(chat_id, "🔍 Анализирую GOLD M15/H1/H4... 15 сек...")
        try:
            r=analyze()
            send_telegram(chat_id, format_signal(r) if r else "❌ Нет данных")
        except Exception as e:
            send_telegram(chat_id, f"Ошибка: {e}")
    elif text.startswith("/status"):
        sess="✅ Внутри сессии 12-22" if is_session() else "💤 Вне сессии"
        send_telegram(chat_id, f"🟢 <b>STAR TRADER V2</b>\nСтатус: работает\n{sess}\nИнструмент: GC=F\nАвтоскан: каждые 5 мин")
    elif text.startswith("/help"):
        send_telegram(chat_id, "📌 <b>Команды</b>\n/gold - анализ\n/status - статус\n/help - помощь")
    return "ok",200

# запуск автоскана в фоне
threading.Thread(target=scan_loop, daemon=True).start()

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",10000)))
