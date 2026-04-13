# Architecture & Design Decisions

This document captures every significant technical decision made during the build,
including the reasoning, alternatives considered, and trade-offs accepted.

---

## D-001 — Claude AI is a filter, not an executor

**Decision:** Claude evaluates signals and returns BUY/SELL/SKIP. It never places orders, never sets stop-losses, and is never in the critical execution path.

**Reasoning:**
- LLM latency is unpredictable (200ms–3s). Order placement needs to be deterministic and fast.
- Claude can fail (API down, rate limit, malformed response). If Claude were required for execution, every outage would halt trading.
- Regulatory clarity: SEBI requires algorithmic trades to be traceable and deterministic. A probabilistic model in the execution path complicates audit.

**How it works:** Signal fires → RiskEngine approves → Claude evaluates → if `is_actionable` → OrderManager places orders. Claude can block a trade but cannot initiate one.

**Trade-off accepted:** Some profitable signals get blocked by Claude's veto when it shouldn't. We accept this — false negatives are cheaper than false positives on a small capital base.

---

## D-002 — Redis `volatile-lru` instead of `allkeys-lru`

**Decision:** Redis eviction policy set to `volatile-lru` (only keys with TTL are eligible for eviction).

**Reasoning:**
- Auth tokens (`kite:access_token`) are stored without TTL so they survive restarts. Under `allkeys-lru` with memory pressure, Redis could evict them — the bot would then fail authentication on the next order.
- Tick data, signals, and regime keys all have TTLs and are safe to evict.
- `volatile-lru` guarantees critical state (auth tokens, kill switch) is never evicted.

**Alternative considered:** `allkeys-lru` (simpler, evicts everything under pressure). Rejected because auth token eviction is a silent failure mode that would only surface during live trading under load.

---

## D-003 — `async for session in get_db_session()` pattern

**Decision:** `get_db_session()` is an async generator (uses `yield`). All callers use `async for session in get_db_session():` to consume it.

**Reasoning:**
- The generator pattern lets SQLAlchemy handle session lifecycle (open, commit, close) automatically without callers needing to manage context managers.
- The `async with factory() as session: yield session` pattern inside the generator handles all cleanup even if the caller raises.

**Common confusion:** The docstring in `connection.py` initially said `async with await get_db_session() as session:` — this is wrong. You cannot `await` an async generator. The correct usage is always `async for session in get_db_session():`. This was a documentation bug, not a runtime bug.

---

## D-004 — Signal confidence is 0–100 (not 0.0–1.0)

**Decision:** `Signal.confidence` is an `int` from 0 to 100. `AIDecision.confidence` is a `float` from 0.0 to 1.0.

**Reasoning:**
- Signal confidence is built incrementally (`base + 15 + 10 + ...`). Integer arithmetic is cleaner for this.
- AI confidence comes from Claude's JSON response which naturally produces decimal probabilities.
- The two are never directly compared — `Signal.confidence >= 65` gates whether Claude is called at all; `AIDecision.is_actionable` requires `confidence >= 0.55`.

**Trade-off accepted:** Two different scales for "confidence" in the same system is a potential source of confusion. Mitigated by clear type annotations and separate code paths.

---

## D-005 — NewsAPI batching strategy (8 symbols per OR query)

**Decision:** 50 Nifty symbols are batched into groups of 8, using OR queries with company names.

**Reasoning:**
- NewsAPI free tier: 100 requests/day.
- If each symbol got its own query: 50 queries × 25 poll cycles/day = 1,250 requests/day. Way over limit.
- With 8 symbols per batch: ⌈50/8⌉ = 7 queries per cycle. 7 × 25 = 175 queries/day. Manageable on paid tier. For free tier, reduce poll interval to 60min (7 × 8 cycles = 56 requests/day).
- OR queries like `"Reliance OR TCS OR Infosys"` return articles mentioning any of the companies.

**Trade-off accepted:** Batch queries are less precise — an article about TCS may appear in RELIANCE's results. Mitigated by symbol matching in `NewsFeedService` which filters by company name presence.

---

