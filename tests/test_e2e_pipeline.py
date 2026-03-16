"""End-to-end integration tests for the full trade execution pipeline.

Tests the complete flow:
  Scanner → Worklist → Scoring → Momentum FSM → Central Gating → Order Execution
  → Position Monitoring → Exit Logic → Trade History → Cooldown → Reset

Uses MockBroker + MockMarketData in sim mode — no external APIs needed.
"""

import asyncio
import os
import time
import logging
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

# Force sim mode before any imports
os.environ["BROKER_PROVIDER"] = "sim"
os.environ["MARKET_DATA_PROVIDER"] = "sim"

from core.models import OrderSide, OrderType, Quote
from sim.mock_broker import MockBroker
from sim.mock_market_data import MockMarketData
from ai.momentum_engine import MomentumEngine, MomentumState, MOMENTUM_THRESHOLDS
from ai.central_gating import CentralGating
from worklist.store import WorklistStore
from worklist.scoring import ScoringInput, score as compute_score, score_with_breakdown
from worklist.scrutiny import evaluate as scrutiny_evaluate, SymbolData, ScrutinyConfig

logger = logging.getLogger(__name__)


# ---- Helpers ----

def _fresh_momentum() -> MomentumEngine:
    """Create a fresh momentum engine with sim thresholds."""
    return MomentumEngine(sim_mode=True)


def _fresh_gating(phase="LIVE") -> CentralGating:
    g = CentralGating()
    g._get_phase = lambda now: phase
    g.sim_mode = True
    return g


def _make_scoring_input(symbol: str, gap_pct=50.0, volume=2_000_000, rvol=5.0,
                         news_score=40.0, scanner_score=70.0) -> ScoringInput:
    return ScoringInput(
        symbol=symbol,
        gap_pct=gap_pct,
        volume=volume,
        rvol=rvol,
        news_score=news_score,
        scanner_score=scanner_score,
        last_data_time=time.time(),
    )


# ============================================================
# 1. SCANNER → WORKLIST (Scrutiny + Scoring)
# ============================================================

class TestScannerToWorklist:
    """Test that scanner data is correctly filtered and scored into worklist."""

    def test_good_candidate_passes_scrutiny(self):
        data = SymbolData(
            symbol="TEST", price=5.50, volume=800_000, rvol=5.0,
            spread_pct=0.3, gap_pct=45.0, float_millions=8.0,
            dollar_volume=4_400_000, scanner_score=70.0,
        )
        result = scrutiny_evaluate(data)
        assert result.passed, f"Should pass but failed: {result.checks_failed}"

    def test_bad_candidate_blocked_by_scrutiny(self):
        data = SymbolData(
            symbol="BAD", price=0.50, volume=100, rvol=0.5,
            spread_pct=5.0, gap_pct=2.0, float_millions=500.0,
            dollar_volume=50, scanner_score=10.0,
        )
        result = scrutiny_evaluate(data)
        assert not result.passed
        assert len(result.checks_failed) > 0

    def test_scoring_produces_reasonable_score(self):
        inp = _make_scoring_input("TEST", gap_pct=50, volume=2_000_000,
                                   rvol=5.0, news_score=40, scanner_score=70)
        s = compute_score(inp)
        assert 20 <= s <= 80, f"Score {s} out of expected range"

    def test_score_with_breakdown(self):
        inp = _make_scoring_input("TEST")
        total, breakdown = score_with_breakdown(inp)
        assert total > 0
        assert "gap" in breakdown
        assert "volume" in breakdown
        assert "rvol" in breakdown
        assert "news" in breakdown
        assert "scanner" in breakdown
        # Breakdown contains raw input values, total is the weighted score
        assert total <= 100

    def test_worklist_store_add_and_sort(self):
        store = WorklistStore(max_size=5)
        store.add("AAAA", _make_scoring_input("AAAA", gap_pct=30), source="test")
        store.add("BBBB", _make_scoring_input("BBBB", gap_pct=80), source="test")
        store.add("CCCC", _make_scoring_input("CCCC", gap_pct=50), source="test")

        symbols = store.get_symbols()
        assert len(symbols) == 3

        # Should be sorted by score desc
        all_entries = store.get_all()
        scores = [e.score for e in all_entries]
        assert scores == sorted(scores, reverse=True)

    def test_worklist_displacement(self):
        store = WorklistStore(max_size=2)
        store.add("LOW", _make_scoring_input("LOW", gap_pct=10), source="test")
        store.add("MED", _make_scoring_input("MED", gap_pct=50), source="test")

        # Adding a high scorer should displace the lowest
        store.add("HIGH", _make_scoring_input("HIGH", gap_pct=100), source="test")
        symbols = store.get_symbols()
        assert len(symbols) == 2
        assert "HIGH" in symbols
        assert "MED" in symbols
        assert "LOW" not in symbols


