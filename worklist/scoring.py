"""Scoring engine — Priority scoring for worklist symbols (0-100).

Weighted composite score with time decay for stale data.
"""

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ScoringConfig:
    weight_gap: float = 0.35
    weight_volume: float = 0.25
    weight_rvol: float = 0.15
    weight_news: float = 0.15
    weight_scanner_score: float = 0.10
    # Time decay: 2% per minute after stale_threshold
    stale_threshold_seconds: float = 600.0  # 10 minutes
    decay_per_minute: float = 2.0

    def validate(self) -> bool:
        total = (self.weight_gap + self.weight_volume + self.weight_rvol
                 + self.weight_news + self.weight_scanner_score)
        return abs(total - 1.0) < 0.01


@dataclass
class ScoringInput:
    symbol: str
    gap_pct: float = 0.0
    volume: int = 0
    rvol: float = 0.0
    news_score: float = 0.0       # 0-100
    scanner_score: float = 0.0    # 0-100
    last_data_time: float = 0.0   # epoch timestamp of data freshness

    # Normalization caps
    gap_cap: float = 200.0        # Cap gap% at 200% for scoring
    volume_cap: int = 10_000_000  # Cap volume at 10M
    rvol_cap: float = 20.0        # Cap RVOL at 20x


_config = ScoringConfig()


def get_scoring_config() -> ScoringConfig:
    return _config


def set_scoring_config(config: ScoringConfig):
    global _config
    _config = config


def score(inp: ScoringInput, config: ScoringConfig | None = None) -> float:
    """Calculate composite score (0-100) for a symbol."""
    cfg = config or _config

    # Normalize each component to 0-100
    gap_norm = min(inp.gap_pct / inp.gap_cap, 1.0) * 100
    vol_norm = min(inp.volume / inp.volume_cap, 1.0) * 100
    rvol_norm = min(inp.rvol / inp.rvol_cap, 1.0) * 100
    news_norm = min(max(inp.news_score, 0), 100)
    scanner_norm = min(max(inp.scanner_score, 0), 100)

    # Weighted composite
    raw_score = (
        cfg.weight_gap * gap_norm
        + cfg.weight_volume * vol_norm
        + cfg.weight_rvol * rvol_norm
        + cfg.weight_news * news_norm
        + cfg.weight_scanner_score * scanner_norm
    )

    # Time decay
    if inp.last_data_time > 0:
        age_seconds = time.time() - inp.last_data_time
        if age_seconds > cfg.stale_threshold_seconds:
            stale_minutes = (age_seconds - cfg.stale_threshold_seconds) / 60
            decay = stale_minutes * cfg.decay_per_minute
            raw_score = max(0, raw_score - decay)

    return round(min(100, max(0, raw_score)), 1)


def score_with_breakdown(inp: ScoringInput, config: ScoringConfig | None = None) -> tuple[float, dict]:
    """Calculate composite score AND return individual normalized components.

    Returns (total_score, breakdown_dict) where breakdown_dict contains
    each component's normalized 0-100 value before weighting.
    """
    cfg = config or _config

    # Normalize each component to 0-100
    gap_norm = round(min(inp.gap_pct / inp.gap_cap, 1.0) * 100, 1)
    vol_norm = round(min(inp.volume / inp.volume_cap, 1.0) * 100, 1)
    rvol_norm = round(min(inp.rvol / inp.rvol_cap, 1.0) * 100, 1)
    news_norm = round(min(max(inp.news_score, 0), 100), 1)
    scanner_norm = round(min(max(inp.scanner_score, 0), 100), 1)

    # Weighted composite
    raw_score = (
        cfg.weight_gap * gap_norm
        + cfg.weight_volume * vol_norm
        + cfg.weight_rvol * rvol_norm
        + cfg.weight_news * news_norm
        + cfg.weight_scanner_score * scanner_norm
    )

    # Time decay
    if inp.last_data_time > 0:
        age_seconds = time.time() - inp.last_data_time
        if age_seconds > cfg.stale_threshold_seconds:
            stale_minutes = (age_seconds - cfg.stale_threshold_seconds) / 60
            decay = stale_minutes * cfg.decay_per_minute
            raw_score = max(0, raw_score - decay)

    total = round(min(100, max(0, raw_score)), 1)
    breakdown = {
        "gap": gap_norm,
        "volume": vol_norm,
        "rvol": rvol_norm,
        "news": news_norm,
        "scanner": scanner_norm,
    }
    return total, breakdown


def score_batch(inputs: list[ScoringInput], config: ScoringConfig | None = None) -> list[tuple[str, float]]:
    """Score multiple symbols and return sorted (symbol, score) pairs."""
    results = [(inp.symbol, score(inp, config)) for inp in inputs]
    results.sort(key=lambda x: x[1], reverse=True)
    return results
