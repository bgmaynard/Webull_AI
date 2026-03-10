# Webull Trading Bot — Build Specification

## Purpose

Build a standalone HFT momentum scalper bot on a separate computer, connecting to the **Webull API** for broker operations. Architecture mirrors the proven Morpheus/IBKR ecosystem but is fully independent.

## Target Setup

| Component | Detail |
|-----------|--------|
| Broker | Webull (via `webull-python` or Webull OpenAPI) |
| Language | Python 3.11+ |
| Framework | FastAPI + uvicorn (single server, WebSocket + REST) |
| UI | React dashboard (Vite) |
| AI Assistant | Claude Code for development |
| OS | Windows 11 |
| Account | Paper trading first, then live |

---

## Architecture Overview

```
webull_trading_api.py (port 9100) — FastAPI + WebSocket
    |
    +-- webull_broker.py        (orders, positions, account)
    +-- webull_market_data.py   (quotes, streaming)
    +-- webull_auth.py          (OAuth/token management)
    |
    +-- ai/hft_scalper.py       (core trading engine)
    +-- ai/central_gating.py    (all trades go through here)
    +-- ai/momentum_engine.py   (FSM: IDLE→CANDIDATE→IGNITING→GATED→IN_POSITION→EXITING→COOLDOWN)
    |
    +-- scanner/                (symbol discovery — can poll external or use Webull screener)
    +-- worklist/               (capped watchlist with scoring + displacement)
    |
    +-- ui/trading/             (React dashboard)
```

---

## Phase 1: Foundation (Days 1-3)

### 1.1 Project Scaffold

```
webull_bot/
├── .env                        # API keys, secrets
├── .claude/CLAUDE.md           # Claude Code instructions
├── webull_trading_api.py       # Main FastAPI server
├── webull_broker.py            # Webull order/position abstraction
├── webull_market_data.py       # Webull quote/streaming
├── webull_auth.py              # Token management
├── ai/
│   ├── hft_scalper.py          # Core scalper engine
│   ├── central_gating.py       # Pre-trade gate checks
│   ├── momentum_engine.py      # Momentum state machine
│   ├── server_middleware.py    # Rate limiting, backpressure
│   └── scalper_config.json     # Runtime config (persisted)
├── worklist/
│   ├── store.py                # Worklist storage (capped)
│   ├── pipeline.py             # Intake → scrutiny → scoring
│   ├── scrutiny.py             # Filter: price, vol, RVOL, spread, gap, float
│   └── scoring.py              # Priority scoring (0-100)
├── reports/
│   └── YYYY-MM-DD/             # Daily trade ledger, entry blocks, shadow trades
├── state/                      # Persistent state files
├── data/                       # Open positions, trade journal
├── ui/trading/                 # React dashboard
├── tests/                      # pytest suite
├── tools/                      # Token status, validation scripts
└── startup/
    └── start_bot.ps1           # PowerShell startup script
```

### 1.2 Webull API Integration

```python
# webull_auth.py
# Webull uses OAuth2. Token must be refreshed periodically.
# Store token in tokens/webull_token.json
# NEVER commit tokens to git.

# Key endpoints (webull-python library or REST):
#   - Login / MFA
#   - Get account info
#   - Get positions
#   - Place order (LIMIT only for premarket)
#   - Cancel order
#   - Get order status
#   - Get quote / streaming quotes

# webull_broker.py — Unified broker interface:
class WebullBroker:
    async def get_positions(self) -> list[Position]
    async def get_account(self) -> AccountInfo
    async def place_order(self, symbol, side, qty, order_type, limit_price) -> OrderResult
    async def cancel_order(self, order_id) -> bool
    async def get_order_status(self, order_id) -> OrderStatus

# webull_market_data.py — Market data:
class WebullMarketData:
    async def get_quote(self, symbol) -> Quote
    async def get_quotes_batch(self, symbols) -> dict[str, Quote]
    async def subscribe_streaming(self, symbols, callback) -> None
```

### 1.3 Environment Variables (.env)

```
WEBULL_DEVICE_ID=your_device_id
WEBULL_EMAIL=your_email
WEBULL_PASSWORD=encrypted_or_env
WEBULL_TRADING_PIN=your_pin
WEBULL_ACCOUNT_TYPE=paper  # paper or live
BOT_PORT=9100
```

---

## Phase 2: Trading Engine (Days 3-5)

### 2.1 HFT Scalper Parameters

```json
{
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
  "use_symbol_circuit_breaker": true,
  "hard_stop_limit_per_symbol": 3,
  "lean_mode": true
}
```

### 2.2 Scalper Exit Logic (Priority Order)

