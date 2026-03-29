# MT5 Paper Trading Framework
### Bollinger Band Engulfing Breakout Strategy

A modular, event-driven paper trading framework for MetaTrader 5 — built in Python.

---

## 📁 Project Structure

```
mt5_paper_trader/
├── bb_engulfing_main.py        ← Entry point — run this
├── connection_tester.py        ← Test MT5 connection first
├── inspect_data.py             ← Live data inspector
├── requirements.txt
│
├── config/
│   └── settings.py             ← All framework config dataclasses
│
├── core/
│   ├── events.py               ← Event types (Tick, Bar, Signal, Order, Fill)
│   └── base_strategy.py        ← Abstract base — subclass to build strategies
│
├── connectors/
│   └── mt5_connector.py        ← MT5 terminal wrapper
│
├── data/
│   └── data_feed.py            ← Tick + Bar polling, fires events
│
├── strategy/
│   └── bb_engulfing_breakout.py ← The main strategy
│
├── orders/
│   └── order_manager.py        ← Paper execution, SL/TP, portfolio state
│
├── risk/
│   └── risk_manager.py         ← Sizing, drawdown guard, max trades
│
├── analytics/
│   ├── performance.py          ← Metrics, Sharpe, equity curve
│   ├── dashboard.py            ← Live Rich terminal dashboard
│   └── trade_journal.py        ← CSV + text report exporter
│
└── engine/
    └── trading_engine.py       ← Main event loop
```

---

## ⚡ Quick Start

### Step 1 — Install dependencies

```bash
pip install -r requirements.txt
```

> **Windows only** — MetaTrader5 Python package only works on Windows
> with MT5 terminal installed.

---

### Step 2 — Test your MT5 connection

Open MetaTrader 5 terminal manually and log in. Then:

```bash
python connection_tester.py
```

Expected output:
```
Result : True
Error  : (1, 'Success')
Terminal      : MetaTrader 5
Connected     : True
Trade allowed : True
Gold symbols  : ['XAUUSD.GNE']   ← note your broker's exact symbol name
```

If it fails:
1. Make sure MT5 terminal is **open and logged in**
2. Enable **Algo Trading** (green button in MT5 toolbar → Tools → Options → Expert Advisors)
3. Update `MT5_PATH` in `connection_tester.py` to your actual `terminal64.exe` path

Find your path with PowerShell:
```powershell
Get-Process | Where-Object {$_.Name -like "*terminal*"} | Select-Object Path
```

---

### Step 3 — Update your symbol name

In `bb_engulfing_main.py`, update line:
```python
SYMBOL = "XAUUSD.GNE"   # ← change to what connection_tester.py printed
```

---

### Step 4 — Run

```bash
python bb_engulfing_main.py
```

Stop with **CTRL+C** — results save automatically to `./results/`.

---

## 📊 Strategy Logic

### Signal Detection (on every M15 bar close)

```
1. Compute Bollinger Bands (20 period, 2.0 std dev)
2. Check if current candle is engulfing:
      expanded_body = curr_body + (curr_body × tolerance% × 2)
      expanded_body > previous candle body → engulfing confirmed
3. Check Bollinger Band touch:
      LONG  : green engulfing candle opens below lower BB, closes above lower BB
      SHORT : red engulfing candle opens above upper BB, closes below upper BB
4. Set breakout levels = high and low of the signal candle
5. Wait for breakout (up to expiry_candles bars)
```

### Breakout Execution (on every tick)

```
LONG  : ask price >= signal candle high → open long
SHORT : bid price <= signal candle low  → open short
```

### State Machine

```
IDLE  ──(engulfing + BB)──►  WAITING  ──(breakout tick)──►  IN_TRADE
  ▲                             │                               │
  │◄──(expiry_candles exceeded)─┤                               │
  │◄──(new signal replaces old)─┘                               │
  └────────────────────────────────────(TP / SL hit)────────────┘
```

---

## 🎛️ Configuration Guide

All configuration lives at the top of `bb_engulfing_main.py`.

### MT5 Connection

```python
MT5_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
SYMBOL   = "XAUUSD.GNE"
```

---

## 📐 Position Sizing Modes

The framework supports **three sizing modes**. Pick one and comment out the others.

---

### Mode 1 — Fixed Lot Size

Always trade the same number of lots regardless of balance.

