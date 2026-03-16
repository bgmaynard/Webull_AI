"""Trading Bot — Main FastAPI server.

Broker-agnostic. Reads BROKER_PROVIDER and MARKET_DATA_PROVIDER from env.
Single server handling REST + WebSocket on port 9100.
"""

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime

import pytz
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

from core.registry import get_broker, get_market_data, get_provider_name, is_sim_mode, wire_sim_market_data
from ai.hft_scalper import get_scalper
from ai.central_gating import get_gating
from ai.momentum_engine import MomentumState, get_momentum_engine
from ai.server_middleware import RateLimitMiddleware, BackpressureMiddleware
from worklist.store import get_worklist_store
from worklist.pipeline import get_pipeline
from ai.event_system import get_event_system
from ai.reports import get_reports
from ai.watchdog import get_watchdog

# --- Logging (ASCII-only for Windows) ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("trading_bot")

ET = pytz.timezone("US/Eastern")


# --- WebSocket Manager ---
class ConnectionManager:
    """Manages WebSocket connections for real-time UI updates."""

    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
        logger.info("WebSocket client connected (%d total)", len(self.active))

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        logger.info("WebSocket client disconnected (%d total)", len(self.active))

    async def broadcast(self, message: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


ws_manager = ConnectionManager()


# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    provider = get_provider_name()
    logger.info("Trading Bot starting... (broker=%s)", provider)

    if is_sim_mode():
        logger.info("Running in SIMULATION mode -- mock broker + market data active")
        get_gating().sim_mode = True
        get_momentum_engine(sim_mode=True)
        # Wire mock market data to mock broker
        wire_sim_market_data(get_broker(), get_market_data())

    elif provider == "webull":
        from webull_auth import get_auth
        auth = get_auth()
        if auth.login():
            logger.info("Webull authentication successful (mode=%s)", auth.account_type)
        else:
            logger.warning("Webull authentication failed - bot will run with limited functionality")

    elif provider == "alpaca":
        # Alpaca uses API key auth -- no login step needed
        acct = await get_broker().get_account()
        logger.info("Alpaca connected (account=%s, equity=$%.2f)", acct.account_id[:8], acct.net_liquidation)

        # Set gating account_type for paper trading (expanded trading window)
        if os.getenv("ALPACA_PAPER", "true").lower() == "true":
            get_gating().account_type = "paper"
            logger.info("Gating account_type set to 'paper' (expanded trading window)")

    # Auto-start the worklist/scanner pipeline
    try:
        pipeline = get_pipeline()
        await pipeline.start()
        logger.info("Scanner pipeline auto-started")
    except Exception as e:
        logger.warning("Failed to auto-start scanner pipeline: %s", e)

    # Start watchdog
    watchdog = get_watchdog()
    await watchdog.start()

    # Auto-enable and start the scalper
    try:
        scalper = get_scalper()
        scalper.enable()
        await scalper.start()
        logger.info("Scalper auto-enabled and started")
    except Exception as e:
        logger.warning("Failed to auto-start scalper: %s", e)

    yield

    # Shutdown — Mar 12: proper cleanup sequence
    logger.info("Trading Bot shutting down...")

    # 1. Stop watchdog first (stop monitoring)
    await watchdog.stop()

    # 2. Stop scalper (persists positions to disk)
    try:
        scalper = get_scalper()
        if scalper.running:
            await scalper.stop()
            logger.info("Scalper stopped, positions persisted")
    except Exception as e:
        logger.error("Error stopping scalper: %s", e)

    # 3. Stop scanner pipeline
    try:
        pipeline = get_pipeline()
        await pipeline.stop()
    except Exception as e:
        logger.error("Error stopping pipeline: %s", e)

    # 4. Flush all remaining events to disk
    await get_event_system().flush()
    logger.info("Shutdown complete")


# --- App ---
app = FastAPI(
    title="Trading Bot",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware, requests_per_second=20, burst=50)
app.add_middleware(BackpressureMiddleware, max_concurrent=100)


# --- Auth (Webull-specific, gated) ---
@app.get("/api/auth/status")
async def auth_status():
    if get_provider_name() != "webull":
        return {"provider": get_provider_name(), "message": "No auth required for this broker"}
    from webull_auth import get_auth
    auth = get_auth()
    return auth.get_login_status()


@app.post("/api/auth/login")
async def auth_login():
    if get_provider_name() != "webull":
        return {"success": True, "message": "No login required for this broker"}
    from webull_auth import get_auth
    auth = get_auth()
    success = await asyncio.to_thread(auth.login)
    return {"success": success, "status": auth.get_login_status()}


@app.post("/api/auth/mfa/request")
async def auth_mfa_request():
    if get_provider_name() != "webull":
        return {"sent": False, "message": "MFA not applicable for this broker"}
    from webull_auth import get_auth
    auth = get_auth()
    sent = await asyncio.to_thread(auth.request_mfa)
    return {"sent": sent, "message": "Check your email/phone for the MFA code" if sent else "Failed to request MFA"}


@app.post("/api/auth/mfa/submit")
async def auth_mfa_submit(code: str):
    if get_provider_name() != "webull":
        return {"success": False, "message": "MFA not applicable for this broker"}
    from webull_auth import get_auth
    auth = get_auth()
    success = await asyncio.to_thread(auth.login_with_mfa, code)
    return {"success": success, "status": auth.get_login_status()}


@app.post("/api/auth/refresh")
async def auth_refresh():
    if get_provider_name() != "webull":
        return {"success": True, "message": "No token refresh needed for this broker"}
    from webull_auth import get_auth
    auth = get_auth()
    success = await asyncio.to_thread(auth.refresh_token)
    return {"success": success}


@app.post("/api/auth/logout")
async def auth_logout():
    if get_provider_name() != "webull":
        return {"logged_out": True}
    from webull_auth import get_auth
    auth = get_auth()
    auth.clear_token()
    return {"logged_out": True}


# --- Health & Status ---
@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(ET).isoformat()}


