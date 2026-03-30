"""
orders/live_order_manager.py
=============================
Real MT5 order execution — drop-in replacement for OrderManager.

⚠️  WARNING: This sends REAL orders to your broker.
    Only use on a DEMO account until fully validated.

Bug fixes vs original version
------------------------------
1. open_positions dict — kept in sync so dashboard works
2. closed_trades list — ClosedTrade objects created on close detection
3. _known_tickets seeded on startup from existing MT5 positions
4. monitor_closed_positions on a timer NOT on every tick
5. history_deals_get called correctly (position= kwarg only, no date range)
6. strategy_id tracked per ticket so on_fill() state machine resets correctly
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from queue import Queue
from typing import Dict, List, Optional, Set

import MetaTrader5 as mt5

from config.settings import AccountConfig
from connectors.mt5_connector import MT5Connector
from core.events import (
    OrderEvent, OrderSide, FillEvent, FillStatus
)
from orders.order_manager import ClosedTrade, Position   # reuse dataclasses

logger = logging.getLogger(__name__)


class LiveOrderManager:
    """
    Sends real orders to MT5 via order_send().
    Mirrors OrderManager interface exactly — dashboard and engine
    work identically in paper and live modes.

    Parameters
    ----------
    magic_number        : unique int to identify this EA's orders in MT5
    slippage_points     : max deviation from requested price (points)
    monitor_interval_s  : how often to poll MT5 for closed positions (seconds)
                          default 2.0 — do NOT poll on every tick
    """

    def __init__(
        self,
        account_config: AccountConfig,
        connector: MT5Connector,
        event_queue: Queue,
        magic_number: int       = 234001,
        slippage_points: int    = 10,
        monitor_interval_s: float = 2.0,
    ):
        self.connector          = connector
        self.event_queue        = event_queue
        self.magic              = magic_number
        self.slippage           = slippage_points
        self.monitor_interval   = monitor_interval_s
        self.initial_balance    = account_config.initial_balance

        # ── State — mirrors OrderManager interface exactly ─────────────────────
        self.open_positions: Dict[str, Position]  = {}   # ticket_str → Position
        self.closed_trades:  List[ClosedTrade]    = []

        # Internal tracking
        self._known_tickets:    Set[int]          = set()
        self._ticket_to_strategy: Dict[int, str]  = {}   # ticket → strategy_id
        self._last_monitor_time: float            = 0.0

        # Seed from any already-open positions (e.g. opened before engine start)
        self._seed_from_mt5()

    # =========================================================================
    # Properties — mirror OrderManager interface
    # =========================================================================

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
        return len(self.open_positions)

    def positions_for_symbol(self, symbol: str) -> List[Position]:
        return [p for p in self.open_positions.values() if p.symbol == symbol]

    # =========================================================================
    # Execute order
    # =========================================================================

    def execute_order(self, order: OrderEvent) -> Optional[FillEvent]:
        """Send a real market order to MT5."""

        # Handle FLAT — close all positions for this symbol
        if order.comment == "FLAT" or order.volume == 0.0:
            self._close_all_for_symbol(order)
            return None

        tick = mt5.symbol_info_tick(order.symbol)
        if tick is None:
            logger.error(f"[LiveOrderManager] No tick for {order.symbol}")
            return self._reject(order, "no_tick")

        order_type = (
            mt5.ORDER_TYPE_BUY  if order.side == OrderSide.BUY
            else mt5.ORDER_TYPE_SELL
        )
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
            "comment":      (order.comment or "BB_Engulf")[:31],  # MT5 max 31 chars
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        logger.info(
            f"[LiveOrderManager] Sending {order.symbol} "
            f"{order.side.value} {order.volume:.2f} lots @ {price:.5f} "
            f"SL={order.sl} TP={order.tp}"
        )

        result = mt5.order_send(request)

        if result is None:
            logger.error(
                f"[LiveOrderManager] order_send returned None: {mt5.last_error()}"
            )
            return self._reject(order, "null_result")

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            ticket = result.order

            # Track this ticket so we detect when it closes
            self._known_tickets.add(ticket)
            self._ticket_to_strategy[ticket] = order.strategy_id

            # Add to open_positions dict so dashboard shows it
            position = Position(
                position_id=str(ticket),
                strategy_id=order.strategy_id,
                symbol=order.symbol,
                side=order.side,
                volume=order.volume,
                entry_price=result.price,
                sl=order.sl,
                tp=order.tp,
                open_time=datetime.utcnow(),
                commission=0.0,
            )
            self.open_positions[str(ticket)] = position

            logger.info(
                f"[LiveOrderManager] ✅ FILLED {order.symbol} "
                f"{order.side.value} {order.volume:.2f} @ {result.price:.5f} "
                f"ticket={ticket}"
            )

            fill = FillEvent(
                type=None,
                order_id=str(ticket),
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

    # =========================================================================
    # SL/TP monitoring — called by engine on every tick
    # but internally throttled to monitor_interval seconds
    # =========================================================================

    def check_sl_tp(self, tick) -> None:
        """
        Called by engine on every tick.
        Internally throttled — only polls MT5 every monitor_interval seconds.
        MT5 handles SL/TP server-side, we just detect when positions close.
        """
        now = time.monotonic()
        if now - self._last_monitor_time < self.monitor_interval:
            return
        self._last_monitor_time = now
        self._monitor_closed_positions()

    # =========================================================================
    # Private — close detection
    # =========================================================================

    def _monitor_closed_positions(self) -> None:
        """
        Poll MT5 for positions we were tracking that are now gone.
        Creates ClosedTrade objects and emits FillEvents with pnl set.
        """
        if not self._known_tickets:
            return

        # Get current open positions from MT5
        current_mt5 = mt5.positions_get()
        current_tickets = (
            {p.ticket for p in current_mt5 if p.magic == self.magic}
            if current_mt5 else set()
        )

        # Tickets we tracked that are now closed
        closed_tickets = self._known_tickets - current_tickets

        for ticket in list(closed_tickets):
            self._known_tickets.discard(ticket)

            # ── Fetch deal history for this position ──────────────────────
            # Correct MT5 Python API: position= kwarg only, no date range
            deals = mt5.history_deals_get(position=ticket)

            if deals is None or len(deals) == 0:
                # Fallback: search with wide date range
                from_time = datetime(2020, 1, 1)
                deals = mt5.history_deals_get(from_time, datetime.utcnow())
                if deals:
                    deals = [d for d in deals if d.position_id == ticket]
                else:
                    deals = []

            if not deals:
                logger.warning(
                    f"[LiveOrderManager] No deal history for ticket {ticket}"
                )
                # Still remove from open_positions to stay in sync
                pos = self.open_positions.pop(str(ticket), None)
                strategy_id = self._ticket_to_strategy.pop(ticket, "")
                self._emit_close_fill(ticket, pos, strategy_id, pnl=0.0,
                                      exit_price=0.0, reason="UNKNOWN")
                continue

            # Opening deal = entry_type IN (0=buy, 1=sell)
            # Closing deal = entry_type OUT (1 or 5)
            opening_deal = next(
                (d for d in deals if d.entry == mt5.DEAL_ENTRY_IN), None
            )
            closing_deal = next(
                (d for d in reversed(deals) if d.entry in (
                    mt5.DEAL_ENTRY_OUT,
                    mt5.DEAL_ENTRY_INOUT,
                    mt5.DEAL_ENTRY_OUT_BY,
                )), None
            )

            if closing_deal is None:
                closing_deal = deals[-1]  # fallback to last deal

            # Calculate total PnL including commission and swap
            pnl = sum(
                d.profit + d.commission + d.swap
                for d in deals
            )
            pnl = round(pnl, 4)

            # Determine close reason
            reason = self._close_reason(closing_deal, ticket)

            # Remove from open_positions
            pos = self.open_positions.pop(str(ticket), None)
            strategy_id = self._ticket_to_strategy.pop(ticket, "")

            # Create ClosedTrade for analytics
            if pos:
                trade = ClosedTrade(
                    position_id=str(ticket),
                    strategy_id=strategy_id,
                    symbol=closing_deal.symbol,
                    side=pos.side,
                    volume=closing_deal.volume,
                    entry_price=opening_deal.price if opening_deal else pos.entry_price,
                    exit_price=closing_deal.price,
                    sl=pos.sl,
                    tp=pos.tp,
                    open_time=pos.open_time,
                    close_time=datetime.utcfromtimestamp(closing_deal.time),
                    pnl=pnl,
                    commission=sum(d.commission for d in deals),
                    close_reason=reason,
                )
                self.closed_trades.append(trade)

            self._emit_close_fill(
                ticket, pos, strategy_id,
                pnl=pnl,
                exit_price=closing_deal.price,
                reason=reason,
            )

            logger.info(
                f"[LiveOrderManager] 📋 CLOSED ticket={ticket} "
                f"symbol={closing_deal.symbol} "
                f"pnl={pnl:+.2f} reason={reason}"
            )

    def _emit_close_fill(self, ticket: int, pos: Optional[Position],
                         strategy_id: str, pnl: float,
                         exit_price: float, reason: str) -> None:
        """Push a closing FillEvent onto the queue."""
        symbol    = pos.symbol if pos else ""
        side      = (OrderSide.SELL if pos and pos.side == OrderSide.BUY
                     else OrderSide.BUY)
        volume    = pos.volume if pos else 0.0

        fill = FillEvent(
            type=None,
            order_id=str(ticket),
            strategy_id=strategy_id,      # ← correct id so on_fill() works
            symbol=symbol,
            side=side,
            volume=volume,
            fill_price=exit_price,
            commission=0.0,
            status=FillStatus.FILLED,
            pnl=pnl,                       # ← pnl set → strategy resets state
            closing_order_id=str(ticket),
        )
        self.event_queue.put(fill)

    def _close_reason(self, deal, ticket: int) -> str:
        """Infer close reason from deal comment or price vs SL/TP."""
        if deal is None:
            return "UNKNOWN"
        comment = (deal.comment or "").lower()
        if "sl" in comment or "stop loss" in comment:
            return "SL"
        if "tp" in comment or "take profit" in comment:
            return "TP"
        # Check if exit price matches our recorded SL/TP
        pos = self.open_positions.get(str(ticket))
        if pos:
            price = deal.price
            if pos.sl and abs(price - pos.sl) < 0.001:
                return "SL"
            if pos.tp and abs(price - pos.tp) < 0.001:
                return "TP"
        return "MANUAL"

    # =========================================================================
    # Seed existing positions on startup
    # =========================================================================

    def _seed_from_mt5(self) -> None:
        """
        On startup, load any already-open MT5 positions into our tracking.
        This handles positions opened before the engine started.
        """
        positions = mt5.positions_get()
        if not positions:
            return

        count = 0
        for p in positions:
            if p.magic != self.magic:
                continue

            ticket = p.ticket
            self._known_tickets.add(ticket)
            self._ticket_to_strategy[ticket] = ""  # unknown strategy

            side = OrderSide.BUY if p.type == mt5.POSITION_TYPE_BUY else OrderSide.SELL
            position = Position(
                position_id=str(ticket),
                strategy_id="",
                symbol=p.symbol,
                side=side,
                volume=p.volume,
                entry_price=p.price_open,
                sl=p.sl if p.sl > 0 else None,
                tp=p.tp if p.tp > 0 else None,
                open_time=datetime.utcfromtimestamp(p.time),
                commission=p.commission,
            )
            self.open_positions[str(ticket)] = position
            count += 1

        if count:
            logger.info(
                f"[LiveOrderManager] Seeded {count} existing position(s) from MT5."
            )

    # =========================================================================
    # Close all positions for a symbol (FLAT signal)
    # =========================================================================

    def _close_all_for_symbol(self, order: OrderEvent) -> None:
        symbol    = order.symbol
        positions = self.positions_for_symbol(symbol)

        if not positions:
            logger.debug(
                f"[LiveOrderManager] FLAT {symbol} — no open positions."
            )
            return

        for pos in positions:
            ticket_int = int(pos.position_id)
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                continue

            close_type = (
                mt5.ORDER_TYPE_SELL if pos.side == OrderSide.BUY
                else mt5.ORDER_TYPE_BUY
            )
            price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

            request = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       symbol,
                "volume":       pos.volume,
                "type":         close_type,
                "price":        price,
                "position":     ticket_int,
                "deviation":    self.slippage,
                "magic":        self.magic,
                "comment":      "FLAT",
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }

            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                logger.info(
                    f"[LiveOrderManager] FLAT {symbol} "
                    f"ticket={ticket_int} @ {result.price:.5f}"
                )
            else:
                retcode = result.retcode if result else "no result"
                logger.error(
                    f"[LiveOrderManager] Could not close {ticket_int}: {retcode}"
                )

    # =========================================================================
    # Reject helper
    # =========================================================================

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