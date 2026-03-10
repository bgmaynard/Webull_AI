"""Momentum State Machine — 8-state FSM for trade lifecycle.

States: IDLE -> CANDIDATE -> IGNITING -> GATED -> IN_POSITION -> MONITORING -> EXITING -> COOLDOWN
Each symbol gets its own FSM instance with ownership tracking.
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

logger = logging.getLogger(__name__)


class MomentumState(str, Enum):
    IDLE = "IDLE"
    CANDIDATE = "CANDIDATE"
    IGNITING = "IGNITING"
    GATED = "GATED"
    IN_POSITION = "IN_POSITION"
    MONITORING = "MONITORING"
    EXITING = "EXITING"
    COOLDOWN = "COOLDOWN"


# Valid state transitions
VALID_TRANSITIONS: dict[MomentumState, list[MomentumState]] = {
    MomentumState.IDLE: [MomentumState.CANDIDATE],
    MomentumState.CANDIDATE: [MomentumState.IGNITING, MomentumState.IDLE],
    MomentumState.IGNITING: [MomentumState.GATED, MomentumState.IDLE],
    MomentumState.GATED: [MomentumState.IN_POSITION, MomentumState.IDLE],
    MomentumState.IN_POSITION: [MomentumState.MONITORING, MomentumState.EXITING],
    MomentumState.MONITORING: [MomentumState.EXITING, MomentumState.IN_POSITION],
    MomentumState.EXITING: [MomentumState.COOLDOWN],
    MomentumState.COOLDOWN: [MomentumState.IDLE],
}

# Momentum thresholds — tuned for partial data coverage
# (no real news feed or precise RVOL yet, so max realistic score ~40-50)
MOMENTUM_THRESHOLDS = {
    "candidate_score": 15,   # Min score to become CANDIDATE
    "igniting_score": 22,    # Min score to start IGNITING
    "gated_score": 28,       # Min score to pass through gate
}

# Sim-mode thresholds (same for now, can diverge later)
SIM_MOMENTUM_THRESHOLDS = {
    "candidate_score": 15,
    "igniting_score": 22,
    "gated_score": 28,
}


@dataclass
class MomentumEvent:
    symbol: str
    from_state: MomentumState
    to_state: MomentumState
    timestamp: float
    reason: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class SymbolMomentum:
    """Per-symbol momentum state tracker."""
    symbol: str
    state: MomentumState = MomentumState.IDLE
    score: float = 0.0
    entered_state_at: float = field(default_factory=time.time)
    entry_price: float = 0.0
    high_since_entry: float = 0.0
    position_qty: int = 0
    hard_stop_count: int = 0
    last_update: float = field(default_factory=time.time)

    @property
    def time_in_state(self) -> float:
        return time.time() - self.entered_state_at

    @property
    def is_active(self) -> bool:
        return self.state not in (MomentumState.IDLE, MomentumState.COOLDOWN)


class MomentumEngine:
    """Manages momentum FSMs for all tracked symbols."""

    def __init__(self, sim_mode: bool = False):
        self._symbols: dict[str, SymbolMomentum] = {}
        self._event_listeners: list[Callable[[MomentumEvent], None]] = []
        self._cooldown_seconds: float = 60.0
        self._thresholds = SIM_MOMENTUM_THRESHOLDS if sim_mode else MOMENTUM_THRESHOLDS

    def on_event(self, listener: Callable[[MomentumEvent], None]):
        """Register an event listener for state transitions."""
        self._event_listeners.append(listener)

    def get_symbol(self, symbol: str) -> SymbolMomentum:
        """Get or create a symbol's momentum state."""
        if symbol not in self._symbols:
            self._symbols[symbol] = SymbolMomentum(symbol=symbol)
        return self._symbols[symbol]

    def get_all_active(self) -> list[SymbolMomentum]:
        """Get all symbols with active (non-IDLE/COOLDOWN) states."""
        return [s for s in self._symbols.values() if s.is_active]

    def get_all(self) -> dict[str, SymbolMomentum]:
        return dict(self._symbols)

    def transition(self, symbol: str, to_state: MomentumState, reason: str = "", data: dict | None = None) -> bool:
        """Attempt a state transition. Returns True if valid and applied."""
        sm = self.get_symbol(symbol)
        from_state = sm.state

        if to_state not in VALID_TRANSITIONS.get(from_state, []):
            logger.warning(
                "Invalid transition for %s: %s -> %s (reason: %s)",
                symbol, from_state.value, to_state.value, reason
            )
            return False

        sm.state = to_state
        sm.entered_state_at = time.time()
        sm.last_update = time.time()

        event = MomentumEvent(
            symbol=symbol,
            from_state=from_state,
            to_state=to_state,
            timestamp=time.time(),
            reason=reason,
            data=data or {},
        )

        logger.info(
            "FSM %s: %s -> %s (%s)",
            symbol, from_state.value, to_state.value, reason
        )

        for listener in self._event_listeners:
            try:
                listener(event)
            except Exception:
                logger.exception("Event listener error")

        return True

    def update_score(self, symbol: str, score: float):
        """Update a symbol's momentum score and evaluate transitions."""
        sm = self.get_symbol(symbol)
        sm.score = score
        sm.last_update = time.time()

        # Auto-transitions based on score thresholds
        thresholds = self._thresholds

        if sm.state == MomentumState.IDLE and score >= thresholds["candidate_score"]:
            self.transition(symbol, MomentumState.CANDIDATE, f"score={score:.1f} >= {thresholds['candidate_score']}")

        elif sm.state == MomentumState.CANDIDATE:
            if score >= thresholds["igniting_score"]:
                self.transition(symbol, MomentumState.IGNITING, f"score={score:.1f} >= {thresholds['igniting_score']}")
            elif score < thresholds["candidate_score"]:
                self.transition(symbol, MomentumState.IDLE, f"score={score:.1f} dropped below threshold")

        elif sm.state == MomentumState.IGNITING:
            if score >= thresholds["gated_score"]:
                self.transition(symbol, MomentumState.GATED, f"score={score:.1f} >= {thresholds['gated_score']}")
            elif score < thresholds["candidate_score"]:
                self.transition(symbol, MomentumState.IDLE, f"score={score:.1f} momentum faded")

    def mark_position_entered(self, symbol: str, entry_price: float, qty: int):
        """Called when a fill is received — transition to IN_POSITION."""
        sm = self.get_symbol(symbol)
        sm.entry_price = entry_price
        sm.high_since_entry = entry_price
        sm.position_qty = qty
        if sm.state == MomentumState.GATED:
            self.transition(symbol, MomentumState.IN_POSITION, f"filled @ {entry_price}")

    def update_price(self, symbol: str, current_price: float):
        """Update tracking price for position monitoring."""
        sm = self.get_symbol(symbol)
        if sm.state in (MomentumState.IN_POSITION, MomentumState.MONITORING):
            if current_price > sm.high_since_entry:
                sm.high_since_entry = current_price
            sm.last_update = time.time()

    def mark_exiting(self, symbol: str, reason: str):
        """Initiate exit for a position."""
        sm = self.get_symbol(symbol)
        if sm.state in (MomentumState.IN_POSITION, MomentumState.MONITORING):
            self.transition(symbol, MomentumState.EXITING, reason)

    def mark_exited(self, symbol: str):
        """Position fully closed — move to COOLDOWN."""
        sm = self.get_symbol(symbol)
        if sm.state == MomentumState.EXITING:
            self.transition(symbol, MomentumState.COOLDOWN, "position closed")

    def mark_hard_stop(self, symbol: str):
        """Record a hard stop hit for circuit breaker tracking."""
        sm = self.get_symbol(symbol)
        sm.hard_stop_count += 1

    def check_cooldowns(self):
        """Move symbols from COOLDOWN back to IDLE if cooldown expired."""
        for sm in self._symbols.values():
            if sm.state == MomentumState.COOLDOWN and sm.time_in_state >= self._cooldown_seconds:
                self.transition(sm.symbol, MomentumState.IDLE, "cooldown expired")

    def reset_symbol(self, symbol: str):
        """Force-reset a symbol to IDLE."""
        if symbol in self._symbols:
            del self._symbols[symbol]

    def reset_all(self):
        """Force-reset all symbols."""
        self._symbols.clear()


_instance: MomentumEngine | None = None


def get_momentum_engine(sim_mode: bool = False) -> MomentumEngine:
    global _instance
    if _instance is None:
        _instance = MomentumEngine(sim_mode=sim_mode)
    return _instance
