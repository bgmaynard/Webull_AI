# CLAUDE.md — Webull Trading Bot

## Project Overview

HFT momentum scalper bot for Webull premarket trading. Python 3.11+ backend (FastAPI), React/TypeScript dashboard, paper trading first.

## Architecture

```
webull_trading_api.py (port 9300) — FastAPI + WebSocket
    |
    +-- webull_broker.py        (orders, positions, account)
    +-- webull_market_data.py   (quotes, batch quotes, premarket gainers)
    +-- webull_auth.py          (OAuth/token management, MFA, token caching)
    |
    +-- ai/hft_scalper.py       (core scalper engine — entry, exit, position sizing)
    +-- ai/central_gating.py    (9-check pre-trade gate, circuit breakers)
    +-- ai/momentum_engine.py   (8-state FSM per symbol)
    +-- ai/event_system.py      (structured events, JSONL ledger)
    +-- ai/persistence.py       (atomic JSON file I/O)
    +-- ai/reports.py           (EOD report generation)
    +-- ai/watchdog.py          (health monitoring)
    +-- ai/server_middleware.py  (rate limiting, backpressure)
    +-- ai/scalper_config.json  (runtime config — update via API only!)
    |
    +-- worklist/scrutiny.py    (8 intake filters)
    +-- worklist/scoring.py     (weighted composite 0-100 with time decay)
    +-- worklist/store.py       (capped worklist, displacement logic)
    +-- worklist/pipeline.py    (scanner -> scrutiny -> scoring -> store)
    |
    +-- ui/trading/             (React + Vite + TypeScript dashboard)
```

## Development Setup

```bash
# Backend
pip install -r requirements.txt
cp .env.example .env  # Fill in credentials
python webull_trading_api.py

# Frontend
cd ui/trading && npm install && npm run dev
```

### Testing

```bash
python -m pytest tests/ -v       # 96 tests
cd ui/trading && npm run build   # TypeScript type check + build
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `WEBULL_DEVICE_ID` | Webull device ID |
| `WEBULL_EMAIL` | Webull account email |
| `WEBULL_PASSWORD` | Webull password |
| `WEBULL_TRADING_PIN` | Trading PIN |
| `WEBULL_ACCOUNT_TYPE` | `paper` or `live` |
| `BOT_PORT` | Server port (default 9300) |

## API Endpoints

### Core
- `GET /api/health` `GET /api/status` `GET /api/account` `GET /api/positions` `GET /api/price/{symbol}`

### Scalper
- `GET /api/scalper/status` `POST /api/scalper/start` `POST /api/scalper/stop`
- `POST /api/scalper/enable` `POST /api/scalper/disable`
- `GET|POST /api/scalper/config` `GET /api/scalper/trades` `GET /api/scalper/watchlist`
- `POST /api/scalper/kill-switch` `GET /api/scalper/circuit-breakers`

### Worklist
- `GET /api/worklist` `POST /api/worklist/add/{symbol}` `DELETE /api/worklist/remove/{symbol}`
- `POST /api/worklist/rescore` `POST /api/worklist/pipeline/start|stop`

### Reports & Events
- `GET /api/reports/eod/today` `GET /api/reports/eod/{date}`
- `GET /api/events/today` `GET /api/events/{date}` `POST /api/events/flush`
- `GET /api/watchdog/status`

### WebSocket
- `WS /ws` — real-time push (positions, worklist, signals, quotes)

## Trading Parameters (scalper_config.json)

- Account: $500, 2% risk/trade, max 3 positions
- Entry: $2-$20 price, limit orders only in premarket
- Exit priority: Hard stop (1-3% adaptive) > Trailing stop > Profit target (2.5%) > Momentum decay > Max hold (300s) > Emergency
- Circuit breaker: 3 hard stops = blocked 30 min
- Session cap: 50 trades/day

## Trading Clock (Eastern Time)

| Phase | Time | Behavior |
|-------|------|----------|
| OFFHOURS | 16:00-04:00 | Nothing |
| DISCOVERY | 04:00-07:00 | Scan + score, NO trades |
| LIVE | 07:00-09:15 | Limit orders allowed |
| EXIT_ONLY | 09:15-09:30 | Close positions only |
| SHADOW | 09:30-16:00 | Simulate trades, log for research |

## Momentum FSM (8 States)

```
IDLE -> CANDIDATE -> IGNITING -> GATED -> IN_POSITION -> MONITORING -> EXITING -> COOLDOWN -> IDLE
```
Thresholds: candidate=30, igniting=45, gated=60

## Critical Patterns

### DO:
- **Singleton pattern**: `get_*()` with global `_instance` for all engines
- **`asyncio.to_thread()`** for ALL file I/O and Webull API calls
- **Atomic file writes**: temp file + `os.replace()` (prevents corruption)
- **Limit orders only** in premarket (never market orders)
- **UTF-8 encoding** on all file operations
- **ASCII-only** in logger calls (Windows charmap codec crashes on unicode)
- **Dedicated thread pools** for API calls vs file I/O
- **Update config via API only** (`POST /api/scalper/config`), never edit the JSON file

### DON'T:
- **HTTP self-calls**: code inside FastAPI must NOT call `localhost:9300/api/...`, use direct imports
- **Persist `enabled` field** to config — causes poison restart cycle
- **Market orders** in premarket — slippage kills edge
- **`asyncio.get_event_loop()`** — deprecated, use `asyncio.to_thread()` instead
- **Amend commits** after hook failure — always new commit

## Data Files

| Path | Purpose |
|------|---------|
| `reports/{date}/trade_ledger.jsonl` | Append-only event log |
| `reports/{date}/eod_report.json` | Generated EOD summary |
| `data/open_positions.json` | Atomic position writes |
| `state/blacklist_rehab.json` | Auto-rehab blacklisted symbols |
| `tokens/webull_token.json` | Cached auth token (gitignored) |

## Notes for AI Assistants

- Read this file first before making changes
- Check existing singleton patterns before creating new instances
- Run `python -m pytest tests/ -v` before committing
- Never commit tokens, .env, or credentials
- The scalper config JSON is overwritten at runtime — always use the API endpoint
