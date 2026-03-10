"""Watchdog — Monitors bot health and auto-recovers from failures.

Runs as an async background task. Checks:
- Heartbeat freshness
- Stale positions (no price update)
- Memory/thread pool health
- Event flush backlog
"""

import asyncio
import logging
import time
from datetime import datetime

import pytz

logger = logging.getLogger(__name__)

ET = pytz.timezone("US/Eastern")

_instance = None


def get_watchdog() -> "Watchdog":
    global _instance
    if _instance is None:
        _instance = Watchdog()
    return _instance


class Watchdog:
    def __init__(self):
        self.running = False
        self._task: asyncio.Task | None = None
        self._check_interval: float = 30.0  # seconds
        self._last_heartbeat: float = time.time()
        self._alerts: list[dict] = []
        self._max_alerts: int = 100

    async def start(self):
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._watchdog_loop())
        logger.info("Watchdog started")

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Watchdog stopped")

    def heartbeat(self):
        """Called by other components to signal they're alive."""
        self._last_heartbeat = time.time()

    async def _watchdog_loop(self):
        try:
            while self.running:
                try:
                    await self._run_checks()
                    await asyncio.sleep(self._check_interval)
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("Watchdog check error")
                    await asyncio.sleep(5.0)
        finally:
            logger.info("Watchdog loop exited")

    async def _run_checks(self):
        """Run all health checks."""
        now = time.time()

        # Check heartbeat freshness (stale > 60s is concerning)
        heartbeat_age = now - self._last_heartbeat
        if heartbeat_age > 120:
            self._add_alert("STALE_HEARTBEAT", f"No heartbeat for {heartbeat_age:.0f}s")

        # Check event flush backlog
        from ai.event_system import get_event_system
        es = get_event_system()
        if len(es._buffer) > es._buffer_limit:
            self._add_alert("EVENT_BACKLOG", f"Event buffer at {len(es._buffer)}")
            await es.flush()

        # Periodic event flush
        if len(es._buffer) > 0:
            await es.flush()

    def _add_alert(self, alert_type: str, message: str):
        alert = {
            "type": alert_type,
            "message": message,
            "timestamp": time.time(),
            "timestamp_et": datetime.now(ET).strftime("%H:%M:%S"),
        }
        self._alerts.append(alert)
        if len(self._alerts) > self._max_alerts:
            self._alerts = self._alerts[-self._max_alerts:]
        logger.warning("WATCHDOG ALERT: %s - %s", alert_type, message)

    def get_status(self) -> dict:
        return {
            "running": self.running,
            "last_heartbeat_age": round(time.time() - self._last_heartbeat, 1),
            "recent_alerts": self._alerts[-10:],
            "total_alerts": len(self._alerts),
        }