1. **Hard Stop (adaptive 1-3%)** — Capital protection, spread+volatility-aware
2. **Profit target hit** — Start trailing from high
3. **Trail drops from high** — Exit with profit locked
4. **Momentum decay** — FSM detects fading momentum
5. **Max hold time (300s)** — Exit if flat/down, let winners run
6. **Emergency liquidate** — On scalper disable/crash

### 2.3 Momentum State Machine (8 States)

```
IDLE → CANDIDATE → IGNITING → GATED → IN_POSITION → MONITORING → EXITING → COOLDOWN
                                                                              ↓
                                                                          IDLE (reset)
```

- Grid-searched thresholds: 30/45/60
- Ownership tracking per state
- State transitions emit events

### 2.4 Central Gating (All Trades Must Pass)

```python
# Every trade attempt goes through central_gating.py
# Checks (fail-open on error for paper, fail-closed for live):
#   1. Kill switch not active
#   2. Trading phase is LIVE (07:00-09:15 ET)
#   3. Symbol not halted
#   4. Symbol not on blacklist
#   5. Spread <= 1%
#   6. Position limit not exceeded
#   7. Session trade cap not exceeded
#   8. Symbol circuit breaker not triggered
#   9. Risk dollar cap not exceeded
```

---

## Phase 3: Premarket Momentum Mode (Days 5-7)

### 3.1 Trading Clock (Eastern Time)

| Phase | Time (ET) | Behavior |
|-------|-----------|----------|
| DISCOVERY | 04:00-07:00 | Scan + score, NO trades |
| LIVE | 07:00-09:15 | Limit orders allowed |
| EXIT_ONLY | 09:15-09:30 | Close positions only |
| SHADOW | 09:30-16:00 | Simulate trades, log for research |
| OFFHOURS | 16:00-04:00 | Nothing |

### 3.2 Scrutiny Filters (Symbol Intake)

```python
ScrutinyConfig(
    min_price=2.00,          # $2 minimum
    max_price=20.00,         # $20 maximum
    min_volume=500_000,      # 500K premarket volume
    min_rvol=3.0,            # 3x relative volume
    max_spread_pct=1.0,      # 1% max spread
    min_scanner_score=50.0,  # Minimum quality score
    min_dollar_volume=100_000,
    max_float_millions=20.0, # Float <= 20M shares
    min_gap_pct=30.0,        # Gap >= 30%
)
```

### 3.3 Scoring Weights

```python
ScoringConfig(
    weight_gap=0.35,             # Gap % is king
    weight_volume=0.25,          # Volume conviction
    weight_rvol=0.15,            # RVOL momentum quality
    weight_news=0.15,            # News catalyst
    weight_scanner_score=0.10,   # Scanner quality
)
```

### 3.4 Worklist (Capped at 15)

- Hard cap: 15 active symbols max
- Displacement: new symbol must beat lowest scorer to enter
- Rescore every 5 min with time-decay (2%/min after 10 min stale)
- Force-add uses full pipeline (no score bypass)

---

## Phase 4: Infrastructure (Days 7-10)

### 4.1 Data Persistence

| File | Purpose |
|------|---------|
| `reports/{date}/trade_ledger.jsonl` | Append-only trade log |
| `reports/{date}/entry_blocks.jsonl` | Blocked trade audit |
| `reports/{date}/shadow_trades.jsonl` | Simulated trades (SHADOW phase) |
| `data/open_positions.json` | Atomic position writes |
| `state/blacklist_rehab.json` | Auto-rehab blacklisted symbols |

### 4.2 Event System

All decisions recorded as events (not freeform logs):
- `Event(event_id, event_type, timestamp, payload, symbol, trade_id)`
- Event types: SIGNAL_CANDIDATE, META_APPROVED, RISK_APPROVED, ORDER_SUBMITTED, ORDER_FILL_RECEIVED, EXECUTION_BLOCKED, etc.
- Events are source of truth for replay and audit

### 4.3 API Endpoints

```
# Core
GET  /api/health
GET  /api/status
GET  /api/positions
GET  /api/worklist
GET  /api/price/{symbol}

# Scalper
GET  /api/scalper/status
POST /api/scalper/start
POST /api/scalper/stop
POST /api/scalper/enable
POST /api/scalper/disable
GET  /api/scalper/config
POST /api/scalper/config
GET  /api/scalper/watchlist
GET  /api/scalper/trades

# Reports
GET  /api/reports/eod/today
GET  /api/reports/eod/{date}
```

### 4.4 WebSocket

Single WebSocket endpoint for real-time UI updates:
- Position updates
- Worklist changes
- Signal events
- Quote updates
- Use per-client stats + adaptive degrade for high load

---

## Critical Patterns (Learned from Production)