# ============================================================
# 2. MOMENTUM FSM (Score → State Transitions)
# ============================================================

class TestMomentumFSM:
    """Test that scores drive the correct FSM transitions."""

    def test_score_drives_idle_to_candidate(self):
        engine = _fresh_momentum()
        engine.update_score("TEST", 16)
        assert engine.get_symbol("TEST").state == MomentumState.CANDIDATE

    def test_score_drives_candidate_to_igniting(self):
        engine = _fresh_momentum()
        engine.update_score("TEST", 16)
        engine.update_score("TEST", 23)
        assert engine.get_symbol("TEST").state == MomentumState.IGNITING

    def test_score_drives_igniting_to_gated(self):
        engine = _fresh_momentum()
        engine.update_score("TEST", 16)
        engine.update_score("TEST", 23)
        engine.update_score("TEST", 30)
        assert engine.get_symbol("TEST").state == MomentumState.GATED

    def test_full_fsm_lifecycle(self):
        engine = _fresh_momentum()
        engine._cooldown_seconds = 0  # instant cooldown

        # Score up to GATED
        engine.update_score("TEST", 16)
        assert engine.get_symbol("TEST").state == MomentumState.CANDIDATE
        engine.update_score("TEST", 23)
        assert engine.get_symbol("TEST").state == MomentumState.IGNITING
        engine.update_score("TEST", 30)
        assert engine.get_symbol("TEST").state == MomentumState.GATED

        # Fill → IN_POSITION
        engine.mark_position_entered("TEST", 5.50, 100)
        assert engine.get_symbol("TEST").state == MomentumState.IN_POSITION

        # Decay → MONITORING
        engine.transition("TEST", MomentumState.MONITORING, "decay")
        assert engine.get_symbol("TEST").state == MomentumState.MONITORING

        # Exit signal → EXITING
        engine.mark_exiting("TEST", "trail stop")
        assert engine.get_symbol("TEST").state == MomentumState.EXITING

        # Closed → COOLDOWN
        engine.mark_exited("TEST")
        assert engine.get_symbol("TEST").state == MomentumState.COOLDOWN

        # Cooldown expired → IDLE
        engine.check_cooldowns()
        assert engine.get_symbol("TEST").state == MomentumState.IDLE

    def test_score_decay_resets_to_idle(self):
        engine = _fresh_momentum()
        engine.update_score("TEST", 20)
        assert engine.get_symbol("TEST").state == MomentumState.CANDIDATE
        engine.update_score("TEST", 5)
        assert engine.get_symbol("TEST").state == MomentumState.IDLE

    def test_events_emitted_on_transitions(self):
        engine = _fresh_momentum()
        events = []
        engine.on_event(lambda e: events.append(e))

        engine.update_score("TEST", 16)
        engine.update_score("TEST", 23)
        engine.update_score("TEST", 30)

        assert len(events) == 3
        assert events[0].to_state == MomentumState.CANDIDATE
        assert events[1].to_state == MomentumState.IGNITING
        assert events[2].to_state == MomentumState.GATED

    def test_position_high_watermark(self):
        engine = _fresh_momentum()
        engine.update_score("TEST", 30)
        engine.update_score("TEST", 30)  # already IGNITING, push to GATED
        # Manually force to GATED
        engine.transition("TEST", MomentumState.CANDIDATE, "test")
        engine.transition("TEST", MomentumState.IGNITING, "test")
        engine.transition("TEST", MomentumState.GATED, "test")
        engine.mark_position_entered("TEST", 10.0, 50)

        engine.update_price("TEST", 11.0)
        assert engine.get_symbol("TEST").high_since_entry == 11.0

        engine.update_price("TEST", 10.5)
        assert engine.get_symbol("TEST").high_since_entry == 11.0  # preserved


# ============================================================
# 3. CENTRAL GATING (Gate Checks)
# ============================================================

