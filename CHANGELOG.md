# Changelog

All notable changes to the trading bot are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]
_Next up: Tune signal confidence thresholds based on backtest findings (raise ORB threshold, tighten VWAP filter); paper trading validation run_

---

## [0.4.3] — 2026-04-09 — Docs & Glossary

### Added
- **`frontend/src/pages/Changelog.jsx`** — Full glossary of all technical terms shown in the dashboard, organized into five categories: Dashboard, Signals, Trades & Risk, Indicators, Performance Metrics. Each term has a one-line plain-English summary and a detailed explanation. Accessible at `/changelog` → Glossary section.
- **`README.md`** — Key Terms quick-reference table covering all dashboard terms (Market Regime, Signal, ATR, R:R, VWAP, ORB, Sharpe, Profit Factor, Max Drawdown, etc.)

### Changed
- **`frontend/src/pages/Changelog.jsx`** — Added Phase 9 entry documenting the 0.4.2 WebSocket fixes
- **`README.md`** — Guide page description updated to mention the in-app glossary

---

## [0.4.2] — 2026-04-09 — WebSocket & Dashboard Live-Update Fixes

### Fixed
- **[#27] `api/main.py`** — `ConnectionManager.disconnect()` had dead code: called `.discard()` on a `list` (set method), which always evaluated to `None`. Removed the dead line; `remove()` path was already correct.
- **[#28] `api/main.py`** — Dashboard `positions_update` and `pnl_update` WebSocket messages were never sent. Added `_db_broadcast_loop` background task that pushes live positions and P&L to all clients every 10 seconds. Dashboard now reflects new trades and closed positions without a manual refresh.
- **[#29] `api/main.py`** — WebSocket endpoint only caught `WebSocketDisconnect`; unclean disconnects (network drop, browser close without close frame) raised `RuntimeError` that escaped uncaught, leaving `disconnect()` never called. Fixed with `except Exception / finally: manager.disconnect(ws)`.
- **[#29] `frontend/src/ws.js`** — Client never sent any messages, causing the server's `receive_text()` loop to block indefinitely. Added a 30-second heartbeat: client sends `"ping"` every 30s while connected. Heartbeat timer is properly cleared on close and on manual disconnect.
- **[#30] `api/main.py`** — `/api/pnl/history` filtered by `entry_time` instead of `exit_time`. Trades entered before the window but closed within it were excluded. Fixed to filter by `exit_time`.
- **[#31] `frontend/src/components/PositionsTable.jsx`** — Risk (₹) column used `PnLCell` with a negated value, displaying the risk amount in red as if it were a realised loss. Changed to `PriceMono` (neutral styling).

### Changed
- **`api/main.py`** — Redis `KEYS` replaced with `scan_iter` in `_redis_broadcast_loop` to avoid blocking on large keyspaces.
- **`frontend/src/ws.js`** — Removed stale `console.log` / `console.warn` calls.

---

## [0.3.1] — 2026-04-09 — Backtest Run: Fixes & First Results

### Fixed
- **`indicators.py`** — Bollinger Band column lookup broken on `pandas-ta 0.4.71b0` (Python 3.12):
  - Column names changed from `BBU_20_2.0` → `BBU_20_2.0_2.0` (std value appended twice in new version)
  - Added `_find_col(prefix)` helper that matches by prefix, making code version-agnostic
- **`indicators.py`** — `TypeError: '>' not supported between instances of 'float' and 'NoneType'` in `_derived()`:
  - New pandas-ta returns `None` scalars instead of `NaN` in some columns on Python 3.12
  - Fixed by coercing all `object`-dtype columns via `pd.to_numeric(errors="coerce")` before comparisons; EMA/RSI series explicitly cast to `float`
- **`risk_engine/engine.py`** — DB helpers raised `asyncpg` connection errors when PostgreSQL not running:
  - Wrapped `_get_todays_pnl`, `_get_open_count`, `_has_open_position` each in `try/except`
  - Returns safe defaults (`0.0` / `0` / `False`) on DB failure — backtester runs without a live DB

### Changed
- **`signal_generator.py`** — Added `pre_computed: bool = False` to `SignalDetector.detect()`; when `True`, skips `compute_all()` call
- **`backtesting/engine.py`** — Major performance overhaul:
  - Indicators pre-computed **once per timeframe** on data load instead of once per candle (~3 calls/symbol vs ~3,450) — **60× speedup**; 50 Nifty symbols complete in ~3.5 min
  - Regime detection inlined from pre-computed daily row (reads `adx` + `ema_stack` directly)
  - Signal detector called with `pre_computed=True`

### Added
- `results/backtest_with_regime.json` — First full backtest: 90 days, Nifty 50, regime filter ON
- `results/backtest_no_regime.json` — Same run, regime filter OFF (comparison baseline)

### Backtest Findings (90 days · Nifty 50 · Jan–Apr 2026)

| Metric | Regime Filter ON | Regime Filter OFF |
|---|---|---|
| Total trades | 22,068 | 26,367 |
| Win rate | 43.6% | 43.2% |
| Net PnL | ₹2.93 Cr | ₹2.92 Cr |
| Avg PnL / trade | ₹1,329 | ₹1,109 |
| Profit factor | 36.37× | 30.06× |
| Sharpe ratio | 2.82 | 2.57 |
| Max drawdown | ₹−12,173 | ₹−27,223 |

**Signal type performance (regime filter ON):**
- `BREAKOUT_LOW`: 55% WR — dominant alpha source; market was `TRENDING_DOWN` for ~60% of period
- `MACD_CROSS_UP`: 64% WR — highest win rate, low trade count
- `RSI_OVERBOUGHT`: 58% WR — effective in downtrend
- `BREAKOUT_HIGH`: 57% WR — solid
- `ORB_BREAKOUT`: 38% WR — noisy; candidate for higher confidence threshold or disable
- `VWAP_RECLAIM`: 39% WR — needs tighter entry filters

**SHORT vs LONG:** SHORT trades ₹2.93 Cr vs LONG ₹49K — Jan–Apr 2026 was a strong downtrend. Regime filter correctly classified 13K/22K trades as `TRENDING_DOWN`.

---

---

## [0.4.1] — 2026-04-07
### Changed
- Intraday square-off time moved from **3:20 PM → 3:12 PM** IST
  - Reason: gives an 8-minute buffer before Zerodha's auto square-off at 3:20 PM, avoiding race conditions and broker-side forced closure slippage

---

## [0.4.0] — 2026-04-07 — Phase 4: Trade Lifecycle
### Added
- `services/execution/charges.py` — Zerodha intraday equity charge calculator
  - Brokerage: min(₹20, 0.03%) per side
  - STT: 0.025% on sell-side turnover
  - NSE exchange charges: 0.00345% of total turnover
  - SEBI charges: ₹10 per crore
  - GST: 18% on (brokerage + exchange + SEBI)
  - Stamp duty: 0.003% on buy-side turnover
- `services/execution/trade_lifecycle.py` — `TradeLifecycleManager` background service
  - **Dev/paper mode**: polls Redis tick cache every 10s, simulates SL/target hit from live price feed
  - **Live mode**: polls Kite Connect order book every 30s, detects COMPLETE SL-M/LIMIT orders, auto-cancels the sibling order
  - Calculates gross P&L, all charges, net P&L, risk_reward_actual, r_multiple on every closure
  - Updates `Trade` DB record to `CLOSED` with full exit metadata
  - Upserts `DailyPnL` aggregate row after every trade closure (`ON CONFLICT UPDATE`)
  - `close_all_open_trades()` force-closes everything at 3:12 PM and on kill-switch
  - Telegram alerts: 💚 TARGET HIT, 🔴 STOP LOSS HIT, TIME_EXIT

### Changed
- `main.py` — lifecycle manager started as background `asyncio.create_task` on boot, stopped on graceful shutdown
- `job_square_off_intraday` — now also calls `close_all_open_trades(reason="TIME_EXIT")` to close DB records, not just cancel broker orders

### Fixed
- Trades previously stayed `status=OPEN` forever — now every trade path (target, stop, EOD, kill-switch) ends in `status=CLOSED` with accurate P&L
- `DailyPnL` table was never written — now updated after every trade closure
- Risk engine open-position count was effectively broken (counted non-closed trades) — now resolves correctly as trades are properly closed

---

## [0.3.0] — 2026-04-07 — Phase 3: Signal Quality + Backtesting
### Added
- `services/market_regime/detector.py` — `MarketRegimeDetector`
  - Classifies market into `TRENDING_UP`, `TRENDING_DOWN`, `RANGING`, `HIGH_VOLATILITY` using ADX + EMA stack + India VIX
  - Writes `market:regime` to Redis (20min TTL) on every NIFTY 50 15min candle close
  - Fixes the gap where `market:regime` was read in many places but never written
- `services/technical_engine/signal_generator.py` — two new signal types:
  - `ORB_BREAKOUT` — Opening Range Breakout (9:15–9:30 AM range, fires 9:30 AM–1:00 PM, 15min TF only)
  - `VWAP_RECLAIM` — price reclaims/breaks VWAP with volume confirmation (intraday TFs only: 1min, 5min, 15min)
- `services/technical_engine/signal_generator.py` — `RegimeFilter` class
  - Gates signals by current market regime before they reach Claude
  - `TRENDING_UP`: allows breakout, EMA crossover, MACD, ORB, VWAP signals only
  - `TRENDING_DOWN`: allows breakdown, EMA crossover down, MACD down, ORB, VWAP signals only
  - `RANGING`: allows RSI mean-reversion, BB signals, VWAP only
  - `HIGH_VOLATILITY`: only VWAP reclaim, confidence capped at 60
  - `UNKNOWN`: all signals pass (safe default during startup)
- `services/backtesting/engine.py` — `BacktestEngine`
  - Replays historical OHLCV through full signal → risk pipeline
  - Data sources: TimescaleDB (primary) → yfinance (fallback, no API key)
  - No look-ahead bias: entry at next candle open, exit checks on subsequent candles
  - Exit types: TARGET, STOP, EOD, MAX_HOLD (5 days)
- `services/backtesting/reporter.py` — `BacktestReporter`
  - Metrics: win rate, net P&L, Sharpe ratio, max drawdown, profit factor, avg R:R
  - Breakdowns by signal type, market regime, direction, exit reason
  - Rich terminal output
- `services/backtesting/run.py` — CLI entrypoint
  - `python -m services.backtesting.run --universe nifty50 --days 90`
  - Flags: `--symbols`, `--universe nifty50|nifty500`, `--days`, `--start`, `--end`, `--output`, `--no-regime-filter`

### Changed
- `MultiTimeframeSignalEngine.analyse()` now accepts `regime` parameter and applies `RegimeFilter`
- `main.py` reads `market:regime` from Redis and passes it into every `analyse()` call
- `main.py` triggers regime detection on NIFTY 50 1day candle close

---

## [0.2.1] — 2026-04-07 — Post-Phase-2 Bug Fixes
### Fixed
- **[CRITICAL]** `trade_executor.py`: wrong ATR key `"atr"` → `"atr_14"` — was silently blocking all trades
- **[CRITICAL]** `order_manager.py`: `place_stop_loss()` and `place_target()` hardcoded `"SELL"` regardless of direction — broke all SHORT position exits; added `direction` param
- **[CRITICAL]** `api/main.py`: `INTERVAL ':days days'` SQL syntax error in `/api/pnl/history` — PostgreSQL never substituted the parameter inside a string literal; fixed with `MAKE_INTERVAL(days => :days)`
- **[HIGH]** `telegram_bot.py`: malformed ternary in `trade_entry` message body caused Python implicit string concatenation to drop either the header or footer depending on `target_2` value
- **[HIGH]** `api/main.py`: WebSocket broadcast `seen` set never cleared — after first signal, subsequent updates to the same symbol were never broadcast to clients; changed to `dict[str, last_value]`
- **[MEDIUM]** `api/main.py`: `/api/signals/recent` returned 3–4 duplicate entries per symbol after Phase 2 introduced per-timeframe Redis keys; now filters to top-level keys only (`key.count(":") == 2`)

---

## [0.2.0] — 2026-04-07 — Phase 2: AI Intelligence Layer
### Added
- `services/ai_strategy/schemas.py` — Pydantic models: `AIDecision`, `SignalContext`, `NewsContext`
  - `AIDecision.is_actionable`: `action != SKIP and confidence >= 0.55`
- `services/ai_strategy/prompts.py` — `SYSTEM_PROMPT` and `build_signal_prompt()` for NSE quant trading context
- `services/ai_strategy/claude_client.py` — `ClaudeStrategyClient`
  - Cost guard: signals with confidence < 50 skip the Claude call entirely
  - 2-attempt retry with exponential backoff
  - Strips markdown fences from response before JSON parsing
  - Returns `AIDecision.skip()` on any error — never crashes the trade pipeline
  - Full `AIDecisionLog` audit trail written to DB on every decision (SEBI compliance)
- `services/data_ingestion/news_feed.py` — `NewsFeedService`
  - Polls NewsAPI every 15 minutes
  - Batches 50 symbols into groups of 8 (OR queries) to stay within 100 req/day free tier
  - URL-based deduplication before DB insert
  - 429 rate-limit handled gracefully (log + continue)

### Changed
- `services/execution/trade_executor.py` — Claude AI evaluation inserted between risk check and order placement
  - `ai_confidence` and `ai_reasoning` now persisted on every `Trade` record
  - AI confidence + truncated reasoning appended to Telegram signal alert
- `main.py` — `NewsFeedService` started/stopped in lifecycle
- `main.py` — per-timeframe signal keys (`signal:latest:{symbol}:{tf}`) published to Redis for multi-timeframe AI context assembly

---

## [0.1.1] — 2026-04-07 — Phase 1 Bug Fixes
### Fixed (8 issues from GitHub)
- **[#2]** `websocket_feed.py`: Redis write errors swallowed silently — added try/except + done_callback
- **[#3]** `authenticator.py`: Playwright browser not closed on exception — wrapped in try/finally with explicit timeouts on all page interactions
- **[#4]** `main.py`: unbounded candle buffer (`list`) — replaced with `deque(maxlen=300)`
- **[#5]** `authenticator.py`: TOTP code logged at DEBUG level — replaced `code=totp_code` with `totp_generated=True`
- **[#6]** `order_manager.py`: no rollback on DB commit failure — added try/except/rollback pattern
- **[#15]** `websocket_feed.py` + `main.py`: hardcoded `9:15` market open time — extracted to `config/market_hours.py`; all scheduler jobs guard with `is_trading_day()`
- **[#16]** `indicators.py`: division by zero on zero close price — `safe_close = df["close"].replace(0, np.nan)`; Telegram RR zero-division guarded with log.warning
- **[#17]** `docker-compose.yml`: Redis `allkeys-lru` eviction policy could evict auth tokens — changed to `volatile-lru`

### Added
- `config/market_hours.py` — `is_trading_day()`, `is_market_open()`, `next_market_open()`
- `config/nse_holidays.json` — NSE holiday calendar 2025–2026

---

## [0.1.0] — 2026-04-07 — Phase 1: Foundation
### Added
- `config/settings.py` — Pydantic `BaseSettings`; capital ₹1L; three `AppEnv` modes; computed risk properties
- `database/models.py` — 6 ORM models: `Instrument`, `Order`, `Trade`, `DailyPnL`, `AIDecisionLog`, `NewsItem`
- `database/connection.py` — async SQLAlchemy engine, session factory, Redis async pool
- `docker-compose.yml` — TimescaleDB (PostgreSQL 16) + Redis 7 + Redis Commander
- `services/data_ingestion/websocket_feed.py` — `ZerodhaFeed` (Kite WebSocket), `MockFeed` (random walk with mean reversion for dev), `CandleAggregator` (multi-timeframe OHLCV), `FeedManager`
- `services/data_ingestion/historical_seed.py` — seeds OHLCV from Kite historical API or mock data
- `services/technical_engine/indicators.py` — 30+ indicators: EMA (9/21/50/200), VWAP, ADX, Supertrend, PSAR, RSI, Stochastic, MACD, CCI, Williams %R, MFI, BB, ATR, Keltner, OBV, CMF, RVOL, pivot points, swing highs/lows, derived composites
- `services/technical_engine/signal_generator.py` — 8 signal types with 0–100 confidence scoring; multi-timeframe confluence boost
- `services/risk_engine/engine.py` — 5 pre-trade checks; ATR-based position sizing (₹2K risk/trade); 2:1 R:R
- `services/execution/zerodha/authenticator.py` — daily TOTP re-auth via Playwright + pyotp
- `services/execution/zerodha/order_manager.py` — MARKET/LIMIT/SL-M order placement; dev simulation mode
- `services/execution/trade_executor.py` — full signal → risk → entry → SL → target → Telegram pipeline
- `services/notifications/telegram_bot.py` — trade entry, fill, SL hit, target hit, daily summary, kill-switch, system error, signal alert, market open
- `api/main.py` — FastAPI: 7 REST endpoints + WebSocket `/ws` live feed
- `frontend/` — React 18 + Vite + Tailwind + Recharts dashboard (positions, signals, P&L bar, trade journal)
- `main.py` — bot entry point; APScheduler jobs (8:30 AM auth, 9:10 AM briefing, 3:12 PM square-off, 4:30 PM EOD summary)

---

## [0.0.1] — Initial
### Added
- Repository scaffolding, README, GitHub Actions workflow
