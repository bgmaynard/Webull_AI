"""Central Gating — Every trade must pass through here.

Paper mode: fail-open (allow trade on gate error)
Live mode: fail-closed (block trade on gate error)
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

import pytz

logger = logging.getLogger(__name__)

ET = pytz.timezone("US/Eastern")

_instance = None


def get_gating() -> "CentralGating":
    global _instance
    if _instance is None:
        _instance = CentralGating()
    return _instance


@dataclass
class GateResult:
    approved: bool
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def summary(self) -> str:
        if self.approved:
            return f"APPROVED ({len(self.checks_passed)} checks passed)"
        return f"BLOCKED: {self.reason} ({len(self.checks_failed)} failed)"


@dataclass
class CircuitBreakerState:
    hard_stop_count: int = 0
    last_hard_stop_at: float = 0.0
    blocked_until: float = 0.0

    @property
    def is_blocked(self) -> bool:
        return time.time() < self.blocked_until


class CentralGating:
    def __init__(self):
        self.kill_switch: bool = False
        self.account_type: str = "paper"
        self.sim_mode: bool = False  # Bypasses trading phase check
        self.blacklist: set[str] = set()
        self.session_trade_count: int = 0
        self.session_trade_cap: int = 50
        self.max_position_count: int = 3
        self.max_risk_dollars: float = 10.0
        self.max_spread_pct: float = 1.0
        self._circuit_breakers: dict[str, CircuitBreakerState] = {}
        self._hard_stop_limit: int = 3
        self._circuit_breaker_cooldown: float = 30 * 60  # 30 minutes

    @property
    def fail_open(self) -> bool:
        """Paper mode = fail-open, live = fail-closed."""
        return self.account_type == "paper"

    def configure(self, config: dict):
        """Update gating config from scalper config."""
        self.session_trade_cap = config.get("session_trade_cap", self.session_trade_cap)
        self.max_position_count = config.get("max_position_count", self.max_position_count)
        self.max_risk_dollars = config.get("max_risk_dollars_per_trade", self.max_risk_dollars)
        self.max_spread_pct = config.get("max_spread_percent", self.max_spread_pct)
        self._hard_stop_limit = config.get("hard_stop_limit_per_symbol", self._hard_stop_limit)
        cooldown_min = config.get("circuit_breaker_cooldown_minutes", 30)
        self._circuit_breaker_cooldown = cooldown_min * 60

    def evaluate(
        self,
        symbol: str,
        spread_pct: float,
        current_position_count: int,
        risk_dollars: float,
    ) -> GateResult:
        """Run all gate checks. Returns GateResult."""
        passed = []
        failed = []

        # 1. Kill switch
        if self.kill_switch:
            failed.append("kill_switch")
            return GateResult(approved=False, checks_passed=passed, checks_failed=failed, reason="Kill switch active")
        passed.append("kill_switch")

        # 2. Trading phase (bypassed in sim mode)
        if self.sim_mode:
            passed.append("trading_phase")
        else:
            now = datetime.now(ET)
            phase = self._get_phase(now)
            if self.account_type == "paper":
                # Paper/sim: allow trading during all market-active phases (4AM-4PM ET)
                paper_allowed = {"DISCOVERY", "LIVE", "EXIT_ONLY", "SHADOW"}
                if phase not in paper_allowed:
                    failed.append("trading_phase")
                    return GateResult(approved=False, checks_passed=passed, checks_failed=failed,
                                      reason=f"Trading phase is {phase}, paper mode allows {paper_allowed}")
            elif phase != "LIVE":
                failed.append("trading_phase")
                return GateResult(approved=False, checks_passed=passed, checks_failed=failed, reason=f"Trading phase is {phase}, not LIVE")
            passed.append("trading_phase")

        # 3. Symbol not halted (placeholder — Webull doesn't expose halt status directly)
        passed.append("halt_check")

        # 4. Blacklist
        if symbol in self.blacklist:
            failed.append("blacklist")
            return GateResult(approved=False, checks_passed=passed, checks_failed=failed, reason=f"{symbol} is blacklisted")
        passed.append("blacklist")

        # 5. Spread check
        if spread_pct > self.max_spread_pct:
            failed.append("spread")
            return GateResult(approved=False, checks_passed=passed, checks_failed=failed, reason=f"Spread {spread_pct:.2f}% > {self.max_spread_pct}%")
        passed.append("spread")

        # 6. Position limit
        if current_position_count >= self.max_position_count:
            failed.append("position_limit")
            return GateResult(approved=False, checks_passed=passed, checks_failed=failed, reason=f"Position limit {self.max_position_count} reached")
        passed.append("position_limit")

        # 7. Session trade cap
        if self.session_trade_count >= self.session_trade_cap:
            failed.append("session_cap")
            return GateResult(approved=False, checks_passed=passed, checks_failed=failed, reason=f"Session cap {self.session_trade_cap} reached")
        passed.append("session_cap")

        # 8. Circuit breaker
        cb = self._circuit_breakers.get(symbol)
        if cb and cb.is_blocked:
            failed.append("circuit_breaker")
            remaining = int(cb.blocked_until - time.time())
            return GateResult(approved=False, checks_passed=passed, checks_failed=failed,
                              reason=f"Circuit breaker: {symbol} blocked for {remaining}s")
        passed.append("circuit_breaker")

        # 9. Risk dollar cap
        if risk_dollars > self.max_risk_dollars:
            failed.append("risk_cap")
            return GateResult(approved=False, checks_passed=passed, checks_failed=failed,
                              reason=f"Risk ${risk_dollars:.2f} > cap ${self.max_risk_dollars:.2f}")
        passed.append("risk_cap")

        return GateResult(approved=True, checks_passed=passed, checks_failed=failed)

    def record_trade(self):
        """Increment session trade counter."""
        self.session_trade_count += 1

    def record_hard_stop(self, symbol: str):
        """Record a hard stop for circuit breaker tracking."""
        if symbol not in self._circuit_breakers:
            self._circuit_breakers[symbol] = CircuitBreakerState()

        cb = self._circuit_breakers[symbol]
        cb.hard_stop_count += 1
        cb.last_hard_stop_at = time.time()

        if cb.hard_stop_count >= self._hard_stop_limit:
            cb.blocked_until = time.time() + self._circuit_breaker_cooldown
            logger.warning(
                "Circuit breaker TRIGGERED for %s (%d hard stops, blocked for %d min)",
                symbol, cb.hard_stop_count, int(self._circuit_breaker_cooldown / 60)
            )

    def add_blacklist(self, symbol: str):
        self.blacklist.add(symbol.upper())
        logger.info("Blacklisted: %s", symbol)

    def remove_blacklist(self, symbol: str):
        self.blacklist.discard(symbol.upper())
        logger.info("Un-blacklisted: %s", symbol)

    def activate_kill_switch(self):
        self.kill_switch = True
        logger.warning("KILL SWITCH ACTIVATED")

    def deactivate_kill_switch(self):
        self.kill_switch = False
        logger.info("Kill switch deactivated")

    def reset_session(self):
        """Reset session counters (call at start of day)."""
        self.session_trade_count = 0
        self._circuit_breakers.clear()
        logger.info("Session counters reset")

    def get_circuit_breaker_status(self) -> dict[str, dict]:
        """Get all circuit breaker states."""
        result = {}
        for symbol, cb in self._circuit_breakers.items():
            result[symbol] = {
                "hard_stop_count": cb.hard_stop_count,
                "is_blocked": cb.is_blocked,
                "blocked_until": cb.blocked_until,
                "remaining_seconds": max(0, int(cb.blocked_until - time.time())) if cb.is_blocked else 0,
            }
        return result

    def _get_phase(self, now: datetime) -> str:
        hour, minute = now.hour, now.minute
        t = hour * 60 + minute
        if t < 240:
            return "OFFHOURS"
        elif t < 420:
            return "DISCOVERY"
        elif t < 555:
            return "LIVE"
        elif t < 570:
            return "EXIT_ONLY"
        elif t < 960:
            return "SHADOW"
        else:
            return "OFFHOURS"