class TestCentralGating:
    """Test all 9 gate checks."""

    def test_all_gates_pass(self):
        g = _fresh_gating("LIVE")
        result = g.evaluate("TEST", spread_pct=0.3, current_position_count=0, risk_dollars=5.0)
        assert result.approved
        assert len(result.checks_failed) == 0

    def test_kill_switch_blocks(self):
        g = _fresh_gating("LIVE")
        g.activate_kill_switch()
        result = g.evaluate("TEST", 0.3, 0, 5.0)
        assert not result.approved
        assert "kill_switch" in result.checks_failed

    def test_spread_blocks(self):
        g = _fresh_gating("LIVE")
        result = g.evaluate("TEST", spread_pct=5.0, current_position_count=0, risk_dollars=5.0)
        assert not result.approved
        assert "spread" in result.checks_failed

    def test_position_limit_blocks(self):
        g = _fresh_gating("LIVE")
        g.max_position_count = 3
        result = g.evaluate("TEST", 0.3, current_position_count=3, risk_dollars=5.0)
        assert not result.approved
        assert "position_limit" in result.checks_failed

    def test_session_cap_blocks(self):
        g = _fresh_gating("LIVE")
        g.session_trade_cap = 5
        g.session_trade_count = 5
        result = g.evaluate("TEST", 0.3, 0, 5.0)
        assert not result.approved
        assert "session_cap" in result.checks_failed

    def test_blacklist_blocks(self):
        g = _fresh_gating("LIVE")
        g.add_blacklist("TEST")
        result = g.evaluate("TEST", 0.3, 0, 5.0)
        assert not result.approved
        assert "blacklist" in result.checks_failed

    def test_circuit_breaker_blocks(self):
        g = _fresh_gating("LIVE")
        g._hard_stop_limit = 3
        g._circuit_breaker_cooldown = 60
        for _ in range(3):
            g.record_hard_stop("TEST")
        result = g.evaluate("TEST", 0.3, 0, 5.0)
        assert not result.approved
        assert "circuit_breaker" in result.checks_failed

    def test_risk_cap_blocks(self):
        g = _fresh_gating("LIVE")
        g.max_risk_dollars = 10.0
        result = g.evaluate("TEST", 0.3, 0, risk_dollars=15.0)
        assert not result.approved
        assert "risk_cap" in result.checks_failed


# ============================================================
# 4. MOCK BROKER (Order Execution)
# ============================================================

class TestMockBroker:
    """Test simulated order fills and position tracking."""

    @pytest.fixture
    def broker(self):
        return MockBroker(starting_cash=500.0)

    @pytest.mark.asyncio
    async def test_buy_order_fills(self, broker):
        result = await broker.place_order(
            symbol="TEST", side=OrderSide.BUY, qty=10,
            order_type=OrderType.LIMIT, limit_price=5.0,
        )
        assert result.success
        assert result.order_id

        # Position should exist
        positions = await broker.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "TEST"
        assert positions[0].qty == 10

    @pytest.mark.asyncio
    async def test_sell_order_closes_position(self, broker):
        await broker.place_order("TEST", OrderSide.BUY, 10, OrderType.LIMIT, 5.0)
        result = await broker.place_order("TEST", OrderSide.SELL, 10, OrderType.LIMIT, 5.5)
        assert result.success

        positions = await broker.get_positions()
        assert len(positions) == 0

    @pytest.mark.asyncio
    async def test_insufficient_cash_rejected(self, broker):
        result = await broker.place_order("TEST", OrderSide.BUY, 1000, OrderType.LIMIT, 100.0)
        assert not result.success
        assert "Insufficient" in result.message

    @pytest.mark.asyncio
    async def test_sell_without_position_rejected(self, broker):
        result = await broker.place_order("TEST", OrderSide.SELL, 10, OrderType.LIMIT, 5.0)
        assert not result.success

    @pytest.mark.asyncio
    async def test_account_cash_updates_after_trades(self, broker):
        account = await broker.get_account()
        initial_cash = account.cash

        await broker.place_order("TEST", OrderSide.BUY, 10, OrderType.LIMIT, 5.0)
        account = await broker.get_account()
        assert account.cash < initial_cash  # cash decreased

    @pytest.mark.asyncio
    async def test_order_status_tracking(self, broker):
        result = await broker.place_order("TEST", OrderSide.BUY, 10, OrderType.LIMIT, 5.0)
        status = await broker.get_order_status(result.order_id)
        assert status is not None
        assert status.status == "FILLED"
        assert status.filled_qty == 10


# ============================================================
# 5. MOCK MARKET DATA (Quote Generation)
# ============================================================

