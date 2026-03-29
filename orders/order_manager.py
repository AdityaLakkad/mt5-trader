"""
orders/order_manager.py
========================
Paper trading execution engine.
Simulates fills, manages SL/TP, tracks portfolio state.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from queue import Queue
from typing import Dict, List, Optional

from config.settings import AccountConfig
from connectors.mt5_connector import MT5Connector
from core.events import (
    OrderEvent, OrderSide, FillEvent, FillStatus, TickEvent
)

logger = logging.getLogger(__name__)


@dataclass
class Position:
    position_id: str
    strategy_id: str
    symbol: str
    side: OrderSide
    volume: float
    entry_price: float
    sl: Optional[float]
    tp: Optional[float]
    open_time: datetime
    commission: float = 0.0

    def unrealised_pnl(self, current_price: float,
                       contract_size: float = 100_000.0) -> float:
        direction = 1 if self.side == OrderSide.BUY else -1
        return direction * (current_price - self.entry_price) * self.volume * contract_size

    def is_sl_hit(self, bid: float, ask: float) -> bool:
        if self.sl is None:
            return False
        return bid <= self.sl if self.side == OrderSide.BUY else ask >= self.sl

    def is_tp_hit(self, bid: float, ask: float) -> bool:
        if self.tp is None:
            return False
        return bid >= self.tp if self.side == OrderSide.BUY else ask <= self.tp


@dataclass
class ClosedTrade:
    position_id: str
    strategy_id: str
    symbol: str
    side: OrderSide
    volume: float
    entry_price: float
    exit_price: float
    sl: Optional[float]
    tp: Optional[float]
    open_time: datetime
    close_time: datetime
    pnl: float
    commission: float
    close_reason: str

    @property
    def duration_seconds(self) -> float:
        return (self.close_time - self.open_time).total_seconds()

    @property
    def net_pnl(self) -> float:
        return self.pnl - self.commission


class OrderManager:

    def __init__(self, account_config: AccountConfig,
                 connector: MT5Connector, event_queue: Queue,
                 slippage_pips: float = 0.5,
                 commission_per_lot: float = 7.0):
        self.connector          = connector
        self.event_queue        = event_queue
        self.slippage_pips      = slippage_pips
        self.commission_per_lot = commission_per_lot
        self.initial_balance    = account_config.initial_balance
        self.balance            = account_config.initial_balance
        self.open_positions:    Dict[str, Position]  = {}
        self.closed_trades:     List[ClosedTrade]    = []

    @property
    def equity(self) -> float:
        unrealised = sum(
            p.unrealised_pnl(self._mid_price(p.symbol))
            for p in self.open_positions.values()
        )
        return self.balance + unrealised

    @property
    def open_trade_count(self) -> int:
        return len(self.open_positions)

    def positions_for_symbol(self, symbol: str) -> List[Position]:
        return [p for p in self.open_positions.values() if p.symbol == symbol]

    def execute_order(self, order: OrderEvent) -> Optional[FillEvent]:
        tick = self.connector.get_tick(order.symbol)
        if tick is None:
            logger.error(f"[OrderManager] No tick for {order.symbol} – rejected.")
            return self._reject(order, "no_tick")

        sym_info = self.connector.get_symbol_info(order.symbol)
        pip      = sym_info["pip_size"]            if sym_info else 0.0001
        contract = sym_info["trade_contract_size"] if sym_info else 100_000.0

        if order.comment == "FLAT" or order.volume == 0.0:
            return self._close_positions(order, tick, contract, pip, reason="FLAT")

        fill_price = self._fill_price(order.side, tick, pip)
        commission = round(order.volume * self.commission_per_lot, 4)
        pos_id     = str(uuid.uuid4())[:8]

        position = Position(
            position_id=pos_id,
            strategy_id=order.strategy_id,
            symbol=order.symbol,
            side=order.side,
            volume=order.volume,
            entry_price=fill_price,
            sl=order.sl,
            tp=order.tp,
            open_time=datetime.utcnow(),
            commission=commission,
        )
        self.open_positions[pos_id] = position
        self.balance -= commission

        fill = FillEvent(
            type=None,
            order_id=pos_id,
            strategy_id=order.strategy_id,
            symbol=order.symbol,
            side=order.side,
            volume=order.volume,
            fill_price=fill_price,
            commission=commission,
            sl=order.sl,
            tp=order.tp,
            status=FillStatus.FILLED,
        )
        self.event_queue.put(fill)
        logger.info(
            f"[OrderManager] OPEN {order.symbol} {order.side.value} "
            f"{order.volume:.2f} @ {fill_price:.5f} | id={pos_id}"
        )
        return fill

    def check_sl_tp(self, tick: TickEvent) -> None:
        to_close = []
        for pos_id, pos in self.open_positions.items():
            if pos.symbol != tick.symbol:
                continue
            if pos.is_sl_hit(tick.bid, tick.ask):
                price = tick.bid if pos.side == OrderSide.BUY else tick.ask
                to_close.append((pos_id, "SL", price))
            elif pos.is_tp_hit(tick.bid, tick.ask):
                price = tick.bid if pos.side == OrderSide.BUY else tick.ask
                to_close.append((pos_id, "TP", price))
        for pos_id, reason, exit_price in to_close:
            self._close_position_by_id(pos_id, exit_price, reason)

    def _fill_price(self, side: OrderSide, tick: dict, pip: float) -> float:
        slippage = self.slippage_pips * pip
        return round(tick["ask"] + slippage, 8) if side == OrderSide.BUY \
               else round(tick["bid"] - slippage, 8)

    def _mid_price(self, symbol: str) -> float:
        tick = self.connector.get_tick(symbol)
        return tick["ask"] if tick else 0.0

    def _close_positions(self, order, tick, contract, pip, reason):
        for pos in self.positions_for_symbol(order.symbol):
            exit_price = tick["bid"] if pos.side == OrderSide.BUY else tick["ask"]
            self._close_position_by_id(pos.position_id, exit_price, reason)
        return None

    def _close_position_by_id(self, pos_id: str,
                               exit_price: float, reason: str) -> None:
        pos = self.open_positions.pop(pos_id, None)
        if pos is None:
            return

        sym_info = self.connector.get_symbol_info(pos.symbol)
        contract = sym_info["trade_contract_size"] if sym_info else 100_000.0
        direction = 1 if pos.side == OrderSide.BUY else -1
        pnl = round(
            direction * (exit_price - pos.entry_price) * pos.volume * contract, 4
        )
        self.balance += pnl

        trade = ClosedTrade(
            position_id=pos_id,
            strategy_id=pos.strategy_id,
            symbol=pos.symbol,
            side=pos.side,
            volume=pos.volume,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            sl=pos.sl,
            tp=pos.tp,
            open_time=pos.open_time,
            close_time=datetime.utcnow(),
            pnl=pnl,
            commission=pos.commission,
            close_reason=reason,
        )
        self.closed_trades.append(trade)

        close_side = OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY
        fill = FillEvent(
            type=None,
            order_id=pos_id,
            strategy_id=pos.strategy_id,
            symbol=pos.symbol,
            side=close_side,
            volume=pos.volume,
            fill_price=exit_price,
            commission=0.0,
            status=FillStatus.FILLED,
            pnl=pnl,
            closing_order_id=pos_id,
        )
        self.event_queue.put(fill)
        logger.info(
            f"[OrderManager] CLOSE {pos.symbol} {pos.side.value} "
            f"@ {exit_price:.5f} | PnL={pnl:+.2f} | reason={reason}"
        )

    def _reject(self, order: OrderEvent, reason: str) -> FillEvent:
        fill = FillEvent(
            type=None,
            strategy_id=order.strategy_id,
            symbol=order.symbol,
            side=order.side,
            volume=order.volume,
            fill_price=0.0,
            status=FillStatus.REJECTED,
        )
        self.event_queue.put(fill)
        logger.warning(f"[OrderManager] Order REJECTED – {reason}")
        return fill
