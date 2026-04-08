"""
services/data_ingestion/historical_seed.py
───────────────────────────────────────────
Seeds historical OHLCV data into TimescaleDB.

Sources (in order of preference):
  1. Kite Connect historical API  (requires API key — most accurate)
  2. yfinance                      (free fallback, good for daily candles)

Run once at setup, then nightly for EOD updates.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pandas as pd
import structlog
import yfinance as yf
from sqlalchemy import text

from config.settings import settings
from database.connection import get_db_session
from services.data_ingestion.nifty50_instruments import NIFTY50

log = structlog.get_logger(__name__)

# TimescaleDB hypertable DDL — created once on first seed
OHLCV_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS ohlcv (
    trading_symbol  VARCHAR(50)  NOT NULL,
    exchange        VARCHAR(10)  NOT NULL DEFAULT 'NSE',
    timeframe       VARCHAR(10)  NOT NULL,  -- '1min','5min','15min','1hr','1day'
    ts              TIMESTAMPTZ  NOT NULL,
    open            NUMERIC(12,4),
    high            NUMERIC(12,4),
    low             NUMERIC(12,4),
    close           NUMERIC(12,4),
    volume          BIGINT,
    PRIMARY KEY (trading_symbol, timeframe, ts)
);

-- Convert to TimescaleDB hypertable (partitioned by time)
SELECT create_hypertable(
    'ohlcv', 'ts',
    if_not_exists => TRUE,
    chunk_time_interval => INTERVAL '7 days'
);

-- Data retention: keep 1min candles 90 days, daily candles forever
SELECT add_retention_policy(
    'ohlcv',
    INTERVAL '90 days',
    if_not_exists => TRUE
);

-- Index for fast symbol+timeframe queries
CREATE INDEX IF NOT EXISTS ix_ohlcv_symbol_tf_ts ON ohlcv (trading_symbol, timeframe, ts DESC);
"""