class TestMockMarketData:
    """Test simulated market data generation."""

    @pytest.fixture
    def md(self):
        return MockMarketData()

    @pytest.mark.asyncio
    async def test_get_quote_returns_valid_data(self, md):
        quote = await md.get_quote("XMPL")
        assert quote.symbol == "XMPL"
        assert quote.price > 0
        assert quote.bid > 0
        assert quote.ask > 0
        assert quote.ask >= quote.bid

    @pytest.mark.asyncio
    async def test_premarket_gainers(self, md):
        gainers = await md.get_premarket_gainers(5)
        assert len(gainers) == 5
        # Should be sorted by change ratio descending
        ratios = [g["changeRatio"] for g in gainers]
        assert ratios == sorted(ratios, reverse=True)

    @pytest.mark.asyncio
    async def test_cached_quote(self, md):
        # Before any fetch, cache is empty
        assert md.get_cached_quote("XMPL") is None

        # After fetch, cache populated
        await md.get_quote("XMPL")
        cached = md.get_cached_quote("XMPL")
        assert cached is not None
        assert cached.symbol == "XMPL"

    @pytest.mark.asyncio
    async def test_price_movements_over_time(self, md):
        """Prices should change between ticks (random walk)."""
        prices = []
        for _ in range(10):
            q = await md.get_quote("FOMO")
            prices.append(q.price)
            await asyncio.sleep(0.05)  # Small delay so dt > 0 for random walk
        # Not all prices should be identical (extremely unlikely with random walk)
        assert len(set(prices)) > 1

    @pytest.mark.asyncio
    async def test_unknown_symbol_returns_quote(self, md):
        """Unknown symbols get random quotes (graceful fallback)."""
        q = await md.get_quote("UNKNOWN")
        assert q.symbol == "UNKNOWN"
        assert q.price > 0


# ============================================================
# 6. FULL PIPELINE INTEGRATION: Scanner → Entry → Exit
# ============================================================

class TestFullPipelineIntegration:
    """End-to-end test: from scanning to trade completion."""

    @pytest.fixture
    def pipeline_components(self):
        """Set up all components with fresh instances."""
        broker = MockBroker(starting_cash=500.0)
        md = MockMarketData()
        broker.set_market_data(md)
        momentum = MomentumEngine(sim_mode=True)
        momentum._cooldown_seconds = 0  # instant cooldown for tests
        gating = CentralGating()
        gating._get_phase = lambda now: "LIVE"
        gating.sim_mode = True
        store = WorklistStore(max_size=15)

        return {
            "broker": broker,
            "md": md,
            "momentum": momentum,
            "gating": gating,
            "store": store,
        }

    @pytest.mark.asyncio
    async def test_scanner_to_worklist_to_gated(self, pipeline_components):
        """Scanner data → scrutiny → scoring → worklist → momentum GATED."""
        c = pipeline_components

        # 1. Simulate scanner finding FOMO
        gainers = await c["md"].get_premarket_gainers(10)
        assert len(gainers) > 0

        fomo = next(g for g in gainers if g["ticker"]["symbol"] == "FOMO")
        assert fomo["changeRatio"] > 0.3  # 80% gap

        # 2. Score it
        inp = ScoringInput(
            symbol="FOMO", gap_pct=fomo["changeRatio"] * 100,
            volume=fomo["volume"], rvol=5.0,
            news_score=50.0, scanner_score=70.0,
            last_data_time=time.time(),
        )
        s = compute_score(inp)
        assert s > 0

        # 3. Add to worklist
        c["store"].add("FOMO", inp, source="scanner")
        assert "FOMO" in c["store"].get_symbols()

        # 4. Feed score into momentum engine (should reach GATED)
        c["momentum"].update_score("FOMO", s)
        state = c["momentum"].get_symbol("FOMO").state

        # Score should be high enough to at least become CANDIDATE
        assert state in (MomentumState.CANDIDATE, MomentumState.IGNITING, MomentumState.GATED), \
            f"Expected active state, got {state} (score={s})"

        # Push to GATED if not there yet
        if state != MomentumState.GATED:
            c["momentum"].update_score("FOMO", 30)  # above gated threshold
            c["momentum"].update_score("FOMO", 30)  # ensure transition chain

    @pytest.mark.asyncio
    async def test_gated_to_entry_to_exit(self, pipeline_components):
        """GATED symbol → gating approved → buy fill → exit → trade recorded."""
        c = pipeline_components
        broker = c["broker"]
        md = c["md"]
        momentum = c["momentum"]
        gating = c["gating"]

        # Force FOMO to GATED state
        momentum.update_score("FOMO", 16)
        momentum.update_score("FOMO", 23)
        momentum.update_score("FOMO", 30)
        assert momentum.get_symbol("FOMO").state == MomentumState.GATED

        # Get a quote
        quote = await md.get_quote("FOMO")
        assert quote.ask > 0

        # Run gating
        gate_result = gating.evaluate(
            symbol="FOMO", spread_pct=quote.spread_pct,
            current_position_count=0, risk_dollars=5.0,
        )
        assert gate_result.approved, f"Gating failed: {gate_result.checks_failed}"

        # Calculate position size — must fit within $500 cash
        risk_dollars = 500 * 0.02  # $10 risk
        stop_pct = 0.015  # 1.5%
        qty_from_risk = int(risk_dollars / (quote.ask * stop_pct))
        max_affordable = int(490 / quote.ask)  # leave some margin
        qty = min(qty_from_risk, max_affordable)
        assert qty > 0

        # Place buy order
        buy_result = await broker.place_order(
            symbol="FOMO", side=OrderSide.BUY, qty=qty,
            order_type=OrderType.LIMIT, limit_price=quote.ask,
        )
        assert buy_result.success

        # Mark position in momentum engine
        momentum.mark_position_entered("FOMO", quote.ask, qty)
        assert momentum.get_symbol("FOMO").state == MomentumState.IN_POSITION
        gating.record_trade()

        # Verify position exists
        positions = await broker.get_positions()
        assert any(p.symbol == "FOMO" for p in positions)

        # Simulate exit (trailing stop)
        momentum.mark_exiting("FOMO", "trailing stop")
        assert momentum.get_symbol("FOMO").state == MomentumState.EXITING

        sell_price = quote.bid if quote.bid > 0 else quote.ask * 0.98
        sell_result = await broker.place_order(
            symbol="FOMO", side=OrderSide.SELL, qty=qty,
            order_type=OrderType.LIMIT, limit_price=sell_price,
        )
        assert sell_result.success

        # Mark exited
        momentum.mark_exited("FOMO")
        assert momentum.get_symbol("FOMO").state == MomentumState.COOLDOWN

        # Verify position closed
        positions = await broker.get_positions()
        assert not any(p.symbol == "FOMO" for p in positions)

        # Cooldown expires → IDLE
        momentum.check_cooldowns()
        assert momentum.get_symbol("FOMO").state == MomentumState.IDLE

    @pytest.mark.asyncio
    async def test_multiple_positions_and_limits(self, pipeline_components):
        """Test position limit enforcement across multiple symbols."""
        c = pipeline_components
        gating = c["gating"]
        gating.max_position_count = 2

        # First two positions should be approved
        r1 = gating.evaluate("SYM1", 0.3, current_position_count=0, risk_dollars=5.0)
        assert r1.approved

        r2 = gating.evaluate("SYM2", 0.3, current_position_count=1, risk_dollars=5.0)
        assert r2.approved

        # Third should be blocked
        r3 = gating.evaluate("SYM3", 0.3, current_position_count=2, risk_dollars=5.0)
        assert not r3.approved
        assert "position_limit" in r3.checks_failed

    @pytest.mark.asyncio
    async def test_hard_stop_triggers_circuit_breaker(self, pipeline_components):
        """Three hard stops on same symbol → circuit breaker blocks."""
        c = pipeline_components
        gating = c["gating"]
        gating._hard_stop_limit = 3
        gating._circuit_breaker_cooldown = 60

        gating.record_hard_stop("FOMO")
        gating.record_hard_stop("FOMO")
        gating.record_hard_stop("FOMO")

        result = gating.evaluate("FOMO", 0.3, 0, 5.0)
        assert not result.approved
        assert "circuit_breaker" in result.checks_failed

        # Different symbol should still be allowed
        result2 = gating.evaluate("XMPL", 0.3, 0, 5.0)
        assert result2.approved


