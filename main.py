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
from services.data_ingestion.websocket_feed import FeedManager, OHLCVCandle
from services.notifications.telegram_bot import get_notifier
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
# Stores recent candles per (symbol, timeframe) for indicator computation
# Format: {symbol: {timeframe: [candle_dicts...]}}
_candle_buffer: dict[str, dict[str, list[dict]]] = {}
BUFFER_MAX = 300   # Keep last 300 candles per symbol/timeframe

# ─── Per-symbol signal task registry ─────────────────────────────────────────
# Prevents concurrent _run_signals tasks for the same symbol.
# Format: {symbol: asyncio.Task}
_signal_tasks: dict[str, asyncio.Task] = {}


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
        _candle_buffer[sym][tf] = []

    _candle_buffer[sym][tf].append({
        "open":   candle.open,
        "high":   candle.high,
        "low":    candle.low,
        "close":  candle.close,
        "volume": candle.volume,
        "ts":     candle.timestamp,
    })

    # Keep buffer bounded
    if len(_candle_buffer[sym][tf]) > BUFFER_MAX:
        _candle_buffer[sym][tf] = _candle_buffer[sym][tf][-BUFFER_MAX:]

    # Run signal detection on the 15min candle close
    # (avoids running on every 1min candle — too noisy)
    if tf == "15min":
        existing = _signal_tasks.get(sym)
        if existing and not existing.done():
            log.warning(
                "signal.task_skipped",
                symbol=sym,
                reason="Previous signal task still in-flight",
            )
            return
        task = asyncio.create_task(_run_signals(sym))
        _signal_tasks[sym] = task
        task.add_done_callback(lambda t: _signal_tasks.pop(sym, None))


async def _run_signals(symbol: str) -> None:
    """Run multi-timeframe signal detection for a symbol."""
    try:
        import pandas as pd

        engine = MultiTimeframeSignalEngine()
        ohlcv_by_tf: dict[str, pd.DataFrame] = {}

        for tf, candles in _candle_buffer.get(symbol, {}).items():
            if len(candles) >= 30:   # Need enough data
                df = pd.DataFrame(candles).set_index("ts")
                ohlcv_by_tf[tf] = df

        if not ohlcv_by_tf:
            return

        signals = engine.analyse(symbol, ohlcv_by_tf)

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

            # In paper/dev mode: send Telegram alert for every high-confidence signal
            if not settings.is_live and top.confidence >= 65:
                notifier = get_notifier()
                await notifier.signal_alert(
                    symbol     = symbol,
                    signal     = top.signal_type.value,
                    direction  = top.direction.value,
                    confidence = top.confidence,
                    timeframe  = top.timeframe,
                    price      = top.price_at_signal,
                    notes      = top.notes,
                )

    except Exception as e:
        log.error("signal.run_error", symbol=symbol, error=str(e))


# ─── Scheduled Jobs ───────────────────────────────────────────────────────────

async def job_daily_auth() -> None:
    """8:30 AM IST — Re-authenticate Zerodha and refresh tokens."""
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
    """3:20 PM IST — Square off all intraday positions."""
    log.warning("scheduler.square_off_intraday", time="15:20")
    if settings.is_live:
        from services.execution.zerodha.order_manager import OrderManager
        om = OrderManager()
        await om.square_off_all_intraday()


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
                        COALESCE(SUM(brokerage + stt + exchange_charges + gst), 0) as charges
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

    log.info("startup.complete", env=settings.app_env.value)


async def shutdown(scheduler: AsyncIOScheduler) -> None:
    """Graceful shutdown."""
    log.info("shutdown.start")
    scheduler.shutdown(wait=False)
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
    scheduler.add_job(job_square_off_intraday,  CronTrigger(day_of_week="0-4", hour=15, minute=20, timezone="Asia/Kolkata"))
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
