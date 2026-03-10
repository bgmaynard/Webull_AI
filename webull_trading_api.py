"""Webull Trading Bot — Main FastAPI server.

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
from fastapi.middleware.cors import CORSMiddleware

from webull_auth import get_auth
from webull_broker import get_broker
from webull_market_data import get_market_data
from ai.hft_scalper import get_scalper
from ai.central_gating import get_gating
from ai.momentum_engine import MomentumState, get_momentum_engine
from ai.server_middleware import RateLimitMiddleware, BackpressureMiddleware
from worklist.store import get_worklist_store
from worklist.pipeline import get_pipeline
from ai.event_system import get_event_system
from ai.reports import get_reports
from ai.watchdog import get_watchdog

load_dotenv()

# --- Logging (ASCII-only for Windows) ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("webull_bot")

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
    logger.info("Webull Trading Bot starting...")

    # Login to Webull
    auth = get_auth()
    if auth.login():
        logger.info("Webull authentication successful (mode=%s)", auth.account_type)
    else:
        logger.warning("Webull authentication failed - bot will run with limited functionality")

    # Start watchdog
    watchdog = get_watchdog()
    await watchdog.start()

    yield

    # Shutdown
    await watchdog.stop()
    await get_event_system().flush()
    logger.info("Webull Trading Bot shutting down...")


# --- App ---
app = FastAPI(
    title="Webull Trading Bot",
    version="0.1.0",
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


# --- Health & Status ---
@app.get("/api/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(ET).isoformat()}


@app.get("/api/status")
async def status():
    auth = get_auth()
    now = datetime.now(ET)
    return {
        "server": "webull_trading_bot",
        "version": "0.1.0",
        "authenticated": auth.is_logged_in,
        "account_type": auth.account_type,
        "time_et": now.strftime("%H:%M:%S"),
        "trading_phase": _get_trading_phase(now),
    }


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
    return {
        "symbol": quote.symbol,
        "price": quote.price,
        "bid": quote.bid,
        "ask": quote.ask,
        "spread": round(quote.spread, 4),
        "spread_pct": round(quote.spread_pct, 2),
        "volume": quote.volume,
        "change_pct": round(quote.change_pct, 2),
    }


# --- Worklist ---
@app.get("/api/worklist")
async def worklist():
    store = get_worklist_store()
    return {"symbols": store.to_list(), "count": store.count, "max_size": store.max_size}


@app.post("/api/worklist/add/{symbol}")
async def worklist_add(symbol: str):
    pipeline = get_pipeline()
    success = await pipeline.process_single(symbol.upper())
    store = get_worklist_store()
    if success:
        return {"added": True, "symbol": symbol.upper(), "worklist": store.to_list()}
    return {"added": False, "symbol": symbol.upper(), "message": "Symbol did not pass scrutiny filters"}


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


# --- Reports ---
@app.get("/api/reports/eod/today")
async def eod_report_today():
    reports = get_reports()
    return await reports.generate_eod()


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
            # Keep connection alive, listen for client messages
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
