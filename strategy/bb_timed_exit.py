"""
strategy/bb_timed_exit.py
=========================
Engulfing Breakout — Timed Exit Variant

Entry logic (candle-only, no Bollinger Bands):
  LONG  : bullish engulfing candle → go long when ask crosses above candle high
  SHORT : bearish engulfing candle → go short when bid crosses below candle low

Exit logic (checked on every tick):
  1. Hard SL  : loss >= sl_points (default 10) → close immediately
  2. Timed    : trade open >= exit_after_minutes (default 11) → close at market,
                profit or loss — no choice.

No take-profit. The timed exit is always the maximum holding period.
Lot size is always fixed (default 1.0).
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from queue import Queue
from typing import Optional

from core.base_strategy import BaseStrategy
from core.events import BarEvent, TickEvent, FillEvent, FillStatus
from strategy.bb_engulfing_breakout import is_engulfing, SignalStateManager

logger = logging.getLogger(__name__)

# TP set impossibly far away so the paper broker never closes via TP —
# the strategy owns all exits (SL check and timed exit).
_NO_TP_POINTS = 999_999


@dataclass
class BBTimedExitParams:
    # ── Timeframe ──────────────────────────────────────────────────────────────
    timeframe: int = 16390            # M15 default (mt5.TIMEFRAME_M15)

    # ── Engulfing filter ───────────────────────────────────────────────────────
    engulf_tolerance_pct: float = 10.0

    # ── Signal expiry ──────────────────────────────────────────────────────────
    expiry_candles: int = 5

    # ── Candle size filter ─────────────────────────────────────────────────────
    max_candle_size_points: float = 0.0   # 0 = disabled

    # ── Lot size ───────────────────────────────────────────────────────────────
    fixed_lot_size: float = 1.0

    # ── Exit rules ─────────────────────────────────────────────────────────────
    sl_points:          float = 10.0   # close if loss >= this many points
    exit_after_minutes: int   = 11     # always close after N minutes (profit or loss)

    # ── Fill retry ─────────────────────────────────────────────────────────────
    max_fill_attempts: int = 3


@dataclass
class _TradeState:
    direction:   str      # "long" or "short"
    entry_price: float    # actual fill price (from FillEvent)
    open_time:   datetime # UTC time of fill confirmation
    sl_price:    float    # strategy-side SL price (for logging)


class BBTimedExitStrategy(BaseStrategy):

    def __init__(self, symbols: list, event_queue: Queue,
                 params: Optional[BBTimedExitParams] = None):
        super().__init__(
            strategy_id="BB_Timed_Exit",
            symbols=symbols,
            event_queue=event_queue,
        )
        self.params = params or BBTimedExitParams()
        self._sym_info_cache: dict = {}
        self._state_managers = {
            sym: SignalStateManager(
                expiry_candles=self.params.expiry_candles,
                max_fill_attempts=self.params.max_fill_attempts,
            )
            for sym in symbols
        }
        # Per-symbol open trade tracking (populated on fill open, cleared on fill close)
        self._trade_state: dict[str, Optional[_TradeState]] = {
            sym: None for sym in symbols
        }
        # Symbols for which we've already sent signal_flat, while waiting for
        # the close FillEvent — prevents duplicate flat signals.
        self._exit_pending: set = set()

    # =========================================================================
    # on_bar — engulfing-only entry detection (no BB)
    # =========================================================================

    def on_bar(self, event: BarEvent) -> None:
        if event.symbol not in self.symbols:
            return

        df = event.bars_df
        if df is None or len(df) < 2:
            return

        sm = self._state_managers[event.symbol]

        expired = sm.on_new_bar()
        if expired:
            logger.info(
                f"[{self.strategy_id}] {event.symbol} signal EXPIRED "
                f"after {self.params.expiry_candles} candles → IDLE"
            )

        if sm.is_in_trade():
            return

        current  = df.iloc[-1]
        previous = df.iloc[-2]

        engulf    = is_engulfing(current, previous, self.params.engulf_tolerance_pct)
        curr_body = abs(current["close"] - current["open"])
        prev_body = abs(previous["close"] - previous["open"])
        tol_amt   = curr_body * (self.params.engulf_tolerance_pct / 100.0)
        expanded  = curr_body + tol_amt * 2

        d_char = "🟢" if current["close"] >= current["open"] else "🔴"
        logger.info(
            f"[{self.strategy_id}] BAR {event.symbol} {d_char} | "
            f"O={current['open']:.5f} H={current['high']:.5f} "
            f"L={current['low']:.5f} C={current['close']:.5f} | "
            f"body={curr_body:.5f} expanded={expanded:.5f} prev={prev_body:.5f} "
            f"engulf={'✅ '+engulf if engulf else '❌ no'} | "
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

        if self.params.max_candle_size_points > 0:
            candle_size = float(current["high"]) - float(current["low"])
            if candle_size > self.params.max_candle_size_points:
                logger.info(
                    f"[{self.strategy_id}] CANDLE TOO BIG {event.symbol} | "
                    f"size={candle_size:.5f} > max={self.params.max_candle_size_points:.5f} → skip"
                )
                return

        if engulf == "bullish":
            sm.set_signal("long", float(current["high"]), float(current["low"]),
                          event.timestamp)
            logger.info(
                f"[{self.strategy_id}] 🟢 LONG SETUP {event.symbol} | "
                f"breakout_high={current['high']:.5f} | "
                f"expires in {self.params.expiry_candles} bars"
            )

        elif engulf == "bearish":
            sm.set_signal("short", float(current["high"]), float(current["low"]),
                          event.timestamp)
            logger.info(
                f"[{self.strategy_id}] 🔴 SHORT SETUP {event.symbol} | "
                f"breakout_low={current['low']:.5f} | "
                f"expires in {self.params.expiry_candles} bars"
            )

    # =========================================================================
    # on_tick — entry execution + exit monitoring
    # =========================================================================

    def on_tick(self, event: TickEvent) -> None:
        if event.symbol not in self.symbols:
            return

        sm = self._state_managers[event.symbol]

        # ── Exit monitoring ───────────────────────────────────────────────────
        if sm.is_in_trade():
            if event.symbol not in self._exit_pending:
                self._check_exits(event)
            return

        # ── Entry execution ───────────────────────────────────────────────────
        if not sm.is_waiting():
            return

        pending = sm.pending

        # Invalidation: opposite side breached before breakout
        if pending.direction == "long" and event.bid <= pending.breakout_low:
            logger.info(
                f"[{self.strategy_id}] ❌ LONG INVALIDATED {event.symbol} | "
                f"bid={event.bid:.5f} broke below low={pending.breakout_low:.5f} → IDLE"
            )
            sm.cancel_signal()
            return

        if pending.direction == "short" and event.ask >= pending.breakout_high:
            logger.info(
                f"[{self.strategy_id}] ❌ SHORT INVALIDATED {event.symbol} | "
                f"ask={event.ask:.5f} broke above high={pending.breakout_high:.5f} → IDLE"
            )
            sm.cancel_signal()
            return

        sym_info = self._get_symbol_info(event.symbol)
        point    = sym_info["point"]
        digits   = sym_info["digits"]

        # Long breakout
        if pending.direction == "long" and event.ask >= pending.breakout_high:
            entry  = event.ask
            sl_px  = round(entry - self.params.sl_points * point, digits)
            tp_px  = round(entry + _NO_TP_POINTS * point, digits)
            logger.info(
                f"[{self.strategy_id}] ✅ LONG BREAKOUT {event.symbol} | "
                f"ask={event.ask:.5f} >= {pending.breakout_high:.5f} | "
                f"SL={sl_px:.5f} ({self.params.sl_points}pt) | "
                f"lots={self.params.fixed_lot_size} | "
                f"timed-exit={self.params.exit_after_minutes}min"
            )
            sm.enter_trade()
            self.signal_long(
                event.symbol, sl=sl_px, tp=tp_px,
                metadata={"fixed_volume": self.params.fixed_lot_size, "entry": entry},
            )

        # Short breakout
        elif pending.direction == "short" and event.bid <= pending.breakout_low:
            entry  = event.bid
            sl_px  = round(entry + self.params.sl_points * point, digits)
            tp_px  = max(point, round(entry - _NO_TP_POINTS * point, digits))
            logger.info(
                f"[{self.strategy_id}] ✅ SHORT BREAKOUT {event.symbol} | "
                f"bid={event.bid:.5f} <= {pending.breakout_low:.5f} | "
                f"SL={sl_px:.5f} ({self.params.sl_points}pt) | "
                f"lots={self.params.fixed_lot_size} | "
                f"timed-exit={self.params.exit_after_minutes}min"
            )
            sm.enter_trade()
            self.signal_short(
                event.symbol, sl=sl_px, tp=tp_px,
                metadata={"fixed_volume": self.params.fixed_lot_size, "entry": entry},
            )

    # =========================================================================
    # on_fill
    # =========================================================================

    def on_fill(self, event: FillEvent) -> None:
        if event.symbol not in self._state_managers:
            return
        sm = self._state_managers[event.symbol]

        if event.pnl is not None:
            # Position closed (by paper SL, paper TP, or our signal_flat)
            self._trade_state[event.symbol] = None
            self._exit_pending.discard(event.symbol)
            sm.exit_trade()
            logger.info(
                f"[{self.strategy_id}] 📋 CLOSED {event.symbol} | "
                f"pnl={event.pnl:+.2f} → IDLE"
            )

        elif event.status == FillStatus.REJECTED:
            self._trade_state[event.symbol] = None
            self._exit_pending.discard(event.symbol)
            will_retry = sm.on_fill_rejected()
            logger.warning(
                f"[{self.strategy_id}] ❌ FILL REJECTED {event.symbol} | "
                f"{'will retry' if will_retry else 'giving up → IDLE'}"
            )

        elif event.status == FillStatus.FILLED and event.pnl is None:
            # Position opened — record actual fill price for exit monitoring
            sym_info  = self._get_symbol_info(event.symbol)
            point     = sym_info["point"]
            digits    = sym_info["digits"]
            fill      = event.fill_price
            direction = "long" if event.side.value == "BUY" else "short"
            sl_px     = (
                round(fill - self.params.sl_points * point, digits)
                if direction == "long"
                else round(fill + self.params.sl_points * point, digits)
            )
            self._trade_state[event.symbol] = _TradeState(
                direction=direction,
                entry_price=fill,
                open_time=datetime.utcnow(),
                sl_price=sl_px,
            )
            logger.info(
                f"[{self.strategy_id}] 🟩 OPENED {event.symbol} | "
                f"dir={direction} fill={fill:.5f} strategy-SL={sl_px:.5f} | "
                f"will close at {self.params.exit_after_minutes}min "
                f"or -{self.params.sl_points}pt loss"
            )

    # =========================================================================
    # Lifecycle
    # =========================================================================

    def on_start(self) -> None:
        logger.info(
            f"[{self.strategy_id}] Started\n"
            f"  Symbols        : {self.symbols}\n"
            f"  Timeframe      : {self.params.timeframe}\n"
            f"  Tolerance      : {self.params.engulf_tolerance_pct}%\n"
            f"  Max candle     : {self.params.max_candle_size_points or 'disabled'} pts\n"
            f"  Signal expiry  : {self.params.expiry_candles} candles\n"
            f"  Lot size       : {self.params.fixed_lot_size} (fixed)\n"
            f"  SL             : {self.params.sl_points} points from entry\n"
            f"  Timed exit     : {self.params.exit_after_minutes} minutes\n"
        )

    def get_state_summary(self) -> dict:
        return {sym: str(sm) for sym, sm in self._state_managers.items()}

    # =========================================================================
    # Private helpers
    # =========================================================================

    def _check_exits(self, event: TickEvent) -> None:
        symbol = event.symbol
        ts     = self._trade_state.get(symbol)
        if ts is None:
            return

        sym_info = self._get_symbol_info(symbol)
        point    = sym_info["point"]

        # Current P&L in points (negative = loss)
        if ts.direction == "long":
            pnl_points = (event.bid - ts.entry_price) / point
        else:
            pnl_points = (ts.entry_price - event.ask) / point

        elapsed_min = (datetime.utcnow() - ts.open_time).total_seconds() / 60.0

        if pnl_points <= -self.params.sl_points:
            logger.info(
                f"[{self.strategy_id}] 🛑 SL HIT {symbol} | "
                f"pnl={pnl_points:+.2f}pt (limit -{self.params.sl_points}pt) → FLAT"
            )
            self._exit_pending.add(symbol)
            self.signal_flat(symbol)

        elif elapsed_min >= self.params.exit_after_minutes:
            logger.info(
                f"[{self.strategy_id}] ⏱ TIMED EXIT {symbol} | "
                f"open={elapsed_min:.1f}min >= {self.params.exit_after_minutes}min | "
                f"pnl={pnl_points:+.2f}pt → FLAT"
            )
            self._exit_pending.add(symbol)
            self.signal_flat(symbol)

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
