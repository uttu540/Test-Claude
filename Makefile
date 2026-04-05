.PHONY: help up down db-migrate db-upgrade install dev logs ps clean

# ─── Default ──────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  Trading Bot — Available Commands"
	@echo "  ─────────────────────────────────────────────────────"
	@echo "  make up          Start PostgreSQL + Redis (Docker)"
	@echo "  make down        Stop all Docker services"
	@echo "  make logs        Tail Docker logs"
	@echo "  make ps          Show running containers"
	@echo "  make install     Install Python dependencies"
	@echo "  make playwright  Install Playwright browsers"
	@echo "  make db-init     Run first-time DB migration"
	@echo "  make db-upgrade  Apply pending Alembic migrations"
	@echo "  make dev         Start the bot in development mode"
	@echo "  make paper       Start in paper trading mode"
	@echo "  make clean       Stop Docker + remove volumes (CAUTION)"
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
	pip install -r requirements.txt

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

# ─── Bot ──────────────────────────────────────────────────────────────────────
dev:
	APP_ENV=development python main.py

paper:
	APP_ENV=paper python main.py

live:
	@echo "⚠️  Starting in LIVE mode — real money at risk!"
	@read -p "Type 'yes' to confirm: " confirm; \
	if [ "$$confirm" = "yes" ]; then APP_ENV=live python main.py; fi
