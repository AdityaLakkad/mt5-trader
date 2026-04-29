# DESIGN_DECISIONS.md
## Every Explicit Design Decision Made

This document captures every decision that was discussed and resolved during
the original development conversation. Each entry explains what was decided
and why so that future development doesn't revisit the same ground.

---

## Architecture Decisions

### D1 — Event-driven queue, not direct function calls

**Decision:** All modules communicate via a shared `Queue`, never by calling
each other's methods directly.

**Rationale:** Makes every module independently replaceable. Paper trading and
live trading share identical strategy/analytics code. Backtesting replays
events through the same dispatcher without any code changes.

**Implementation:** `engine/trading_engine.py` → `_process_event()` dispatches
based on `event.type`.

---

### D2 — One state machine per symbol, not one per strategy

**Decision:** `_state_managers` is a dict keyed by symbol name.

**Rationale:** A strategy trading multiple symbols needs independent state
per symbol. XAUUSD and EURUSD each have their own IDLE/WAITING/IN_TRADE
state. If both trigger simultaneously, they don't interfere.

**Implementation:** `BBEngulfingBreakoutStrategy.__init__` creates
one `SignalStateManager` per symbol.

---

### D3 — `enter_trade()` fires BEFORE `signal_long/short()`

**Decision:** State is set to IN_TRADE before pushing the signal to the queue.

**Rationale:** Ticks arrive faster than the queue drains. If state is still
WAITING when the next tick arrives, a duplicate order fires. Setting IN_TRADE
first uses the state check as a lock.

**Code location:** `on_tick()` in `bb_engulfing_breakout.py`.

---

### D4 — `fixed_volume` in signal metadata bypasses RiskManager sizing

**Decision:** Strategy calculates its own lot size and passes it via
`metadata["fixed_volume"]`. RiskManager checks for this key and skips
its own calculation.

**Rationale:** The strategy needs access to candle high/low for CANDLE mode
sizing, which RiskManager doesn't have. Keeping sizing in the strategy is
cleaner than passing candle data through events.

**Implementation:** `risk_manager.py` → `_calc_volume()` checks
`if "fixed_volume" in signal.metadata`.
RiskManager still enforces `max_open_trades` and `max_drawdown_pct`.

---

### D5 — Signal expiry resets automatically via new object creation

**Decision:** `set_signal()` always creates a brand new `PendingSignal`
dataclass rather than mutating an existing one.

**Rationale:** `candles_elapsed` resets to 0 automatically because a new
object is created. No manual reset code needed. Eliminates an entire class
of "forgot to reset the counter" bugs.

---

### D6 — Python framework language choice

**Decision:** Python via MT5 API (not MQL5 native).

**Rationale:** User explicitly chose Python for flexibility, pandas/numpy
access, and easier strategy development. MQL5 EA was built additionally
as a parallel deployment option, not a replacement.

---

### D7 — Single strategy at a time (not portfolio mode)

**Decision:** Engine supports one active strategy (`_strategy` single object).

**Rationale:** User's explicit choice. Portfolio mode (multiple strategies
in parallel) was discussed as a future option but not implemented.

---

### D8 — Bar close detection via polling, not MT5 callbacks

**Decision:** `BarFeed.poll()` compares latest bar timestamp to last seen,
fires event only when timestamp advances.

**Rationale:** MT5 Python API has no native callback/event system.
Polling every 5 seconds (configurable `bar_check_interval_seconds`)
is reliable and low overhead.

---

### D9 — Dashboard replaced with logger-based status lines

**Decision:** Rich Live dashboard removed. Replaced with `logger.info()`
calls every N seconds.

**Rationale:** Rich Live `screen=False` mode ran in a background thread and
completely swallowed all log output. Logs are more important than a pretty
dashboard for debugging strategy behaviour. Logger-based status allows
normal log scrolling.

**Implementation:** `analytics/dashboard.py` now calls `logger.info()` with
a portfolio summary line. `refresh_rate=60.0` default.

---

### D10 — `check_sl_tp()` throttled to every 2s in live mode

**Decision:** `LiveOrderManager.check_sl_tp()` is called on every tick by
the engine but internally only polls MT5 every `monitor_interval_s=2.0`.

