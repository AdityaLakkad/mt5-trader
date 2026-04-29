# SIZING_MODES.md
## Position Sizing and TP/SL Modes — Complete Reference

---

## Sizing Modes

### Mode 1: FIXED_LOTS

```python
sizing_mode    = SizingMode.FIXED_LOTS
fixed_lot_size = 0.1
```

Always trades exactly `fixed_lot_size` lots. No math, no balance consideration.

**Use for:** Initial testing, validating strategy logic before worrying about sizing.

---

### Mode 2: FIXED_USD

```python
sizing_mode     = SizingMode.FIXED_USD
risk_amount_usd = 100.0   # risk exactly $100 per trade
```

**Formula (when tpsl_mode is POINTS):**
```
sl_value_per_lot = sl_points × point_size × contract_size
lot_size         = risk_amount_usd ÷ sl_value_per_lot
```

**Gold example (sl_points=20):**
```
sl_value_per_lot = 20 × 0.01 × 100 = $20
lot_size         = $100 ÷ $20      = 5.0 lots
```

Always risks exactly $100 regardless of account balance.
Balance grows or shrinks but $100 risk stays constant.

**Use for:** Consistent dollar risk, professional money management.

---

### Mode 3: RISK_PCT

```python
sizing_mode = SizingMode.RISK_PCT
risk_pct    = 1.0   # risk 1% of balance
```

**Formula:**
```
risk_amount      = balance × risk_pct / 100
sl_value_per_lot = sl_points × point_size × contract_size
lot_size         = risk_amount ÷ sl_value_per_lot
```

**Gold example ($10,000 balance, sl_points=20):**
```
risk_amount      = $10,000 × 1% = $100
sl_value_per_lot = $20
lot_size         = $100 ÷ $20   = 5.0 lots

Next month ($12,000 balance):
risk_amount      = $12,000 × 1% = $120
lot_size         = $120 ÷ $20   = 6.0 lots  ← scales automatically
```

**Use for:** Compounding — position size grows with profits, shrinks with losses.

---

## TP/SL Modes

### Mode 1: POINTS

```python
tpsl_mode = TPSLMode.POINTS
tp_points = 40.0
sl_points = 20.0
```

Fixed distance from entry in price points.

**Gold example (entry=3015.55, point=0.01):**
```
TP = 3015.55 + (40 × 0.01) = 3015.95
SL = 3015.55 - (20 × 0.01) = 3015.35
```

**Risk/reward:** 40pt TP ÷ 20pt SL = 2:1 RR (fixed regardless of candle)

---

### Mode 2: PERCENT

```python
tpsl_mode = TPSLMode.PERCENT
tp_pct    = 2.0   # 2% above entry
sl_pct    = 1.0   # 1% below entry
```

**Gold example (entry=3015.55):**
```
TP = 3015.55 × 1.02 = 3075.86
SL = 3015.55 × 0.99 = 2985.39
```

**Use for:** Percentage-based exits — scales with price level.

---

### Mode 3: CANDLE (Dynamic)

```python
tpsl_mode       = TPSLMode.CANDLE
rr_ratio        = 3.0               # TP = 3× the SL distance
sizing_mode     = SizingMode.FIXED_USD
risk_amount_usd = 100.0             # risk $100 per trade
```

**How it works:**

SL is placed at the signal candle's low (long) or high (short).
TP is derived from SL distance × risk/reward ratio.
Lot size is derived from how far SL is from entry.

**LONG example:**
```
Signal candle: high=3015.50, low=3011.20
Entry (ask):   3015.55
SL price:      3011.20  (candle low)
SL distance:   3015.55 - 3011.20 = 4.35 points
TP price:      3015.55 + (4.35 × 3.0) = 3028.60

Sizing:
sl_value_per_lot = 4.35 × 100 = $435
lot_size         = $100 ÷ $435 = 0.23 lots → rounded to 0.23
```

**SHORT example:**
```
Signal candle: high=3020.00, low=3015.00
Entry (bid):   3014.95
SL price:      3020.00  (candle high)
SL distance:   3020.00 - 3014.95 = 5.05 points
TP price:      3014.95 - (5.05 × 3.0) = 2999.80

Sizing:
sl_value_per_lot = 5.05 × 100 = $505
lot_size         = $100 ÷ $505 = 0.20 lots → rounded to 0.20
```

**Key property — wide candles get smaller lots:**
```
Wide candle (10pt body):  sl_dist=10, sl_val=$1000, lots=0.10
Tight candle (2pt body):  sl_dist=2,  sl_val=$200,  lots=0.50
```

This is mathematically correct — you're always risking the same dollar amount.
The position size adjusts to the actual risk of each specific trade.

**Why this is the preferred mode for this strategy:**
The signal candle's high/low are the natural breakout levels.
Setting SL at the candle low (long) means: "if price goes back below
where this signal started, I was wrong." The TP at RR × SL distance
gives a mathematical edge requirement: you need to be right less than
50% of the time to be profitable at RR=3 (25% win rate breaks even).

---

## Lot Size Clamping

All modes apply two layers of clamping:

```python
# Layer 1: strategy limits (user-configurable)
final = max(min_lot_size, min(max_lot_size, raw_lots))

# Layer 2: broker limits (from MT5 symbol_info)
final = max(volume_min, min(volume_max, final))
```

Volume is also snapped to the broker's volume step:
```python
snapped = round(round(raw / volume_step) * volume_step, 8)
```

Example with step=0.01: 0.2345 → 0.23

---

## Configuration in `bb_engulfing_main.py`

```python
params = BBEngulfingParams(

    # ── Choose ONE sizing mode ────────────────────────────────────
    sizing_mode     = SizingMode.FIXED_USD,   # or FIXED_LOTS or RISK_PCT
    fixed_lot_size  = 0.1,                    # used when FIXED_LOTS
    risk_amount_usd = 100.0,                  # used when FIXED_USD
    risk_pct        = 1.0,                    # used when RISK_PCT
    min_lot_size    = 0.01,                   # all modes
    max_lot_size    = 10.0,                   # all modes

    # ── Choose ONE TP/SL mode ────────────────────────────────────
    tpsl_mode  = TPSLMode.CANDLE,   # or POINTS or PERCENT
    tp_points  = 40.0,              # used when POINTS
    sl_points  = 20.0,              # used when POINTS
    tp_pct     = 2.0,               # used when PERCENT
    sl_pct     = 1.0,               # used when PERCENT
    rr_ratio   = 3.0,               # used when CANDLE
)
```

---

## Required Import

When using `TPSLMode` in `bb_engulfing_main.py`:

```python
from strategy.bb_engulfing_breakout import (
    BBEngulfingBreakoutStrategy,
    BBEngulfingParams,
    SizingMode,
    TPSLMode,    # ← must import this too
)
```
