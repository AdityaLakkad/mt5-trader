# ARCHITECTURE.md
## Event-Driven Architecture

---

## Core Principle

The framework is **event-driven**. Every piece of market data, every trading decision,
and every order confirmation flows as a typed event through a single shared queue.
No module ever calls another module directly — they only put events on the queue
and the engine dispatches them.

This means:
- Paper trading and live trading use **identical strategy code**
- Backtesting replays events through the **same pipeline** as live
- Every module is independently testable

---

## Event Flow Diagram

```
MT5 Terminal
    │
    ▼
DataFeed.poll_ticks()          ← every loop iteration (1s default)
DataFeed.poll_bars()           ← every 5s (bar_check_interval)
    │
    ├── TickEvent ─────────────► Strategy.on_tick()
    │                                │ (if breakout detected)
    │                                ▼
    │                           signal_long() / signal_short()
    │                                │
    │                           SignalEvent → queue
    │
    └── BarEvent  ─────────────► Strategy.on_bar()
                                     │ (if setup detected)
                                     ▼
                                SignalEvent → queue

                    ┌────────────────────────────────────┐
                    │         EVENT QUEUE                │
                    │  TickEvent, BarEvent,              │
                    │  SignalEvent, OrderEvent, FillEvent │
                    └────────────────────────────────────┘
                                     │
                                     ▼
                    Engine._process_event() dispatcher

SignalEvent ──────► RiskManager.process_signal()
                         │ checks: max_open_trades, max_drawdown
                         │ calculates: volume (or reads fixed_volume from metadata)
                         ▼
                    OrderEvent → queue

OrderEvent ────────► OrderManager.execute_order()   (paper)
                    OR
                    LiveOrderManager.execute_order() (live)
                         │
                         ▼
                    FillEvent → queue

FillEvent ─────────► Analytics.update()
                    + Strategy.on_fill()
                         │ resets state machine to IDLE
                         ▼
                    (loop continues)

TickEvent also ───► OrderManager.check_sl_tp()
                         │ paper: checks bid/ask vs SL/TP in memory
                         │ live:  polls MT5 every 2s for closed positions
                         ▼
                    FillEvent (closing) → queue → on_fill() → IDLE
```

---

## Module Responsibilities

### `config/settings.py`
- Single source of truth for all configuration
- Pure dataclasses — no logic
- `FrameworkConfig` composes: MT5Config, AccountConfig, RiskConfig, DataConfig, EngineConfig
- Never import MT5 here — keeps config importable on any platform

### `core/events.py`
- All event types as frozen dataclasses
- EventType, SignalDirection, OrderType, OrderSide, FillStatus enums
- Events: TickEvent, BarEvent, SignalEvent, OrderEvent, FillEvent
- `type` field is always set in `__post_init__` (never passed by caller)

### `core/base_strategy.py`
- Abstract base class
- Provides `signal_long()`, `signal_short()`, `signal_flat()` helpers
- These push `SignalEvent` onto the queue — strategy never touches queue directly
- Hooks: `on_start()`, `on_stop()`, `on_tick()`, `on_bar()`, `on_fill()`