class HistoricalSeeder:
    """
    Seeds OHLCV data for all Nifty 50 stocks.
    Uses yfinance as the free fallback (daily data, good quality).
    Switches to Kite historical API when credentials are available.
    """

    def __init__(self, use_kite: bool = False):
        self._use_kite = use_kite and bool(settings.kite_api_key)

    async def create_hypertable(self) -> None:
        """Create the TimescaleDB ohlcv hypertable if it doesn't exist."""
        statements = [s.strip() for s in OHLCV_TABLE_DDL.split(";") if s.strip()]
        async with await get_db_session().__anext__() as session:
            for stmt in statements:
                await session.execute(text(stmt))
            await session.commit()
        log.info("historical_seed.hypertable", status="ready")

    async def seed_all(
        self,
        start_date: date | None = None,
        timeframes: list[str] | None = None,
    ) -> None:
        """
        Seed all Nifty 50 symbols.
        Default: 2 years of daily data.
        """
        if start_date is None:
            start_date = date.today() - timedelta(days=730)
        if timeframes is None:
            timeframes = ["1day"]   # Start with daily; intraday added after API key

        symbols = [sym for sym, _, _ in NIFTY50]
        log.info("historical_seed.start", symbols=len(symbols), from_date=start_date, timeframes=timeframes)

        for i, symbol in enumerate(symbols):
            try:
                await self._seed_symbol(symbol, start_date, timeframes)
                log.info("historical_seed.progress", symbol=symbol, done=i + 1, total=len(symbols))
            except Exception as e:
                log.error("historical_seed.symbol_error", symbol=symbol, error=str(e))
            await asyncio.sleep(0.5)   # Rate limit yfinance

        log.info("historical_seed.complete", symbols=len(symbols))

    async def _seed_symbol(
        self,
        symbol: str,
        start_date: date,
        timeframes: list[str],
    ) -> None:
        for tf in timeframes:
            if self._use_kite:
                df = await self._fetch_kite(symbol, start_date, tf)
            else:
                df = self._fetch_yfinance(symbol, start_date, tf)

            if df is None or df.empty:
                log.warning("historical_seed.no_data", symbol=symbol, timeframe=tf)
                return

            await self._upsert_candles(symbol, tf, df)

    # ── yfinance (free fallback) ──────────────────────────────────────────────

    def _fetch_yfinance(
        self, symbol: str, start_date: date, timeframe: str
    ) -> pd.DataFrame | None:
        """
        Fetch OHLCV from yfinance.
        yfinance uses Yahoo Finance symbols: NSE stocks are suffixed with .NS
        """
        yf_symbol = f"{symbol}.NS"
        interval  = self._tf_to_yfinance(timeframe)
        if interval is None:
            log.warning("yfinance.unsupported_tf", timeframe=timeframe)
            return None

        try:
            ticker = yf.Ticker(yf_symbol)
            df = ticker.history(
                start=start_date.strftime("%Y-%m-%d"),
                interval=interval,
                auto_adjust=True,
            )
            if df.empty:
                return None

            df.index = pd.to_datetime(df.index, utc=True)
            df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
            df = df.dropna()
            return df
        except Exception as e:
            log.error("yfinance.fetch_error", symbol=symbol, error=str(e))
            return None

    @staticmethod
    def _tf_to_yfinance(timeframe: str) -> str | None:
        mapping = {
            "1min":  "1m",
            "5min":  "5m",
            "15min": "15m",
            "1hr":   "1h",
            "1day":  "1d",
            "1week": "1wk",
        }
        return mapping.get(timeframe)

    # ── Kite (when API key available) ─────────────────────────────────────────

    async def _fetch_kite(
        self, symbol: str, start_date: date, timeframe: str
    ) -> pd.DataFrame | None:
        """
        Fetch OHLCV from Kite Connect historical API.
        Requires access_token to be set in Redis.
        """
        try:
            from kiteconnect import KiteConnect
            import json
            from database.connection import get_redis

            redis = get_redis()
            access_token = await redis.get("kite:access_token")
            token_map_raw = await redis.get("kite:token_map")

            if not access_token or not token_map_raw:
                log.warning("kite_seed.no_token", fallback="yfinance")
                return self._fetch_yfinance(symbol, start_date, timeframe)

            token_map = json.loads(token_map_raw)
            instrument_token = token_map.get(symbol)
            if not instrument_token:
                return self._fetch_yfinance(symbol, start_date, timeframe)

            kite = KiteConnect(api_key=settings.kite_api_key)
            kite.set_access_token(access_token)

            interval_map = {
                "1min":  "minute",
                "5min":  "5minute",
                "15min": "15minute",
                "1hr":   "60minute",
                "1day":  "day",
            }
            interval = interval_map.get(timeframe, "day")

            records = kite.historical_data(
                instrument_token=instrument_token,
                from_date=start_date,
                to_date=date.today(),
                interval=interval,
            )
            if not records:
                return None

            df = pd.DataFrame(records)
            df = df.set_index("date")
            df.index = pd.to_datetime(df.index, utc=True)
            return df[["open", "high", "low", "close", "volume"]]

        except Exception as e:
            log.error("kite_seed.fetch_error", symbol=symbol, error=str(e))
            return self._fetch_yfinance(symbol, start_date, timeframe)

    # ── Database upsert ───────────────────────────────────────────────────────

    async def _upsert_candles(
        self, symbol: str, timeframe: str, df: pd.DataFrame
    ) -> None:
        rows = [
            {
                "trading_symbol": symbol,
                "timeframe":      timeframe,
                "ts":             ts,
                "open":           float(row["open"]),
                "high":           float(row["high"]),
                "low":            float(row["low"]),
                "close":          float(row["close"]),
                "volume":         int(row["volume"]),
            }
            for ts, row in df.iterrows()
        ]

        if not rows:
            return

        upsert_sql = text("""
            INSERT INTO ohlcv (trading_symbol, timeframe, ts, open, high, low, close, volume)
            VALUES (:trading_symbol, :timeframe, :ts, :open, :high, :low, :close, :volume)
            ON CONFLICT (trading_symbol, timeframe, ts) DO UPDATE SET
                open   = EXCLUDED.open,
                high   = EXCLUDED.high,
                low    = EXCLUDED.low,
                close  = EXCLUDED.close,
                volume = EXCLUDED.volume
        """)

        async with await get_db_session().__anext__() as session:
            await session.execute(upsert_sql, rows)
            await session.commit()

        log.debug("historical_seed.upserted", symbol=symbol, tf=timeframe, rows=len(rows))


# ─── CLI entrypoint ───────────────────────────────────────────────────────────

async def main() -> None:
    from database.connection import close_db, init_db

    await init_db()
    seeder = HistoricalSeeder(use_kite=bool(settings.kite_api_key))
    await seeder.create_hypertable()
    await seeder.seed_all(timeframes=["1day"])
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