@app.get("/api/status")
async def status():
    now = datetime.now(ET)
    provider = get_provider_name()

    base = {
        "server": "trading_bot",
        "version": "0.2.0",
        "broker_provider": provider,
        "market_data_provider": os.getenv("MARKET_DATA_PROVIDER", "sim").lower(),
        "time_et": now.strftime("%H:%M:%S"),
        "trading_phase": _get_trading_phase(now),
    }

    if provider == "sim":
        base.update({
            "mode": "SIMULATION",
            "authenticated": True,
            "account_type": "simulation",
        })
    elif provider == "alpaca":
        base.update({
            "mode": "LIVE" if os.getenv("ALPACA_PAPER", "true").lower() != "true" else "PAPER",
            "authenticated": True,
            "account_type": "paper" if os.getenv("ALPACA_PAPER", "true").lower() == "true" else "live",
        })
    elif provider == "webull":
        from webull_auth import get_auth
        auth = get_auth()
        base.update({
            "mode": "LIVE",
            "authenticated": auth.is_logged_in,
            "account_type": auth.account_type,
        })

    return base


@app.get("/api/sim/universe")
async def sim_universe():
    if not is_sim_mode():
        return {"error": "Not in simulation mode"}
    md = get_market_data()
    if hasattr(md, "get_universe"):
        return {"stocks": md.get_universe()}
    return {"stocks": []}


@app.get("/api/sim/stats")
async def sim_stats():
    if not is_sim_mode():
        return {"error": "Not in simulation mode"}
    broker = get_broker()
    if hasattr(broker, "get_sim_stats"):
        return broker.get_sim_stats()
    return {}


# --- Account & Positions ---
@app.get("/api/positions")
async def positions():
    broker = get_broker()
    pos_list = await broker.get_positions()
    return {
        "positions": [
            {
                "symbol": p.symbol,
                "qty": p.qty,
                "avg_cost": p.avg_cost,
                "market_value": p.market_value,
                "unrealized_pnl": p.unrealized_pnl,
                "current_price": p.current_price,
            }
            for p in pos_list
        ],
        "count": len(pos_list),
    }


