"""Tests for the event system."""

import asyncio
import json
import time
from pathlib import Path

import pytest

from ai.event_system import Event, EventSystem, EventType


@pytest.fixture
def event_system(tmp_path):
    return EventSystem(reports_dir=str(tmp_path))


def test_event_creation():
    e = Event(event_type="TEST", symbol="AAPL", trade_id="t1", payload={"price": 10.0})
    assert e.event_type == "TEST"
    assert e.symbol == "AAPL"
    assert len(e.event_id) == 12
    assert e.timestamp > 0
    assert e.timestamp_et != ""


def test_event_to_json():
    e = Event(event_type="TEST", symbol="AAPL")
    j = e.to_json()
    data = json.loads(j)
    assert data["event_type"] == "TEST"
    assert data["symbol"] == "AAPL"


def test_emit_notifies_listeners(event_system):
    received = []
    event_system.on_event(lambda e: received.append(e))

    e = Event(event_type="TEST")
    event_system.emit(e)
    assert len(received) == 1
    assert received[0].event_type == "TEST"


def test_emit_buffers(event_system):
    e = Event(event_type="TEST")
    event_system.emit(e)
    assert len(event_system._buffer) == 1


@pytest.mark.asyncio
async def test_flush_writes_to_disk(event_system, tmp_path):
    e = Event(event_type="TEST", symbol="AAPL", payload={"price": 10.0})
    event_system.emit(e)
    await event_system.flush()

    assert len(event_system._buffer) == 0

    # Find the ledger file
    ledger_files = list(tmp_path.rglob("trade_ledger.jsonl"))
    assert len(ledger_files) == 1

    with open(ledger_files[0], "r") as f:
        lines = f.readlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_multiple_events_append(event_system, tmp_path):
    for i in range(5):
        event_system.emit(Event(event_type="TEST", symbol=f"SYM{i}"))
    await event_system.flush()

    ledger_files = list(tmp_path.rglob("trade_ledger.jsonl"))
    with open(ledger_files[0], "r") as f:
        lines = f.readlines()
    assert len(lines) == 5


def test_convenience_trade_event(event_system):
    e = event_system.emit_trade_event(
        EventType.ORDER_SUBMITTED, symbol="AAPL", trade_id="t1", price=10.0, qty=100
    )
    assert e.event_type == "ORDER_SUBMITTED"
    assert e.symbol == "AAPL"
    assert e.payload["price"] == 10.0


def test_convenience_system_event(event_system):
    e = event_system.emit_system_event(EventType.SCALPER_STARTED, mode="paper")
    assert e.event_type == "SCALPER_STARTED"
    assert e.payload["mode"] == "paper"


@pytest.mark.asyncio
async def test_read_events_for_date(event_system, tmp_path):
    e = Event(event_type="TEST", symbol="AAPL")
    event_system.emit(e)
    await event_system.flush()

    from datetime import datetime
    import pytz
    today = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d")

    events = await event_system.get_events_for_date(today)
    assert len(events) >= 1
    assert events[0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_read_empty_date(event_system):
    events = await event_system.get_events_for_date("1999-01-01")
    assert events == []