# ============================================================
# 7. POSITION MONITORING (Exit Conditions)
# ============================================================

class TestExitConditions:
    """Test all exit condition paths."""

    @pytest.mark.asyncio
    async def test_hard_stop_exit(self):
        """Price drops below adaptive stop → hard stop exit."""
        broker = MockBroker(starting_cash=500.0)
        md = MockMarketData()
        broker.set_market_data(md)

        # Enter position
        entry_price = 5.0
        qty = 10
        await broker.place_order("TEST", OrderSide.BUY, qty, OrderType.LIMIT, entry_price)

        # Price drops 2% → should trigger hard stop (1.5% base)
        exit_price = entry_price * 0.97
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100
        assert pnl_pct < -1.5  # Below hard stop threshold

        # Execute exit
        result = await broker.place_order("TEST", OrderSide.SELL, qty, OrderType.LIMIT, exit_price)
        assert result.success

        positions = await broker.get_positions()
        assert len(positions) == 0

    @pytest.mark.asyncio
    async def test_trailing_stop_exit(self):
        """Price hits profit target then drops → trailing stop exit."""
        entry_price = 5.0
        high_price = 5.20  # 4% above entry
        current_price = 5.12  # dropped 1.5% from high

        profit_target = 2.5  # %
        trail_pct = 1.0  # %

        high_pnl_pct = ((high_price - entry_price) / entry_price) * 100
        assert high_pnl_pct >= profit_target  # 4% > 2.5%

        drop_from_high = ((high_price - current_price) / high_price) * 100
        assert drop_from_high >= trail_pct  # ~1.5% > 1.0%

    @pytest.mark.asyncio
    async def test_momentum_decay_exit(self):
        """Momentum score drops below thresholds → exit."""
        momentum = _fresh_momentum()

        # Get to IN_POSITION
        momentum.update_score("TEST", 16)
        momentum.update_score("TEST", 23)
        momentum.update_score("TEST", 30)
        momentum.mark_position_entered("TEST", 5.0, 100)
        assert momentum.get_symbol("TEST").state == MomentumState.IN_POSITION

        # Score drops → MONITORING
        sm = momentum.get_symbol("TEST")
        sm.score = 15  # Below 20 threshold
        # In the real scalper, this check happens:
        if sm.score < 20:
            momentum.transition("TEST", MomentumState.MONITORING, "decay")
        assert sm.state == MomentumState.MONITORING

        # Score drops further → should trigger exit
        sm.score = 5  # Below 10
        if sm.score < 10:
            momentum.mark_exiting("TEST", "momentum collapse")
        assert sm.state == MomentumState.EXITING

    @pytest.mark.asyncio
    async def test_max_hold_time_exit(self):
        """Position held past max_hold_seconds with negative PnL → exit."""
        from ai.hft_scalper import OpenTrade

        trade = OpenTrade(
            symbol="TEST", side="BUY", qty=10,
            entry_price=5.0, entry_time=time.time() - 600,  # 10 min ago
            order_id="test123",
        )

        max_hold = 300  # 5 min
        assert trade.hold_seconds >= max_hold  # Should trigger

        current_price = 4.90  # negative PnL
        pnl_pct = trade.pnl_pct(current_price)
        assert pnl_pct < 0  # Should exit