@app.get("/api/account")
async def account():
    broker = get_broker()
    info = await broker.get_account()
    return {
        "account_id": info.account_id,
        "net_liquidation": info.net_liquidation,
        "buying_power": info.buying_power,
        "cash": info.cash,
        "day_trades_remaining": info.day_trades_remaining,
        "account_type": info.account_type,
    }


# --- Market Data ---
@app.get("/api/price/{symbol}")
async def price(symbol: str):
    md = get_market_data()
    quote = await md.get_quote(symbol.upper())
    # Real spread only when bid/ask differ meaningfully (not synthetic)
    has_real_spread = quote.bid > 0 and quote.ask > 0 and abs(quote.ask - quote.bid) > 0.001
    return {
        "symbol": quote.symbol,
        "price": quote.price,
        "bid": quote.bid,
        "ask": quote.ask,
        "spread": round(quote.spread, 4) if has_real_spread else None,
        "spread_pct": round(quote.spread_pct, 2) if has_real_spread else None,
        "volume": quote.volume if quote.volume > 0 else None,
        "change_pct": round(quote.change_pct, 2),
        "prev_close": quote.prev_close,
    }


# --- Worklist ---
@app.get("/api/worklist")
async def worklist():
    store = get_worklist_store()
    return {"symbols": store.to_list(), "count": store.count, "max_size": store.max_size}


@app.post("/api/worklist/add/{symbol}")
async def worklist_add(symbol: str):
    from worklist.scoring import ScoringInput
    from data.finnhub import get_finnhub_news
    import time as _time

    symbol = symbol.upper()
    store = get_worklist_store()

    # Manual add: get quote for scoring, skip scrutiny filters
    md = get_market_data()
    quote = await md.get_quote(symbol)

    gap_pct = quote.change_pct if quote and quote.change_pct > 0 else 0
    volume = quote.volume if quote else 0

    # Manual adds get boosted scores — user intent overrides scanner thresholds.
    # If market data is unavailable (after hours), use reasonable defaults.
    rvol = max(3.0, gap_pct / 10) if gap_pct > 0 else 3.0
    # Base scanner + news score to compensate for missing data sources
    scanner_score = max(70.0, min(100.0, gap_pct + 50)) if gap_pct > 0 else 70.0
    # Fetch live news score from Finnhub (falls back to 50 on failure)
    try:
        news_score = await get_finnhub_news().get_news_score(symbol)
        if news_score == 0.0:
            news_score = 50.0  # Default if no articles found
    except Exception:
        logger.warning("Finnhub news fetch failed for %s, using default", symbol)
        news_score = 50.0

    scoring_input = ScoringInput(
        symbol=symbol,
        gap_pct=gap_pct,
        volume=volume,
        rvol=rvol,
        scanner_score=scanner_score,
        news_score=news_score,
        last_data_time=_time.time(),
    )

    success = store.add(symbol, scoring_input, source="manual")
    if success:
        return {"added": True, "symbol": symbol, "worklist": store.to_list()}
    return {"added": False, "symbol": symbol, "message": "Could not add -- score too low to displace existing symbols"}


@app.delete("/api/worklist/remove/{symbol}")
async def worklist_remove(symbol: str):
    store = get_worklist_store()
    removed = store.remove(symbol.upper())
    return {"removed": removed, "symbol": symbol.upper(), "count": store.count}


@app.post("/api/worklist/rescore")
async def worklist_rescore():
    store = get_worklist_store()
    store.rescore_all()
    return {"rescored": True, "symbols": store.to_list(), "count": store.count}


@app.get("/api/worklist/pipeline/stats")
async def worklist_pipeline_stats():
    pipeline = get_pipeline()
    return pipeline.get_stats()


