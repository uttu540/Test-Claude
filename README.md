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
- **AI Strategy** — Claude claude-sonnet-4-6 validates signals, checks timeframe alignment
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

- **Python 3.12** (`python3.12 --version`)
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

---

## Running the Bot

```bash
source venv/bin/activate   # always activate venv first

make start           # development mode (safe, no real money)
make start-paper     # paper mode (real feed, simulated orders)
make start-semi-auto # semi-auto (requires Telegram approval per trade)
make start-live      # live mode (real money — requires confirmation)
```

All three processes (bot, API, dashboard) start together via honcho and stream logs with colour prefixes.

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
DAILY_LOSS_LIMIT_INR=2000
MAX_OPEN_POSITIONS=5

# Optional
NEWS_API_KEY=...          # NewsAPI.org for sentiment (free tier works)
```

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
| **Confidence** | 0–100 score. Below 50 → skipped before Claude AI. Below 55 → skipped even if Claude approves. |
| **Signal Type** | Pattern that triggered the signal: `BREAKOUT_HIGH/LOW`, `EMA_CROSS_UP/DOWN`, `MACD_CROSS_UP/DOWN`, `RSI_OVERSOLD/OVERBOUGHT`, `ORB_BREAKOUT`, `VWAP_RECLAIM`, `BB_SQUEEZE` |
| **ATR** | Average True Range — how much a stock moves per candle. SL = 1.5× ATR, Target = 3× ATR. |
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
python3.12 services/auth/kite_auto_auth.py
```

This uses Playwright to automate the browser login flow and caches the access token in Redis.

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
├── migrations/                      # Alembic migration files
│
├── services/
│   ├── auth/kite_auto_auth.py       # Playwright-based Kite re-auth
│   ├── data_ingestion/
│   │   ├── kite_feed.py             # Kite WebSocket live feed
│   │   ├── mock_feed.py             # Dev mode random walk feed
│   │   └── historical_seed.py       # Seed OHLCV data (yfinance/Kite)
│   ├── technical_engine/
│   │   ├── indicators.py            # pandas-ta indicator calculation
│   │   └── signal_generator.py      # Signal detection + regime filter
│   ├── ai_strategy/
│   │   └── claude_client.py         # Claude AI signal validation
│   ├── execution/
│   │   ├── broker_router.py         # Broker abstraction layer
│   │   ├── kite_broker.py           # Zerodha Kite execution
│   │   └── paper_broker.py          # Simulated paper execution
│   ├── risk_manager.py              # Position sizing + daily loss limit
│   └── notification/telegram_bot.py # Telegram alerts + semi-auto approval
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

**`Too many connections` in Redis logs**
Already fixed — Redis pool is set to `max_connections=100`.

**Kite access token expired**
Run `python3.12 services/auth/kite_auto_auth.py` to re-authenticate.

**Dashboard shows no data**
Ensure all three processes are running (`make start`). Check the WebSocket status dot in the navbar — it should be green.