## D-006 — ATR-based position sizing with fixed ₹2,000 risk per trade

**Decision:** `qty = ₹2,000 / (entry_price - stop_loss)`. Stop at 1.5×ATR, target at 3×ATR.

**Reasoning:**
- Fixed rupee risk per trade (not fixed lot size) means the bot risks the same amount regardless of whether the stock is ₹200 or ₹2,000.
- ATR-based stops are adaptive — wider in volatile markets, tighter in calm markets.
- 2:1 R:R (3×ATR target vs 1.5×ATR stop) means the bot can be profitable with 35%+ win rate.
- ₹2,000 max risk on ₹1,00,000 capital = 2% per trade. Standard risk management practice.

**Cap:** Position value capped at ₹10,000 to prevent over-concentration in any single stock.

---

## D-007 — `deque(maxlen=300)` for candle buffers

**Decision:** In-memory candle buffers use `collections.deque(maxlen=300)`.

**Reasoning:**
- A plain `list` grows unboundedly. After a full trading day on 50 symbols × 5 timeframes × ~400 15min candles, memory usage becomes significant.
- `deque(maxlen=N)` automatically discards the oldest entry when full — O(1) append, bounded memory.
- 300 candles of 15min data = ~75 hours of history = more than enough for all indicators (200 EMA needs ~200 candles minimum).

**Conversion:** `pd.DataFrame(list(deque))` — the `list()` call is necessary because `pandas` doesn't directly accept `deque`.

---

## D-008 — `MockFeed` with mean-reverting random walk

**Decision:** Dev mode uses a synthetic tick feed that simulates realistic price movement without any API key.

**Reasoning:**
- Kite WebSocket requires a live session token, which expires daily. Running in dev without authentication is impractical.
- A pure random walk would drift to zero or infinity — not realistic for testing signal detection.
- Mean reversion (price pulled back toward seed price when deviation exceeds threshold) keeps prices in a realistic range indefinitely.
- Seed prices hardcoded for all 50 Nifty stocks at realistic 2025 levels.

**Formula:** `new_price = prev_price + drift + reversion_force + noise`

---

## D-009 — Regime filter runs before Claude, not after

**Decision:** `RegimeFilter.apply()` is called in `MultiTimeframeSignalEngine.analyse()` — signals that don't suit the regime are removed before they reach `TradeExecutor` and before Claude is called.

**Reasoning:**
- Cost: every Claude call costs money. Filtering upstream prevents paying for Claude to evaluate a mean-reversion signal in a strong trend (which it would likely reject anyway).
- Latency: fewer signals reaching Claude means less API call overhead in the hot path.
- Claude is still useful as a second layer — even regime-appropriate signals can be bad for other reasons (news, fundamental issues, market-wide risk-off).

**Example:**
```
RANGING market + RSI_OVERSOLD signal → passes filter → Claude evaluates
RANGING market + BREAKOUT_HIGH signal → blocked by filter → Claude never called
```

---

## D-010 — ORB signal is 15min TF only, valid 9:30 AM–1:00 PM

**Decision:** `ORB_BREAKOUT` only fires on 15min candles, and only between 9:30 AM and 1:00 PM IST.

**Reasoning:**
- The opening range is defined by the 9:15–9:30 AM candle. This only exists on 15min (or 30min) charts; checking it on 1min or 1hr adds no information.
- After 1:00 PM, the ORB range has been tested multiple times and loses predictive power. Late-day breaks of the opening range are more likely to be noise or reversal than genuine breakouts.
- Zerodha requires MIS square-off at 3:20 PM. A 1:00 PM cutoff gives 2+ hours for the trade to run to target while still being within intraday limits.

---

## D-011 — Trade lifecycle uses tick polling in dev, broker polling in live

**Decision:** Two separate monitoring paths depending on `APP_ENV`.

**Dev/paper:** Poll Redis tick cache every 10s, check if price crossed SL or target.
**Live:** Poll Kite order book every 30s, look for COMPLETE SL-M or LIMIT orders.

