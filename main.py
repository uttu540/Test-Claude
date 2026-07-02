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
import logging.handlers
from pathlib import Path

# Ensure logs directory exists
Path("logs").mkdir(exist_ok=True)

_log_file = Path("logs") / f"bot_{datetime.now().strftime('%Y-%m-%d')}.log"

# File handler — plain text, no ANSI colours, rotates at 20 MB, keeps 7 days
_file_handler = logging.handlers.RotatingFileHandler(
    _log_file, maxBytes=20 * 1024 * 1024, backupCount=7, encoding="utf-8"
)
_file_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

# Console handler — coloured structlog output (existing behaviour)
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(logging.Formatter("%(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[_console_handler, _file_handler])
# Ensure APScheduler job errors propagate to our handlers (not silently dropped)
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)

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
_active_v2_tasks: set[str] = set()       # Symbols with an in-flight intraday V2 task
_signal_semaphore: asyncio.Semaphore | None = None  # Initialised in main() after event loop starts
_v2_semaphore: asyncio.Semaphore | None = None      # Throttle for intraday V2 5-min scans
BUFFER_MAX = 300   # Default; overridden per-timeframe by BUFFER_MAX_BY_TF
# 1min: 375 candles/session — 300 would silently drop the first ~75 bars of the day.
# Use 500 to keep today + partial yesterday without unbounded memory growth.
BUFFER_MAX_BY_TF: dict[str, int] = {
    "1min":  500,
    "5min":  300,
    "15min": 300,
    "1hr":   300,
    "1day":  300,
}
_scheduler: AsyncIOScheduler | None = None   # Set in main(); used by retry jobs
_tick_count: int = 0                          # Rolling tick counter for diagnostics
_feed_manager: "FeedManager | None" = None   # Set in main(); used by EOD flush
_orb_scan_lock: asyncio.Lock | None = None   # prevents concurrent ORB scan executions


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
        _candle_buffer[sym][tf] = deque(maxlen=BUFFER_MAX_BY_TF.get(tf, BUFFER_MAX))

    _candle_buffer[sym][tf].append({
        "open":   candle.open,
        "high":   candle.high,
        "low":    candle.low,
        "close":  candle.close,
        "volume": candle.volume,
        "ts":     candle.timestamp,
    })

    buf_len = len(_candle_buffer[sym][tf])
    # Only print/log 15min candles — 1min would flood the terminal (50 symbols × 1/min)
    if tf == "15min":
        print(f"[{datetime.now().strftime('%H:%M:%S')}] candle.closed_15min  {sym}  bars={buf_len}  close={candle.close:.2f}", flush=True)
        log.info("candle.closed_15min", symbol=sym, bars=buf_len, close=candle.close, tf=tf)

    # Run signal detection on the 15min candle close
    # (avoids running on every 1min candle — too noisy)
    # Guard: skip if a signal task is already running for this symbol
    if tf == "15min" and sym not in _active_signal_tasks:
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(_run_signals(sym))
            _active_signal_tasks.add(sym)
            task.add_done_callback(lambda _: _active_signal_tasks.discard(sym))
        except RuntimeError as e:
            log.error("candle.create_task_failed", symbol=sym, error=str(e))

    # Intraday V2 engine runs on 5-min candle closes (two-sided box breakouts).
    # Routed directly to TradeExecutor — bypasses the long-only swing pipeline.
    # Disabled in swing-only mode (see settings.swing_only_mode).
    if tf == "5min" and not settings.swing_only_mode and sym not in _active_v2_tasks:
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(_run_v2_signal(sym))
            _active_v2_tasks.add(sym)
            task.add_done_callback(lambda _: _active_v2_tasks.discard(sym))
        except RuntimeError as e:
            log.error("candle.v2_task_failed", symbol=sym, error=str(e))


async def _run_v2_signal(symbol: str) -> None:
    """
    Intraday V2 detection on a 5-min candle close.

    Cheap pre-checks happen inside detect() (day context missing/C-grade,
    cooldown, caps) so most calls return in <1ms with one Redis read.
    Fires INTRADAY_V2_BOX signals straight to TradeExecutor (shorts allowed).
    """
    sem = _v2_semaphore
    if sem is not None:
        await sem.acquire()
    try:
        from config.market_hours import is_market_open
        if not is_market_open():
            return

        buf_5m = _candle_buffer.get(symbol, {}).get("5min")
        if not buf_5m or len(buf_5m) < 9:
            return
        daily_buf = _candle_buffer.get(symbol, {}).get("1day")

        from services.intraday_engine_v2.live import IntradayV2LiveEngine
        redis = get_redis()
        sig = await IntradayV2LiveEngine().detect(symbol, buf_5m, daily_buf, redis)
        if sig is None:
            return

        log.info(
            "v2_signal.firing",
            symbol=symbol, direction=sig.direction.value,
            entry=sig.entry_price, stop=sig.stop_loss, target=sig.target,
        )
        from services.execution.trade_executor import TradeExecutor
        trade = await TradeExecutor().execute(sig)
        if trade:
            log.info("v2_signal.trade_opened", symbol=symbol, direction=sig.direction.value)
    except Exception as e:
        log.warning("v2_signal.error", symbol=symbol, error=str(e))
    finally:
        if sem is not None:
            sem.release()


async def job_intraday_v2_context() -> None:
    """9:26 IST — compute universe breadth + day grade for the intraday V2 engine."""
    try:
        if settings.swing_only_mode:
            log.info("v2_context.skip", reason="swing_only_mode")
            return
        from config.market_hours import is_market_open
        if not is_market_open():
            log.info("v2_context.market_closed")
            return
        from services.intraday_engine_v2.live import compute_day_context
        redis = get_redis()
        ctx = await compute_day_context(_candle_buffer, redis)
        if ctx:
            await get_notifier()._send(
                f"📊 *Intraday V2 day context*\n"
                f"Grade: *{ctx['grade']}* ({ctx['direction']})\n"
                f"Breadth: {ctx['breadth']:.0%} | Market: {ctx['market_925']:+.2f}%\n"
                f"Universe: {ctx['universe']} stocks",
                parse_mode="Markdown",
            )
    except Exception as e:
        log.error("v2_context.error", error=str(e))


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
        log.info("signal.eval_start", symbol=symbol)
        from config.market_hours import is_market_open
        if not is_market_open():
            log.info("signal.market_closed", symbol=symbol)
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
        # V2 momentum engine: relaxed 200EMA gate (near_200) + ADX lookforward,
        # TRENDING_DOWN hard-blocked (backtest-aligned). Validated better than V1
        # on 3yr N500 + full-NSE backtests.
        from services.momentum_engine_v2.live import MomentumV2LiveEngine as MomentumLiveEngine

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
            log.info("signal.no_buffer", symbol=symbol,
                     buffer_tfs=list(_candle_buffer.get(symbol, {}).keys()),
                     buffer_lens={tf: len(b) for tf, b in _candle_buffer.get(symbol, {}).items()})
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
        _INTRADAY_ONLY = frozenset({"VWAP_RECLAIM", "ORB_BREAKOUT", "INTRADAY_IDARVAS"})

        # Momentum engine — regime/RS logic handled inside MomentumLiveEngine
        if "1day" in ohlcv_by_tf:
            momentum_engine = MomentumLiveEngine()
            signals = await momentum_engine.detect(
                symbol   = symbol,
                daily_df = ohlcv_by_tf["1day"],
                regime   = regime,
                redis    = redis,
            )

        # ── Earnings catalyst engine (runs in parallel with momentum) ─────────
        # Checks if this symbol reported results recently.
        # Earnings signals bypass regime gates — the catalyst IS the regime override.
        try:
            earnings_recent_raw = await redis.get("earnings:recent_symbols")
            earnings_recent: list[str] = json.loads(earnings_recent_raw) if earnings_recent_raw else []
            if symbol in earnings_recent:
                from services.earnings_engine.engine import EarningsSignalEngine
                earnings_sig = await EarningsSignalEngine().check_symbol(symbol, _candle_buffer, redis)
                if earnings_sig:
                    signals.append(earnings_sig)
        except Exception as _e:
            log.warning("earnings_engine.check_error", symbol=symbol, error=str(_e))

        # ── Catalyst gap PEAD engine (Day2 entry after gap day) ───────────────
        # Detects: yesterday gapped 7%+ with RVOL 8×+ and held the gap (close/high≥90%).
        # Today enters at open if gap still holding (today open ≥99% yesterday close).
        # Bypass regime gates — the volume-confirmed gap IS the catalyst.
        try:
            if "1day" in ohlcv_by_tf:
                from services.catalyst_engine.live import CatalystLiveEngine
                # Extract today's 9:15 candle open from buffer
                _today_open: float | None = None
                _buf_15m = _candle_buffer.get(symbol, {}).get("15min")
                if _buf_15m:
                    from datetime import timezone as _tz, timedelta as _td, date as _date
                    _IST = _tz(_td(hours=5, minutes=30))
                    _today = datetime.now(_IST).date()
                    for _c in _buf_15m:
                        _ts = _c["ts"]
                        _ts_ist = _ts.astimezone(_IST) if getattr(_ts, "tzinfo", None) else _ts.replace(tzinfo=_IST)
                        if _ts_ist.date() == _today and _ts_ist.hour == 9 and _ts_ist.minute == 15:
                            _today_open = float(_c["open"])
                            break
                cat_sig = await CatalystLiveEngine().detect(
                    symbol     = symbol,
                    daily_df   = ohlcv_by_tf["1day"],
                    today_open = _today_open,
                    redis      = redis,
                )
                if cat_sig:
                    signals.append(cat_sig)
        except Exception as _e:
            log.warning("catalyst_engine.check_error", symbol=symbol, error=str(_e))

        # ── IDARVAS intraday engine (Darvas box breakout on gap stocks) ──────
        # Gap ≥1.5% + RVOL ≥2× stock; session high consolidates ≥75 min;
        # breakout of box top with volume + VWAP confirmation.
        # Validated 2024: 32 trades, 68.75% WR, Sharpe 9.8, max DD ₹1,060.
        # Bypasses regime gate — gap+RVOL IS the catalyst context.
        # Disabled in swing-only mode (pure-intraday MIS engine).
        try:
            _buf_15m_idarvas = _candle_buffer.get(symbol, {}).get("15min")
            if not settings.swing_only_mode and _buf_15m_idarvas and "1day" in ohlcv_by_tf:
                from services.intraday_engine.live import IntradayLiveEngine as _ILE
                _prev_close_idarvas = float(ohlcv_by_tf["1day"]["close"].iloc[-2]) \
                    if len(ohlcv_by_tf["1day"]) >= 2 else 0.0
                _idarvas_sig = await _ILE().detect(
                    symbol    = symbol,
                    buf_15m   = _buf_15m_idarvas,
                    prev_close = _prev_close_idarvas,
                    daily_df  = ohlcv_by_tf["1day"],
                    redis     = redis,
                )
                if _idarvas_sig:
                    signals.append(_idarvas_sig)
        except Exception as _e:
            log.warning("idarvas_engine.check_error", symbol=symbol, error=str(_e))

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

        # ── Signal priority sort ──────────────────────────────────────────────
        # Priority order (explicit, not just insertion order):
        #   1. Momentum engine signals (DARVAS_BREAKOUT, BREAKOUT_52W, etc.) — primary swing engine
        #   2. Catalyst / earnings signals (CATALYST_GAP_PEAD, EARNINGS_BEAT)
        #   3. IDARVAS intraday
        #   4. Everything else
        # Within each tier: sort by confidence descending.
        _MOMENTUM_SIGNALS = {
            "DARVAS_BREAKOUT", "BREAKOUT_52W", "VOLUME_THRUST", "EMA_RIBBON", "BULL_MOMENTUM",
        }
        _CATALYST_SIGNALS = {"CATALYST_GAP_PEAD", "EARNINGS_BEAT", "EARNINGS_MISS"}

        def _signal_priority(s: Signal) -> tuple:
            t = s.signal_type.value
            tier = 3 if t in _MOMENTUM_SIGNALS else \
                   2 if t in _CATALYST_SIGNALS else \
                   1 if t == "INTRADAY_IDARVAS" else 0
            return (tier, s.confidence)

        signals.sort(key=_signal_priority, reverse=True)
        top = signals[0]   # Highest priority + confidence signal

        # ── Daily signal deduplication ────────────────────────────────────────
        # 1day signals fire on every 15min candle close but the daily bar hasn't
        # changed — it's identical data re-evaluated 26× per day. Once we've run
        # the pipeline for a given (symbol, signal_type) on the daily TF, mark it
        # as evaluated for today. Subsequent candle closes skip it.
        # 15min signals are NOT deduplicated — each candle is genuinely new data.
        # EARNINGS_BEAT/MISS signals use their own earnings:fired cooldown key instead.
        _earnings_signal = top.signal_type.value in (
            "EARNINGS_BEAT", "EARNINGS_MISS", "CATALYST_GAP_PEAD", "INTRADAY_IDARVAS"
        )
        if top.timeframe == "1day" and not _earnings_signal:
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
        # Paper/dev: 4 — observe more setups, validate full trade flow.
        # Live: 6 — tighter quality bar before real money.
        _MIN_CONFLUENCE = 4 if settings.app_env.value in ("paper", "development") else 6
        _HQ_SIGNALS = {
            "BREAKOUT_HIGH",   "BREAKOUT_LOW",
            "DOUBLE_BOTTOM",   "DOUBLE_TOP",
            "DARVAS_BREAKOUT",
            "ENGULFING_BULL",  "ENGULFING_BEAR",
            "EVENING_STAR",    "MORNING_STAR",
            "BULL_FLAG",       "BEAR_FLAG",
            "KEY_LEVEL_BOUNCE",               # structural reversal — high quality
            "OPENING_DRIVE",                  # gap + strong first candle — clean setup
            "EARNINGS_BEAT",                  # fundamental catalyst — always high quality
            "CATALYST_GAP_PEAD",              # PEAD Day2 entry — backtest validated 2.28 Sharpe
            "INTRADAY_IDARVAS",               # Darvas box on gap stock — backtest validated Sharpe 9.8
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

        # Factor 5: multi-timeframe agreement.
        # Count distinct timeframes that agree with top signal's direction.
        # Previously counted distinct signal_types — but MACD + RSI + EMA all firing
        # on the same 15min bar are driven by the same price move (correlated, not
        # independent). A 15min signal + 1hr signal + 1day signal agreeing is genuine
        # multi-source confirmation.
        _agreeing_tfs = len({s.timeframe for s in signals if s.direction == top.direction})
        if _agreeing_tfs >= 3:
            _f_multi = 2
        elif _agreeing_tfs >= 2:
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

        # ── Time-of-day filter (intraday signals only) ────────────────────────
        # Swing signals (1day, 1week) are evaluated once on daily candle close
        # — time-of-day doesn't apply. Intraday signals are time-sensitive.
        #
        # Rules (IST):
        #   After 14:30 — no new intraday entries. EOD unwinding distorts moves.
        #   11:30 – 13:00 — lunch chop zone. Confidence docked 15 points.
        #                   Signal can still fire if it clears threshold after penalty.
        if top.timeframe not in ("1day", "1week"):
            from zoneinfo import ZoneInfo as _ZI
            _now_ist = datetime.now(_ZI("Asia/Kolkata"))
            _h, _m = _now_ist.hour, _now_ist.minute
            _mins = _h * 60 + _m

            if _mins >= 14 * 60 + 30:   # after 2:30 PM
                log.info(
                    "signal.time_blocked",
                    symbol=symbol, signal=top.signal_type.value,
                    reason="No new intraday entries after 14:30 IST",
                    time=f"{_h:02d}:{_m:02d}",
                )
                return

            if 11 * 60 + 30 <= _mins < 13 * 60:   # 11:30 AM – 1:00 PM lunch chop
                _penalty = 15
                top.confidence = max(0, top.confidence - _penalty)
                log.info(
                    "signal.lunch_chop_penalty",
                    symbol=symbol, signal=top.signal_type.value,
                    confidence_after=top.confidence,
                    penalty=_penalty,
                    reason="Lunch chop window 11:30–13:00 IST",
                )

        # Execute if above confidence threshold → RiskEngine → Claude → broker
        confidence_threshold = cfg.get("confidence_threshold", 65)
        if top.confidence >= confidence_threshold:
            executor = TradeExecutor()
            trade = await executor.execute(top)
            # Set cooldown ONLY after a trade actually opened.
            if trade is not None and top.timeframe == "1day":
                if top.signal_type.value in ("EARNINGS_BEAT", "EARNINGS_MISS"):
                    # 72h cooldown — same earnings event shouldn't re-fire next morning
                    await redis.setex(f"earnings:fired:{symbol}", 72 * 3600, "1")
                elif top.signal_type.value == "CATALYST_GAP_PEAD":
                    pass  # cooldown already set in CatalystLiveEngine.detect()
                elif top.signal_type.value == "INTRADAY_IDARVAS":
                    pass  # cooldown already set in IntradayLiveEngine.detect()
                else:
                    cooldown_key = f"momentum:cooldown:{symbol}"
                    await redis.setex(cooldown_key, 86_400, "1")
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
        # Kick WebSocket feed to reconnect with the new token immediately.
        # Without this, the feed's retry loop may be backed off for minutes.
        if _feed_manager is not None:
            asyncio.create_task(_feed_manager.force_reconnect())
            log.info("scheduler.auth_feed_reconnect_triggered")
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


async def job_fetch_earnings_calendar() -> None:
    """
    8:00 AM IST — Fetch today's NSE earnings announcements and cache in Redis.
    Runs before auth so the list is ready when the market opens.
    Also invalidates the recent_symbols cache so it rebuilds with fresh data.
    """
    from config.market_hours import is_trading_day
    if not is_trading_day():
        return
    if await _is_job_done("earnings_calendar"):
        log.info("scheduler.earnings_calendar_skip", reason="already_done_today")
        return
    try:
        from services.earnings_engine.announcements import (
            fetch_results_for_date,
            invalidate_recent_cache,
            get_recent_results_symbols,
            check_fetch_failures,
        )
        from datetime import date as _date
        today = _date.today()
        today_symbols = await fetch_results_for_date(today)
        await invalidate_recent_cache()
        all_recent = await get_recent_results_symbols(lookback_days=3)
        log.info(
            "scheduler.earnings_calendar_fetched",
            today=len(today_symbols),
            recent_3d=len(all_recent),
            today_symbols=today_symbols[:10],
        )
        notifier = get_notifier()
        if await check_fetch_failures(today):
            log.warning("scheduler.earnings_fetch_failed_alert", date=str(today))
            await notifier._send(
                f"⚠️ *Earnings fetch FAILED* for {today}\n"
                "NSE API returned empty/error. Earnings signals may be unreliable today.",
                parse_mode="Markdown",
            )
        elif today_symbols:
            syms_str = ", ".join(today_symbols[:15])
            await notifier._send(
                f"📊 *Earnings Today ({len(today_symbols)} stocks)*\n`{syms_str}`",
                parse_mode="Markdown",
            )
        await _mark_job_done("earnings_calendar")
    except Exception as e:
        log.error("scheduler.earnings_calendar_error", error=str(e))


async def job_earnings_scan() -> None:
    """
    9:30 AM IST — Bulk scan all stocks with recent earnings for gap-and-go signals.
    Fires 15 minutes after open so the gap is confirmed (not just pre-open noise).
    """
    from config.market_hours import is_trading_day
    if not is_trading_day():
        return
    if await _is_job_done("earnings_scan"):
        log.info("scheduler.earnings_scan_skip", reason="already_done_today")
        return
    try:
        from services.earnings_engine.engine import EarningsSignalEngine
        from services.execution.trade_executor import TradeExecutor

        redis = get_redis()
        engine = EarningsSignalEngine()
        signals = await engine.scan_all(_candle_buffer, redis)

        if not signals:
            log.info("scheduler.earnings_scan_no_signals")
            return

        # Long-only filter (same as main pipeline)
        signals = [s for s in signals if s.direction.value == "BULLISH"]
        log.info("scheduler.earnings_scan_signals", count=len(signals),
                 symbols=[s.trading_symbol for s in signals])

        executor = TradeExecutor()
        for sig in signals:
            try:
                trade = await executor.execute(sig)
                if trade:
                    # 72h cooldown — per-candle check won't re-fire on same earnings event
                    await redis.setex(f"earnings:fired:{sig.trading_symbol}", 72 * 3600, "1")
            except Exception as e:
                log.warning("scheduler.earnings_execute_error",
                            symbol=sig.trading_symbol, error=str(e))
        await _mark_job_done("earnings_scan")
    except Exception as e:
        log.error("scheduler.earnings_scan_error", error=str(e))


async def job_market_open_briefing() -> None:
    """9:10 AM IST — Claude researches market conditions and sends an informed briefing."""
    from config.market_hours import is_trading_day
    if not is_trading_day():
        log.info("scheduler.briefing_skip", reason="NSE holiday or weekend")
        return
    if await _is_job_done("market_briefing"):
        log.info("scheduler.briefing_skip", reason="already_sent_today")
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

    # ── Nifty 50 expected change % ────────────────────────────────────────────
    # NOTE: briefing fires at 9:10 AM — market still in pre-open session.
    # Redis tick lp = pre-open equilibrium price, NOT actual opening price.
    # Pre-open price can differ 50-100pts from actual open → use GIFT Nifty instead.
    # nifty_change_pct will be overridden below with GIFT Nifty once fetched.
    nifty_change_pct = 0.0
    nifty_prev_close = 0.0
    nifty_raw = await redis.get("market:tick:NIFTY 50")
    if nifty_raw:
        try:
            nifty_data   = _json.loads(nifty_raw)
            nifty_prev_close = float(nifty_data.get("c", 0) or 0)  # prev close only
        except Exception:
            pass

    # ── Recent news headlines + GIFT Nifty (run in parallel) ─────────────────
    from services.data_ingestion.gift_nifty import (
        fetch_gift_nifty_change,
        fetch_market_news_sentiment,
        fetch_india_vix,
        fetch_fii_data,
        fetch_advance_decline,
    )

    headlines:   list[str] = []
    gift_pct:    float | None = None
    news_score:  float | None = None
    fii_str:     str | None = None
    adv_dec_str: str | None = None

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

    gift_pct, news_score, fii_str, adv_dec_str = await asyncio.gather(
        fetch_gift_nifty_change(),
        fetch_market_news_sentiment(hours=12),
        fetch_fii_data(),
        fetch_advance_decline(),
        return_exceptions=True,
    )
    gift_pct    = gift_pct    if isinstance(gift_pct,    float) else None
    news_score  = news_score  if isinstance(news_score,  float) else None
    fii_str     = fii_str     if isinstance(fii_str,     str)   else None
    adv_dec_str = adv_dec_str if isinstance(adv_dec_str, str)   else None

    # Use GIFT Nifty % as the expected Nifty change — more accurate than pre-open lp
    # GIFT Nifty futures closely track Nifty 50 expected open
    if gift_pct is not None:
        nifty_change_pct = gift_pct

    # VIX fallback: if Redis tick unavailable (WebSocket not yet subscribed), use yfinance
    if vix is None:
        try:
            vix = await fetch_india_vix()
        except Exception:
            pass

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
        advance_decline  = adv_dec_str or "N/A",
        fii_activity     = fii_str     or "N/A",
        top_movers       = [],
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
        fii_activity     = fii_str,
        advance_decline  = adv_dec_str,
        headlines        = len(headlines),
    )

    await notifier.market_open(
        regime   = regime,
        vix      = vix,
        briefing = briefing,
    )
    await _mark_job_done("market_briefing")


async def job_orb_scan() -> list:
    """
    10:00 AM IST — Scan all symbols for ORB breakouts.

    The 9:45 candle (9:45–10:00 IST) closes at exactly 10:00. This job runs
    immediately after, reads today's 15-min candles from _candle_buffer, applies
    the ORB rules (Nifty gate + OR high breakout + volume surge), and routes
    qualifying setups directly through TradeExecutor — bypassing the daily-signal
    dedup and momentum confluence gate (ORB has its own entry criteria).
    """
    # Redis SETNX: atomic dedup gate — works across asyncio tasks AND APScheduler threads.
    # asyncio.Lock alone is insufficient when APScheduler fires two tasks near-simultaneously.
    from datetime import date as _orb_date
    _redis_dedup = get_redis()
    _inprog_key  = f"job:inprogress:orb_scan:{_orb_date.today()}"
    _acquired = await _redis_dedup.set(_inprog_key, "1", nx=True, ex=7200)
    if not _acquired:
        log.info("scheduler.orb_scan_skip", reason="already_running_or_done")
        return []

    global _orb_scan_lock
    if _orb_scan_lock is None:
        _orb_scan_lock = asyncio.Lock()
    async with _orb_scan_lock:
        if await _is_job_done("orb_scan"):
            log.info("scheduler.orb_scan_skip", reason="already_done_today")
            return []
        from config.market_hours import is_trading_day
        if not is_trading_day():
            log.info("scheduler.orb_scan_skip", reason="NSE holiday or weekend")
            return []

        from datetime import date as _date
        from services.orb_engine.live import scan_orb_signals
        from services.data_ingestion.nifty500_instruments import get_live_universe
        from services.execution.trade_executor import TradeExecutor

        # Check daily loss limit before scanning — no point firing if already at limit
        from services.risk_engine.engine import RiskEngine
        _risk = RiskEngine()
        daily_pnl = await _risk._get_todays_pnl()
        from config.settings import settings as _settings
        if daily_pnl <= -_settings.daily_loss_limit_inr:
            log.warning("orb_scan.blocked_daily_loss_limit", daily_pnl=daily_pnl)
            return []

        symbols = get_live_universe()
        today   = _date.today()

        log.info("orb_scan.start", symbols=len(symbols))
        import asyncio as _asyncio
        import functools as _functools
        signals = await _asyncio.get_running_loop().run_in_executor(
            None, _functools.partial(scan_orb_signals, _candle_buffer, symbols, today)
        )

        # Audit log — persist daily scan results to Redis (TTL 7 days)
        import json as _json
        _redis = get_redis()
        _audit = {
            "date": str(today),
            "total_symbols": len(symbols),
            "setups_found": len(signals),
            "setups": [
                {"symbol": s.trading_symbol, "entry": s.price_at_signal,
                 "or_high": s.indicators.get("or_high"), "or_low": s.indicators.get("or_low"),
                 "vol_ratio": s.indicators.get("vol_ratio")}
                for s in signals
            ],
        }
        await _redis.setex(f"orb:audit:{today}", 7 * 86400, _json.dumps(_audit))

        if not signals:
            log.info("orb_scan.no_setups")
            return []

        # Cap at top 5 setups by volume ratio — prevents flooding positions on strong trend days
        MAX_ORB_TRADES = 5
        signals = sorted(signals, key=lambda s: s.indicators.get("vol_ratio", 0), reverse=True)[:MAX_ORB_TRADES]

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

        await _mark_job_done("orb_scan")
        return signals


async def job_market_open_ping() -> None:
    """
    9:15 AM IST — Send opening tick snapshot to Telegram.
    Shows Nifty 50 and Sensex opening price vs previous close (up/down + %).
    Uses yfinance for prev-close and Redis tick for current price; falls back
    to yfinance if Redis tick not available yet (feed may still be connecting).
    """
    from config.market_hours import is_trading_day
    if not is_trading_day():
        return
    if await _is_job_done("market_open_ping"):
        log.info("scheduler.market_open_ping_skip", reason="already_sent_today")
        return
    try:
        import json as _json
        import asyncio as _asyncio

        redis = get_redis()

        async def _fetch_index(symbol: str, yf_ticker: str) -> tuple[float | None, float | None]:
            """Return (current_price, prev_close) for the given index."""
            lp, prev_close = None, None

            # Try Redis tick first (live feed) — only if tick is from TODAY
            tick_raw = await redis.get(f"market:tick:{symbol}")
            if tick_raw:
                try:
                    from datetime import date as _date
                    d  = _json.loads(tick_raw)
                    ts = d.get("ts", "")
                    # Accept tick only if it was written today (TTL=300s so stale ticks expire,
                    # but guard explicitly in case clock skew re-delivers old data)
                    if ts and ts[:10] == _date.today().isoformat():
                        lp         = float(d.get("lp") or 0) or None
                        prev_close = float(d.get("c")  or 0) or None
                except Exception:
                    pass

            # Fall back to yfinance — use 1m interval to get today's actual open price.
            # Daily interval at 9:16 AM returns yesterday's close as iloc[-1] (today not complete).
            # 1m interval returns actual intraday bars including the 9:15 opening candle.
            if lp is None or prev_close is None:
                try:
                    import yfinance as yf
                    _ticker_sym = yf_ticker

                    def _fetch():
                        # Get today's 1m bars — first bar = opening price
                        intra = yf.Ticker(_ticker_sym).history(period="1d", interval="1m", auto_adjust=True)
                        # Get prev close from daily data
                        daily = yf.Ticker(_ticker_sym).history(period="5d", interval="1d", auto_adjust=True)
                        return intra, daily

                    intra, daily = await _asyncio.get_running_loop().run_in_executor(None, _fetch)

                    # prev_close = last completed daily bar close (yesterday)
                    if daily is not None and len(daily) >= 1:
                        prev_close = float(daily["Close"].iloc[-1])

                    # lp = latest available intraday price (most recent 1m bar close)
                    if intra is not None and len(intra) >= 1:
                        lp = float(intra["Close"].iloc[-1])
                    elif prev_close is not None:
                        lp = prev_close  # market not yet open, show flat

                except Exception as _yf_err:
                    log.warning("scheduler.market_open_ping_yfinance_error",
                                symbol=symbol, error=str(_yf_err))
                    # keep lp/prev_close as None — handled below

            return lp, prev_close

        nifty_lp, nifty_prev   = await _fetch_index("NIFTY 50",  "^NSEI")
        sensex_lp, sensex_prev = await _fetch_index("SENSEX",    "^BSESN")

        def _fmt(name: str, lp: float | None, prev: float | None) -> str:
            if lp is None or prev is None or prev == 0:
                return f"*{name}*: data unavailable"
            chg     = lp - prev
            pct     = chg / prev * 100
            arrow   = "▲" if chg >= 0 else "▼"
            emoji   = "🟢" if chg >= 0 else "🔴"
            return f"{emoji} *{name}*: {lp:,.0f}  {arrow} {abs(chg):,.0f} ({abs(pct):.2f}%)"

        lines = [
            f"🔔 *Market Open — {datetime.now().strftime('%d %b %Y')}*",
            "──────────────────",
            _fmt("NIFTY 50", nifty_lp, nifty_prev),
            _fmt("SENSEX",   sensex_lp, sensex_prev),
        ]
        notifier = get_notifier()
        await notifier._send("\n".join(lines), parse_mode="Markdown")
        log.info("scheduler.market_open_ping_sent")
        await _mark_job_done("market_open_ping")
    except Exception as e:
        log.error("scheduler.market_open_ping_failed", error=str(e), exc_info=True)


async def job_square_off_intraday() -> None:
    """3:12 PM IST — Square off all intraday positions and close them in DB."""
    from config.market_hours import is_trading_day
    if not is_trading_day():
        log.info("scheduler.square_off_skip", reason="NSE holiday or weekend")
        return
    log.warning("scheduler.square_off_intraday", time="15:12")
    try:
        if settings.uses_real_broker:
            from services.execution.broker_router import get_broker
            try:
                await get_broker().square_off_all_intraday()
            except Exception as e:
                log.error("scheduler.square_off_broker_error", error=str(e))
        # Close all remaining OPEN trades in DB at current market price
        closed = await get_lifecycle_manager().close_all_open_trades(reason="TIME_EXIT")
        log.info("scheduler.square_off_db_closed", count=closed)
    except Exception as e:
        log.error("scheduler.square_off_failed", error=str(e), exc_info=True)


async def job_flush_eod_candles() -> None:
    """3:31 PM IST — Flush any open (in-progress) candles so last bar of day is not lost."""
    try:
        if _feed_manager is not None:
            _feed_manager.flush_open_candles()
            log.info("scheduler.eod_candle_flush", status="done")
        else:
            log.warning("scheduler.eod_candle_flush_skip", reason="feed_manager not initialised")
    except Exception as e:
        log.error("scheduler.eod_candle_flush_failed", error=str(e), exc_info=True)


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


async def job_watchlist_update() -> None:
    """4:00 PM IST — Rebuild RS-ranked incubator watchlist for the momentum engine."""
    try:
        from services.data_ingestion.nifty500_instruments import NIFTY500
        from services.momentum_engine.watchlist import WatchlistBuilder

        symbols = [s for s, _, _ in NIFTY500]
        builder = WatchlistBuilder()
        watchlist = await builder.update(symbols)
        log.info("scheduler.watchlist_update_done", count=len(watchlist), top5=watchlist[:5])
    except Exception as e:
        log.error("scheduler.watchlist_update_error", error=str(e), exc_info=True)


async def job_sector_roc_update() -> None:
    """3:45 PM IST — Compute sector index ROC-20 and store in Redis.

    Used by MomentumLiveEngine to gate RANGING-regime trades:
    only fire Darvas signals when stock's sector ROC-20 ≥ 5%
    (sector trending even though Nifty is flat = sector rotation play).

    Redis keys: momentum:sector_roc20:{sector_name}  (float, TTL 28h)
    """
    try:
        import yfinance as yf
        import pandas as pd
        from services.data_ingestion.nifty500_instruments import SECTOR_INDEX_MAP

        redis  = _get_redis()
        loaded = 0

        for sector, yf_ticker in SECTOR_INDEX_MAP.items():
            try:
                raw = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda t=yf_ticker: yf.Ticker(t).history(period="60d", interval="1d", auto_adjust=True),
                )
                if raw.empty:
                    continue

                closes = raw["Close"].ffill()
                if len(closes) < 21:
                    continue

                roc20 = float((closes.iloc[-1] / closes.iloc[-21] - 1) * 100)
                redis_key = f"momentum:sector_roc20:{sector}"
                await redis.set(redis_key, str(round(roc20, 2)), ex=28 * 3600)
                loaded += 1
            except Exception as e:
                log.warning("scheduler.sector_roc_symbol_error", sector=sector, error=str(e))

        log.info("scheduler.sector_roc_update_done", loaded=loaded, total=len(SECTOR_INDEX_MAP))
    except Exception as e:
        log.error("scheduler.sector_roc_update_error", error=str(e), exc_info=True)


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
                        LIMIT 100
                    """),
                    {"sym": symbol},
                )
                rows = result.fetchall()
                if not rows:
                    continue

                if symbol not in _candle_buffer:
                    _candle_buffer[symbol] = {}
                if "1day" not in _candle_buffer[symbol]:
                    _candle_buffer[symbol]["1day"] = deque(maxlen=BUFFER_MAX_BY_TF.get("1day", BUFFER_MAX))

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

        loop = asyncio.get_running_loop()
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

                df = None
                for _attempt in range(3):
                    try:
                        df = await loop.run_in_executor(None, _fetch, yf_ticker)
                        break
                    except Exception:
                        await asyncio.sleep(2 ** _attempt)

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
                    _candle_buffer[symbol]["15min"] = deque(maxlen=BUFFER_MAX_BY_TF.get("15min", BUFFER_MAX))

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


async def _is_job_done(job_name: str) -> bool:
    """Return True if job already ran successfully today (Redis flag set)."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    today = _dt.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    redis = get_redis()
    return bool(await redis.get(f"job:done:{job_name}:{today}"))