@app.post("/api/worklist/pipeline/start")
async def worklist_pipeline_start():
    pipeline = get_pipeline()
    await pipeline.start()
    return {"running": True}


@app.post("/api/worklist/pipeline/stop")
async def worklist_pipeline_stop():
    pipeline = get_pipeline()
    await pipeline.stop()
    return {"running": False}


class BulkAddRequest(BaseModel):
    symbols: list[str]


@app.get("/api/worklist/enriched")
async def worklist_enriched():
    """Return worklist symbols enriched with live market data, momentum state, and news."""
    from worklist.scoring import ScoringInput, score_with_breakdown
    from data.finnhub import get_finnhub_news

    store = get_worklist_store()
    md = get_market_data()
    momentum = get_momentum_engine()
    finnhub = get_finnhub_news()

    entries = store.get_all()
    enriched = []

    # Fetch fresh quotes for all worklist symbols in one batch call
    symbols = [e.symbol for e in entries]
    quotes = await md.get_quotes_batch(symbols) if symbols else {}

    for entry in entries:
        base = entry.to_dict()

        # Market data from batch fetch
        quote = quotes.get(entry.symbol)
        if quote and quote.price > 0:
            base["price"] = quote.price
            base["change_pct"] = round(quote.change_pct, 2)
            base["bid"] = quote.bid
            base["ask"] = quote.ask
            base["spread_pct"] = round(quote.spread_pct, 2)
            base["volume"] = quote.volume
        else:
            base["price"] = None
            base["change_pct"] = None
            base["bid"] = None
            base["ask"] = None
            base["spread_pct"] = None
            base["volume"] = None

        # Momentum FSM state
        sm = momentum.get_symbol(entry.symbol)
        base["momentum_state"] = sm.state.value
        base["momentum_score"] = round(sm.score, 1)

        # News data (cached internally by Finnhub)
        try:
            news_score = await finnhub.get_news_score(entry.symbol)
            articles = await finnhub.get_news(entry.symbol)
            base["news_score"] = round(news_score, 1)
            base["news_count"] = len(articles)
        except Exception:
            base["news_score"] = None
            base["news_count"] = 0

        # Score breakdown from scoring engine
        if entry.scoring_input:
            _, breakdown = score_with_breakdown(entry.scoring_input)
            base["score_breakdown"] = breakdown
        else:
            base["score_breakdown"] = None

        enriched.append(base)

    return {"symbols": enriched, "count": len(enriched), "max_size": store.max_size}


@app.post("/api/worklist/add-bulk")
async def worklist_add_bulk(req: BulkAddRequest):
    """Add multiple symbols to the worklist at once."""
    from worklist.scoring import ScoringInput
    from data.finnhub import get_finnhub_news
    import time as _time

    store = get_worklist_store()
    md = get_market_data()
    results = []

    for raw_symbol in req.symbols:
        symbol = raw_symbol.upper()
        try:
            quote = await md.get_quote(symbol)
            gap_pct = quote.change_pct if quote and quote.change_pct > 0 else 0
            volume = quote.volume if quote else 0

            rvol = max(3.0, gap_pct / 10) if gap_pct > 0 else 3.0
            scanner_score = max(70.0, min(100.0, gap_pct + 50)) if gap_pct > 0 else 70.0

            try:
                news_score = await get_finnhub_news().get_news_score(symbol)
                if news_score == 0.0:
                    news_score = 50.0
            except Exception:
                news_score = 50.0

            scoring_input = ScoringInput(
                symbol=symbol,
                gap_pct=gap_pct,
                volume=volume,
                rvol=rvol,
                scanner_score=scanner_score,
                news_score=news_score,
                last_data_time=_time.time(),
            )

            added = store.add(symbol, scoring_input, source="manual_bulk")
            results.append({"symbol": symbol, "added": added})

        except Exception as e:
            logger.warning("Bulk add failed for %s: %s", symbol, e)
            results.append({"symbol": symbol, "added": False, "error": str(e)})

    return {"results": results, "count": store.count}


