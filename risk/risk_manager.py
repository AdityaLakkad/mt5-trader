"""
risk/risk_manager.py
=====================
Converts SignalEvents into sized OrderEvents.
Supports fixed_volume override via signal metadata.
"""

import logging
from queue import Queue
from typing import Optional

from config.settings import RiskConfig
from connectors.mt5_connector import MT5Connector
from core.events import (
    SignalEvent, SignalDirection,
    OrderEvent, OrderType, OrderSide,
)

logger = logging.getLogger(__name__)


class RiskManager:

    def __init__(self, config: RiskConfig, connector: MT5Connector,
                 event_queue: Queue):
        self.config      = config
        self.connector   = connector
        self.event_queue = event_queue

    def process_signal(self, signal: SignalEvent,
                       current_balance: float, current_equity: float,
                       initial_balance: float, open_trade_count: int,
                       current_price: Optional[float] = None) -> Optional[OrderEvent]:

        if signal.direction == SignalDirection.FLAT:
            return self._emit_flat(signal)

        if open_trade_count >= self.config.max_open_trades:
            logger.warning(
                f"[RiskManager] Blocked — max open trades "
                f"({self.config.max_open_trades}) reached."
            )
            return None

        if initial_balance > 0:
            dd_pct = (initial_balance - current_equity) / initial_balance * 100
            if dd_pct >= self.config.max_drawdown_pct:
                logger.warning(
                    f"[RiskManager] PAUSED — drawdown {dd_pct:.1f}% "
                    f">= limit {self.config.max_drawdown_pct}%"
                )
                return None

        sym_info = self.connector.get_symbol_info(signal.symbol)
        tick     = self.connector.get_tick(signal.symbol)
        if tick is None or sym_info is None:
            logger.warning(f"[RiskManager] No tick/info for {signal.symbol}.")
            return None

        if current_price is None:
            current_price = (
                tick["ask"] if signal.direction == SignalDirection.LONG
                else tick["bid"]
            )

        pip = sym_info["pip_size"]
        sl_price, tp_price = self._calc_sl_tp(signal, current_price, pip)

        sl_pips = abs(current_price - sl_price) / pip if sl_price else self.config.default_sl_pips
        volume  = self._calc_volume(signal, current_balance, sl_pips, signal.symbol)
        volume  = self.connector.normalize_volume(signal.symbol, volume)

        if volume <= 0:
            logger.warning(f"[RiskManager] Volume = 0 for {signal.symbol}. Skipping.")
            return None

        side  = OrderSide.BUY if signal.direction == SignalDirection.LONG else OrderSide.SELL
        order = OrderEvent(
            type=None,
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            order_type=OrderType.MARKET,
            side=side,
            volume=volume,
            price=None,
            sl=sl_price,
            tp=tp_price,
            comment=f"sig:{signal.strategy_id}",
        )
        self.event_queue.put(order)
        logger.info(
            f"[RiskManager] Order queued | {signal.symbol} {side.value} "
            f"{volume:.2f} lots | SL={sl_price} TP={tp_price}"
        )
        return order

    def _calc_sl_tp(self, signal, price, pip):
        is_long  = signal.direction == SignalDirection.LONG
        mult     = 1 if is_long else -1
        sl = signal.suggested_sl or round(price - mult * self.config.default_sl_pips * pip, 8)
        tp = signal.suggested_tp or round(price + mult * self.config.default_tp_pips * pip, 8)
        return sl, tp

    def _calc_volume(self, signal, balance, sl_pips, symbol) -> float:
        # Fixed volume override — strategy passes this via metadata
        if "fixed_volume" in signal.metadata:
            return float(signal.metadata["fixed_volume"])

        # Fixed fractional
        risk_amount = balance * self.config.risk_per_trade_pct / 100.0
        pip_val     = self.connector.pip_value(symbol, volume=1.0)
        if pip_val <= 0 or sl_pips <= 0:
            return 0.01
        return (risk_amount / (sl_pips * pip_val)) * signal.strength

    def _emit_flat(self, signal) -> OrderEvent:
        order = OrderEvent(
            type=None,
            strategy_id=signal.strategy_id,
            symbol=signal.symbol,
            order_type=OrderType.MARKET,
            side=OrderSide.SELL,
            volume=0.0,
            comment="FLAT",
        )
        self.event_queue.put(order)
        return order
