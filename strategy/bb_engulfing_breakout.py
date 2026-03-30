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

Expiry: If no breakout within N candles, signal is cancelled.

Position Sizing Modes
---------------------
1. FIXED_LOTS  : always trade X lots
2. FIXED_USD   : risk exactly $X per trade (uses fixed SL points for sizing)
3. RISK_PCT    : risk X% of balance per trade (uses fixed SL points for sizing)

TP/SL Modes
-----------
1. POINTS  : fixed N points above/below entry
2. PERCENT : fixed % above/below entry
3. CANDLE  : SL = candle low (long) or candle high (short)
             TP = entry ± (SL_distance × rr_ratio)
             Lot size dynamically calculated: risk_amount ÷ (SL_distance × contract_size)
             This means wide candles get smaller lots, tight candles get bigger lots.

Example — CANDLE mode with FIXED_USD:
    Signal candle high = 3015.50, low = 3011.20
    Entry (ask)        = 3015.55
    SL distance        = 3015.55 - 3011.20 = 4.35 points
    SL price           = 3011.20 (candle low)
    TP price           = 3015.55 + (4.35 × 3.0) = 3028.60  (RR = 3)
    SL value per lot   = 4.35 × 100 (contract) = $435
    Lot size           = $100 risk ÷ $435 = 0.23 lots → rounded to 0.23
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
    FIXED_LOTS = "fixed_lots"
    FIXED_USD  = "fixed_usd"
    RISK_PCT   = "risk_pct"


class TPSLMode(Enum):
    POINTS  = "points"
    PERCENT = "percent"
    CANDLE  = "candle"


# =============================================================================
# PARAMETERS
# =============================================================================

@dataclass
class BBEngulfingParams:
    # ── Timeframe ─────────────────────────────────────────────────────────────
    timeframe: int = 16390          # M1=1 M5=5 M15=16390 M30=16392 H1=16385

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    bb_period:  int   = 20
    bb_std_dev: float = 2.0

    # ── Engulfing filter ──────────────────────────────────────────────────────
    engulf_tolerance_pct: float = 10.0

    # ── Signal expiry ─────────────────────────────────────────────────────────
    expiry_candles: int = 5

    # ── Max simultaneous trades ───────────────────────────────────────────────
    max_trades_per_symbol: int = 1

    # ── Position sizing ───────────────────────────────────────────────────────
    sizing_mode:     SizingMode = SizingMode.FIXED_USD
    fixed_lot_size:  float = 0.1
    risk_amount_usd: float = 100.0
    risk_pct:        float = 1.0
    min_lot_size:    float = 0.01
    max_lot_size:    float = 10.0

    # ── TP / SL ───────────────────────────────────────────────────────────────
    tpsl_mode: TPSLMode = TPSLMode.POINTS
    tp_points: float = 40.0
    sl_points: float = 20.0
    tp_pct:    float = 2.0
    sl_pct:    float = 1.0
    rr_ratio:  float = 3.0      # CANDLE mode only: TP = SL_distance × rr_ratio

    # ── Fill retry ────────────────────────────────────────────────────────────
    max_fill_attempts: int = 3


# =============================================================================
# PURE FUNCTIONS
# =============================================================================

