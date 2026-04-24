"""
main.py
────────
Trading bot entry point.

Starts all services in sequence:
  1. Database + Redis connection check
  2. Historical data seed (first run only)
  3. Market data feed (live or mock)
  4. Signal monitoring loop
  5. Scheduled jobs (daily auth, EOD summary, etc.)

Usage:
  python main.py               # development mode (mock feed)
  APP_ENV=paper python main.py # paper trading
  make live                    # live trading (with confirmation prompt)
"""
from __future__ import annotations

import asyncio
import signal
import sys
from collections import deque
from datetime import datetime, timedelta

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from config.settings import settings
from database.connection import close_db, close_redis, get_db_session, get_redis, init_db
from services.data_ingestion.historical_seed import HistoricalSeeder
from services.data_ingestion.news_feed import get_news_service
from services.data_ingestion.websocket_feed import FeedManager, OHLCVCandle
from services.execution.trade_lifecycle import get_lifecycle_manager
from services.market_regime.detector import get_regime_detector
from services.notifications.telegram_bot import get_notifier
from services.execution.trade_executor import TradeExecutor
from services.technical_engine.signal_generator import Signal
# MultiTimeframeSignalEngine (swing engine) — preserved but not used in live routing

# ─── Logging Setup ────────────────────────────────────────────────────────────

import logging
logging.basicConfig(
    format="%(message)s",
    level=logging.INFO,
)

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
        structlog.dev.ConsoleRenderer(colors=True),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)

log     = structlog.get_logger("main")
console = Console()

# ─── In-memory OHLCV buffer ───────────────────────────────────────────────────
# deque(maxlen=BUFFER_MAX) automatically discards the oldest entry when full,
# giving O(1) append and bounded memory regardless of how long the bot runs.
# Format: {symbol: {timeframe: deque([candle_dicts...], maxlen=BUFFER_MAX)}}
_candle_buffer: dict[str, dict[str, deque]] = {}
_active_signal_tasks: set[str] = set()   # Symbols with an in-flight signal task
_signal_semaphore: asyncio.Semaphore | None = None  # Initialised in main() after event loop starts
BUFFER_MAX = 300   # Keep last 300 candles per symbol/timeframe
_scheduler: AsyncIOScheduler | None = None   # Set in main(); used by retry jobs
_tick_count: int = 0                          # Rolling tick counter for diagnostics


# ─── Candle Handler ───────────────────────────────────────────────────────────

def on_candle_complete(candle: OHLCVCandle) -> None:
    """
    Called every time a candle period closes.
    Adds to buffer and triggers signal generation.
    """
    global _tick_count
    sym = candle.trading_symbol
    tf  = candle.timeframe

    if sym not in _candle_buffer:
        _candle_buffer[sym] = {}
    if tf not in _candle_buffer[sym]:
        _candle_buffer[sym][tf] = deque(maxlen=BUFFER_MAX)

    _candle_buffer[sym][tf].append({
        "open":   candle.open,
        "high":   candle.high,
        "low":    candle.low,
        "close":  candle.close,
        "volume": candle.volume,
        "ts":     candle.timestamp,
    })

    buf_len = len(_candle_buffer[sym][tf])
    # Only print 15min candles — 1min would flood the terminal (50 symbols × 1/min)
    if tf == "15min":
        print(f"[{datetime.now().strftime('%H:%M:%S')}] candle.closed_15min  {sym}  bars={buf_len}  close={candle.close:.2f}", flush=True)

    # Run signal detection on the 15min candle close
    # (avoids running on every 1min candle — too noisy)
    # Guard: skip if a signal task is already running for this symbol
    if tf == "15min" and sym not in _active_signal_tasks:
        task = asyncio.create_task(_run_signals(sym))
        _active_signal_tasks.add(sym)
        task.add_done_callback(lambda _: _active_signal_tasks.discard(sym))


