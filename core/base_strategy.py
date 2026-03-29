"""
core/base_strategy.py
=====================
Abstract base class for all strategies.
Subclass this and implement on_bar() and/or on_tick().
"""

from abc import ABC
from queue import Queue
from typing import Optional

from core.events import (
    TickEvent, BarEvent, FillEvent,
    SignalEvent, SignalDirection,
)


class BaseStrategy(ABC):

    def __init__(self, strategy_id: str, symbols: list, event_queue: Queue):
        self.strategy_id = strategy_id
        self.symbols = symbols
        self.event_queue = event_queue
        self._active = True

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    def on_start(self) -> None:  pass
    def on_stop(self)  -> None:  pass

    # ── Market data hooks ─────────────────────────────────────────────────────
    def on_tick(self, event: TickEvent) -> None: pass
    def on_bar(self,  event: BarEvent)  -> None: pass
    def on_fill(self, event: FillEvent) -> None: pass

    # ── Signal helpers ────────────────────────────────────────────────────────
    def signal_long(self, symbol: str, strength: float = 1.0,
                    sl: Optional[float] = None, tp: Optional[float] = None,
                    metadata: Optional[dict] = None) -> None:
        self._push_signal(symbol, SignalDirection.LONG, strength, sl, tp, metadata)

    def signal_short(self, symbol: str, strength: float = 1.0,
                     sl: Optional[float] = None, tp: Optional[float] = None,
                     metadata: Optional[dict] = None) -> None:
        self._push_signal(symbol, SignalDirection.SHORT, strength, sl, tp, metadata)

    def signal_flat(self, symbol: str,
                    metadata: Optional[dict] = None) -> None:
        self._push_signal(symbol, SignalDirection.FLAT, metadata=metadata)

    def _push_signal(self, symbol: str, direction: SignalDirection,
                     strength: float = 1.0, sl: Optional[float] = None,
                     tp: Optional[float] = None,
                     metadata: Optional[dict] = None) -> None:
        if not self._active:
            return
        self.event_queue.put(SignalEvent(
            type=None,
            strategy_id=self.strategy_id,
            symbol=symbol,
            direction=direction,
            strength=strength,
            suggested_sl=sl,
            suggested_tp=tp,
            metadata=metadata or {},
        ))

    @property
    def active(self) -> bool:
        return self._active

    @active.setter
    def active(self, value: bool) -> None:
        self._active = value

    def __repr__(self) -> str:
        return f"<Strategy id={self.strategy_id} symbols={self.symbols} active={self._active}>"
