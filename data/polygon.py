"""Polygon.io market data connector — Starter tier ($29/mo).

Uses polygon-api-client SDK with snapshot endpoints for real-time data:
  - get_snapshot_ticker(symbol)    — real-time quote + bid/ask + day OHLCV
  - get_snapshot_all("stocks")     — all tickers snapshot (batch + scanner)
  - get_previous_close_agg(ticker) — fallback for prev close

Starter tier: 100 API calls/minute. Unlimited snapshots.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta

from core.interfaces import MarketDataInterface
from core.models import Quote

logger = logging.getLogger(__name__)

# Starter tier: 100/min → 0.6s between calls. Leave headroom.
_MIN_CALL_INTERVAL = float(os.getenv("POLYGON_RATE_LIMIT_SECONDS", "0.5"))


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
        self._cache_ttl: float = float(os.getenv("POLYGON_CACHE_TTL", "3.0"))
        # Snapshot-all cache for scanner/batch
        self._snapshot_all_cache: dict[str, object] = {}
        self._snapshot_all_ts: float = 0.0
        self._snapshot_all_ttl: float = 30.0  # Refresh all-snapshot every 30s
        logger.info("Polygon.io market data initialized (starter tier — real-time snapshots)")

    async def _rate_limit(self):
        """Enforce rate limiting between API calls."""
        elapsed = time.time() - self._last_api_call
        if elapsed < _MIN_CALL_INTERVAL:
            await asyncio.sleep(_MIN_CALL_INTERVAL - elapsed)
        self._last_api_call = time.time()

    def _is_cache_fresh(self, symbol: str) -> bool:
        ts = self._cache_timestamps.get(symbol, 0)
        return (time.time() - ts) < self._cache_ttl

    def _snapshot_to_quote(self, snap, symbol: str) -> Quote:
        """Convert a Polygon snapshot object to a Quote."""
        day = snap.day or {}
        prev_day = snap.prev_day or {}
        last_quote = snap.last_quote or {}
        last_trade = snap.last_trade or {}

        # Price priority: last_trade > day.close > prev_day.close
        price = float(getattr(last_trade, "price", 0) or 0)
        day_close = float(getattr(day, "close", 0) or 0)
        prev_close = float(getattr(prev_day, "close", 0) or 0)

        # Use Polygon's todays_change_percent if available (reliable in premarket)
        change_pct = float(getattr(snap, "todays_change_percent", 0) or 0)

        if price <= 0:
            price = day_close if day_close > 0 else prev_close
        # If price fell back to prev_close but there's a known change, compute real price
        if price == prev_close and change_pct != 0 and prev_close > 0:
            price = round(prev_close * (1 + change_pct / 100), 4)

        if change_pct == 0 and prev_close > 0 and price > 0 and price != prev_close:
            change_pct = ((price - prev_close) / prev_close) * 100

        # Bid/ask: fall back to price if empty (after hours)
        bid = float(getattr(last_quote, "bid_price", 0) or 0)
        ask = float(getattr(last_quote, "ask_price", 0) or 0)
        if bid <= 0 and price > 0:
            bid = price * 0.999  # synthetic tight spread
        if ask <= 0 and price > 0:
            ask = price * 1.001

        return Quote(
            symbol=symbol,
            price=price,
            bid=round(bid, 2),
            ask=round(ask, 2),
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
                if symbol in self._quote_cache:
                    return self._quote_cache[symbol]
                return Quote(symbol=symbol)

            quote = self._snapshot_to_quote(snapshot, symbol)

            # Don't overwrite a cached quote that has better data
            existing = self._quote_cache.get(symbol)
            if existing and existing.change_pct != 0 and quote.change_pct == 0:
                # Keep existing change_pct/price from gainers direction cache
                quote = Quote(
                    symbol=symbol,
                    price=existing.price if existing.price > 0 else quote.price,
                    bid=quote.bid if quote.bid != quote.price * 0.999 else existing.bid or quote.bid,
                    ask=quote.ask if quote.ask != quote.price * 1.001 else existing.ask or quote.ask,
                    bid_size=quote.bid_size or existing.bid_size,
                    ask_size=quote.ask_size or existing.ask_size,
                    volume=quote.volume or existing.volume,
                    change_pct=existing.change_pct,
                    prev_close=quote.prev_close or existing.prev_close,
                    high=quote.high or existing.high,
                    low=quote.low or existing.low,
                    open=quote.open or existing.open,
                    timestamp=quote.timestamp or existing.timestamp,
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

    async def _refresh_snapshot_all(self):
        """Refresh the all-tickers snapshot cache if stale."""
        if (time.time() - self._snapshot_all_ts) < self._snapshot_all_ttl:
            return

        try:
            await self._rate_limit()
            snapshots = await asyncio.to_thread(
                self._client.get_snapshot_all, "stocks"
            )

            if not snapshots:
                return

            snap_map = {}
            for snap in snapshots:
                ticker = getattr(snap, "ticker", "")
                if ticker:
                    snap_map[ticker.upper()] = snap

            self._snapshot_all_cache = snap_map
            self._snapshot_all_ts = time.time()

            # Also update individual quote caches — but don't overwrite
            # good data with empty snapshot_all data (common in premarket)
            now = time.time()
            for sym, snap in snap_map.items():
                quote = self._snapshot_to_quote(snap, sym)
                if quote.price <= 0:
                    continue  # Don't clobber existing cache with empty data
                existing = self._quote_cache.get(sym)
                if existing and existing.price > 0 and existing.change_pct != 0 and quote.change_pct == 0:
                    continue  # Keep existing quote that has change data
                self._quote_cache[sym] = quote
                self._cache_timestamps[sym] = now

            logger.debug("Snapshot-all refreshed: %d tickers", len(snap_map))

        except Exception:
            logger.exception("Polygon snapshot-all error")

    async def get_quotes_batch(self, symbols: list[str]) -> dict[str, Quote]:
        """Get quotes for a list of symbols. Uses individual snapshots for
        small lists (more reliable in premarket), snapshot_all for large lists."""
        results = {}

        if len(symbols) <= 20:
            # Small list: fetch individually for reliable premarket data
            for symbol in symbols:
                symbol = symbol.upper()
                try:
                    results[symbol] = await self.get_quote(symbol)
                except Exception:
                    results[symbol] = Quote(symbol=symbol)
        else:
            # Large list: use snapshot_all for efficiency
            await self._refresh_snapshot_all()
            for symbol in symbols:
                symbol = symbol.upper()
                if symbol in self._quote_cache:
                    results[symbol] = self._quote_cache[symbol]
                else:
                    try:
                        results[symbol] = await self.get_quote(symbol)
                    except Exception:
                        results[symbol] = Quote(symbol=symbol)

        return results

    async def get_premarket_gainers(self, count: int = 50) -> list[dict]:
        """Get real-time gainers using Polygon's gainers direction endpoint,
        with snapshot_all as fallback.

        Returns normalized format: [{symbol, price, prev_close, volume, change_pct, ...}]
        """
        # Primary: use the dedicated gainers endpoint (works reliably in premarket)
        gainers = await self._get_gainers_direction(count)
        if gainers:
            return gainers

        # Fallback: scan snapshot_all cache
        await self._refresh_snapshot_all()

        gainers = []
        for sym, snap in self._snapshot_all_cache.items():
            day = snap.day or {}
            prev_day = snap.prev_day or {}
            last_trade = snap.last_trade or {}

            price = float(getattr(last_trade, "price", 0) or 0)
            if price <= 0:
                price = float(getattr(day, "close", 0) or 0)
            prev_close = float(getattr(prev_day, "close", 0) or 0)
            volume = int(getattr(day, "volume", 0) or 0)

            if prev_close <= 0 or price <= 0:
                continue

            change_pct = ((price - prev_close) / prev_close) * 100
            if change_pct <= 0:
                continue

            gainers.append({
                "ticker": {"symbol": sym},
                "symbol": sym,
                "close": price,
                "price": price,
                "preClose": prev_close,
                "volume": volume,
                "changeRatio": change_pct / 100,
                "change_pct": change_pct,
                "avgVolume": int(getattr(prev_day, "volume", 0) or 0),
                "outstandingShares": 0,
            })

        gainers.sort(key=lambda x: x["change_pct"], reverse=True)
        return gainers[:count]

    async def _get_gainers_direction(self, count: int = 50) -> list[dict]:
        """Fetch gainers using Polygon's snapshot direction endpoint."""
        try:
            await self._rate_limit()
            snaps = await asyncio.to_thread(
                self._client.get_snapshot_direction, "stocks", "gainers"
            )

            if not snaps:
                return []

            gainers = []
            now = time.time()
            for snap in snaps:
                ticker = getattr(snap, "ticker", "")
                if not ticker:
                    continue

                prev_day = snap.prev_day or {}
                day = snap.day or {}
                last_trade = snap.last_trade or {}

                prev_close = float(getattr(prev_day, "close", 0) or 0)
                change_pct = float(getattr(snap, "todays_change_percent", 0) or 0)

                # Compute price from prev_close + change, or from trade/day data
                price = float(getattr(last_trade, "price", 0) or 0)
                if price <= 0:
                    price = float(getattr(day, "close", 0) or 0)
                if price <= 0 and prev_close > 0 and change_pct != 0:
                    price = prev_close * (1 + change_pct / 100)

                if prev_close <= 0 or price <= 0 or change_pct <= 0:
                    continue

                volume = int(getattr(day, "volume", 0) or 0)
                avg_volume = int(getattr(prev_day, "volume", 0) or 0)

                gainers.append({
                    "ticker": {"symbol": ticker},
                    "symbol": ticker,
                    "close": round(price, 4),
                    "price": round(price, 4),
                    "preClose": prev_close,
                    "volume": volume,
                    "changeRatio": change_pct / 100,
                    "change_pct": round(change_pct, 2),
                    "avgVolume": avg_volume,
                    "outstandingShares": 0,
                })

                # Also cache the quote
                quote = Quote(
                    symbol=ticker,
                    price=round(price, 4),
                    prev_close=prev_close,
                    volume=volume,
                    change_pct=round(change_pct, 2),
                )
                self._quote_cache[ticker.upper()] = quote
                self._cache_timestamps[ticker.upper()] = now

            gainers.sort(key=lambda x: x["change_pct"], reverse=True)
            logger.info("Gainers direction: %d candidates", len(gainers))
            return gainers[:count]

        except Exception:
            logger.exception("Polygon gainers direction error")
            return []

    async def get_premarket_losers(self, count: int = 50) -> list[dict]:
        """Get real-time losers via snapshot_all."""
        await self._refresh_snapshot_all()

        losers = []
        for sym, snap in self._snapshot_all_cache.items():
            day = snap.day or {}
            prev_day = snap.prev_day or {}
            last_trade = snap.last_trade or {}

            price = float(getattr(last_trade, "price", 0) or 0)
            if price <= 0:
                price = float(getattr(day, "close", 0) or 0)
            prev_close = float(getattr(prev_day, "close", 0) or 0)

            if prev_close <= 0 or price <= 0:
                continue

            change_pct = ((price - prev_close) / prev_close) * 100
            if change_pct >= 0:
                continue

            losers.append({
                "ticker": {"symbol": sym},
                "symbol": sym,
                "close": price,
                "price": price,
                "preClose": prev_close,
                "volume": int(getattr(day, "volume", 0) or 0),
                "changeRatio": change_pct / 100,
                "change_pct": change_pct,
            })

        losers.sort(key=lambda x: x["change_pct"])
        return losers[:count]

    def get_cached_quote(self, symbol: str) -> Quote | None:
        return self._quote_cache.get(symbol.upper())