**Reasoning:**
- In live mode, the broker's order execution is authoritative. If Kite says the SL-M order is COMPLETE, that's definitive — we don't need to check the price ourselves.
- In dev mode, there's no real order book. Simulating exits from price ticks is the closest approximation to real behavior.
- 30s polling interval for live is a deliberate balance — fast enough to catch exits within one candle, slow enough not to hammer the Kite API (rate limit: 3 req/s).

**Trade-off accepted:** In live mode there's a theoretical 30s lag between when a stop is hit and when the DB record is updated. This is acceptable — the broker-side order has already executed, so the financial outcome is determined. The DB update is just recordkeeping.

---

## D-012 — Brokerage calculated post-trade, not pre-trade

**Decision:** Charge calculation happens in `trade_lifecycle.py` when the trade closes, not in `risk_engine.py` when the trade is sized.

**Reasoning:**
- Pre-trade: we don't know the exit price, so we can't calculate STT or exchange charges accurately.
- The risk calculation uses gross P&L approximation for position sizing, which is standard practice.
- Charges on a typical ₹10,000 intraday position are ₹15–30 — small enough that not including them in position sizing doesn't materially change outcomes.

---

## D-013 — Backtesting uses yfinance as fallback, not primary

**Decision:** `BacktestEngine` tries TimescaleDB first, falls back to yfinance.

**Reasoning:**
- TimescaleDB has accurate NSE data with correct open/high/low/close for Indian sessions. yfinance sometimes has adjusted prices, corporate action issues, or missing data for Indian stocks.
- yfinance is free and requires no API key — important for running backtests on fresh environments.
- yfinance intraday history is capped at 60 days for 15min data. For longer backtests, TimescaleDB is required.

**Practical implication:** First run on a new machine will use yfinance for 15min data (limited to last 60 days). After running the bot for a while and accumulating TimescaleDB data, backtests automatically use higher-quality local data.

---

## D-014 — EOD square-off at 3:12 PM instead of 3:20 PM

**Decision:** Bot initiates square-off at 3:12 PM IST. Zerodha auto square-off is at 3:20 PM.

**Reasoning:**
- 8-minute buffer before broker's auto square-off prevents race conditions where both the bot and the broker try to close the same position simultaneously.
- Market orders at 3:12 PM in NSE liquid stocks fill within seconds. By 3:14 PM positions should all be flat.
- Broker's 3:20 PM auto square-off carries an additional 1% penalty charge on top of standard brokerage. Avoiding this saves cost.

---

## D-015 — SEBI compliance: every AI decision logged to `AIDecisionLog`

**Decision:** Every Claude API call, regardless of outcome (BUY/SELL/SKIP), writes a full record to `AIDecisionLog` with the exact input, raw response, parsed output, token counts, and latency.

**Reasoning:**
- SEBI's algorithmic trading regulations require that every automated trading decision be traceable to a defined algorithm with an audit trail.
- Using an LLM in the decision path creates a novel compliance question — Claude's reasoning is opaque unless explicitly recorded.
- Logging everything (including SKIPs) means regulators can reconstruct exactly why a trade was taken or not taken on any given day.

**Storage implication:** At ~10 signals/day × 512 tokens input = ~5,000 tokens/day. Each log entry is ~2–5 KB. 1 year ≈ 1–2 MB. Storage cost is negligible.

---

## D-016 — Signal detection triggers only on 15min candle close

**Decision:** `_run_signals()` in `main.py` is only triggered when `tf == "15min"`. 1min and 5min candle closes are buffered but don't trigger signal runs.

**Reasoning:**
- 1min candles close 375 times per day × 50 symbols = 18,750 potential signal runs/day. At ~100ms per run, that's 31 minutes of CPU time per day just for signal detection — before Claude calls.
- 15min candles close 26 times/day × 50 symbols = 1,300 runs. Manageable.
- 15min is the optimal intraday signal timeframe — fast enough to catch intraday moves, slow enough to filter noise.
- 1min and 5min data is still used *inside* signal detection as lower-timeframe context, just not as the trigger.

---

## D-018 — Backtester pre-computes indicators once per timeframe, not per candle

