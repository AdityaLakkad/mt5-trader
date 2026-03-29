"""
core/events.py
==============
All event types that flow through the framework event queue.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import pandas as pd


class EventType(Enum):
    MARKET_TICK = "MARKET_TICK"
    MARKET_BAR  = "MARKET_BAR"
    SIGNAL      = "SIGNAL"
    ORDER       = "ORDER"
    FILL        = "FILL"


class SignalDirection(Enum):
    LONG  = "LONG"
    SHORT = "SHORT"
    FLAT  = "FLAT"


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT  = "LIMIT"
    STOP   = "STOP"


class OrderSide(Enum):
    BUY  = "BUY"
    SELL = "SELL"


class FillStatus(Enum):
    FILLED   = "FILLED"
    REJECTED = "REJECTED"
    PARTIAL  = "PARTIAL"


@dataclass
class BaseEvent:
    type: EventType
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class TickEvent(BaseEvent):
    symbol: str = ""
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    volume: float = 0.0

    def __post_init__(self):
        self.type = EventType.MARKET_TICK

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass
class BarEvent(BaseEvent):
    symbol: str = ""
    timeframe: int = 0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    bars_df: Optional[pd.DataFrame] = field(default=None, repr=False)

    def __post_init__(self):
        self.type = EventType.MARKET_BAR


@dataclass
class SignalEvent(BaseEvent):
    strategy_id: str = ""
    symbol: str = ""
    direction: SignalDirection = SignalDirection.FLAT
    strength: float = 1.0
    suggested_sl: Optional[float] = None
    suggested_tp: Optional[float] = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        self.type = EventType.SIGNAL


@dataclass
class OrderEvent(BaseEvent):
    strategy_id: str = ""
    symbol: str = ""
    order_type: OrderType = OrderType.MARKET
    side: OrderSide = OrderSide.BUY
    volume: float = 0.01
    price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    comment: str = ""

    def __post_init__(self):
        self.type = EventType.ORDER


@dataclass
class FillEvent(BaseEvent):
    order_id: str = ""
    strategy_id: str = ""
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    volume: float = 0.01
    fill_price: float = 0.0
    commission: float = 0.0
    sl: Optional[float] = None
    tp: Optional[float] = None
    status: FillStatus = FillStatus.FILLED
    pnl: Optional[float] = None
    closing_order_id: Optional[str] = None

    def __post_init__(self):
        self.type = EventType.FILL
