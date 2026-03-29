"""
connectors/mt5_connector.py
============================
Low-level wrapper around the MetaTrader5 Python package.
"""

import logging
from typing import Optional
import pandas as pd

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

from config.settings import MT5Config

logger = logging.getLogger(__name__)


class MT5Connector:

    def __init__(self, config: MT5Config):
        self.config = config
        self._connected = False
        self._symbol_cache: dict = {}

    def connect(self) -> bool:
        if not MT5_AVAILABLE:
            logger.error("MetaTrader5 package not installed.")
            return False

        kwargs = {}
        if self.config.path:     kwargs["path"]     = self.config.path
        if self.config.login:    kwargs["login"]    = self.config.login
        if self.config.password: kwargs["password"] = self.config.password
        if self.config.server:   kwargs["server"]   = self.config.server
        kwargs["timeout"] = self.config.timeout

        if not mt5.initialize(**kwargs):
            logger.error(f"MT5 initialize failed: {mt5.last_error()}")
            return False

        info = mt5.terminal_info()
        logger.info(
            f"Connected to MT5 | Terminal: {info.name} | "
            f"Build: {info.build} | Connected: {info.connected}"
        )
        self._connected = True
        return True

    def disconnect(self) -> None:
        if self._connected and MT5_AVAILABLE:
            mt5.shutdown()
            self._connected = False
            logger.info("MT5 disconnected.")

    @property
    def is_connected(self) -> bool:
        return self._connected

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    def get_bars(self, symbol: str, timeframe: int,
                 count: int = 500) -> Optional[pd.DataFrame]:
        if not self._connected:
            return None

        # Force symbol visible in Market Watch
        mt5.symbol_select(symbol, True)

        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count + 1)
        if rates is None or len(rates) == 0:
            logger.warning(f"No bars returned for {symbol}: {mt5.last_error()}")
            return None

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)
        df.rename(columns={"tick_volume": "volume"}, inplace=True)
        return df.iloc[:-1][["open", "high", "low", "close", "volume"]]

    def get_tick(self, symbol: str) -> Optional[dict]:
        if not self._connected:
            return None
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        return {
            "bid":    tick.bid,
            "ask":    tick.ask,
            "last":   tick.last,
            "volume": tick.volume,
            "time":   pd.Timestamp(tick.time, unit="s", tz="UTC"),
        }

    def get_symbol_info(self, symbol: str) -> Optional[dict]:
        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]
        if not self._connected:
            return None

        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)
        if info is None:
            logger.warning(f"No symbol info for {symbol}: {mt5.last_error()}")
            return None

        result = {
            "digits":              info.digits,
            "point":               info.point,
            "trade_contract_size": info.trade_contract_size,
            "volume_min":          info.volume_min,
            "volume_max":          info.volume_max,
            "volume_step":         info.volume_step,
            "currency_profit":     info.currency_profit,
            "pip_size":            info.point * (10 if info.digits in (3, 5) else 1),
        }
        self._symbol_cache[symbol] = result
        return result

    def pip_value(self, symbol: str, volume: float = 1.0) -> float:
        info = self.get_symbol_info(symbol)
        if info is None:
            return 10.0
        tick = self.get_tick(symbol)
        price = tick["ask"] if tick else 1.0
        pip = info["pip_size"]
        contract = info["trade_contract_size"]
        return (pip / price) * contract * volume

    def normalize_volume(self, symbol: str, raw_volume: float) -> float:
        info = self.get_symbol_info(symbol)
        if info is None:
            return round(raw_volume, 2)
        step  = info["volume_step"]
        v_min = info["volume_min"]
        v_max = info["volume_max"]
        volume = round(round(raw_volume / step) * step, 8)
        return max(v_min, min(v_max, volume))
