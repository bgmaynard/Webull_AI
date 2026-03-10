"""Data persistence — Atomic file operations for position and state data.

All writes use temp-then-rename pattern to prevent corruption.
All file I/O uses dedicated thread executor.
"""

import asyncio
import json
import logging
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

logger = logging.getLogger(__name__)

_file_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="persist_io")


class AtomicJsonStore:
    """Atomic JSON file store with read/write operations."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def read_sync(self) -> dict | list | None:
        """Synchronous read."""
        if not self.path.exists():
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.exception("Failed to read %s", self.path)
            return None

    def write_sync(self, data: dict | list):
        """Synchronous atomic write (temp + rename)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.path.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=True)
            os.replace(tmp_path, str(self.path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    async def read(self) -> dict | list | None:
        """Async read via thread executor."""
        return await asyncio.to_thread(self.read_sync)

    async def write(self, data: dict | list):
        """Async atomic write via thread executor."""
        await asyncio.to_thread(self.write_sync, data)

    def exists(self) -> bool:
        return self.path.exists()


class PositionStore:
    """Persistent open position storage."""

    def __init__(self, path: str = "data/open_positions.json"):
        self._store = AtomicJsonStore(path)

    async def save(self, positions: list[dict]):
        await self._store.write(positions)

    async def load(self) -> list[dict]:
        data = await self._store.read()
        if isinstance(data, list):
            return data
        return []

    async def clear(self):
        await self._store.write([])


class BlacklistStore:
    """Persistent blacklist with auto-rehab tracking."""

    def __init__(self, path: str = "state/blacklist_rehab.json"):
        self._store = AtomicJsonStore(path)

    async def save(self, blacklist: dict[str, dict]):
        """Save blacklist. Format: {symbol: {reason, blocked_at, rehab_at}}"""
        await self._store.write(blacklist)

    async def load(self) -> dict[str, dict]:
        data = await self._store.read()
        if isinstance(data, dict):
            return data
        return {}


class StateStore:
    """Generic persistent state store."""

    def __init__(self, base_dir: str = "state"):
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _get_store(self, key: str) -> AtomicJsonStore:
        return AtomicJsonStore(self._base_dir / f"{key}.json")

    async def get(self, key: str) -> dict | list | None:
        return await self._get_store(key).read()

    async def set(self, key: str, data: dict | list):
        await self._get_store(key).write(data)

    async def delete(self, key: str):
        path = self._base_dir / f"{key}.json"
        if path.exists():
            await asyncio.to_thread(path.unlink)


# Singletons
_position_store: PositionStore | None = None
_blacklist_store: BlacklistStore | None = None
_state_store: StateStore | None = None


def get_position_store() -> PositionStore:
    global _position_store
    if _position_store is None:
        _position_store = PositionStore()
    return _position_store


def get_blacklist_store() -> BlacklistStore:
    global _blacklist_store
    if _blacklist_store is None:
        _blacklist_store = BlacklistStore()
    return _blacklist_store


def get_state_store() -> StateStore:
    global _state_store
    if _state_store is None:
        _state_store = StateStore()
    return _state_store
