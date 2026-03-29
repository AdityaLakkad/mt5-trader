"""
inspect_data.py
===============
Live data inspector — prints bars, BB values, and engulfing check
every X seconds using the same functions as the main strategy.

Usage:
    python inspect_data.py
"""

import sys
import os
import time
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import MetaTrader5 as mt5
import pandas as pd

from connectors.mt5_connector import MT5Connector
from config.settings import MT5Config
from strategy.bb_engulfing_breakout import (
    compute_bb, is_engulfing,
    candle_touches_lower_bb, candle_touches_upper_bb,
    BBEngulfingParams,
)

# =============================================================================
# CONFIGURE HERE
# =============================================================================

SYMBOL       = "XAUUSD.GNE"
TIMEFRAME    = mt5.TIMEFRAME_M15
INTERVAL_SEC = 5
BAR_COUNT    = 5
SHOW_TICKS   = True
SHOW_BB      = True
SHOW_ENGULF  = True
MT5_PATH     = r"C:\Program Files\MetaTrader 5\terminal64.exe"

PARAMS = BBEngulfingParams(bb_period=20, bb_std_dev=2.0, engulf_tolerance_pct=10.0)

TF_MAP = {1:"M1",5:"M5",16390:"M15",16392:"M30",16385:"H1",16388:"H4",16408:"D1"}

# =============================================================================
# HELPERS
# =============================================================================

def sep(c="─", w=70): print(c * w)

def print_tick(connector, symbol):
    tick = connector.get_tick(symbol)
    if not tick:
        print("  ⚠️  No tick"); return
    print(f"\n  📍 LATEST TICK")
    sep()
    for k in ["time","bid","ask","last","volume"]:
        v = tick[k]
        print(f"  {k:<10}: {v:.5f}" if isinstance(v, float) else f"  {k:<10}: {v}")
    print(f"  {'spread':<10}: {tick['ask']-tick['bid']:.5f}")

def print_bars(df, count):
    recent = df.tail(count)
    print(f"\n  📊 LAST {count} CLOSED BARS")
    sep()
    print(f"  {'Time':<22} {'Open':>10} {'High':>10} {'Low':>10} {'Close':>10} {'Vol':>8}  Dir")
    sep()
    for ts, row in recent.iterrows():
        d = "🟢" if row["close"] >= row["open"] else "🔴"
        print(f"  {str(ts):<22} {row['open']:>10.5f} {row['high']:>10.5f} "
              f"{row['low']:>10.5f} {row['close']:>10.5f} {row['volume']:>8.0f}  {d}")

def print_bb(df):
    cur = df.iloc[-1]
    if math.isnan(cur.get("upper_bb", float("nan"))):
        print("\n  ⚠️  BB not ready"); return
    p, u, l, m = cur["close"], cur["upper_bb"], cur["lower_bb"], cur["middle_bb"]
    if p > u:   pos = "⬆️  ABOVE upper band"
    elif p < l: pos = "⬇️  BELOW lower band"
    else:       pos = f"↔️  Inside ({(p-l)/(u-l)*100:.1f}% from lower)"
    print(f"\n  📉 BOLLINGER BANDS")
    sep()
    print(f"  {'Upper':<12}: {u:.5f}")
    print(f"  {'Middle':<12}: {m:.5f}")
    print(f"  {'Lower':<12}: {l:.5f}")
    print(f"  {'Close':<12}: {p:.5f}")
    print(f"  {'Position':<12}: {pos}")

def print_engulf(df):
    if len(df) < 2: return
    cur, prev = df.iloc[-1], df.iloc[-2]
    cb = abs(cur["close"] - cur["open"])
    pb = abs(prev["close"] - prev["open"])
    ta = cb * (PARAMS.engulf_tolerance_pct / 100.0)
    eb = cb + ta * 2
    engulf = is_engulfing(cur, prev, PARAMS.engulf_tolerance_pct)
    print(f"\n  🕯️  ENGULFING CHECK (tolerance={PARAMS.engulf_tolerance_pct}%)")
    sep()
    print(f"  {'Current body':<22}: {cb:.5f}  ({'green' if cur['close']>cur['open'] else 'red'})")
    print(f"  {'Previous body':<22}: {pb:.5f}")
    print(f"  {'Tolerance each side':<22}: {ta:.5f}")
    print(f"  {'Expanded body':<22}: {eb:.5f}")
    print(f"  {'Result':<22}: {engulf or 'None'}")
    if "upper_bb" in df.columns and not math.isnan(cur.get("lower_bb", float("nan"))):
        if engulf == "bullish":
            ok = candle_touches_lower_bb(cur)
            print(f"  {'BB touch (lower)':<22}: {'✅ LONG SETUP VALID' if ok else '❌ open not below lower BB'}")
        elif engulf == "bearish":
            ok = candle_touches_upper_bb(cur)
            print(f"  {'BB touch (upper)':<22}: {'✅ SHORT SETUP VALID' if ok else '❌ open not above upper BB'}")

# =============================================================================
# MAIN
# =============================================================================

def main():
    connector = MT5Connector(MT5Config(path=MT5_PATH, timeout=10_000))
    if not connector.connect():
        print("❌ Could not connect to MT5.")
        return

    tf_str = TF_MAP.get(TIMEFRAME, str(TIMEFRAME))
    sep("═")
    print(f"  📊 LIVE DATA INSPECTOR  |  {SYMBOL}  |  {tf_str}  |  every {INTERVAL_SEC}s")
    sep("═")

    iteration = 0
    try:
        while True:
            iteration += 1
            now = pd.Timestamp.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            print(f"\n\n  🔄 UPDATE #{iteration}  —  {now}")

            df = connector.get_bars(SYMBOL, TIMEFRAME,
                                    count=PARAMS.bb_period + BAR_COUNT + 5)
            if df is None or df.empty:
                print("  ⚠️  No bar data — waiting...")
            else:
                if SHOW_BB:
                    df = compute_bb(df, PARAMS.bb_period, PARAMS.bb_std_dev)
                print_bars(df, BAR_COUNT)
                if SHOW_BB:    print_bb(df)
                if SHOW_ENGULF: print_engulf(df)

            if SHOW_TICKS:
                print_tick(connector, SYMBOL)

            sep("═")
            print(f"  ⏱  Next update in {INTERVAL_SEC}s  |  CTRL+C to stop")
            sep("═")
            time.sleep(INTERVAL_SEC)

    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        connector.disconnect()
        print("  Disconnected.\n")

if __name__ == "__main__":
    main()
