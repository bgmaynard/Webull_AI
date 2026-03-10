"""Polygon.io market data connector — Free tier with rate limiting.

Uses polygon-api-client SDK. Caches aggressively to respect rate limits.
Free tier: 5 API calls/minute. Uses snapshot endpoints for efficiency.
"""

import asyncio
import logging
import os
import time

from core.interfaces import MarketDataInterface
from core.models import Quote

logger = logging.getLogger(__name__)

# Minimum seconds between API calls (free tier: 5/min = 12s between calls)
# Starter tier: 100/min. Set via env var.
_MIN_CALL_INTERVAL = float(os.getenv("POLYGON_RATE_LIMIT_SECONDS", "1.0"))


class PolygonMarketData(MarketDataInterface):
    def __init__(self):
        api_key = os.getenv("POLYGON_API_KEY", "")
        if not api_key:
            raise ValueError("POLYGON_API_KEY is required")

        from polygon import RESTClient
        self._client = RESTClient(api_key)
        self._quote_cache: dict[str, Quote] = {}
        self._cache_timestamps: dict[str, float] = {}
        self._last_api_call: float = 0.0
        self._cache_ttl: float = float(os.getenv("POLYGON_CACHE_TTL", "5.0"))
        logger.info("Polygon.io market data initialized")

    async def _rate_limit(self):
        """Enforce rate limiting between API calls."""
        elapsed = time.time() - self._last_api_call
        if elapsed < _MIN_CALL_INTERVAL:
            await asyncio.sleep(_MIN_CALL_INTERVAL - elapsed)
        self._last_api_call = time.time()

    def _is_cache_fresh(self, symbol: str) -> bool:
        ts = self._cache_timestamps.get(symbol, 0)
        return (time.time() - ts) < self._cache_ttl

    async def get_quote(self, symbol: str) -> Quote:
        symbol = symbol.upper()

        # Return cached if fresh
        if self._is_cache_fresh(symbol) and symbol in self._quote_cache:
            return self._quote_cache[symbol]

        try:
            await self._rate_limit()
            snapshot = await asyncio.to_thread(
                self._client.get_snapshot_ticker, "stocks", symbol
            )

            if not snapshot:
                return Quote(symbol=symbol)

            # Build quote from snapshot
            day = snapshot.day or {}
            prev_day = snapshot.prev_day or {}
            last_quote = snapshot.last_quote or {}
            last_trade = snapshot.last_trade or {}

            price = float(getattr(last_trade, "price", 0) or 0)
            prev_close = float(getattr(prev_day, "close", 0) or 0)
            change_pct = 0.0
            if prev_close > 0 and price > 0:
                change_pct = ((price - prev_close) / prev_close) * 100

            quote = Quote(
                symbol=symbol,
                price=price,
                bid=float(getattr(last_quote, "bid_price", 0) or 0),
                ask=float(getattr(last_quote, "ask_price", 0) or 0),
                bid_size=int(getattr(last_quote, "bid_size", 0) or 0),
                ask_size=int(getattr(last_quote, "ask_size", 0) or 0),
                volume=int(getattr(day, "volume", 0) or 0),
                change_pct=round(change_pct, 2),
                prev_close=round(prev_close, 2),
                high=float(getattr(day, "high", 0) or 0),
                low=float(getattr(day, "low", 0) or 0),
                open=float(getattr(day, "open", 0) or 0),
                timestamp=str(getattr(last_trade, "sip_timestamp", "") or ""),
            )

            self._quote_cache[symbol] = quote
            self._cache_timestamps[symbol] = time.time()
            return quote

        except Exception:
            logger.exception("Polygon quote error for %s", symbol)
            # Return stale cache if available
            if symbol in self._quote_cache:
                return self._quote_cache[symbol]
            return Quote(symbol=symbol)

    async def get_quotes_batch(self, symbols: list[str]) -> dict[str, Quote]:
        """Get quotes via snapshot_all for efficiency (1 API call)."""
        results = {}

        try:
            await self._rate_limit()
            snapshots = await asyncio.to_thread(
                self._client.get_snapshot_all, "stocks"
            )

            # Build lookup
            snap_map = {}
            if snapshots:
                for snap in snapshots:
                    ticker = getattr(snap, "ticker", "")
                    if ticker:
                        snap_map[ticker.upper()] = snap

            for symbol in symbols:
                symbol = symbol.upper()
                snap = snap_map.get(symbol)
                if snap:
                    day = snap.day or {}
                    prev_day = snap.prev_day or {}
                    last_quote = snap.last_quote or {}
                    last_trade = snap.last_trade or {}

                    price = float(getattr(last_trade, "price", 0) or 0)
                    prev_close = float(getattr(prev_day, "close", 0) or 0)
                    change_pct = ((price - prev_close) / prev_close * 100) if prev_close > 0 and price > 0 else 0

                    quote = Quote(
                        symbol=symbol,
                        price=price,
                        bid=float(getattr(last_quote, "bid_price", 0) or 0),
                        ask=float(getattr(last_quote, "ask_price", 0) or 0),
                        bid_size=int(getattr(last_quote, "bid_size", 0) or 0),
                        ask_size=int(getattr(last_quote, "ask_size", 0) or 0),
                        volume=int(getattr(day, "volume", 0) or 0),
                        change_pct=round(change_pct, 2),
                        prev_close=round(prev_close, 2),
                        high=float(getattr(day, "high", 0) or 0),
                        low=float(getattr(day, "low", 0) or 0),
                        open=float(getattr(day, "open", 0) or 0),
                    )
                    self._quote_cache[symbol] = quote
                    self._cache_timestamps[symbol] = time.time()
                    results[symbol] = quote
                else:
                    results[symbol] = self._quote_cache.get(symbol, Quote(symbol=symbol))

        except Exception:
            logger.exception("Polygon batch quote error")
            for symbol in symbols:
                results[symbol] = self._quote_cache.get(symbol.upper(), Quote(symbol=symbol.upper()))

        return results

    async def get_premarket_gainers(self, count: int = 50) -> list[dict]:
        """Get premarket/current gainers via snapshots.

        Returns normalized format: [{symbol, price, prev_close, volume, change_pct, ...}]
        """
        try:
            await self._rate_limit()
            snapshots = await asyncio.to_thread(
                self._client.get_snapshot_all, "stocks"
            )

            if not snapshots:
                return []

            gainers = []
            for snap in snapshots:
                ticker = getattr(snap, "ticker", "")
                if not ticker:
                    continue

                day = snap.day or {}
                prev_day = snap.prev_day or {}
                last_trade = snap.last_trade or {}

                price = float(getattr(last_trade, "price", 0) or 0)
                prev_close = float(getattr(prev_day, "close", 0) or 0)
                volume = int(getattr(day, "volume", 0) or 0)

                if prev_close <= 0 or price <= 0:
                    continue

                change_pct = ((price - prev_close) / prev_close) * 100
                if change_pct <= 0:
                    continue

                # Standardized format (compatible with pipeline._build_symbol_data)
                gainers.append({
                    "ticker": {"symbol": ticker},
                    "symbol": ticker,
                    "close": price,
                    "price": price,
                    "preClose": prev_close,
                    "volume": volume,
                    "changeRatio": change_pct / 100,
                    "change_pct": change_pct,
                    "avgVolume": int(getattr(prev_day, "volume", 0) or 0),
                    "outstandingShares": 0,  # Not available from snapshot
                })

            gainers.sort(key=lambda x: x["change_pct"], reverse=True)
            return gainers[:count]

        except Exception:
            logger.exception("Polygon gainers error")
            return []

    async def get_premarket_losers(self, count: int = 50) -> list[dict]:
        try:
            await self._rate_limit()
            snapshots = await asyncio.to_thread(
                self._client.get_snapshot_all, "stocks"
            )

            if not snapshots:
                return []

            losers = []
            for snap in snapshots:
                ticker = getattr(snap, "ticker", "")
                if not ticker:
                    continue

                day = snap.day or {}
                prev_day = snap.prev_day or {}
                last_trade = snap.last_trade or {}

                price = float(getattr(last_trade, "price", 0) or 0)
                prev_close = float(getattr(prev_day, "close", 0) or 0)

                if prev_close <= 0 or price <= 0:
                    continue

                change_pct = ((price - prev_close) / prev_close) * 100
                if change_pct >= 0:
                    continue

                losers.append({
                    "ticker": {"symbol": ticker},
                    "symbol": ticker,
                    "close": price,
                    "price": price,
                    "preClose": prev_close,
                    "volume": int(getattr(day, "volume", 0) or 0),
                    "changeRatio": change_pct / 100,
                    "change_pct": change_pct,
                })

            losers.sort(key=lambda x: x["change_pct"])
            return losers[:count]

        except Exception:
            logger.exception("Polygon losers error")
            return []

    def get_cached_quote(self, symbol: str) -> Quote | None:
        return self._quote_cache.get(symbol.upper())