```python
SIZING = SizingMode.FIXED_LOTS
FIXED_LOT_SIZE = 0.1          # always trade 0.1 lots

MAX_LOT_SIZE = 10.0           # hard ceiling (safety)
MIN_LOT_SIZE = 0.01           # hard floor
```

Best for: beginners, consistency, simple paper trading validation.

---

### Mode 2 — Fixed USD Risk

Risk exactly $X per trade. Lot size is calculated automatically from SL distance.

```python
SIZING = SizingMode.FIXED_USD
RISK_AMOUNT_USD = 100.0       # risk exactly $100 per trade

MAX_LOT_SIZE = 10.0           # never trade more than this
MIN_LOT_SIZE = 0.01
```

Formula:
```
sl_value_per_lot = sl_points × point_size × contract_size
lot_size         = risk_amount_usd ÷ sl_value_per_lot

Example (Gold, SL=20pts):
  sl_value_per_lot = 20 × 0.01 × 100 = $20
  lot_size         = $100 ÷ $20 = 5.0 lots
```

Best for: consistent dollar risk per trade, professional money management.

---

### Mode 3 — Risk % of Balance

Risk a percentage of your current balance per trade. Lot size grows/shrinks with your account.

```python
SIZING = SizingMode.RISK_PCT
RISK_PCT = 1.0                # risk 1% of balance per trade

MAX_LOT_SIZE = 10.0           # never trade more than this regardless of balance
MIN_LOT_SIZE = 0.01
```

Formula:
```
risk_amount      = balance × risk_pct / 100
sl_value_per_lot = sl_points × point_size × contract_size
lot_size         = risk_amount ÷ sl_value_per_lot

Example ($10,000 balance, 1%, SL=20pts on Gold):
  risk_amount      = $10,000 × 1% = $100
  sl_value_per_lot = $20
  lot_size         = $100 ÷ $20 = 5.0 lots

If balance grows to $12,000:
  risk_amount = $120 → lot_size = 6.0 lots  (scales up automatically)
```

Best for: compounding growth, long-term position sizing.

---

## 🎯 TP / SL Modes

### Mode 1 — Points

```python
TPSL      = TPSLMode.POINTS
TP_POINTS = 40.0    # take profit 40 points above entry
SL_POINTS = 20.0    # stop loss 20 points below entry
```

For Gold (point = 0.01):
```
Entry  = 3012.50
TP     = 3012.50 + (40 × 0.01) = 3013.10
SL     = 3012.50 - (20 × 0.01) = 3012.10
```

---

### Mode 2 — Percentage

```python
TPSL   = TPSLMode.PERCENT
TP_PCT = 2.0        # take profit 2% above entry
SL_PCT = 1.0        # stop loss 1% below entry
```

```
Entry  = 3012.50
TP     = 3012.50 × 1.02 = 3072.75
SL     = 3012.50 × 0.99 = 2982.38
```

---

## 🔧 Strategy Parameters

```python
params = BBEngulfingParams(
    # Timeframe (raw MT5 int)
    timeframe            = mt5.TIMEFRAME_M15,  # M1=1 M5=5 M15=16390 H1=16385

    # Bollinger Bands
    bb_period            = 20,      # SMA period for BB calculation
    bb_std_dev           = 2.0,     # standard deviation multiplier

    # Engulfing filter
    engulf_tolerance_pct = 10.0,    # expand current body by 10% each side
                                    # 0  = any engulfing qualifies (loose)
                                    # 10 = current body + 20% must beat prev
                                    # 50 = current body + 100% must beat prev

    # Signal expiry
    expiry_candles       = 5,       # cancel signal if no breakout in 5 bars

    # Trade limits
    max_trades_per_symbol= 1,       # max 1 open trade per symbol at a time

    # Fill retry
    max_fill_attempts    = 3,       # retry rejected fills up to 3 times
)
```

---

## 📈 What You See While Running

### Terminal logs (real-time):
```
2026-03-23 09:00:00  INFO  Connected to MT5 | Terminal: MetaTrader 5
2026-03-23 09:00:01  INFO  Pre-loading 300 bars for 1 symbols ...
2026-03-23 09:00:01  INFO  [BB_Engulfing_Breakout] Started
2026-03-23 09:15:00  INFO  🟢 LONG SETUP XAUUSD.GNE | H=3015.50 L=3011.20
2026-03-23 09:15:04  INFO  ✅ LONG BREAKOUT XAUUSD.GNE | ask=3015.55 lots=5.00
2026-03-23 09:15:04  INFO  [OrderManager] OPEN XAUUSD.GNE BUY 5.00 @ 3015.55
2026-03-23 09:18:00  INFO  [OrderManager] CLOSE XAUUSD.GNE @ 3015.95 | PnL=+200.00 | reason=TP
```

