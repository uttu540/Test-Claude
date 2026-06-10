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
from services.data_ingestion.nifty500_instruments import NIFTY500

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
        """Create the ohlcv table. Uses TimescaleDB hypertable if available, plain table otherwise."""
        statements = [s.strip() for s in OHLCV_TABLE_DDL.split(";") if s.strip()]
        async with get_db_session() as session:
            for stmt in statements:
                try:
                    await session.execute(text(stmt))
                    await session.commit()
                except Exception as e:
                    await session.rollback()
                    if "create_hypertable" in stmt or "add_retention_policy" in stmt:
                        log.warning("historical_seed.timescale_unavailable",
                                    msg="TimescaleDB not installed — using plain Postgres table",
                                    error=str(e))
                    else:
                        raise
        log.info("historical_seed.hypertable", status="ready")

    async def seed_all(
        self,
        start_date: date | None = None,
        timeframes: list[str] | None = None,
        batch_size: int = 50,
        workers: int = 6,
    ) -> None:
        """
        Seed all NSE symbols — optimised for speed.

        Optimisations vs naive loop:
          • Skip symbols that already have data within last 2 days (no re-seed needed)
          • Batch yf.download() for up to `batch_size` symbols per HTTP request
          • `workers` concurrent download threads (yfinance is blocking I/O)
          • No per-symbol asyncio.sleep (was: 0.5s × 2126 = 17 min of pure sleep)
          • Single DB upsert per batch (not per symbol)

        Net result: cold seed ~3-5 min (vs 20+ min), warm restart ~10-30 sec.
        """
        if start_date is None:
            start_date = date.today() - timedelta(days=730)
        if timeframes is None:
            timeframes = ["1day"]

        symbols = await self._get_universe()
        index_symbols = [("NIFTY 50", "^NSEI")]

        log.warning(
            "historical_seed.survivorship_bias",
            note="Seeding TODAY's universe — delisted/merged stocks absent from history",
            symbols=len(symbols),
            from_date=str(start_date),
        )

        # ── Step 1: find which symbols need seeding ───────────────────────────
        # Skip any symbol whose latest candle is within 2 calendar days of today
        # (already up-to-date — e.g. daily restart after previous successful seed).
        stale_cutoff = date.today() - timedelta(days=2)
        need_seed = await self._filter_stale(symbols, "1day", stale_cutoff)
        log.info(
            "historical_seed.start",
            total=len(symbols),
            need_seed=len(need_seed),
            skipped=len(symbols) - len(need_seed),
            from_date=start_date,
            timeframes=timeframes,
        )

        if not need_seed:
            log.info("historical_seed.all_fresh", msg="All symbols up-to-date — skipping seed")
        else:
            # ── Step 2: batch download + upsert ──────────────────────────────
            chunks = [need_seed[i: i + batch_size] for i in range(0, len(need_seed), batch_size)]
            loop   = asyncio.get_running_loop()

            semaphore = asyncio.Semaphore(workers)

            async def _process_batch(batch: list[str], batch_idx: int) -> int:
                async with semaphore:
                    yf_tickers = [f"{s}.NS" for s in batch]
                    try:
                        df_raw = await loop.run_in_executor(
                            None,
                            lambda t=yf_tickers: yf.download(
                                t,
                                start=start_date.strftime("%Y-%m-%d"),
                                interval="1d",
                                auto_adjust=True,
                                progress=False,
                                threads=True,
                                group_by="ticker",
                            ),
                        )
                    except Exception as e:
                        log.warning("historical_seed.batch_download_error", batch=batch_idx, error=str(e))
                        return 0

                    if df_raw is None or df_raw.empty:
                        return 0

                    # Parse multi-symbol response and upsert per symbol
                    upserted  = 0
                    failed    = []   # symbols missing from batch result → retry individually
                    top_level = df_raw.columns.get_level_values(0) if hasattr(df_raw.columns, "get_level_values") else []

                    for sym, yf_ticker in zip(batch, yf_tickers):
                        try:
                            if len(batch) == 1:
                                sym_df = df_raw
                            else:
                                sym_df = df_raw[yf_ticker] if yf_ticker in top_level else None
                            if sym_df is None or sym_df.empty:
                                failed.append((sym, yf_ticker))
                                continue
                            sym_df = sym_df.rename(columns=str.lower)
                            cols = [c for c in ["open", "high", "low", "close", "volume"] if c in sym_df.columns]
                            sym_df = sym_df[cols].dropna()
                            if sym_df.empty:
                                failed.append((sym, yf_ticker))
                                continue
                            sym_df.index = pd.to_datetime(sym_df.index, utc=True)
                            for tf in timeframes:
                                await self._upsert_candles(sym, tf, sym_df)
                            upserted += 1
                        except Exception as sym_err:
                            log.debug("historical_seed.symbol_parse_error", symbol=sym, error=str(sym_err))
                            failed.append((sym, yf_ticker))

                    # Retry failed symbols individually (batch timeout can drop valid symbols)
                    for sym, yf_ticker in failed:
                        try:
                            sym_df = await loop.run_in_executor(
                                None,
                                lambda t=yf_ticker: yf.Ticker(t).history(
                                    start=start_date.strftime("%Y-%m-%d"),
                                    interval="1d",
                                    auto_adjust=True,
                                    timeout=15,
                                ),
                            )
                            if sym_df is None or sym_df.empty:
                                continue
                            sym_df = sym_df.rename(columns=str.lower)
                            cols = [c for c in ["open", "high", "low", "close", "volume"] if c in sym_df.columns]
                            sym_df = sym_df[cols].dropna()
                            if sym_df.empty:
                                continue
                            sym_df.index = pd.to_datetime(sym_df.index, utc=True)
                            for tf in timeframes:
                                await self._upsert_candles(sym, tf, sym_df)
                            upserted += 1
                            log.debug("historical_seed.retry_ok", symbol=sym)
                        except Exception:
                            pass   # truly delisted — ignore silently

                    log.info(
                        "historical_seed.batch_done",
                        batch=batch_idx + 1,
                        total_batches=len(chunks),
                        upserted=upserted,
                        batch_size=len(batch),
                        retried=len(failed),
                    )
                    return upserted

            tasks = [_process_batch(chunk, i) for i, chunk in enumerate(chunks)]
            results = await asyncio.gather(*tasks)
            total_upserted = sum(results)
            log.info("historical_seed.equity_done", upserted=total_upserted, batches=len(chunks))

        # ── Step 3: indices (small list, direct fetch) ────────────────────────
        for trading_symbol, yf_ticker in index_symbols:
            try:
                df = self._fetch_yfinance_raw(yf_ticker, start_date, "1day")
                if df is not None and not df.empty:
                    await self._upsert_candles(trading_symbol, "1day", df)
                    log.info("historical_seed.index_seeded", symbol=trading_symbol, rows=len(df))
            except Exception as e:
                log.error("historical_seed.index_error", symbol=trading_symbol, error=str(e))

        log.info("historical_seed.complete", symbols=len(symbols) + len(index_symbols))

    async def _filter_stale(
        self, symbols: list[str], timeframe: str, cutoff: date
    ) -> list[str]:
        """
        Return only symbols whose latest candle in DB is older than `cutoff`.
        Symbols with no data at all are also returned (need initial seed).
        Done in one query — fast even for 2000+ symbols.
        """
        try:
            async with get_db_session() as session:
                result = await session.execute(
                    text("""
                        SELECT trading_symbol, MAX(ts)::date AS latest
                        FROM ohlcv
                        WHERE timeframe = :tf
                          AND trading_symbol = ANY(:syms)
                        GROUP BY trading_symbol
                    """),
                    {"tf": timeframe, "syms": symbols},
                )
                fresh = {row.trading_symbol for row in result if row.latest and row.latest >= cutoff}
            return [s for s in symbols if s not in fresh]
        except Exception as e:
            log.warning("historical_seed.filter_stale_error", error=str(e))
            return symbols  # fallback: seed everything

    async def _seed_symbol(
        self,
        symbol: str,
        start_date: date,
        timeframes: list[str],
    ) -> None:
        for tf in timeframes:
            # Always use yfinance for daily historical seeding — Kite historical API
            # has a daily request quota that gets exhausted when bulk-seeding 2000+ symbols.
            # Kite is reserved for live intraday data only.
            df = self._fetch_yfinance(symbol, start_date, tf)

            if df is None or df.empty:
                log.warning("historical_seed.no_data", symbol=symbol, timeframe=tf)
                return

            await self._upsert_candles(symbol, tf, df)

    # ── Universe ──────────────────────────────────────────────────────────────

    async def _get_universe(self) -> list[str]:
        """
        Return NSE EQ trading universe (~2160 stocks).
        Uses get_live_universe() which is already filtered to equity-only.
        Falls back to Nifty 500 if unavailable.
        """
        try:
            from services.data_ingestion.nifty500_instruments import get_live_universe
            symbols = get_live_universe()
            if symbols:
                log.info("historical_seed.universe", source="live_universe", count=len(symbols))
                return symbols
        except Exception as e:
            log.warning("historical_seed.universe_fallback", error=str(e))
        symbols = [sym for sym, _, _ in NIFTY500]
        log.info("historical_seed.universe", source="nifty500_fallback", count=len(symbols))
        return symbols

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
                timeout=10,
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

    def _fetch_yfinance_raw(
        self, yf_ticker: str, start_date: date, timeframe: str
    ) -> pd.DataFrame | None:
        """Fetch OHLCV using an exact yfinance ticker (no .NS suffix added)."""
        interval = self._tf_to_yfinance(timeframe)
        if interval is None:
            return None
        try:
            ticker = yf.Ticker(yf_ticker)
            df = ticker.history(
                start=start_date.strftime("%Y-%m-%d"),
                interval=interval,
                auto_adjust=True,
            )
            if df.empty:
                return None
            df.index = pd.to_datetime(df.index, utc=True)
            df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
            return df.dropna()
        except Exception as e:
            log.error("yfinance.raw_fetch_error", ticker=yf_ticker, error=str(e))
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

        async with get_db_session() as session:
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
