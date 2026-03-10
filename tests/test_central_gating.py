"""Tests for the central gating system."""

import time
from unittest.mock import patch
from datetime import datetime
import pytz

from ai.central_gating import CentralGating

ET = pytz.timezone("US/Eastern")


def _make_gating(phase="LIVE") -> CentralGating:
    """Create a gating instance with mocked trading phase."""
    g = CentralGating()
    g._get_phase = lambda now: phase
    return g


def test_all_gates_pass():
    g = _make_gating("LIVE")
    result = g.evaluate(symbol="AAPL", spread_pct=0.5, current_position_count=0, risk_dollars=5.0)
    assert result.approved
    assert len(result.checks_failed) == 0
    assert len(result.checks_passed) == 9


def test_kill_switch_blocks():
    g = _make_gating("LIVE")
    g.activate_kill_switch()
    result = g.evaluate("AAPL", 0.5, 0, 5.0)
    assert not result.approved
    assert "kill_switch" in result.checks_failed


def test_wrong_phase_blocks():
    g = _make_gating("DISCOVERY")
    g.account_type = "live"  # Live mode blocks non-LIVE phases
    result = g.evaluate("AAPL", 0.5, 0, 5.0)
    assert not result.approved
    assert "DISCOVERY" in result.reason


def test_paper_mode_allows_discovery():
    g = _make_gating("DISCOVERY")
    g.account_type = "paper"
    result = g.evaluate("AAPL", 0.5, 0, 5.0)
    assert result.approved


def test_paper_mode_blocks_offhours():
    g = _make_gating("OFFHOURS")
    g.account_type = "paper"
    result = g.evaluate("AAPL", 0.5, 0, 5.0)
    assert not result.approved


def test_blacklist_blocks():
    g = _make_gating("LIVE")
    g.add_blacklist("AAPL")
    result = g.evaluate("AAPL", 0.5, 0, 5.0)
    assert not result.approved
    assert "blacklist" in result.checks_failed


def test_spread_blocks():
    g = _make_gating("LIVE")
    result = g.evaluate("AAPL", 2.5, 0, 5.0)
    assert not result.approved
    assert "spread" in result.checks_failed


def test_position_limit_blocks():
    g = _make_gating("LIVE")
    g.max_position_count = 3
    result = g.evaluate("AAPL", 0.5, 3, 5.0)
    assert not result.approved
    assert "position_limit" in result.checks_failed


def test_session_cap_blocks():
    g = _make_gating("LIVE")
    g.session_trade_cap = 5
    g.session_trade_count = 5
    result = g.evaluate("AAPL", 0.5, 0, 5.0)
    assert not result.approved
    assert "session_cap" in result.checks_failed


def test_risk_cap_blocks():
    g = _make_gating("LIVE")
    g.max_risk_dollars = 10.0
    result = g.evaluate("AAPL", 0.5, 0, 15.0)
    assert not result.approved
    assert "risk_cap" in result.checks_failed


def test_circuit_breaker():
    g = _make_gating("LIVE")
    g._hard_stop_limit = 3
    g._circuit_breaker_cooldown = 60

    # 3 hard stops should trigger circuit breaker
    g.record_hard_stop("AAPL")
    g.record_hard_stop("AAPL")
    g.record_hard_stop("AAPL")

    result = g.evaluate("AAPL", 0.5, 0, 5.0)
    assert not result.approved
    assert "circuit_breaker" in result.checks_failed


def test_circuit_breaker_other_symbol_unaffected():
    g = _make_gating("LIVE")
    g._hard_stop_limit = 3
    g.record_hard_stop("AAPL")
    g.record_hard_stop("AAPL")
    g.record_hard_stop("AAPL")

    # MSFT should still be fine
    result = g.evaluate("MSFT", 0.5, 0, 5.0)
    assert result.approved


def test_blacklist_remove():
    g = _make_gating("LIVE")
    g.add_blacklist("AAPL")
    g.remove_blacklist("AAPL")
    result = g.evaluate("AAPL", 0.5, 0, 5.0)
    assert result.approved


def test_session_reset():
    g = _make_gating("LIVE")
    g.session_trade_count = 50
    g.record_hard_stop("AAPL")
    g.reset_session()
    assert g.session_trade_count == 0
    assert len(g._circuit_breakers) == 0


def test_configure():
    g = _make_gating("LIVE")
    g.configure({
        "session_trade_cap": 100,
        "max_position_count": 5,
        "max_risk_dollars_per_trade": 20.0,
        "max_spread_percent": 2.0,
        "hard_stop_limit_per_symbol": 5,
        "circuit_breaker_cooldown_minutes": 60,
    })
    assert g.session_trade_cap == 100
    assert g.max_position_count == 5
    assert g.max_risk_dollars == 20.0
    assert g.max_spread_pct == 2.0
