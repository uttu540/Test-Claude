# TradeBot — NSE Algorithmic Trading System

An automated trading bot for NSE (Nifty 50) stocks with technical analysis, AI-powered signal refinement, multi-mode execution, Telegram notifications, and a real-time React dashboard.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Processes (honcho)                    │
│                                                             │
│  bot.1  →  main.py           Market data + strategy engine  │
│  api.1  →  uvicorn           FastAPI REST + WebSocket        │
│  web.1  →  vite dev server   React dashboard                 │
└─────────────────────────────────────────────────────────────┘
         │                  │
    PostgreSQL          Redis
  (TimescaleDB)     (tick cache + signals)
```

**Key services:**
- **Data Ingestion** — Kite Connect WebSocket (live ticks) or mock feed in dev
- **Technical Engine** — 130+ indicators via pandas-ta, signal generation for all Nifty 50
- **AI Strategy** — Claude validates signals (Haiku 4.5 for fast signal scoring, Sonnet for market briefing) and checks timeframe alignment; optional NVIDIA `gpt-oss` secondary LLM for **offline** research/eval only
- **Execution** — Zerodha Kite (live/semi-auto) or paper broker (dev/paper modes)
- **Risk Manager** — Daily loss limit, max position sizing, per-trade R:R gate
- **Telegram Bot** — Multi-user approval in semi-auto mode, trade alerts

---

## Modes

| Mode | Data Feed | Orders | Telegram approval |
|------|-----------|--------|-------------------|
| `development` | Mock (random walk) | Paper (simulated) | No |
| `paper` | Kite WebSocket (real) | Paper (simulated) | No |
| `semi-auto` | Kite WebSocket | Real (Kite) | **Yes — per trade** |
| `live` | Kite WebSocket | Real (Kite) | No (fully automated) |

---

## Prerequisites

- **Python 3.12 — required.** 3.11 and 3.13 do **not** work:
  - `pandas-ta` (unpinned) now only publishes releases requiring Python ≥3.12, so 3.11 (and older) cannot install this project at all.
  - `playwright==1.44.0` hard-pins `greenlet==3.0.3`, and `pydantic==2.7.1` / `aiohttp==3.9.5` / `asyncpg==0.29.0` / `sqlalchemy==2.0.30` all predate cp313 wheels — on 3.13 pip tries to build them from source and fails without MSVC build tools (Windows) / a C toolchain (macOS/Linux).
- **Node.js 18+** (`node --version`)
- **Docker Desktop** (for PostgreSQL + Redis)
- Zerodha Kite Connect API key (optional — only needed for `paper`/`semi-auto`/`live` modes)
- Anthropic API key (for AI signal validation)
- Telegram bot token (optional — for `semi-auto` mode and trade alerts)

---

## First-Time Setup

```bash
# 1. Clone and enter the project
git clone <repo> && cd Test-Claude

# 2. Create Python virtual environment
python3.12 -m venv venv
source venv/bin/activate      # macOS/Linux

# 3. Configure environment
cp .env.example .env
# Edit .env — fill in ANTHROPIC_API_KEY, KITE_API_KEY, TELEGRAM_BOT_TOKEN, etc.

# 4. Run setup (installs deps, starts Docker, runs migrations, installs frontend)
make setup
```

`make setup` does the following:
1. `pip install -r requirements.txt`
2. `docker compose up -d` (starts TimescaleDB + Redis)
3. `alembic upgrade head` (creates DB schema)
4. `cd frontend && npm install`

### Windows

The venv layout differs from macOS/Linux — activate with the Scripts path, not `bin`:

```powershell
py -3.12 -m venv venv
venv\Scripts\activate          # PowerShell / cmd.exe
# or: source venv/Scripts/activate   # Git Bash
```

`make setup` / `make start*` and the `Procfile` (`venv/bin/python`, `venv/bin/uvicorn`) currently
assume a POSIX venv layout and will not find the interpreter on Windows. Until the Makefile and
Procfile are made cross-platform, run the underlying commands directly from an activated venv
instead of through `make`:

```powershell
pip install -r requirements.txt
docker compose up -d
alembic upgrade head
cd frontend; npm install; cd ..

