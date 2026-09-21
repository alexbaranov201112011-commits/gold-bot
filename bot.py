import os, requests
from flask import Flask, request
app=Flask(__name__)
TOKEN=os.getenv("BOT_TOKEN")
CHAT_ID=os.getenv("MY_CHAT_ID") # добавь в Render свой ID

def get_gold():
    try:
        r=requests.get("https://api.gold-api.com/price/XAU", timeout=5)
        return float(r.json()['price'])
    except:
        return 4347.10

def send(msg):
    if not CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": msg})
    except:
        pass

@app.route("/")
def home():
    return f"STAR TRADER V3.4 AUTO LIVE {get_gold()}"

@app.route("/check")
def check():
    # Это будет дергать UptimeRobot каждые 5 мин
    p=get_gold()
    buy_bottom = 4341.1
    buy_top = 4345.1
    
    if p < buy_bottom:
        send(f"🔴 SELL GOLD! Пробой {p} < 4341\nSL 4355 | TP 4300 / 4250")
    elif p >= buy_bottom and p <= buy_top:
        send(f"🟢 BUY GOLD! Цена {p} в зоне 4341-4345\nSL 4331 | TP 4380 / 4420")
    
    return f"checked {p}",200

@app.route("/webhook",methods=["POST"])
def webhook():
    try:
        d=request.get_json()
        if "message" in d and "text" in d["message"]:
            cid=d["message"]["chat"]["id"]
            txt=d["message"]["text"]
            if "/gold" in txt:
                p=get_gold()
                msg=f"📊 GOLD {p}\nBUY ZONE 4341-4345 | SELL <4341\nSL BUY 4331 | SL SELL 4355"
                requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": cid, "text": msg})
    except:
        pass
    return "ok",200

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT", 5000)))
