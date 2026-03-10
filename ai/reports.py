"""EOD Reports — Daily performance summary and trade analysis.

Generates reports from the event ledger.
"""

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pytz

from ai.event_system import EventSystem, EventType, get_event_system

logger = logging.getLogger(__name__)

ET = pytz.timezone("US/Eastern")

_instance = None


def get_reports() -> "ReportGenerator":
    global _instance
    if _instance is None:
        _instance = ReportGenerator()
    return _instance


class ReportGenerator:
    def __init__(self, reports_dir: str = "reports", event_system: EventSystem | None = None):
        self._reports_dir = Path(reports_dir)
        self._event_system = event_system

    @property
    def _es(self) -> EventSystem:
        return self._event_system or get_event_system()

    async def generate_eod(self, date_str: str | None = None) -> dict:
        """Generate end-of-day report from event ledger."""
        if not date_str:
            date_str = datetime.now(ET).strftime("%Y-%m-%d")

        events = await self._es.get_events_for_date(date_str)

        if not events:
            return {
                "date": date_str,
                "total_trades": 0,
                "message": "No events for this date",
            }

        # Analyze trades
        trades = []
        opens = {}  # trade_id -> open event
        blocked = []
        shadow_trades = []

        for e in events:
            et = e.get("event_type", "")
            trade_id = e.get("trade_id", "")

            if et == EventType.POSITION_OPENED.value:
                opens[trade_id] = e

            elif et == EventType.POSITION_CLOSED.value:
                open_event = opens.get(trade_id)
                if open_event:
                    entry_price = open_event.get("payload", {}).get("entry_price", 0)
                    exit_price = e.get("payload", {}).get("exit_price", 0)
                    qty = open_event.get("payload", {}).get("qty", 0)
                    pnl = (exit_price - entry_price) * qty
                    trades.append({
                        "trade_id": trade_id,
                        "symbol": e.get("symbol", ""),
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "qty": qty,
                        "pnl": round(pnl, 2),
                        "exit_reason": e.get("payload", {}).get("reason", ""),
                        "hold_seconds": e.get("payload", {}).get("hold_seconds", 0),
                    })

            elif et == EventType.GATE_BLOCKED.value:
                blocked.append(e)

            elif et == EventType.SHADOW_TRADE.value:
                shadow_trades.append(e)

        # Compute stats
        total_pnl = sum(t["pnl"] for t in trades)
        winners = [t for t in trades if t["pnl"] > 0]
        losers = [t for t in trades if t["pnl"] < 0]
        win_rate = (len(winners) / len(trades) * 100) if trades else 0
        gross_profit = sum(t["pnl"] for t in winners)
        gross_loss = abs(sum(t["pnl"] for t in losers))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf") if gross_profit > 0 else 0

        # Exit reason breakdown
        exit_reasons = defaultdict(int)
        for t in trades:
            exit_reasons[t.get("exit_reason", "unknown")] += 1

        # Average hold time
        avg_hold = (sum(t.get("hold_seconds", 0) for t in trades) / len(trades)) if trades else 0

        # Symbols traded
        symbols = list(set(t["symbol"] for t in trades))

        report = {
            "date": date_str,
            "total_trades": len(trades),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
            "avg_hold_seconds": round(avg_hold, 1),
            "exit_reasons": dict(exit_reasons),
            "symbols_traded": symbols,
            "blocked_trades": len(blocked),
            "shadow_trades": len(shadow_trades),
            "total_events": len(events),
        }

        # Save report
        await self._save_report(date_str, report)
        return report

    async def _save_report(self, date_str: str, report: dict):
        """Save EOD report to disk."""
        day_dir = self._reports_dir / date_str
        day_dir.mkdir(parents=True, exist_ok=True)
        report_path = day_dir / "eod_report.json"

        def _write():
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=True)

        await asyncio.to_thread(_write)
        logger.info("EOD report saved to %s", report_path)

    async def get_saved_report(self, date_str: str) -> dict | None:
        """Load a previously saved report."""
        report_path = self._reports_dir / date_str / "eod_report.json"
        if not report_path.exists():
            return None

        def _read():
            with open(report_path, "r", encoding="utf-8") as f:
                return json.load(f)

        return await asyncio.to_thread(_read)