async def _run_signals(symbol: str) -> None:
    """
    Regime-gated signal detection for a symbol.

    Routing logic (D-025):
      TRENDING_UP   → Momentum engine (daily TF, long-only)
      TRENDING_DOWN → Swing engine (daily→1H, shorts dominate)
      RANGING       → Swing engine (mean-reversion signals)
      UNKNOWN       → Swing engine (safe default during startup)

    Both engines feed the same TradeExecutor → Claude → broker pipeline.
    """
    # Throttle: with 2000+ symbols, all first 15min candles close simultaneously.
    # Without a semaphore this creates 2000+ concurrent Redis operations and exhausts
    # the connection pool (max_connections=100). Queue tasks; run at most 75 at once.
    sem = _signal_semaphore
    if sem is not None:
        await sem.acquire()
    try:
        from config.market_hours import is_market_open
        if not is_market_open():
            return

        # Check for macro shock override (set by morning briefing)
        redis_check = get_redis()
        news_alert = await redis_check.get("market:news_alert")
        if news_alert:
            regime_override = news_alert
        else:
            regime_override = None

        import json
        import pandas as pd
        from config.bot_config import get_bot_config
        from services.momentum_engine.live import MomentumLiveEngine

        cfg   = await get_bot_config()
        redis = get_redis()
        ohlcv_by_tf: dict[str, pd.DataFrame] = {}

        # Timeframes used for signal detection.
        # 1min is excluded — too noisy for trade signals, only used for tick aggregation.
        # Swing:   1day + 1hr
        # Intraday: 1hr + 15min (+ 5min as supplementary context)
        SIGNAL_TIMEFRAMES = {"1day", "1hr", "15min", "5min"}
        TF_MIN_BARS = {"1day": 30, "1hr": 14, "15min": 14, "5min": 14}
        for tf, candles in _candle_buffer.get(symbol, {}).items():
            if tf not in SIGNAL_TIMEFRAMES:
                continue
            min_bars = TF_MIN_BARS.get(tf, 14)
            if len(candles) >= min_bars:
                ohlcv_by_tf[tf] = pd.DataFrame(list(candles)).set_index("ts")

        if not ohlcv_by_tf:
            log.debug("signal.no_buffer", symbol=symbol)
            return

        # Update market regime when the daily candle closes (EOD only).
        # Intraday regime comes from _bootstrap_regime() at startup.
        REGIME_PROXY = "NIFTY 50"
        if symbol == REGIME_PROXY and "1day" in ohlcv_by_tf:
            vix_raw   = await redis.get("market:tick:INDIA VIX")
            india_vix = json.loads(vix_raw).get("lp") if vix_raw else None
            await get_regime_detector().detect_and_publish(
                ohlcv_by_tf["1day"], india_vix=india_vix
            )

            # Cache momentum gate context in Redis for MomentumLiveEngine:
            #   Gate 1b → is Nifty 200 EMA rising?
            #   Gate 1c → how many consecutive TRENDING_UP days so far?
            # Both keys have 48h TTL so weekend/holiday gaps don't wipe state.
            try:
                from services.technical_engine.indicators import compute_all as _compute_all
                _nifty_df = _compute_all(ohlcv_by_tf["1day"].copy())

                # Gate 1b: 200 EMA slope (compare vs 10 bars ago)
                _ema200_rising = False
                if "ema_200" in _nifty_df.columns:
                    _ema200 = _nifty_df["ema_200"].dropna()
                    if len(_ema200) >= 12:
                        _ema200_rising = bool(_ema200.iloc[-1] > _ema200.iloc[-11])
                await redis.setex(
                    "momentum:nifty_200ema_rising", 48 * 3600,
                    "1" if _ema200_rising else "0",
                )

                # Gate 1c: consecutive TRENDING_UP days (walk full buffer history)
                _consec = 0
                for _, _row in _nifty_df.iterrows():
                    _adx  = _row.get("adx")
                    _stk  = _row.get("ema_stack", 0) or 0
                    if _adx is None or pd.isna(_adx):
                        continue
                    if _adx < 20:
                        _consec = 0
                    elif _stk >= 0:
                        _consec += 1
                    else:
                        _consec = 0
                await redis.setex(
                    "momentum:nifty_consec_up", 48 * 3600, str(_consec),
                )

                # RS gate: Nifty 20-day ROC for relative strength calculations
                # stock_roc20 - this value = how much the stock is outperforming
                _nifty_roc20 = 0.0
                if "close" in _nifty_df.columns and len(_nifty_df) >= 21:
                    _cl = _nifty_df["close"].dropna()
                    if len(_cl) >= 21:
                        _nifty_roc20 = float((_cl.iloc[-1] / _cl.iloc[-21] - 1) * 100)
                await redis.setex(
                    "momentum:nifty_roc20", 48 * 3600, str(round(_nifty_roc20, 2)),
                )
                log.debug(
                    "momentum.nifty_context_updated",
                    ema200_rising=_ema200_rising, consec_up=_consec,
                    nifty_roc20=round(_nifty_roc20, 2),
                )
            except Exception as _e:
                log.warning("momentum.nifty_context_error", error=str(_e))

        regime = regime_override or await redis.get("market:regime") or "UNKNOWN"

        # ── VIX emergency override (beats the 10:15 AM regime lock) ──────────
        # The regime lock prevents noise-driven flips but would miss a genuine
        # intraday shock (circuit breaker, surprise RBI decision, geopolitical event).
        # India VIX tick is always live in Redis (WebSocket, ~30s TTL) so this
        # check reflects real-time fear regardless of what the locked regime says.
        vix_live_raw = await redis.get("market:tick:INDIA VIX")
        if vix_live_raw:
            try:
                live_vix = float(json.loads(vix_live_raw).get("lp", 0) or 0)
                if live_vix > 20.0:
                    regime = "HIGH_VOLATILITY"
                    log.warning("regime.vix_emergency_override", vix=live_vix)
            except Exception:
                pass

        # ── Regime-gated engine routing ───────────────────────────────────────
        #
        # Strategy: LONG-ONLY, momentum engine only (swing engine code preserved).
        #
        # Regime handling is done inside MomentumLiveEngine.detect():
        #   HIGH_VOLATILITY  → hard block here (VIX > 20 or macro shock)
        #   TRENDING_UP      → fire freely
        #   RANGING          → blocked (23% WR even with RS filter — too noisy)
        #   TRENDING_DOWN    → fire only if stock RS > Nifty 20d ROC by ≥8%
        #                      (sector rotation leaders — defense, PSU, sugar etc.)
        #   UNKNOWN          → treated same as TRENDING_UP
        #
        signals = []

        if regime == "HIGH_VOLATILITY":
            log.info("signal.blocked_high_volatility", symbol=symbol)
            return

        # Signals that only make sense intraday (VWAP resets daily, ORB is 9:15-9:30)
        _INTRADAY_ONLY = frozenset({"VWAP_RECLAIM", "ORB_BREAKOUT"})

        # Momentum engine — regime/RS logic handled inside MomentumLiveEngine
        if "1day" in ohlcv_by_tf:
            momentum_engine = MomentumLiveEngine()
            signals = await momentum_engine.detect(
                symbol   = symbol,
                daily_df = ohlcv_by_tf["1day"],
                regime   = regime,
                redis    = redis,
            )

        # Long-only: short-side code preserved but disabled from paper/live
        signals = [s for s in signals if s.direction.value == "BULLISH"]

        if not signals:
            log.info("signal.none", symbol=symbol, regime=regime, timeframes=list(ohlcv_by_tf.keys()))
            return

        # Filter intraday-only signals when the top signal comes from daily timeframe
        # (VWAP_RECLAIM and ORB_BREAKOUT have no meaning on multi-day holds)
        if signals and signals[0].timeframe == "1day":
            signals = [s for s in signals if s.signal_type.value not in _INTRADAY_ONLY]

        if not signals:
            log.info("signal.none_after_filter", symbol=symbol, regime=regime)
            return

        top = signals[0]   # Highest confidence signal

        # ── Daily signal deduplication ────────────────────────────────────────
        # 1day signals fire on every 15min candle close but the daily bar hasn't
        # changed — it's identical data re-evaluated 26× per day. Once we've run
        # the pipeline for a given (symbol, signal_type) on the daily TF, mark it
        # as evaluated for today. Subsequent candle closes skip it.
        # 15min signals are NOT deduplicated — each candle is genuinely new data.
        if top.timeframe == "1day":
            dedup_key = f"signal:daily_evaluated:{symbol}:{top.signal_type.value}"
            if await redis.get(dedup_key):
                log.debug(
                    "signal.daily_dedup_skip",
                    symbol=symbol,
                    signal=top.signal_type.value,
                )
                return
            # Mark as evaluated — TTL = seconds remaining until 3:30 PM IST
            from zoneinfo import ZoneInfo as _ZI
            from datetime import time as _time
            _now_ist = datetime.now(_ZI("Asia/Kolkata"))
            _close   = _now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
            _ttl     = max(60, int((_close - _now_ist).total_seconds()))
            await redis.setex(dedup_key, _ttl, "1")

        log.info(
            "signal.detected",
            symbol    = symbol,
            signal    = top.signal_type.value,
            direction = top.direction.value,
            confidence= top.confidence,
            timeframe = top.timeframe,
            regime    = regime,
        )

        # Publish to Redis for dashboard + AI multi-TF context
        signal_payload = json.dumps(top.to_dict())
        await redis.setex(f"signal:latest:{symbol}",                 900, signal_payload)
        await redis.setex(f"signal:latest:{symbol}:{top.timeframe}", 900, signal_payload)
        for sig in signals:
            await redis.setex(
                f"signal:latest:{symbol}:{sig.timeframe}",
                900,
                json.dumps(sig.to_dict()),
            )

        # ── Confluence gate ───────────────────────────────────────────────────
        # Port of _score_confluence from backtesting engine (5 factors, max 10).
        # MIN_CONFLUENCE_SCORE = 8: score_8 → 50% WR; score_9 over-filters.
        _MIN_CONFLUENCE = 8
        _HQ_SIGNALS = {
            "BREAKOUT_HIGH", "BREAKOUT_LOW",
            "DOUBLE_BOTTOM",  "DOUBLE_TOP",
            "DARVAS_BREAKOUT",
            "ENGULFING_BULL", "ENGULFING_BEAR",
            "EVENING_STAR",   "MORNING_STAR",
            "BULL_FLAG",      "BEAR_FLAG",
            "EMA_CROSSOVER_UP", "EMA_CROSSOVER_DOWN",
        }
        _ind  = top.indicators if hasattr(top, "indicators") and top.indicators else {}
        _bull = top.direction.value == "BULLISH"

        # Factor 1: signal strength (confidence + pattern quality, cap 2)
        _conf = top.confidence
        _hq   = top.signal_type.value in _HQ_SIGNALS
        if _conf >= 80 and _hq:
            _f_signal = 2
        elif _conf >= 80 or (_conf >= 65 and _hq):
            _f_signal = min(2, 1 + (1 if _hq else 0))
        elif _conf >= 65:
            _f_signal = 1
        else:
            _f_signal = 0
        _f_signal = min(2, _f_signal)

        # Factor 2: volume (RVOL)
        _rvol = float(_ind.get("rvol") or 1.0)
        if _rvol >= 2.5:
            _f_vol = 2
        elif _rvol >= 1.5:
            _f_vol = 1
        else:
            _f_vol = 0

        # Factor 3: trend alignment (EMA stack + above 200 EMA)
        _above_200 = bool(_ind.get("above_200ema", False))
        _ema_stack = int(_ind.get("ema_stack") or 0)
        if _bull:
            _trend_ok = _above_200
            _stack_ok = _ema_stack >= 0
        else:
            _trend_ok = not _above_200
            _stack_ok = _ema_stack <= 0
        if _trend_ok and _stack_ok:
            _f_trend = 2
        elif _trend_ok or _stack_ok:
            _f_trend = 1
        else:
            _f_trend = 0

        # Factor 4: momentum (RSI sweet spot)
        _rsi = float(_ind.get("rsi_14") or _ind.get("rsi") or 50.0)
        if _bull:
            if 45.0 <= _rsi <= 70.0:
                _f_mom = 2
            elif 35.0 <= _rsi <= 80.0:
                _f_mom = 1
            else:
                _f_mom = 0
        else:
            if 30.0 <= _rsi <= 55.0:
                _f_mom = 2
            elif 20.0 <= _rsi <= 65.0:
                _f_mom = 1
            else:
                _f_mom = 0

        # Factor 5: multi-signal agreement (distinct signal types in full list)
        _distinct = len({s.signal_type for s in signals})
        if _distinct >= 3:
            _f_multi = 2
        elif _distinct >= 2:
            _f_multi = 1
        else:
            _f_multi = 0

        _confluence_total = _f_signal + _f_vol + _f_trend + _f_mom + _f_multi
        _confluence_breakdown = {
            "signal_strength": _f_signal,
            "volume":          _f_vol,
            "trend_alignment": _f_trend,
            "momentum":        _f_mom,
            "multi_signal":    _f_multi,
            "total":           _confluence_total,
        }

        if _confluence_total < _MIN_CONFLUENCE:
            log.info(
                "signal.confluence_failed",
                symbol     = symbol,
                signal     = top.signal_type.value,
                score      = _confluence_total,
                min_score  = _MIN_CONFLUENCE,
                breakdown  = _confluence_breakdown,
                regime     = regime,
            )
            return

        log.info(
            "signal.confluence_passed",
            symbol    = symbol,
            signal    = top.signal_type.value,
            score     = _confluence_total,
            breakdown = _confluence_breakdown,
        )

        # Execute if above confidence threshold → RiskEngine → Claude → broker
        confidence_threshold = cfg.get("confidence_threshold", 75)
        if top.confidence >= confidence_threshold:
            executor = TradeExecutor()
            await executor.execute(top)
        else:
            log.info(
                "signal.below_threshold",
                symbol=symbol,
                signal=top.signal_type.value,
                confidence=top.confidence,
                threshold=confidence_threshold,
                regime=regime,
            )

    except Exception as e:
        log.error("signal.run_error", symbol=symbol, error=str(e))
    finally:
        if sem is not None:
            sem.release()


