"""
AUTO TRADING BOT (BYBIT SPOT + AUTO SL/TP + TRAILING STOP)
-----------------------------------------------------------
✅ Bybit 현물 실거래
✅ EMA + RSI + 거래량 점수 기반 진입
✅ ATR 기반 SL/TP
✅ 트레일링 스탑으로 이익 보호
✅ 자동 로그/체결 저장
"""

import json, time, pandas as pd, numpy as np
from datetime import datetime
from dotenv import load_dotenv
import os, ccxt


# ✅ 전역 변수 선언 (이 위치!)
OPEN_POS = None         # 현재 보유 포지션 정보
TOTAL_PNL = 0.0         # 누적 손익 (USDT 기준)

load_dotenv()

def show_dashboard(ex):
    """터미널에 실시간 대시보드 출력"""
    global OPEN_POS, TOTAL_PNL
    os.system('cls' if os.name == 'nt' else 'clear')  # 콘솔 초기화

    # 현재 잔고
    try:
        bal = ex.fetch_balance()
        usdt = float(bal["free"].get("USDT", 0))
    except:
        usdt = 0.0

    print("========== 📊 BYBIT SPOT AUTO TRADER DASHBOARD ==========")
    print(f"🕒 {nowstr()}")
    print(f"💰 잔고 (USDT): {usdt:.2f}")
    print(f"📈 누적 손익 (PnL): {TOTAL_PNL:.2f} USDT")
    if OPEN_POS:
        print(f"🔹 보유: {OPEN_POS['symbol']} | 수량={OPEN_POS['qty']:.4f} | 진입가={OPEN_POS['entry']:.2f}")
        print(f"   SL={OPEN_POS['sl']:.2f} / TP={OPEN_POS['tp']:.2f}")
    else:
        print("🔸 현재 보유 포지션 없음")
    print("=========================================================\n")

# =========================
# 유틸
# =========================
def nowstr(): return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def log(msg):
    line = f"[{nowstr()}] {msg}"
    print(line)
    with open("live_log.txt", "a", encoding="utf-8") as f:
        f.write(line + "\n")

def load_config(path="config_real.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

# =========================
# 전역 상태
# =========================
OPEN_POS = None
MIN_QTY = {"BTC/USDT":0.0005,"ETH/USDT":0.005,"SOL/USDT":0.05}

# =========================
# 거래소 초기화
# =========================
def init_exchange():
    API_KEY = os.getenv("BYBIT_API_KEY")
    API_SECRET = os.getenv("BYBIT_API_SECRET")
    ex = ccxt.bybit({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
        "options": {
            "defaultType": "spot",
            "recvWindow": 20000,
            "adjustForTimeDifference": True
        }
    })
    ex.urls["api"] = {"public": "https://api.bybit.com","private":"https://api.bybit.com"}
    log("[INFO] Connected to Bybit SPOT REAL ✅")
    return ex

# =========================
# 지표
# =========================
def add_indicators(df, short, long, rsi_period, atr_period):
    df["ema_short"] = df["close"].ewm(span=short, adjust=False).mean()
    df["ema_long"]  = df["close"].ewm(span=long,  adjust=False).mean()

    d = df["close"].diff()
    gain, loss = d.clip(lower=0), (-d).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/rsi_period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/rsi_period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    df["rsi"] = 100 - (100 / (1 + rs))

    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high-low),(high-prev_close).abs(),(low-prev_close).abs()],axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/atr_period, adjust=False).mean()
    return df.dropna()

# =========================
# 데이터 / 점수
# =========================
def fetch_ohlcv_df(ex, symbol, timeframe, limit=200):
    data = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(data, columns=["timestamp","open","high","low","close","volume"])
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("datetime", inplace=True)
    return df

def score_symbol(ex, sym, cfg):
    df = fetch_ohlcv_df(ex, sym, cfg["timeframe"], limit=cfg["lookback_bars"])
    df = add_indicators(df, cfg["sma_short"], cfg["sma_long"], cfg["rsi_period"], cfg["atr_period"])
    last = df.iloc[-1]
    score = 0
    if last["ema_short"] > last["ema_long"]: score += 2
    if last["rsi"] > 50: score += 1
    if last["volume"] > df["volume"].rolling(20).mean().iloc[-1]: score += 0.5
    return score, last["atr"], float(last["close"])

def compare_symbols(ex, symbols, cfg):
    results = []
    for sym in symbols:
        try:
            s, atr, price = score_symbol(ex, sym, cfg)
            results.append({"symbol": sym, "score": s, "atr": atr, "price": price})
        except Exception as e:
            log(f"[WARN] {sym} 데이터 오류: {e}")
    df_score = pd.DataFrame(results).sort_values("score", ascending=False)
    return df_score.iloc[0]["symbol"], df_score