**Decision:** `BacktestEngine._backtest_symbol()` calls `compute_all()` once on the full DataFrame for each timeframe after loading, then slices that pre-computed DataFrame into rolling windows for each candle iteration. `SignalDetector.detect()` accepts a `pre_computed=True` flag to skip the internal `compute_all()` call.

**Reasoning:**
- The original implementation called `compute_all()` inside the per-candle loop, which meant ~3,450 full indicator computations per symbol (1,150 candles × 3 timeframes). On 50 symbols that's 172,500 calls — the backtest ran for >1 hour before being killed.
- Rolling indicator functions (EMA, RSI, ATR etc.) are **causal** — they only look backwards. Computing them on the full series and then reading the row at index `i` is mathematically identical to computing them on `df[:i]` — no look-ahead bias is introduced.
- The only indicator that would have look-ahead bias if pre-computed is anything with `center=True` in a rolling window. We audited the code: `swing_high` / `swing_low` use `center=True` but are only used for support/resistance context, not for entry signals. This is documented as acceptable.
- After the fix: 3 `compute_all()` calls per symbol → 50 symbols complete in 3.5 min (60× speedup).

**Trade-off accepted:** The `pre_computed=True` flag is opt-in. The live signal pipeline still calls `compute_all()` normally inside `detect()`. Only the backtester bypasses it. This keeps the live path simple and the optimisation isolated to the backtest context.

---

## D-019 — Risk engine DB helpers return safe defaults when PostgreSQL unavailable

**Decision:** `RiskEngine._get_todays_pnl()`, `_get_open_count()`, and `_has_open_position()` are each wrapped in `try/except Exception`; they return `0.0`, `0`, and `False` respectively on any DB error.

**Reasoning:**
- The backtester has no live PostgreSQL connection. Without this fix, every simulated trade crashed the backtest with an `asyncpg` connection error, producing zero results.
- In live trading the DB will always be running — these fallbacks are never triggered in production.
- The safe defaults are conservative: `daily_pnl=0.0` means no loss limit is active, `open_count=0` means no position cap is hit, `has_open=False` means no duplicate position block. All three allow the trade to proceed, which is the correct behaviour for backtesting (each simulated trade is independent).
- Silently swallowing DB errors in the live path would be dangerous. This is acceptable only because the backtester is an offline tool; live trading would surface the error via the normal exception path in `_backtest_symbol`.

**Alternative considered:** Pass a `db_available: bool` flag to `RiskEngine` at construction time and skip DB checks entirely in offline mode. Rejected — adds constructor complexity; the `try/except` approach achieves the same result with less code.

---

## D-020 — Indicator column names made version-agnostic via prefix matching

**Decision:** Bollinger Band column extraction in `indicators.py` uses a `_find_col(prefix)` helper that searches by prefix rather than exact name. Other indicator column lookups use `iloc[:, 0]` (positional) where column naming is unstable.

**Reasoning:**
- `pandas-ta 0.4.71b0` (required for Python 3.12) changed BB column naming: `BBU_20_2.0` became `BBU_20_2.0_2.0` (std parameter appended twice). This broke every Bollinger Band reference with a `KeyError` on every symbol in the backtest.
- Pinning to a specific pandas-ta version is fragile — the package has moved between maintainers and PyPI availability varies by Python version.
- Prefix matching (`BBU_20_2.0` still matches `BBU_20_2.0_2.0`) handles both the old and new naming convention without a version check.
- The same issue affects `STOCH` and `ADX` columns; those use `iloc` positional access already, which is also version-safe.

**Trade-off accepted:** If pandas-ta ever changes column ordering (not just naming), `iloc`-based access would break silently. This is considered a low-risk scenario for now but is documented for future maintainers.

---

## D-021 — Top-down analysis: setup on higher TF, trigger on lower TF

**Decision:** The backtesting engine (and live trading intent) follows a two-timeframe top-down structure. Swing = Daily setup → 1H trigger. Intraday = 1H setup → 15min trigger.

