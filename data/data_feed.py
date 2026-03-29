"""
data/data_feed.py
=================
Polls MT5 for ticks and bars, fires events into the queue.
"""

import logging
from datetime import datetime, timezone
from queue import Queue
from typing import Dict, Optional

import pandas as pd

from config.settings import DataConfig
from connectors.mt5_connector import MT5Connector
from core.events import TickEvent, BarEvent

logger = logging.getLogger(__name__)


class TickFeed:
    def __init__(self, connector: MT5Connector, symbols: list, queue: Queue):
        self.connector = connector
        self.symbols   = symbols
        self.queue     = queue
        self._last_tick_time: Dict[str, datetime] = {}

    def poll(self) -> None:
        for symbol in self.symbols:
            tick = self.connector.get_tick(symbol)
            if tick is None:
                continue
            last      = self._last_tick_time.get(symbol)
            tick_time = tick["time"]
            if last is None or tick_time > last:
                self._last_tick_time[symbol] = tick_time
                self.queue.put(TickEvent(
                    type=None,
                    timestamp=tick_time.to_pydatetime(),
                    symbol=symbol,
                    bid=tick["bid"],
                    ask=tick["ask"],
                    last=tick["last"],
                    volume=tick["volume"],
                ))


class BarFeed:
    def __init__(self, connector: MT5Connector, symbols: list,
                 timeframe: int, history_count: int, queue: Queue):
        self.connector     = connector
        self.symbols       = symbols
        self.timeframe     = timeframe
        self.history_count = history_count
        self.queue         = queue
        self._last_bar_time: Dict[str, pd.Timestamp] = {}
        self._bars_cache:    Dict[str, pd.DataFrame]  = {}

    def initialise(self) -> None:
        logger.info(
            f"Pre-loading {self.history_count} bars "
            f"for {len(self.symbols)} symbols ..."
        )
        for symbol in self.symbols:
            df = self.connector.get_bars(symbol, self.timeframe, self.history_count)
            if df is not None and not df.empty:
                self._bars_cache[symbol]    = df
                self._last_bar_time[symbol] = df.index[-1]
                logger.info(f"  {symbol}: {len(df)} bars loaded, last @ {df.index[-1]}")
            else:
                logger.warning(f"  {symbol}: could not load bars.")

    def get_bars(self, symbol: str) -> Optional[pd.DataFrame]:
        return self._bars_cache.get(symbol)

    def poll(self) -> None:
        for symbol in self.symbols:
            df = self.connector.get_bars(symbol, self.timeframe, self.history_count)
            if df is None or df.empty:
                continue
            latest_time = df.index[-1]
            last_seen   = self._last_bar_time.get(symbol)
            if last_seen is None or latest_time > last_seen:
                self._last_bar_time[symbol] = latest_time
                self._bars_cache[symbol]    = df
                if last_seen is not None:
                    last_bar = df.iloc[-1]
                    self.queue.put(BarEvent(
                        type=None,
                        timestamp=latest_time.to_pydatetime(),
                        symbol=symbol,
                        timeframe=self.timeframe,
                        open=last_bar["open"],
                        high=last_bar["high"],
                        low=last_bar["low"],
                        close=last_bar["close"],
                        volume=last_bar["volume"],
                        bars_df=df.copy(),
                    ))
                    logger.debug(f"New bar: {symbol} @ {latest_time} close={last_bar['close']}")


class DataFeed:
    def __init__(self, connector: MT5Connector, config: DataConfig, queue: Queue):
        self.config    = config
        self.tick_feed = TickFeed(connector, config.symbols, queue) if config.tick_enabled else None
        self.bar_feed  = BarFeed(connector, config.symbols, config.bar_timeframe,
                                 config.bar_history, queue) if config.bar_enabled else None

    def initialise(self) -> None:
        if self.bar_feed:
            self.bar_feed.initialise()

    def poll_ticks(self) -> None:
        if self.tick_feed:
            self.tick_feed.poll()

    def poll_bars(self) -> None:
        if self.bar_feed:
            self.bar_feed.poll()

    def get_bars(self, symbol: str) -> Optional[pd.DataFrame]:
        return self.bar_feed.get_bars(symbol) if self.bar_feed else None