### Live dashboard (refreshes every 2s):
```
╭──── 🚀 MT5 Paper Trader  2026-03-23 09:18:00 UTC ────╮
│  BALANCE    EQUITY    OPEN P&L  TOTAL P&L  DD   OPEN  CLOSED
│  $10,193    $10,193   $0        +$193      0%   0     3
╰──────────────────────────────────────────────────────╯
```

### On CTRL+C — files saved to `./results/`:
```
trades_BB_Engulfing_Breakout_20260323_091800.csv     ← every trade
summary_BB_Engulfing_Breakout_20260323_091800.csv    ← metrics
summary_report_BB_Engulfing_Breakout_20260323.txt    ← plain text report
equity_BB_Engulfing_Breakout_20260323_091800.csv     ← equity curve data
master_runs.csv                                       ← all-time run history
equity_curve.png                                      ← chart image
```

---

## 🔍 Live Data Inspector

Before running the strategy, inspect real data to verify your setup:

```bash
python inspect_data.py
```

Configure at the top of the file:
```python
SYMBOL       = "XAUUSD.GNE"
TIMEFRAME    = mt5.TIMEFRAME_M15
INTERVAL_SEC = 5          # refresh every 5 seconds
BAR_COUNT    = 5          # show last 5 bars
SHOW_BB      = True       # show Bollinger Band values
SHOW_ENGULF  = True       # run engulfing check live
```

Output every 5 seconds:
```
  📊 LAST 5 CLOSED BARS
  Time                     Open       High        Low      Close   Vol  Dir
  2026-03-23 08:45:00   3010.500  3013.200  3009.100  3011.800  1423  🟢

  📉 BOLLINGER BANDS
  Upper  : 3024.50000
  Middle : 3012.30000
  Lower  : 3000.10000
  Close  : 3011.80000

  🕯️  ENGULFING CHECK (tolerance=10.0%)
  Current body        : 1.30000  (green)
  Previous body       : 0.80000
  Expanded body       : 1.56000
  Result              : bullish
  BB touch (lower)    : ❌ No — open not below lower BB
```

---

## 🔄 Event Flow

```
MT5 Terminal
    │
    ▼
DataFeed.poll_ticks() / poll_bars()
    │
    ├── TickEvent ──► Strategy.on_tick() ──► (if breakout) signal_long/short()
    │                 OrderManager.check_sl_tp()
    │
    └── BarEvent  ──► Strategy.on_bar()  ──► (if setup) set_signal()
                                             SignalEvent → queue
                                                 │
                                             RiskManager.process_signal()
                                                 │ checks max_trades, drawdown
                                             OrderEvent → queue
                                                 │
                                             OrderManager.execute_order()
                                                 │ paper fill
                                             FillEvent → queue
                                                 │
                                         Analytics.update() + Strategy.on_fill()
```

---

## 🛡️ Risk Guardrails Built In

| Guardrail | Where | Default |
|---|---|---|
| Max open trades | RiskManager | 3 |
| Max drawdown circuit breaker | RiskManager | 20% — pauses trading |
| Max lot size ceiling | Strategy | 10.0 lots |
| Min lot size floor | Strategy | 0.01 lots |
| Signal expiry | State machine | 5 candles |
| Fill retry limit | State machine | 3 attempts |
| Stuck-trade timeout | State machine | 3 bars |

---

## 📅 Timeframe Reference

| Constant | Raw int | Use |
|---|---|---|
| `mt5.TIMEFRAME_M1`  | 1     | Very fast signals, many trades |
| `mt5.TIMEFRAME_M5`  | 5     | Fast, good for intraday |
| `mt5.TIMEFRAME_M15` | 16390 | Default — balanced |
| `mt5.TIMEFRAME_M30` | 16392 | Fewer, higher quality signals |
| `mt5.TIMEFRAME_H1`  | 16385 | Swing trading |
| `mt5.TIMEFRAME_H4`  | 16388 | Position trading |

---

## ❓ Troubleshooting

