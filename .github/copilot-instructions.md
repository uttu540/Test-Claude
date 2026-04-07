# Copilot Workspace Instructions

## Purpose
This repository is an AI-assisted algorithmic trading bot for Indian markets built with Python, FastAPI, PostgreSQL/TimescaleDB, Redis, and a React + Vite dashboard.

Copilot should use these instructions to be productive quickly and stay aligned with project conventions.

## What to read first
- `README.md` — architecture, setup, run commands, and repo structure
- `Makefile` — official developer commands
- `.github/workflows/claude-implement.yml` — existing automation and issue handling flow
- `.github/ISSUE_TEMPLATE/*` — issue structure and labels used by maintainers

## Primary commands
Use the supported Makefile targets and project scripts rather than inventing custom tooling.

- `make help` — list available commands
- `make install` — install Python dependencies
- `make playwright` — install Playwright browsers
- `make up` — start PostgreSQL + Redis with Docker Compose
- `make down` — stop Docker Compose services
- `make db-init` — initialize database schema with Alembic
- `make db-upgrade` — apply migrations
- `make dev` — start the bot in development mode
- `make paper` — start in paper trading mode
- `make live` — start in live trading mode (requires confirmation)

Frontend commands
- `cd frontend && npm install`
- `cd frontend && npm run dev`

## Project conventions
- Python code lives at the repository root and under `api/`, `config/`, `database/`, `services/`
- Frontend code lives under `frontend/`
- Runtime configuration is managed through `.env` and `.env.example`
- Docker Compose is used only for supporting services (Postgres/Redis)
- Live trading is a sensitive operation; do not modify `make live` behavior lightly

## Important files and directories
- `main.py` — primary bot entrypoint
- `api/main.py` — FastAPI REST + WebSocket server
- `config/settings.py` — application configuration
- `database/models.py` + `database/connection.py` — ORM and DB connection setup
- `services/technical_engine/` — indicators and signal generation
- `services/risk_engine/` — risk checks and sizing logic
- `services/execution/` — order execution and broker integration
- `services/ai_strategy/` — Claude AI-related logic and prompts
- `frontend/` — React dashboard and client-side application
- `.github/workflows/claude-implement.yml` — Claude Code automation for issues

## Review guidance
- Preserve existing architecture and avoid broad refactors unless the issue explicitly requires them
- Add tests only when the issue involves deterministic logic or if a regression is fixed
- Keep changes minimal and focused on the problem statement
- Use existing helper patterns in `services/`, `database/`, and `api/`

## Etiquette for Copilot suggestions
- If a requested change affects live trading, call out risk and safety concerns
- Do not suggest committing secrets or `.env` values
- Prefer `make` commands and documented scripts rather than ad hoc shell commands

## When to ask follow-up questions
Ask for clarification if:
- the issue text is ambiguous about production vs paper mode
- a requested feature affects order execution, risk limits, or broker integration
- the change would require new environment variables or cloud infrastructure
