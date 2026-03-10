"""Abstract interfaces — Every broker and data provider implements these.

BrokerInterface: order execution, positions, account info.
MarketDataInterface: quotes, batch quotes, scanner data.
"""

from abc import ABC, abstractmethod

from core.models import (
    AccountInfo,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Quote,
)


class BrokerInterface(ABC):
    @abstractmethod
    async def get_account(self) -> AccountInfo: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: int,
        order_type: OrderType = OrderType.LIMIT,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    async def get_order_status(self, order_id: str) -> OrderStatus | None: ...

    @abstractmethod
    async def get_open_orders(self) -> list[OrderStatus]: ...


class MarketDataInterface(ABC):
    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    async def get_quotes_batch(self, symbols: list[str]) -> dict[str, Quote]: ...

    @abstractmethod
    async def get_premarket_gainers(self, count: int = 50) -> list[dict]: ...

    @abstractmethod
    async def get_premarket_losers(self, count: int = 50) -> list[dict]: ...

    def get_cached_quote(self, symbol: str) -> Quote | None:
        """Get last cached quote (non-blocking). Override if caching supported."""
        return None
