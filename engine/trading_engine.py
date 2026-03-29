"""
engine/trading_engine.py
=========================
Main event loop — wires all modules together for live/paper trading.
"""

import logging
import signal
import threading
import time
from queue import Empty, Queue

from config.settings import FrameworkConfig
from connectors.mt5_connector import MT5Connector
from core.base_strategy import BaseStrategy
from core.events import EventType, SignalDirection
from data.data_feed import DataFeed
from orders.order_manager import OrderManager
from risk.risk_manager import RiskManager
from analytics.performance import PerformanceAnalytics

logger = logging.getLogger(__name__)


class TradingEngine:

    def __init__(self, config: FrameworkConfig, mode: str = "paper"):
        self.config      = config
        self._setup_logging()

        self.event_queue: Queue = Queue()
        self.connector   = MT5Connector(config.mt5)
        # ── Order manager — swap here to go live ──────────────────────
        if mode == "live":
            from orders.live_order_manager import LiveOrderManager
            self.order_manager = LiveOrderManager(
                account_config=config.account,
                connector=self.connector,
                event_queue=self.event_queue,
            )
            logger.warning(
                "[Engine] ⚠️  LIVE MODE — real orders will be sent to MT5"
            )
        else:
            self.order_manager = OrderManager(
                account_config=config.account,
                connector=self.connector,
                event_queue=self.event_queue,
            )
            logger.info("[Engine] Paper trading mode.")
        self.risk_manager = RiskManager(
            config=config.risk,
            connector=self.connector,
            event_queue=self.event_queue,
        )
        self.analytics  = PerformanceAnalytics(self.order_manager)
        self.data_feed: DataFeed = None
        self._strategy: BaseStrategy = None
        self._running   = False
        self._last_bar_check = 0.0

        signal.signal(signal.SIGINT,  self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    def set_strategy(self, strategy: BaseStrategy) -> None:
        self._strategy = strategy
        logger.info(f"[Engine] Strategy registered: {strategy}")

    def run(self) -> None:
        logger.info("[Engine] Starting MT5 Paper Trading Framework ...")

        if not self.connector.connect():
            logger.error("[Engine] MT5 connection failed. Aborting.")
            return

        self.data_feed = DataFeed(
            connector=self.connector,
            config=self.config.data,
            queue=self.event_queue,
        )
        self.data_feed.initialise()

        if self._strategy:
            self._strategy.on_start()

        self._running = True
        logger.info("[Engine] Entering main loop. Press CTRL+C to stop.")

        try:
            while self._running:
                self._loop_iteration()
                time.sleep(self.config.engine.loop_interval_seconds)
        finally:
            self._shutdown()

    def stop(self) -> None:
        self._running = False

    def print_report(self) -> None:
        self.analytics.print_report()

    def plot_equity(self, save_path: str = None) -> None:
        self.analytics.plot_equity_curve(save_path=save_path)

    def _loop_iteration(self) -> None:
        self.data_feed.poll_ticks()
        now = time.monotonic()
        if now - self._last_bar_check >= self.config.engine.bar_check_interval_seconds:
            self.data_feed.poll_bars()
            self._last_bar_check = now
        while not self.event_queue.empty():
            try:
                event = self.event_queue.get_nowait()
                self._process_event(event)
            except Empty:
                break

    def _process_event(self, event) -> None:
        etype = event.type

        if etype == EventType.MARKET_TICK:
            self.order_manager.check_sl_tp(event)
            if self._strategy:
                self._strategy.on_tick(event)

        elif etype == EventType.MARKET_BAR:
            if self._strategy:
                self._strategy.on_bar(event)

        elif etype == EventType.SIGNAL:
            tick = self.connector.get_tick(event.symbol)
            self.risk_manager.process_signal(
                signal=event,
                current_balance=self.order_manager.balance,
                current_equity=self.order_manager.equity,
                initial_balance=self.order_manager.initial_balance,
                open_trade_count=self.order_manager.open_trade_count,
                current_price=(
                    tick["ask"] if tick and event.direction == SignalDirection.LONG
                    else tick["bid"] if tick else None
                ),
            )

        elif etype == EventType.ORDER:
            self.order_manager.execute_order(event)

        elif etype == EventType.FILL:
            self.analytics.update()
            if self._strategy:
                self._strategy.on_fill(event)
            logger.info(
                f"[Portfolio] Balance=${self.order_manager.balance:.2f} | "
                f"Equity=${self.order_manager.equity:.2f} | "
                f"Open={self.order_manager.open_trade_count} | "
                f"Closed={len(self.order_manager.closed_trades)}"
            )

    def _shutdown(self) -> None:
        logger.info("[Engine] Shutting down ...")
        if self._strategy:
            self._strategy.on_stop()
        self.analytics.print_report()
        self.analytics.plot_equity_curve(save_path="equity_curve.png")
        self.connector.disconnect()
        logger.info("[Engine] Shutdown complete.")

    def _handle_shutdown(self, signum, frame) -> None:
        logger.info("[Engine] Shutdown signal received.")
        self._running = False
        t = threading.Thread(target=self._force_stop, daemon=True)
        t.start()

    def _force_stop(self) -> None:
        import os
        time.sleep(4)
        os._exit(0)

    def _setup_logging(self) -> None:
        level = getattr(logging, self.config.engine.log_level.upper(), logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s  %(levelname)-8s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
