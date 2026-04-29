# BUGS_FIXED.md
## All Bugs Found and Fixed During Development

This document is important context for Claude Code — these bugs have already
been fixed and the fixes should not be reverted.

---

## Bug Group 1 — LiveOrderManager (live_order_manager.py)

All 6 bugs were in the original `LiveOrderManager` implementation.
The file was completely rewritten to fix them.

### Bug 1 — `open_positions` attribute missing

**Symptom:** Dashboard crashed with `AttributeError: 'LiveOrderManager' object
has no attribute 'open_positions'`.

**Root cause:** `LiveOrderManager` had no `open_positions` dict.
The dashboard iterates `order_manager.open_positions` to display trades.

**Fix:** Added `open_positions: Dict[str, Position]` maintained in sync
with real MT5 positions. Populated in `execute_order()`, cleaned in
`_monitor_closed_positions()`.

---

### Bug 2 — `closed_trades` never populated

**Symptom:** Recent trades table always empty. Win rate, PnL analytics
showed no data even after trades closed.

**Root cause:** `ClosedTrade` objects were never created. The `closed_trades`
list existed but was always empty.

**Fix:** `_monitor_closed_positions()` now creates `ClosedTrade` objects
from MT5 deal history when it detects a position closed.

---

### Bug 3 — `_known_tickets` not seeded on startup

**Symptom:** Positions opened before the engine started were never detected
as closed. If you opened a position manually or from a previous session,
the framework was blind to it.

**Root cause:** `_known_tickets` was an empty `set()` at init.

**Fix:** `_seed_from_mt5()` runs in `__init__`, loads all existing
positions with matching magic number into `_known_tickets` and
`open_positions`.

---

### Bug 4 — `positions_get()` called on every tick

**Symptom:** MT5 terminal performance degradation. Potential IPC timeout
errors. `positions_get()` called hundreds of times per second.

**Root cause:** `check_sl_tp()` called `monitor_closed_positions()` on
every tick with no throttling.

**Fix:** Time-based throttle in `check_sl_tp()`:
```python
now = time.monotonic()
if now - self._last_monitor_time < self.monitor_interval:
    return
self._last_monitor_time = now
```
Default `monitor_interval_s=2.0`.

---

### Bug 5 — `history_deals_get()` wrong API call

**Symptom:** Close detection silently failed. No closing FillEvents fired.
State machine stayed stuck at IN_TRADE.

**Root cause:**
```python
# WRONG — date range + position= is inconsistent across MT5 builds
deals = mt5.history_deals_get(from_time, to_time, position=ticket)
```

**Fix:**
```python
# CORRECT — keyword-only form is reliable
deals = mt5.history_deals_get(position=ticket)
# Fallback with wide date range if primary call returns empty
```

---

### Bug 6 — `strategy_id` empty on closing FillEvent

**Symptom:** `on_fill()` received closing fills with `strategy_id=""`.
The state machine lookup `self._state_managers[event.symbol]` worked
but the strategy_id check failed silently in some paths.
After first live trade closed, strategy stayed stuck IN_TRADE.

**Root cause:** Closing `FillEvent` constructed with hardcoded `strategy_id=""`.

**Fix:** Added `_ticket_to_strategy: Dict[int, str]` mapping each ticket
to the strategy_id that created the order. Closing FillEvent reads from
this dict: `strategy_id=self._ticket_to_strategy.pop(ticket, "")`.

---

## Bug Group 2 — `TradingEngine`

### Bug 7 — `mode` parameter not implemented

**Symptom:** `TypeError: TradingEngine.__init__() got an unexpected keyword
argument 'mode'`

**Root cause:** `mode="paper"/"live"` was documented and used in main.py
but never added to `TradingEngine.__init__`.

**Fix:** Added `mode: str = "paper"` parameter. Engine instantiates
`LiveOrderManager` when `mode="live"`, `OrderManager` when `mode="paper"`.

---

## Bug Group 3 — Dashboard

### Bug 8 — Rich Live swallows all log output

**Symptom:** Terminal showed only the dashboard. No log lines visible.
Strategy activity invisible. Impossible to debug.

**Root cause:** `Rich Live` takes over the entire terminal when using
`screen=False` mode in a background thread.

**Fix:** Complete rewrite of `dashboard.py`. Rich Live removed entirely.
Replaced with `logger.info()` calls every N seconds. Normal log scrolling
restored.

---

### Bug 9 — Dashboard crashes with LiveOrderManager

**Symptom:** After switching to live mode, dashboard thread crashed silently.

**Root cause:** Dashboard used `self.om.open_positions` and `self.om.closed_trades`
without `getattr()` guards. If LiveOrderManager had different attribute
names or types, it crashed.

**Fix:** All attribute access in dashboard uses `getattr(self.om, "attr", default)`.

---

## Bug Group 4 — Analytics

### Bug 10 — Excel export crashes with no trades

**Symptom:** `IndexError: At least one sheet must be visible` on session exit.

**Root cause:** `export_excel()` called `pd.ExcelWriter.save()` when
no sheets had been added (because there were no trades). openpyxl
requires at least one visible sheet.

**Fix:** Guard clause at top of `export_excel()`:
```python
if trades_df.empty and "error" in metrics:
    logger.warning("[Journal] No trades to export — skipping Excel report.")
    return None
```
Also removed Excel export from default session end flow. Now opt-in only.

---

## Bug Group 5 — Configuration

### Bug 11 — `import MetaTrader5 as mt5` in strategy file

**Symptom:** `ModuleNotFoundError` when running on a machine without
MT5 installed (e.g. for unit testing).

**Root cause:** `BBEngulfingParams` used `mt5.TIMEFRAME_M15` as a default
value, requiring MT5 to be importable at class definition time.

**Fix:** Use raw int value `16390` instead of `mt5.TIMEFRAME_M15`.
MT5 is now only imported inside `_get_symbol_info()` and `_get_current_balance()`
which are only called during live trading.

---

### Bug 12 — `ModuleNotFoundError: No module named 'config'`

**Symptom:** Import errors when running main file from wrong directory.

**Root cause:** Python path not set up. Imports relative to project root
fail if running from a subdirectory.

**Fix:** Add to top of all entry point scripts:
```python
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
```

---

## Bug Group 6 — MT5 Connectivity

### Bug 13 — `IPC initialize failed` / `IPC timeout`

**Symptom:** Connection fails with error codes -10003 or -10005.

**Root cause (multiple):**
- MT5 terminal not open
- Wrong `terminal64.exe` path specified
- Algo Trading disabled in MT5 (Trade allowed: False)
- Weekend / market closed (broker server offline)

**Resolution:** Not a code bug. Diagnostic checklist:
1. Open MT5 terminal manually
2. Verify Algo Trading button is GREEN
3. Tools → Options → Expert Advisors → Allow automated trading ✅
4. Find exact path via PowerShell: `Get-Process | Where {$_.Name -like "*terminal*"} | Select Path`
5. Markets closed Sunday (Gold/Forex open Sunday 5PM New York time)

---

## What NOT to Change

These decisions resolved specific bugs and must not be reverted:

1. `sm.enter_trade()` must fire BEFORE `self.signal_long()` in `on_tick()`
2. `_seed_from_mt5()` must run in `LiveOrderManager.__init__()`
3. `check_sl_tp()` must use time-based throttle, not call on every tick
4. `history_deals_get(position=ticket)` keyword-only form
5. Dashboard must NOT use Rich Live
6. `open_positions` dict must exist on LiveOrderManager
7. `strategy_id` must be tracked per ticket in `_ticket_to_strategy`
