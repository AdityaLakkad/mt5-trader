# CLAUDE.md
## MT5 Paper Trading Framework — Claude Code Context

This file gives Claude Code full context to continue development of this project
without losing any decisions, architecture choices, or implementation details
from the original conversation that built this framework.

---

## Project Overview

A **modular, event-driven paper trading framework** for MetaTrader 5, written in Python.
Built around a specific trading strategy: **Bollinger Band Engulfing Breakout**.

The framework supports:
- Paper trading (simulated fills)
- Live trading (real MT5 order execution)
- Backtesting (historical bar replay)
- Parameter optimization (grid search)
- Native MQL5 Expert Advisor (same logic, runs inside MT5)

**Platform:** Windows only (MT5 Python API is Windows-exclusive)
**Python version:** 3.11+
**Primary symbol:** XAUUSD.GNE (Gold — broker-specific suffix)

---

## Repository Structure

```
mt5_paper_trader/
│
├── CLAUDE.md                           ← this file
├── README.md                           ← user-facing tutorial
├── requirements.txt
│
├── bb_engulfing_main.py                ← PRIMARY ENTRY POINT (run this)
├── backtest_runner.py                  ← backtest entry point
├── connection_tester.py                ← MT5 connection diagnostic
├── inspect_data.py                     ← live data inspector (no trades)
│
├── config/
│   └── settings.py                     ← all config dataclasses
│
├── core/
│   ├── events.py                       ← all event dataclasses
│   └── base_strategy.py                ← abstract strategy base class
│
├── connectors/
│   └── mt5_connector.py                ← MT5 terminal wrapper
│
├── data/
│   └── data_feed.py                    ← tick + bar polling
│
├── strategy/
│   ├── bb_engulfing_breakout.py        ← THE main strategy (most active file)
│   └── BB_Engulfing_Breakout.mq5       ← native MQL5 EA (same logic)
│
├── orders/
│   ├── order_manager.py                ← paper trading execution
│   └── live_order_manager.py           ← real MT5 order execution
│
├── risk/
│   └── risk_manager.py                 ← signal sizing + drawdown guard
│
├── analytics/
│   ├── performance.py                  ← metrics + equity curve
│   ├── dashboard.py                    ← terminal status logger (NOT Rich Live)
│   └── trade_journal.py                ← CSV + text export
│
├── engine/
│   ├── trading_engine.py               ← main event loop
│   ├── backtest_engine.py              ← historical replay engine
│   └── optimizer.py                    ← grid search optimizer
│
└── tests/
    └── test_strategy.py                ← unit tests (no MT5 needed)
```

---

## Docs Index

| File | Contents |
|---|---|
| `docs/ARCHITECTURE.md` | Full event-driven architecture and flow |
| `docs/STRATEGY.md` | BB Engulfing Breakout strategy logic |
| `docs/DESIGN_DECISIONS.md` | Every explicit design decision made |
| `docs/SIZING_MODES.md` | All 3 sizing modes + CANDLE mode explained |
| `docs/LIVE_TRADING.md` | Live order manager, bugs fixed, deployment |
| `docs/BUGS_FIXED.md` | All bugs found and fixed during development |

---

## Current Status

### Working ✅
- Paper trading via `bb_engulfing_main.py`
- MT5 connection with auto-detection
- Bar close signal detection (M15 default)
- State machine (IDLE → WAITING → IN_TRADE)
- Signal expiry after N candles
- Fill rejection handling with retry
- Stuck-trade timeout (3 bars)
- All 3 sizing modes (FIXED_LOTS, FIXED_USD, RISK_PCT)
- All 3 TP/SL modes (POINTS, PERCENT, CANDLE)
- CANDLE mode: dynamic lot sizing from candle high/low
- Live order execution via `LiveOrderManager`
- Dashboard replaced with logger-based status lines
- CSV trade journal export
- Unit tests for pure functions
- Backtest engine
- MQL5 EA

### Known Issues / Next Steps
- Live trading: `Trade allowed: False` when Algo Trading button not enabled in MT5
- `monitor_interval_s=2.0` in LiveOrderManager — poll frequency is configurable
- MQL5 EA does not yet have CANDLE sizing mode (only Python version has it)
- Optimizer does not yet support CANDLE tpsl_mode grid search

---

## Key Files to Know

### `strategy/bb_engulfing_breakout.py`
The most-edited file. Contains:
- `BBEngulfingParams` dataclass (all strategy config)
- `SizingMode` enum (FIXED_LOTS, FIXED_USD, RISK_PCT)
- `TPSLMode` enum (POINTS, PERCENT, CANDLE)
- `is_engulfing()` pure function
- `compute_bb()` pure function
- `candle_touches_lower_bb()` / `candle_touches_upper_bb()` pure functions
- `StrategyState` enum (IDLE, WAITING, IN_TRADE)
- `PendingSignal` dataclass
- `SignalStateManager` class
- `BBEngulfingBreakoutStrategy` class

### `bb_engulfing_main.py`
The entry point. Has 3 clearly marked config sections:
1. MT5 connection settings
2. Sizing mode selection (uncomment one)
3. TP/SL mode selection (uncomment one)

### `orders/live_order_manager.py`
The most bug-prone file. Was fully rewritten. Key behaviours:
- Maintains `open_positions` dict (mirrors paper OrderManager)
- Seeds existing positions from MT5 on startup
- Throttles position monitoring to every 2 seconds (not every tick)
- Uses `history_deals_get(position=ticket)` (correct MT5 API)
- Tracks `strategy_id` per ticket so `on_fill()` resets state correctly

---

## Running the Project

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Test MT5 connection
python connection_tester.py

# 3. Inspect live data (no trades)
python inspect_data.py

# 4. Run paper trading
python bb_engulfing_main.py

# 5. Run backtest
python backtest_runner.py

# 6. Run unit tests (no MT5 needed)
python tests/test_strategy.py

# 7. Run optimizer
python engine/optimizer.py
```

---

## MT5 Connection Notes

- MT5 terminal must be **open and logged in** before running any script
- **Algo Trading button must be GREEN** in MT5 toolbar
- `Tools → Options → Expert Advisors → Allow automated trading ✅`
- Some brokers rename symbols: XAUUSD → XAUUSD.GNE, XAUUSD., XAUUSDm etc
- Run `connection_tester.py` to find exact symbol names
- `Trade allowed: False` = Algo Trading button is off, not a code issue
- IPC timeout on weekends = market closed, broker server offline (normal)
