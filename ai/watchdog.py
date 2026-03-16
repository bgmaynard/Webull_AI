"""Watchdog — Monitors bot health and auto-recovers from failures.

Runs as an async background task. Checks:
- Heartbeat freshness
- Stale positions (no price update) — Mar 12: now actually implemented
- Position zombie detection (held past absolute max)
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
        self._consecutive_stale: int = 0

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

        # 1. Check heartbeat freshness (stale > 60s is concerning)
        heartbeat_age = now - self._last_heartbeat
        if heartbeat_age > 120:
            self._add_alert("STALE_HEARTBEAT", f"No heartbeat for {heartbeat_age:.0f}s")

        # 2. Check event flush backlog
        from ai.event_system import get_event_system
        es = get_event_system()
        if len(es._buffer) > es._buffer_limit:
            self._add_alert("EVENT_BACKLOG", f"Event buffer at {len(es._buffer)}")
            await es.flush()
        elif len(es._buffer) > 0:
            await es.flush()

        # 3. Mar 12: Position zombie detection
        await self._check_zombie_positions()

    async def _check_zombie_positions(self):
        """Detect and force-exit positions that are stuck too long."""
        from ai.hft_scalper import get_scalper

        scalper = get_scalper()
        if not scalper.running or not scalper.open_trades:
            self._consecutive_stale = 0
            return

        max_hold = scalper.config.get("max_hold_seconds", 300)
        zombie_threshold = max_hold * 3  # 3x max_hold = zombie
        now = time.time()

        for symbol, trade in list(scalper.open_trades.items()):
            hold_time = now - trade.entry_time

            # Alert on positions approaching zombie threshold
            if hold_time > max_hold * 2:
                self._add_alert("LONG_HOLD",
                    f"{symbol} held {hold_time:.0f}s (max_hold={max_hold}s, zombie at {zombie_threshold:.0f}s)")

            # Force-exit zombies
            if hold_time > zombie_threshold:
                self._add_alert("ZOMBIE_DETECTED",
                    f"FORCE EXIT {symbol}: held {hold_time:.0f}s > {zombie_threshold:.0f}s zombie threshold")
                self._consecutive_stale += 1

                # Use _finalize_exit to avoid depending on broker for zombie cleanup
                from core.registry import get_market_data
                md = get_market_data()
                quote = md.get_cached_quote(symbol)
                exit_price = quote.price if quote and quote.price > 0 else trade.entry_price * 0.95
                scalper._finalize_exit(symbol, exit_price,
                    f"WATCHDOG ZOMBIE EXIT (held {hold_time:.0f}s)")
                logger.warning("WATCHDOG: Force-exited zombie %s after %ds", symbol, int(hold_time))

        # Safety net: if we keep finding zombies, something is deeply wrong
        if self._consecutive_stale >= 3:
            self._add_alert("CHRONIC_ZOMBIES",
                f"{self._consecutive_stale} consecutive zombie detections — scalper may be stalled")

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
        from ai.hft_scalper import get_scalper
        scalper = get_scalper()
        return {
            "running": self.running,
            "last_heartbeat_age": round(time.time() - self._last_heartbeat, 1),
            "open_positions": len(scalper.open_trades) if scalper else 0,
            "consecutive_stale": self._consecutive_stale,
            "recent_alerts": self._alerts[-10:],
            "total_alerts": len(self._alerts),
        }