# --- Scalper ---
@app.get("/api/scalper/status")
async def scalper_status():
    scalper = get_scalper()
    return scalper.get_status()


@app.post("/api/scalper/start")
async def scalper_start():
    scalper = get_scalper()
    success = await scalper.start()
    return {"success": success, "status": scalper.get_status()}


@app.post("/api/scalper/stop")
async def scalper_stop():
    scalper = get_scalper()
    success = await scalper.stop()
    return {"success": success, "status": scalper.get_status()}


@app.post("/api/scalper/enable")
async def scalper_enable():
    scalper = get_scalper()
    scalper.enable()
    return {"enabled": True}


@app.post("/api/scalper/disable")
async def scalper_disable():
    scalper = get_scalper()
    scalper.disable()
    return {"enabled": False, "message": "Scalper disabled. Positions will be liquidated if running."}


@app.get("/api/scalper/config")
async def scalper_config_get():
    scalper = get_scalper()
    return scalper.config


@app.post("/api/scalper/config")
async def scalper_config_update(updates: dict):
    scalper = get_scalper()
    return scalper.update_config(updates)


@app.post("/api/scalper/reset-session")
async def scalper_reset_session():
    """Reset session counters and trade history for new trading day."""
    scalper = get_scalper()
    scalper._completed_trades.clear()
    get_gating().reset_session()
    logger.info("Session reset: trade history cleared, gating counters reset")
    return {"success": True, "session_trades": 0}


@app.get("/api/scalper/history")
async def scalper_history():
    scalper = get_scalper()
    return {
        "trades": scalper.get_trade_history(),
        "summary": scalper.get_session_pnl(),
    }


@app.get("/api/scalper/trades")
async def scalper_trades():
    scalper = get_scalper()
    return {"trades": scalper.get_trades(), "count": len(scalper.open_trades)}


@app.get("/api/scalper/watchlist")
async def scalper_watchlist():
    momentum = get_momentum_engine()
    active = momentum.get_all_active()
    return {
        "symbols": [
            {
                "symbol": sm.symbol,
                "state": sm.state.value,
                "score": round(sm.score, 1),
                "time_in_state": round(sm.time_in_state, 1),
            }
            for sm in active
        ],
        "count": len(active),
    }


@app.post("/api/scalper/kill-switch")
async def kill_switch(activate: bool = True):
    gating = get_gating()
    if activate:
        gating.activate_kill_switch()
        scalper = get_scalper()
        await scalper.emergency_liquidate("kill switch")
        return {"kill_switch": True, "message": "Kill switch activated. All positions liquidated."}
    else:
        gating.deactivate_kill_switch()
        return {"kill_switch": False, "message": "Kill switch deactivated."}


@app.get("/api/scalper/circuit-breakers")
async def circuit_breakers():
    gating = get_gating()
    return gating.get_circuit_breaker_status()


# --- Optimizer ---

@app.post("/api/optimizer/run")
async def optimizer_run(population: int = 50, generations: int = 100,
                        use_bars: bool = True, date: str | None = None,
                        trade_file: str | None = None):
    from ai.optimizer import get_optimizer
    opt = get_optimizer()
    if opt.running:
        return {"error": "Optimizer already running", "progress": opt.progress}
    # Run in background task
    opt._task = asyncio.create_task(opt.run(
        trade_file=trade_file, date=date,
        population_size=population, generations=generations, use_bars=use_bars,
    ))
    return {"started": True, "population": population, "generations": generations,
            "date": date or "today"}


@app.get("/api/optimizer/status")
async def optimizer_status():
    from ai.optimizer import get_optimizer
    opt = get_optimizer()
    return {"running": opt.running, "progress": opt.progress}


@app.get("/api/optimizer/results")
async def optimizer_results():
    from ai.optimizer import get_optimizer
    opt = get_optimizer()
    if opt.latest_result:
        return opt.latest_result
    return {"error": "No optimization results available. Run POST /api/optimizer/run first."}


