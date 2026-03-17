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
from ai.event_system import Event, EventType, get_event_system
from ai.momentum_engine import MomentumState, get_momentum_engine
from ai.persistence import get_position_store
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
        self._session_date: str = ""  # tracks current trading day for auto-reset
        self._symbol_loss_cooldown: dict[str, float] = {}  # symbol -> cooldown_until timestamp
        self._pending_exits: dict[str, str] = {}  # symbol -> exit_order_id (Mar 12: fill confirmation)
        self._last_trade_time: float = 0.0  # global cooldown between trades
        self._loop_task: asyncio.Task | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_alive: bool = False
        self._liquidation_lock = asyncio.Lock()  # Mar 12: was threading.Lock, blocks event loop
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
            "session_trade_cap": 30,
            "max_position_count": 3,
            "max_risk_dollars_per_trade": 10.0,
            "symbol_loss_cooldown_seconds": 1800,
            "min_seconds_between_trades": 30,
            "market_hours_start": "09:30",
            "market_hours_end": "16:00",
            "last_entry_before_close_minutes": 5,
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
        """Start the scalper loop. Restores persisted positions from disk."""
        if self.running:
            logger.warning("Scalper already running")
            return False
        if not self.enabled:
            logger.warning("Cannot start: scalper not enabled")
            return False

        # Auto-reset session on new trading day
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if self._session_date and today != self._session_date:
            logger.info("New trading day (%s -> %s): clearing session", self._session_date, today)
            self._completed_trades.clear()
            self._symbol_loss_cooldown.clear()
            get_gating().reset_session()
        self._session_date = today

        # Restore positions from disk (survive restarts)
        await self._restore_positions()

        # Reconcile with broker — only needed for real brokers (alpaca/webull)
        from core.registry import is_sim_mode
        if not is_sim_mode():
            await self._reconcile_broker_positions()

        self.running = True
        self._start_heartbeat()
        self._loop_task = asyncio.create_task(self._scalper_loop())
        get_event_system().emit_system_event(EventType.SCALPER_STARTED)
        logger.info("Scalper STARTED")
        return True

    async def stop(self) -> bool:
        """Stop the scalper loop. Persists open positions to disk."""
        if not self.running:
            return False
        self.running = False
        if self._loop_task:
            self._loop_task.cancel()
            self._loop_task = None
        self._stop_heartbeat()
        # Persist positions so they survive restart
        await self._persist_positions()
        get_event_system().emit_system_event(EventType.SCALPER_STOPPED,
                                              open_positions=len(self.open_trades))
        logger.info("Scalper STOPPED (%d positions persisted)", len(self.open_trades))
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
            # Schedule async stop + liquidation (safe from any context)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._disable_and_liquidate())
            except RuntimeError:
                # No running loop — can't schedule async work
                logger.error("Cannot schedule liquidation: no running event loop")

    async def _disable_and_liquidate(self):
        """Stop loop and liquidate all positions."""
        await self.stop()
        await self.emergency_liquidate("scalper disabled")

    # --- Main Loop ---

    async def _scalper_loop(self):
        """Core scalper tick loop."""
        from core.registry import is_sim_mode
        _sim = is_sim_mode()
        logger.info("Scalper loop started (sim=%s)", _sim)
        momentum = get_momentum_engine()
        _persist_counter = 0

        try:
            while self.running:
                try:
                    # Daily reset check — resets session at date boundary (4 AM ET rollover)
                    self._check_daily_reset(momentum)

                    # Check cooldowns
                    momentum.check_cooldowns()

                    # Check FSM timeouts (IGNITING/GATED stuck)
                    self._check_fsm_timeouts(momentum)

                    # Check pending exit orders — only needed for real brokers
                    if not _sim:
                        await self._check_pending_exits()

                    # Feed worklist scores into momentum engine
                    await self._update_momentum_scores(momentum)

                    # Monitor open positions
                    await self._monitor_positions()

                    # Evaluate GATED symbols for entry
                    await self._evaluate_entries()

                    # Persist positions every 30 ticks (~15s)
                    _persist_counter += 1
                    if _persist_counter >= 30:
                        await self._persist_positions()
                        _persist_counter = 0

                    # Tick interval
                    await asyncio.sleep(0.5)

                except asyncio.CancelledError:
                    break
                except Exception:
                    logger.exception("Scalper loop error")
                    await asyncio.sleep(1.0)
        finally:
            await self._persist_positions()
            logger.info("Scalper loop exited")

    # --- Momentum Score Feeding ---

    async def _update_momentum_scores(self, momentum):
        """Feed live scores into momentum engine to drive FSM transitions.

        Refreshes quotes and computes real-time scores from current market data.
        With starter tier Polygon (100 calls/min), we can afford direct calls
        with 3s cache TTL. Falls back to cached quotes if rate-limited.
        Fetches Finnhub news scores for each symbol (cached 5min).
        """
        from worklist.scoring import ScoringInput, score as compute_score
        from data.finnhub import get_finnhub_news

        store = get_worklist_store()
        md = get_market_data()
        finnhub = get_finnhub_news()

        entries = store.to_list()
        if not entries:
            return

        for entry in entries:
            symbol = entry["symbol"]

            # Skip symbols already in position or exiting
            sm = momentum.get_symbol(symbol)
            if sm.state in (MomentumState.IN_POSITION, MomentumState.MONITORING,
                            MomentumState.EXITING, MomentumState.COOLDOWN):
                continue

            # Fetch quote — uses cache (3s TTL), only hits API when stale
            try:
                quote = await md.get_quote(symbol)
            except Exception:
                quote = md.get_cached_quote(symbol)

            # Fetch news score from Finnhub (cached, non-blocking on failure)
            try:
                news_score = await finnhub.get_news_score(symbol)
            except Exception:
                news_score = 0.0

            if quote and quote.price > 0:
                # Live market data available — build real-time score
                gap_pct = max(quote.change_pct, 0)
                volume = quote.volume
                rvol = max(1.0, gap_pct / 10) if gap_pct > 0 else 1.0

                scoring_input = ScoringInput(
                    symbol=symbol,
                    gap_pct=gap_pct,
                    volume=volume,
                    rvol=rvol,
                    news_score=news_score,
                    scanner_score=50.0,
                    last_data_time=time.time(),
                )
                live_score = compute_score(scoring_input)
            else:
                # No market data (after hours) — use worklist score + news boost
                base_score = entry.get("score", 0)
                # Add news contribution: 15% weight × news_norm
                news_boost = 0.15 * min(news_score, 100)
                live_score = max(base_score, base_score + news_boost)

            # Also update the worklist entry score
            wl_entry = store.get(symbol)
            if wl_entry:
                wl_entry.score = live_score
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
            # Skip symbols with pending exit orders (waiting for fill confirmation)
            if symbol in self._pending_exits:
                continue

            quote = md.get_cached_quote(symbol)
            if not quote or quote.price <= 0:
                # Mar 12 fix: stale/missing quote — if past max hold, emergency exit
                max_hold = self.config.get("max_hold_seconds", 300)
                if trade.hold_seconds >= max_hold * 2:
                    logger.warning("STALE QUOTE emergency exit: %s held %ds with no quote data",
                                   symbol, int(trade.hold_seconds))
                    await self._exit_position(symbol, f"STALE QUOTE EXIT (held {trade.hold_seconds:.0f}s, no quote)")
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
                # Mar 12 fix: absolute cap for winners — 3x max_hold, no exceptions
                if trade.hold_seconds >= max_hold * 3:
                    await self._exit_position(symbol, f"ABSOLUTE MAX HOLD ({trade.hold_seconds:.0f}s, pnl={pnl_pct:+.1f}%)")
                    continue
                # Winners can run past max hold, but start trailing tighter
                drop_from_high = ((trade.high_since_entry - current_price) / trade.high_since_entry) * 100 if trade.high_since_entry > 0 else 0
                if drop_from_high >= trail_pct * 0.5:
                    await self._exit_position(symbol, f"MAX HOLD TRAIL (hold={trade.hold_seconds:.0f}s)")
                    continue

    def _adaptive_stop_pct(self, quote: Quote) -> float:
        """Adaptive hard stop: base + spread adjustment, clamped 1.5-5%."""
        base = self.config.get("stop_loss_percent", 2.5)
        spread_adj = quote.spread_pct * 0.5  # Add half the spread
        return max(1.5, min(5.0, base + spread_adj))

    # --- Entry Evaluation ---

    def _is_entry_allowed(self) -> tuple[bool, str]:
        """Check if new entries are allowed based on market hours and global cooldown."""
        now_et = datetime.now(ET)
        hour, minute = now_et.hour, now_et.minute
        t = hour * 60 + minute

        # Market hours check (configurable)
        start_str = self.config.get("market_hours_start", "09:30")
        end_str = self.config.get("market_hours_end", "16:00")
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        market_open = sh * 60 + sm
        market_close = eh * 60 + em

        if t < market_open or t >= market_close:
            return False, f"MARKET_CLOSED ({now_et.strftime('%H:%M')} ET, hours={start_str}-{end_str})"

        # No new entries in last N minutes before close
        last_entry_min = self.config.get("last_entry_before_close_minutes", 5)
        cutoff = market_close - last_entry_min
        if t >= cutoff:
            return False, f"CLOSE_APPROACHING ({now_et.strftime('%H:%M')} ET, cutoff={last_entry_min}min before close)"

        # Global cooldown between trades
        min_between = self.config.get("min_seconds_between_trades", 30)
        since_last = time.time() - self._last_trade_time
        if self._last_trade_time > 0 and since_last < min_between:
            return False, f"GLOBAL_COOLDOWN ({since_last:.0f}s < {min_between}s)"

        return True, ""

    async def _evaluate_entries(self):
        """Check GATED symbols and attempt entry."""
        # Market hours + global cooldown check
        allowed, reason = self._is_entry_allowed()
        if not allowed:
            return

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
            if symbol in self.open_trades or symbol in self._pending_exits:
                continue

            # Per-symbol loss cooldown: skip if recently lost on this symbol
            cooldown_until = self._symbol_loss_cooldown.get(symbol, 0)
            if time.time() < cooldown_until:
                remaining = int(cooldown_until - time.time())
                logger.debug("Skipping %s: loss cooldown (%ds remaining)", symbol, remaining)
                continue

            quote = md.get_cached_quote(symbol)
            if not quote or quote.ask <= 0:
                continue

            # Price range filter
            min_price = self.config.get("min_price", 2.0)
            max_price = self.config.get("max_price", 20.0)
            if quote.ask < min_price or quote.ask > max_price:
                logger.debug("Skipping %s: price %.2f outside range [%.2f, %.2f]",
                             symbol, quote.ask, min_price, max_price)
                momentum.transition(symbol, MomentumState.IDLE, f"price {quote.ask:.2f} out of range")
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

            # Place limit order at ask (rounded to penny)
            limit_price = round(quote.ask, 2)
            result = await broker.place_order(
                symbol=symbol,
                side=OrderSide.BUY,
                qty=qty,
                order_type=OrderType.LIMIT,
                limit_price=limit_price,
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
                self._last_trade_time = time.time()
                # Emit entry event for trade ledger
                get_event_system().emit_trade_event(
                    EventType.POSITION_OPENED, symbol=symbol, trade_id=result.order_id,
                    entry_price=quote.ask, qty=qty, limit_price=limit_price,
                )
                logger.info("ENTRY: %s %d shares @ %.2f (order=%s)", symbol, qty, quote.ask, result.order_id)
            else:
                logger.warning("Entry order FAILED for %s: %s", symbol, result.message)
                momentum.transition(symbol, MomentumState.IDLE, f"order failed: {result.message}")

    # --- Exit ---

    async def _exit_position(self, symbol: str, reason: str):
        """Exit a position. Sim mode: instant fill + finalize. Real broker: pending exit tracking."""
        trade = self.open_trades.get(symbol)
        if not trade:
            return

        # Guard against duplicate exit calls in same tick
        if symbol in self._pending_exits:
            return

        from core.registry import is_sim_mode

        momentum = get_momentum_engine()
        momentum.mark_exiting(symbol, reason)

        md = get_market_data()
        quote = md.get_cached_quote(symbol)
        sell_price = quote.bid if quote and quote.bid > 0 else trade.entry_price * 0.98
        sell_price = round(sell_price, 2)

        broker = get_broker()

        if is_sim_mode():
            # Mark as exiting to prevent duplicate calls
            self._pending_exits[symbol] = "sim_exit"
            # SIM MODE: Place order (fills instantly in MockBroker) and finalize
            result = await broker.place_order(
                symbol=symbol,
                side=OrderSide.SELL,
                qty=trade.qty,
                order_type=OrderType.LIMIT,
                limit_price=sell_price,
            )
            if result.success:
                # MockBroker fills instantly — get actual fill price
                order_status = await broker.get_order_status(result.order_id)
                fill_price = order_status.price if order_status and order_status.price > 0 else sell_price
                self._finalize_exit(symbol, fill_price, reason)
            else:
                # MockBroker may not have the position (restored from disk) — just finalize
                self._finalize_exit(symbol, sell_price, reason)
            return

        # REAL BROKER MODE: Throttle retries, track pending exits
        now = time.time()
        last_attempt = getattr(trade, '_last_exit_attempt', 0)
        if now - last_attempt < 5:
            return
        trade._last_exit_attempt = now

        # Cancel any open buy order to avoid wash trade rejection
        if trade.order_id and trade.order_id != "reconciled":
            try:
                await broker.cancel_order(trade.order_id)
            except Exception:
                pass

        # Use MARKET order after 2 failed limit attempts
        exit_failures = getattr(trade, '_exit_failures', 0)
        if exit_failures >= 2:
            logger.warning("Switching to MARKET order for %s after %d limit failures", symbol, exit_failures)
            result = await broker.place_order(
                symbol=symbol,
                side=OrderSide.SELL,
                qty=trade.qty,
                order_type=OrderType.MARKET,
            )
        else:
            result = await broker.place_order(
                symbol=symbol,
                side=OrderSide.SELL,
                qty=trade.qty,
                order_type=OrderType.LIMIT,
                limit_price=sell_price,
            )

        if result.success:
            trade.exit_order_id = result.order_id
            logger.info("EXIT ORDER PLACED: %s %d shares @ %.2f | Reason: %s (order=%s)",
                        symbol, trade.qty, sell_price, reason, result.order_id)
            self._pending_exits[symbol] = result.order_id
            trade._exit_reason = reason
            trade._exit_sell_price = sell_price
        else:
            trade._exit_failures = getattr(trade, '_exit_failures', 0) + 1
            logger.warning("Exit order FAILED for %s (attempt %d): %s",
                           symbol, trade._exit_failures, result.message)

            # Detect unrecoverable errors
            unrecoverable = ("cannot be sold short", "hard-to-borrow",
                             "asset is not tradable", "account is restricted")
            msg_lower = (result.message or "").lower()
            is_fatal = any(err in msg_lower for err in unrecoverable)

            if is_fatal:
                logger.error("UNRECOVERABLE exit failure for %s: %s -- blacklisting",
                             symbol, result.message)
                get_gating().blacklist.add(symbol)
                del self.open_trades[symbol]
                self._pending_exits.pop(symbol, None)
                return

            if trade._exit_failures >= 3:
                logger.error("FORCE CLOSING zombie %s after %d failed exits",
                             symbol, trade._exit_failures)
                self._finalize_exit(symbol, sell_price,
                                    f"FORCE CLOSE (exit failed: {result.message})")

    async def _check_pending_exits(self):
        """Check pending exit orders for fill confirmation.

        Mar 12: Positions stay in open_trades until the sell order is confirmed filled.
        If the order is not filled within 60s, cancel and retry.
        """
        if not self._pending_exits:
            return

        broker = get_broker()
        for symbol, order_id in list(self._pending_exits.items()):
            trade = self.open_trades.get(symbol)
            if not trade:
                # Position already removed somehow
                del self._pending_exits[symbol]
                continue

            try:
                order_status = await broker.get_order_status(order_id)
            except Exception:
                logger.warning("Failed to check exit order status for %s", symbol)
                continue

            status_str = (order_status.status or "").upper() if order_status else ""

            if status_str == "FILLED":
                # Order filled — finalize the exit
                fill_price = order_status.price if order_status.price > 0 else getattr(trade, '_exit_sell_price', trade.entry_price)
                reason = getattr(trade, '_exit_reason', 'unknown')
                self._finalize_exit(symbol, fill_price, reason)
                del self._pending_exits[symbol]
                logger.info("EXIT CONFIRMED: %s filled @ %.2f", symbol, fill_price)

            elif status_str in ("CANCELLED", "CANCELED", "EXPIRED", "REJECTED"):
                # Order was cancelled/rejected — retry exit
                del self._pending_exits[symbol]
                trade._last_exit_attempt = 0  # Allow immediate retry
                logger.warning("Exit order %s for %s, will retry", status_str, symbol)

            else:
                # Still pending — check if stale (>30s)
                exit_placed_at = getattr(trade, '_last_exit_attempt', time.time())
                if time.time() - exit_placed_at > 30:
                    # Cancel stale exit order and retry
                    try:
                        await broker.cancel_order(order_id)
                    except Exception:
                        pass
                    del self._pending_exits[symbol]
                    trade._last_exit_attempt = 0
                    logger.warning("Stale exit order for %s cancelled after 30s, will retry", symbol)

    def _finalize_exit(self, symbol: str, exit_price: float, reason: str):
        """Remove position from tracking, record trade, emit events."""
        trade = self.open_trades.get(symbol)
        if not trade:
            return

        pnl = trade.pnl(exit_price)
        pnl_pct = trade.pnl_pct(exit_price)

        trade_record = {
            "symbol": symbol,
            "entry_price": trade.entry_price,
            "exit_price": exit_price,
            "qty": trade.qty,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "hold_seconds": round(trade.hold_seconds, 1),
            "exit_reason": reason,
            "timestamp": datetime.now(ET).isoformat(),
        }
        self._completed_trades.append(trade_record)

        # Emit event for trade ledger
        es = get_event_system()
        es.emit_trade_event(EventType.POSITION_CLOSED,
                            trade_id=trade.order_id, **trade_record)

        logger.info("EXIT FINAL: %s %d shares @ %.2f | PnL: $%.2f (%+.1f%%) | %s",
                     symbol, trade.qty, exit_price, pnl, pnl_pct, reason)

        # Per-symbol loss cooldown
        if pnl < 0:
            cooldown_secs = self.config.get("symbol_loss_cooldown_seconds", 600)
            self._symbol_loss_cooldown[symbol] = time.time() + cooldown_secs
            logger.info("Loss cooldown: %s blocked for %ds", symbol, cooldown_secs)

        # Clean up
        del self.open_trades[symbol]
        self._pending_exits.pop(symbol, None)
        get_momentum_engine().mark_exited(symbol)

    # --- Emergency Liquidation ---

    async def emergency_liquidate(self, reason: str = "emergency"):
        """Idempotent emergency liquidation of all positions."""
        async with self._liquidation_lock:  # Mar 12: async lock, won't block event loop
            if not self.open_trades:
                logger.info("Emergency liquidate: no positions to close")
                return

            es = get_event_system()
            es.emit_system_event(EventType.EXIT_EMERGENCY, reason=reason,
                                 position_count=len(self.open_trades))
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

    # --- Position Persistence (Mar 12: survive restarts) ---

    async def _persist_positions(self):
        """Save open positions to disk for crash recovery."""
        if not self.open_trades:
            await get_position_store().clear()
            return
        positions = []
        for t in self.open_trades.values():
            positions.append({
                "symbol": t.symbol,
                "side": t.side,
                "qty": t.qty,
                "entry_price": t.entry_price,
                "entry_time": t.entry_time,
                "order_id": t.order_id,
                "high_since_entry": t.high_since_entry,
                "exit_order_id": t.exit_order_id,
            })
        await get_position_store().save(positions)
        logger.debug("Persisted %d positions to disk", len(positions))

    async def _restore_positions(self):
        """Restore positions from disk on startup.

        In sim mode, also sync into MockBroker so sell orders work.
        """
        saved = await get_position_store().load()
        if not saved:
            return

        from core.registry import is_sim_mode
        momentum = get_momentum_engine()

        for p in saved:
            symbol = p["symbol"]
            trade = OpenTrade(
                symbol=symbol,
                side=p["side"],
                qty=p["qty"],
                entry_price=p["entry_price"],
                entry_time=p["entry_time"],
                order_id=p["order_id"],
                high_since_entry=p.get("high_since_entry", p["entry_price"]),
                exit_order_id=p.get("exit_order_id", ""),
            )
            self.open_trades[symbol] = trade
            # Restore FSM state
            sm = momentum.get_symbol(symbol)
            sm.state = MomentumState.IN_POSITION
            sm.entry_price = trade.entry_price
            sm.position_qty = trade.qty
            sm.entered_state_at = trade.entry_time
            logger.info("RESTORED position: %s %d shares @ %.2f (held %.0fs)",
                        symbol, trade.qty, trade.entry_price, trade.hold_seconds)

        # Sync restored positions into MockBroker so sell orders succeed
        if is_sim_mode() and saved:
            broker = get_broker()
            if hasattr(broker, '_positions'):
                for p in saved:
                    symbol = p["symbol"]
                    from sim.mock_broker import MockPosition
                    broker._positions[symbol] = MockPosition(
                        symbol=symbol, qty=p["qty"], avg_cost=p["entry_price"]
                    )
                    # Also deduct cash so accounting stays consistent
                    broker._cash -= p["entry_price"] * p["qty"]
                logger.info("Synced %d restored positions into MockBroker", len(saved))

        logger.info("Restored %d positions from disk", len(saved))

    # --- FSM Timeout Check (Mar 12: prevent stuck states) ---

    def _check_daily_reset(self, momentum):
        """Reset all session metrics at date boundary. Runs in the scalper loop
        so it triggers even if the bot runs 24/7 without restart."""
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if today == self._session_date:
            return
        if not self._session_date:
            # First run — just record the date
            self._session_date = today
            return

        logger.info("=== DAILY RESET: %s -> %s ===", self._session_date, today)
        self._session_date = today

        # Clear in-memory trade history
        self._completed_trades.clear()
        self._symbol_loss_cooldown.clear()
        self._pending_exits.clear()
        self._last_trade_time = 0.0

        # Reset gating counters and circuit breakers
        get_gating().reset_session()

        # Reset all momentum FSM states (stale from yesterday)
        active_count = len(momentum.get_all_active())
        momentum.reset_all()
        logger.info("Daily reset complete: cleared trades, cooldowns, gating, %d FSM states", active_count)

        # Flush events from previous day
        get_event_system().emit_system_event(EventType.SESSION_RESET, new_date=today)

    def _check_fsm_timeouts(self, momentum):
        """Expire FSM states that have been stuck too long."""
        for sm in list(momentum.get_all_active()):
            # IGNITING stuck > 120s with no score progress -> back to IDLE
            if sm.state == MomentumState.IGNITING and sm.time_in_state > 120:
                momentum.transition(sm.symbol, MomentumState.IDLE,
                                    f"IGNITING timeout ({sm.time_in_state:.0f}s, score={sm.score:.1f})")
            # GATED stuck > 60s with no fill -> back to IDLE
            elif sm.state == MomentumState.GATED and sm.time_in_state > 60:
                momentum.transition(sm.symbol, MomentumState.IDLE,
                                    f"GATED timeout ({sm.time_in_state:.0f}s, no fill)")

    # --- Broker Reconciliation (Mar 13: sync on startup) ---

    async def _reconcile_broker_positions(self):
        """Reconcile persisted positions with what broker actually holds.

        On startup, cancel any stale open orders and check if broker has
        positions we don't know about (or vice versa).
        """
        try:
            broker = get_broker()

            # Cancel all open orders from previous session
            open_orders = await broker.get_open_orders()
            for order in open_orders:
                try:
                    await broker.cancel_order(order.order_id)
                    logger.info("Cancelled stale order: %s %s %s",
                                order.side, order.symbol, order.order_id)
                except Exception:
                    pass

            # Check broker positions vs our tracked positions
            broker_positions = await broker.get_positions()
            broker_symbols = {p.symbol for p in broker_positions}
            our_symbols = set(self.open_trades.keys())

            # Positions in broker but not in our tracking — add them
            for bp in broker_positions:
                if bp.symbol not in our_symbols and bp.qty > 0:
                    logger.warning("RECONCILE: Found broker position %s (%s shares @ %.2f) not in our tracking — adding",
                                   bp.symbol, bp.qty, bp.avg_cost)
                    self.open_trades[bp.symbol] = OpenTrade(
                        symbol=bp.symbol,
                        side="BUY",
                        qty=int(bp.qty),
                        entry_price=bp.avg_cost,
                        entry_time=time.time() - 60,  # Approximate — mark as 1min old
                        order_id="reconciled",
                        high_since_entry=bp.avg_cost,
                    )

            # Positions in our tracking but not in broker — remove them
            for symbol in our_symbols - broker_symbols:
                logger.warning("RECONCILE: Tracked position %s not found in broker — removing ghost",
                               symbol)
                del self.open_trades[symbol]
                self._pending_exits.pop(symbol, None)

            if broker_positions or (our_symbols - broker_symbols):
                logger.info("RECONCILE: broker=%d positions, tracked=%d, synced",
                            len(broker_positions), len(self.open_trades))

        except Exception:
            logger.exception("Broker reconciliation failed — continuing with persisted positions")

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
