.PHONY: help up down logs ps clean install playwright \
        db-init db-upgrade db-downgrade db-history db-stamp \
        dev paper semi-auto live test \
        start start-dev start-paper start-semi-auto start-live setup

# ─── Venv paths (no manual activation needed) ─────────────────────────────────
PYTHON  = venv/bin/python3
PIP     = venv/bin/pip
HONCHO  = venv/bin/honcho
ALEMBIC = venv/bin/alembic

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
	$(PIP) install --upgrade setuptools
	$(PIP) install -r requirements.txt

playwright:
	$(PYTHON) -m playwright install chromium

# ─── Database ─────────────────────────────────────────────────────────────────
db-init:
	$(ALEMBIC) upgrade head
	@echo "✅ Database schema created"

db-upgrade:
	$(ALEMBIC) upgrade head

db-downgrade:
	$(ALEMBIC) downgrade -1

db-history:
	$(ALEMBIC) history --verbose

db-stamp:
	$(ALEMBIC) stamp 001
	@echo "✅ Existing DB stamped at migration 001 — run 'make db-upgrade' to apply newer migrations"

# ─── First-time setup ─────────────────────────────────────────────────────────
setup:
	@echo "==> Creating virtual environment..."
	python3 -m venv venv
	@echo "==> Installing Python dependencies..."
	venv/bin/pip install --upgrade setuptools pip
	venv/bin/pip install -r requirements.txt
	@echo "==> Starting Docker services..."
	docker compose up -d
	@echo "==> Waiting for DB to be ready..."
	@sleep 3
	@echo "==> Running database migrations..."
	venv/bin/alembic upgrade head
	@echo "==> Installing frontend dependencies..."
	cd frontend && npm install
	@echo ""
	@echo "✅ Setup complete!"
	@echo "   Copy .env.example to .env and fill in your credentials."
	@echo "   Then run: make start"
	@echo ""

# ─── One-command start (bot + API + frontend via honcho) ──────────────────────
# honcho reads Procfile and streams all three processes with colour prefixes.
# venv is used automatically — no need to activate it manually.

start: start-dev

start-dev: _check-env
	@echo "==> Starting in DEVELOPMENT mode (mock feed, paper orders)..."
	APP_ENV=development $(HONCHO) start

start-paper: _check-env
	@echo "==> Starting in PAPER mode (real feed, simulated orders)..."
	APP_ENV=paper $(HONCHO) start

start-semi-auto: _check-env
	@echo "==> Starting in SEMI-AUTO mode — Telegram approval required per trade"
	@echo "   Ensure TELEGRAM_BOT_TOKEN and TELEGRAM_AUTHORIZED_IDS are set."
	APP_ENV=semi-auto $(HONCHO) start

start-live: _check-env
	@echo "⚠️  Starting in LIVE mode — real money at risk!"
	@read -p "Type 'yes' to confirm: " confirm; \
	if [ "$$confirm" = "yes" ]; then APP_ENV=live $(HONCHO) start; fi

# ─── Internal: pre-flight checks ──────────────────────────────────────────────
_check-env:
	@if [ ! -f venv/bin/python3 ]; then \
		echo "❌ venv not found. Run: make setup"; \
		exit 1; \
	fi
	@if [ ! -f .env ]; then \
		echo "❌ .env file not found. Run: cp .env.example .env"; \
		exit 1; \
	fi
	@docker compose ps --services --filter status=running | grep -q db || \
		(echo "==> Docker not running, starting now..." && docker compose up -d && sleep 3)
	@$(ALEMBIC) upgrade head 2>/dev/null || true

# ─── Bot only (single process, useful for debugging) ──────────────────────────
dev:
	APP_ENV=development $(PYTHON) main.py

paper:
	APP_ENV=paper $(PYTHON) main.py

semi-auto:
	APP_ENV=semi-auto $(PYTHON) main.py

live:
	@echo "⚠️  Starting in LIVE mode — real money at risk!"
	@read -p "Type 'yes' to confirm: " confirm; \
	if [ "$$confirm" = "yes" ]; then APP_ENV=live $(PYTHON) main.py; fi

test:
	$(PYTHON) -m pytest tests/ -v
