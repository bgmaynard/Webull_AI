"""Tests for the worklist store."""

import time
from worklist.store import WorklistStore
from worklist.scoring import ScoringInput


def _make_input(symbol: str, gap_pct: float = 50.0, volume: int = 2_000_000) -> ScoringInput:
    return ScoringInput(
        symbol=symbol,
        gap_pct=gap_pct,
        volume=volume,
        rvol=5.0,
        scanner_score=70.0,
        last_data_time=time.time(),
    )


def test_add_symbol():
    store = WorklistStore(max_size=5)
    added = store.add("AAPL", _make_input("AAPL"))
    assert added
    assert store.count == 1
    assert store.get("AAPL") is not None


def test_max_size():
    store = WorklistStore(max_size=3)
    store.add("A", _make_input("A", gap_pct=50))
    store.add("B", _make_input("B", gap_pct=60))
    store.add("C", _make_input("C", gap_pct=70))
    assert store.count == 3
    assert store.is_full


def test_displacement():
    store = WorklistStore(max_size=3)
    store.add("A", _make_input("A", gap_pct=30))
    store.add("B", _make_input("B", gap_pct=40))
    store.add("C", _make_input("C", gap_pct=50))

    # D has higher score than A (lowest), should displace it
    added = store.add("D", _make_input("D", gap_pct=60))
    assert added
    assert store.count == 3
    assert store.get("A") is None
    assert store.get("D") is not None


def test_displacement_rejected():
    store = WorklistStore(max_size=3)
    store.add("A", _make_input("A", gap_pct=50))
    store.add("B", _make_input("B", gap_pct=60))
    store.add("C", _make_input("C", gap_pct=70))

    # D has lower score than A (lowest), should be rejected
    added = store.add("D", _make_input("D", gap_pct=10))
    assert not added
    assert store.count == 3
    assert store.get("D") is None


def test_update_existing():
    store = WorklistStore(max_size=5)
    store.add("AAPL", _make_input("AAPL", gap_pct=50))
    old_score = store.get("AAPL").score

    store.add("AAPL", _make_input("AAPL", gap_pct=100))
    new_score = store.get("AAPL").score
    assert new_score > old_score


def test_remove():
    store = WorklistStore(max_size=5)
    store.add("AAPL", _make_input("AAPL"))
    assert store.remove("AAPL")
    assert store.count == 0
    assert not store.remove("AAPL")  # Already removed


def test_get_all_sorted():
    store = WorklistStore(max_size=5)
    store.add("LOW", _make_input("LOW", gap_pct=20))
    store.add("MID", _make_input("MID", gap_pct=50))
    store.add("HIGH", _make_input("HIGH", gap_pct=100))

    entries = store.get_all()
    assert entries[0].symbol == "HIGH"
    assert entries[-1].symbol == "LOW"


def test_get_symbols():
    store = WorklistStore(max_size=5)
    store.add("A", _make_input("A", gap_pct=30))
    store.add("B", _make_input("B", gap_pct=60))
    symbols = store.get_symbols()
    assert symbols == ["B", "A"]  # Sorted by score desc


def test_clear():
    store = WorklistStore(max_size=5)
    store.add("A", _make_input("A"))
    store.add("B", _make_input("B"))
    store.clear()
    assert store.count == 0


def test_to_list():
    store = WorklistStore(max_size=5)
    store.add("AAPL", _make_input("AAPL"))
    result = store.to_list()
    assert len(result) == 1
    assert result[0]["symbol"] == "AAPL"
    assert "score" in result[0]
    assert "age_seconds" in result[0]


def test_uppercase():
    store = WorklistStore(max_size=5)
    store.add("aapl", _make_input("aapl"))
    assert store.get("AAPL") is not None
