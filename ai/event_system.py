"""Event system — All decisions recorded as structured events.

Events are the source of truth for replay and audit.
Append-only JSONL files per day. All file I/O uses asyncio.to_thread().
"""

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable

import pytz

logger = logging.getLogger(__name__)

ET = pytz.timezone("US/Eastern")

_file_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="event_io")

_instance = None


def get_event_system() -> "EventSystem":
    global _instance
    if _instance is None:
        _instance = EventSystem()
    return _instance


class EventType(str, Enum):
    # Discovery & Scoring
    SIGNAL_CANDIDATE = "SIGNAL_CANDIDATE"
    WORKLIST_ADD = "WORKLIST_ADD"
    WORKLIST_REMOVE = "WORKLIST_REMOVE"
    WORKLIST_DISPLACE = "WORKLIST_DISPLACE"
    WORKLIST_RESCORE = "WORKLIST_RESCORE"

    # Gating
    GATE_APPROVED = "GATE_APPROVED"
    GATE_BLOCKED = "GATE_BLOCKED"

    # Orders
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_FILL_RECEIVED = "ORDER_FILL_RECEIVED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_FAILED = "ORDER_FAILED"

    # Position lifecycle
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_CLOSED = "POSITION_CLOSED"

    # Exit reasons
    EXIT_HARD_STOP = "EXIT_HARD_STOP"
    EXIT_TRAILING_STOP = "EXIT_TRAILING_STOP"
    EXIT_PROFIT_TARGET = "EXIT_PROFIT_TARGET"
    EXIT_MOMENTUM_DECAY = "EXIT_MOMENTUM_DECAY"
    EXIT_MAX_HOLD = "EXIT_MAX_HOLD"
    EXIT_EMERGENCY = "EXIT_EMERGENCY"

    # System
    SCALPER_STARTED = "SCALPER_STARTED"
    SCALPER_STOPPED = "SCALPER_STOPPED"
    KILL_SWITCH_ACTIVATED = "KILL_SWITCH_ACTIVATED"
    KILL_SWITCH_DEACTIVATED = "KILL_SWITCH_DEACTIVATED"
    CIRCUIT_BREAKER_TRIGGERED = "CIRCUIT_BREAKER_TRIGGERED"
    SESSION_RESET = "SESSION_RESET"

    # Shadow
    SHADOW_TRADE = "SHADOW_TRADE"


@dataclass
class Event:
    event_type: str
    symbol: str = ""
    trade_id: str = ""
    payload: dict = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = field(default_factory=time.time)
    timestamp_et: str = ""

    def __post_init__(self):
        if not self.timestamp_et:
            dt = datetime.fromtimestamp(self.timestamp, tz=ET)
            self.timestamp_et = dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "timestamp_et": self.timestamp_et,
            "symbol": self.symbol,
            "trade_id": self.trade_id,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=True)


class EventSystem:
    def __init__(self, reports_dir: str = "reports"):
        self._reports_dir = Path(reports_dir)
        self._listeners: list[Callable[[Event], None]] = []
        self._buffer: list[Event] = []
        self._buffer_limit = 50
        self._today: str = ""

    def on_event(self, listener: Callable[[Event], None]):
        """Register a synchronous event listener."""
        self._listeners.append(listener)

    def emit(self, event: Event):
        """Emit an event — notify listeners and buffer for persistence."""
        # Notify listeners
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:
                logger.exception("Event listener error")

        # Buffer for async flush
        self._buffer.append(event)

        logger.info("EVENT %s: %s %s", event.event_type, event.symbol, event.trade_id)

    async def flush(self):
        """Flush buffered events to disk."""
        if not self._buffer:
            return

        events = self._buffer[:]
        self._buffer.clear()

        await asyncio.to_thread(self._write_events, events)

    def _write_events(self, events: list[Event]):
        """Write events to JSONL file (runs in thread)."""
        today = datetime.now(ET).strftime("%Y-%m-%d")
        day_dir = self._reports_dir / today
        day_dir.mkdir(parents=True, exist_ok=True)

        ledger_path = day_dir / "trade_ledger.jsonl"

        try:
            with open(ledger_path, "a", encoding="utf-8") as f:
                for event in events:
                    f.write(event.to_json() + "\n")
        except Exception:
            logger.exception("Failed to write events to %s", ledger_path)

    async def emit_and_flush(self, event: Event):
        """Emit an event and immediately flush to disk."""
        self.emit(event)
        await self.flush()

    def emit_trade_event(
        self,
        event_type: EventType,
        symbol: str,
        trade_id: str = "",
        **payload,
    ) -> Event:
        """Convenience method for trade-related events."""
        event = Event(
            event_type=event_type.value,
            symbol=symbol,
            trade_id=trade_id,
            payload=payload,
        )
        self.emit(event)
        return event

    def emit_system_event(self, event_type: EventType, **payload) -> Event:
        """Convenience method for system events."""
        event = Event(
            event_type=event_type.value,
            payload=payload,
        )
        self.emit(event)
        return event

    async def get_events_for_date(self, date_str: str) -> list[dict]:
        """Read all events for a given date."""
        ledger_path = self._reports_dir / date_str / "trade_ledger.jsonl"
        if not ledger_path.exists():
            return []

        def _read():
            events = []
            with open(ledger_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            logger.warning("Skipping malformed event line")
            return events

        return await asyncio.to_thread(_read)

    async def get_today_events(self) -> list[dict]:
        today = datetime.now(ET).strftime("%Y-%m-%d")
        return await self.get_events_for_date(today)