# to run the bot/API (no honcho on Windows without extra setup):
python main.py                                  # bot (set APP_ENV first)
uvicorn api.main:app --host 0.0.0.0 --port 8000  # API, separate terminal
cd frontend; npm run dev                          # dashboard, separate terminal
```

---

## Running the Bot

```bash
source venv/bin/activate   # macOS/Linux/Git Bash — always activate venv first
# venv\Scripts\activate    # Windows PowerShell / cmd.exe

make start           # development mode (safe, no real money)
make start-paper     # paper mode (real feed, simulated orders)
make start-semi-auto # semi-auto (requires Telegram approval per trade)
make start-live      # live mode (real money — requires confirmation)
```

All three processes (bot, API, dashboard) start together via honcho and stream logs with colour
prefixes. **On Windows**, `make start*` and honcho will fail to find `venv/bin/python` — run the
three processes directly as shown in the Windows section above.

**Dashboard:** http://localhost:5173
**API:** http://localhost:8000
**API docs:** http://localhost:8000/docs

---

## Environment Variables

Copy `.env.example` to `.env` and configure:

```env
# Required
ANTHROPIC_API_KEY=sk-ant-...

# For paper/semi-auto/live modes
KITE_API_KEY=...
KITE_API_SECRET=...

# For semi-auto mode and alerts
TELEGRAM_BOT_TOKEN=...
TELEGRAM_AUTHORIZED_IDS=123456789,987654321   # comma-separated Telegram user IDs

# Capital settings
TOTAL_CAPITAL=100000
MAX_RISK_PER_TRADE_PCT=4.0     # % of capital risked per trade
DAILY_LOSS_LIMIT_PCT=6.0       # Halt trading if day loss exceeds this % of capital
MAX_OPEN_POSITIONS=8

# Required in live/semi-auto — see "API Authentication" below
API_KEY=

# Optional
NEWS_API_KEY=...          # NewsAPI.org for sentiment (free tier works)

