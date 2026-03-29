"""
engine/optimizer.py
====================
Grid search optimizer for the BB Engulfing Breakout strategy.

Runs the backtest engine across every combination of parameters
you specify and ranks results by your chosen metric.

Usage
-----
    python optimizer.py

Output
------
    ./results/optimization/
        optimization_results.csv    ← all runs ranked by metric
        optimization_report.txt     ← plain text summary
        best_params.txt             ← copy-paste ready params block
"""

import sys
import os
import itertools
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import MetaTrader5 as mt5

from config.settings import (
    FrameworkConfig, MT5Config, AccountConfig,
    RiskConfig, DataConfig, EngineConfig,
)
from engine.backtest_engine import BacktestEngine
from strategy.bb_engulfing_breakout import (
    BBEngulfingBreakoutStrategy,
    BBEngulfingParams,
    SizingMode,
    TPSLMode,
)

import pandas as pd

# =============================================================================
# CONFIGURE
# =============================================================================

MT5_PATH        = r"C:\Program Files\MetaTrader 5\terminal64.exe"
SYMBOL          = "XAUUSD.GNE"
TIMEFRAME       = mt5.TIMEFRAME_M15
BARS_TO_LOAD    = 3000
INITIAL_BALANCE = 10_000.0
OUTPUT_DIR      = "./results/optimization"

# Metric to rank by — options:
#   profit_factor | total_net_pnl | win_rate_pct | sharpe_ratio | return_pct
RANK_BY = "profit_factor"

# Minimum trades required for a result to be valid
MIN_TRADES = 10

# =============================================================================
# PARAMETER GRID
# Define lists of values to test for each parameter.
# Total runs = product of all list lengths.
# Keep it small to start — 3×3×3 = 27 runs.
# =============================================================================

PARAM_GRID = {
    "bb_period":            [15, 20, 25],
    "bb_std_dev":           [1.5, 2.0, 2.5],
    "engulf_tolerance_pct": [0.0, 10.0, 20.0],
    "expiry_candles":       [3, 5, 8],
    "tp_points":            [30.0, 40.0, 60.0],
    "sl_points":            [15.0, 20.0, 30.0],
}

# Fixed params (not optimized)
FIXED_PARAMS = dict(
    sizing_mode    = SizingMode.FIXED_USD,
    risk_amount_usd= 100.0,
    max_lot_size   = 10.0,
    min_lot_size   = 0.01,
    tpsl_mode      = TPSLMode.POINTS,
)

SPREADS = {"XAUUSD.GNE": 3.0}

# =============================================================================
# OPTIMIZER
# =============================================================================

def make_config() -> FrameworkConfig:
    return FrameworkConfig(
        mt5=MT5Config(path=MT5_PATH, timeout=10_000),
        account=AccountConfig(initial_balance=INITIAL_BALANCE),
        risk=RiskConfig(max_open_trades=5, max_drawdown_pct=50.0),
        data=DataConfig(symbols=[SYMBOL], bar_timeframe=TIMEFRAME, bar_history=300),
        engine=EngineConfig(log_level="ERROR"),   # silent during optimization
    )


def run_single(param_combo: dict) -> dict:
    """Run one backtest with given parameter combo. Returns metrics + params."""
    all_params = {**param_combo, **FIXED_PARAMS}
    params = BBEngulfingParams(**all_params)

    config = make_config()
    engine = BacktestEngine(
        config=config,
        spread_pips=SPREADS,
        slippage_pips=0.5,
        commission_per_lot=7.0,
    )

    if not engine.connect():
        return {"error": "connection_failed", **param_combo}

    if not engine.load_symbol(SYMBOL, TIMEFRAME, BARS_TO_LOAD):
        engine.disconnect()
        return {"error": "data_load_failed", **param_combo}

    strategy = BBEngulfingBreakoutStrategy(
        symbols=[SYMBOL],
        event_queue=engine.event_queue,
        params=params,
        initial_balance=INITIAL_BALANCE,
    )
    engine.set_strategy(strategy)

    try:
        results = engine.run()
    except Exception as e:
        engine.disconnect()
        return {"error": str(e), **param_combo}

    engine.disconnect()

    if "error" in results:
        return {"error": results["error"], **param_combo}

    return {**param_combo, **results}


