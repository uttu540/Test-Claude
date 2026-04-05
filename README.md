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
AI Strategy Engine (Claude — reasoning, not execution)
    ↓
Risk Engine (deterministic rules — daily loss limit, position sizing)
    ↓
Order Manager (Zerodha Kite Connect)
    ↓
Database (PostgreSQL + TimescaleDB) + Redis (live state)
    ↓
Telegram Alerts
```

## Project Structure

```
├── config/                  # Settings (loaded from .env)
├── database/                # SQLAlchemy models + migrations
├── services/
│   ├── data_ingestion/      # Zerodha WebSocket feed, historical data seeder
│   ├── technical_engine/    # Indicators (pandas-ta), signal generator
│   ├── ai_strategy/         # Claude AI client, prompts, schemas (Phase 2)
│   ├── risk_engine/         # Pre-trade checks, position sizing (Phase 2)
│   ├── execution/
│   │   ├── zerodha/         # Kite Connect authenticator + order manager
│   │   └── groww/           # Groww Trade API (Phase 2)
│   └── notifications/       # Telegram bot
├── tests/
├── docker-compose.yml       # PostgreSQL/TimescaleDB + Redis
├── main.py                  # Entry point
├── Makefile                 # Dev commands
└── requirements.txt
```

## Phases

| Phase | Status | Description |
|-------|--------|-------------|
| 1 | ✅ In Progress | Data pipeline, live feed, TA engine, Telegram |
| 2 | Pending | Fundamental data, news, Claude sentiment analysis |
| 3 | Pending | Strategy engine + backtesting |
| 4 | Pending | Paper trading |
| 5 | Pending | Semi-automated live trading |
| 6 | Pending | Full automation + Groww integration |

## Setup

### Prerequisites
- Python 3.11+
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

### 3. Start databases
```bash
make up          # Start PostgreSQL + Redis
make db-init     # Create tables
```

### 4. Run in development mode (no API key needed)
```bash
make dev
# Uses mock feed with synthetic prices
```

### 5. Run with real Zerodha data
```bash
# 1. Add your Kite API key to .env
# 2. Run authentication (once per day)
python -m services.execution.zerodha.authenticator
# 3. Start
APP_ENV=paper python main.py
```

## Risk Parameters (under ₹1L capital)

| Parameter | Value |
|-----------|-------|
| Total Capital | ₹1,00,000 |
| Max risk per trade | 2% = ₹2,000 |
| Daily loss limit | 2% = ₹2,000 |
| Max open positions | 8 |
| Max single position | 10% = ₹10,000 |

## Makefile Commands

```bash
make up          # Start Docker services
make down        # Stop Docker services
make dev         # Run in development mode
make paper       # Run in paper trading mode
make live        # Run live (asks for confirmation)
make db-init     # Create database schema
make db-upgrade  # Apply migrations
make logs        # Tail Docker logs
```

## ⚠️ Important Notes

1. **SEBI Compliance**: All automated orders must be registered with your broker. Use semi-automated mode first.
2. **Zerodha Re-auth**: Kite Connect requires daily re-authentication (automated via Playwright + TOTP).
3. **No financial advice**: This bot is for educational purposes. Trade at your own risk.
4. **Never commit `.env`**: Your API keys are in `.env` — it is git-ignored.