# Optional — research/eval only (NOT used for live trading)
NVIDIA_API_KEY=...                                  # build.nvidia.com free-tier key
NVIDIA_BASE_URL=https://integrate.api.nvidia.com/v1
NVIDIA_MODEL=openai/gpt-oss-120b
```

> **NVIDIA / `openai` SDK:** the `openai` package is used only as an OpenAI-compatible
> client for the free-tier NVIDIA NIM `gpt-oss` endpoint — no requests go to OpenAI.
> It powers **offline research** (backtest pattern discovery, strategy/code critique,
> correlation → hypothesis generation) and is deliberately never wired into the live
> trade-execution path. Trading decisions stay on the validated Claude pipeline.

> **API Authentication (`API_KEY`):** shared secret required in the `X-API-Key` header
> to call state-changing API routes (`POST /api/config`, `POST /api/bot/square-off`).
> CORS alone does not protect these — curl or any process on the network bypasses
> browser CORS checks entirely. **Required in `live`/`semi-auto`** — the API hard-refuses
> mutating requests with a 401 if `API_KEY` is unset while a real broker is in use, rather
> than silently allowing unauthenticated writes to reach it. Optional in `development`/`paper`
> (works with no key for a zero-config local dashboard, but logs a warning once). Generate
> with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

---

## Dashboard Pages

| Page | URL | Description |
|------|-----|-------------|
| Dashboard | `/` | P&L summary, stat cards, sparkline, signals + positions snapshot |
| Live Positions | `/positions` | All open trades with entry price, SL, target, R:R |
| Signals | `/signals` | Real-time signals with direction filter, confidence bars, indicator details |
| Trades | `/trades` | Full trade history (paginated) |
| P&L History | `/pnl` | Daily P&L bar chart (last N days) |
| Guide | `/changelog` | Quick-start, **glossary of all terms**, mode reference, env vars, changelog |

The navbar shows:
- IST clock (live)
- WebSocket status dot (green = live)
- Trading mode badge (DEV / PAPER / SEMI-AUTO / LIVE)
- Capital
- **Square Off All** button (emergency close all intraday positions)

---

## Key Terms

Full plain-English definitions are available in the dashboard at **Guide → Glossary** (`/changelog`). Quick reference:

| Term | Meaning |
|------|---------|
| **Market Regime** | Bot's classification of current market: `TRENDING_UP`, `TRENDING_DOWN`, `RANGING`, `HIGH_VOLATILITY`. Signals are filtered by regime. |
| **Signal** | A trading opportunity detected by the technical engine. Has a direction (LONG/SHORT), confidence (0–100), and signal type. |
| **Confidence** | 0–100 score. Configurable in the dashboard (Config): signals below `signal_min_confidence` (default 40) are dropped before regime filtering; `ORB_BREAKOUT`/`VWAP_RECLAIM` signals below their own floor (`orb_min_confidence`/`vwap_min_confidence`, default 70 each) are dropped; a signal must still be ≥ `confidence_threshold` (default 65) to be executed (RiskEngine → Claude → broker). |
| **Signal Type** | Pattern that triggered the signal: `BREAKOUT_HIGH/LOW`, `EMA_CROSS_UP/DOWN`, `MACD_CROSS_UP/DOWN`, `RSI_OVERSOLD/OVERBOUGHT`, `ORB_BREAKOUT`, `VWAP_RECLAIM`, `BB_SQUEEZE` |
| **ATR** | Average True Range — how much a stock moves per candle. SL = 2× ATR, Target = 4× ATR (2:1 R:R). 1.5×/3× was tried and abandoned — too tight for normal NSE intraday noise, stopping out legitimate positions before the move. |
| **R:R** | Risk:Reward ratio. Default 2:1 — target is twice as far as the stop-loss. |
| **SL** | Stop-loss — the price at which the trade exits automatically to cap losses. |
| **Square Off** | Closing all intraday positions. Auto-triggered at 3:12 PM IST; also available as an emergency button in the navbar. |
| **VWAP** | Volume Weighted Average Price — institutional benchmark price for the day. |
| **ORB** | Opening Range Breakout — breakout above/below the 9:15–9:30 AM high/low. |
| **Sharpe Ratio** | Risk-adjusted return. > 1 is good; > 2 is excellent. |
| **Profit Factor** | Gross profit ÷ gross loss. > 1.5 is a healthy system. |
| **Max Drawdown** | Largest peak-to-trough loss over a period. Lower is better. |

---

## Database

Uses PostgreSQL with TimescaleDB extension (via Docker).

```bash
make db-upgrade      # apply pending migrations
make db-downgrade    # rollback one migration
make db-history      # show migration history
make db-stamp        # stamp existing DB at migration 001 (for existing installs)
```

**Tables:**
- `trades` — all trade records (open + closed)
- `orders` — individual broker orders linked to trades
- `ohlcv` — TimescaleDB hypertable for OHLCV candle data

Seed historical data (optional — used for backtesting):
```bash
python3.12 services/data_ingestion/historical_seed.py
```

---

## Kite Connect Auth

Zerodha requires a daily re-authentication (access tokens expire at midnight).

```bash
# First time or after token expiry:
python3.12 -m services.execution.zerodha.authenticator
```

This uses Playwright to automate the browser login flow and caches the access token in Redis.
Normally you don't need to run this manually — a scheduled job (`job_daily_auth` in `main.py`)
calls it automatically at 8:30 AM IST every weekday while the bot is running.

---

## Running Tests

```bash
source venv/bin/activate
make test
```

---

## Makefile Reference

```bash
make setup           # First-time setup (all-in-one)
make start           # Start all services (development mode)
make start-paper     # Start in paper mode
make start-semi-auto # Start in semi-auto mode
make start-live      # Start in live mode (confirmation required)

make up              # Start Docker (PostgreSQL + Redis)
make down            # Stop Docker
make logs            # Tail Docker logs
make clean           # Stop Docker + delete all data volumes (CAUTION)

make install         # pip install -r requirements.txt
make playwright      # Install Playwright browsers

make db-upgrade      # Apply Alembic migrations
make db-downgrade    # Rollback last migration
make db-stamp        # Stamp DB at migration 001

