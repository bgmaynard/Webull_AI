"""Worklist store — Capped watchlist with scoring and displacement.

Hard cap of N symbols. New symbols must beat the lowest scorer to enter.
Rescore periodically with time-decay.
"""

import logging
import time
from dataclasses import dataclass, field

from worklist.scoring import ScoringInput, score

logger = logging.getLogger(__name__)


@dataclass
class WorklistEntry:
    symbol: str
    score: float = 0.0
    scoring_input: ScoringInput | None = None
    added_at: float = field(default_factory=time.time)
    last_scored_at: float = field(default_factory=time.time)
    source: str = ""  # scanner, manual, etc.

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "score": round(self.score, 1),
            "added_at": self.added_at,
            "last_scored_at": self.last_scored_at,
            "source": self.source,
            "age_seconds": round(time.time() - self.added_at, 0),
        }


class WorklistStore:
    def __init__(self, max_size: int = 15):
        self.max_size = max_size
        self._entries: dict[str, WorklistEntry] = {}

    @property
    def count(self) -> int:
        return len(self._entries)

    @property
    def is_full(self) -> bool:
        return self.count >= self.max_size

    def get(self, symbol: str) -> WorklistEntry | None:
        return self._entries.get(symbol)

    def get_all(self) -> list[WorklistEntry]:
        """Get all entries sorted by score descending."""
        return sorted(self._entries.values(), key=lambda e: e.score, reverse=True)

    def get_symbols(self) -> list[str]:
        return [e.symbol for e in self.get_all()]

    def get_lowest(self) -> WorklistEntry | None:
        """Get the lowest-scored entry."""
        if not self._entries:
            return None
        return min(self._entries.values(), key=lambda e: e.score)

    def add(self, symbol: str, scoring_input: ScoringInput, source: str = "scanner") -> bool:
        """Add a symbol to the worklist. Uses displacement if full.

        Returns True if symbol was added (or updated).
        """
        symbol = symbol.upper()
        new_score = score(scoring_input)

        # Already in worklist — update score
        if symbol in self._entries:
            self._entries[symbol].score = new_score
            self._entries[symbol].scoring_input = scoring_input
            self._entries[symbol].last_scored_at = time.time()
            logger.debug("Updated %s score to %.1f", symbol, new_score)
            return True

        # Worklist not full — add directly
        if not self.is_full:
            self._entries[symbol] = WorklistEntry(
                symbol=symbol,
                score=new_score,
                scoring_input=scoring_input,
                source=source,
            )
            logger.info("Added %s to worklist (score=%.1f, count=%d/%d)",
                        symbol, new_score, self.count, self.max_size)
            return True

        # Worklist full — displacement: must beat lowest
        lowest = self.get_lowest()
        if lowest and new_score > lowest.score:
            logger.info("Displaced %s (%.1f) with %s (%.1f)",
                        lowest.symbol, lowest.score, symbol, new_score)
            del self._entries[lowest.symbol]
            self._entries[symbol] = WorklistEntry(
                symbol=symbol,
                score=new_score,
                scoring_input=scoring_input,
                source=source,
            )
            return True

        logger.debug("Rejected %s (score=%.1f) — below lowest %.1f",
                      symbol, new_score, lowest.score if lowest else 0)
        return False

    def remove(self, symbol: str) -> bool:
        symbol = symbol.upper()
        if symbol in self._entries:
            del self._entries[symbol]
            logger.info("Removed %s from worklist", symbol)
            return True
        return False

    def rescore_all(self):
        """Rescore all entries (applies time decay via scoring engine)."""
        for entry in self._entries.values():
            if entry.scoring_input:
                entry.score = score(entry.scoring_input)
                entry.last_scored_at = time.time()

    def clear(self):
        self._entries.clear()

    def to_list(self) -> list[dict]:
        return [e.to_dict() for e in self.get_all()]


_instance: WorklistStore | None = None


def get_worklist_store() -> WorklistStore:
    global _instance
    if _instance is None:
        _instance = WorklistStore(max_size=15)
    return _instance
