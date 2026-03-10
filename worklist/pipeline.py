"""Worklist pipeline — Intake -> Scrutiny -> Scoring -> Store.

Orchestrates the full flow from raw scanner data to scored worklist entries.
Runs periodically during DISCOVERY and LIVE phases.
"""

import asyncio
import logging
import time

from webull_market_data import Quote, get_market_data
from worklist.scoring import ScoringInput, score
from worklist.scrutiny import ScrutinyConfig, SymbolData, evaluate, get_scrutiny_config
from worklist.store import get_worklist_store

logger = logging.getLogger(__name__)

_instance = None


def get_pipeline() -> "WorklistPipeline":
    global _instance
    if _instance is None:
        _instance = WorklistPipeline()
    return _instance


class WorklistPipeline:
    def __init__(self):
        self.running = False
        self._task: asyncio.Task | None = None
        self._scan_interval: float = 60.0  # seconds between scans
        self._rescore_interval: float = 300.0  # 5 min rescore
        self._last_rescore: float = 0.0
        self._stats = {
            "scans_completed": 0,
            "symbols_evaluated": 0,
            "symbols_passed": 0,
            "symbols_rejected": 0,
        }

    async def start(self):
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._pipeline_loop())
        logger.info("Worklist pipeline started")

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Worklist pipeline stopped")

    async def _pipeline_loop(self):
        try:
            while self.running:
                try:
                    await self._scan_and_process()

                    # Periodic rescore
                    if time.time() - self._last_rescore >= self._rescore_interval:
                        store = get_worklist_store()
                        store.rescore_all()
                        self._last_rescore = time.time()
                        logger.info("Worklist rescored (%d symbols)", store.count)

                    await asyncio.sleep(self._scan_interval)
                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("Pipeline loop error")
                    await asyncio.sleep(5.0)
        finally:
            logger.info("Pipeline loop exited")

    async def _scan_and_process(self):
        """Fetch premarket gainers and run through pipeline."""
        md = get_market_data()

        # Get scanner candidates
        gainers = await md.get_premarket_gainers(count=50)
        if not gainers:
            return

        self._stats["scans_completed"] += 1
        store = get_worklist_store()
        scrutiny_cfg = get_scrutiny_config()

        for item in gainers:
            symbol = item.get("ticker", {}).get("symbol", item.get("symbol", ""))
            if not symbol:
                continue

            self._stats["symbols_evaluated"] += 1

            # Build symbol data from scanner result
            sym_data = self._build_symbol_data(item)

            # Scrutiny filter
            result = evaluate(sym_data, scrutiny_cfg)
            if not result.passed:
                self._stats["symbols_rejected"] += 1
                logger.debug("Scrutiny rejected %s: %s", symbol, result.reason)
                continue

            self._stats["symbols_passed"] += 1

            # Build scoring input and add to worklist
            scoring_input = ScoringInput(
                symbol=symbol,
                gap_pct=sym_data.gap_pct,
                volume=sym_data.volume,
                rvol=sym_data.rvol,
                news_score=sym_data.news_score,
                scanner_score=sym_data.scanner_score,
                last_data_time=time.time(),
            )

            store.add(symbol, scoring_input, source="scanner")

        logger.info("Scan complete: %d candidates, worklist %d/%d",
                     len(gainers), store.count, store.max_size)

    def _build_symbol_data(self, item: dict) -> SymbolData:
        """Convert scanner result dict to SymbolData."""
        ticker = item.get("ticker", {})
        symbol = ticker.get("symbol", item.get("symbol", ""))
        price = float(item.get("close", item.get("price", 0)) or 0)
        prev_close = float(item.get("preClose", 0) or 0)
        volume = int(item.get("volume", 0) or 0)
        change_pct = float(item.get("changeRatio", 0) or 0) * 100

        # Estimate gap% from change
        gap_pct = abs(change_pct) if change_pct > 0 else 0

        # Estimate dollar volume
        dollar_volume = price * volume if price > 0 else 0

        # RVOL: estimate from volume vs average (rough heuristic)
        # In production, this would use historical average volume
        avg_volume = float(item.get("avgVol10d", item.get("avgVolume", 0)) or 0)
        rvol = (volume / avg_volume) if avg_volume > 0 else 1.0

        # Float (may not always be available)
        float_shares = float(item.get("outstandingShares", 0) or 0)
        float_millions = float_shares / 1_000_000 if float_shares > 0 else 0

        return SymbolData(
            symbol=symbol,
            price=price,
            volume=volume,
            rvol=rvol,
            spread_pct=0.5,  # Will be updated with live quote
            gap_pct=gap_pct,
            float_millions=float_millions,
            dollar_volume=dollar_volume,
            scanner_score=min(100, gap_pct + rvol * 5),  # Simple composite
            prev_close=prev_close,
        )

    async def process_single(self, symbol: str, quote: Quote | None = None) -> bool:
        """Process a single symbol through the pipeline (for force-add).

        Force-add uses the full pipeline — no score bypass.
        """
        md = get_market_data()
        if not quote:
            quote = await md.get_quote(symbol)

        if not quote or quote.price <= 0:
            logger.warning("Cannot process %s: no quote data", symbol)
            return False

        gap_pct = quote.change_pct if quote.change_pct > 0 else 0
        dollar_volume = quote.price * quote.volume

        sym_data = SymbolData(
            symbol=symbol,
            price=quote.price,
            volume=quote.volume,
            rvol=1.0,  # Unknown for manual add
            spread_pct=quote.spread_pct,
            gap_pct=gap_pct,
            float_millions=0,  # Unknown
            dollar_volume=dollar_volume,
            scanner_score=50.0,
        )

        result = evaluate(sym_data)
        if not result.passed:
            logger.info("Force-add %s failed scrutiny: %s", symbol, result.reason)
            return False

        scoring_input = ScoringInput(
            symbol=symbol,
            gap_pct=gap_pct,
            volume=quote.volume,
            rvol=1.0,
            scanner_score=50.0,
            last_data_time=time.time(),
        )

        store = get_worklist_store()
        return store.add(symbol, scoring_input, source="manual")

    def get_stats(self) -> dict:
        return dict(self._stats)
