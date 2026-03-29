"""
engine/backtest_engine.py
==========================
Replays historical OHLCV data through the EXACT same event pipeline
as live paper trading. A strategy that works in backtest works live
with zero code changes.

Features
--------
- Synthetic tick simulation from OHLC (4 ticks per bar)
- Multi-symbol support (bars interleaved by timestamp)
- Warmup period so indicators are ready before first signal
- Rich progress bar during replay
- Full PerformanceAnalytics at the end

Usage
-----
    from engine.backtest_engine import BacktestEngine

    engine = BacktestEngine(config)
    engine.connect()
    engine.load_symbol("XAUUSD.GNE", mt5.TIMEFRAME_M15, bars=2000)

    strategy = BBEngulfingBreakoutStrategy(["XAUUSD.GNE"], engine.event_queue, params)
    engine.set_strategy(strategy)

    results = engine.run()
    engine.print_report()
    engine.plot_equity(save_path="./results/backtest_equity.png")
"""

import logging
from datetime import datetime, timezone
from queue import Queue
from typing import Optional

import pandas as pd

from config.settings import FrameworkConfig
from connectors.mt5_connector import MT5Connector
from core.base_strategy import BaseStrategy
from core.events import (
    EventType, TickEvent, BarEvent,
    SignalEvent, SignalDirection,
    OrderEvent, FillEvent,
)
from orders.order_manager import OrderManager
from risk.risk_manager import RiskManager
from analytics.performance import PerformanceAnalytics

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Synthetic tick generator
# ---------------------------------------------------------------------------

def _synthetic_ticks(symbol: str, bar: pd.Series,
                     bar_time, spread: float = 0.002):
    """
    Generate 4 synthetic ticks from one OHLC bar.
    Sequence:
        Bullish bar: O → L → H → C
        Bearish bar: O → H → L → C
    This simulates realistic intra-bar SL/TP hits.
    """
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    sequence = [o, l, h, c] if c >= o else [o, h, l, c]
    ticks = []
    for i, price in enumerate(sequence):
        bid = round(price - spread / 2, 8)
        ask = round(price + spread / 2, 8)
        ts  = pd.Timestamp(bar_time, tz=timezone.utc) + pd.Timedelta(seconds=i)
        ticks.append(TickEvent(
            type=None,
            timestamp=ts,
            symbol=symbol,
            bid=bid,
            ask=ask,
            last=price,
            volume=bar.get("volume", 0) / 4,
        ))
    return ticks


# ---------------------------------------------------------------------------
# BacktestEngine
# ---------------------------------------------------------------------------

