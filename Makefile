.PHONY: help up down logs ps clean install playwright \
        db-init db-upgrade db-downgrade db-history db-stamp \
        dev paper semi-auto live test \
        start start-dev start-paper start-semi-auto start-live setup

# ─── Default ──────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  Trading Bot — Available Commands"
	@echo "  ─────────────────────────────────────────────────────────────"
	@echo "  ── One-command start (bot + API + dashboard) ──────────────"
	@echo "  make start           Start everything in development mode"
	@echo "  make start-dev       Same as start"
	@echo "  make start-paper     Start everything in paper-trading mode"
	@echo "  make start-semi-auto Start everything in semi-auto mode"
	@echo "  make start-live      Start everything in live mode"
	@echo ""
	@echo "  ── First-time setup ───────────────────────────────────────"
	@echo "  make setup           Install deps, start Docker, run migrations"
	@echo ""
	@echo "  ── Individual services ────────────────────────────────────"
	@echo "  make up              Start PostgreSQL + Redis (Docker)"
	@echo "  make down            Stop all Docker services"
	@echo "  make install         Install Python dependencies"
	@echo "  make playwright      Install Playwright browsers"
	@echo "  make db-upgrade      Apply pending Alembic migrations"
	@echo "  make db-stamp        Stamp existing DB at migration 001"
	@echo "  make test            Run pytest test suite"
	@echo "  make logs            Tail Docker logs"
	@echo "  make clean           Stop Docker + delete volumes (CAUTION)"
	@echo ""

# ─── Docker ───────────────────────────────────────────────────────────────────
up:
	docker compose up -d
	@echo "✅ PostgreSQL + Redis are running"

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

clean:
	@echo "⚠️  This will delete ALL trading data in Docker volumes!"
	@read -p "Type 'yes' to confirm: " confirm; \
	if [ "$$confirm" = "yes" ]; then docker compose down -v; echo "🗑️  Volumes deleted"; fi

# ─── Python ───────────────────────────────────────────────────────────────────
install:
	pip3.12 install -r requirements.txt

playwright:
	playwright install chromium

# ─── Database ─────────────────────────────────────────────────────────────────
db-init:
	alembic upgrade head
	@echo "✅ Database schema created"

db-upgrade:
	alembic upgrade head

db-downgrade:
	alembic downgrade -1

db-history:
	alembic history --verbose

db-stamp:
	alembic stamp 001
	@echo "✅ Existing DB stamped at migration 001 — run 'make db-upgrade' to apply newer migrations"

# ─── First-time setup ─────────────────────────────────────────────────────────
setup:
	@echo "==> Installing Python dependencies..."
	pip3.12 install -r requirements.txt
	@echo "==> Starting Docker services..."
	docker compose up -d
	@echo "==> Waiting for DB to be ready..."
	@sleep 3
	@echo "==> Running database migrations..."
	alembic upgrade head
	@echo "==> Installing frontend dependencies..."
	cd frontend && npm install
	@echo ""
	@echo "✅ Setup complete!"
	@echo "   Copy .env.example to .env and fill in your credentials."
	@echo "   Then run: make start"
	@echo ""

# ─── One-command start (bot + API + frontend via honcho) ──────────────────────
# honcho reads Procfile and streams all three processes with colour prefixes.
# Install: pip install honcho  (already in requirements.txt)

start: start-dev

start-dev: _check-env
	@echo "==> Starting in DEVELOPMENT mode (mock feed, paper orders)..."
	APP_ENV=development honcho start

start-paper: _check-env
	@echo "==> Starting in PAPER mode (real feed, simulated orders)..."
	APP_ENV=paper honcho start

start-semi-auto: _check-env
	@echo "🟣 Starting in SEMI-AUTO mode — Telegram approval required per trade"
	@echo "   Ensure TELEGRAM_BOT_TOKEN and TELEGRAM_AUTHORIZED_IDS are set."
	APP_ENV=semi-auto honcho start

start-live: _check-env
	@echo "⚠️  Starting in LIVE mode — real money at risk!"
	@read -p "Type 'yes' to confirm: " confirm; \
	if [ "$$confirm" = "yes" ]; then APP_ENV=live honcho start; fi

# ─── Internal: pre-flight checks ──────────────────────────────────────────────
_check-env:
	@if [ ! -f .env ]; then \
		echo "❌ .env file not found. Run: cp .env.example .env"; \
		exit 1; \
	fi
	@docker compose ps --services --filter status=running | grep -q db || \
		(echo "==> Docker not running, starting now..." && docker compose up -d && sleep 3)
	@alembic upgrade head 2>/dev/null || true

# ─── Bot only (single process, useful for debugging) ──────────────────────────
dev:
	APP_ENV=development python3.12 main.py

paper:
	APP_ENV=paper python3.12 main.py

semi-auto:
	APP_ENV=semi-auto python3.12 main.py

live:
	@echo "⚠️  Starting in LIVE mode — real money at risk!"
	@read -p "Type 'yes' to confirm: " confirm; \
	if [ "$$confirm" = "yes" ]; then APP_ENV=live python3.12 main.py; fi

test:
	python3.12 -m pytest tests/ -v