# ─── Scheduled Jobs ───────────────────────────────────────────────────────────

async def job_daily_auth(retry_count: int = 0) -> None:
    """
    8:30 AM IST — Re-authenticate Zerodha and refresh tokens.
    On failure: retries up to 2 more times, 15 minutes apart.
    Sends Telegram alert after all retries exhausted.
    """
    from config.market_hours import is_trading_day
    if not is_trading_day():
        log.info("scheduler.auth_skip", reason="NSE holiday or weekend")
        return
    if not settings.kite_api_key:
        log.info("scheduler.auth_skip", reason="No KITE_API_KEY configured yet")
        return
    try:
        from services.execution.zerodha.authenticator import ZerodhaAuthenticator
        auth = ZerodhaAuthenticator()
        await auth.authenticate()
        if retry_count > 0:
            log.info("scheduler.auth_retry_succeeded", attempt=retry_count + 1)
    except Exception as e:
        log.error("scheduler.auth_failed", attempt=retry_count + 1, error=str(e))
        MAX_RETRIES = 2
        if retry_count < MAX_RETRIES:
            retry_at = datetime.now() + timedelta(minutes=15)
            log.warning(
                "scheduler.auth_retry_scheduled",
                retry_in_mins=15,
                attempt=retry_count + 2,
            )
            _scheduler.add_job(
                job_daily_auth,
                trigger    = DateTrigger(run_date=retry_at, timezone="Asia/Kolkata"),
                kwargs     = {"retry_count": retry_count + 1},
                id         = f"auth_retry_{retry_count + 1}",
                replace_existing = True,
            )
        else:
            notifier = get_notifier()
            await notifier.system_error(
                "DailyAuth",
                f"Authentication failed after {MAX_RETRIES + 1} attempts. "
                f"Manual login required. Last error: {e}",
            )
            log.error("scheduler.auth_exhausted", attempts=MAX_RETRIES + 1)


