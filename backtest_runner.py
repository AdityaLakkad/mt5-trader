"""
backtest_runner.py
==================
Run the BB Engulfing Breakout strategy on historical MT5 data.

Run from project root:
    python backtest_runner.py

Outputs to ./results/backtest/:
    trades_*.csv
    summary_*.csv
    equity_*.csv
    summary_report_*.txt
    equity_curve.png
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import MetaTrader5 as mt5

from config.settings import (
    FrameworkConfig, MT5Config, AccountConfig,
    RiskConfig, DataConfig, EngineConfig,
)
from engine.backtest_engine import BacktestEngine
from analytics.trade_journal import TradeJournal
from strategy.bb_engulfing_breakout import (
    BBEngulfingBreakoutStrategy,
    BBEngulfingParams,
    SizingMode,
    TPSLMode,
)

# =============================================================================
# CONFIGURE BACKTEST
# =============================================================================

MT5_PATH        = r"C:\Program Files\MetaTrader 5\terminal64.exe"
SYMBOL          = "XAUUSD.GNE"
TIMEFRAME       = mt5.TIMEFRAME_M15
BARS_TO_LOAD    = 3000               # how many bars of history to load
INITIAL_BALANCE = 10_000.0
OUTPUT_DIR      = "./results/backtest"

# Spread simulation per symbol (pips) — match your broker
SPREADS = {
    "XAUUSD.GNE": 3.0,
    "EURUSD":     1.2,
    "GBPUSD":     1.5,
    "USDJPY":     1.4,
    "BTCUSD":    50.0,
    "US30":       3.0,
    "NAS100":     2.0,
}

# Strategy parameters to test
params = BBEngulfingParams(
    timeframe            = TIMEFRAME,
    bb_period            = 20,
    bb_std_dev           = 2.0,
    engulf_tolerance_pct = 10.0,
    expiry_candles       = 5,
    max_trades_per_symbol= 1,

    sizing_mode    = SizingMode.FIXED_USD,
    risk_amount_usd= 100.0,
    max_lot_size   = 10.0,
    min_lot_size   = 0.01,

    tpsl_mode  = TPSLMode.POINTS,
    tp_points  = 40.0,
    sl_points  = 20.0,
)

# =============================================================================
# RUN
# =============================================================================

def run_backtest(params: BBEngulfingParams,
                 label: str = "") -> dict:
    """
    Run a single backtest with given params.
    Returns the metrics dict.
    """
    config = FrameworkConfig(
        mt5=MT5Config(path=MT5_PATH, timeout=10_000),
        account=AccountConfig(initial_balance=INITIAL_BALANCE),
        risk=RiskConfig(
            max_open_trades  = 5,
            max_drawdown_pct = 50.0,    # wide for backtests
        ),
        data=DataConfig(
            symbols       = [SYMBOL],
            bar_timeframe = TIMEFRAME,
            bar_history   = 300,
        ),
        engine=EngineConfig(log_level="WARNING"),
    )

    engine = BacktestEngine(
        config=config,
        spread_pips=SPREADS,
        slippage_pips=0.5,
        commission_per_lot=7.0,
    )

    print(f"\nConnecting to MT5 ...")
    if not engine.connect():
        print("❌ Could not connect to MT5.")
        raise SystemExit(1)

    print(f"Loading {BARS_TO_LOAD} bars for {SYMBOL} ...")
    if not engine.load_symbol(SYMBOL, TIMEFRAME, BARS_TO_LOAD):
        print(f"❌ Could not load data for {SYMBOL}.")
        engine.disconnect()
        raise SystemExit(1)

    strategy = BBEngulfingBreakoutStrategy(
        symbols=[SYMBOL],
        event_queue=engine.event_queue,
        params=params,
        initial_balance=INITIAL_BALANCE,
    )
    engine.set_strategy(strategy)

    print(f"\nRunning backtest [{label or 'default'}] ...")
    results = engine.run()

    # Save results
    run_label = f"backtest_{label}" if label else "backtest"
    journal = TradeJournal(
        analytics=engine.analytics,
        output_dir=OUTPUT_DIR,
        run_label=run_label,
    )
    paths = journal.export()
    journal.append_to_master(
        master_path=f"{OUTPUT_DIR}/master_backtest_runs.csv"
    )

    engine.plot_equity(
        save_path=f"{OUTPUT_DIR}/equity_{run_label}.png"
    )
    engine.disconnect()

    return results, paths


def print_results(results: dict, label: str = "") -> None:
    if "error" in results:
        print(f"\n[{label}] No trades: {results['error']}")
        return
    print(f"\n{'='*50}")
    print(f"  BACKTEST RESULTS — {label}")
    print(f"{'='*50}")
    print(f"  Trades       : {results['total_trades']}")
    print(f"  Win Rate     : {results['win_rate_pct']}%")
    print(f"  Profit Factor: {results['profit_factor']}")
    print(f"  Net PnL      : ${results['total_net_pnl']:+}")
    print(f"  Return %     : {results['return_pct']:+}%")
    print(f"  Max DD %     : {results['max_drawdown_pct']}%")
    print(f"  Sharpe       : {results['sharpe_ratio']}")
    print(f"{'='*50}")


if __name__ == "__main__":
    results, paths = run_backtest(params, label="default")
    print_results(results, label="default")

    print("\n📁 Files saved:")
    for name, path in paths.items():
        print(f"   {name:<15} → {path}")