async def _mark_job_done(job_name: str) -> None:
    """
    Set Redis flag indicating `job_name` completed successfully today.
    TTL = seconds until midnight IST — flag auto-expires, no manual reset needed.
    Key: job:done:{job_name}:{YYYY-MM-DD}
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    now = _dt.now(IST)
    midnight = now.replace(hour=23, minute=59, second=59, microsecond=0)
    ttl = max(60, int((midnight - now).total_seconds()))
    redis = get_redis()
    await redis.setex(f"job:done:{job_name}:{now.date().isoformat()}", ttl, "1")
    log.info("watchdog.job_flagged_done", job=job_name, ttl_sec=ttl)


async def job_watchdog() -> None:
    """
    Runs every 5 min (7:45–11:00 AM IST, weekdays).
    Checks if each critical morning job completed today.
    If flag missing AND current time is within the job's retry window → reruns it.

    Redis flags: job:done:{job_name}:{YYYY-MM-DD}  (TTL auto-expires at midnight)

    Critical jobs tracked:
      earnings_calendar  — window 08:00–09:14
      market_briefing    — window 09:10–09:59
      earnings_scan      — window 09:31–10:30
      market_open_ping   — window 09:15–09:59
      orb_scan           — window 10:00–11:00
    """
    from config.market_hours import is_trading_day
    if not is_trading_day():
        return

    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
    now  = _dt.now(IST)
    today = now.date().isoformat()
    redis = get_redis()

    # (flag_name, job_fn, retry_window_start_hhmm, retry_window_end_hhmm)
    WATCHLIST = [
        ("earnings_calendar", job_fetch_earnings_calendar, (8,  0), (9, 14)),
        ("market_briefing",   job_market_open_briefing,    (9, 10), (9, 59)),
        ("earnings_scan",     job_earnings_scan,            (9, 31), (10, 30)),
        ("market_open_ping",  job_market_open_ping,         (9, 15), (9, 59)),
        ("orb_scan",          job_orb_scan,                 (10, 0), (11,  0)),
    ]

    for flag_name, job_fn, (wh, wm), (eh, em) in WATCHLIST:
        key = f"job:done:{flag_name}:{today}"
        if await redis.get(key):
            continue  # already ran — skip

        window_start = now.replace(hour=wh, minute=wm, second=0, microsecond=0)
        window_end   = now.replace(hour=eh, minute=em, second=0, microsecond=0)

        if window_start <= now <= window_end:
            log.warning("watchdog.rerunning_missed_job", job=flag_name,
                        time=now.strftime("%H:%M:%S"))
            try:
                await job_fn()
            except Exception as e:
                log.error("watchdog.rerun_error", job=flag_name, error=str(e))


_tick_silence_alerted: bool = False   # rate-limit: one alert per silence episode


async def job_status_heartbeat() -> None:
    """
    Prints a status line every 5 minutes during market hours.
    Shows regime, candle buffer depth, open positions, and last signal seen.
    Also checks for tick feed silence during market hours and sends Telegram alert.
    """
    global _tick_silence_alerted
    try:
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
        now_ist = datetime.now(IST)
        in_market_hours = (
            now_ist.weekday() < 5
            and (9, 15) <= (now_ist.hour, now_ist.minute) <= (15, 30)
        )

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

        # ── Tick silence detection ────────────────────────────────────────────
        # During market hours, alert if no tick received in last 5 minutes.
        # Uses in-process _last_tick_time first, falls back to Redis timestamp.
        if in_market_hours and _feed_manager is not None:
            last_tick = _feed_manager._last_tick_time
            if last_tick is None:
                # Try Redis fallback (persisted every 500 ticks)
                ts_raw = await redis.get("feed:last_tick_time")
                if ts_raw:
                    try:
                        last_tick = datetime.fromisoformat(ts_raw)
                    except ValueError:
                        pass

            silence_threshold = 5 * 60  # 5 minutes in seconds
            if last_tick is not None:
                # Make naive for comparison
                lt = last_tick.replace(tzinfo=None) if last_tick.tzinfo else last_tick
                silent_secs = (datetime.now() - lt).total_seconds()
                if silent_secs > silence_threshold and not _tick_silence_alerted:
                    _tick_silence_alerted = True
                    mins = int(silent_secs // 60)
                    try:
                        from services.notifications.telegram_bot import get_notifier
                        notifier = get_notifier()
                        if notifier:
                            await notifier.send(
                                f"⚠️ No ticks for {mins}min during market hours. "
                                f"Feed may be disconnected or throttled."
                            )
                    except Exception:
                        pass
                elif silent_secs <= silence_threshold and _tick_silence_alerted:
                    _tick_silence_alerted = False  # reset after recovery

        sample = {sym: {tf: len(buf) for tf, buf in tfs.items()}
                  for sym, tfs in list(_candle_buffer.items())[:3]}
        last_tick_str = (
            _feed_manager._last_tick_time.strftime("%H:%M:%S")
            if _feed_manager and _feed_manager._last_tick_time
            else "none"
        )
        print(
            f"[{datetime.now().strftime('%H:%M:%S')}] HEARTBEAT | "
            f"regime={regime} | symbols_buffered={buffered} | open_positions={open_positions} | "
            f"last_tick={last_tick_str} | buffer_sample={sample}",
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


async def _bootstrap_sector_roc() -> None:
    """
    Compute sector_roc20 Redis keys at startup if missing or stale.
    The 3:45 PM cron keeps these fresh during normal operation, but a mid-day
    restart leaves all keys missing → MomentumLiveEngine skips every RANGING
    symbol conservatively → zero momentum trades even when valid setups exist.
    TTL 28h so keys survive overnight until next day's cron run.
    """
    try:
        import yfinance as yf
        from services.data_ingestion.nifty500_instruments import SECTOR_INDEX_MAP
        from database.connection import get_redis as _get_redis

        redis = _get_redis()

        # Check if keys already populated (any sector key exists and is fresh)
        existing = await redis.keys("momentum:sector_roc20:*")
        if len(existing) >= len(SECTOR_INDEX_MAP) // 2:
            log.info("startup.sector_roc_ok", count=len(existing))
            return

        log.info("startup.sector_roc_bootstrap_start",
                 missing=len(SECTOR_INDEX_MAP) - len(existing))
        loaded = 0
        for sector, yf_ticker in SECTOR_INDEX_MAP.items():
            try:
                raw = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda t=yf_ticker: yf.Ticker(t).history(
                        period="60d", interval="1d", auto_adjust=True
                    ),
                )
                if raw is None or raw.empty or len(raw) < 21:
                    continue
                closes = raw["Close"].ffill()
                roc20 = float((closes.iloc[-1] / closes.iloc[-21] - 1) * 100)
                await redis.set(
                    f"momentum:sector_roc20:{sector}",
                    str(round(roc20, 2)),
                    ex=28 * 3600,
                )
                loaded += 1
            except Exception as _e:
                log.warning("startup.sector_roc_symbol_error", sector=sector, error=str(_e))

        log.info("startup.sector_roc_bootstrap_done",
                 loaded=loaded, total=len(SECTOR_INDEX_MAP))
    except Exception as e:
        log.warning("startup.sector_roc_bootstrap_failed", error=str(e))


async def _bootstrap_watchlist() -> None:
    """
    Ensure the RS watchlist is populated before market open.
    Runs in background at startup. Rebuilds if:
      - Redis key missing (first run / Redis flush)
      - Last update timestamp is not from today
    Skips rebuild if watchlist was already updated today (4 PM cron already ran).
    """
    try:
        from services.momentum_engine.watchlist import WatchlistBuilder, REDIS_UPDATED_KEY
        from services.data_ingestion.nifty500_instruments import NIFTY500
        from database.connection import get_redis as _get_redis

        redis = _get_redis()
        updated_raw = await redis.get(REDIS_UPDATED_KEY)
        today_str   = datetime.now().strftime("%Y-%m-%d")

        if updated_raw and today_str in updated_raw:
            log.info("startup.watchlist_ok", updated=updated_raw)
            return

        log.info("startup.watchlist_rebuild", reason="stale or missing")
        symbols = [s for s, _, _ in NIFTY500]
        builder = WatchlistBuilder()
        watchlist = await builder.update(symbols)
        log.info("startup.watchlist_ready", count=len(watchlist), top5=watchlist[:5])
    except Exception as e:
        log.warning("startup.watchlist_error", error=str(e))


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
        from services.notifications.telegram_bot import start_telegram_polling, register_orb_scan_callback
        app_tg = await start_telegram_polling()
        register_orb_scan_callback(job_orb_scan)
        import main as _self
        _self._telegram_app = app_tg

    # 4. Zerodha auth — authenticate immediately if real feed needed and no valid token yet
    if settings.use_real_feed:
        existing_token = await redis.get("kite:access_token")
        if existing_token:
            log.info("startup.zerodha_token_ok", msg="Token already in Redis — skipping auth")
        else:
            log.info("startup.zerodha_auth_start", msg="No token in Redis — authenticating now")
            try:
                from services.execution.zerodha.authenticator import ZerodhaAuthenticator
                auth = ZerodhaAuthenticator()
                await auth.authenticate()
                log.info("startup.zerodha_auth_ok")
            except Exception as _auth_err:
                log.error("startup.zerodha_auth_failed", error=str(_auth_err),
                          msg="Bot will run without real feed — check KITE_* env vars")

    # 5. Seed historical data (skips if already seeded today)
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

    # 6. Pre-seed candle buffer from DB (fast) then 15-min from yfinance (slow —
    #    runs as background task so startup completes quickly)
    await _preseed_candle_buffer()

    # 7. Bootstrap market regime from historical data so it's never UNKNOWN at open
    await _bootstrap_regime()

    # 8. News feed (background polling — no-op if NEWS_API_KEY not set)
    news_service = get_news_service()
    await news_service.start()

    # 9. Trade lifecycle manager (monitors open trades, closes on SL/target hit)
    asyncio.create_task(get_lifecycle_manager().run())

    # 9a. Stale open position cleanup — close any OPEN intraday trades left over
    #     from a previous day (bot restart missed the 3:12 PM squareoff).
    #     Closes at the last known tick price (or entry price if no tick available).
    try:
        from sqlalchemy import text as _text
        from datetime import timezone as _tz, date as _date
        IST_OFFSET = _tz(timedelta(hours=5, minutes=30))
        today_ist  = datetime.now(IST_OFFSET).date()
        async with get_db_session() as _session:
            stale = await _session.execute(
                _text(
                    "SELECT id, trading_symbol, entry_price, entry_quantity "
                    "FROM trades WHERE status = 'OPEN' "
                    "AND DATE(entry_time AT TIME ZONE 'Asia/Kolkata') < :today"
                ),
                {"today": today_ist},
            )
            stale_rows = stale.fetchall()
        if stale_rows:
            log.warning(
                "startup.stale_positions_found",
                count=len(stale_rows),
                symbols=[r.trading_symbol for r in stale_rows],
                msg="Closing stale intraday positions from previous day(s)",
            )
            closed_stale = await get_lifecycle_manager().close_all_open_trades(
                reason="STALE_POSITION_CLEANUP"
            )
            log.warning("startup.stale_positions_closed", count=closed_stale)
        else:
            log.info("startup.stale_positions_none")
    except Exception as _stale_err:
        log.error("startup.stale_position_check_failed", error=str(_stale_err))

    # 10. Watchlist bootstrap — ensure RS watchlist is populated before market open.
    #     Runs as background task so it doesn't block startup.
    #     Logic: if watchlist key missing or last update was not today, rebuild now.
    asyncio.create_task(_bootstrap_watchlist())

    # 10a. Sector ROC bootstrap — compute sector_roc20 keys if missing.
    #      Without these, MomentumLiveEngine returns [] for ALL RANGING symbols
    #      (conservative skip when sector data absent). The cron runs at 3:45 PM,
    #      so a mid-day restart leaves keys missing for the entire trading session.
    asyncio.create_task(_bootstrap_sector_roc())

    log.info("startup.complete", env=settings.app_env.value)

    # ── Startup Telegram notification ─────────────────────────────────────────
    # Fires after all services are ready.  Useful when launchd starts the bot
    # in the background (no Terminal window) — confirms it actually launched.
    try:
        from datetime import timezone as _tz
        _now = datetime.now(tz=_tz.utc).astimezone()
        _env_label = settings.app_env.value.upper()
        _startup_msg = (
            f"🚀 *Bot started — {_env_label}*\n"
            f"Time: `{_now.strftime('%Y-%m-%d %H:%M:%S %Z')}`\n"
            f"All services ready. Market opens at 09:15 IST."
        )
        await get_notifier()._send(_startup_msg, parse_mode="Markdown")
    except Exception as _tg_err:
        log.warning("startup.telegram_notification_failed", error=str(_tg_err))


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
    global _v2_semaphore
    _v2_semaphore = asyncio.Semaphore(50)      # max 50 concurrent intraday V2 scans

    # ── Feed ─────────────────────────────────────────────────────────────────
    global _feed_manager
    feed = FeedManager()
    _feed_manager = feed
    feed.add_candle_listener(on_candle_complete)
    await feed.start()

    # ── Scheduler ────────────────────────────────────────────────────────────
    # Module-level reference so job_daily_auth can schedule its own retries
    global _scheduler
    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
    _scheduler = scheduler

    # Weekdays only (Mon=0 … Fri=4)
    scheduler.add_job(job_fetch_earnings_calendar, CronTrigger(day_of_week="0-4", hour=8, minute=0, timezone="Asia/Kolkata"))
    scheduler.add_job(job_daily_auth,          CronTrigger(day_of_week="0-4", hour=8,  minute=30, timezone="Asia/Kolkata"))
    scheduler.add_job(job_market_open_briefing, CronTrigger(day_of_week="0-4", hour=9, minute=10, timezone="Asia/Kolkata"))
    scheduler.add_job(job_earnings_scan,        CronTrigger(day_of_week="0-4", hour=9, minute=31, timezone="Asia/Kolkata"))
    scheduler.add_job(job_market_open_ping,     CronTrigger(day_of_week="0-4", hour=9, minute=16, timezone="Asia/Kolkata"))  # 9:16 not 9:15 — first trade needs 60s to propagate through WS
    scheduler.add_job(job_intraday_v2_context,  CronTrigger(day_of_week="0-4", hour=9, minute=26, timezone="Asia/Kolkata"))  # breadth from first two 5-min candles
    scheduler.add_job(job_orb_scan,             CronTrigger(day_of_week="0-4", hour=10, minute=0, timezone="Asia/Kolkata"))
    scheduler.add_job(job_square_off_intraday,  CronTrigger(day_of_week="0-4", hour=15, minute=12, timezone="Asia/Kolkata"))
    scheduler.add_job(job_flush_eod_candles,    CronTrigger(day_of_week="0-4", hour=15, minute=31, timezone="Asia/Kolkata"))
    scheduler.add_job(job_sector_roc_update,    CronTrigger(day_of_week="0-4", hour=15, minute=45, timezone="Asia/Kolkata"))
    scheduler.add_job(job_watchlist_update,     CronTrigger(day_of_week="0-4", hour=16, minute=0,  timezone="Asia/Kolkata"))
    scheduler.add_job(job_eod_summary,          CronTrigger(day_of_week="0-4", hour=16, minute=30, timezone="Asia/Kolkata"))
    scheduler.add_job(job_db_backup,            CronTrigger(day_of_week="0-4", hour=16, minute=45, timezone="Asia/Kolkata"))
    # Paper/dev only: status heartbeat every 5 minutes
    scheduler.add_job(job_status_heartbeat,     CronTrigger(minute="*/5"))
    # Watchdog: reruns missed morning jobs if network blip caused them to be skipped
    scheduler.add_job(job_watchdog, CronTrigger(day_of_week="0-4", hour="7-10", minute="*/5", timezone="Asia/Kolkata"))
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


def _acquire_pid_lock() -> None:
    """
    Write PID file to /tmp/trading_bot.pid.
    Abort if another instance is already running.
    """
    import os
    pid_file = "/tmp/trading_bot.pid"
    if os.path.exists(pid_file):
        try:
            old_pid = int(open(pid_file).read().strip())
            os.kill(old_pid, 0)   # signal 0 = check existence only
            console.print(
                f"[red]ERROR: Bot already running (PID {old_pid}). "
                f"Stop it first or delete {pid_file}.[/red]"
            )
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass   # stale lock — overwrite
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))


def _release_pid_lock() -> None:
    import os
    try:
        os.unlink("/tmp/trading_bot.pid")
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    _acquire_pid_lock()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
    finally:
        _release_pid_lock()
        sys.exit(0)
