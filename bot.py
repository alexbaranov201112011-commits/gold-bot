import os, telebot, yfinance as yf, pandas as pd
from flask import Flask, request
from datetime import datetime
import pytz
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("BOT_") or os.environ.get("BOT")
if not TOKEN:
    raise ValueError("BOT_TOKEN не найден! Добавь в Render Environment")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
AKTAU_TZ = pytz.timezone('Asia/Aqtau')
LOT = 0.03
STOP_DOLLAR = 7
TAKE_DOLLAR = 18

def get_gold_data():
    tickers = ["XAUUSD=X", "GC=F"]
    for ticker in tickers:
        try:
            df = yf.download(ticker, period="10d", interval="15m", progress=False, auto_adjust=True)
            if df is None or len