def run_optimization() -> pd.DataFrame:
    """Run all parameter combinations and return ranked DataFrame."""

    keys   = list(PARAM_GRID.keys())
    values = list(PARAM_GRID.values())
    combos = list(itertools.product(*values))

    total = len(combos)
    print(f"\n{'='*55}")
    print(f"  BB ENGULFING BREAKOUT — PARAMETER OPTIMIZER")
    print(f"{'='*55}")
    print(f"  Symbol       : {SYMBOL}")
    print(f"  Bars         : {BARS_TO_LOAD}")
    print(f"  Total runs   : {total}")
    print(f"  Rank by      : {RANK_BY}")
    print(f"{'='*55}\n")

    all_results = []
    start_time  = time.time()

    for i, combo in enumerate(combos, 1):
        param_combo = dict(zip(keys, combo))
        label = " | ".join(f"{k}={v}" for k, v in param_combo.items())
        print(f"  [{i:>3}/{total}] {label}", end=" ... ", flush=True)

        result = run_single(param_combo)
        all_results.append(result)

        if "error" not in result:
            trades = result.get("total_trades", 0)
            metric = result.get(RANK_BY, 0)
            print(f"trades={trades} {RANK_BY}={metric}")
        else:
            print(f"ERROR: {result['error']}")

    elapsed = time.time() - start_time
    print(f"\n  Done in {elapsed:.1f}s")

    # Build DataFrame
    df = pd.DataFrame(all_results)

    # Filter by min trades
    if "total_trades" in df.columns:
        valid = df[df["total_trades"] >= MIN_TRADES].copy()
    else:
        valid = df.copy()

    if valid.empty:
        print(f"\n⚠️  No runs had >= {MIN_TRADES} trades. Lower MIN_TRADES.")
        return df

    # Rank
    if RANK_BY in valid.columns:
        valid = valid.sort_values(RANK_BY, ascending=False)

    return valid


def save_results(df: pd.DataFrame) -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Full CSV
    csv_path = f"{OUTPUT_DIR}/optimization_results.csv"
    df.to_csv(csv_path, index=False)
    print(f"\n📁 Results saved → {csv_path}")

    if df.empty:
        return

    best = df.iloc[0]

    # Plain text report
    lines = [
        "=" * 55,
        "  OPTIMIZATION REPORT",
        f"  Symbol    : {SYMBOL}",
        f"  Bars      : {BARS_TO_LOAD}",
        f"  Total runs: {len(df)}",
        f"  Ranked by : {RANK_BY}",
        "=" * 55,
        "",
        "  TOP 5 PARAMETER SETS",
        "-" * 55,
    ]
    for i, (_, row) in enumerate(df.head(5).iterrows(), 1):
        lines.append(f"\n  #{i}")
        for k in list(PARAM_GRID.keys()):
            lines.append(f"    {k:<28}: {row.get(k, '—')}")
        lines.append(f"    {'---':<28}")
        for metric in ["total_trades", "win_rate_pct", "profit_factor",
                        "total_net_pnl", "return_pct", "max_drawdown_pct", "sharpe_ratio"]:
            lines.append(f"    {metric:<28}: {row.get(metric, '—')}")

    lines += ["", "=" * 55, "", "  BEST PARAMS (copy-paste into bb_engulfing_main.py)", "-" * 55]
    lines.append("  params = BBEngulfingParams(")
    for k in PARAM_GRID.keys():
        lines.append(f"      {k:<28} = {best.get(k)},")
    for k, v in FIXED_PARAMS.items():
        lines.append(f"      {k:<28} = {v},")
    lines.append("  )")

    txt_path = f"{OUTPUT_DIR}/optimization_report.txt"
    with open(txt_path, "w") as f:
        f.write("\n".join(lines))
    print(f"📁 Report saved   → {txt_path}")

    # Best params file
    best_path = f"{OUTPUT_DIR}/best_params.txt"
    with open(best_path, "w") as f:
        f.write("\n".join(lines[-10:]))
    print(f"📁 Best params    → {best_path}")

    # Print top 3 to console
    print("\n  TOP 3 RESULTS:")
    print(f"  {'#':<3} ", end="")
    print(" ".join(f"{k[:10]:<10}" for k in PARAM_GRID.keys()), end="  ")
    print(f"{'trades':>6}  {RANK_BY[:12]:>12}")
    print("  " + "-" * 70)
    for i, (_, row) in enumerate(df.head(3).iterrows(), 1):
        print(f"  {i:<3} ", end="")
        print(" ".join(f"{str(row.get(k,''))[:10]:<10}" for k in PARAM_GRID.keys()), end="  ")
        print(f"{int(row.get('total_trades',0)):>6}  {row.get(RANK_BY,0):>12.3f}")


if __name__ == "__main__":
    results_df = run_optimization()
    save_results(results_df)
