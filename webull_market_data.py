"""Webull market data — quotes and streaming.

Uses asyncio.to_thread() for REST calls.
WebSocket streaming for real-time quotes.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from webull_auth import get_auth

logger = logging.getLogger(__name__)

_instance = None


def get_market_data() -> "WebullMarketData":
    global _instance
    if _instance is None:
        _instance = WebullMarketData()
    return _instance


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


class WebullMarketData:
    def __init__(self):
        self._auth = get_auth()
        self._quote_cache: dict[str, Quote] = {}
        self._streaming = False
        self._stream_task: asyncio.Task | None = None

    @property
    def _wb(self):
        return self._auth.client

    async def get_quote(self, symbol: str) -> Quote:
        """Get a single quote."""
        try:
            data = await asyncio.to_thread(self._wb.get_quote, symbol)
            if not data:
                return Quote(symbol=symbol)
            quote = self._parse_quote(symbol, data)
            self._quote_cache[symbol] = quote
            return quote
        except Exception:
            logger.exception("Failed to get quote for %s", symbol)
            return Quote(symbol=symbol)

    async def get_quotes_batch(self, symbols: list[str]) -> dict[str, Quote]:
        """Get quotes for multiple symbols."""
        results = {}
        # Webull doesn't have a batch quote API, so we parallelize
        tasks = [self.get_quote(s) for s in symbols]
        quotes = await asyncio.gather(*tasks, return_exceptions=True)
        for symbol, quote in zip(symbols, quotes):
            if isinstance(quote, Exception):
                logger.warning("Quote error for %s: %s", symbol, quote)
                results[symbol] = Quote(symbol=symbol)
            else:
                results[symbol] = quote
        return results

    async def get_premarket_gainers(self, count: int = 50) -> list[dict]:
        """Get premarket gainers from Webull screener."""
        try:
            data = await asyncio.to_thread(self._wb.get_pre_market_gainers)
            if not data:
                return []
            return data[:count]
        except Exception:
            logger.exception("Failed to get premarket gainers")
            return []

    async def get_premarket_losers(self, count: int = 50) -> list[dict]:
        """Get premarket losers."""
        try:
            data = await asyncio.to_thread(self._wb.get_pre_market_losers)
            if not data:
                return []
            return data[:count]
        except Exception:
            logger.exception("Failed to get premarket losers")
            return []

    async def get_ticker_id(self, symbol: str) -> int | None:
        """Get Webull internal ticker ID for a symbol."""
        try:
            result = await asyncio.to_thread(self._wb.get_ticker, symbol)
            return int(result) if result else None
        except Exception:
            logger.exception("Failed to get ticker ID for %s", symbol)
            return None

    def get_cached_quote(self, symbol: str) -> Quote | None:
        """Get last cached quote (non-blocking)."""
        return self._quote_cache.get(symbol)

    def _parse_quote(self, symbol: str, data: dict) -> Quote:
        """Parse Webull quote response into Quote dataclass."""
        return Quote(
            symbol=symbol,
            price=float(data.get("close", data.get("price", 0)) or 0),
            bid=float(data.get("bid", 0) or 0),
            ask=float(data.get("ask", 0) or 0),
            bid_size=int(data.get("bidSize", 0) or 0),
            ask_size=int(data.get("askSize", 0) or 0),
            volume=int(data.get("volume", 0) or 0),
            change_pct=float(data.get("changeRatio", 0) or 0) * 100,
            prev_close=float(data.get("preClose", 0) or 0),
            high=float(data.get("high", 0) or 0),
            low=float(data.get("low", 0) or 0),
            open=float(data.get("open", 0) or 0),
            timestamp=data.get("tradeTime", ""),
        )