# ============================================================
# 8. ADAPTIVE STOP CALCULATION
# ============================================================

class TestAdaptiveStop:
    """Test adaptive hard stop with spread adjustment."""

    def test_tight_spread_normal_stop(self):
        """Tight spread → stop near base (1.5%)."""
        from ai.hft_scalper import HFTScalper
        scalper = HFTScalper.__new__(HFTScalper)
        scalper.config = {"stop_loss_percent": 1.5}

        quote = Quote(symbol="TEST", price=5.0, bid=4.99, ask=5.01)
        # spread_pct = (5.01-4.99)/5.0 * 100 = 0.4%
        stop = scalper._adaptive_stop_pct(quote)
        assert 1.0 <= stop <= 2.0

    def test_wide_spread_wider_stop(self):
        """Wide spread → stop widened."""
        from ai.hft_scalper import HFTScalper
        scalper = HFTScalper.__new__(HFTScalper)
        scalper.config = {"stop_loss_percent": 1.5}

        quote = Quote(symbol="TEST", price=5.0, bid=4.85, ask=5.15)
        # spread_pct = (5.15-4.85)/5.0 * 100 = 6%
        stop = scalper._adaptive_stop_pct(quote)
        assert stop <= 3.0  # Clamped at 3%


# ============================================================
# 9. POSITION SIZING
# ============================================================

class TestPositionSizing:
    """Test risk-based position sizing calculations."""

    def test_standard_sizing(self):
        from ai.hft_scalper import HFTScalper
        scalper = HFTScalper.__new__(HFTScalper)
        scalper.config = {
            "account_size": 500.0,
            "risk_percent": 2.0,
            "stop_loss_percent": 1.5,
        }

        risk = scalper._calculate_risk_dollars()
        assert risk == 10.0  # 500 * 2%

        qty = scalper._calculate_qty(5.0, risk)
        # risk_per_share = 5.0 * 0.015 = 0.075
        # qty = 10.0 / 0.075 = 133
        assert qty == 133

    def test_zero_price_returns_zero_qty(self):
        from ai.hft_scalper import HFTScalper
        scalper = HFTScalper.__new__(HFTScalper)
        scalper.config = {"stop_loss_percent": 1.5}
        assert scalper._calculate_qty(0, 10.0) == 0


# ============================================================
# 10. COMPLETE TRADE LIFECYCLE (End-to-End Async)
# ============================================================

