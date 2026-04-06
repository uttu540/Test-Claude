# Trading Bot

An AI-powered algorithmic trading bot for NSE/BSE (Indian markets) using Zerodha Kite Connect and Claude AI.

## Architecture

```
Data Feed (Zerodha WebSocket / Mock)
    ↓
Candle Aggregator (1min → 5min → 15min → 1hr → 1day)
    ↓
Technical Analysis Engine (30+ indicators, multi-timeframe confluence)
    ↓
Signal Generator (confidence 0–100, deduped per symbol)
    ↓
Risk Engine (daily loss limit · max positions · ATR position sizing)
    ↓
Trade Executor (entry order · stop-loss · target · DB record)
    ↓
Order Manager (Zerodha Kite Connect / Paper simulation)
    ↓
Database (PostgreSQL + TimescaleDB) + Redis (live state)
    ↓
Telegram Alerts + REST API + React Dashboard
```

## Project Structure

```
├── api/
│   └── main.py              # FastAPI REST + WebSocket API (port 8000)
├── config/
│   └── settings.py          # All settings loaded from .env
├── database/
│   ├── models.py            # SQLAlchemy ORM models
│   ├── connection.py        # Async DB + Redis connections
│   └── init.sql             # TimescaleDB extension setup
├── frontend/
│   ├── src/
│   │   ├── pages/           # Dashboard, Trades
│   │   └── components/      # Navbar, StatCard, tables, charts
│   └── package.json         # React 18 + Vite + Tailwind + Recharts
├── services/
│   ├── data_ingestion/      # Zerodha WebSocket feed, historical seeder
│   ├── technical_engine/    # 30+ indicators (pandas-ta), signal generator
│   ├── risk_engine/         # Pre-trade checks + ATR position sizing ✅
│   ├── ai_strategy/         # Claude AI client (Phase 2)
│   ├── execution/
│   │   ├── trade_executor.py  # Signal → orders → DB → Telegram ✅
│   │   ├── zerodha/           # Kite Connect authenticator + order manager
│   │   └── groww/             # Groww Trade API (Phase 6)
│   └── notifications/       # Telegram bot
├── tests/
├── docker-compose.yml       # PostgreSQL/TimescaleDB + Redis
├── main.py                  # Bot entry point
└── Makefile                 # Dev commands
```

## Phases

| Phase | Status | Description |
|-------|--------|-------------|
| 1 | 🔄 In Progress | Data pipeline, TA engine, risk engine, trade execution, dashboard |
| 2 | Pending | Claude AI strategy, news ingestion, fundamental data |
| 3 | Pending | Backtesting framework, strategy validation |
| 4 | Pending | Paper trading mode (simulated broker) |
| 5 | Pending | Semi-automated live trading (human approval gate) |
| 6 | Pending | Full automation + Groww integration |

## Setup

### Prerequisites
- Python 3.11+
- Node.js 18+
- Docker Desktop
- Zerodha account (API key from [kite.trade](https://kite.trade) — ₹2000/month)
- Telegram bot (optional but recommended)

### 1. Clone and install

```bash
git clone https://github.com/uttu540/Test-Claude.git
cd Test-Claude
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your API keys
```

### 3. Start infrastructure

```bash
make up       # Start PostgreSQL + Redis via Docker
make db-init  # Create tables and TimescaleDB hypertables
```

### 4. Run the bot

```bash
# Development mode (mock feed, simulated orders, no API keys needed)
make dev

# Paper trading (real Zerodha data, simulated orders)
APP_ENV=paper python main.py

# Live trading (real orders — requires Zerodha API key + daily auth)
make live
```

### 5. Run the API

```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```

### 6. Run the dashboard

```bash
cd frontend
npm install
npm run dev
# → http://localhost:5173
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/positions` | Open trades |
| GET | `/api/trades` | Trade history (paginated) |
| GET | `/api/trades/{id}` | Single trade with all orders |
| GET | `/api/pnl/today` | Today's P&L summary |
| GET | `/api/signals/recent` | Latest signals from Redis |
| GET | `/api/bot/status` | Bot health + mode + stats |
| POST | `/api/bot/square-off` | Emergency exit all intraday |
| WS | `/ws` | Live signals + position updates |

## Risk Parameters

| Parameter | Value | Notes |
|-----------|-------|-------|
| Total Capital | ₹1,00,000 | Configurable in `.env` |
| Max risk per trade | 2% = ₹2,000 | ATR-based stop sizing |
| Daily loss limit | 2% = ₹2,000 | Halts all trading when hit |
| Max open positions | 8 | Hard cap |
| Max single position | 10% = ₹10,000 | Caps position value |
| Stop loss | 1.5× ATR | Auto-calculated |
| Target | 2:1 R:R | 3× ATR from entry |

## Trade Flow

When a signal with confidence ≥ 65 is detected:

1. **Risk Engine** checks daily P&L, open positions, duplicate symbols
2. **Position sizing**: `qty = ₹2,000 / (entry - stop_loss)`
3. **Entry order**: MARKET order placed (or simulated in dev/paper)
4. **Trade recorded** in PostgreSQL with all metadata
5. **Stop-loss**: SL-M order at `entry ± 1.5× ATR`
6. **Target**: LIMIT order at `entry ± 3× ATR` (2:1 R:R)
7. **Telegram alert** sent with full trade details

## Makefile Commands

```bash
make up          # Start Docker services
make down        # Stop Docker services
make dev         # Run bot in development mode (mock feed)
make paper       # Run in paper trading mode
make live        # Run live (asks for confirmation)
make db-init     # Create database schema
make db-upgrade  # Apply migrations
make logs        # Tail Docker logs
```

## GitHub Workflow — Claude Code Automation

Contributors can create issues and have Claude Code implement them automatically:

1. Create an issue using one of the issue templates (Bug / Feature / Improvement)
2. Fill in the acceptance criteria
3. Add the `claude` label
4. GitHub Actions fires → Claude Code implements → PR opened automatically
5. Review the PR → merge → issue auto-closes

**Required secret:** `ANTHROPIC_API_KEY` in repo Settings → Secrets → Actions.

## ⚠️ Important Notes

1. **SEBI Compliance**: All automated orders must be registered with your broker. Use paper/semi-auto mode before going live.
2. **Zerodha Re-auth**: Kite Connect requires daily re-authentication (automated via Playwright + TOTP at 8:30 AM IST).
3. **No financial advice**: This bot is for educational purposes. Trade at your own risk.
4. **Never commit `.env`**: Your API keys are in `.env` — it is git-ignored.
