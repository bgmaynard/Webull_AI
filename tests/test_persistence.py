"""Tests for data persistence."""

import pytest
from pathlib import Path

from ai.persistence import AtomicJsonStore, PositionStore, StateStore


@pytest.fixture
def store(tmp_path):
    return AtomicJsonStore(tmp_path / "test.json")


def test_write_and_read_sync(store):
    data = {"symbol": "AAPL", "qty": 100}
    store.write_sync(data)
    result = store.read_sync()
    assert result == data


def test_read_nonexistent(store):
    assert store.read_sync() is None


def test_atomic_write_creates_file(store):
    store.write_sync({"test": True})
    assert store.exists()


def test_overwrite(store):
    store.write_sync({"v": 1})
    store.write_sync({"v": 2})
    assert store.read_sync()["v"] == 2


def test_list_data(store):
    data = [1, 2, 3]
    store.write_sync(data)
    assert store.read_sync() == [1, 2, 3]


@pytest.mark.asyncio
async def test_async_write_and_read(store):
    data = {"async": True}
    await store.write(data)
    result = await store.read()
    assert result == data


@pytest.mark.asyncio
async def test_position_store(tmp_path):
    ps = PositionStore(path=str(tmp_path / "positions.json"))
    positions = [
        {"symbol": "AAPL", "qty": 100, "entry_price": 10.0},
        {"symbol": "MSFT", "qty": 50, "entry_price": 20.0},
    ]
    await ps.save(positions)
    loaded = await ps.load()
    assert len(loaded) == 2
    assert loaded[0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_position_store_empty(tmp_path):
    ps = PositionStore(path=str(tmp_path / "empty.json"))
    loaded = await ps.load()
    assert loaded == []


@pytest.mark.asyncio
async def test_position_store_clear(tmp_path):
    ps = PositionStore(path=str(tmp_path / "positions.json"))
    await ps.save([{"symbol": "AAPL"}])
    await ps.clear()
    loaded = await ps.load()
    assert loaded == []


@pytest.mark.asyncio
async def test_state_store(tmp_path):
    ss = StateStore(base_dir=str(tmp_path / "state"))
    await ss.set("test_key", {"data": 42})
    result = await ss.get("test_key")
    assert result["data"] == 42


@pytest.mark.asyncio
async def test_state_store_delete(tmp_path):
    ss = StateStore(base_dir=str(tmp_path / "state"))
    await ss.set("temp", {"remove": True})
    await ss.delete("temp")
    result = await ss.get("temp")
    assert result is None