make test            # Run pytest
```

---

## Project Structure

```
.
├── main.py                          # Bot entry point — orchestrates all services
├── config/settings.py               # Pydantic settings (loaded from .env)
├── Makefile                         # All commands
├── Procfile                         # honcho process definitions
├── requirements.txt
│
├── api/
│   └── main.py                      # FastAPI app (REST + WebSocket)
│
├── database/
│   ├── connection.py                # SQLAlchemy async engine + Redis pool
│   └── models.py                    # ORM models (Trade, Order)
│
├── alembic/versions/                # Alembic migration files (alembic.ini at repo root)
│
├── services/
│   ├── data_ingestion/
│   │   ├── websocket_feed.py        # Kite WebSocket live feed + MockFeed (dev random walk)
│   │   ├── historical_seed.py       # Seed OHLCV data (yfinance/Kite)
│   │   ├── gift_nifty.py            # GIFT Nifty / NSE scrape helpers
│   │   └── news_feed.py             # NewsAPI polling for headline sentiment
│   ├── technical_engine/
│   │   ├── indicators.py            # pandas-ta indicator calculation
│   │   └── signal_generator.py      # Signal detection + regime filter
│   ├── market_regime/               # NIFTY 50 index regime detector (TRENDING_UP/DOWN, RANGING, HIGH_VOLATILITY)
│   ├── ai_strategy/
│   │   ├── claude_client.py         # Claude AI signal validation
│   │   └── nvidia_client.py         # NVIDIA gpt-oss (offline research/eval only)
│   ├── orb_engine/                  # Opening Range Breakout (15-min TF, 9:30 AM–1 PM)
│   ├── momentum_engine/             # V1 daily swing engine (kept for backtest comparison; V2 is live)
│   ├── momentum_engine_v2/          # Active daily swing engine (relaxed-gate V2)
│   ├── earnings_engine/             # Earnings gap-and-go + Day 2 entry
│   ├── catalyst_engine/            # News/event-driven plays (PEAD/catalyst)
│   ├── intraday_engine/            # IDARVAS 15-min gap box (swing_only: off)
│   ├── intraday_engine_v2/         # Two-sided 5-min box (swing_only: off)
│   ├── backtesting/                 # Generic backtest engine + reporter
│   ├── risk_engine/
│   │   └── engine.py                # RiskEngine — position sizing + daily loss limit
│   ├── execution/
│   │   ├── broker_router.py         # Broker abstraction layer
│   │   ├── trade_executor.py        # Order placement orchestration
│   │   ├── trade_lifecycle.py       # Position lifecycle / square-off reconciliation
│   │   ├── paper_broker.py          # Simulated paper execution
│   │   └── zerodha/
│   │       ├── order_manager.py     # Zerodha Kite order execution
│   │       └── authenticator.py     # Playwright-based daily Kite re-auth
│   └── notifications/telegram_bot.py # Telegram alerts + semi-auto approval
│
├── scripts/                         # Offline research / profit-max tooling
│   ├── profit_max_sweep.py          # V2 backtest sweep harness (JSONL log)
│   ├── llm_edge_research.py         # gpt-oss edge-hypothesis generation
│   └── correlation_discovery.py     # Global lead-lag → NSE signal hypotheses
│
└── frontend/
    ├── src/
    │   ├── App.jsx                  # Router + layout
    │   ├── api.js                   # API client
    │   ├── ws.js                    # WebSocket hook (auto-reconnect)
    │   ├── pages/
    │   │   ├── Dashboard.jsx
    │   │   ├── Positions.jsx        # Live positions page
    │   │   ├── Signals.jsx          # Signals page with filters
    │   │   ├── Trades.jsx
    │   │   ├── PnLHistory.jsx
    │   │   └── Changelog.jsx
    │   └── components/
    │       ├── Navbar.jsx
    │       ├── PositionsTable.jsx
    │       ├── SignalsTable.jsx
    │       ├── StatCard.jsx
    │       ├── PnLBar.jsx
    │       └── TradesTable.jsx
    └── package.json
```

---

## Common Issues

**`make: alembic: No such file or directory`**
The venv isn't activated. Run `source venv/bin/activate` first.

**`DuplicateColumnError` during migration**
DB is already partially migrated. Run: `alembic stamp head`

**`ModuleNotFoundError: No module named 'pkg_resources'`**
Upgrade honcho: `pip install "honcho>=1.2.0"`

**`make setup` fails to resolve dependencies (`websockets` conflict)**
Older `requirements.txt` pinned `websockets==12.0`, but `yfinance==1.2.1` requires `websockets>=13.0` —
pip could not resolve the pin and `make setup` failed before installing anything. Fixed by relaxing
the pin to `websockets>=13.0,<16` (it's only a transitive dependency via `uvicorn[standard]`, not
imported directly). If you still see this, make sure `requirements.txt` has the range, not `==12.0`.

**`Too many connections` in Redis logs**
Already fixed — Redis pool is set to `max_connections=100`.

**Kite access token expired**
Run `python3.12 services/auth/kite_auto_auth.py` to re-authenticate.

**Dashboard shows no data**
Ensure all three processes are running (`make start`). Check the WebSocket status dot in the navbar — it should be green.