### DO:
- **Singleton pattern** for engine instances (`get_*()` with global `_instance`)
- **Fail-open gates** in paper mode (allow trade on gate error)
- **Limit orders only** in premarket (never market orders)
- **Dedicated thread pools** — separate executor for API calls vs file I/O
- **Heartbeat in dedicated daemon thread** — never couple to main processing loop
- **Emergency liquidation** — idempotent via `threading.Lock`, two chokepoints
- **Atomic file writes** — write to temp, then rename (prevents corruption)
- **UTF-8 encoding** on all file operations (Windows requirement)
- **ASCII-only in logger calls** — Windows `charmap` codec crashes on unicode
- **`asyncio.to_thread()`** for all file I/O — never block the event loop
- **Circuit breaker per symbol** — 3 hard stops = blocked for 30 min

### DON'T:
- **HTTP self-calls** — code inside FastAPI must NOT call `localhost:9100/api/...`, use direct imports
- **`httpx.AsyncClient`** across event loop restarts — it binds to creating loop, deadlocks after restart. Use `urllib.request` in executor instead
- **Default thread pool** (`asyncio.to_thread`) for everything — it gets exhausted. Use named executors
- **Persist `enabled` field** to config file — causes poison restart cycle
- **`timeout` command** in batch scripts — hangs in minimized windows
- **Market orders** in premarket — slippage kills edge
- **Amend commits** after hook failure — creates data loss. Always new commit

### Config Gotcha:
Running scalper saves config to `scalper_config.json` periodically, **overwriting file edits**. Always update config via API endpoint, never edit the file directly.

---

## Phase 5: React Dashboard (Days 10-12)

### Tech Stack
- Vite + React + TypeScript
- Single polling manager (1-second tick loop, endpoint dedup)
- Tab visibility pause (no fetches when hidden)
- WebSocket for real-time push (positions, worklist)

### Key Components
- **Positions Panel** — open trades with P&L
- **Watchlist Panel** — scored symbols with momentum state
- **Quote Panel** — live bid/ask/spread
- **Chart Panel** — price candles
- **Controls** — start/stop/enable/disable scalper
- **EOD Report** — daily performance summary

---

## Phase 6: Validation & Go-Live

### Paper Trading Checklist
- [ ] Bot starts and connects to Webull paper account
- [ ] Quotes streaming correctly
- [ ] Worklist populates from scanner
- [ ] Scrutiny filters reject bad symbols
- [ ] Trading clock enforces phases correctly
- [ ] Limit orders place and fill in paper
- [ ] Exit logic fires (hard stop, trail, max hold)
- [ ] Position persistence survives restart
- [ ] EOD report generates with trade data
- [ ] Shadow trades log during RTH
- [ ] Circuit breaker triggers after 3 hard stops
- [ ] Emergency liquidation works on disable

### Performance Targets (Paper)
- 50+ probe trades before considering live
- Win rate > 45%
- Profit factor > 1.5
- No system crashes or data loss

---

## Webull-Specific Notes

1. **Webull premarket hours**: 4:00 AM - 9:30 AM ET (earlier than Schwab's 7:00 AM)
   - This gives a wider LIVE window if desired
   - Can keep 07:00-09:15 or expand to 04:00-09:15

2. **Webull API options**:
   - `webull-python` (unofficial, community): pip install webull
   - Webull OpenAPI (official, newer): requires developer application
   - Both support paper trading

3. **Rate limits**: Webull has undocumented rate limits. Implement backoff.

4. **Streaming**: Webull provides WebSocket streaming for quotes. Use it instead of REST polling.

5. **Order types**: Webull supports LIMIT, MARKET, STOP, STOP_LIMIT in premarket.

6. **MFA**: Webull requires MFA on login. Token caching is critical to avoid repeated MFA prompts.

---

## CLAUDE.md Template for New Bot

When setting up Claude Code on the new machine, create `.claude/CLAUDE.md` with:
- Architecture overview (copy relevant sections above)
- File map (what each file does)
- API endpoints
- Trading parameters
- Critical patterns (DO/DON'T lists)
- Webull-specific gotchas
- Current scalper config JSON
- Bot ecosystem context (if connecting to shared services)

This gives Claude Code full context to develop, debug, and extend the bot autonomously.

---

## Estimated Timeline

| Phase | Duration | Deliverable |
|-------|----------|-------------|
| 1. Foundation | 3 days | Server + Webull auth + broker abstraction |
| 2. Engine | 2 days | Scalper + gating + momentum FSM |
| 3. Premarket | 2 days | Trading clock + filters + scoring |
| 4. Infrastructure | 3 days | Events + persistence + API + watchdog |
| 5. Dashboard | 2 days | React UI with polling + WebSocket |
| 6. Validation | Ongoing | Paper trading + tuning |

Total: ~12 days to functional paper trading bot.
