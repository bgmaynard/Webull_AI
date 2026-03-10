"""Mock market data — Simulates realistic premarket price action.

Generates synthetic quotes with random walks, momentum bursts,
and mean reversion for testing the full scalper pipeline.
"""

import logging
import math
import random
import time
from dataclasses import dataclass, field

from core.models import Quote

logger = logging.getLogger(__name__)

# Simulated universe of premarket movers
SIMULATED_STOCKS = [
    {"symbol": "XMPL", "base_price": 5.50, "gap_pct": 45, "float_m": 8, "vol_base": 800_000},
    {"symbol": "FOMO", "base_price": 3.20, "gap_pct": 80, "float_m": 4, "vol_base": 2_000_000},
    {"symbol": "PUMP", "base_price": 7.80, "gap_pct": 35, "float_m": 12, "vol_base": 600_000},
    {"symbol": "MOMO", "base_price": 12.40, "gap_pct": 55, "float_m": 6, "vol_base": 1_500_000},
    {"symbol": "RUSH", "base_price": 4.10, "gap_pct": 120, "float_m": 2, "vol_base": 3_000_000},
    {"symbol": "BLZE", "base_price": 8.90, "gap_pct": 40, "float_m": 15, "vol_base": 700_000},
    {"symbol": "RKTX", "base_price": 6.25, "gap_pct": 65, "float_m": 5, "vol_base": 1_200_000},
    {"symbol": "NVAX", "base_price": 15.30, "gap_pct": 32, "float_m": 18, "vol_base": 900_000},
    {"symbol": "SQZZ", "base_price": 2.80, "gap_pct": 95, "float_m": 3, "vol_base": 2_500_000},
    {"symbol": "BRKR", "base_price": 9.60, "gap_pct": 50, "float_m": 10, "vol_base": 1_000_000},
]


@dataclass
class SimStock:
    symbol: str
    base_price: float
    current_price: float
    gap_pct: float
    float_millions: float
    volume_base: int
    current_volume: int = 0
    momentum: float = 0.0  # -1 to 1
    volatility: float = 0.02
    last_tick: float = field(default_factory=time.time)
    prev_close: float = 0.0

    def tick(self) -> Quote:
        """Advance price by one tick with random walk + momentum."""
        now = time.time()
        dt = min(now - self.last_tick, 2.0)
        self.last_tick = now

        # Momentum drift + random noise
        self.momentum += random.gauss(0, 0.1) * dt
        self.momentum = max(-1.0, min(1.0, self.momentum))

        # Mean reversion toward base
        reversion = (self.base_price - self.current_price) / self.base_price * 0.01

        # Price change
        noise = random.gauss(0, self.volatility * self.current_price * math.sqrt(dt))
        drift = self.momentum * self.volatility * self.current_price * dt * 0.5
        self.current_price += noise + drift + reversion
        self.current_price = max(0.50, self.current_price)

        # Volume accumulates
        vol_tick = int(self.volume_base * dt * random.uniform(0.5, 2.0) / 60)
        self.current_volume += vol_tick

        # Spread: tighter for higher volume
        spread_pct = random.uniform(0.1, 0.8)
        half_spread = self.current_price * spread_pct / 200

        bid = round(self.current_price - half_spread, 2)
        ask = round(self.current_price + half_spread, 2)

        change_pct = ((self.current_price - self.prev_close) / self.prev_close * 100) if self.prev_close > 0 else self.gap_pct

        return Quote(
            symbol=self.symbol,
            price=round(self.current_price, 2),
            bid=max(0.01, bid),
            ask=max(0.02, ask),
            bid_size=random.randint(100, 5000),
            ask_size=random.randint(100, 5000),
            volume=self.current_volume,
            change_pct=round(change_pct, 2),
            prev_close=round(self.prev_close, 2),
            high=round(self.current_price * 1.02, 2),
            low=round(self.current_price * 0.98, 2),
            open=round(self.base_price, 2),
            timestamp=str(int(now)),
        )


class MockMarketData:
    """Drop-in replacement for WebullMarketData using simulated data."""

    def __init__(self):
        self._stocks: dict[str, SimStock] = {}
        self._quote_cache: dict[str, Quote] = {}
        self._init_universe()

    def _init_universe(self):
        for s in SIMULATED_STOCKS:
            prev_close = s["base_price"] / (1 + s["gap_pct"] / 100)
            self._stocks[s["symbol"]] = SimStock(
                symbol=s["symbol"],
                base_price=s["base_price"],
                current_price=s["base_price"],
                gap_pct=s["gap_pct"],
                float_millions=s["float_m"],
                volume_base=s["vol_base"],
                prev_close=prev_close,
                volatility=0.01 + s["gap_pct"] / 5000,
            )

    async def get_quote(self, symbol: str) -> Quote:
        symbol = symbol.upper()
        if symbol in self._stocks:
            quote = self._stocks[symbol].tick()
            self._quote_cache[symbol] = quote
            return quote
        # Unknown symbol — return a generic simulated quote
        return Quote(
            symbol=symbol,
            price=round(random.uniform(5, 50), 2),
            bid=round(random.uniform(5, 50), 2),
            ask=round(random.uniform(5, 50), 2),
            volume=random.randint(100_000, 5_000_000),
            change_pct=round(random.uniform(-5, 15), 2),
        )

    async def get_quotes_batch(self, symbols: list[str]) -> dict[str, Quote]:
        results = {}
        for s in symbols:
            results[s] = await self.get_quote(s)
        return results

    async def get_premarket_gainers(self, count: int = 50) -> list[dict]:
        """Return simulated premarket gainers."""
        gainers = []
        for sym, stock in self._stocks.items():
            quote = stock.tick()
            self._quote_cache[sym] = quote
            gainers.append({
                "ticker": {"symbol": sym},
                "close": quote.price,
                "preClose": stock.prev_close,
                "volume": quote.volume,
                "changeRatio": stock.gap_pct / 100,
                "avgVolume": stock.volume_base,
                "outstandingShares": stock.float_millions * 1_000_000,
            })
        gainers.sort(key=lambda x: x["changeRatio"], reverse=True)
        return gainers[:count]

    async def get_premarket_losers(self, count: int = 50) -> list[dict]:
        return []

    async def get_ticker_id(self, symbol: str) -> int | None:
        return hash(symbol) % 1_000_000

    def get_cached_quote(self, symbol: str) -> Quote | None:
        return self._quote_cache.get(symbol)

    def get_universe(self) -> list[dict]:
        """Get all simulated stocks info."""
        return [
            {
                "symbol": s.symbol,
                "price": round(s.current_price, 2),
                "gap_pct": s.gap_pct,
                "float_m": s.float_millions,
                "volume": s.current_volume,
                "momentum": round(s.momentum, 3),
            }
            for s in self._stocks.values()
        ]
