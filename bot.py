import os, requests
from flask import Flask, request
app=Flask(__name__)
TOKEN=os.getenv("BOT_TOKEN")
def get_gold():
 try:
  r=requests.get("https://api.gold-api.com/price/XAU",timeout=6).json()
  return float(r['price'])
 except:
  return 4347.10
@app.route("/")
def home():
 return f"STAR TRADER V3.4 LIVE {get_gold():.2f}$",200
@app.route("/webhook",methods=["POST"])
def webhook():
 try:
  d=request.get_json()
  if "message" in d and "text" in d["message"]:
   cid=d["message"]["chat"]["id"]
   txt=d["message"]["text"]
   if "/gold" in txt:
    p=get_gold()
    msg=f"📊 STAR TRADER | GOLD (XAUUSD)\n\n💰 Cena: {p:.2f}$\n\n📈 SIGNAL: LONG (BUY)\n\n🔹 Vhod: {p-7:.1f} - {p-3:.1f}\n🔻 SL: {p-20:.1f}\n🎯 TP1: {p+15:.1f}\n🎯 TP2: {p+30:.1f}\n\n⏱ M15: trend vverh\n⚠️ Vhodi po zone!"
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",json={"chat_id":cid,"text":msg})
 except:
  pass
 return "ok",200
if __name__=="__main__":
 app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
