from flask import Flask
import threading, os
app = Flask(__name__)
@app.route('/')
def home(): return "Bot is alive!"
def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
threading.Thread(target=run_flask, daemon=True).start()

# --- НИЖЕ ВСТАВЬ ВЕСЬ ТВОЙ СТАРЫЙ КОД БОТА ---
import telebot
import yfinance as yf
import matplotlib.pyplot as plt
... и т.д. все что было ...