**Rationale:** `mt5.positions_get()` is an IPC call. Calling it hundreds of
times per second hammers the MT5 terminal. MT5 manages SL/TP server-side
anyway — we only need to detect when positions close.

---

### D11 — MT5 `history_deals_get(position=ticket)` not `(from, to, position=...)`

**Decision:** Use `history_deals_get(position=ticket)` with keyword arg only,
not the date-range version.

**Rationale:** The date-range + position= combination has inconsistent
behaviour across MT5 builds. The keyword-only form is reliable.
A fallback with wide date range is implemented if the primary call returns empty.

---

### D12 — strategy_id tracked per ticket in LiveOrderManager

**Decision:** `_ticket_to_strategy: Dict[int, str]` maps each MT5 ticket
number to the strategy ID that created it.

**Rationale:** When MT5 closes a position via TP/SL, the closing FillEvent
needs `strategy_id` set correctly so `on_fill()` can find the right
state machine and reset it to IDLE. Without this, the strategy stays
stuck IN_TRADE permanently after the first live close.

---

## Strategy Parameter Decisions

### D13 — Engulfing tolerance formula: expand body on BOTH sides

**Decision:**
```python
tolerance_amount = curr_body * (tolerance_pct / 100.0)
expanded_body    = curr_body + (tolerance_amount * 2)  # both sides
```

**Rationale:** User specified "add X% on both sides of the body".
Higher tolerance = easier to trigger (more signals).
`0%` = strictest, `100%` = loosest.
This is counterintuitive from the name "tolerance" but matches the
user's specified logic.

---

### D14 — Default tolerance: 10%

**Decision:** `engulf_tolerance_pct = 10.0` as default.

**Rationale:** Balanced between too strict (no signals) and too loose
(too many false signals). User confirmed 10% as the starting value
pending backtesting to find optimal.

---

### D15 — Breakout confirmation: tick price, not candle close

**Decision:** Breakout is confirmed when a tick crosses the level
(intra-candle), not when a full candle closes above/below it.

**Rationale:** User's explicit preference. Faster entry, more slippage
risk, but earlier position capture.

---

### D16 — LTP vs Ask/Bid for breakout: Ask/Bid (Option 2)

**Decision:** Long breakout uses `event.ask`, short uses `event.bid`.

**Rationale:**
- LTP (`event.last`) is 0.0 on OTC CFD instruments (Gold, Forex, Indices)
- MT5 CFD brokers don't have a central exchange — no real "last traded price"
- Ask/Bid is always populated and represents actual executable prices
- Using ask for long: "the market is genuinely offering above your level"
- Using bid for short: "the market is genuinely bidding below your level"
- This is the correct real-world model for OTC instruments

---

### D17 — Entry price for TP/SL: current market price at time of order

**Decision:** TP and SL are calculated from `event.ask` (long) or `event.bid`
(short) at the moment the breakout tick fires.

**Rationale:** User's explicit choice. More realistic than calculating from
the signal candle close price.

---

### D18 — Signal replacement: replace immediately on new signal

**Decision:** When WAITING and a new engulfing setup appears, replace the
old breakout levels immediately.

**Rationale:** User's explicit choice. Alternatives discussed were:
- Ignore new signal until old one expires (rejected)
- Only update if new candle is bigger (rejected)
- Replace immediately (chosen)

---

### D19 — Signal expiry: N candles (not session end, not indefinite)

**Decision:** `expiry_candles=5` default. Signal cancelled after 5 bars.

**Rationale:** User's explicit choice. Session-end expiry rejected as
too complex. Indefinite rejected as too risky (stale signals). N candles
is clean and configurable.

---

### D20 — Max one direction at a time (no simultaneous LONG and SHORT pending)

**Decision:** State machine allows only one direction per symbol at a time.
New signal replaces old regardless of direction.

**Rationale:** User's explicit choice. A LONG setup appearing while SHORT
is pending replaces it — don't hold both.

---

## Sizing Decisions

### D21 — Three sizing modes with explicit enum

**Decision:** `SizingMode.FIXED_LOTS`, `SizingMode.FIXED_USD`, `SizingMode.RISK_PCT`

**Rationale:** User wanted to configure each independently with clear names.
FIXED_LOTS = simple testing, FIXED_USD = consistent dollar risk,
RISK_PCT = compounding/proportional risk.

---

### D22 — CANDLE mode: SL = candle low/high, lots dynamic

