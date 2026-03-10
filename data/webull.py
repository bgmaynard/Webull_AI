"""Webull market data connector — Uses webull-python unofficial API.

Currently blocked by Webull (403). Kept as reference.
"""

import asyncio
import logging

from core.interfaces import MarketDataInterface
from core.models import Quote
from webull_auth import get_auth

logger = logging.getLogger(__name__)


class WebullMarketData(MarketDataInterface):
    def __init__(self):
        self._auth = get_auth()
        self._quote_cache: dict[str, Quote] = {}

    @property
    def _wb(self):
        return self._auth.client

    async def get_quote(self, symbol: str) -> Quote:
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
        results = {}
        tasks = [self.get_quote(s) for s in symbols]
        quotes = await asyncio.gather(*tasks, return_exceptions=True)
        for symbol, quote in zip(symbols, quotes):
            if isinstance(quote, Exception):
                results[symbol] = Quote(symbol=symbol)
            else:
                results[symbol] = quote
        return results

    async def get_premarket_gainers(self, count: int = 50) -> list[dict]:
        try:
            data = await asyncio.to_thread(self._wb.get_pre_market_gainers)
            return (data or [])[:count]
        except Exception:
            logger.exception("Failed to get premarket gainers")
            return []

    async def get_premarket_losers(self, count: int = 50) -> list[dict]:
        try:
            data = await asyncio.to_thread(self._wb.get_pre_market_losers)
            return (data or [])[:count]
        except Exception:
            logger.exception("Failed to get premarket losers")
            return []

    def get_cached_quote(self, symbol: str) -> Quote | None:
        return self._quote_cache.get(symbol)

    def _parse_quote(self, symbol: str, data: dict) -> Quote:
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