async def job_market_open_briefing() -> None:
    """9:10 AM IST — Claude researches market conditions and sends an informed briefing."""
    from config.market_hours import is_trading_day
    if not is_trading_day():
        log.info("scheduler.briefing_skip", reason="NSE holiday or weekend")
        return

    import json as _json
    from services.ai_strategy.claude_client import get_claude_client

    notifier = get_notifier()
    redis    = get_redis()

    # ── Regime ────────────────────────────────────────────────────────────────
    regime = await redis.get("market:regime") or "UNKNOWN"

    # ── India VIX ─────────────────────────────────────────────────────────────
    vix = None
    vix_raw = await redis.get("market:tick:INDIA VIX")
    if vix_raw:
        vix = _json.loads(vix_raw).get("lp")

    # ── Nifty 50 pre-open change % ────────────────────────────────────────────
    nifty_change_pct = 0.0
    nifty_raw = await redis.get("market:tick:NIFTY 50")
    if nifty_raw:
        nifty_data = _json.loads(nifty_raw)
        lp = nifty_data.get("lp", 0)
        c  = nifty_data.get("c", lp)   # previous close
        if c and c != 0:
            nifty_change_pct = (lp - c) / c * 100

    # ── Recent news headlines + GIFT Nifty (run in parallel) ─────────────────
    from services.data_ingestion.gift_nifty import (
        fetch_gift_nifty_change,
        fetch_market_news_sentiment,
    )

    headlines: list[str] = []
    gift_pct:   float | None = None
    news_score: float | None = None

    try:
        news_service = get_news_service()
        headline_tasks = [news_service.get_recent_news(sym, hours=12)
                          for sym in ["NIFTY", "RELIANCE", "HDFCBANK", "TCS"]]
        results = await asyncio.gather(*headline_tasks, return_exceptions=True)
        for articles in results:
            if isinstance(articles, list):
                for a in articles[:3]:
                    h = a.get("headline", "").strip()
                    if h and h not in headlines:
                        headlines.append(h)
        headlines = headlines[:8]
    except Exception as e:
        log.warning("scheduler.briefing_news_error", error=str(e))

    gift_pct, news_score = await asyncio.gather(
        fetch_gift_nifty_change(),
        fetch_market_news_sentiment(hours=12),
        return_exceptions=True,
    )
    gift_pct   = gift_pct   if isinstance(gift_pct,   float) else None
    news_score = news_score if isinstance(news_score, float) else None

    # ── Re-publish regime with fresh GIFT Nifty + news data ──────────────────
    from sqlalchemy import text as _sql_text
    try:
        async with get_db_session() as session:
            result = await session.execute(
                _sql_text("""
                    SELECT ts, open, high, low, close, volume
                    FROM ohlcv
                    WHERE trading_symbol = 'NIFTY 50' AND timeframe = '1day'
                    ORDER BY ts DESC LIMIT 200
                """)
            )
            rows = result.fetchall()
        if len(rows) >= 50:
            import pandas as pd
            df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
            df = df.sort_values("ts").set_index("ts")
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            regime = await get_regime_detector().detect_and_publish(
                df,
                india_vix=vix,
                gift_nifty_pct=gift_pct,
                news_sentiment=news_score,
            )
    except Exception as e:
        log.warning("scheduler.briefing_regime_refresh_error", error=str(e))

    # ── Ask Claude for the briefing ───────────────────────────────────────────
    briefing, macro_shock = await get_claude_client().get_market_briefing(
        nifty_change_pct = nifty_change_pct,
        vix              = vix,
        regime           = regime,
        news_headlines   = headlines,
    )

    # If Claude detects a macro shock, override regime to HIGH_VOLATILITY
    if macro_shock:
        redis2 = get_redis()
        await redis2.setex("market:regime", 86_400, "HIGH_VOLATILITY")
        await redis2.setex("market:regime:structural", 86_400, "HIGH_VOLATILITY")
        await redis2.setex("market:news_alert", 86_400, "HIGH_VOLATILITY")
        log.warning("regime.macro_shock_override", source="morning_briefing")

    log.info(
        "scheduler.briefing_done",
        regime           = regime,
        macro_shock      = macro_shock,
        vix              = vix,
        nifty_change_pct = round(nifty_change_pct, 2),
        gift_nifty_pct   = gift_pct,
        news_sentiment   = news_score,
        headlines        = len(headlines),
    )

    await notifier.market_open(
        regime   = regime,
        vix      = vix,
        briefing = briefing,
    )


