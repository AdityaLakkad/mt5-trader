"""
strategy/bb_engulfing_breakout.py
==================================
Bollinger Band Engulfing Breakout Strategy

Signal Logic
------------
LONG  : Bullish engulfing candle (body + tolerance > prev body) that
        opens below lower BB and closes above lower BB.
        Go LONG when ask crosses above signal candle's high.

SHORT : Bearish engulfing candle that opens above upper BB and closes
        below upper BB.
        Go SHORT when bid crosses below signal candle's low.

Exit  : Fixed TP and SL (points or %) from entry price at time of order.
        Managed by OrderManager after fill.

Expiry: If no breakout within N candles, signal is cancelled.

Position Sizing Modes
---------------------
1. Fixed lots           : always trade X lots
2. Fixed risk USD       : risk exactly $X per trade
3. Risk % of balance    : risk X% of current balance per trade
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from queue import Queue
from typing import Optional

import pandas as pd

from core.base_strategy import BaseStrategy
from core.events import BarEvent, TickEvent, FillEvent, FillStatus

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS
# =============================================================================

class SizingMode(Enum):
    FIXED_LOTS    = "fixed_lots"      # always trade X lots
    FIXED_USD     = "fixed_usd"       # risk exactly $X
    RISK_PCT      = "risk_pct"        # risk X% of balance


class TPSLMode(Enum):
    POINTS  = "points"                # TP/SL in price points
    PERCENT = "percent"               # TP/SL as % of entry price


# =============================================================================
# PARAMETERS
# =============================================================================

@dataclass
class BBEngulfingParams:
    # ── Timeframe ─────────────────────────────────────────────────────────────
    timeframe: int = 16390            # raw int: M15=16390 M5=5 H1=16385

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    bb_period:  int   = 20
    bb_std_dev: float = 2.0

    # ── Engulfing filter ──────────────────────────────────────────────────────
    # Expands current body by tolerance% on each side before comparing to prev
    engulf_tolerance_pct: float = 10.0

    # ── Signal expiry ─────────────────────────────────────────────────────────
    expiry_candles: int = 5           # cancel if no breakout within N bars

    # ── Max simultaneous trades (this symbol) ─────────────────────────────────
    max_trades_per_symbol: int = 1    # 1 = only one trade at a time per symbol

    # ── Position sizing ───────────────────────────────────────────────────────
    sizing_mode: SizingMode = SizingMode.FIXED_USD

    # SizingMode.FIXED_LOTS
    fixed_lot_size: float = 0.1

    # SizingMode.FIXED_USD
    risk_amount_usd: float = 100.0   # risk exactly this many dollars per trade

    # SizingMode.RISK_PCT
    risk_pct: float = 1.0            # risk this % of current balance per trade

    # Lot size limits (applied in all sizing modes)
    min_lot_size: float = 0.01
    max_lot_size: float = 10.0       # hard ceiling — never trade more than this

    # ── TP / SL ───────────────────────────────────────────────────────────────
    tpsl_mode: TPSLMode = TPSLMode.POINTS

    # TPSLMode.POINTS
    tp_points: float = 40.0
    sl_points: float = 20.0

    # TPSLMode.PERCENT
    tp_pct: float = 2.0              # 2% above entry
    sl_pct: float = 1.0              # 1% below entry

    # ── Fill retry ────────────────────────────────────────────────────────────
    max_fill_attempts: int = 3


# =============================================================================
# PURE FUNCTIONS (testable in isolation)
# =============================================================================

def is_engulfing(current: pd.Series, previous: pd.Series,
                 tolerance_pct: float) -> Optional[str]:
    """
    Returns 'bullish', 'bearish', or None.

    Expands current body by tolerance_pct% on each side:
        tolerance_amount = curr_body * (tolerance_pct / 100)
        expanded_body    = curr_body + (tolerance_amount * 2)

    If expanded_body > prev_body → engulfing confirmed.
    """
    curr_body = abs(current["close"] - current["open"])
    prev_body = abs(previous["close"] - previous["open"])

    if prev_body == 0:
        return None

    tolerance_amount = curr_body * (tolerance_pct / 100.0)
    expanded_body    = curr_body + (tolerance_amount * 2)

    if not expanded_body > prev_body:
        return None

    if current["close"] > current["open"]:
        return "bullish"
    if current["close"] < current["open"]:
        return "bearish"
    return None


def compute_bb(df: pd.DataFrame, period: int,
               std_dev: float) -> pd.DataFrame:
    """Appends upper_bb, lower_bb, middle_bb. Never mutates original."""
    close  = df["close"]
    middle = close.rolling(period).mean()
    std    = close.rolling(period).std()
    df     = df.copy()
    df["middle_bb"] = middle
    df["upper_bb"]  = middle + std_dev * std
    df["lower_bb"]  = middle - std_dev * std
    return df


def candle_touches_lower_bb(candle: pd.Series) -> bool:
    """Long setup: opens below lower BB, closes above it."""
    if candle["low"]  < candle["lower_bb"] and candle["high"] > candle["upper_bb"]:
        return False
    return (
        candle["open"] < candle["close"] and 
        candle["low"]  < candle["lower_bb"] and candle["high"] > candle["lower_bb"]
    )


def candle_touches_upper_bb(candle: pd.Series) -> bool:
    """Short setup: opens above upper BB, closes below it."""
    if candle["low"]  < candle["lower_bb"] and candle["high"] > candle["upper_bb"]:
        return False
    return (
        candle["open"] > candle["close"] and 
        candle["open"]  > candle["upper_bb"] and candle["close"] < candle["upper_bb"]
    )


# =============================================================================
# STATE MACHINE
# =============================================================================

class StrategyState(Enum):
    IDLE     = auto()
    WAITING  = auto()
    IN_TRADE = auto()


@dataclass
class PendingSignal:
    direction: str
    breakout_high: float
    breakout_low: float
    signal_candle_time: datetime
    candles_elapsed: int = 0


class SignalStateManager:
    """One instance per symbol. Owns all state transitions."""

    def __init__(self, expiry_candles: int, max_fill_attempts: int = 3):
        self.expiry_candles   = expiry_candles
        self._state: StrategyState          = StrategyState.IDLE
        self._pending: Optional[PendingSignal] = None
        self._fill_attempts   = 0
        self._max_fill_attempts = max_fill_attempts
        self._bars_in_trade   = 0

    # ── Read ──────────────────────────────────────────────────────────────────
    @property
    def state(self):       return self._state
    @property
    def pending(self):     return self._pending
    def is_idle(self):     return self._state == StrategyState.IDLE
    def is_waiting(self):  return self._state == StrategyState.WAITING
    def is_in_trade(self): return self._state == StrategyState.IN_TRADE

    # ── Transitions ───────────────────────────────────────────────────────────
    def set_signal(self, direction: str, high: float, low: float,
                   candle_time: datetime) -> None:
        """IDLE→WAITING or replace existing signal."""
        self._pending = PendingSignal(
            direction=direction,
            breakout_high=high,
            breakout_low=low,
            signal_candle_time=candle_time,
        )
        self._state         = StrategyState.WAITING
        self._fill_attempts = 0

    def on_new_bar(self) -> bool:
        """
        Increment expiry and bars_in_trade counters.
        Returns True if signal expired.
        """
        if self.is_waiting():
            self._pending.candles_elapsed += 1
            if self._pending.candles_elapsed >= self.expiry_candles:
                self._reset()
                return True

        if self.is_in_trade():
            self._bars_in_trade += 1
            # Safety: if stuck IN_TRADE for 3 bars with no fill confirmation
            if self._bars_in_trade >= 3:
                logger.warning(
                    "[SSM] Stuck IN_TRADE for 3 bars — force reset to IDLE"
                )
                self._reset()

        return False

    def enter_trade(self) -> None:
        """WAITING → IN_TRADE."""
        self._state         = StrategyState.IN_TRADE
        self._pending       = None
        self._bars_in_trade = 0

    def exit_trade(self) -> None:
        """IN_TRADE → IDLE."""
        self._reset()

    def on_fill_rejected(self) -> bool:
        """
        Called on a rejected fill while IN_TRADE.
        Returns True if should retry (go back to WAITING).
        Returns False if max attempts exceeded (go to IDLE).
        """
        self._fill_attempts += 1
        if self._fill_attempts >= self._max_fill_attempts:
            logger.warning(
                f"[SSM] Max fill attempts ({self._max_fill_attempts}) → IDLE"
            )
            self._reset()
            return False
        # Retry — go back to WAITING
        self._state = StrategyState.WAITING
        logger.warning(
            f"[SSM] Fill rejected — retry "
            f"{self._fill_attempts}/{self._max_fill_attempts} → WAITING"
        )
        return True

    def _reset(self) -> None:
        self._state         = StrategyState.IDLE
        self._pending       = None
        self._fill_attempts = 0
        self._bars_in_trade = 0

    def __repr__(self) -> str:
        if self._pending:
            return (
                f"<SSM state={self._state.name} "
                f"dir={self._pending.direction} "
                f"elapsed={self._pending.candles_elapsed}/{self.expiry_candles}>"
            )
        return f"<SSM state={self._state.name}>"


# =============================================================================
# STRATEGY CLASS
# =============================================================================

class BBEngulfingBreakoutStrategy(BaseStrategy):
    """
    Bollinger Band Engulfing Breakout Strategy.

    Parameters
    ----------
    symbols         : list of MT5 symbol strings
    event_queue     : shared engine queue
    params          : BBEngulfingParams
    initial_balance : fallback balance if MT5 account info unavailable
    """

    def __init__(self, symbols: list, event_queue: Queue,
                 params: Optional[BBEngulfingParams] = None,
                 initial_balance: float = 10_000.0):
        super().__init__(
            strategy_id="BB_Engulfing_Breakout",
            symbols=symbols,
            event_queue=event_queue,
        )
        self.params           = params or BBEngulfingParams()
        self._fallback_balance = initial_balance
        self._sym_info_cache:  dict = {}

        # One independent state machine per symbol
        self._state_managers: dict = {
            symbol: SignalStateManager(
                expiry_candles=self.params.expiry_candles,
                max_fill_attempts=self.params.max_fill_attempts,
            )
            for symbol in symbols
        }

    # =========================================================================
    # on_bar — signal detection
    # =========================================================================

    def on_bar(self, event: BarEvent) -> None:
        if event.symbol not in self.symbols:
            return

        df = event.bars_df
        if df is None or len(df) < self.params.bb_period + 1:
            return

        sm = self._state_managers[event.symbol]

        # ── Tick expiry / stuck-trade counter ─────────────────────────────────
        expired = sm.on_new_bar()
        if expired:
            logger.info(
                f"[{self.strategy_id}] {event.symbol} signal EXPIRED "
                f"after {self.params.expiry_candles} candles → IDLE"
            )

        # ── Compute Bollinger Bands ────────────────────────────────────────────
        df = compute_bb(df, self.params.bb_period, self.params.bb_std_dev)

        current  = df.iloc[-1]
        previous = df.iloc[-2]

        # ── Guards ────────────────────────────────────────────────────────────
        if math.isnan(current["lower_bb"]) or math.isnan(current["upper_bb"]):
            return

        if sm.is_in_trade():
            return

        # ── Engulfing check ───────────────────────────────────────────────────
        engulf = is_engulfing(current, previous, self.params.engulf_tolerance_pct)
        if engulf is None:
            return

        # ── BB touch + direction ──────────────────────────────────────────────
        if engulf == "bullish" and candle_touches_lower_bb(current):
            sm.set_signal(
                direction="long",
                high=float(current["high"]),
                low=float(current["low"]),
                candle_time=event.timestamp,
            )
            logger.info(
                f"[{self.strategy_id}] 🟢 LONG SETUP {event.symbol} | "
                f"H={current['high']:.5f} L={current['low']:.5f} | "
                f"lower_bb={current['lower_bb']:.5f} | {sm}"
            )

        elif engulf == "bearish" and candle_touches_upper_bb(current):
            sm.set_signal(
                direction="short",
                high=float(current["high"]),
                low=float(current["low"]),
                candle_time=event.timestamp,
            )
            logger.info(
                f"[{self.strategy_id}] 🔴 SHORT SETUP {event.symbol} | "
                f"H={current['high']:.5f} L={current['low']:.5f} | "
                f"upper_bb={current['upper_bb']:.5f} | {sm}"
            )

    # =========================================================================
    # on_tick — breakout execution
    # =========================================================================

    def on_tick(self, event: TickEvent) -> None:
        if event.symbol not in self.symbols:
            return

        sm = self._state_managers[event.symbol]
        if not sm.is_waiting():
            return

        pending = sm.pending

        # ── Long breakout: ask crosses above signal candle high ────────────────
        if pending.direction == "long" and event.ask >= pending.breakout_high:
            entry_price = event.ask
            tp_price, sl_price = self._calc_tp_sl("long", entry_price, event.symbol)
            lot_size = self._calculate_lot_size(event.symbol)

            logger.info(
                f"[{self.strategy_id}] ✅ LONG BREAKOUT {event.symbol} | "
                f"ask={event.ask:.5f} >= level={pending.breakout_high:.5f} | "
                f"entry={entry_price:.5f} TP={tp_price:.5f} SL={sl_price:.5f} "
                f"lots={lot_size:.2f}"
            )

            sm.enter_trade()

            self.signal_long(
                event.symbol,
                sl=sl_price,
                tp=tp_price,
                metadata={
                    "fixed_volume":  lot_size,
                    "entry":         entry_price,
                    "breakout_high": pending.breakout_high,
                    "breakout_low":  pending.breakout_low,
                }
            )

        # ── Short breakout: bid crosses below signal candle low ────────────────
        elif pending.direction == "short" and event.bid <= pending.breakout_low:
            entry_price = event.bid
            tp_price, sl_price = self._calc_tp_sl("short", entry_price, event.symbol)
            lot_size = self._calculate_lot_size(event.symbol)

            logger.info(
                f"[{self.strategy_id}] ✅ SHORT BREAKOUT {event.symbol} | "
                f"bid={event.bid:.5f} <= level={pending.breakout_low:.5f} | "
                f"entry={entry_price:.5f} TP={tp_price:.5f} SL={sl_price:.5f} "
                f"lots={lot_size:.2f}"
            )

            sm.enter_trade()

            self.signal_short(
                event.symbol,
                sl=sl_price,
                tp=tp_price,
                metadata={
                    "fixed_volume":  lot_size,
                    "entry":         entry_price,
                    "breakout_high": pending.breakout_high,
                    "breakout_low":  pending.breakout_low,
                }
            )

    # =========================================================================
    # on_fill — close the loop
    # =========================================================================

    def on_fill(self, event: FillEvent) -> None:
        if event.symbol not in self._state_managers:
            return

        sm = self._state_managers[event.symbol]

        # Trade closed (TP or SL hit)
        if event.pnl is not None:
            sm.exit_trade()
            logger.info(
                f"[{self.strategy_id}] 📋 CLOSED {event.symbol} | "
                f"pnl={event.pnl:+.2f} → IDLE"
            )

        # Fill rejected — retry or give up
        elif event.status == FillStatus.REJECTED:
            will_retry = sm.on_fill_rejected()
            logger.warning(
                f"[{self.strategy_id}] ❌ FILL REJECTED {event.symbol} | "
                f"{'will retry' if will_retry else 'giving up → IDLE'}"
            )

        # Fill confirmed — position opened
        elif event.status == FillStatus.FILLED and event.pnl is None:
            logger.info(
                f"[{self.strategy_id}] 🟩 OPENED {event.symbol} | "
                f"fill={event.fill_price:.5f} sl={event.sl} tp={event.tp}"
            )

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def on_start(self) -> None:
        logger.info(
            f"[{self.strategy_id}] Started\n"
            f"  Symbols     : {self.symbols}\n"
            f"  Timeframe   : {self.params.timeframe}\n"
            f"  BB          : period={self.params.bb_period} std={self.params.bb_std_dev}\n"
            f"  Tolerance   : {self.params.engulf_tolerance_pct}%\n"
            f"  Expiry      : {self.params.expiry_candles} candles\n"
            f"  Max trades  : {self.params.max_trades_per_symbol} per symbol\n"
            f"  Sizing mode : {self.params.sizing_mode.value}\n"
            f"  TP/SL mode  : {self.params.tpsl_mode.value}\n"
            + self._sizing_summary()
            + self._tpsl_summary()
        )

    def get_state_summary(self) -> dict:
        return {sym: str(sm) for sym, sm in self._state_managers.items()}

    # =========================================================================
    # Private helpers
    # =========================================================================

    def _calc_tp_sl(self, direction: str, entry: float,
                    symbol: str) -> tuple:
        """Calculate TP and SL prices from entry price."""
        sym_info = self._get_symbol_info(symbol)
        digits   = sym_info["digits"]

        if self.params.tpsl_mode == TPSLMode.POINTS:
            point = sym_info["point"]
            if direction == "long":
                tp = round(entry + self.params.tp_points * point, digits)
                sl = round(entry - self.params.sl_points * point, digits)
            else:
                tp = round(entry - self.params.tp_points * point, digits)
                sl = round(entry + self.params.sl_points * point, digits)

        else:  # PERCENT
            if direction == "long":
                tp = round(entry * (1 + self.params.tp_pct / 100), digits)
                sl = round(entry * (1 - self.params.sl_pct / 100), digits)
            else:
                tp = round(entry * (1 - self.params.tp_pct / 100), digits)
                sl = round(entry * (1 + self.params.sl_pct / 100), digits)

        return tp, sl

    def _calculate_lot_size(self, symbol: str) -> float:
        """
        Calculate lot size based on sizing_mode.
        Always clamped to min/max lot size and broker limits.
        """
        sym_info      = self._get_symbol_info(symbol)
        contract_size = sym_info["contract_size"]
        volume_step   = sym_info["volume_step"]
        volume_min    = sym_info["volume_min"]
        volume_max    = sym_info["volume_max"]

        mode = self.params.sizing_mode

        # ── Fixed lots ────────────────────────────────────────────────────────
        if mode == SizingMode.FIXED_LOTS:
            raw = self.params.fixed_lot_size

        # ── Fixed USD risk ────────────────────────────────────────────────────
        elif mode == SizingMode.FIXED_USD:
            point        = sym_info["point"]
            sl_val_lot   = self.params.sl_points * point * contract_size
            if sl_val_lot <= 0:
                return self.params.min_lot_size
            raw = self.params.risk_amount_usd / sl_val_lot

        # ── Risk % of balance ─────────────────────────────────────────────────
        elif mode == SizingMode.RISK_PCT:
            balance      = self._get_current_balance()
            risk_amount  = balance * (self.params.risk_pct / 100.0)
            point        = sym_info["point"]
            sl_val_lot   = self.params.sl_points * point * contract_size
            if sl_val_lot <= 0:
                return self.params.min_lot_size
            raw = risk_amount / sl_val_lot

        else:
            raw = self.params.fixed_lot_size

        # ── Snap to volume step ───────────────────────────────────────────────
        snapped = round(round(raw / volume_step) * volume_step, 8)

        # ── Clamp to strategy limits ──────────────────────────────────────────
        clamped = max(self.params.min_lot_size,
                      min(self.params.max_lot_size, snapped))

        # ── Clamp to broker limits ────────────────────────────────────────────
        final = max(volume_min, min(volume_max, clamped))

        logger.info(
            f"[{self.strategy_id}] LOT SIZE {symbol} | "
            f"mode={mode.value} raw={raw:.4f} → final={final:.2f}"
        )
        return final

    def _get_current_balance(self) -> float:
        try:
            import MetaTrader5 as mt5
            info = mt5.account_info()
            if info:
                return info.balance
        except Exception:
            pass
        return self._fallback_balance

    def _get_symbol_info(self, symbol: str) -> dict:
        if symbol not in self._sym_info_cache:
            try:
                import MetaTrader5 as mt5
                info = mt5.symbol_info(symbol)
                if info:
                    self._sym_info_cache[symbol] = {
                        "point":         info.point,
                        "digits":        info.digits,
                        "contract_size": info.trade_contract_size,
                        "volume_min":    info.volume_min,
                        "volume_max":    info.volume_max,
                        "volume_step":   info.volume_step,
                    }
                    return self._sym_info_cache[symbol]
            except Exception:
                pass
            # Gold fallback defaults
            self._sym_info_cache[symbol] = {
                "point":         0.01,
                "digits":        2,
                "contract_size": 100.0,
                "volume_min":    0.01,
                "volume_max":    50.0,
                "volume_step":   0.01,
            }
        return self._sym_info_cache[symbol]

    def _sizing_summary(self) -> str:
        mode = self.params.sizing_mode
        if mode == SizingMode.FIXED_LOTS:
            return f"  Lots        : {self.params.fixed_lot_size} (fixed)\n"
        elif mode == SizingMode.FIXED_USD:
            return (
                f"  Risk amount : ${self.params.risk_amount_usd} per trade\n"
                f"  Lot limits  : {self.params.min_lot_size} – {self.params.max_lot_size}\n"
            )
        else:
            return (
                f"  Risk %      : {self.params.risk_pct}% of balance\n"
                f"  Lot limits  : {self.params.min_lot_size} – {self.params.max_lot_size}\n"
            )

    def _tpsl_summary(self) -> str:
        mode = self.params.tpsl_mode
        if mode == TPSLMode.POINTS:
            return f"  TP/SL       : {self.params.tp_points}pt / {self.params.sl_points}pt\n"
        else:
            return f"  TP/SL       : {self.params.tp_pct}% / {self.params.sl_pct}%\n"