# =========================
# 매도 함수
# =========================
def sell_all(ex, symbol, reason="EXIT"):
    base = symbol.split("/")[0]
    try:
        bal = ex.fetch_balance()
        qty = float(bal["free"].get(base, 0))
        if qty > 0:
            ex.create_market_sell_order(symbol, qty)
            log(f"[SELL-{reason}] {symbol} qty={qty}")
            pd.DataFrame([[nowstr(), f"SELL_{reason}", symbol, "mkt", qty]]).to_csv(
                "live_trades.csv", mode="a", index=False, header=False
            )
            return True
    except Exception as e:
        log(f"[WARN] 매도 실패({reason}): {e}")
    return False

def flatten_others(ex, keep, symbols):
    for s in symbols:
        if s != keep:
            sell_all(ex, s, "SWITCH")

# =========================
# 매수 + 리스크
# =========================
def place_buy(ex, symbol, cfg, atr, price, capital):
    risk = capital * cfg["risk_per_trade"]
    stop_dist = atr * cfg["atr_multiplier_sl"]
    qty = max(round(risk / stop_dist, 4), MIN_QTY.get(symbol, 0.001))

    entry, sl, tp = price, price - stop_dist, price + atr * cfg["atr_multiplier_tp"]
    try:
        ex.create_market_buy_order(symbol, qty)
        log(f"[BUY] {symbol} @ {entry:.4f} qty={qty} → SL={sl:.4f} / TP={tp:.4f}")
        pd.DataFrame([[nowstr(),"BUY",symbol,entry,qty,capital]]).to_csv(
            "live_trades.csv", mode="a", index=False, header=False)
        return {"symbol":symbol,"qty":qty,"entry":entry,"sl":sl,"tp":tp,"trail_max":entry}
    except Exception as e:
        log(f"[WARN] 매수 실패: {e}")
        return None

# =========================
# SL/TP + 트레일링 감시
# =========================
def monitor_position(ex, cfg):
    global OPEN_POS
    if not OPEN_POS: return

    s, qty, sl, tp = OPEN_POS["symbol"], OPEN_POS["qty"], OPEN_POS["sl"], OPEN_POS["tp"]
    trail = float(cfg.get("trailing_percent", 0.02))

    try:
        last = float(ex.fetch_ticker(s)["last"])
    except Exception as e:
        log(f"[WARN] 티커 실패: {e}")
        return

    # 트레일링 스탑 상향
    if trail > 0 and last > OPEN_POS["trail_max"]:
        OPEN_POS["trail_max"] = last
        new_sl = last * (1 - trail)
        if new_sl > OPEN_POS["sl"]:
            log(f"[TRAIL] {s}: SL {OPEN_POS['sl']:.4f} → {new_sl:.4f}")
            OPEN_POS["sl"] = new_sl

    # 손절 / 익절 판정
    if last <= OPEN_POS["sl"]:
        if sell_all(ex, s, "SL"):
            log(f"[EXIT] SL hit ({last:.4f} ≤ {OPEN_POS['sl']:.4f})")
            OPEN_POS = None
    elif last >= tp:
        if sell_all(ex, s, "TP"):
            log(f"[EXIT] TP hit ({last:.4f} ≥ {tp:.4f})")
            OPEN_POS = None

# =========================
# 메인 루프
# =========================
def main():
    global OPEN_POS
    cfg = load_config("config_real.json")
    ex = init_exchange()
    interval = cfg.get("trade_interval",300)
    poll = cfg.get("price_check_interval",10)

    while True:
        try:
            show_dashboard(ex)  # 💡 대시보드 실시간 표시
            monitor_position(ex, cfg)
            if OPEN_POS is None:
                top, df = compare_symbols(ex, cfg["symbols"], cfg)
                log(f"[INFO] Best symbol: {top}")
                flatten_others(ex, top, cfg["symbols"])

                try:
                    bal = ex.fetch_balance()
                    cap = float(bal["free"].get("USDT",0))
                except: cap = 0
                s, atr, p = score_symbol(ex, top, cfg)
                pos = place_buy(ex, top, cfg, atr, p, cap)
                if pos: OPEN_POS = pos

            for _ in range(int(interval/poll)):
                monitor_position(ex, cfg)
                time.sleep(poll)

        except Exception as e:
            log(f"[ERROR] {e}")
            time.sleep(3)

if __name__ == "__main__":
    main()