async def job_orb_scan() -> None:
    """
    10:00 AM IST — Scan all symbols for ORB breakouts.

    The 9:45 candle (9:45–10:00 IST) closes at exactly 10:00. This job runs
    immediately after, reads today's 15-min candles from _candle_buffer, applies
    the ORB rules (Nifty gate + OR high breakout + volume surge), and routes
    qualifying setups directly through TradeExecutor — bypassing the daily-signal
    dedup and momentum confluence gate (ORB has its own entry criteria).
    """
    from config.market_hours import is_trading_day
    if not is_trading_day():
        return

    from datetime import date as _date
    from services.orb_engine.live import scan_orb_signals
    from services.data_ingestion.nifty500_instruments import get_live_universe
    from services.execution.trade_executor import TradeExecutor

    symbols = get_live_universe()
    today   = _date.today()

    log.info("orb_scan.start", symbols=len(symbols))
    signals = scan_orb_signals(_candle_buffer, symbols, today)

    if not signals:
        log.info("orb_scan.no_setups")
        return

    log.info("orb_scan.firing", count=len(signals), symbols=[s.trading_symbol for s in signals])

    executor = TradeExecutor()
    for sig in signals:
        try:
            trade = await executor.execute(sig)
            if trade:
                log.info("orb_scan.trade_opened",
                         symbol=sig.trading_symbol, entry=sig.price_at_signal,
                         stop=sig.indicators.get("stop_price"))
        except Exception as e:
            log.warning("orb_scan.execute_error", symbol=sig.trading_symbol, error=str(e))


async def job_square_off_intraday() -> None:
    """3:12 PM IST — Square off all intraday positions and close them in DB."""
    from config.market_hours import is_trading_day
    if not is_trading_day():
        return
    log.warning("scheduler.square_off_intraday", time="15:12")
    if settings.uses_real_broker:
        from services.execution.broker_router import get_broker
        await get_broker().square_off_all_intraday()
    # Close all remaining OPEN trades in DB at current market price
    closed = await get_lifecycle_manager().close_all_open_trades(reason="TIME_EXIT")
    log.info("scheduler.square_off_db_closed", count=closed)


async def job_eod_summary() -> None:
    """4:30 PM IST — Send daily P&L summary via Telegram."""
    try:
        from sqlalchemy import func, select, text
        from database.connection import get_db_session
        from database.models import Trade

        today = datetime.now().date()
        async with get_db_session() as session:
            result = await session.execute(
                text("""
                    SELECT
                        COUNT(*) as total,
                        SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
                        SUM(CASE WHEN net_pnl < 0 THEN 1 ELSE 0 END) as losses,
                        COALESCE(SUM(net_pnl), 0) as net_pnl,
                        COALESCE(SUM(brokerage + stt + exchange_charges + gst + sebi_charges + stamp_duty), 0) as charges
                    FROM trades
                    WHERE DATE(entry_time) = :today AND status = 'CLOSED'
                """),
                {"today": today},
            )
            row = result.fetchone()
            if row:
                redis  = get_redis()
                regime = await redis.get("market:regime") or "UNKNOWN"
                notifier = get_notifier()
                await notifier.daily_summary(
                    trading_date  = today.strftime("%d %b %Y"),
                    total_trades  = row.total or 0,
                    winning       = row.wins or 0,
                    losing        = row.losses or 0,
                    net_pnl       = float(row.net_pnl or 0),
                    total_charges = float(row.charges or 0),
                    market_regime = regime,
                )
    except Exception as e:
        log.error("scheduler.eod_summary_error", error=str(e))


# ─── Startup & Shutdown ───────────────────────────────────────────────────────

def _print_banner() -> None:
    env_colours = {
        "development": "yellow",
        "paper":       "cyan",
        "semi-auto":   "magenta",
        "live":        "red",
    }
    colour = env_colours.get(settings.app_env.value, "white")
    mode_text = Text(settings.app_env.value.upper(), style=f"bold {colour}")

    panel = Panel(
        f"[bold white]Trading Bot[/bold white]  |  Mode: {mode_text}\n"
        f"Capital: ₹{settings.total_capital:,.0f}  |  "
        f"Max risk/trade: ₹{settings.max_risk_per_trade_inr:,.0f}  |  "
        f"Daily limit: ₹{settings.daily_loss_limit_inr:,.0f}",
        title="[bold green]Starting Up[/bold green]",
        border_style="green",
    )
    console.print(panel)


