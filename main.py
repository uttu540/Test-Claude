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
from datetime import datetime

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from config.settings import settings
from database.connection import close_db, close_redis, get_redis, init_db
from services.data_ingestion.historical_seed import HistoricalSeeder
from services.data_ingestion.news_feed import get_news_service
from services.data_ingestion.websocket_feed import FeedManager, OHLCVCandle
from services.execution.trade_lifecycle import get_lifecycle_manager
from services.market_regime.detector import get_regime_detector
from services.notifications.telegram_bot import get_notifier
from services.execution.trade_executor import TradeExecutor
from services.technical_engine.signal_generator import (
    MultiTimeframeSignalEngine,
    Signal,
)

# ─── Logging Setup ────────────────────────────────────────────────────────────

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
BUFFER_MAX = 300   # Keep last 300 candles per symbol/timeframe


# ─── Candle Handler ───────────────────────────────────────────────────────────

def on_candle_complete(candle: OHLCVCandle) -> None:
    """
    Called every time a candle period closes.
    Adds to buffer and triggers signal generation.
    """
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

    # Run signal detection on the 15min candle close
    # (avoids running on every 1min candle — too noisy)
    # Guard: skip if a signal task is already running for this symbol
    if tf == "15min" and sym not in _active_signal_tasks:
        task = asyncio.create_task(_run_signals(sym))
        _active_signal_tasks.add(sym)
        task.add_done_callback(lambda _: _active_signal_tasks.discard(sym))


async def _run_signals(symbol: str) -> None:
    """Run multi-timeframe signal detection for a symbol."""
    try:
        import pandas as pd

        engine = MultiTimeframeSignalEngine()
        ohlcv_by_tf: dict[str, pd.DataFrame] = {}

        for tf, candles in _candle_buffer.get(symbol, {}).items():
            if len(candles) >= 30:   # Need enough data
                df = pd.DataFrame(list(candles)).set_index("ts")  # convert deque → list for pandas
                ohlcv_by_tf[tf] = df

        if not ohlcv_by_tf:
            return

        # Update market regime when processing the market proxy (NIFTY 50 or any liquid index)
        # Uses 1day data for a stable regime read; falls back to cached Redis value
        REGIME_PROXY = "NIFTY 50"
        redis = get_redis()
        if symbol == REGIME_PROXY and "1day" in ohlcv_by_tf:
            import json as _json
            vix_raw   = await redis.get("market:tick:INDIA VIX")
            india_vix = _json.loads(vix_raw).get("lp") if vix_raw else None
            await get_regime_detector().detect_and_publish(
                ohlcv_by_tf["1day"], india_vix=india_vix
            )

        # Read current regime for signal filtering
        regime = await redis.get("market:regime") or "UNKNOWN"

        signals = engine.analyse(symbol, ohlcv_by_tf, regime=regime)

        if signals:
            top = signals[0]   # Highest confidence signal
            log.info(
                "signal.detected",
                symbol=symbol,
                signal=top.signal_type.value,
                direction=top.direction.value,
                confidence=top.confidence,
                timeframe=top.timeframe,
            )

            # Publish to Redis for the API / dashboard and for AI tf_alignment context
            import json
            signal_payload = json.dumps(top.to_dict())
            await redis.setex(f"signal:latest:{symbol}",              900, signal_payload)
            await redis.setex(f"signal:latest:{symbol}:{top.timeframe}", 900, signal_payload)

            # Also cache direction for every detected signal (all timeframes)
            for sig in signals:
                await redis.setex(
                    f"signal:latest:{symbol}:{sig.timeframe}",
                    900,
                    json.dumps(sig.to_dict()),
                )

            # Execute trade if signal confidence meets threshold
            if top.confidence >= 65:
                executor = TradeExecutor()
                await executor.execute(top)

    except Exception as e:
        log.error("signal.run_error", symbol=symbol, error=str(e))