class BacktestEngine:

    def __init__(self, config: FrameworkConfig,
                 spread_pips: Optional[dict] = None,
                 slippage_pips: float = 0.5,
                 commission_per_lot: float = 7.0):
        self.config         = config
        self.spread_pips    = spread_pips or {}
        self.event_queue    = Queue()
        self.connector      = MT5Connector(config.mt5)
        self.order_manager  = OrderManager(
            account_config=config.account,
            connector=self.connector,
            event_queue=self.event_queue,
            slippage_pips=slippage_pips,
            commission_per_lot=commission_per_lot,
        )
        self.risk_manager   = RiskManager(
            config=config.risk,
            connector=self.connector,
            event_queue=self.event_queue,
        )
        self.analytics      = PerformanceAnalytics(self.order_manager)
        self._strategy: Optional[BaseStrategy] = None
        self._data: dict    = {}
        self._setup_logging()

    # ── Data loading ──────────────────────────────────────────────────────────

    def connect(self) -> bool:
        return self.connector.connect()

    def disconnect(self) -> None:
        self.connector.disconnect()

    def load_symbol(self, symbol: str, timeframe: int,
                    bars: int = 1000) -> bool:
        if not self.connector.is_connected:
            logger.error("[Backtest] Not connected. Call connect() first.")
            return False
        df = self.connector.get_bars(symbol, timeframe, bars)
        if df is None or df.empty:
            logger.warning(f"[Backtest] No data for {symbol}.")
            return False
        self._data[symbol] = df
        logger.info(f"[Backtest] Loaded {len(df)} bars for {symbol}.")
        return True

    def load_dataframe(self, symbol: str, df: pd.DataFrame) -> None:
        """
        Load a custom DataFrame directly (offline / CSV data).
        df must have a DatetimeIndex and columns: open, high, low, close, volume
        """
        required = {"open", "high", "low", "close"}
        if not required.issubset(df.columns):
            raise ValueError(f"DataFrame must have columns: {required}")
        self._data[symbol] = df.copy()
        logger.info(f"[Backtest] Loaded {len(df)} bars for {symbol} (from DataFrame).")

    def set_strategy(self, strategy: BaseStrategy) -> None:
        self._strategy = strategy

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self) -> dict:
        if not self._data:
            raise RuntimeError("[Backtest] No data loaded.")
        if not self._strategy:
            raise RuntimeError("[Backtest] No strategy set.")

        combined     = self._build_timeline()
        total_bars   = len(combined)
        warmup       = max(self.config.data.bar_history, 50)
        history: dict = {}

        logger.info(
            f"[Backtest] Replaying {total_bars} bars "
            f"across {len(self._data)} symbol(s). Warmup={warmup} bars."
        )

        if self._strategy:
            self._strategy.on_start()

        try:
            from rich.progress import (
                Progress, BarColumn, TextColumn, TimeRemainingColumn
            )
            _rich = True
        except ImportError:
            _rich = False

        def _replay():
            for idx, (bar_time, symbol, bar) in enumerate(combined):
                # Extend rolling history
                new_row = pd.DataFrame([bar], index=[bar_time])
                history[symbol] = (
                    pd.concat([history[symbol], new_row]).iloc[-self.config.data.bar_history:]
                    if symbol in history else new_row
                )
                bars_df = history[symbol]

                # Intra-bar tick simulation for SL/TP
                sym_info = self.connector.get_symbol_info(symbol) or {}
                pip      = sym_info.get("pip_size", 0.0001)
                spread   = self.spread_pips.get(symbol, 1.5) * pip
                for tick in _synthetic_ticks(symbol, bar, bar_time, spread):
                    self.order_manager.check_sl_tp(tick)
                    self._drain(bar_time)

                # Fire BarEvent only after warmup
                if len(bars_df) >= warmup:
                    ts = bar_time.to_pydatetime() if hasattr(bar_time, "to_pydatetime") \
                         else bar_time
                    self.event_queue.put(BarEvent(
                        type=None, timestamp=ts, symbol=symbol, timeframe=0,
                        open=bar["open"], high=bar["high"],
                        low=bar["low"],   close=bar["close"],
                        volume=bar.get("volume", 0), bars_df=bars_df.copy(),
                    ))
                    self._drain(bar_time)

                yield idx

        if _rich:
            from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn
            with Progress(
                TextColumn("[cyan]Backtesting"),
                BarColumn(),
                TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
                TimeRemainingColumn(),
            ) as progress:
                task = progress.add_task("", total=total_bars)
                for _ in _replay():
                    progress.advance(task)
        else:
            for i, _ in enumerate(_replay()):
                if i % 200 == 0:
                    print(f"  Progress: {i}/{total_bars} bars ...", end="\r")
            print()

        self._close_all_open()

        if self._strategy:
            self._strategy.on_stop()

        logger.info("[Backtest] Replay complete.")
        return self.analytics.compute()

    # ── Reporting ─────────────────────────────────────────────────────────────

    def print_report(self) -> None:
        self.analytics.print_report()

    def plot_equity(self, save_path: Optional[str] = None) -> None:
        self.analytics.plot_equity_curve(save_path=save_path)

    def trades_df(self) -> pd.DataFrame:
        return self.analytics.trades_dataframe()

    # ── Private ───────────────────────────────────────────────────────────────

    def _build_timeline(self) -> list:
        rows = []
        for symbol, df in self._data.items():
            for ts, bar in df.iterrows():
                rows.append((ts, symbol, bar))
        rows.sort(key=lambda x: x[0])
        return rows

    def _drain(self, current_time) -> None:
        while not self.event_queue.empty():
            event = self.event_queue.get_nowait()
            self._process_event(event)

    def _process_event(self, event) -> None:
        etype = event.type
        if etype == EventType.MARKET_BAR:
            if self._strategy:
                self._strategy.on_bar(event)
        elif etype == EventType.MARKET_TICK:
            if self._strategy:
                self._strategy.on_tick(event)
        elif etype == EventType.SIGNAL:
            tick = self.connector.get_tick(event.symbol) \
                   if self.connector.is_connected else None
            self.risk_manager.process_signal(
                signal=event,
                current_balance=self.order_manager.balance,
                current_equity=self.order_manager.equity,
                initial_balance=self.order_manager.initial_balance,
                open_trade_count=self.order_manager.open_trade_count,
                current_price=tick["ask"] if tick else None,
            )
        elif etype == EventType.ORDER:
            self.order_manager.execute_order(event)
        elif etype == EventType.FILL:
            self.analytics.update()
            if self._strategy:
                self._strategy.on_fill(event)

    def _close_all_open(self) -> None:
        for pos_id in list(self.order_manager.open_positions.keys()):
            pos  = self.order_manager.open_positions.get(pos_id)
            if pos:
                tick = self.connector.get_tick(pos.symbol) \
                       if self.connector.is_connected else None
                if tick:
                    exit_price = tick["bid"] if pos.side.value == "BUY" else tick["ask"]
                else:
                    df = self._data.get(pos.symbol)
                    exit_price = float(df["close"].iloc[-1]) if df is not None \
                                 else pos.entry_price
                self.order_manager._close_position_by_id(pos_id, exit_price, "END_OF_TEST")
        self._drain(datetime.utcnow())

    def _setup_logging(self) -> None:
        level = getattr(logging, self.config.engine.log_level.upper(), logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s  %(levelname)-8s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