async def job_db_backup() -> None:
    """4:45 PM IST — pg_dump the trading DB to a timestamped file."""
    import os
    import subprocess
    from config.market_hours import is_trading_day
    if not is_trading_day():
        return

    backup_dir = os.environ.get("DB_BACKUP_DIR", "backups")
    os.makedirs(backup_dir, exist_ok=True)

    timestamp   = datetime.now().strftime("%Y%m%d_%H%M")
    backup_file = os.path.join(backup_dir, f"trading_bot_{timestamp}.sql.gz")

    # Parse DB URL for pg_dump env vars
    db_url = settings.database_url.replace("postgresql+asyncpg://", "")
    # Format: user:password@host:port/dbname
    try:
        userpass, rest   = db_url.split("@", 1)
        user, password   = userpass.split(":", 1)
        hostport, dbname = rest.split("/", 1)
        host, port       = (hostport.split(":", 1) + ["5432"])[:2]
    except ValueError:
        log.error("scheduler.backup_parse_error", db_url=db_url[:30])
        return

    env = {**os.environ, "PGPASSWORD": password}
    cmd = [
        "pg_dump",
        "-h", host, "-p", port,
        "-U", user,
        "-d", dbname,
        "--no-password",
        "-F", "c",   # custom compressed format
        "-f", backup_file,
    ]

    try:
        result = subprocess.run(cmd, env=env, capture_output=True, timeout=120)
        if result.returncode == 0:
            size_kb = os.path.getsize(backup_file) // 1024
            log.info("scheduler.backup_done", file=backup_file, size_kb=size_kb)
        else:
            err = result.stderr.decode()[:200]
            log.error("scheduler.backup_failed", error=err)
            await get_notifier().system_error("DBBackup", err)
    except FileNotFoundError:
        log.warning("scheduler.backup_skip", reason="pg_dump not found — install postgresql-client")
    except subprocess.TimeoutExpired:
        log.error("scheduler.backup_timeout")
        await get_notifier().system_error("DBBackup", "pg_dump timed out after 120s")
    except Exception as e:
        log.error("scheduler.backup_error", error=str(e))


async def _preseed_candle_buffer() -> None:
    """
    Pre-load _candle_buffer from TimescaleDB so signals can fire from the
    first live candle close instead of waiting 7.5 hours for 30 bars to accumulate.

    Loads last 50 daily candles per symbol (enough for EMA-50, ATR, ADX).
    15min bars are now seeded from yfinance (5d / 15m interval, ~78 bars across
    ~3 trading days) so ORB and intraday signals are available from the first tick.
    5min/1min bars are not pre-seeded and build up live.
    """
    import asyncio
    import pandas as pd
    from sqlalchemy import text as _text
    from services.data_ingestion.nifty500_instruments import get_live_universe

    symbols = get_live_universe() + ["NIFTY 50"]
    loaded = 0

    try:
        async with get_db_session() as session:
            for symbol in symbols:
                result = await session.execute(
                    _text("""
                        SELECT ts, open, high, low, close, volume
                        FROM ohlcv
                        WHERE trading_symbol = :sym AND timeframe = '1day'
                        ORDER BY ts DESC
                        LIMIT 50
                    """),
                    {"sym": symbol},
                )
                rows = result.fetchall()
                if not rows:
                    continue

                if symbol not in _candle_buffer:
                    _candle_buffer[symbol] = {}
                if "1day" not in _candle_buffer[symbol]:
                    _candle_buffer[symbol]["1day"] = deque(maxlen=BUFFER_MAX)

                # Insert oldest-first into the deque
                for row in reversed(rows):
                    _candle_buffer[symbol]["1day"].append({
                        "open":   float(row.open   or 0),
                        "high":   float(row.high   or 0),
                        "low":    float(row.low    or 0),
                        "close":  float(row.close  or 0),
                        "volume": int(row.volume   or 0),
                        "ts":     row.ts,
                    })
                loaded += 1

        log.info("startup.candle_buffer_preseeded", symbols=loaded, timeframe="1day", bars_per_symbol=len(rows))
    except Exception as e:
        log.warning("startup.candle_buffer_preseed_error", error=str(e))

    # ── 15min preseed from yfinance (background) ─────────────────────────────
    # Runs as a background task — 2175 symbols × 0.2s sleep = 7+ min if awaited.
    # Signals and Telegram are fully functional before this completes.
    async def _preseed_15min_bg():
      try:
        import yfinance as yf

        loop = asyncio.get_event_loop()
        seeded_15min = 0
        bars_per_symbol_15min = 0

        for symbol in symbols:
            try:
                # Map symbol to yfinance ticker format
                if symbol == "NIFTY 50":
                    yf_ticker = "^NSEI"
                else:
                    yf_ticker = f"{symbol}.NS"

                def _fetch(ticker_name: str):
                    t = yf.Ticker(ticker_name)
                    return t.history(period="5d", interval="15m", auto_adjust=True)

                df = await loop.run_in_executor(None, _fetch, yf_ticker)

                if df is None or df.empty:
                    continue

                # Normalise columns to lowercase, keep only OHLCV
                df.columns = [c.lower() for c in df.columns]
                df = df[["open", "high", "low", "close", "volume"]].dropna()

                if df.empty:
                    continue

                if symbol not in _candle_buffer:
                    _candle_buffer[symbol] = {}
                if "15min" not in _candle_buffer[symbol]:
                    _candle_buffer[symbol]["15min"] = deque(maxlen=BUFFER_MAX)

                for ts, row in df.iterrows():
                    _candle_buffer[symbol]["15min"].append({
                        "open":   float(row["open"]),
                        "high":   float(row["high"]),
                        "low":    float(row["low"]),
                        "close":  float(row["close"]),
                        "volume": int(row["volume"]),
                        "ts":     ts,
                    })

                bars_per_symbol_15min = len(df)
                seeded_15min += 1

                # Avoid hammering yfinance rate limits
                await asyncio.sleep(0.2)

            except Exception as sym_err:
                log.warning("startup.candle_buffer_15min_symbol_error", symbol=symbol, error=str(sym_err))

        log.info(
            "startup.candle_buffer_15min_preseeded",
            symbols=seeded_15min,
            bars_per_symbol=bars_per_symbol_15min,
        )
      except Exception as e:
        log.warning("startup.candle_buffer_15min_preseed_error", error=str(e))

    asyncio.create_task(_preseed_15min_bg())


