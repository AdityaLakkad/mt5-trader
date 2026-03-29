"""
config/settings.py
==================
Central configuration for the MT5 Paper Trading Framework.
Edit this file or override values in your main entry point.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class MT5Config:
    login: int = 0
    password: str = ""
    server: str = ""
    path: str = ""
    timeout: int = 10_000


@dataclass
class AccountConfig:
    initial_balance: float = 10_000.0
    currency: str = "USD"
    leverage: int = 100


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 1.0
    max_open_trades: int = 5
    default_sl_pips: float = 30.0
    default_tp_pips: float = 60.0
    max_drawdown_pct: float = 20.0


@dataclass
class DataConfig:
    symbols: List[str] = field(default_factory=lambda: ["XAUUSD"])
    bar_timeframe: int = 16390          # TIMEFRAME_M15
    bar_history: int = 500
    tick_enabled: bool = True
    bar_enabled: bool = True


@dataclass
class EngineConfig:
    loop_interval_seconds: float = 1.0
    bar_check_interval_seconds: float = 5.0
    log_level: str = "INFO"


@dataclass
class FrameworkConfig:
    mt5: MT5Config = field(default_factory=MT5Config)
    account: AccountConfig = field(default_factory=AccountConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    data: DataConfig = field(default_factory=DataConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
