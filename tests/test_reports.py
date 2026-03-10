"""Tests for EOD report generation."""

import json
import pytest
from datetime import datetime
from pathlib import Path

import pytz

from ai.event_system import Event, EventSystem, EventType
from ai.reports import ReportGenerator

ET = pytz.timezone("US/Eastern")


@pytest.fixture
def setup(tmp_path):
    """Create event system and report generator sharing same dir."""
    es = EventSystem(reports_dir=str(tmp_path))
    rg = ReportGenerator(reports_dir=str(tmp_path), event_system=es)
    return es, rg, tmp_path


@pytest.mark.asyncio
async def test_empty_report(setup):
    es, rg, _ = setup
    report = await rg.generate_eod("2020-01-01")
    assert report["total_trades"] == 0


@pytest.mark.asyncio
async def test_report_with_trades(setup):
    es, rg, _ = setup
    today = datetime.now(ET).strftime("%Y-%m-%d")

    # Simulate a winning trade
    es.emit(Event(
        event_type=EventType.POSITION_OPENED.value,
        symbol="AAPL", trade_id="t1",
        payload={"entry_price": 10.0, "qty": 100}
    ))
    es.emit(Event(
        event_type=EventType.POSITION_CLOSED.value,
        symbol="AAPL", trade_id="t1",
        payload={"exit_price": 10.50, "reason": "TRAILING_STOP", "hold_seconds": 120}
    ))

    # Simulate a losing trade
    es.emit(Event(
        event_type=EventType.POSITION_OPENED.value,
        symbol="MSFT", trade_id="t2",
        payload={"entry_price": 15.0, "qty": 50}
    ))
    es.emit(Event(
        event_type=EventType.POSITION_CLOSED.value,
        symbol="MSFT", trade_id="t2",
        payload={"exit_price": 14.50, "reason": "HARD_STOP", "hold_seconds": 60}
    ))

    # Simulate a blocked trade
    es.emit(Event(
        event_type=EventType.GATE_BLOCKED.value,
        symbol="TSLA",
        payload={"reason": "spread too wide"}
    ))

    await es.flush()

    report = await rg.generate_eod(today)
    assert report["total_trades"] == 2
    assert report["winners"] == 1
    assert report["losers"] == 1
    assert report["win_rate"] == 50.0
    assert report["total_pnl"] == 25.0  # (0.50*100) + (-0.50*50) = 50 - 25 = 25
    assert report["blocked_trades"] == 1
    assert "TRAILING_STOP" in report["exit_reasons"]
    assert "HARD_STOP" in report["exit_reasons"]


@pytest.mark.asyncio
async def test_report_saved_to_disk(setup):
    es, rg, tmp_path = setup
    today = datetime.now(ET).strftime("%Y-%m-%d")

    es.emit(Event(
        event_type=EventType.POSITION_OPENED.value,
        symbol="TEST", trade_id="t1",
        payload={"entry_price": 5.0, "qty": 10}
    ))
    es.emit(Event(
        event_type=EventType.POSITION_CLOSED.value,
        symbol="TEST", trade_id="t1",
        payload={"exit_price": 5.50, "reason": "profit", "hold_seconds": 30}
    ))
    await es.flush()

    await rg.generate_eod(today)

    report_path = tmp_path / today / "eod_report.json"
    assert report_path.exists()

    with open(report_path) as f:
        saved = json.load(f)
    assert saved["total_trades"] == 1


@pytest.mark.asyncio
async def test_get_saved_report(setup):
    es, rg, tmp_path = setup
    today = datetime.now(ET).strftime("%Y-%m-%d")

    es.emit(Event(event_type=EventType.POSITION_OPENED.value, symbol="X", trade_id="t1",
                  payload={"entry_price": 3.0, "qty": 100}))
    es.emit(Event(event_type=EventType.POSITION_CLOSED.value, symbol="X", trade_id="t1",
                  payload={"exit_price": 3.10, "reason": "trail", "hold_seconds": 45}))
    await es.flush()
    await rg.generate_eod(today)

    saved = await rg.get_saved_report(today)
    assert saved is not None
    assert saved["total_trades"] == 1


@pytest.mark.asyncio
async def test_profit_factor(setup):
    es, rg, _ = setup
    today = datetime.now(ET).strftime("%Y-%m-%d")

    # 3 winners @ $10 each, 1 loser @ $5
    for i in range(3):
        es.emit(Event(event_type=EventType.POSITION_OPENED.value, symbol=f"W{i}", trade_id=f"w{i}",
                      payload={"entry_price": 10.0, "qty": 100}))
        es.emit(Event(event_type=EventType.POSITION_CLOSED.value, symbol=f"W{i}", trade_id=f"w{i}",
                      payload={"exit_price": 10.10, "reason": "trail", "hold_seconds": 30}))

    es.emit(Event(event_type=EventType.POSITION_OPENED.value, symbol="L0", trade_id="l0",
                  payload={"entry_price": 10.0, "qty": 100}))
    es.emit(Event(event_type=EventType.POSITION_CLOSED.value, symbol="L0", trade_id="l0",
                  payload={"exit_price": 9.95, "reason": "stop", "hold_seconds": 20}))

    await es.flush()

    report = await rg.generate_eod(today)
    assert report["total_trades"] == 4
    assert report["winners"] == 3
    assert report["losers"] == 1
    # Gross profit = 3 * 10 = 30, Gross loss = 5
    assert report["profit_factor"] == 6.0
