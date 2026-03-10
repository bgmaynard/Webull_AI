"""Scrutiny filters — Symbol intake quality gate.

Every symbol must pass all filters before entering the worklist.
Filters: price, volume, RVOL, spread, gap%, float, dollar volume, scanner score.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ScrutinyConfig:
    min_price: float = 2.00
    max_price: float = 20.00
    min_volume: int = 500_000
    min_rvol: float = 3.0
    max_spread_pct: float = 1.0
    min_scanner_score: float = 50.0
    min_dollar_volume: int = 100_000
    max_float_millions: float = 20.0
    min_gap_pct: float = 30.0


@dataclass
class ScrutinyResult:
    passed: bool
    symbol: str
    checks_passed: list[str]
    checks_failed: list[str]
    reason: str = ""

    @property
    def summary(self) -> str:
        if self.passed:
            return f"{self.symbol} PASSED ({len(self.checks_passed)} checks)"
        return f"{self.symbol} REJECTED: {self.reason}"


@dataclass
class SymbolData:
    """Raw symbol data for scrutiny evaluation."""
    symbol: str
    price: float = 0.0
    volume: int = 0
    rvol: float = 0.0
    spread_pct: float = 0.0
    gap_pct: float = 0.0
    float_millions: float = 0.0
    dollar_volume: float = 0.0
    scanner_score: float = 0.0
    prev_close: float = 0.0
    has_news: bool = False
    news_score: float = 0.0


_config = ScrutinyConfig()


def get_scrutiny_config() -> ScrutinyConfig:
    return _config


def set_scrutiny_config(config: ScrutinyConfig):
    global _config
    _config = config


def evaluate(data: SymbolData, config: ScrutinyConfig | None = None) -> ScrutinyResult:
    """Run all scrutiny filters on a symbol. Returns ScrutinyResult."""
    cfg = config or _config
    passed = []
    failed = []

    # 1. Price range
    if data.price < cfg.min_price:
        failed.append("min_price")
        return ScrutinyResult(False, data.symbol, passed, failed,
                              f"Price ${data.price:.2f} < ${cfg.min_price:.2f}")
    if data.price > cfg.max_price:
        failed.append("max_price")
        return ScrutinyResult(False, data.symbol, passed, failed,
                              f"Price ${data.price:.2f} > ${cfg.max_price:.2f}")
    passed.append("price_range")

    # 2. Volume
    if data.volume < cfg.min_volume:
        failed.append("volume")
        return ScrutinyResult(False, data.symbol, passed, failed,
                              f"Volume {data.volume:,} < {cfg.min_volume:,}")
    passed.append("volume")

    # 3. RVOL (relative volume)
    if data.rvol < cfg.min_rvol:
        failed.append("rvol")
        return ScrutinyResult(False, data.symbol, passed, failed,
                              f"RVOL {data.rvol:.1f}x < {cfg.min_rvol:.1f}x")
    passed.append("rvol")

    # 4. Spread
    if data.spread_pct > cfg.max_spread_pct:
        failed.append("spread")
        return ScrutinyResult(False, data.symbol, passed, failed,
                              f"Spread {data.spread_pct:.2f}% > {cfg.max_spread_pct:.2f}%")
    passed.append("spread")

    # 5. Gap %
    if data.gap_pct < cfg.min_gap_pct:
        failed.append("gap")
        return ScrutinyResult(False, data.symbol, passed, failed,
                              f"Gap {data.gap_pct:.1f}% < {cfg.min_gap_pct:.1f}%")
    passed.append("gap")

    # 6. Float
    if data.float_millions > 0 and data.float_millions > cfg.max_float_millions:
        failed.append("float")
        return ScrutinyResult(False, data.symbol, passed, failed,
                              f"Float {data.float_millions:.1f}M > {cfg.max_float_millions:.1f}M")
    passed.append("float")

    # 7. Dollar volume
    if data.dollar_volume < cfg.min_dollar_volume:
        failed.append("dollar_volume")
        return ScrutinyResult(False, data.symbol, passed, failed,
                              f"Dollar vol ${data.dollar_volume:,.0f} < ${cfg.min_dollar_volume:,}")
    passed.append("dollar_volume")

    # 8. Scanner score
    if data.scanner_score < cfg.min_scanner_score:
        failed.append("scanner_score")
        return ScrutinyResult(False, data.symbol, passed, failed,
                              f"Scanner score {data.scanner_score:.0f} < {cfg.min_scanner_score:.0f}")
    passed.append("scanner_score")

    return ScrutinyResult(True, data.symbol, passed, failed)