# ─── Scheduled Jobs ───────────────────────────────────────────────────────────

async def job_daily_auth() -> None:
    """8:30 AM IST — Re-authenticate Zerodha and refresh tokens."""
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
    except Exception as e:
        notifier = get_notifier()
        await notifier.system_error("DailyAuth", str(e))
        log.error("scheduler.auth_failed", error=str(e))


async def job_market_open_briefing() -> None:
    """9:10 AM IST — Send market open Telegram notification."""
    from config.market_hours import is_trading_day
    if not is_trading_day():
        log.info("scheduler.briefing_skip", reason="NSE holiday or weekend")
        return
    notifier = get_notifier()
    redis    = get_redis()

    regime = await redis.get("market:regime") or "UNKNOWN"

    # Try to get India VIX from cached ticks
    vix_data = await redis.get("market:tick:INDIA VIX")
    vix = None
    if vix_data:
        import json
        vix = json.loads(vix_data).get("lp")

    await notifier.market_open(
        regime   = regime,
        vix      = vix,
        briefing = f"Market opens in 5 mins. Monitoring {len(_candle_buffer) or 50} instruments.",
    )


async def job_square_off_intraday() -> None:
    """3:12 PM IST — Square off all intraday positions and close them in DB."""
    from config.market_hours import is_trading_day
    if not is_trading_day():
        return
    log.warning("scheduler.square_off_intraday", time="15:12")
    if settings.is_live:
        from services.execution.zerodha.order_manager import OrderManager
        om = OrderManager()
        await om.square_off_all_intraday()
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
        async for session in get_db_session():
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

    # 3. Seed historical data (skips if already seeded today)
    last_seed = await redis.get("meta:last_seed_date")
    today_str = datetime.now().strftime("%Y-%m-%d")
    if last_seed != today_str:
        log.info("startup.seeding_historical_data")
        seeder = HistoricalSeeder(use_kite=bool(settings.kite_api_key))
        await seeder.create_hypertable()
        await seeder.seed_all(timeframes=["1day"])
        await redis.setex("meta:last_seed_date", 86_400 * 2, today_str)
    else:
        log.info("startup.seed_skip", reason="Already seeded today")

    # 4. News feed (background polling — no-op if NEWS_API_KEY not set)
    news_service = get_news_service()
    await news_service.start()

    # 5. Trade lifecycle manager (monitors open trades, closes on SL/target hit)
    asyncio.create_task(get_lifecycle_manager().run())

    log.info("startup.complete", env=settings.app_env.value)


async def shutdown(scheduler: AsyncIOScheduler) -> None:
    """Graceful shutdown."""
    log.info("shutdown.start")
    scheduler.shutdown(wait=False)
    get_lifecycle_manager().stop()
    await get_news_service().stop()
    await close_db()
    await close_redis()
    log.info("shutdown.complete")


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    await startup()

    # ── Feed ─────────────────────────────────────────────────────────────────
    feed = FeedManager()
    feed.add_candle_listener(on_candle_complete)
    await feed.start()

    # ── Scheduler ────────────────────────────────────────────────────────────
    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")

    # Weekdays only (Mon=0 … Fri=4)
    scheduler.add_job(job_daily_auth,          CronTrigger(day_of_week="0-4", hour=8,  minute=30, timezone="Asia/Kolkata"))
    scheduler.add_job(job_market_open_briefing, CronTrigger(day_of_week="0-4", hour=9, minute=10, timezone="Asia/Kolkata"))
    scheduler.add_job(job_square_off_intraday,  CronTrigger(day_of_week="0-4", hour=15, minute=12, timezone="Asia/Kolkata"))
    scheduler.add_job(job_eod_summary,          CronTrigger(day_of_week="0-4", hour=16, minute=30, timezone="Asia/Kolkata"))
    scheduler.start()

    log.info("main.running", feed=feed._feed.__class__.__name__)

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
