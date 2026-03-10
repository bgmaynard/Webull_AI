"""Tests for the momentum state machine."""

import time
from ai.momentum_engine import (
    MomentumEngine,
    MomentumState,
    MOMENTUM_THRESHOLDS,
)


def test_initial_state():
    engine = MomentumEngine()
    sm = engine.get_symbol("AAPL")
    assert sm.state == MomentumState.IDLE
    assert sm.score == 0.0


def test_valid_transition():
    engine = MomentumEngine()
    engine.get_symbol("AAPL")
    assert engine.transition("AAPL", MomentumState.CANDIDATE, "test")
    assert engine.get_symbol("AAPL").state == MomentumState.CANDIDATE


def test_invalid_transition():
    engine = MomentumEngine()
    engine.get_symbol("AAPL")
    # IDLE -> IN_POSITION is not valid
    assert not engine.transition("AAPL", MomentumState.IN_POSITION, "test")
    assert engine.get_symbol("AAPL").state == MomentumState.IDLE


def test_full_lifecycle():
    engine = MomentumEngine()
    s = "TEST"
    assert engine.transition(s, MomentumState.CANDIDATE, "found")
    assert engine.transition(s, MomentumState.IGNITING, "momentum")
    assert engine.transition(s, MomentumState.GATED, "ready")
    assert engine.transition(s, MomentumState.IN_POSITION, "filled")
    assert engine.transition(s, MomentumState.MONITORING, "watching")
    assert engine.transition(s, MomentumState.EXITING, "trail hit")
    assert engine.transition(s, MomentumState.COOLDOWN, "closed")
    assert engine.transition(s, MomentumState.IDLE, "reset")


def test_score_auto_transitions():
    engine = MomentumEngine()
    # Below candidate threshold (15) — stays IDLE
    engine.update_score("AAPL", 10)
    assert engine.get_symbol("AAPL").state == MomentumState.IDLE

    # At candidate threshold — moves to CANDIDATE
    engine.update_score("AAPL", 15)
    assert engine.get_symbol("AAPL").state == MomentumState.CANDIDATE

    # At igniting threshold (22) — moves to IGNITING
    engine.update_score("AAPL", 22)
    assert engine.get_symbol("AAPL").state == MomentumState.IGNITING

    # At gated threshold (28) — moves to GATED
    engine.update_score("AAPL", 28)
    assert engine.get_symbol("AAPL").state == MomentumState.GATED


def test_score_decay_resets_to_idle():
    engine = MomentumEngine()
    engine.update_score("AAPL", 16)
    assert engine.get_symbol("AAPL").state == MomentumState.CANDIDATE

    # Score drops below threshold (15)
    engine.update_score("AAPL", 10)
    assert engine.get_symbol("AAPL").state == MomentumState.IDLE


def test_get_all_active():
    engine = MomentumEngine()
    engine.update_score("AAPL", 16)  # CANDIDATE
    engine.update_score("MSFT", 10)  # stays IDLE
    engine.update_score("TSLA", 23)  # IGNITING

    active = engine.get_all_active()
    symbols = {sm.symbol for sm in active}
    assert "AAPL" in symbols
    assert "TSLA" in symbols
    assert "MSFT" not in symbols


def test_position_tracking():
    engine = MomentumEngine()
    s = "XYZ"
    engine.transition(s, MomentumState.CANDIDATE)
    engine.transition(s, MomentumState.IGNITING)
    engine.transition(s, MomentumState.GATED)

    engine.mark_position_entered(s, 10.0, 50)
    sm = engine.get_symbol(s)
    assert sm.state == MomentumState.IN_POSITION
    assert sm.entry_price == 10.0
    assert sm.position_qty == 50

    engine.update_price(s, 11.0)
    assert sm.high_since_entry == 11.0

    engine.update_price(s, 10.5)
    assert sm.high_since_entry == 11.0  # High watermark preserved


def test_cooldown_expiry():
    engine = MomentumEngine()
    engine._cooldown_seconds = 0.0  # instant cooldown for test

    s = "ABC"
    engine.transition(s, MomentumState.CANDIDATE)
    engine.transition(s, MomentumState.IGNITING)
    engine.transition(s, MomentumState.GATED)
    engine.transition(s, MomentumState.IN_POSITION)
    engine.transition(s, MomentumState.EXITING)
    engine.transition(s, MomentumState.COOLDOWN)

    engine.check_cooldowns()
    assert engine.get_symbol(s).state == MomentumState.IDLE


def test_event_listener():
    engine = MomentumEngine()
    events = []
    engine.on_event(lambda e: events.append(e))

    engine.transition("AAPL", MomentumState.CANDIDATE, "test")
    assert len(events) == 1
    assert events[0].symbol == "AAPL"
    assert events[0].to_state == MomentumState.CANDIDATE


def test_reset():
    engine = MomentumEngine()
    engine.update_score("AAPL", 35)
    engine.reset_symbol("AAPL")
    sm = engine.get_symbol("AAPL")
    assert sm.state == MomentumState.IDLE
    assert sm.score == 0.0