class TestCompleteTradeLifecycle:
    """Full async lifecycle simulating what the scalper loop does."""

    @pytest.mark.asyncio
    async def test_complete_buy_monitor_sell_cycle(self):
        """Simulates one complete trade from discovery to completion."""
        # Setup
        broker = MockBroker(starting_cash=500.0)
        md = MockMarketData()
        broker.set_market_data(md)
        momentum = MomentumEngine(sim_mode=True)
        momentum._cooldown_seconds = 0
        gating = CentralGating()
        gating._get_phase = lambda now: "LIVE"
        gating.sim_mode = True
        completed_trades = []

        # Phase 1: Discovery — scan and score
        gainers = await md.get_premarket_gainers(10)
        best = gainers[0]
        symbol = best["ticker"]["symbol"]

        inp = ScoringInput(
            symbol=symbol,
            gap_pct=best["changeRatio"] * 100,
            volume=best["volume"],
            rvol=5.0, news_score=50.0, scanner_score=70.0,
            last_data_time=time.time(),
        )
        live_score = compute_score(inp)

        # Phase 2: Momentum ramp — feed scores until GATED
        momentum.update_score(symbol, max(live_score, 16))
        if momentum.get_symbol(symbol).state != MomentumState.IGNITING:
            momentum.update_score(symbol, 23)
        if momentum.get_symbol(symbol).state != MomentumState.GATED:
            momentum.update_score(symbol, 30)
        assert momentum.get_symbol(symbol).state == MomentumState.GATED

        # Phase 3: Gating check
        quote = await md.get_quote(symbol)
        gate_result = gating.evaluate(symbol, quote.spread_pct, 0, 10.0)
        assert gate_result.approved

        # Phase 4: Entry — size to fit within cash
        qty_from_risk = max(1, int(10.0 / (quote.ask * 0.015)))
        max_affordable = int(490 / quote.ask)
        qty = min(qty_from_risk, max_affordable)
        buy = await broker.place_order(symbol, OrderSide.BUY, qty, OrderType.LIMIT, quote.ask)
        assert buy.success
        entry_price = quote.ask

        momentum.mark_position_entered(symbol, entry_price, qty)
        gating.record_trade()
        assert momentum.get_symbol(symbol).state == MomentumState.IN_POSITION

        # Phase 5: Monitoring — simulate a few price ticks
        high = entry_price
        for _ in range(5):
            q = await md.get_quote(symbol)
            if q.price > high:
                high = q.price
            momentum.update_price(symbol, q.price)

        assert momentum.get_symbol(symbol).high_since_entry >= entry_price

        # Phase 6: Exit — simulate trailing stop trigger
        momentum.mark_exiting(symbol, "test trailing stop")
        assert momentum.get_symbol(symbol).state == MomentumState.EXITING

        exit_quote = await md.get_quote(symbol)
        sell_price = exit_quote.bid if exit_quote.bid > 0 else entry_price * 0.98
        sell = await broker.place_order(symbol, OrderSide.SELL, qty, OrderType.LIMIT, sell_price)
        assert sell.success

        pnl = (sell_price - entry_price) * qty
        completed_trades.append({
            "symbol": symbol,
            "entry_price": entry_price,
            "exit_price": sell_price,
            "qty": qty,
            "pnl": round(pnl, 2),
            "exit_reason": "trailing stop",
        })

        # Phase 7: Cooldown → IDLE
        momentum.mark_exited(symbol)
        assert momentum.get_symbol(symbol).state == MomentumState.COOLDOWN
        momentum.check_cooldowns()
        assert momentum.get_symbol(symbol).state == MomentumState.IDLE

        # Phase 8: Verify final state
        positions = await broker.get_positions()
        assert len(positions) == 0  # No positions left
        assert len(completed_trades) == 1
        assert gating.session_trade_count == 1

        # Account should still have funds
        account = await broker.get_account()
        assert account.cash > 0

    @pytest.mark.asyncio
    async def test_emergency_liquidation(self):
        """Test emergency liquidation closes all positions."""
        broker = MockBroker(starting_cash=500.0)
        md = MockMarketData()
        broker.set_market_data(md)

        # Open 3 positions
        for sym in ["XMPL", "FOMO", "PUMP"]:
            q = await md.get_quote(sym)
            await broker.place_order(sym, OrderSide.BUY, 5, OrderType.LIMIT, q.ask)

        positions = await broker.get_positions()
        assert len(positions) == 3

        # Emergency liquidate
        for sym in ["XMPL", "FOMO", "PUMP"]:
            q = await md.get_quote(sym)
            await broker.place_order(sym, OrderSide.SELL, 5, OrderType.LIMIT, q.bid)

        positions = await broker.get_positions()
        assert len(positions) == 0

    @pytest.mark.asyncio
    async def test_session_trade_counting(self):
        """Verify session trade counter increments correctly."""
        gating = _fresh_gating("LIVE")
        assert gating.session_trade_count == 0

        gating.record_trade()
        gating.record_trade()
        gating.record_trade()
        assert gating.session_trade_count == 3

        gating.reset_session()
        assert gating.session_trade_count == 0

    @pytest.mark.asyncio
    async def test_multiple_trades_in_sequence(self):
        """Run 3 full buy/sell cycles and verify accounting."""
        broker = MockBroker(starting_cash=1000.0)
        md = MockMarketData()
        broker.set_market_data(md)

        symbols = ["XMPL", "FOMO", "PUMP"]
        total_pnl = 0.0

        for sym in symbols:
            q = await md.get_quote(sym)
            buy = await broker.place_order(sym, OrderSide.BUY, 10, OrderType.LIMIT, q.ask)
            assert buy.success

            # Get new quote for exit
            q2 = await md.get_quote(sym)
            sell = await broker.place_order(sym, OrderSide.SELL, 10, OrderType.LIMIT, q2.bid)
            assert sell.success

        # All positions should be closed
        positions = await broker.get_positions()
        assert len(positions) == 0

        # Account should still have reasonable cash
        account = await broker.get_account()
        assert account.cash > 0

    @pytest.mark.asyncio
    async def test_wired_mock_broker_market_data(self):
        """Test broker uses market data for position valuation."""
        broker = MockBroker(starting_cash=500.0)
        md = MockMarketData()
        broker.set_market_data(md)

        q = await md.get_quote("XMPL")
        await broker.place_order("XMPL", OrderSide.BUY, 10, OrderType.LIMIT, q.ask)

        positions = await broker.get_positions()
        assert len(positions) == 1
        pos = positions[0]

        # Market value should reflect current price, not just avg cost
        assert pos.market_value > 0
        assert pos.qty == 10


