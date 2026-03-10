"""Shared models — Broker-agnostic dataclasses and enums.

All broker connectors and market data providers use these types.
"""

from dataclasses import dataclass
from enum import Enum


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


@dataclass
class Position:
    symbol: str
    qty: float
    avg_cost: float
    market_value: float
    unrealized_pnl: float
    side: str = "LONG"

    @property
    def current_price(self) -> float:
        if self.qty == 0:
            return 0.0
        return self.market_value / self.qty


@dataclass
class AccountInfo:
    account_id: str = ""
    net_liquidation: float = 0.0
    buying_power: float = 0.0
    cash: float = 0.0
    day_trades_remaining: int = 0
    account_type: str = "paper"


@dataclass
class OrderResult:
    success: bool
    order_id: str = ""
    message: str = ""


@dataclass
class OrderStatus:
    order_id: str
    symbol: str
    side: str
    qty: float
    filled_qty: float
    price: float
    status: str  # PENDING, FILLED, CANCELLED, FAILED
    timestamp: str = ""


@dataclass
class Quote:
    symbol: str
    price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bid_size: int = 0
    ask_size: int = 0
    volume: int = 0
    change_pct: float = 0.0
    prev_close: float = 0.0
    high: float = 0.0
    low: float = 0.0
    open: float = 0.0
    timestamp: str = ""

    @property
    def spread(self) -> float:
        if self.ask > 0 and self.bid > 0:
            return self.ask - self.bid
        return 0.0

    @property
    def spread_pct(self) -> float:
        if self.ask > 0:
            return (self.spread / self.ask) * 100
        return 0.0

    @property
    def mid(self) -> float:
        if self.ask > 0 and self.bid > 0:
            return (self.ask + self.bid) / 2
        return self.price
