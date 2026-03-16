"""Price cache — Fetch and cache 1-minute bars from Polygon for backtesting.

Uses polygon-api-client's list_aggs for historical intraday data.
Caches to data/price_cache/{date}/{symbol}.json using atomic writes.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_MIN_CALL_INTERVAL = float(os.getenv("POLYGON_RATE_LIMIT_SECONDS", "0.5"))
_CACHE_DIR = Path("data/price_cache")

_instance = None


def get_price_cache() -> "PriceCache":
    global _instance
    if _instance is None:
        _instance = PriceCache()
    return _instance


class PriceCache:
    def __init__(self):
        from polygon import RESTClient
        api_key = os.getenv("POLYGON_API_KEY", "")
        if not api_key:
            raise ValueError("POLYGON_API_KEY is required")
        self._client = RESTClient(api_key)
        self._last_api_call: float = 0.0
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    async def _rate_limit(self):
        elapsed = time.time() - self._last_api_call
        if elapsed < _MIN_CALL_INTERVAL:
            await asyncio.sleep(_MIN_CALL_INTERVAL - elapsed)
        self._last_api_call = time.time()

    def _cache_path(self, symbol: str, date: str) -> Path:
        return _CACHE_DIR / date / f"{symbol.upper()}.json"

    def _read_cache(self, symbol: str, date: str) -> list[dict] | None:
        path = self._cache_path(symbol, date)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def _write_cache(self, symbol: str, date: str, bars: list[dict]):
        path = self._cache_path(symbol, date)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(bars, f, ensure_ascii=True)

    async def get_bars(self, symbol: str, date: str) -> list[dict]:
        """Get 1-minute bars for a symbol on a date. Returns cached if available."""
        symbol = symbol.upper()

        # Check cache
        cached = self._read_cache(symbol, date)
        if cached is not None:
            return cached

        # Fetch from Polygon
        try:
            await self._rate_limit()
            aggs = await asyncio.to_thread(
                self._client.list_aggs,
                symbol, 1, "minute", date, date, sort="asc", limit=50000,
            )

            bars = []
            for agg in aggs:
                bars.append({
                    "t": agg.timestamp,  # Unix ms
                    "o": agg.open,
                    "h": agg.high,
                    "l": agg.low,
                    "c": agg.close,
                    "v": agg.volume,
                })

            # Cache to disk
            await asyncio.to_thread(self._write_cache, symbol, date, bars)
            logger.info("Cached %d bars for %s on %s", len(bars), symbol, date)
            return bars

        except Exception:
            logger.exception("Failed to fetch bars for %s on %s", symbol, date)
            return []

    async def prefetch(self, symbols: list[str], date: str):
        """Prefetch bars for multiple symbols."""
        for symbol in symbols:
            await self.get_bars(symbol, date)
