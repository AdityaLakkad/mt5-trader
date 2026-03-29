"""
orders/live_order_manager.py
=============================
Real MT5 order execution — drop-in replacement for OrderManager.

⚠️  WARNING: This sends REAL orders to your broker.
    Only use on a DEMO account until fully validated.

Switch between paper and live in trading_engine.py:
    engine = TradingEngine(config, mode="paper")  ← safe
    engine = TradingEngine(config, mode="live")   ← real money
"""

import logging
from datetime import datetime
from queue import Queue
from typing import List, Optional, Set

import MetaTrader5 as mt5

from config.settings import AccountConfig
from connectors.mt5_connector import MT5Connector
from core.events import (
    OrderEvent, OrderSide, FillEvent, FillStatus
)
from orders.order_manager import ClosedTrade   # reuse dataclass

logger = logging.getLogger(__name__)


class LiveOrderManager:
    """
    Sends real orders to MT5 via order_send().
    Mirrors the OrderManager interface exactly so the engine works unchanged.
    """

    def __init__(self, account_config: AccountConfig,
                 connector: MT5Connector, event_queue: Queue,
                 magic_number: int = 234001,
                 slippage_points: int = 10):
        self.connector       = connector
        self.event_queue     = event_queue
        self.magic           = magic_number
        self.slippage        = slippage_points
        self.initial_balance = account_config.initial_balance
        self.closed_trades:  List[ClosedTrade] = []
        self._known_tickets: Set[int]          = set()

        trade = None
        try:
            from MetaTrader5 import _CTrade
        except ImportError:
            pass

        # Use the Trade helper from MT5
        # (We import inline to avoid issues if not available)
        self._trade = None
        self._init_trade()

    def _init_trade(self):
        try:
            import importlib
            self._trade_module = importlib.import_module("MetaTrader5")
        except Exception as e:
            logger.error(f"[LiveOrderManager] Could not import MT5: {e}")

    # ── Mirror paper OrderManager interface ───────────────────────────────────

    @property
    def balance(self) -> float:
        info = mt5.account_info()
        return float(info.balance) if info else 0.0

    @property
    def equity(self) -> float:
        info = mt5.account_info()
        return float(info.equity) if info else 0.0

    @property
    def open_trade_count(self) -> int:
        positions = mt5.positions_get()
        return len([p for p in positions
                    if p.magic == self.magic]) if positions else 0

    def positions_for_symbol(self, symbol: str) -> list:
        positions = mt5.positions_get(symbol=symbol)
        return [p for p in positions
                if p.magic == self.magic] if positions else []

    # ── Execute order ─────────────────────────────────────────────────────────

    def execute_order(self, order: OrderEvent) -> Optional[FillEvent]:
        """Send a real market order to MT5."""
        tick = mt5.symbol_info_tick(order.symbol)
        if tick is None:
            logger.error(f"[LiveOrderManager] No tick for {order.symbol}")
            return self._reject(order, "no_tick")

        # Handle FLAT (close all positions for this symbol)
        if order.comment == "FLAT" or order.volume == 0.0:
            return self._close_all_for_symbol(order.symbol)

        order_type = (mt5.ORDER_TYPE_BUY if order.side == OrderSide.BUY
                      else mt5.ORDER_TYPE_SELL)
        price = tick.ask if order.side == OrderSide.BUY else tick.bid

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       order.symbol,
            "volume":       float(order.volume),
            "type":         order_type,
            "price":        price,
            "sl":           float(order.sl) if order.sl else 0.0,
            "tp":           float(order.tp) if order.tp else 0.0,
            "deviation":    self.slippage,
            "magic":        self.magic,
            "comment":      order.comment or "BB_Engulf",
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        logger.info(
            f"[LiveOrderManager] Sending order: {order.symbol} "
            f"{order.side.value} {order.volume} lots @ {price:.5f}"
        )

        result = mt5.order_send(request)

        if result is None:
            logger.error(f"[LiveOrderManager] order_send returned None: {mt5.last_error()}")
            return self._reject(order, "null_result")

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            self._known_tickets.add(result.order)
            logger.info(
                f"[LiveOrderManager] ✅ FILLED {order.symbol} "
                f"{order.side.value} {order.volume} @ {result.price} "
                f"ticket={result.order}"
            )
            fill = FillEvent(
                type=None,
                order_id=str(result.order),
                strategy_id=order.strategy_id,
                symbol=order.symbol,
                side=order.side,
                volume=order.volume,
                fill_price=result.price,
                commission=0.0,
                sl=order.sl,
                tp=order.tp,
                status=FillStatus.FILLED,
            )
            self.event_queue.put(fill)
            return fill
        else:
            logger.error(
                f"[LiveOrderManager] ❌ REJECTED {order.symbol} "
                f"retcode={result.retcode} — {result.comment}"
            )
            return self._reject(order, f"retcode_{result.retcode}")

    def check_sl_tp(self, tick) -> None:
        """
        MT5 manages SL/TP server-side.
        This polls for positions closed by the broker since last check.
        """
        self.monitor_closed_positions()

    def monitor_closed_positions(self) -> None:
        """
        Detect positions closed by broker (SL/TP hit) since last check.
        Emits FillEvents with pnl set so strategy.on_fill() resets state.
        """
        current_positions = mt5.positions_get()
        current_tickets = (
            {p.ticket for p in current_positions if p.magic == self.magic}
            if current_positions else set()
        )

        closed_tickets = self._known_tickets - current_tickets

        for ticket in closed_tickets:
            self._known_tickets.discard(ticket)

            # Look up the close deal in history
            from_time = datetime(2020, 1, 1)
            to_time   = datetime.utcnow()
            deals = mt5.history_deals_get(
                from_time, to_time, position=ticket
            )
            if not deals:
                continue

            closing_deal = deals[-1]
            pnl = closing_deal.profit + closing_deal.commission + closing_deal.swap

            fill = FillEvent(
                type=None,
                order_id=str(ticket),
                strategy_id="",
                symbol=closing_deal.symbol,
                side=OrderSide.SELL,
                volume=closing_deal.volume,
                fill_price=closing_deal.price,
                commission=closing_deal.commission,
                status=FillStatus.FILLED,
                pnl=pnl,
                closing_order_id=str(ticket),
            )
            self.event_queue.put(fill)
            logger.info(
                f"[LiveOrderManager] Position closed by broker "
                f"ticket={ticket} symbol={closing_deal.symbol} pnl={pnl:+.2f}"
            )

    # ── Private ───────────────────────────────────────────────────────────────

    def _close_all_for_symbol(self, symbol: str) -> None:
        """Close all open positions for this symbol."""
        positions = self.positions_for_symbol(symbol)
        for pos in positions:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                continue
            close_type = (mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY
                          else mt5.ORDER_TYPE_BUY)
            price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask
            request = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       symbol,
                "volume":       pos.volume,
                "type":         close_type,
                "price":        price,
                "position":     pos.ticket,
                "deviation":    self.slippage,
                "magic":        self.magic,
                "comment":      "FLAT",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(f"[LiveOrderManager] Closed position {pos.ticket} for {symbol}")
            else:
                logger.error(
                    f"[LiveOrderManager] Could not close {pos.ticket}: "
                    f"{result.retcode if result else 'no result'}"
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
        logger.warning(f"[LiveOrderManager] Order REJECTED — {reason}")
        return fill
