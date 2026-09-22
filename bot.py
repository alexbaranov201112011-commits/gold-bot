def get_winrate(closes):
    wins=0; total=0
    if len(closes)<80: return 68
    for i in range(60, len(closes)-12):
        hist = closes[:i]
        if len(hist)<60: continue
        # EMA50
        k=2/51; e=hist[0]
        for v in hist[1:]: e=v*k+e*(1-k)
        ema=e
        # RSI14
        gains=[]; losses=[]
        for kk in range(i-14, i):
            if kk<=0: continue
            d=closes[kk]-closes[kk-1]
            if d>0: gains.append(d)
            else: losses.append(abs(d))
        if not gains or not losses: continue
        rs = (sum(gains)/14) / (sum(losses)/14 + 0.0001)
        rsi = 100 - (100/(1+rs))

        price=hist[-1]
        is_buy=None
        if price>ema+3 and rsi>=52: is_buy=True
        elif price<ema-3 and rsi<=48: is_buy=False
        else: continue

        total+=1
        future = closes[i:i+10]
        for fp in future:
            if is_buy:
                if fp >= price+18: wins+=1; break
                if fp <= price-18: break
            else:
                if fp <= price-18: wins+=1; break
                if fp >= price+18: break
    return int(wins/total*100) if total>20 else 65

def get_levels(closes, highs, lows):
    cur = closes[-1]
    last = closes[-50:]
    h50 = highs[-50:]
    l50 = lows[-50:]
    # поддержка ниже цены, но не текущая
    below = sorted([x for x in l50 if x < cur-1], reverse=True)
    above = sorted([x for x in h50 if x > cur+1])
    s1 = below[0] if below else min(l50)
    s2 = min(l50)
    r1 = above[0] if above else max(h50)
    r2 = max(h50)
    return round(s1,2), round(s2,2), round(r1,2), round(r2,2)