async def job_status_heartbeat() -> None:
    """
    Prints a status line every 5 minutes during market hours.
    Shows regime, candle buffer depth, open positions, and last signal seen.
    Only active in paper/dev mode — disabled in live to reduce noise.
    """
    if settings.app_env.value == "live":
        return
    try:
        redis  = get_redis()
        regime = await redis.get("market:regime") or "UNKNOWN"

        # Count symbols with candle data
        buffered = sum(1 for sym in _candle_buffer if _candle_buffer[sym])

        # Count open positions from DB
        from sqlalchemy import text as _text
        open_positions = 0
        try:
            async with get_db_session() as session:
                result = await session.execute(_text("SELECT COUNT(*) FROM trades WHERE status = 'OPEN'"))
                open_positions = int(result.scalar() or 0)
        except Exception:
            pass

        sample = {sym: {tf: len(buf) for tf, buf in tfs.items()}
                  for sym, tfs in list(_candle_buffer.items())[:3]}
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] HEARTBEAT | "
            f"regime={regime} | symbols_buffered={buffered} | open_positions={open_positions} | "
            f"buffer_sample={sample}",
            flush=True,
        )
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] heartbeat.error: {e}", flush=True)


async def job_session_regime(lock_after: bool = False) -> None:
    """
    Evaluate session regime from live Nifty 50 intraday data.
    Called at 9:45 AM (lock_after=False) and 10:15 AM (lock_after=True).

    Merges with structural regime and re-publishes market:regime to Redis.
    After 10:15, writes market:regime:locked so no further intraday updates occur.
    """
    from config.market_hours import is_trading_day
    if not is_trading_day():
        return

    # Skip if already locked (shouldn't happen but guard anyway)
    redis = get_redis()
    if await redis.get("market:regime:locked"):
        return

    try:
        from services.market_regime.session import fetch_nifty_intraday, evaluate_session_regime, merge_regimes

        df = await fetch_nifty_intraday()
        session = evaluate_session_regime(df)

        structural = await redis.get("market:regime:structural") or "UNKNOWN"
        merged     = merge_regimes(structural, session)

        await get_regime_detector().publish(merged, detail={
            "structural": structural,
            "session":    session,
            "locked":     lock_after,
        })

        if lock_after:
            await redis.setex("market:regime:locked", 86_400, "1")
            log.info("regime.locked_for_day", regime=merged, structural=structural, session=session)
        else:
            log.info("regime.session_updated", regime=merged, structural=structural, session=session)

        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] SESSION REGIME | "
            f"structural={structural} session={session} merged={merged} locked={lock_after}",
            flush=True,
        )
    except Exception as e:
        log.error("regime.session_job_error", error=str(e))


async def _bootstrap_regime() -> None:
    """
    Compute and publish market regime from historical daily candles at startup.
    Prevents UNKNOWN regime persisting all day in paper/live mode.
    Reads last 200 daily candles for NIFTY 50 from TimescaleDB.
    """
    import json as _json
    import pandas as pd
    from sqlalchemy import text as _text

    try:
        async with get_db_session() as session:
            result = await session.execute(
                _text("""
                    SELECT ts, open, high, low, close, volume
                    FROM ohlcv
                    WHERE trading_symbol = 'NIFTY 50'
                      AND timeframe = '1day'
                    ORDER BY ts DESC
                    LIMIT 200
                """)
            )
            rows = result.fetchall()

        if len(rows) < 50:
            log.warning("startup.regime_bootstrap_skip", rows=len(rows), reason="Insufficient historical data")
            return

        df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
        df = df.sort_values("ts").set_index("ts")
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        from services.data_ingestion.gift_nifty import (
            fetch_gift_nifty_change,
            fetch_market_news_sentiment,
        )

        from services.data_ingestion.gift_nifty import fetch_india_vix as _fetch_vix
        redis = get_redis()
        vix_raw   = await redis.get("market:tick:INDIA VIX")
        india_vix = _json.loads(vix_raw).get("lp") if vix_raw else None

        # Fetch live India VIX if not in Redis
        if india_vix is None:
            india_vix = await _fetch_vix()

        gift_pct, news_score = await asyncio.gather(
            fetch_gift_nifty_change(),
            fetch_market_news_sentiment(hours=12),
            return_exceptions=True,
        )
        gift_pct   = gift_pct   if isinstance(gift_pct,   float) else None
        news_score = news_score if isinstance(news_score, float) else None

        regime = await get_regime_detector().detect_and_publish(
            df,
            india_vix=india_vix,
            gift_nifty_pct=gift_pct,
            news_sentiment=news_score,
        )
        # Store structural regime separately so session layer can reference it
        await redis.setex("market:regime:structural", 86_400, regime)
        # Clear any stale lock from previous day
        await redis.delete("market:regime:locked")

        log.info(
            "startup.regime_bootstrapped",
            regime=regime,
            candles=len(df),
            gift_nifty_pct=gift_pct,
            news_sentiment=news_score,
            india_vix=india_vix,
        )

    except Exception as e:
        log.warning("startup.regime_bootstrap_error", error=str(e))


async def _ensure_index_seeded(seeder: "HistoricalSeeder") -> None:
    """Seed NIFTY 50 index data if not already in ohlcv. Runs on every startup."""
    from sqlalchemy import text as _text
    try:
        async with get_db_session() as session:
            result = await session.execute(
                _text("SELECT COUNT(*) FROM ohlcv WHERE trading_symbol = 'NIFTY 50' AND timeframe = '1day'")
            )
            count = int(result.scalar() or 0)

        if count < 50:
            log.info("startup.seeding_nifty50_index", existing_rows=count)
            from datetime import date, timedelta
            start_date = date.today() - timedelta(days=730)
            df = seeder._fetch_yfinance_raw("^NSEI", start_date, "1day")
            if df is not None and not df.empty:
                await seeder._upsert_candles("NIFTY 50", "1day", df)
                log.info("startup.nifty50_index_seeded", rows=len(df))
            else:
                log.warning("startup.nifty50_index_no_data")
        else:
            log.info("startup.nifty50_index_ok", rows=count)
    except Exception as e:
        log.warning("startup.nifty50_index_seed_error", error=str(e))