| Problem | Fix |
|---|---|
| `IPC timeout` | Open MT5 terminal manually before running |
| `No bars returned` | Right-click Market Watch → Show All, add your symbol |
| `Terminal: Call failed` | Symbol not in Market Watch — enable it |
| Wrong symbol name | Run `connection_tester.py` — it prints available Gold symbols |
| CTRL+C not working | Click PowerShell window for focus, then CTRL+Break |
| No signals after 30min | Lower `engulf_tolerance_pct` to 0.0 temporarily for testing |
| Market closed errors | Forex/Gold opens Sunday 5PM New York time |

---

## 📝 License

MIT — use freely for personal and commercial projects.

---

## 🔁 Backtesting

Run the strategy on historical MT5 data:

```bash
python backtest_runner.py
```

Configure at the top of `backtest_runner.py`:
```python
SYMBOL       = "XAUUSD.GNE"
TIMEFRAME    = mt5.TIMEFRAME_M15
BARS_TO_LOAD = 3000              # bars of history
```

Output saved to `./results/backtest/`:
```
trades_backtest_*.csv
summary_backtest_*.csv
equity_backtest.png
summary_report_*.txt
```

---

## 🔬 Parameter Optimization

Grid search over parameter combinations to find the best settings:

```bash
python engine/optimizer.py
```

Configure the grid in `engine/optimizer.py`:
```python
PARAM_GRID = {
    "bb_period":            [15, 20, 25],
    "bb_std_dev":           [1.5, 2.0, 2.5],
    "engulf_tolerance_pct": [0.0, 10.0, 20.0],
    "tp_points":            [30.0, 40.0, 60.0],
    "sl_points":            [15.0, 20.0, 30.0],
}
RANK_BY = "profit_factor"   # metric to rank by
```

Output saved to `./results/optimization/`:
```
optimization_results.csv    ← all runs ranked
optimization_report.txt     ← readable summary
best_params.txt             ← copy-paste ready params block
```

---

## ✅ Unit Tests

Test the core strategy functions without needing MT5:

```bash
# With pytest
pip install pytest
python -m pytest tests/ -v

# Without pytest
python tests/test_strategy.py
```

Tests cover:
- `is_engulfing()` — 8 test cases including tolerance edge cases
- `compute_bb()` — column addition, NaN handling, band ordering
- `candle_touches_lower_bb()` / `candle_touches_upper_bb()` — all valid/invalid combos
- `BBEngulfingParams` — default and custom values

Expected output:
```
test_bullish_no_tolerance ... ok
test_bearish_no_tolerance ... ok
test_not_engulfing_smaller_body ... ok
...
✅ All 18 tests passed.
```

---

## 📱 MQL5 Expert Advisor (Native MT5)

The same strategy is available as a native MQL5 EA in `strategy/BB_Engulfing_Breakout.mq5`.

**Deploy it:**
1. Open MetaEditor: press `F4` in MT5
2. `File → New → Expert Advisor` → name `BB_Engulfing_Breakout`
3. Delete template, paste contents of `BB_Engulfing_Breakout.mq5`
4. Press `F7` to compile → should show `0 errors, 0 warnings`
5. Drag onto XAUUSD.GNE M15 chart
6. Enable "Allow live trading"

**Backtest in MT5:**
```
View → Strategy Tester
  EA        : BB_Engulfing_Breakout
  Symbol    : XAUUSD.GNE
  Timeframe : M15
  Mode      : Every tick
  ✅ Visual mode  ← watch trades being placed live
→ Start
```

All inputs (BB period, sizing mode, TP/SL mode etc.) are configurable
from the MT5 EA settings panel — no code changes needed.

---

## 🚀 Going Live (When Ready)

The framework supports real MT5 order execution via `LiveOrderManager`.

**Switch mode in `engine/trading_engine.py`:**

```python
# Paper trading (default — safe)
engine = TradingEngine(config, mode="paper")

# Live trading (real money — use with caution)
engine = TradingEngine(config, mode="live")
```

**Pre-live checklist:**

| Step | Action |
|---|---|
| ✅ | Run unit tests — all pass |
| ✅ | Run backtest — positive expectancy |
| ✅ | Run on demo account for 2+ weeks |
| ✅ | Verify TP/SL levels are correct in first 5 trades |
| ✅ | Set `max_open_trades = 1` for first week live |
| ✅ | Deploy on a VPS (not your home PC) |
| ✅ | Set `max_drawdown_pct` to a conservative value (e.g. 10%) |
