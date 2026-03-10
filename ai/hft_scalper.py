"""HFT Momentum Scalper — Core trading engine.

Manages the scalper lifecycle: start/stop, position sizing,
entry signals, exit logic (hard stop, trail, profit target, max hold, momentum decay).
Config is loaded from scalper_config.json and updated via API only.
"""

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytz

from ai.central_gating import get_gating
from ai.momentum_engine import MomentumState, get_momentum_engine
from core.models import OrderSide, OrderType, Quote
from core.registry import get_broker, get_market_data
from worklist.store import get_worklist_store

logger = logging.getLogger(__name__)

ET = pytz.timezone("US/Eastern")
CONFIG_PATH = Path("ai/scalper_config.json")

# Dedicated executors (never exhaust default pool)
_file_io_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="file_io")
_api_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="api_call")

_instance = None


def get_scalper() -> "HFTScalper":
    global _instance
    if _instance is None:
        _instance = HFTScalper()
    return _instance


@dataclass
class OpenTrade:
    symbol: str
    side: str
    qty: int
    entry_price: float
    entry_time: float
    order_id: str
    high_since_entry: float = 0.0
    exit_order_id: str = ""

    @property
    def hold_seconds(self) -> float:
        return time.time() - self.entry_time

    def pnl(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.qty

    def pnl_pct(self, current_price: float) -> float:
        if self.entry_price == 0:
            return 0.0
        return ((current_price - self.entry_price) / self.entry_price) * 100


class HFTScalper:
    def __init__(self):
        self.config: dict = {}
        self.enabled: bool = False  # Never persisted
        self.running: bool = False
        self.open_trades: dict[str, OpenTrade] = {}
        self._completed_trades: list[dict] = []
        self._loop_task: asyncio.Task | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_alive: bool = False
        self._liquidation_lock = threading.Lock()
        self._load_config()

        # Wire up gating
        gating = get_gating()
        gating.configure(self.config)

    # --- Config ---

    def _load_config(self):
        """Load config from JSON file."""
        try:
            if CONFIG_PATH.exists():
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    self.config = json.load(f)
                logger.info("Scalper config loaded from %s", CONFIG_PATH)
            else:
                self.config = self._default_config()
                self._save_config()
        except Exception:
            logger.exception("Failed to load config, using defaults")
            self.config = self._default_config()

    def _save_config(self):
        """Atomically save config to disk."""
        try:
            CONFIG_PATH.parent.mkdir(exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(CONFIG_PATH.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.config, f, indent=2)
                os.replace(tmp_path, str(CONFIG_PATH))
            except Exception:
                os.unlink(tmp_path)
                raise
        except Exception:
            logger.exception("Failed to save config")

    def _default_config(self) -> dict:
        return {
            "account_size": 500.0,
            "risk_percent": 2.0,
            "profit_target_percent": 2.5,
            "stop_loss_percent": 1.5,
            "trailing_stop_percent": 1.0,
            "max_hold_seconds": 300,
            "max_spread_percent": 1.0,
            "min_price": 2.0,
            "max_price": 20.0,
            "max_watchlist_size": 15,
            "watchlist_rerank_interval_seconds": 300,
            "use_symbol_circuit_breaker": True,
            "hard_stop_limit_per_symbol": 3,
            "circuit_breaker_cooldown_minutes": 30,
            "lean_mode": True,
            "session_trade_cap": 50,
            "max_position_count": 3,
            "max_risk_dollars_per_trade": 10.0,
        }

    def update_config(self, updates: dict) -> dict:
        """Update config via API. Returns updated config."""
        # Never allow 'enabled' to be persisted
        updates.pop("enabled", None)
        self.config.update(updates)
        self._save_config()
        get_gating().configure(self.config)
        logger.info("Config updated: %s", list(updates.keys()))
        return self.config

    # --- Lifecycle ---

    async def start(self) -> bool:
        """Start the scalper loop."""
        if self.running:
            logger.warning("Scalper already running")
            return False
        if not self.enabled:
            logger.warning("Cannot start: scalper not enabled")
            return False

        self.running = True
        self._start_heartbeat()
        self._loop_task = asyncio.create_task(self._scalper_loop())
        logger.info("Scalper STARTED")
        return True

    async def stop(self) -> bool:
        """Stop the scalper loop. Existing positions remain open."""
        if not self.running:
            return False
        self.running = False
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None
        self._stop_heartbeat()
        logger.info("Scalper STOPPED")
        return True

    def enable(self):
        """Enable the scalper (allows starting)."""
        self.enabled = True
        logger.info("Scalper ENABLED")

    def disable(self):
        """Disable the scalper. If running, stop and emergency liquidate."""
        self.enabled = False
        logger.info("Scalper DISABLED")
        if self.running:
            # Schedule async stop + liquidation
            asyncio.create_task(self._disable_and_liquidate())

    async def _disable_and_liquidate(self):
        """Stop loop and liquidate all positions."""
        await self.stop()
        await self.emergency_liquidate("scalper disabled")

    # --- Main Loop ---

    async def _scalper_loop(self):
        """Core scalper tick loop."""
        logger.info("Scalper loop started")
        momentum = get_momentum_engine()

        try:
            while self.running:
                try:
                    # Check cooldowns
                    momentum.check_cooldowns()

                    # Feed worklist scores into momentum engine
                    await self._update_momentum_scores(momentum)

                    # Monitor open positions
                    await self._monitor_positions()

                    # Evaluate GATED symbols for entry
                    await self._evaluate_entries()

                    # Tick interval
                    await asyncio.sleep(0.5)

                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("Scalper loop error")
                    await asyncio.sleep(1.0)
        finally:
            logger.info("Scalper loop exited")

    # --- Momentum Score Feeding ---

    async def _update_momentum_scores(self, momentum):
        """Feed live scores into momentum engine to drive FSM transitions.

        Refreshes quotes and computes real-time scores from current market data,
        rather than relying on potentially stale worklist scores.
        Fetches Finnhub news scores for each symbol (cached 5min).
        """
        from worklist.scoring import ScoringInput, score as compute_score
        from data.finnhub import get_finnhub_news

        store = get_worklist_store()
        md = get_market_data()
        finnhub = get_finnhub_news()

        for entry in store.to_list():
            symbol = entry["symbol"]

            # Skip symbols already in position or exiting
            sm = momentum.get_symbol(symbol)
            if sm.state in (MomentumState.IN_POSITION, MomentumState.MONITORING,
                            MomentumState.EXITING, MomentumState.COOLDOWN):
                continue

            # Refresh quote for live data
            try:
                quote = await md.get_quote(symbol)
            except Exception:
                continue

            if not quote or quote.price <= 0:
                continue

            # Build real-time scoring input from current quote
            gap_pct = max(quote.change_pct, 0)
            volume = quote.volume

            # Estimate RVOL from volume trajectory (sim stocks accumulate volume over time)
            # Use a reasonable estimate: high-gap stocks typically have 5-15x RVOL
            rvol = max(1.0, gap_pct / 10) if gap_pct > 0 else 1.0

            # Fetch news score from Finnhub (cached, non-blocking on failure)
            try:
                news_score = await finnhub.get_news_score(symbol)
            except Exception:
                news_score = 0.0

            scoring_input = ScoringInput(
                symbol=symbol,
                gap_pct=gap_pct,
                volume=volume,
                rvol=rvol,
                news_score=news_score,
                scanner_score=50.0,
                last_data_time=time.time(),  # Fresh data = no time decay
            )

            live_score = compute_score(scoring_input)

            # Also update the worklist entry score
            wl_entry = store.get(symbol)
            if wl_entry:
                wl_entry.score = live_score
                wl_entry.scoring_input = scoring_input
                wl_entry.last_scored_at = time.time()

            # Feed into momentum engine (drives IDLE->CANDIDATE->IGNITING->GATED)
            momentum.update_score(symbol, live_score)

    # --- Position Monitoring (Exit Logic) ---

    async def _monitor_positions(self):
        """Check all open positions for exit conditions.

        Exit priority:
        1. Hard stop (adaptive 1-3%)
        2. Profit target hit -> start trailing
        3. Trail drops from high
        4. Momentum decay
        5. Max hold time
        """
        md = get_market_data()
        momentum = get_momentum_engine()

        for symbol, trade in list(self.open_trades.items()):
            quote = md.get_cached_quote(symbol)
            if not quote or quote.price <= 0:
                continue

            current_price = quote.price
            pnl_pct = trade.pnl_pct(current_price)

            # Track high water mark
            if current_price > trade.high_since_entry:
                trade.high_since_entry = current_price
            momentum.update_price(symbol, current_price)

            # 1. Hard stop
            stop_pct = self._adaptive_stop_pct(quote)
            if pnl_pct <= -stop_pct:
                await self._exit_position(symbol, f"HARD STOP ({pnl_pct:.1f}%)")
                get_gating().record_hard_stop(symbol)
                momentum.mark_hard_stop(symbol)
                continue

            # 2 & 3. Profit target + trailing stop
            profit_target = self.config.get("profit_target_percent", 2.5)
            trail_pct = self.config.get("trailing_stop_percent", 1.0)

            if trade.high_since_entry > 0 and trade.entry_price > 0:
                high_pnl_pct = ((trade.high_since_entry - trade.entry_price) / trade.entry_price) * 100
                if high_pnl_pct >= profit_target:
                    # Trailing stop active
                    drop_from_high = ((trade.high_since_entry - current_price) / trade.high_since_entry) * 100
                    if drop_from_high >= trail_pct:
                        await self._exit_position(symbol, f"TRAILING STOP (drop={drop_from_high:.1f}% from high)")
                        continue

            # 4. Momentum decay
            sm = momentum.get_symbol(symbol)
            if sm.state == MomentumState.IN_POSITION and sm.score < 20:
                momentum.transition(symbol, MomentumState.MONITORING, "momentum decaying")
            if sm.state == MomentumState.MONITORING and sm.score < 10:
                await self._exit_position(symbol, f"MOMENTUM DECAY (score={sm.score:.0f})")
                continue

            # 5. Max hold time
            max_hold = self.config.get("max_hold_seconds", 300)
            if trade.hold_seconds >= max_hold:
                if pnl_pct <= 0:
                    await self._exit_position(symbol, f"MAX HOLD ({trade.hold_seconds:.0f}s, pnl={pnl_pct:.1f}%)")
                    continue
                # Winners can run past max hold, but start trailing tighter
                drop_from_high = ((trade.high_since_entry - current_price) / trade.high_since_entry) * 100 if trade.high_since_entry > 0 else 0
                if drop_from_high >= trail_pct * 0.5:
                    await self._exit_position(symbol, f"MAX HOLD TRAIL (hold={trade.hold_seconds:.0f}s)")
                    continue

    def _adaptive_stop_pct(self, quote: Quote) -> float:
        """Adaptive hard stop: base + spread adjustment, clamped 1-3%."""
        base = self.config.get("stop_loss_percent", 1.5)
        spread_adj = quote.spread_pct * 0.5  # Add half the spread
        return max(1.0, min(3.0, base + spread_adj))

    # --- Entry Evaluation ---

    async def _evaluate_entries(self):
        """Check GATED symbols and attempt entry."""
        momentum = get_momentum_engine()
        gating = get_gating()
        md = get_market_data()
        broker = get_broker()

        gated_symbols = [
            sm for sm in momentum.get_all_active()
            if sm.state == MomentumState.GATED
        ]

        for sm in gated_symbols:
            symbol = sm.symbol
            if symbol in self.open_trades:
                continue

            quote = md.get_cached_quote(symbol)
            if not quote or quote.ask <= 0:
                continue

            # Calculate position size
            risk_dollars = self._calculate_risk_dollars()
            qty = self._calculate_qty(quote.ask, risk_dollars)
            if qty <= 0:
                continue

            # Run through central gating
            gate_result = gating.evaluate(
                symbol=symbol,
                spread_pct=quote.spread_pct,
                current_position_count=len(self.open_trades),
                risk_dollars=risk_dollars,
            )

            if not gate_result.approved:
                logger.info("Entry BLOCKED for %s: %s", symbol, gate_result.reason)
                momentum.transition(symbol, MomentumState.IDLE, f"gate blocked: {gate_result.reason}")
                continue

            # Place limit order at ask
            result = await broker.place_order(
                symbol=symbol,
                side=OrderSide.BUY,
                qty=qty,
                order_type=OrderType.LIMIT,
                limit_price=quote.ask,
            )

            if result.success:
                self.open_trades[symbol] = OpenTrade(
                    symbol=symbol,
                    side="BUY",
                    qty=qty,
                    entry_price=quote.ask,
                    entry_time=time.time(),
                    order_id=result.order_id,
                    high_since_entry=quote.ask,
                )
                momentum.mark_position_entered(symbol, quote.ask, qty)
                gating.record_trade()
                logger.info("ENTRY: %s %d shares @ %.2f (order=%s)", symbol, qty, quote.ask, result.order_id)
            else:
                logger.warning("Entry order FAILED for %s: %s", symbol, result.message)
                momentum.transition(symbol, MomentumState.IDLE, f"order failed: {result.message}")

    # --- Exit ---

    async def _exit_position(self, symbol: str, reason: str):
        """Exit a position with a limit order at bid."""
        trade = self.open_trades.get(symbol)
        if not trade:
            return

        momentum = get_momentum_engine()
        momentum.mark_exiting(symbol, reason)

        md = get_market_data()
        quote = md.get_cached_quote(symbol)
        sell_price = quote.bid if quote and quote.bid > 0 else trade.entry_price * 0.98

        broker = get_broker()
        result = await broker.place_order(
            symbol=symbol,
            side=OrderSide.SELL,
            qty=trade.qty,
            order_type=OrderType.LIMIT,
            limit_price=sell_price,
        )

        if result.success:
            trade.exit_order_id = result.order_id
            pnl = trade.pnl(sell_price)
            pnl_pct = trade.pnl_pct(sell_price)
            logger.info("EXIT: %s %d shares @ %.2f | PnL: $%.2f | Reason: %s",
                        symbol, trade.qty, sell_price, pnl, reason)

            # Record completed trade
            self._completed_trades.append({
                "symbol": symbol,
                "entry_price": trade.entry_price,
                "exit_price": sell_price,
                "qty": trade.qty,
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "hold_seconds": round(trade.hold_seconds, 1),
                "exit_reason": reason,
                "timestamp": datetime.now(ET).isoformat(),
            })
        else:
            logger.warning("Exit order FAILED for %s: %s — will retry", symbol, result.message)
            return

        # Clean up
        del self.open_trades[symbol]
        momentum.mark_exited(symbol)

    # --- Emergency Liquidation ---

    async def emergency_liquidate(self, reason: str = "emergency"):
        """Idempotent emergency liquidation of all positions."""
        with self._liquidation_lock:
            if not self.open_trades:
                logger.info("Emergency liquidate: no positions to close")
                return

            logger.warning("EMERGENCY LIQUIDATION: %s (%d positions)", reason, len(self.open_trades))
            for symbol in list(self.open_trades.keys()):
                await self._exit_position(symbol, f"EMERGENCY: {reason}")

    # --- Position Sizing ---

    def _calculate_risk_dollars(self) -> float:
        """Calculate risk per trade in dollars."""
        account_size = self.config.get("account_size", 500.0)
        risk_pct = self.config.get("risk_percent", 2.0)
        return account_size * (risk_pct / 100)

    def _calculate_qty(self, price: float, risk_dollars: float) -> int:
        """Calculate share quantity based on risk and stop loss."""
        if price <= 0:
            return 0
        stop_pct = self.config.get("stop_loss_percent", 1.5) / 100
        risk_per_share = price * stop_pct
        if risk_per_share <= 0:
            return 0
        qty = int(risk_dollars / risk_per_share)
        return max(0, qty)

    # --- Heartbeat ---

    def _start_heartbeat(self):
        """Start heartbeat in dedicated daemon thread."""
        self._heartbeat_alive = True
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="scalper_heartbeat"
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat(self):
        self._heartbeat_alive = False

    def _heartbeat_loop(self):
        while self._heartbeat_alive:
            logger.debug("Scalper heartbeat - positions: %d", len(self.open_trades))
            time.sleep(10)

    # --- Status ---

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "open_positions": len(self.open_trades),
            "session_trades": get_gating().session_trade_count,
            "kill_switch": get_gating().kill_switch,
            "config": self.config,
        }

    def get_trade_history(self) -> list[dict]:
        """Return all completed trades for this session."""
        return list(self._completed_trades)

    def get_session_pnl(self) -> dict:
        """Return session P&L summary."""
        trades = self._completed_trades
        total_trades = len(trades)
        if total_trades == 0:
            return {
                "total_trades": 0,
                "winners": 0,
                "losers": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
            }

        winners = sum(1 for t in trades if t["pnl"] > 0)
        losers = sum(1 for t in trades if t["pnl"] <= 0)
        total_pnl = sum(t["pnl"] for t in trades)
        win_rate = (winners / total_trades) * 100 if total_trades > 0 else 0.0

        return {
            "total_trades": total_trades,
            "winners": winners,
            "losers": losers,
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
        }

    def get_trades(self) -> list[dict]:
        return [
            {
                "symbol": t.symbol,
                "side": t.side,
                "qty": t.qty,
                "entry_price": t.entry_price,
                "hold_seconds": round(t.hold_seconds, 1),
                "high_since_entry": t.high_since_entry,
                "order_id": t.order_id,
            }
            for t in self.open_trades.values()
        ]
