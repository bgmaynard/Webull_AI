"""Replay Market Data — Feeds stored price cache bars for backtesting.

Reads minute bars from data/price_cache/{date}/*.json and replays them
in chronological order. Used with MARKET_DATA_PROVIDER=replay.

Usage:
    MARKET_DATA_PROVIDER=replay REPLAY_DATE=2026-03-12 python webull_trading_api.py
"""

import json
import logging
import time
from pathlib import Path

from core.models import Quote

logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/price_cache")


class ReplayMarketData:
    """Replays stored minute bars from price cache for backtesting."""

    def __init__(self, date: str | None = None):
        self._date = date
        self._bars: dict[str, list[dict]] = {}  # symbol -> sorted bars
        self._bar_index: dict[str, int] = {}    # symbol -> current index
        self._quote_cache: dict[str, Quote] = {}
        self._replay_speed: float = 0.0  # 0 = instant (no delay between bars)
        self._start_time: float = 0.0
        self._sim_time: float = 0.0

        if date:
            self._load_date(date)

    def _load_date(self, date: str):
        """Load all price cache files for a given date."""
        cache_path = CACHE_DIR / date
        if not cache_path.exists():
            logger.error("No price cache for %s at %s", date, cache_path)
            return

        for f in cache_path.glob("*.json"):
            symbol = f.stem
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    bars = json.load(fh)
                if bars:
                    # Sort by timestamp
                    bars.sort(key=lambda b: b["t"])
                    self._bars[symbol] = bars
                    self._bar_index[symbol] = 0
            except Exception:
                logger.exception("Failed to load %s", f)

        if self._bars:
            # Set sim start time to earliest bar
            earliest = min(b[0]["t"] for b in self._bars.values()) / 1000
            self._sim_time = earliest
            self._start_time = time.time()
            logger.info("Replay loaded: %d symbols, %d total bars for %s",
                        len(self._bars),
                        sum(len(b) for b in self._bars.values()),
                        date)

    def set_replay_date(self, date: str):
        """Switch to a different date's data."""
        self._bars.clear()
        self._bar_index.clear()
        self._quote_cache.clear()
        self._date = date
        self._load_date(date)

    def set_replay_speed(self, speed: float):
        """Set replay speed multiplier. 0=instant, 1=realtime, 10=10x."""
        self._replay_speed = speed

    def advance_all(self) -> int:
        """Advance all symbols by one bar. Returns count of symbols advanced."""
        advanced = 0
        for symbol in list(self._bars.keys()):
            idx = self._bar_index.get(symbol, 0)
            bars = self._bars[symbol]
            if idx < len(bars):
                bar = bars[idx]
                self._quote_cache[symbol] = self._bar_to_quote(symbol, bar)
                self._bar_index[symbol] = idx + 1
                advanced += 1
        return advanced

    def advance_to_time(self, timestamp_ms: int) -> int:
        """Advance all symbols up to a given timestamp. Returns bars consumed."""
        consumed = 0
        for symbol in list(self._bars.keys()):
            bars = self._bars[symbol]
            idx = self._bar_index.get(symbol, 0)
            while idx < len(bars) and bars[idx]["t"] <= timestamp_ms:
                self._quote_cache[symbol] = self._bar_to_quote(symbol, bars[idx])
                idx += 1
                consumed += 1
            self._bar_index[symbol] = idx
        return consumed

    def is_exhausted(self) -> bool:
        """True if all bars for all symbols have been consumed."""
        return all(
            self._bar_index.get(s, 0) >= len(bars)
            for s, bars in self._bars.items()
        )

    def get_replay_progress(self) -> dict:
        """Get replay status."""
        total = sum(len(b) for b in self._bars.values())
        consumed = sum(self._bar_index.get(s, 0) for s in self._bars)
        return {
            "date": self._date,
            "symbols": len(self._bars),
            "total_bars": total,
            "consumed_bars": consumed,
            "progress_pct": round(consumed / total * 100, 1) if total > 0 else 0,
            "exhausted": self.is_exhausted(),
        }

    def _bar_to_quote(self, symbol: str, bar: dict) -> Quote:
        """Convert a minute bar to a Quote object."""
        price = bar.get("c", bar.get("o", 0))
        high = bar.get("h", price)
        low = bar.get("l", price)
        # Estimate bid/ask from high/low
        spread = max(0.01, (high - low) * 0.1)
        return Quote(
            symbol=symbol,
            price=round(price, 2),
            bid=round(price - spread / 2, 2),
            ask=round(price + spread / 2, 2),
            bid_size=1000,
            ask_size=1000,
            volume=bar.get("v", 0),
            change_pct=0.0,  # Calculated from prev_close if available
            high=round(high, 2),
            low=round(low, 2),
            open=round(bar.get("o", price), 2),
            timestamp=str(bar.get("t", "")),
        )

    # --- MarketDataInterface implementation ---

    async def get_quote(self, symbol: str) -> Quote:
        """Get next bar for symbol (advances index)."""
        symbol = symbol.upper()
        bars = self._bars.get(symbol)
        if not bars:
            return Quote(symbol=symbol)

        idx = self._bar_index.get(symbol, 0)
        if idx < len(bars):
            quote = self._bar_to_quote(symbol, bars[idx])
            self._bar_index[symbol] = idx + 1
            self._quote_cache[symbol] = quote
            return quote

        # Exhausted — return last known quote
        cached = self._quote_cache.get(symbol)
        return cached if cached else Quote(symbol=symbol)

    async def get_quotes_batch(self, symbols: list[str]) -> dict[str, Quote]:
        results = {}
        for s in symbols:
            results[s] = await self.get_quote(s)
        return results

    async def get_premarket_gainers(self, count: int = 50) -> list[dict]:
        """Return symbols with price cache data as synthetic gainers."""
        gainers = []
        for symbol, bars in self._bars.items():
            if not bars:
                continue
            first = bars[0]
            last = bars[-1] if len(bars) > 1 else first
            change = ((last["c"] - first["o"]) / first["o"]) if first["o"] > 0 else 0
            gainers.append({
                "ticker": {"symbol": symbol},
                "close": last["c"],
                "preClose": first["o"],
                "volume": sum(b.get("v", 0) for b in bars),
                "changeRatio": round(change, 4),
                "avgVolume": 1_000_000,
                "outstandingShares": 10_000_000,
            })
        gainers.sort(key=lambda x: x["changeRatio"], reverse=True)
        return gainers[:count]

    async def get_premarket_losers(self, count: int = 50) -> list[dict]:
        return []

    def get_cached_quote(self, symbol: str) -> Quote | None:
        return self._quote_cache.get(symbol.upper())

    def get_available_dates(self) -> list[str]:
        """List dates with stored price cache data."""
        if not CACHE_DIR.exists():
            return []
        return sorted(d.name for d in CACHE_DIR.iterdir() if d.is_dir())

    def get_available_symbols(self, date: str | None = None) -> list[str]:
        """List symbols with data for a date."""
        d = date or self._date
        if not d:
            return []
        path = CACHE_DIR / d
        if not path.exists():
            return []
        return sorted(f.stem for f in path.glob("*.json"))
