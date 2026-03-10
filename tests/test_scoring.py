"""Tests for the scoring engine."""

import time
from worklist.scoring import ScoringConfig, ScoringInput, score, score_batch


def test_basic_score():
    inp = ScoringInput(
        symbol="TEST",
        gap_pct=100.0,
        volume=5_000_000,
        rvol=10.0,
        news_score=80.0,
        scanner_score=70.0,
        last_data_time=time.time(),
    )
    s = score(inp)
    assert 0 <= s <= 100
    assert s > 50  # Strong symbol should score well


def test_zero_inputs():
    inp = ScoringInput(symbol="ZERO")
    s = score(inp)
    assert s == 0.0


def test_max_inputs():
    inp = ScoringInput(
        symbol="MAX",
        gap_pct=200.0,
        volume=10_000_000,
        rvol=20.0,
        news_score=100.0,
        scanner_score=100.0,
        last_data_time=time.time(),
    )
    s = score(inp)
    assert s == 100.0


def test_weights_sum_to_one():
    cfg = ScoringConfig()
    assert cfg.validate()


def test_time_decay():
    # Fresh data — no decay
    inp_fresh = ScoringInput(
        symbol="FRESH", gap_pct=50.0, volume=2_000_000, rvol=5.0,
        last_data_time=time.time()
    )
    s_fresh = score(inp_fresh)

    # Stale data — 20 minutes old (10 min past threshold)
    inp_stale = ScoringInput(
        symbol="STALE", gap_pct=50.0, volume=2_000_000, rvol=5.0,
        last_data_time=time.time() - 1200  # 20 min ago
    )
    s_stale = score(inp_stale)

    assert s_stale < s_fresh  # Stale should score lower


def test_no_decay_within_threshold():
    """Data within stale threshold should NOT be decayed."""
    inp = ScoringInput(
        symbol="OK", gap_pct=50.0, volume=2_000_000, rvol=5.0,
        last_data_time=time.time() - 300  # 5 min, within 10 min threshold
    )
    inp_fresh = ScoringInput(
        symbol="FRESH", gap_pct=50.0, volume=2_000_000, rvol=5.0,
        last_data_time=time.time()
    )
    assert score(inp) == score(inp_fresh)


def test_score_batch():
    inputs = [
        ScoringInput(symbol="A", gap_pct=100.0, volume=5_000_000, last_data_time=time.time()),
        ScoringInput(symbol="B", gap_pct=50.0, volume=2_000_000, last_data_time=time.time()),
        ScoringInput(symbol="C", gap_pct=150.0, volume=8_000_000, last_data_time=time.time()),
    ]
    results = score_batch(inputs)
    # Should be sorted descending
    assert results[0][0] == "C"
    assert results[-1][0] == "B"


def test_cap_at_100():
    inp = ScoringInput(
        symbol="HUGE", gap_pct=500.0, volume=50_000_000, rvol=100.0,
        news_score=200.0, scanner_score=200.0, last_data_time=time.time()
    )
    s = score(inp)
    assert s <= 100.0
