"""
bb_engulfing_main.py
====================
Main entry point for the BB Engulfing Breakout strategy.

Run from project root:
    python bb_engulfing_main.py

Configure everything in the sections below.
Stop with CTRL+C — report and CSVs save automatically.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import MetaTrader5 as mt5

from config.settings import (
    FrameworkConfig, MT5Config, AccountConfig,
    RiskConfig, DataConfig, EngineConfig,
)
from engine.trading_engine import TradingEngine
from analytics.dashboard   import Dashboard
from analytics.trade_journal import TradeJournal
from strategy.bb_engulfing_breakout import (
    BBEngulfingBreakoutStrategy,
    BBEngulfingParams,
    SizingMode,
    TPSLMode,
)


# =============================================================================
# 1. MT5 CONNECTION
# =============================================================================

MT5_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
# Find your path: open PowerShell and run:
#   Get-Process | Where-Object {$_.Name -like "*terminal*"} | Select-Object Path

SYMBOL   = "XAUUSD.GNE"   # check your broker's exact symbol name
INITIAL_BALANCE = 10_000.0


# =============================================================================
# 2. POSITION SIZING MODE — pick ONE, comment out the others
# =============================================================================

# ── Option A: Fixed lot size ──────────────────────────────────────────────────
SIZING = SizingMode.FIXED_LOTS
FIXED_LOT_SIZE   = 0.01

# ── Option B: Fixed USD risk per trade ────────────────────────────────────────
# SIZING = SizingMode.FIXED_USD
# RISK_AMOUNT_USD = 100.0    # always risk exactly $100 per trade

# ── Option C: Risk % of current balance ──────────────────────────────────────
# SIZING = SizingMode.RISK_PCT
# RISK_PCT = 1.0             # risk 1% of balance per trade

MAX_LOT_SIZE = 10.0          # hard ceiling — never exceed this
MIN_LOT_SIZE = 0.01          # hard floor


# =============================================================================
# 3. TP / SL MODE — pick ONE
# =============================================================================

# ── Option A: Points ─────────────────────────────────────────────────────────
# TPSL = TPSLMode.POINTS
TP_POINTS = 40.0             # take profit in points
SL_POINTS = 20.0             # stop loss in points

# ── Option B: Percentage ─────────────────────────────────────────────────────
TPSL = TPSLMode.PERCENT
TP_PCT = 1.0               # 2% above entry
SL_PCT = 0.50               # 1% below entry


# =============================================================================
# 4. STRATEGY PARAMETERS
# =============================================================================

params = BBEngulfingParams(
    timeframe            = mt5.TIMEFRAME_M1,

    bb_period            = 10,
    bb_std_dev           = 1.0,

    engulf_tolerance_pct = 10.0,
    expiry_candles       = 5000,
    max_trades_per_symbol= 1,

    # Sizing (values from section 2 above)
    sizing_mode    = SIZING,
    fixed_lot_size = FIXED_LOT_SIZE,
    risk_amount_usd= globals().get("RISK_AMOUNT_USD", 100.0),
    risk_pct       = globals().get("RISK_PCT", 1.0),
    max_lot_size   = MAX_LOT_SIZE,
    min_lot_size   = MIN_LOT_SIZE,

    # TP/SL (values from section 3 above)
    tpsl_mode  = TPSL,
    tp_points  = globals().get("TP_POINTS", 40.0),
    sl_points  = globals().get("SL_POINTS", 20.0),
    tp_pct     = globals().get("TP_PCT", 0.20),
    sl_pct     = globals().get("SL_PCT", 0.10),

    max_fill_attempts = 3,
)


# =============================================================================
# 5. FRAMEWORK CONFIG
# =============================================================================

config = FrameworkConfig(
    mt5=MT5Config(
        login    = 0,
        password = "",
        server   = "",
        path     = MT5_PATH,
        timeout  = 10_000,
    ),
    account=AccountConfig(
        initial_balance = INITIAL_BALANCE,
        currency        = "USD",
        leverage        = 100,
    ),
    risk=RiskConfig(
        risk_per_trade_pct = 2.0,
        max_open_trades    = 1,        # global max across all symbols
        default_sl_pips    = 100.0,
        default_tp_pips    = 100.0,
        max_drawdown_pct   = 100.0,     # pause trading if DD exceeds this
    ),
    data=DataConfig(
        symbols       = [SYMBOL],
        bar_timeframe = params.timeframe,
        bar_history   = 300,
        tick_enabled  = True,
        bar_enabled   = True,
    ),
    engine=EngineConfig(
        loop_interval_seconds      = 1.0,
        bar_check_interval_seconds = 5.0,
        log_level                  = "INFO",
    ),
)


# =============================================================================
# HELPERS
# =============================================================================

def print_config_summary():
    print("\n" + "=" * 50)
    print("  BB ENGULFING BREAKOUT — SESSION CONFIG")
    print("=" * 50)
    print(f"  Symbol       : {SYMBOL}")
    print(f"  Timeframe    : M15")
    print(f"  BB           : {params.bb_period} period / {params.bb_std_dev} std")
    print(f"  Tolerance    : {params.engulf_tolerance_pct}%")
    print(f"  Expiry       : {params.expiry_candles} candles")
    print(f"  Max trades   : {params.max_trades_per_symbol} per symbol")
    print(f"  Max DD guard : {config.risk.max_drawdown_pct}%")
    print("-" * 50)
    print(f"  Sizing mode  : {params.sizing_mode.value}")
    if params.sizing_mode == SizingMode.FIXED_LOTS:
        print(f"  Lot size     : {params.fixed_lot_size} lots (fixed)")
    elif params.sizing_mode == SizingMode.FIXED_USD:
        print(f"  Risk amount  : ${params.risk_amount_usd} per trade")
        print(f"  Lot limits   : {params.min_lot_size} – {params.max_lot_size}")
    else:
        print(f"  Risk %       : {params.risk_pct}% of balance")
        print(f"  Lot limits   : {params.min_lot_size} – {params.max_lot_size}")
    print("-" * 50)
    print(f"  TP/SL mode   : {params.tpsl_mode.value}")
    if params.tpsl_mode == TPSLMode.POINTS:
        print(f"  TP           : {params.tp_points} points")
        print(f"  SL           : {params.sl_points} points")
    else:
        print(f"  TP           : {params.tp_pct}%")
        print(f"  SL           : {params.sl_pct}%")
    print("=" * 50 + "\n")


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":

    print_config_summary()

    engine   = TradingEngine(config, mode="paper")
    strategy = BBEngulfingBreakoutStrategy(
        symbols         = config.data.symbols,
        event_queue     = engine.event_queue,
        params          = params,
        initial_balance = INITIAL_BALANCE,
    )
    engine.set_strategy(strategy)

    dashboard = Dashboard(
        order_manager = engine.order_manager,
        analytics     = engine.analytics,
        refresh_rate  = 2.0,
    )
    dashboard.start()

    try:
        engine.run()
    except KeyboardInterrupt:
        pass
    finally:
        dashboard.stop()
        try:
            journal = TradeJournal(
                analytics  = engine.analytics,
                output_dir = "./results",
                run_label  = strategy.strategy_id,
            )
            paths = journal.export()
            journal.append_to_master()
            if paths:
                print("\n📁 Results saved:")
                for name, path in paths.items():
                    print(f"   {name:<15} → {path}")
        except Exception as e:
            print(f"\n⚠️  Could not export journal: {e}")
        print("\nDone. Goodbye.\n")