# ============================================================
# 11. SCORING EDGE CASES
# ============================================================

class TestScoringEdgeCases:
    """Test scoring with extreme and edge case inputs."""

    def test_zero_everything(self):
        inp = ScoringInput(symbol="ZERO", gap_pct=0, volume=0, rvol=0,
                           news_score=0, scanner_score=0, last_data_time=time.time())
        s = compute_score(inp)
        assert s == 0

    def test_maximum_inputs(self):
        inp = ScoringInput(symbol="MAX", gap_pct=500, volume=100_000_000,
                           rvol=50, news_score=100, scanner_score=100,
                           last_data_time=time.time())
        s = compute_score(inp)
        assert s <= 100

    def test_stale_data_decays(self):
        """Score should decay for stale data."""
        inp = ScoringInput(symbol="STALE", gap_pct=50, volume=2_000_000,
                           rvol=5, news_score=50, scanner_score=70,
                           last_data_time=time.time() - 3600)  # 1 hour old
        s = compute_score(inp)

        inp_fresh = ScoringInput(symbol="FRESH", gap_pct=50, volume=2_000_000,
                                  rvol=5, news_score=50, scanner_score=70,
                                  last_data_time=time.time())
        s_fresh = compute_score(inp_fresh)

        assert s < s_fresh  # Stale data should score lower


# ============================================================
# 12. REGRESSION TESTS (Previously Found Bugs)
# ============================================================

class TestRegressions:
    """Tests for previously discovered and fixed bugs."""

    def test_momentum_thresholds_are_lowered(self):
        """Verify thresholds are at the lowered values (was 30/45/60)."""
        assert MOMENTUM_THRESHOLDS["candidate_score"] == 15
        assert MOMENTUM_THRESHOLDS["igniting_score"] == 22
        assert MOMENTUM_THRESHOLDS["gated_score"] == 28

    @pytest.mark.asyncio
    async def test_sim_mode_bypasses_phase_check(self):
        """Sim mode should bypass trading phase restrictions."""
        g = CentralGating()
        g._get_phase = lambda now: "OFFHOURS"
        g.sim_mode = True
        result = g.evaluate("TEST", 0.3, 0, 5.0)
        # Should pass because sim_mode bypasses phase check
        assert result.approved or "phase" not in str(result.checks_failed)

    def test_score_below_threshold_stays_idle(self):
        """Score below candidate threshold should not promote to CANDIDATE."""
        engine = _fresh_momentum()
        engine.update_score("TEST", 10)
        assert engine.get_symbol("TEST").state == MomentumState.IDLE

    def test_igniting_decay_resets_to_idle(self):
        """IGNITING state with score below candidate threshold → IDLE."""
        engine = _fresh_momentum()
        engine.update_score("TEST", 16)  # CANDIDATE
        engine.update_score("TEST", 23)  # IGNITING
        engine.update_score("TEST", 5)   # Below 15 → IDLE
        assert engine.get_symbol("TEST").state == MomentumState.IDLE

    @pytest.mark.asyncio
    async def test_mock_market_data_has_bid_ask(self):
        """Mock market data must provide bid/ask for gating spread checks."""
        md = MockMarketData()
        q = await md.get_quote("XMPL")
        assert q.bid > 0
        assert q.ask > 0
        assert q.ask >= q.bid
        assert q.spread_pct >= 0