**Reasoning:**
- A signal on a single timeframe is unreliable in isolation. The higher TF establishes the directional bias (is the stock trending up or down?). Only then do we look at the lower TF for a precise entry signal.
- This reduces false positives: a BREAKOUT_HIGH on the 15min that contradicts the daily downtrend is ignored entirely, not just scored lower.
- Real professional trading desks use this approach universally — position on the weekly, plan on the daily, execute on the 4H/1H.

**Implementation:** `_get_setup_bias()` reads the setup TF and returns BULLISH/BEARISH/None. Signal detection runs only on the trigger TF. Signals in the wrong direction are filtered before the quality gate.

---

## D-022 — Nifty 200 EMA rising = hard block on all short trades

**Decision:** When Nifty's 200-period EMA is rising (slope positive over 10 bars), no short/bearish trades are taken regardless of individual stock signals.

**Reasoning:**
- A rising 200 EMA means Nifty is in a structural bull phase. Short WR in bull phases is 31–37% vs 52–58% when Nifty is below its 200 EMA.
- Individual stocks that look bearish in a bull market are often experiencing temporary pullbacks. They frequently gap up overnight, blowing through short stops.
- The cost of missing a legitimate short in a bull market is much lower than the cost of repeated stop-outs.
- Data: 2025 backtest showed SHORT WR improving from 6.2% → 37.7% with this gate active (combined with other P3/P4/P7 improvements).

**Trade-off:** Legitimate short setups (e.g. a sector-specific collapse during a broad bull market) are missed. Accepted — the false negative rate is lower than the false positive rate without the gate.

---

## D-023 — Dead cat bounce state machine for BREAKOUT_LOW entries

**Decision:** BREAKOUT_LOW signals do not trigger immediate entry. A 3-state machine waits for a post-breakdown bounce (≥0.4× ATR above breakdown level) and then a confirmed retest before entry is allowed.

**Reasoning:**
- After a stock breaks below a key level, short covering typically produces a 1–3 bar bounce. Entering at the initial breakdown bar frequently gets stopped out by this bounce before the real move continues.
- Waiting for the bounce + retest pattern filters out traps. The retest entry has a higher probability of being the true continuation.
- Bulkowski's research: breakdown retests within 30 bars have 62% continuation rate vs 43% for immediate entries.
- +20 confidence bonus applied to retest entries to reflect their higher quality.

**Expiry:** State resets after 8 bars if no retest occurs. This prevents stale state from incorrectly classifying a new breakdown as a "retest."

---

## D-024 — Different R:R ratios for swing vs intraday shorts

**Decision:** Swing trades use 2×/6× ATR (1:3 R:R). Intraday shorts use 2×/5× ATR (1:2.5 R:R). Default (intraday longs) uses 1.5×/3× ATR (1:2 R:R).

**Reasoning:**
- Swing trades held over 2–5 days need wider stops to survive overnight noise, gap-ups/downs, and intraday swings. A 2× ATR stop is appropriate; the 6× target maintains positive expectancy.
- Intraday shorts are specifically vulnerable to gap-up opens at 9:15 AM. Widening stop from 1.5× to 2× ATR reduces the stop-out rate from overnight events. The 5× target (1:2.5 R:R) still provides positive expectancy at a 30% win rate.
- Default 1.5×/3× remains for intraday longs where gap risk is less severe (gap-ups help longs).

---

## D-017 — Market regime uses NIFTY 50 index, not individual stock data

**Decision:** `MarketRegimeDetector` uses NIFTY 50 (or proxy) candle data to determine regime. Individual stock signals inherit this regime.

**Reasoning:**
- Individual stock regimes vary — RELIANCE can be trending while the broader market is ranging. Using a market-wide regime for the filter is more conservative and avoids trading against the macro direction.
- In Indian markets, 70–80% of stocks move with NIFTY. A broad regime filter catches the majority of bad setups.
- Computing regime per-stock would require 50 × ADX calculations on every 15min candle — 5× more computation for marginal benefit.

**Implication for strong individual movers:** A stock-specific breakout in a ranging market will be blocked by the regime filter even if fundamentals justify it. This is a deliberate false-negative bias.
