# STRATEGY.md
## BB Engulfing Breakout — Full Strategy Documentation

---

## Strategy Concept

A volatility breakout strategy that combines two filters:
1. **Engulfing candle** — momentum signal (current candle overwhelms previous)
2. **Bollinger Band touch** — mean reversion context (price at extremes)

The combination identifies moments when price is at a volatility extreme AND
shows strong directional momentum, then enters only on confirmed breakout.

---

## Full Signal Logic

### Step 1 — Wait for bar close

Signal detection happens **only on completed (closed) candles**.
`on_bar()` fires when `BarFeed` detects a new bar in MT5.
`df.iloc[-1]` = last closed bar = "current candle".
`df.iloc[-2]` = bar before that = "previous candle".
The forming (unclosed) bar is never seen by the strategy.

### Step 2 — Compute Bollinger Bands

```python
middle_bb = close.rolling(bb_period).mean()       # default: 20 period SMA
upper_bb  = middle_bb + bb_std_dev * std           # default: 2.0 std dev
lower_bb  = middle_bb - bb_std_dev * std
```

### Step 3 — Engulfing check with tolerance

```python
curr_body        = abs(current.close - current.open)
prev_body        = abs(previous.close - previous.open)
tolerance_amount = curr_body * (tolerance_pct / 100.0)
expanded_body    = curr_body + (tolerance_amount * 2)  # expand both sides

engulfing = expanded_body > prev_body
```

**Tolerance meaning:**
- `0%`  → current body just needs to exceed previous body
- `10%` → current body + 10% on each side must exceed previous body
- `50%` → very forgiving — almost any directional candle qualifies
- Higher % = easier to trigger (more signals, potentially lower quality)

**Direction:**
- `bullish` if close > open (green candle)
- `bearish` if close < open (red candle)
- `None` if doji (open == close) or previous body is zero

### Step 4 — Bollinger Band position check

**Long setup (bullish engulfing + lower BB touch):**
```python
candle.open  < lower_bb   # opened below the band (was at extreme)
candle.close > lower_bb   # closed back above it (pushing through)
```

**Short setup (bearish engulfing + upper BB touch):**
```python
candle.open  > upper_bb   # opened above the band
candle.close < upper_bb   # closed back below it
```

### Step 5 — Set breakout levels

When both conditions pass, the state machine moves IDLE → WAITING:
```
breakout_high = signal candle high
breakout_low  = signal candle low
```

### Step 6 — Watch for breakout (every tick)

```python
# Long  breakout: ask price crosses above signal candle high
if event.ask >= pending.breakout_high:  → open long

# Short breakout: bid price crosses below signal candle low
if event.bid <= pending.breakout_low:   → open short
```

**Why ask for long, bid for short:**
- Ask = what you actually pay to buy (includes spread)
- Bid = what you actually receive when selling
- Using the wrong side would trigger on prices you can't trade at

**LTP vs Bid/Ask decision:**
- Was discussed but LTP (`event.last`) is 0.0 on most OTC CFD instruments
- Gold, Forex, Indices are OTC — no central exchange, no real LTP
- Ask/bid is always populated and represents executable prices
- Decision: use ask for long, bid for short (Option 2)

### Step 7 — Entry

Order placed at current market price (ask/bid at moment of breakout tick).
TP and SL calculated from entry price (not from signal candle close).

### Step 8 — Exit

Managed entirely by OrderManager after fill:
- Paper: `check_sl_tp()` compares bid/ask vs SL/TP on every tick
- Live: MT5 manages SL/TP server-side, Python polls for closure

---

## State Machine

```
IDLE  ──(engulfing + BB condition)──► WAITING
  ▲                                      │
  │◄──(expiry_candles exceeded)──────────┤
  │◄──(new signal replaces old)──────────┤ WAITING → WAITING (update levels)
  │                                      │ (tick crosses level)
  │                                      ▼
  │                                   IN_TRADE
  └──(TP/SL hit → FillEvent.pnl set)─────┘
  └──(fill rejected max times)───────────┘
  └──(stuck 3 bars with no fill)─────────┘
```

### State transition rules

| From | Event | To | Notes |
|---|---|---|---|
| IDLE | engulfing + BB | WAITING | stores high/low as breakout levels |
| WAITING | new engulfing + BB | WAITING | replaces levels, resets candles_elapsed |
| WAITING | N bars with no breakout | IDLE | expiry_candles configurable |
| WAITING | tick crosses level | IN_TRADE | state set BEFORE signal pushed |
| IN_TRADE | FillEvent with pnl set | IDLE | TP or SL hit |
| IN_TRADE | FillEvent REJECTED | WAITING or IDLE | retries up to max_fill_attempts |
| IN_TRADE | 3 bars with no fill confirmation | IDLE | safety timeout |

### Critical ordering: enter_trade() before signal push

```python
sm.enter_trade()      # ← state = IN_TRADE FIRST
self.signal_long(...)  # ← THEN push signal
```

Reason: The next tick arrives before the queue drains. If state is still
WAITING when the next tick arrives, a duplicate order fires.
Setting IN_TRADE first blocks all subsequent ticks immediately.

---

## Multi-Symbol Support

One `SignalStateManager` instance per symbol.
```python
self._state_managers = {
    "XAUUSD.GNE": SignalStateManager(...),
    "EURUSD":     SignalStateManager(...),
    ...
}
```

Each symbol's state is completely independent.
XAUUSD can be IN_TRADE while EURUSD is WAITING.

---

## Signal Expiry

```python
expiry_candles = 5  # cancel if no breakout within 5 bars
```

On each new bar close, `on_new_bar()` increments `candles_elapsed`.
When `candles_elapsed >= expiry_candles` → state resets to IDLE.

When a new signal replaces an old one (same symbol, new engulfing),
`set_signal()` creates a brand new `PendingSignal` object with
`candles_elapsed=0`. The reset happens automatically because a new
object is created — no manual reset needed.

---

## Bar Close Logging

Every bar close logs a full diagnostic line:

```
[BB_Engulfing_Breakout] BAR XAUUSD.GNE 🟢 |
O=3012.10 H=3015.50 L=3011.20 C=3014.80 |
body=2.70 expanded=3.24 prev=1.80 engulf=✅ bullish |
BB upper=3022.10 lower=3001.50 |
state=WAITING waiting_for=LONG >3015.5 bars_left=4
```

This fires even when conditions don't pass, so the terminal is never silent.
When state is WAITING, the log also shows which level is being watched
and how many bars remain before expiry.

Additional logs fire when:
- Engulfing passes but BB condition misses (with explanation why)
- A setup is detected (full level info)
- A breakout fires (entry, TP, SL, lots)
- A position is opened (fill confirmation)
- A position is closed (PnL)
- A fill is rejected (with retry status)

---

## Pure Functions

These functions are in the strategy file but have no side effects
and can be tested completely independently of MT5:

```python
is_engulfing(current, previous, tolerance_pct) → "bullish" | "bearish" | None
compute_bb(df, period, std_dev) → pd.DataFrame  (new df, never mutates input)
candle_touches_lower_bb(candle) → bool
candle_touches_upper_bb(candle) → bool
```

Unit tests for these are in `tests/test_strategy.py`.
Run with `python tests/test_strategy.py` — no MT5 needed.