async def startup() -> None:
    """Initialise all connections and seed data on first run."""
    _print_banner()

    # 1. Database
    log.info("startup.db_init")
    await init_db()

    # 2. Redis health check
    redis = get_redis()
    await redis.ping()
    log.info("startup.redis_ok")

    # 3. Telegram polling — start EARLY so commands work immediately.
    #    The 15-min preseed below takes 7+ minutes; starting Telegram last
    #    meant commands were unavailable for the entire startup window.
    if settings.telegram_bot_token:
        if settings.is_semi_auto and not settings.authorized_telegram_ids:
            log.warning(
                "startup.semi_auto_no_auth",
                reason="TELEGRAM_AUTHORIZED_IDS is empty — any Telegram user can approve trades!",
            )
        from services.notifications.telegram_bot import start_telegram_polling
        app_tg = await start_telegram_polling()
        import main as _self
        _self._telegram_app = app_tg

    # 4. Seed historical data (skips if already seeded today)
    last_seed = await redis.get("meta:last_seed_date")
    today_str = datetime.now().strftime("%Y-%m-%d")
    seeder = HistoricalSeeder(use_kite=bool(settings.kite_api_key))
    if last_seed != today_str:
        log.info("startup.seeding_historical_data")
        await seeder.create_hypertable()
        await seeder.seed_all(timeframes=["1day"])
        await redis.setex("meta:last_seed_date", 86_400 * 2, today_str)
    else:
        log.info("startup.seed_skip", reason="Already seeded today")
        await _ensure_index_seeded(seeder)

    # 5. Pre-seed candle buffer from DB (fast) then 15-min from yfinance (slow —
    #    runs as background task so startup completes quickly)
    await _preseed_candle_buffer()

    # 6. Bootstrap market regime from historical data so it's never UNKNOWN at open
    await _bootstrap_regime()

    # 7. News feed (background polling — no-op if NEWS_API_KEY not set)
    news_service = get_news_service()
    await news_service.start()

    # 8. Trade lifecycle manager (monitors open trades, closes on SL/target hit)
    asyncio.create_task(get_lifecycle_manager().run())

    log.info("startup.complete", env=settings.app_env.value)


async def shutdown(scheduler: AsyncIOScheduler) -> None:
    """Graceful shutdown."""
    log.info("shutdown.start")
    scheduler.shutdown(wait=False)
    get_lifecycle_manager().stop()
    await get_news_service().stop()
    # Stop Telegram polling if running
    if settings.telegram_bot_token:
        import main as _self
        tg_app = getattr(_self, "_telegram_app", None)
        if tg_app:
            from services.notifications.telegram_bot import stop_telegram_polling
            await stop_telegram_polling(tg_app)
    await close_db()
    await close_redis()
    log.info("shutdown.complete")


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    await startup()

    # Semaphore must be created inside the running event loop
    global _signal_semaphore
    _signal_semaphore = asyncio.Semaphore(75)  # max 75 concurrent signal scans

    # ── Feed ─────────────────────────────────────────────────────────────────
    feed = FeedManager()
    feed.add_candle_listener(on_candle_complete)
    await feed.start()

    # ── Scheduler ────────────────────────────────────────────────────────────
    # Module-level reference so job_daily_auth can schedule its own retries
    global _scheduler
    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
    _scheduler = scheduler

    # Weekdays only (Mon=0 … Fri=4)
    scheduler.add_job(job_daily_auth,          CronTrigger(day_of_week="0-4", hour=8,  minute=30, timezone="Asia/Kolkata"))
    scheduler.add_job(job_market_open_briefing, CronTrigger(day_of_week="0-4", hour=9, minute=10, timezone="Asia/Kolkata"))
    scheduler.add_job(job_orb_scan,             CronTrigger(day_of_week="0-4", hour=10, minute=0, timezone="Asia/Kolkata"))
    scheduler.add_job(job_square_off_intraday,  CronTrigger(day_of_week="0-4", hour=15, minute=12, timezone="Asia/Kolkata"))
    scheduler.add_job(job_eod_summary,          CronTrigger(day_of_week="0-4", hour=16, minute=30, timezone="Asia/Kolkata"))
    scheduler.add_job(job_db_backup,            CronTrigger(day_of_week="0-4", hour=16, minute=45, timezone="Asia/Kolkata"))
    # Paper/dev only: status heartbeat every 5 minutes
    scheduler.add_job(job_status_heartbeat,     CronTrigger(minute="*/5"))
    # Session regime evaluation at 9:45 and 10:15 AM
    scheduler.add_job(
        job_session_regime,
        CronTrigger(day_of_week="0-4", hour=9, minute=45, timezone="Asia/Kolkata"),
        kwargs={"lock_after": False},
    )
    scheduler.add_job(
        job_session_regime,
        CronTrigger(day_of_week="0-4", hour=10, minute=15, timezone="Asia/Kolkata"),
        kwargs={"lock_after": True},
    )
    scheduler.start()

    log.info("main.running", feed=feed._feed.__class__.__name__)

    # Fire heartbeat immediately so we see regime + buffer state right after startup
    asyncio.create_task(job_status_heartbeat())

    # ── Graceful shutdown on SIGINT/SIGTERM ───────────────────────────────────
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler():
        log.info("main.shutdown_signal")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await stop_event.wait()
    await feed.stop()
    await shutdown(scheduler)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
        sys.exit(0)
