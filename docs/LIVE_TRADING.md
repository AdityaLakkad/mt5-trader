# LIVE_TRADING.md
## Live Trading Setup and Deployment

---

## Overview

Live trading uses `LiveOrderManager` instead of `OrderManager`.
The strategy, risk manager, analytics, and data feed are identical.
Switching is one line in `bb_engulfing_main.py`:

```python
engine = TradingEngine(config, mode="paper")   # safe default
engine = TradingEngine(config, mode="live")    # real orders to MT5
```

---

## Pre-Live Checklist

Run through every item before switching to live mode.

| Step | Check | Notes |
|---|---|---|
| 1 | Unit tests pass | `python tests/test_strategy.py` |
| 2 | Backtest positive expectancy | `python backtest_runner.py` |
| 3 | Paper traded 2+ weeks | Strategy logic validated |
| 4 | First 10 live trades on DEMO | Verify TP/SL levels correct |
| 5 | `max_open_trades = 1` initially | Limit exposure while validating |
| 6 | `max_drawdown_pct` conservative | e.g. 10% to pause if something wrong |
| 7 | VPS deployed | Not home PC (internet drop = open unmanaged position) |
| 8 | MT5 Algo Trading enabled | Green button in toolbar |
| 9 | Confirmed symbol name | XAUUSD.GNE not XAUUSD etc |

---

## LiveOrderManager Architecture

### Order Execution Flow

```
on_tick() → breakout detected → signal_long() → SignalEvent → queue
                                                     ↓
                                              RiskManager (checks limits)
                                                     ↓
                                              OrderEvent → queue
                                                     ↓
                                         LiveOrderManager.execute_order()
                                                     ↓
                                         mt5.order_send(request)
                                                     ↓
                                         result.retcode == TRADE_RETCODE_DONE?
                                           ├── YES: add to _known_tickets
                                           │        add to open_positions
                                           │        FillEvent (status=FILLED, pnl=None)
                                           └── NO:  FillEvent (status=REJECTED)
                                                    on_fill_rejected() → retry or IDLE
```

### Close Detection Flow

```
Every 2 seconds (throttled):
    mt5.positions_get() → current_tickets (set)
    _known_tickets - current_tickets = closed_tickets

For each closed ticket:
    mt5.history_deals_get(position=ticket) → deals list
    Calculate PnL = sum(profit + commission + swap for all deals)
    Create ClosedTrade object
    Emit FillEvent (pnl=set) → on_fill() → state machine → IDLE
```

### Key Attributes

```python
_known_tickets:         Set[int]       # MT5 ticket numbers we're tracking
_ticket_to_strategy:    Dict[int, str] # ticket → strategy_id
open_positions:         Dict[str, Position]  # ticket_str → Position
closed_trades:          List[ClosedTrade]    # all closed trades
```

---

## MT5 Order Request

```python
request = {
    "action":       mt5.TRADE_ACTION_DEAL,
    "symbol":       order.symbol,
    "volume":       float(order.volume),
    "type":         mt5.ORDER_TYPE_BUY,   # or SELL
    "price":        tick.ask,             # or bid for short
    "sl":           float(order.sl),
    "tp":           float(order.tp),
    "deviation":    10,                   # max slippage in points
    "magic":        234001,               # unique EA identifier
    "comment":      "BB_Engulf"[:31],     # MT5 max 31 chars
    "type_time":    mt5.ORDER_TIME_GTC,
    "type_filling": mt5.ORDER_FILLING_IOC, # most compatible across brokers
}
```

**Important:** `type_filling = ORDER_FILLING_IOC` is more compatible
than FOK across brokers. Some brokers reject FOK during high spread.

---

## FLAT (Close All) Request

```python
request = {
    "action":   mt5.TRADE_ACTION_DEAL,
    "symbol":   symbol,
    "volume":   pos.volume,
    "type":     mt5.ORDER_TYPE_SELL,    # opposite of position
    "price":    tick.bid,               # closing long = sell at bid
    "position": ticket_int,             # reference the specific position
    "deviation": 10,
    "magic":    234001,
    "comment":  "FLAT",
    ...
}
```

---

## Trade Allowed: False

When `terminal_info().trade_allowed == False`:

1. **Click Algo Trading button** in MT5 toolbar (must be GREEN)
2. `Tools → Options → Expert Advisors → Allow automated trading ✅`
3. Some brokers disable algo trading at account level — contact broker

Code guard in `trading_engine.py`:
```python
terminal = mt5.terminal_info()
if terminal and not terminal.trade_allowed:
    logger.error("[Engine] Trade not allowed — enable Algo Trading in MT5")
    return
```

---

## Magic Number

`magic_number=234001` by default.

This number identifies all orders placed by this EA in MT5.
Position queries filter by magic number:
```python
{p.ticket for p in positions if p.magic == self.magic}
```

Change magic number if running multiple EAs on the same account to
avoid cross-contamination. Use a different number for each strategy.

---

## Monitoring in Live Mode

The dashboard prints a portfolio summary line every 60 seconds:
```
INFO  [Portfolio] Balance=$10,200.00 Equity=$10,312.00 OpenPnL=+112.00
      TotalPnL=+200.00 DD=0.0% Open=1 Closed=3 WR=67% |
      XAUUSD.GNE BUY 5.00lots @ 3015.55 SL=3015.35
```

Strategy state logs on every bar close:
```
INFO  [BB_Engulfing_Breakout] BAR XAUUSD.GNE 🟢 | ... state=IN_TRADE
INFO  [BB_Engulfing_Breakout] 📋 CLOSED XAUUSD.GNE | pnl=+200.00 → IDLE
```

---

## VPS Requirements

- Windows Server (MT5 Python API is Windows-only)
- MT5 terminal installed and logged in
- Python 3.11+ with virtual environment
- `requirements.txt` installed
- Network connection stable (IPC failure = missed fills)
- Auto-start script recommended so EA restarts on reboot

---

## Difference from Paper Mode

| | Paper (`OrderManager`) | Live (`LiveOrderManager`) |
|---|---|---|
| Fills | Simulated at bid/ask + slippage | Real via `mt5.order_send()` |
| SL/TP | Python checks every tick | MT5 server-side + Python polls every 2s |
| Balance | Internal counter | `mt5.account_info().balance` |
| Position tracking | Internal dict | Internal dict synced to MT5 |
| Closed trade detection | Internal (immediate) | Poll MT5 every 2s |
| Commission | Estimated ($7/lot) | Real from deal history |
