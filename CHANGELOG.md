# Changelog

All notable changes to the trading bot are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]
_Next up: Paper trading validation run (#33) — 2-week live gate before semi-auto_

### Changed (Frontend — Dashboard Redesign)
- **`frontend/src/pages/Dashboard.jsx`** — Full visual redesign with retro-fintech aesthetic. Layout reordered for clearer information hierarchy:
  1. **Masthead header** — `Playfair Display` wordmark, left-bordered mode/regime tags, live IST clock, inline refresh; replaces cluttered badge row
  2. **Hero P&L** — 56px serif number with directional glow (`+₹X,XX,XXX.XX`) as the first visible element; secondary stats (trade count, win rate) shown inline
  3. **Full-width sparkline** — amber/green/red gradient fills, retro tooltip with left-border color coding; replaces 3/5-col cramped layout
  4. **Loss gauge** — percentage-first display with tick-mark bar and ₹ label; replaces `PnLBar` component
  5. **4 secondary stat cards** — amber top-border accent, no icons, cleaner label/value hierarchy
  6. **Serif section dividers** — `Playfair Display` italic labels with hairline rules before Signals and Positions tables
  - Palette: warm near-black `#0d0b07`, amber accent `#c9952a`, parchment text `#e4cfa0`
  - Fonts: `Playfair Display` (display), `JetBrains Mono` (all numeric data), `Inter` (labels)
  - Removed unused `StatCard` and `PnLBar` component imports
- **`frontend/index.html`** — added `Playfair Display` (700, 800) to Google Fonts link

### Fixed (Frontend)
- **`frontend/src/pages/Dashboard.jsx`** — All five backend regime values now correctly styled: `TRENDING_UP` (green ↑), `TRENDING_DOWN` (red ↓), `RANGING` (yellow), `HIGH_VOLATILITY` (orange), `UNKNOWN` (muted). Previously only `TRENDING`, `RANGING`, `UNKNOWN` were mapped.
- **`frontend/src/pages/Dashboard.jsx`** — `fetchBotStatus()` now called on load; bot mode (DEV / PAPER / SEMI_AUTO / LIVE) shown in masthead. The `/api/bot/status` endpoint existed but was never surfaced in the UI.
- **`frontend/src/pages/Settings.jsx`** — `ALL_SIGNAL_TYPES` expanded from 15 → 29 to match all signal types the backend can generate. Added: `HAMMER`, `SHOOTING_STAR`, `ENGULFING_BULL`, `ENGULFING_BEAR`, `MORNING_STAR`, `EVENING_STAR`, `DOUBLE_BOTTOM`, `DOUBLE_TOP`, `BULL_FLAG`, `BEAR_FLAG`, `DARVAS_BREAKOUT`, `NR7_SETUP`, `BREAKOUT_52W`, `VOLUME_THRUST`, `EMA_RIBBON`, `BULL_MOMENTUM`.

### Added
- **`services/momentum_engine/`** — long-only engine for TRENDING_UP markets
  - `signals.py` — `MomentumDetector`: Darvas breakout, 52-week high, EMA ribbon, volume thrust, bull momentum signals; `score_momentum_confluence()` scoring
  - `backtest.py` — `MomentumBacktestEngine`: daily TF only, entry on TRENDING_UP regime, 1.5×/7× ATR (1:4.7 R:R), trailing stop milestones at 1:2/1:3/1:6/1:10, 20-day max hold
  - `run.py` — CLI entrypoint (`python -m services.momentum_engine.run`)
- **`run_combined.py`** — runs both momentum + swing engines on same timeline concurrently; prints side-by-side comparison table + monthly P&L breakdown; saves combined JSON
- **`results/combined_2024.json`** — 2024 full year: momentum +₹1,22,129, swing -₹898, combined +₹1,21,231
- **`results/combined_2026_q1.json`** — 2026 Q1 (trending down): momentum 0 trades, swing 23 trades (20 shorts, 50% WR, +₹4,766, Sharpe 7.40)

### Added (Ops / Infra — #34 #35 #36 #37 #38)
- **`api/main.py`** — `/health` endpoint (#38): liveness + readiness probe (200/503); checks PostgreSQL (`SELECT 1`), Redis (`PING`), Kite access token in Redis (live/semi-auto only); safe for UptimeRobot / ECS health checks
- **`main.py`** — `job_db_backup()` (#37): daily `pg_dump -F c` at 16:45 IST; saves to `backups/trading_bot_{timestamp}.dump`; alerts Telegram on failure; skips silently in dev/paper if `pg_dump` absent
- **`supervisord.conf`** (#35): production process supervisor; manages `bot`, `api`, `frontend` as a `[group:trading]`; auto-restart with configurable retries; logs to `logs/`; graceful SIGTERM shutdown; `supervisorctl` socket for live control
- **`logs/.gitkeep`** — tracked stub so `logs/` directory exists on fresh clone (contents ignored by `.gitignore`)

### Changed (Ops / Infra)
- **`config/settings.py`** (#34): `allowed_origins` env var (default `http://localhost:5173,http://localhost:3000`); `cors_origins` computed property (parses comma-separated string)
- **`api/main.py`** (#34): CORS middleware now uses `settings.cors_origins` — set `ALLOWED_ORIGINS=https://trading.yourdomain.com` in `.env` for production
- **`services/execution/zerodha/order_manager.py`** (#36): `TokenException` mid-session now triggers automatic re-auth via `ZerodhaAuthenticator().authenticate()` then retries the failed order once before raising; `_reauth_and_retry()` helper added
- **`main.py`** (#36): `job_daily_auth()` retries up to 2× on failure using `DateTrigger` (15 min apart); module-level `_scheduler` variable exposes APScheduler instance to retry jobs
- **`.gitignore`** — `!logs/.gitkeep` negation added so gitkeep is tracked despite `logs/` being ignored

### Changed (Signal Engine)
- **`services/ai_strategy/prompts.py`** — `build_market_briefing_prompt()` now accepts `regime` and `news_headlines` params; includes last 12h news in Claude's context
- **`services/ai_strategy/claude_client.py`** — added `get_market_briefing()` method: calls Claude with `MARKET_BRIEFING_SYSTEM` prompt, returns 3-4 sentence plain-text briefing, falls back to canned string on API failure
- **`main.py`** — `job_market_open_briefing()` replaced canned message with real Claude research: fetches Nifty change %, VIX, regime from Redis + last 12h news headlines, asks Claude for briefing, sends result via Telegram

### Backtest Results — Dual Engine (same timeline)

| Period | Momentum | Swing | Combined |
|---|---|---|---|
| 2024 full year (bull + ranging) | +₹1,22,129 (9 trades, 44% WR) | -₹898 (68 trades) | +₹1,21,231 |
| 2026 Q1 (trending down) | ₹0 (0 trades — correctly sat out) | +₹4,766 (23 trades, 20 shorts, 48% WR) | +₹4,766 |

**Key validation:** Momentum engine correctly fires only in TRENDING_UP (3/87 days in 2026 Q1 → zero trades). Swing engine shorts dominate TRENDING_DOWN phases. Engines are genuinely complementary.

---

## [0.5.0] — 2026-04-14 — Short-Side Improvements + Full Top-Down Backtesting

### Added
- **`services/technical_engine/signal_generator.py`** — 7 new candlestick & chart pattern signal types:
  - `HAMMER`, `SHOOTING_STAR`, `ENGULFING_BULL`, `ENGULFING_BEAR`, `MORNING_STAR`, `EVENING_STAR` — classic candlestick patterns with body/wick ratio validation
  - `DOUBLE_BOTTOM`, `DOUBLE_TOP` — W/M patterns using swing high/low columns; 8+ bar separation, neckline break required
  - `BULL_FLAG`, `BEAR_FLAG` — pole ≥3% move over 3–10 bars, flag consolidation <60% pole range, breakout with RVOL
  - `DARVAS_BREAKOUT` — 15-bar box consolidation, breakout above box_top with RVOL
  - `NR7_SETUP` — narrowest daily range of last 7 bars (volatility contraction before expansion)
- **`services/backtesting/engine.py`** — `--trading-mode swing|intraday` flag:
  - `swing`: Daily setup → 1H trigger → 5-day max hold, no EOD exit
  - `intraday`: 1H setup → 15min trigger → 20-bar max hold, EOD exit at 15:20
- **`services/backtesting/engine.py`** — Three-level regime stack:
  - L1: Nifty market regime gate — skip stocks that contradict Nifty direction
  - L2: India VIX gate — EXTREME (>25) = no trades, HIGH (20–25) = HIGH_VOLATILITY regime
  - L3: News/Claude — live only, not applied in backtesting
- **`services/backtesting/engine.py`** — `min_confirming_signals` param: require N distinct signal types in same direction before entering
- **`services/backtesting/run.py`** — `--trading-mode` and `--min-confirming-signals` CLI flags

### Changed (Short-Side Improvements P1–P4, P7)
- **P1 — Nifty 200 EMA hard gate** (`engine.py`): When Nifty 200-period EMA is rising (bull phase), all short/bearish setups are skipped entirely. Short WR improved from 6.2% → 37.7% in 2025 backtest.
- **P2 — BREAKOUT_LOW filters** (`signal_generator.py`): Base confidence reduced 50→40; RVOL threshold raised to 1.8× (was 1.5×); consolidation width filter (≥2× ATR → +15, else -10); RSI oversold penalty (<30 → -20, <40 → -10); round number support trap penalty (within 0.5% of ₹100 multiple → -15).
- **P3 — DOUBLE_TOP improvements** (`signal_generator.py`): Price tolerance tightened 2.5%→2.0%; base confidence 70→65; RVOL required on neckline break (>1.5 → +15, else -15, based on Bulkowski: no-volume breaks fail 71%); EMA stack == -1 adds +10; RSI divergence check (right peak lower RSI than left peak → +15, else -10).
- **P4 — Intraday short stop widening** (`engine.py`): Intraday shorts now use 2×ATR stop / 5×ATR target (1:2.5 R:R) instead of default 1.5×/3×. Prevents gap-up opens blowing through tight stops.
- **P7 — Dead cat bounce state machine** (`engine.py`): BREAKOUT_LOW entries now require a confirmed retest. State machine: IDLE → BROKEN (initial breakdown, entry blocked) → BOUNCED (≥0.4× ATR bounce detected) → retest below breakdown = entry allowed with +20 confidence bonus. Expires after 8 bars.
- **Swing mode R:R** (`engine.py`): Swing trades use 2×/6× ATR (1:3 R:R) instead of default 1.5×/3×.
- **yfinance 1H data fix** (`engine.py`): 1H interval now uses 730-day lookback (was incorrectly capped at 60 days same as 15min). Enables full 2025 swing backtesting.

### Backtest Results — 2025 Swing (first clean daily→1H top-down run)

| Metric | Value |
|---|---|
| Period | 2025-01-01 → 2025-12-31 |
| Total Trades | 1,276 |
| Win Rate | 35.2% |
| Net P&L | ₹+7,088 |
| Profit Factor | 1.08× |
| Sharpe | 0.50 |
| Max Drawdown | ₹-6,267 |

| Direction | Trades | WR | P&L |
|---|---|---|---|
| LONG | 1,077 | 34.7% | ₹+4,576 |
| SHORT | 199 | **37.7%** | **₹+2,512** |

Top signal performers: BREAKOUT_HIGH (41.7% WR, ₹+3,111), DOUBLE_TOP (38.9% WR, ₹+1,953), ENGULFING_BEAR (40.2% WR, ₹+1,719).
Biggest drag: DOUBLE_BOTTOM (229 trades, 32.3% WR, ₹-1,311).

---

## [0.4.4] — 2026-04-09 — Signal Threshold Tuning (Backtest-Driven)

### Changed
- **`config/bot_config.py`** — Added two new per-signal confidence floor parameters, tunable live from the Settings dashboard:
  - `orb_min_confidence` (default **70**, was effectively 65) — raised based on backtest ORB win rate of 38%, which is marginal at 2:1 R:R
  - `vwap_min_confidence` (default **70**, was effectively 60) — raised based on backtest VWAP_RECLAIM win rate of 39%
- **`services/technical_engine/signal_generator.py`** — Applied per-signal-type minimum confidence filter in `MultiTimeframeSignalEngine.analyse()` after regime filter. ORB and VWAP signals below their respective floors are now dropped before reaching Claude AI.

### Closed stale issues
- #10 Backtesting framework — shipped in v0.3.0
- #11 DailyPnL materialisation — shipped in v0.4.0
- #12 Paper trading infrastructure — shipped in v0.4.0
- #13 Human approval gate (semi-auto) — shipped in v0.3.0

### Production roadmap issues opened
- #32 Signal threshold tuning (this change)
- #33 Paper trading validation run (2-week gate)
- #34 CORS tighten for production
- #35 Server deployment + process supervisor
- #36 Kite re-auth failure handling + retry
- #37 Database backups (daily pg_dump)
- #38 External health monitoring
- #39 Semi-auto live run (first 2 weeks on real capital)
- #40 Fully automated live mode

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
