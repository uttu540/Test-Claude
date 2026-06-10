"""
services/momentum_engine/watchlist.py
───────────────────────────────────────
RS-ranked incubator watchlist. Runs daily at 4:00 PM after market close.

Philosophy (from @darvasboxtrader):
  "I don't find stocks from screeners. I keep good strength stocks in
   incubator first, before actual buy."

What this does:
  1. Rank all Nifty500 symbols by 20-day Relative Strength vs Nifty
  2. Apply additional quality filters (52wk high proximity, above 200 EMA,
     minimum volume, not overextended)
  3. Persist top-N symbols to Redis as the "watchlist"
  4. Momentum live engine only runs Darvas detection on watchlist symbols
  5. Rolling 4-week RS history tracked — stock needs 2+ weeks in top-N
     to qualify (avoids one-day flukes)

Redis keys:
  momentum:watchlist          → JSON list of symbols currently in watchlist
  momentum:watchlist:rs:{sym} → rolling RS score history (last 4 weeks)
  momentum:watchlist:updated  → last update timestamp

Why RS filter works:
  In a flat Nifty, 10-15 stocks are always outperforming — defense, capex,
  exports, pharma rotation. These are the ones worth watching for Darvas boxes.
  Scanning 2000 stocks blind finds noise. Watching 50 RS leaders finds signal.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import structlog
import yfinance as yf

from database.connection import get_redis

log = structlog.get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ── Parameters ────────────────────────────────────────────────────────────────

WATCHLIST_SIZE      = 50    # top-N by RS to keep in watchlist
RS_LOOKBACK_DAYS    = 20    # RS = stock 20d ROC vs Nifty 20d ROC
MIN_WEEKS_IN_TOP    = 1     # weeks stock must be in top-N before qualifying
                            # (1 = same week ok, 2 = must appear 2 weeks)
MAX_FROM_52W_HIGH   = 0.25  # max 25% below 52-week high (v2: wider incubator)
MIN_PRICE           = 100   # min stock price
MIN_AVG_VOL         = 200_000  # min 20-day avg daily volume
MAX_PRETREND        = 0.50  # skip if up >50% in prior 60 days (overbought)
NIFTY_TICKER        = "^NSEI"

REDIS_WATCHLIST_KEY = "momentum:watchlist"
REDIS_RS_PREFIX     = "momentum:watchlist:rs"
REDIS_UPDATED_KEY   = "momentum:watchlist:updated"
REDIS_TTL           = 7 * 86400   # 7 days


class WatchlistBuilder:
    """
    Builds and persists the RS-ranked incubator watchlist.
    Called once daily at market close.
    """

    def __init__(self, top_n: int = WATCHLIST_SIZE):
        self.top_n = top_n

    async def update(self, symbols: list[str]) -> list[str]:
        """
        Fetch daily data, compute RS, filter, persist to Redis.
        Returns the new watchlist.
        """
        log.info("watchlist.update_start", symbols=len(symbols))

        # Fetch ~100 days of daily data (60 for pretrend + 40 buffer)
        fetch_start = (date.today() - timedelta(days=120)).strftime("%Y-%m-%d")
        fetch_end   = date.today().strftime("%Y-%m-%d")

        # Fetch Nifty for RS denominator
        nifty_df = self._fetch_daily(NIFTY_TICKER, fetch_start, fetch_end)
        if nifty_df is None or len(nifty_df) < RS_LOOKBACK_DAYS + 5:
            log.error("watchlist.nifty_fetch_failed")
            return []

        nifty_roc20 = self._roc(nifty_df, RS_LOOKBACK_DAYS)

        # Fetch all symbols in batches
        all_data = self._fetch_batch(symbols, fetch_start, fetch_end)
        log.info("watchlist.data_loaded", loaded=len(all_data))

        # Score each symbol
        scored: list[tuple[str, float]] = []
        for sym, df in all_data.items():
            score = self._score(sym, df, nifty_roc20)
            if score is not None:
                scored.append((sym, score))

        # Sort by RS score descending
        scored.sort(key=lambda x: -x[1])
        top = [sym for sym, _ in scored[:self.top_n]]

        log.info("watchlist.scored",
                 total_scored=len(scored),
                 top_n=len(top),
                 top5=[s for s, _ in scored[:5]])

        # Persist to Redis with rolling history
        redis = get_redis()
        today_str = date.today().isoformat()

        # Update rolling RS history for each symbol in top
        for sym, rs_score in scored[:self.top_n * 2]:  # store 2× top_n for history
            key = f"{REDIS_RS_PREFIX}:{sym}"
            raw = await redis.get(key)
            history: dict = json.loads(raw) if raw else {}
            history[today_str] = round(rs_score, 4)
            # Keep only last 28 days
            cutoff = (date.today() - timedelta(days=28)).isoformat()
            history = {d: v for d, v in history.items() if d >= cutoff}
            await redis.setex(key, REDIS_TTL, json.dumps(history))

        # Apply consistency filter: must appear in top-N for MIN_WEEKS_IN_TOP weeks
        if MIN_WEEKS_IN_TOP > 1:
            top = await self._apply_consistency_filter(top, redis)

        # Write final watchlist
        await redis.setex(REDIS_WATCHLIST_KEY, REDIS_TTL, json.dumps(top))
        await redis.setex(REDIS_UPDATED_KEY, REDIS_TTL,
                          datetime.now(IST).isoformat())

        log.info("watchlist.updated", count=len(top), symbols=top[:10])
        return top

    async def _apply_consistency_filter(
        self, candidates: list[str], redis
    ) -> list[str]:
        """Keep only symbols that appeared in top watchlist for MIN_WEEKS_IN_TOP+ distinct weeks."""
        qualified = []
        cutoff = (date.today() - timedelta(weeks=MIN_WEEKS_IN_TOP)).isoformat()

        for sym in candidates:
            key = f"{REDIS_RS_PREFIX}:{sym}"
            raw = await redis.get(key)
            if not raw:
                continue
            history: dict = json.loads(raw)
            # Count distinct ISO weeks in history
            weeks = set()
            for d_str in history:
                if d_str >= cutoff:
                    try:
                        d = date.fromisoformat(d_str)
                        weeks.add(d.isocalendar()[1])  # ISO week number
                    except Exception:
                        pass
            if len(weeks) >= MIN_WEEKS_IN_TOP:
                qualified.append(sym)

        log.info("watchlist.consistency_filter",
                 candidates=len(candidates), qualified=len(qualified),
                 min_weeks=MIN_WEEKS_IN_TOP)
        return qualified

    def _score(
        self, sym: str, df: pd.DataFrame, nifty_roc20: float
    ) -> Optional[float]:
        """
        Return RS score for symbol, or None if it fails quality filters.
        RS score = stock_roc20 - nifty_roc20 (higher = stronger vs market).
        """
        if df is None or len(df) < RS_LOOKBACK_DAYS + 5:
            return None

        try:
            close  = df["close"] if "close" in df.columns else df["Close"]
            high   = df["high"]  if "high"  in df.columns else df["High"]
            volume = df["volume"] if "volume" in df.columns else df["Volume"]

            last_close  = float(close.iloc[-1])
            last_volume = float(volume.iloc[-20:].mean())

            # Price gate
            if last_close < MIN_PRICE:
                return None

            # Volume gate
            if last_volume < MIN_AVG_VOL:
                return None

            # 52-week high proximity
            high_52w = float(high.iloc[-min(252, len(df)):].max())
            if high_52w > 0 and (high_52w - last_close) / high_52w > MAX_FROM_52W_HIGH:
                return None  # too far from 52-week high

            # Overextension check (up >50% in 60 days = buy-the-rumour risk)
            if len(close) >= 60:
                base = float(close.iloc[-60])
                if base > 0 and (last_close - base) / base > MAX_PRETREND:
                    return None

            # RS score
            stock_roc20 = self._roc(df, RS_LOOKBACK_DAYS)
            rs = stock_roc20 - nifty_roc20
            return rs

        except Exception as e:
            log.debug("watchlist.score_error", symbol=sym, error=str(e))
            return None

    @staticmethod
    def _roc(df: pd.DataFrame, period: int) -> float:
        """Return % ROC over `period` bars."""
        close = df["close"] if "close" in df.columns else df["Close"]
        if len(close) < period + 1:
            return 0.0
        base  = float(close.iloc[-(period + 1)])
        last  = float(close.iloc[-1])
        return (last - base) / base * 100 if base > 0 else 0.0

    def _fetch_daily(
        self, ticker: str, start: str, end: str
    ) -> Optional[pd.DataFrame]:
        try:
            df = yf.download(ticker, start=start, end=end,
                             interval="1d", progress=False, auto_adjust=True)
            if df is None or df.empty:
                return None
            df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower()
                          for c in df.columns]
            return df.dropna(subset=["close"])
        except Exception as e:
            log.warning("watchlist.fetch_error", ticker=ticker, error=str(e))
            return None

    def _fetch_batch(
        self, symbols: list[str], start: str, end: str
    ) -> dict[str, pd.DataFrame]:
        result: dict[str, pd.DataFrame] = {}
        tickers = [f"{s}.NS" for s in symbols]
        batch_size = 100

        for i in range(0, len(tickers), batch_size):
            batch_tickers = tickers[i:i + batch_size]
            batch_symbols = symbols[i:i + batch_size]
            try:
                raw = yf.download(
                    batch_tickers, start=start, end=end,
                    interval="1d", progress=False,
                    auto_adjust=True, group_by="ticker",
                )
                for sym, ticker in zip(batch_symbols, batch_tickers):
                    try:
                        df = raw[ticker].copy() if len(batch_tickers) > 1 else raw.copy()
                        df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower()
                                      for c in df.columns]
                        df = df.dropna(subset=["close"])
                        if not df.empty:
                            result[sym] = df
                    except Exception:
                        pass
            except Exception as e:
                log.warning("watchlist.batch_error", batch=i, error=str(e))

        return result


# ── Public helpers ─────────────────────────────────────────────────────────────

async def get_watchlist() -> list[str]:
    """Return current watchlist from Redis. Empty list = watchlist not built yet."""
    redis = get_redis()
    raw = await redis.get(REDIS_WATCHLIST_KEY)
    if not raw:
        return []
    return json.loads(raw)


async def is_in_watchlist(symbol: str) -> bool:
    watchlist = await get_watchlist()
    return symbol in watchlist


async def get_watchlist_meta() -> dict:
    """Return watchlist metadata for logging/dashboard."""
    redis = get_redis()
    watchlist = await get_watchlist()
    updated = await redis.get(REDIS_UPDATED_KEY)
    return {
        "count":   len(watchlist),
        "updated": updated or "never",
        "symbols": watchlist,
    }