### `connectors/mt5_connector.py`
- Only file that imports MetaTrader5 directly (besides strategy's `_get_symbol_info`)
- Wraps: `copy_rates_from_pos`, `symbol_info_tick`, `symbol_info`
- Caches symbol info to avoid repeated MT5 calls
- Always calls `mt5.symbol_select(symbol, True)` before data requests
- Returns plain Python/pandas objects — no MT5 types leak out

### `data/data_feed.py`
- `TickFeed`: polls every loop iteration, fires TickEvent only when timestamp advances
- `BarFeed`: polls every 5s, fires BarEvent only when new bar detected
- `BarFeed.initialise()`: pre-loads history on startup (warmup)
- BarEvent carries full `bars_df` DataFrame so strategy has full lookback
- Last unclosed bar is stripped from `get_bars()` response

### `orders/order_manager.py` (paper)
- Simulates fills at bid/ask + slippage
- Maintains `open_positions: Dict[str, Position]` and `closed_trades: List[ClosedTrade]`
- `check_sl_tp()`: called on every tick, closes positions when SL/TP hit
- `balance` and `equity` are simple float properties
- Commission deducted at open, not close

### `orders/live_order_manager.py` (live)
- Drop-in replacement — identical interface to paper OrderManager
- Uses `mt5.order_send()` for real execution
- `_known_tickets` set tracks open positions by MT5 ticket number
- `_ticker_to_strategy` dict maps ticket → strategy_id (critical for on_fill)
- `check_sl_tp()` is throttled to every 2s (not every tick)
- `_seed_from_mt5()` runs on init to load pre-existing positions
- Position monitoring: detects broker-closed positions via `history_deals_get(position=ticket)`

### `risk/risk_manager.py`
- Converts SignalEvent → OrderEvent
- Gates: max_open_trades, max_drawdown circuit breaker
- `fixed_volume` in signal metadata bypasses RiskManager sizing entirely
- Strategy handles its own sizing and passes `fixed_volume` in metadata
- RiskManager still enforces trade count and drawdown limits

### `analytics/performance.py`
- `update()`: called on every FillEvent, snapshots equity/balance
- `compute()`: calculates all metrics (win rate, PF, Sharpe, drawdown)
- `per_symbol_breakdown()`: DataFrame grouped by symbol
- `plot_equity_curve()`: matplotlib chart saved to file

### `analytics/dashboard.py`
- **NOT Rich Live** — logs a single status line every N seconds via `logger.info`
- Replaced original Rich Live dashboard because it swallowed log output
- `refresh_rate=60.0` default — prints portfolio summary every 60 seconds
- Uses `getattr()` guards to work with both paper and live order managers

### `analytics/trade_journal.py`
- Exports: trades CSV, summary CSV, per-symbol CSV, equity CSV, text report
- `append_to_master()`: appends one row to master_runs.csv across all sessions
- No Excel dependency in current version (removed to eliminate openpyxl crash)

### `engine/trading_engine.py`
- Main event loop with `mode="paper"` or `mode="live"` parameter
- Instantiates correct OrderManager based on mode
- `_force_stop()`: hard kills process after 4s if clean shutdown fails (Windows CTRL+C)
- Bar check is throttled separately from tick polling

### `engine/backtest_engine.py`
- Replays historical bars through exact same pipeline as live
- `_synthetic_ticks()`: generates 4 ticks per bar (O→L→H→C bullish, O→H→L→C bearish)
- Warmup period: skips BarEvents until enough history for indicators
- `load_dataframe()`: accepts CSV data for offline use

### `engine/optimizer.py`
- Grid search via `itertools.product` over PARAM_GRID
- Runs one BacktestEngine per combo
- Ranks by configurable metric (profit_factor, sharpe_ratio, etc.)
- Outputs: ranked CSV, text report, best_params.txt (copy-paste ready)

---

## Paper vs Live Mode

```python
# Paper (default — safe)
engine = TradingEngine(config, mode="paper")

# Live (real money)
engine = TradingEngine(config, mode="live")
```

Switching mode changes only the OrderManager. Everything else (strategy,
risk manager, analytics, data feed) is identical.

---

## Backtest vs Live

```
Live:     MT5 Terminal → DataFeed → Queue → Engine → OrderManager
Backtest: Historical DataFrame → BacktestEngine → Queue → OrderManager
```

Strategy code is zero-change between backtest and live.

---

## Threading Model

- Main thread: event loop (`engine.run()`)
- Dashboard thread: background daemon, logs status every N seconds
- LiveOrderManager: no separate thread — polling is throttled inline via time check
- All queue operations are thread-safe (Python Queue)
- `signal.SIGINT` handler sets `_running=False`, gives 4s for clean shutdown

---

## Symbol Naming

Different brokers rename MT5 symbols. The framework handles this by:
1. User specifies exact broker symbol name in config (e.g. "XAUUSD.GNE")
2. `mt5.symbol_select(symbol, True)` is called before every data request
3. `connection_tester.py` prints all available Gold symbols on startup

Common broker variants:
- XAUUSD, XAUUSD.GNE, XAUUSD., XAUUSDm, GOLD, XAUUSDpro
