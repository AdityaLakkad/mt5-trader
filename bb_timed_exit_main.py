"""
bb_timed_exit_main.py
=====================
BB Engulfing Breakout — Timed Exit strategy.

Exit rules:
  - Loss >= 10 points at any tick → close immediately
  - 11 minutes after entry → close at market (profit or loss, no choice)

Lot size: always 1.0.

Run:
    python bb_timed_exit_main.py

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
from analytics.dashboard import Dashboard
from analytics.trade_journal import TradeJournal
from strategy.bb_timed_exit import BBTimedExitStrategy, BBTimedExitParams


# =============================================================================
# 1. MT5 CONNECTION
# =============================================================================

MT5_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
SYMBOL   = "XAUUSD.GNE"
INITIAL_BALANCE = 10_000.0


# =============================================================================
# 2. STRATEGY PARAMETERS  (edit these to tune)
# =============================================================================

params = BBTimedExitParams(
    timeframe            = mt5.TIMEFRAME_M15,

    bb_period            = 20,
    bb_std_dev           = 2.0,
    engulf_tolerance_pct = 10.0,
    max_candle_size_points = 0.0,   # 0 = disabled; set e.g. 30.0 to skip wide candles
    expiry_candles       = 5,       # signal expires if no breakout within N bars

    fixed_lot_size       = 1.0,     # always trade exactly 1 lot

    # Exit rules
    sl_points            = 10.0,    # 10 points on XAUUSD = $0.10 move × 100 contract × 1 lot = $10
    exit_after_minutes   = 11,      # compulsory market exit after 11 minutes
)


# =============================================================================
# 3. FRAMEWORK CONFIG
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
        max_open_trades    = 1,
        default_sl_pips    = 100.0,
        default_tp_pips    = 100.0,
        max_drawdown_pct   = 100.0,
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
    print("\n" + "=" * 52)
    print("  BB TIMED EXIT — SESSION CONFIG")
    print("=" * 52)
    print(f"  Symbol         : {SYMBOL}")
    print(f"  Timeframe      : M15")
    print(f"  BB             : {params.bb_period} period / {params.bb_std_dev} std")
    print(f"  Tolerance      : {params.engulf_tolerance_pct}%")
    print(f"  Signal expiry  : {params.expiry_candles} candles")
    print(f"  Max candle     : {params.max_candle_size_points or 'disabled'} pts")
    print("-" * 52)
    print(f"  Lot size       : {params.fixed_lot_size} lot (fixed)")
    print(f"  SL             : {params.sl_points} points loss → close immediately")
    print(f"  Timed exit     : {params.exit_after_minutes} min after entry → close always")
    print(f"  Max DD guard   : {config.risk.max_drawdown_pct}%")
    print("=" * 52 + "\n")


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":

    print_config_summary()

    engine   = TradingEngine(config, mode="paper")
    strategy = BBTimedExitStrategy(
        symbols     = config.data.symbols,
        event_queue = engine.event_queue,
        params      = params,
    )
    engine.set_strategy(strategy)

    dashboard = Dashboard(
        order_manager = engine.order_manager,
        analytics     = engine.analytics,
        refresh_rate  = 120.0,
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
                print("\nResults saved:")
                for name, path in paths.items():
                    print(f"   {name:<15} → {path}")
        except Exception as e:
            print(f"\nCould not export journal: {e}")
        print("\nDone. Goodbye.\n")
