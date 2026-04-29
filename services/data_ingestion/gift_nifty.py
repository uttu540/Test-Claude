"""
services/data_ingestion/gift_nifty.py
──────────────────────────────────────
Fetches GIFT Nifty (SGX Nifty) pre-market data as a directional cue
for the market regime detector.

GIFT Nifty trades on NSE IFSC (GIFT City, Gujarat) and reflects global
overnight sentiment before Indian markets open at 9:15 AM IST.

Data source: Yahoo Finance via yfinance.
  Ticker: "^NIFTYFUTURES" — not always reliable.
  Fallback: Compare current ^NSEI spot to previous close using pre-open
            session data (available 9:00–9:15 AM IST on NSE).
  Second fallback: Use Dow Jones / S&P 500 futures overnight change as
                   a global sentiment proxy.

Returns: float (% change from previous close) or None if unavailable.
  Positive = gap up (bullish)
  Negative = gap down (bearish)
"""
from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)

# Tickers tried in order until one succeeds
_GIFT_NIFTY_TICKERS = [
    "^NIFTYFUTURES",   # GIFT Nifty futures (may not be on Yahoo Finance)
    "^NSEI",           # Nifty 50 spot — pre-open session shows indicative price
]

# Global proxy tickers used as fallback when Indian futures unavailable
_GLOBAL_PROXY_TICKERS = [
    "ES=F",   # S&P 500 E-mini futures
    "NQ=F",   # Nasdaq futures
]

# Weight of each global proxy in combined sentiment
_GLOBAL_PROXY_WEIGHTS = {
    "ES=F": 0.6,
    "NQ=F": 0.4,
}


async def fetch_gift_nifty_change() -> float | None:
    """
    Return estimated GIFT Nifty % change from previous close.
    Tries GIFT/SGX Nifty tickers first, falls back to global proxies.
    Returns None if all sources fail.
    """
    import asyncio

    # Run in a thread — yfinance is synchronous
    return await asyncio.get_event_loop().run_in_executor(None, _fetch_sync)


def _fetch_sync() -> float | None:
    try:
        import yfinance as yf

        # ── Try GIFT Nifty / Nifty spot ───────────────────────────────────────
        for ticker in _GIFT_NIFTY_TICKERS:
            try:
                t = yf.Ticker(ticker)
                hist = t.history(period="2d", interval="1d", auto_adjust=True)
                if hist is None or len(hist) < 2:
                    continue
                prev_close = float(hist["Close"].iloc[-2])
                last_price = float(hist["Close"].iloc[-1])
                if prev_close <= 0:
                    continue
                pct = (last_price - prev_close) / prev_close * 100
                log.info("gift_nifty.fetched", ticker=ticker, pct=round(pct, 2))
                return round(pct, 2)
            except Exception as e:
                log.debug("gift_nifty.ticker_failed", ticker=ticker, error=str(e))
                continue

        # ── Fallback: weighted average of global futures ───────────────────────
        weighted_pct = 0.0
        total_weight = 0.0
        for ticker, weight in _GLOBAL_PROXY_WEIGHTS.items():
            try:
                t = yf.Ticker(ticker)
                hist = t.history(period="2d", interval="1d", auto_adjust=True)
                if hist is None or len(hist) < 2:
                    continue
                prev_close = float(hist["Close"].iloc[-2])
                last_price = float(hist["Close"].iloc[-1])
                if prev_close <= 0:
                    continue
                pct = (last_price - prev_close) / prev_close * 100
                weighted_pct += pct * weight
                total_weight  += weight
                log.debug("gift_nifty.proxy_fetched", ticker=ticker, pct=round(pct, 2))
            except Exception as e:
                log.debug("gift_nifty.proxy_failed", ticker=ticker, error=str(e))

        if total_weight > 0:
            result = round(weighted_pct / total_weight, 2)
            log.info("gift_nifty.proxy_used", pct=result, tickers=list(_GLOBAL_PROXY_WEIGHTS.keys()))
            return result

    except Exception as e:
        log.warning("gift_nifty.fetch_failed", error=str(e))

    return None


async def fetch_market_news_sentiment(hours: int = 12) -> float | None:
    """
    Return aggregate news sentiment for market-wide news in [-1.0, +1.0].
    Reads from NewsItem table — scores populated by news_feed.py polling.
    Returns None if no recent news found.

    Score interpretation:
      > +0.3  → positive sentiment (supports bullish regime)
      < -0.3  → negative sentiment (supports bearish / high-vol regime)
      [-0.3, +0.3] → neutral
    """
    try:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        from sqlalchemy import text
        from database.connection import get_db_session

        ist   = ZoneInfo("Asia/Kolkata")
        since = datetime.now(ist) - timedelta(hours=hours)

        async with get_db_session() as session:
            result = await session.execute(
                text("""
                    SELECT AVG(sentiment_score), COUNT(*)
                    FROM news_items
                    WHERE published_at >= :since
                      AND sentiment_score IS NOT NULL
                """),
                {"since": since},
            )
            row = result.fetchone()
            if row and row[1] and int(row[1]) >= 3:   # need at least 3 articles
                score = float(row[0])
                log.info("news_sentiment.fetched", score=round(score, 3), articles=row[1])
                return round(score, 3)
    except Exception as e:
        log.warning("news_sentiment.fetch_failed", error=str(e))

    return None


async def fetch_india_vix() -> float | None:
    """Fetch India VIX current level from Yahoo Finance (^INDIAVIX)."""
    import asyncio
    return await asyncio.get_event_loop().run_in_executor(None, _fetch_vix_sync)


def _fetch_vix_sync() -> float | None:
    try:
        import yfinance as yf
        t = yf.Ticker("^INDIAVIX")
        hist = t.history(period="2d", interval="1d", auto_adjust=True)
        if hist is not None and len(hist) >= 1:
            vix = float(hist["Close"].iloc[-1])
            log.info("india_vix.fetched", vix=round(vix, 2))
            return round(vix, 2)
    except Exception as e:
        log.warning("india_vix.fetch_failed", error=str(e))
    return None