**Decision:** New `TPSLMode.CANDLE` where:
- Long SL = signal candle low
- Short SL = signal candle high
- TP = entry ± (SL_distance × rr_ratio)
- Lots = risk_amount ÷ (SL_distance × contract_size)

**Rationale:** User specified this exact model. Wide candles get smaller
lots (bigger risk per unit), tight candles get bigger lots. The math
self-adjusts every trade based on actual candle geometry.

**Formula:**
```
sl_distance      = entry - candle_low  (long)
sl_value_per_lot = sl_distance × contract_size
lot_size         = risk_amount ÷ sl_value_per_lot
```

---

### D23 — Lot size always clamped to min/max

**Decision:** After all sizing calculations, always apply:
```python
final = max(min_lot_size, min(max_lot_size, snapped))
final = max(volume_min,   min(volume_max,   final))
```

**Rationale:** Two layers of protection:
1. Strategy-level limits (user-configurable `min_lot_size`, `max_lot_size`)
2. Broker-level limits (from `symbol_info`)

---

### D24 — TP/SL calculated from entry price, not signal candle close

**Decision:** `entry_price = event.ask` (or bid), used for TP/SL math.

**Rationale:** User's explicit choice ("calculated from current market
price at time of order").

---

## Live Trading Decisions

### D25 — `open_positions` dict in LiveOrderManager mirrors paper mode

**Decision:** LiveOrderManager maintains its own `open_positions: Dict[str, Position]`
populated from real MT5 positions.

**Rationale:** Dashboard iterates `order_manager.open_positions` to display
open trades. LiveOrderManager must have this attribute or dashboard crashes.
The dict is populated on `execute_order()` success and cleaned on close detection.

---

### D26 — Seed existing positions on startup

**Decision:** `_seed_from_mt5()` runs in `__init__` before any trading starts.

**Rationale:** If the MT5 terminal already has open positions from a previous
session, they need to be in `_known_tickets` so closure is detected.
Without seeding, positions opened before the engine started are invisible.

---

### D27 — `type_filling = ORDER_FILLING_IOC`

**Decision:** MT5 orders use IOC (Immediate Or Cancel) fill policy.

**Rationale:** IOC is the most compatible fill type across brokers.
FOK (Fill Or Kill) is rejected by some brokers, especially during
high spread periods. IOC ensures partial fills are accepted.

---

### D28 — Magic number 234001

**Decision:** `magic_number=234001` default.

**Rationale:** Unique number identifies this EA's orders in MT5.
All position queries filter by magic number to avoid interfering
with manually placed trades or other EAs.

---

## Output Decisions

### D29 — CSV output only, no Excel default

**Decision:** `export()` generates CSV and text files. Excel export removed
from default flow.

**Rationale:** openpyxl crashed when called with zero trades (no sheets
visible error). CSV is simpler, always works, opens in any tool.
Excel export method still exists but is opt-in.

---

### D30 — Text report format (not Rich table on exit)

**Decision:** Session-end report uses plain text with ASCII separators.

**Rationale:** User's preference for simple text output that works in
any terminal and is easy to copy/paste.

---

### D31 — `results/` directory for all outputs

**Decision:** All CSVs, charts, and text reports go to `./results/` subdirectory.

**Rationale:** Keeps project root clean. `master_runs.csv` accumulates
one row per session for easy multi-session comparison.

---

## MQL5 Decisions

### D32 — MQL5 EA built as parallel option, not replacement

**Decision:** `BB_Engulfing_Breakout.mq5` implements the same logic natively.

**Rationale:** MQL5 is better for live trading (no IPC overhead, runs inside
terminal, direct broker connection). Python is better for research and
analytics. Both are maintained.

**Current gap:** MQL5 EA does not yet have CANDLE sizing mode.
Python has it, MQL5 doesn't.

---

### D33 — Bar close detection via `static datetime` trick in MQL5

**Decision:**
```mql5
static datetime last_bar_time = 0;
datetime current_bar_time = iTime(_Symbol, Timeframe, 0);
if (current_bar_time == last_bar_time) return;
last_bar_time = current_bar_time;
OnBarClose();
```

**Rationale:** OnTick fires on every tick. The static variable persists
between calls. New bar = timestamp changed. This is zero-latency compared
to Python's 5-second poll interval.
