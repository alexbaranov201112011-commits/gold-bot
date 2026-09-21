import os, requests
from flask import Flask, request

BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TOKEN")
app = Flask(__name__)

def send_msg(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})

@app.route("/")
def home():
    return "GOLD v6.2 FIXED DIRECT"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "message" not in data:
        return "ok", 200
    
    chat_id = data["message"]["chat"]["id"]
    text = data["message"].get("text", "")
    
    print(f"Got message: {text} from {chat_id}")
    
    if text.startswith("/start"):
        send_msg(chat_id, "Бот проснулся ✅ Напиши /gold")
    elif "/gold" in text:
        send_msg(chat_id, "🔍 Анализирую GOLD... Подожди 15 сек...")
        # тут твой анализ золота, пока заглушка
        try:
            # ЗАМЕНИ ЭТО НА СВОЙ АНАЛИЗ
            result = "📈 GOLD: Ожидаю лонг\nSL: 2580\nTP: 2610\n\n(это заглушка, вставь свою логику)"
            send_msg(chat_id, result)
        except Exception as e:
            print(f"Error: {e}")
            send_msg(chat_id, f"Ошибка: {e}")
    else:
        send_msg(chat_id, "Напиши /gold для сигнала")
    
    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)))