@app.post("/api/optimizer/apply")
async def optimizer_apply():
    from ai.optimizer import get_optimizer
    opt = get_optimizer()
    applied = opt.apply_best()
    if applied:
        return {"applied": True, "config": applied}
    return {"applied": False, "error": "No results to apply"}


# --- Reports ---

def _build_report_from_trades(trades: list[dict]) -> dict:
    """Build an EOD-style report from in-memory trade history."""
    from collections import defaultdict
    from datetime import datetime
    import pytz

    date_str = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d")
    winners = [t for t in trades if t.get("pnl", 0) > 0]
    losers = [t for t in trades if t.get("pnl", 0) <= 0]
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    gross_profit = sum(t["pnl"] for t in winners)
    gross_loss = abs(sum(t["pnl"] for t in losers))
    win_rate = (len(winners) / len(trades) * 100) if trades else 0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else ("inf" if gross_profit > 0 else 0)
    avg_hold = (sum(t.get("hold_seconds", 0) for t in trades) / len(trades)) if trades else 0

    exit_reasons = defaultdict(int)
    for t in trades:
        reason = t.get("exit_reason", "unknown").split("(")[0].strip()
        exit_reasons[reason] += 1

    symbols = list(set(t.get("symbol", "") for t in trades))

    return {
        "date": date_str,
        "total_trades": len(trades),
        "winners": len(winners),
        "losers": len(losers),
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(profit_factor, 2) if isinstance(profit_factor, float) else profit_factor,
        "avg_hold_seconds": round(avg_hold, 1),
        "exit_reasons": dict(exit_reasons),
        "symbols_traded": symbols,
        "blocked_trades": 0,
        "shadow_trades": 0,
        "total_events": len(trades),
    }


@app.get("/api/reports/eod/today")
async def eod_report_today():
    reports = get_reports()
    report = await reports.generate_eod()

    # Fallback: build from in-memory trade history if event ledger is empty
    if report.get("total_trades", 0) == 0:
        scalper = get_scalper()
        trades = scalper.get_trade_history()
        if trades:
            report = _build_report_from_trades(trades)

    return report


@app.get("/api/reports/eod/{date}")
async def eod_report(date: str):
    reports = get_reports()
    saved = await reports.get_saved_report(date)
    if saved:
        return saved
    return await reports.generate_eod(date)


@app.get("/api/events/today")
async def events_today():
    es = get_event_system()
    events = await es.get_today_events()
    return {"events": events, "count": len(events)}


@app.get("/api/events/{date}")
async def events_by_date(date: str):
    es = get_event_system()
    events = await es.get_events_for_date(date)
    return {"events": events, "count": len(events)}


@app.post("/api/events/flush")
async def events_flush():
    es = get_event_system()
    await es.flush()
    return {"flushed": True}


# --- Watchdog ---
@app.get("/api/watchdog/status")
async def watchdog_status():
    wd = get_watchdog()
    return wd.get_status()


# --- WebSocket ---
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)


# --- Helpers ---
def _get_trading_phase(now: datetime) -> str:
    """Determine current trading phase based on ET time."""
    hour, minute = now.hour, now.minute
    t = hour * 60 + minute

    if t < 240:       # before 04:00
        return "OFFHOURS"
    elif t < 420:      # 04:00 - 07:00
        return "DISCOVERY"
    elif t < 555:      # 07:00 - 09:15
        return "LIVE"
    elif t < 570:      # 09:15 - 09:30
        return "EXIT_ONLY"
    elif t < 960:      # 09:30 - 16:00
        return "SHADOW"
    else:              # after 16:00
        return "OFFHOURS"


# --- Entry Point ---
if __name__ == "__main__":
    port = int(os.getenv("BOT_PORT", "9100"))
    uvicorn.run(
        "webull_trading_api:app",
        host="0.0.0.0",
        port=port,
        reload=False,
        log_level="info",
    )
