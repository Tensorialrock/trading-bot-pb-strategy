import os
import time
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime, time as dtime
import pytz

# =============== CONFIG ===============

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SYMBOL = "QQQ"

TZ = pytz.timezone("US/Eastern")
SESSION_START = dtime(9, 30)
SESSION_END = dtime(11, 0)

MAX_TRADES_PER_DAY = 2  # total (long + short combined)

# =============== TELEGRAM ===============

def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=5)
        if not r.ok:
            print("Telegram error:", r.text)
    except Exception as e:
        print("Telegram exception:", e)

# =============== TIME HELPERS ===============

def now_est():
    return datetime.now(TZ)

def in_session_now() -> bool:
    n = now_est()
    return SESSION_START <= n.time() <= SESSION_END

def is_new_session(last_reset_date):
    n = now_est()
    if last_reset_date is None:
        return True
    if n.date() != last_reset_date and n.time() >= SESSION_START:
        return True
    return False

# =============== DATA HELPERS ===============

def get_ohlc(symbol: str, interval: str, lookback: str) -> pd.DataFrame:
    df = yf.download(symbol, period=lookback, interval=interval,
                     auto_adjust=False, progress=False)
    df.dropna(inplace=True)
    return df

# =============== FVG / BIAS LOGIC ===============

def find_fvgs(df: pd.DataFrame):
    fvgs = []
    for i in range(2, len(df)):
        h2 = df["High"].iloc[i - 2]
        l2 = df["Low"].iloc[i - 2]
        h1 = df["High"].iloc[i - 1]
        l1 = df["Low"].iloc[i - 1]
        h0 = df["High"].iloc[i]
        l0 = df["Low"].iloc[i]

        if l1 > h2 and l0 > h2:
            fvgs.append({
                "type": "bullish",
                "idx": df.index[i],
                "gap_low": h2,
                "gap_high": l1
            })

        if h1 < l2 and h0 < l2:
            fvgs.append({
                "type": "bearish",
                "idx": df.index[i],
                "gap_high": l2,
                "gap_low": h1
            })
    return fvgs

def determine_htf_bias():
    df_1h = get_ohlc(SYMBOL, "60m", "30d")
    fvgs = find_fvgs(df_1h)
    if not fvgs:
        return None

    last = fvgs[-7:]
    bulls = sum(1 for f in last if f["type"] == "bullish")
    bears = sum(1 for f in last if f["type"] == "bearish")

    if bulls > bears:
        return "bullish"
    if bears > bulls:
        return "bearish"
    return None

def find_key_levels(bias: str):
    df_15m = get_ohlc(SYMBOL, "15m", "5d")
    fvgs = find_fvgs(df_15m)
    if not fvgs:
        return []

    if bias == "bullish":
        return [f for f in fvgs if f["type"] == "bullish"]
    if bias == "bearish":
        return [f for f in fvgs if f["type"] == "bearish"]
    return []

def find_ifg_signal(bias: str, key_levels):
    if not key_levels:
        return None

    df_1m = get_ohlc(SYMBOL, "1m", "2d")
    recent = df_1m.iloc[-80:]

    last_key = key_levels[-1]
    gap_low = last_key["gap_low"]
    gap_high = last_key.get("gap_high", gap_low)

    touched = recent[(recent["Low"] <= gap_high) & (recent["High"] >= gap_low)]
    if touched.empty:
        return None

    fvgs_1m = find_fvgs(recent)
    if not fvgs_1m:
        return None

    if bias == "bullish":
        candidates = [f for f in fvgs_1m if f["type"] == "bullish"]
        direction = "LONG"
    else:
        candidates = [f for f in fvgs_1m if f["type"] == "bearish"]
        direction = "SHORT"

    if not candidates:
        return None

    ifg = candidates[-1]
    idx = ifg["idx"]
    candle = recent.loc[idx]

    if bias == "bullish":
        if candle["Close"] <= ifg["gap_high"]:
            return None
        entry = candle["Close"]
        stop = recent["Low"].loc[:idx].min()
    else:
        if candle["Close"] >= ifg["gap_low"]:
            return None
        entry = candle["Close"]
        stop = recent["High"].loc[:idx].max()

    risk = abs(entry - stop)
    if risk == 0:
        return None

    if bias == "bullish":
        tp = entry + risk
    else:
        tp = entry - risk

    return {
        "direction": direction,
        "entry": round(float(entry), 2),
        "stop": round(float(stop), 2),
        "tp": round(float(tp), 2),
        "ifg_time": idx
    }

def build_signal_message(bias, key_levels, signal):
    kl = key_levels[-1]
    gap_low = kl["gap_low"]
    gap_high = kl.get("gap_high", gap_low)

    text = (
        f"📈 *PB Blake Signal*\n"
        f"Symbol: {SYMBOL}\n"
        f"Bias: *{bias.upper()}*\n"
        f"Direction: *{signal['direction']}*\n\n"
        f"Key Level (15m FVG):\n"
        f"  Gap low: {gap_low:.2f}\n"
        f"  Gap high: {gap_high:.2f}\n\n"
        f"IFG Time (1m): {signal['ifg_time']}\n"
        f"Entry: {signal['entry']}\n"
        f"Stop: {signal['stop']}\n"
        f"TP (1:1 RR): {signal['tp']}\n\n"
        f"Session: 9:30–11:00 EST\n"
        f"Daily trade cap: {MAX_TRADES_PER_DAY}\n"
    )
    return text

# =============== MAIN LOOP ===============

def main_loop():
    print("Starting PB Blake Telegram bot...")
    last_signal_time = None
    trades_today = 0
    last_reset_date = None

    while True:
        try:
            if is_new_session(last_reset_date):
                trades_today = 0
                last_reset_date = now_est().date()
                print(f"New session day: reset trades_today to 0 ({last_reset_date})")

            if not in_session_now():
                time.sleep(30)
                continue

            if trades_today >= MAX_TRADES_PER_DAY:
                time.sleep(30)
                continue

            bias = determine_htf_bias()
            if bias is None:
                time.sleep(30)
                continue

            key_levels = find_key_levels(bias)
            if not key_levels:
                time.sleep(30)
                continue

            signal = find_ifg_signal(bias, key_levels)
            if signal is None:
                time.sleep(30)
                continue

            if last_signal_time == signal["ifg_time"]:
                time.sleep(30)
                continue

            msg = build_signal_message(bias, key_levels, signal)
            send_telegram_message(msg)
            print("Signal sent:", signal)

            last_signal_time = signal["ifg_time"]
            trades_today += 1

            time.sleep(30)

        except Exception as e:
            print("Error in main loop:", e)
            time.sleep(30)

if __name__ == "__main__":
    main_loop()