def is_engulfing(current: pd.Series, previous: pd.Series,
                 tolerance_pct: float) -> Optional[str]:
    """
    Returns 'bullish', 'bearish', or None.
    expanded_body = curr_body + (curr_body × tolerance% × 2)
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


def compute_bb(df: pd.DataFrame, period: int, std_dev: float) -> pd.DataFrame:
    """Appends upper_bb, lower_bb, middle_bb. Never mutates original."""
    close           = df["close"]
    middle          = close.rolling(period).mean()
    std             = close.rolling(period).std()
    df              = df.copy()
    df["middle_bb"] = middle
    df["upper_bb"]  = middle + std_dev * std
    df["lower_bb"]  = middle - std_dev * std
    return df


def candle_touches_lower_bb(candle: pd.Series) -> bool:
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

    def __init__(self, expiry_candles: int, max_fill_attempts: int = 3):
        self.expiry_candles     = expiry_candles
        self._state: StrategyState             = StrategyState.IDLE
        self._pending: Optional[PendingSignal] = None
        self._fill_attempts                    = 0
        self._max_fill_attempts                = max_fill_attempts
        self._bars_in_trade                    = 0

    @property
    def state(self):       return self._state
    @property
    def pending(self):     return self._pending
    def is_idle(self):     return self._state == StrategyState.IDLE
    def is_waiting(self):  return self._state == StrategyState.WAITING
    def is_in_trade(self): return self._state == StrategyState.IN_TRADE

    def set_signal(self, direction: str, high: float, low: float,
                   candle_time: datetime) -> None:
        self._pending = PendingSignal(
            direction=direction, breakout_high=high,
            breakout_low=low, signal_candle_time=candle_time,
        )
        self._state         = StrategyState.WAITING
        self._fill_attempts = 0

    def on_new_bar(self) -> bool:
        if self.is_waiting():
            self._pending.candles_elapsed += 1
            if self._pending.candles_elapsed >= self.expiry_candles:
                self._reset()
                return True
        if self.is_in_trade():
            self._bars_in_trade += 1
            if self._bars_in_trade >= 3:
                logger.warning("[SSM] Stuck IN_TRADE 3 bars — force reset → IDLE")
                self._reset()
        return False

    def enter_trade(self) -> None:
        self._state         = StrategyState.IN_TRADE
        self._pending       = None
        self._bars_in_trade = 0

    def exit_trade(self) -> None:
        self._reset()

    def on_fill_rejected(self) -> bool:
        self._fill_attempts += 1
        if self._fill_attempts >= self._max_fill_attempts:
            logger.warning(f"[SSM] Max fill attempts ({self._max_fill_attempts}) → IDLE")
            self._reset()
            return False
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

    def __init__(self, symbols: list, event_queue: Queue,
                 params: Optional[BBEngulfingParams] = None,
                 initial_balance: float = 10_000.0):
        super().__init__(
            strategy_id="BB_Engulfing_Breakout",
            symbols=symbols,
            event_queue=event_queue,
        )
        self.params            = params or BBEngulfingParams()
        self._fallback_balance = initial_balance
        self._sym_info_cache:  dict = {}
        self._state_managers: dict = {
            symbol: SignalStateManager(
                expiry_candles=self.params.expiry_candles,
                max_fill_attempts=self.params.max_fill_attempts,
            )
            for symbol in symbols
        }

    # =========================================================================
    # on_bar
    # =========================================================================

    def on_bar(self, event: BarEvent) -> None:
        if event.symbol not in self.symbols:
            return

        df = event.bars_df
        if df is None or len(df) < self.params.bb_period + 1:
            return

        sm = self._state_managers[event.symbol]

        expired = sm.on_new_bar()
        if expired:
            logger.info(
                f"[{self.strategy_id}] {event.symbol} signal EXPIRED "
                f"after {self.params.expiry_candles} candles → IDLE"
            )

        df       = compute_bb(df, self.params.bb_period, self.params.bb_std_dev)
        current  = df.iloc[-1]
        previous = df.iloc[-2]

        if math.isnan(current["lower_bb"]) or math.isnan(current["upper_bb"]):
            return

        if sm.is_in_trade():
            return

        # ── Engulfing check ───────────────────────────────────────────────────
        engulf    = is_engulfing(current, previous, self.params.engulf_tolerance_pct)
        curr_body = abs(current["close"] - current["open"])
        prev_body = abs(previous["close"] - previous["open"])
        tol_amt   = curr_body * (self.params.engulf_tolerance_pct / 100.0)
        expanded  = curr_body + tol_amt * 2

        # ── Always log bar close ──────────────────────────────────────────────
        d_char = "🟢" if current["close"] >= current["open"] else "🔴"
        logger.info(
            f"[{self.strategy_id}] BAR {event.symbol} {d_char} | "
            f"O={current['open']:.5f} H={current['high']:.5f} "
            f"L={current['low']:.5f} C={current['close']:.5f} | "
            f"body={curr_body:.5f} expanded={expanded:.5f} prev={prev_body:.5f} "
            f"engulf={'✅ '+engulf if engulf else '❌ no'} | "
            f"BB upper={current['upper_bb']:.5f} lower={current['lower_bb']:.5f} | "
            f"state={sm.state.name}"
            + (
                f" waiting_for="
                f"{'LONG >'+str(round(sm.pending.breakout_high,5)) if sm.pending and sm.pending.direction=='long' else 'SHORT <'+str(round(sm.pending.breakout_low,5)) if sm.pending else ''}"
                f" bars_left={sm.expiry_candles - sm.pending.candles_elapsed if sm.pending else '-'}"
                if sm.is_waiting() else ""
            )
        )

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
                f"breakout_high={current['high']:.5f} "
                f"breakout_low={current['low']:.5f} | "
                f"lower_bb={current['lower_bb']:.5f} | "
                f"watching ask >= {current['high']:.5f} | "
                f"expires in {self.params.expiry_candles} bars"
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
                f"breakout_high={current['high']:.5f} "
                f"breakout_low={current['low']:.5f} | "
                f"upper_bb={current['upper_bb']:.5f} | "
                f"watching bid <= {current['low']:.5f} | "
                f"expires in {self.params.expiry_candles} bars"
            )

    # =========================================================================
    # on_tick
    # =========================================================================

    def on_tick(self, event: TickEvent) -> None:
        if event.symbol not in self.symbols:
            return

        sm = self._state_managers[event.symbol]
        if not sm.is_waiting():
            return

        pending = sm.pending

        # ── Long breakout ─────────────────────────────────────────────────────
        if pending.direction == "long" and event.ask >= pending.breakout_high:
            entry_price = event.ask
            tp_price, sl_price = self._calc_tp_sl(
                direction="long", entry=entry_price, symbol=event.symbol,
                candle_high=pending.breakout_high, candle_low=pending.breakout_low,
            )
            lot_size = self._calculate_lot_size(
                symbol=event.symbol, entry_price=entry_price,
                candle_high=pending.breakout_high, candle_low=pending.breakout_low,
            )
            logger.info(
                f"[{self.strategy_id}] ✅ LONG BREAKOUT {event.symbol} | "
                f"ask={event.ask:.5f} >= level={pending.breakout_high:.5f} | "
                f"entry={entry_price:.5f} TP={tp_price:.5f} SL={sl_price:.5f} "
                f"lots={lot_size:.2f}"
            )
            sm.enter_trade()
            self.signal_long(
                event.symbol, sl=sl_price, tp=tp_price,
                metadata={
                    "fixed_volume":  lot_size,
                    "entry":         entry_price,
                    "breakout_high": pending.breakout_high,
                    "breakout_low":  pending.breakout_low,
                }
            )

        # ── Short breakout ────────────────────────────────────────────────────
        elif pending.direction == "short" and event.bid <= pending.breakout_low:
            entry_price = event.bid
            tp_price, sl_price = self._calc_tp_sl(
                direction="short", entry=entry_price, symbol=event.symbol,
                candle_high=pending.breakout_high, candle_low=pending.breakout_low,
            )
            lot_size = self._calculate_lot_size(
                symbol=event.symbol, entry_price=entry_price,
                candle_high=pending.breakout_high, candle_low=pending.breakout_low,
            )
            logger.info(
                f"[{self.strategy_id}] ✅ SHORT BREAKOUT {event.symbol} | "
                f"bid={event.bid:.5f} <= level={pending.breakout_low:.5f} | "
                f"entry={entry_price:.5f} TP={tp_price:.5f} SL={sl_price:.5f} "
                f"lots={lot_size:.2f}"
            )
            sm.enter_trade()
            self.signal_short(
                event.symbol, sl=sl_price, tp=tp_price,
                metadata={
                    "fixed_volume":  lot_size,
                    "entry":         entry_price,
                    "breakout_high": pending.breakout_high,
                    "breakout_low":  pending.breakout_low,
                }
            )

    # =========================================================================
    # on_fill
    # =========================================================================

    def on_fill(self, event: FillEvent) -> None:
        if event.symbol not in self._state_managers:
            return
        sm = self._state_managers[event.symbol]

        if event.pnl is not None:
            sm.exit_trade()
            logger.info(
                f"[{self.strategy_id}] 📋 CLOSED {event.symbol} | "
                f"pnl={event.pnl:+.2f} → IDLE"
            )
        elif event.status == FillStatus.REJECTED:
            will_retry = sm.on_fill_rejected()
            logger.warning(
                f"[{self.strategy_id}] ❌ FILL REJECTED {event.symbol} | "
                f"{'will retry' if will_retry else 'giving up → IDLE'}"
            )
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

    def _calc_tp_sl(
        self,
        direction: str,
        entry: float,
        symbol: str,
        candle_high: float = None,
        candle_low:  float = None,
    ) -> tuple:
        """
        Returns (tp_price, sl_price).

        CANDLE mode:
            Long:  SL = candle_low,  TP = entry + (SL_dist × rr_ratio)
            Short: SL = candle_high, TP = entry - (SL_dist × rr_ratio)

        POINTS mode:
            SL/TP = fixed N points from entry

        PERCENT mode:
            SL/TP = fixed % from entry
        """
        sym_info = self._get_symbol_info(symbol)
        digits   = sym_info["digits"]

        if self.params.tpsl_mode == TPSLMode.CANDLE:
            if candle_high is None or candle_low is None:
                logger.warning(
                    f"[{self.strategy_id}] CANDLE mode missing candle levels — "
                    f"falling back to POINTS"
                )
                # Fallback to points
                point = sym_info["point"]
                if direction == "long":
                    return (
                        round(entry + self.params.tp_points * point, digits),
                        round(entry - self.params.sl_points * point, digits),
                    )
                else:
                    return (
                        round(entry - self.params.tp_points * point, digits),
                        round(entry + self.params.sl_points * point, digits),
                    )

            if direction == "long":
                sl          = round(candle_low, digits)
                sl_distance = max(entry - sl, 0)
                tp          = round(entry + sl_distance * self.params.rr_ratio, digits)
            else:
                sl          = round(candle_high, digits)
                sl_distance = max(sl - entry, 0)
                tp          = round(entry - sl_distance * self.params.rr_ratio, digits)

            logger.info(
                f"[{self.strategy_id}] CANDLE TP/SL {symbol} | "
                f"dir={direction} entry={entry:.5f} sl={sl:.5f} "
                f"sl_dist={sl_distance:.5f} rr={self.params.rr_ratio} tp={tp:.5f} "
                f"(risk ${sl_distance:.5f} → target ${sl_distance * self.params.rr_ratio:.5f})"
            )
            return tp, sl

        elif self.params.tpsl_mode == TPSLMode.POINTS:
            point = sym_info["point"]
            if direction == "long":
                return (
                    round(entry + self.params.tp_points * point, digits),
                    round(entry - self.params.sl_points * point, digits),
                )
            else:
                return (
                    round(entry - self.params.tp_points * point, digits),
                    round(entry + self.params.sl_points * point, digits),
                )

        else:  # PERCENT
            if direction == "long":
                return (
                    round(entry * (1 + self.params.tp_pct / 100), digits),
                    round(entry * (1 - self.params.sl_pct / 100), digits),
                )
            else:
                return (
                    round(entry * (1 - self.params.tp_pct / 100), digits),
                    round(entry * (1 + self.params.sl_pct / 100), digits),
                )

    def _calculate_lot_size(
        self,
        symbol:      str,
        entry_price: float = None,
        candle_high: float = None,
        candle_low:  float = None,
    ) -> float:
        """
        Calculate lot size.

        CANDLE tpsl_mode:
            SL distance = entry - candle_low (long) or candle_high - entry (short)
            sl_value_per_lot = SL_distance × contract_size
            lots = risk_amount ÷ sl_value_per_lot

        POINTS / PERCENT tpsl_mode:
            sl_value_per_lot = sl_points × point × contract_size  (fixed)
            lots = risk_amount ÷ sl_value_per_lot

        Always clamped to min_lot_size, max_lot_size, and broker limits.
        """
        sym_info      = self._get_symbol_info(symbol)
        contract_size = sym_info["contract_size"]
        volume_step   = sym_info["volume_step"]
        volume_min    = sym_info["volume_min"]
        volume_max    = sym_info["volume_max"]
        mode          = self.params.sizing_mode

        # ── FIXED LOTS — skip all math ────────────────────────────────────────
        if mode == SizingMode.FIXED_LOTS:
            return self.params.fixed_lot_size

        # ── Determine dollar risk amount ──────────────────────────────────────
        if mode == SizingMode.FIXED_USD:
            risk_amount = self.params.risk_amount_usd
        else:  # RISK_PCT
            balance     = self._get_current_balance()
            risk_amount = balance * (self.params.risk_pct / 100.0)

        # ── Determine SL value per lot ────────────────────────────────────────
        if self.params.tpsl_mode == TPSLMode.CANDLE:
            # Dynamic — different every trade based on candle size
            if entry_price is None or candle_high is None or candle_low is None:
                logger.warning(
                    f"[{self.strategy_id}] CANDLE sizing missing prices — min lot"
                )
                return self.params.min_lot_size

            sl_distance = (
                entry_price - candle_low
                if entry_price >= candle_low
                else candle_high - entry_price
            )

            if sl_distance <= 0:
                logger.warning(
                    f"[{self.strategy_id}] SL distance = 0 "
                    f"entry={entry_price} H={candle_high} L={candle_low} — min lot"
                )
                return volume_min

            sl_value_per_lot = sl_distance * contract_size

            logger.info(
                f"[{self.strategy_id}] CANDLE SIZING {symbol} | "
                f"risk=${risk_amount:.2f} sl_dist={sl_distance:.5f} "
                f"× contract={contract_size} = ${sl_value_per_lot:.2f}/lot"
            )

        elif self.params.tpsl_mode == TPSLMode.POINTS:
            point            = sym_info["point"]
            sl_value_per_lot = self.params.sl_points * point * contract_size

        else:  # PERCENT
            if entry_price:
                sl_price         = entry_price * (1 - self.params.sl_pct / 100)
                sl_distance      = entry_price - sl_price
                sl_value_per_lot = sl_distance * contract_size
            else:
                sl_value_per_lot = (self.params.sl_pct / 100) * 3000 * contract_size

        if sl_value_per_lot <= 0:
            logger.warning(f"[{self.strategy_id}] sl_value_per_lot=0 — min lot")
            return volume_min

        # ── Raw lot calculation ───────────────────────────────────────────────
        raw = risk_amount / sl_value_per_lot

        # ── Snap to broker volume step ────────────────────────────────────────
        snapped = round(round(raw / volume_step) * volume_step, 8)

        # ── Clamp to strategy + broker limits ────────────────────────────────
        final = max(self.params.min_lot_size, min(self.params.max_lot_size, snapped))
        final = max(volume_min, min(volume_max, final))

        logger.info(
            f"[{self.strategy_id}] LOT SIZE {symbol} | "
            f"mode={mode.value} risk=${risk_amount:.2f} "
            f"sl_val/lot=${sl_value_per_lot:.2f} "
            f"raw={raw:.4f} → final={final:.2f} lots"
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
            # Gold fallback
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
        if mode == TPSLMode.CANDLE:
            return (
                f"  TP/SL       : CANDLE mode\n"
                f"  SL          : candle low (long) / candle high (short)\n"
                f"  RR ratio    : {self.params.rr_ratio} "
                f"(TP = {self.params.rr_ratio}× SL distance)\n"
                f"  Lots        : dynamic per trade\n"
            )
        elif mode == TPSLMode.POINTS:
            return f"  TP/SL       : {self.params.tp_points}pt / {self.params.sl_points}pt\n"
        else:
            return f"  TP/SL       : {self.params.tp_pct}% / {self.params.sl_pct}%\n"