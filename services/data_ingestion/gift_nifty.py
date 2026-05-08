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


_NSE_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/",
    "Connection":      "keep-alive",
}


def _nse_get(path: str, retries: int = 3) -> dict | list | None:
    """
    GET an NSE API endpoint with browser session + retry.
    Hits the homepage first to obtain session cookies (NSE requires this).
    Returns parsed JSON or None on all failures.
    """
    import httpx
    import time

    for attempt in range(retries):
        try:
            with httpx.Client(
                headers=_NSE_BROWSER_HEADERS,
                follow_redirects=True,
                timeout=15,
            ) as client:
                client.get("https://www.nseindia.com")
                time.sleep(0.4)
                resp = client.get(f"https://www.nseindia.com{path}")

            if resp.status_code == 200:
                return resp.json()

            log.debug("nse_api.non_200",
                      path=path, status=resp.status_code, attempt=attempt + 1)
        except Exception as e:
            log.debug("nse_api.request_error",
                      path=path, attempt=attempt + 1, error=str(e))

        if attempt < retries - 1:
            time.sleep(2 ** attempt)   # 1s, 2s back-off

    log.warning("nse_api.all_attempts_failed", path=path)
    return None


async def fetch_fii_data() -> str | None:
    """
    Fetch previous day's FII/DII net equity activity from NSE.
    Returns: "FII: Net Sell ₹341cr | DII: Net Buy ₹441cr" or None.
    Cached in Redis for 2h — data doesn't change during the trading day.
    """
    import asyncio

    # Try Redis cache first
    try:
        from database.connection import get_redis
        redis = get_redis()
        cached = await redis.get("briefing:fii_data")
        if cached:
            return cached
    except Exception:
        pass

    result = await asyncio.get_running_loop().run_in_executor(None, _fetch_fii_sync)

    if result:
        try:
            await redis.setex("briefing:fii_data", 2 * 3600, result)
        except Exception:
            pass

    return result


def _fetch_fii_sync() -> str | None:
    data = _nse_get("/api/fiidiiTradeReact")
    if not data:
        return None
    try:
        parts = []
        for item in data:
            cat = item.get("category", "")
            net = float(item.get("netValue", 0))
            label = "FII" if "FII" in cat.upper() else "DII"
            sign  = "Net Buy" if net >= 0 else "Net Sell"
            parts.append(f"{label}: {sign} ₹{abs(net):.0f}cr")
        result = " | ".join(parts) if parts else None
        log.info("fii_data.fetched", result=result)
        return result
    except Exception as e:
        log.warning("fii_data.parse_failed", error=str(e))
        return None


async def fetch_advance_decline() -> str | None:
    """
    Fetch Nifty 50 advance-decline ratio from NSE.
    Returns: "14A / 36D / 0U" or None.
    Cached in Redis for 10min — refreshes each briefing cycle.
    """
    import asyncio

    try:
        from database.connection import get_redis
        redis = get_redis()
        cached = await redis.get("briefing:adv_dec")
        if cached:
            return cached
    except Exception:
        pass

    result = await asyncio.get_running_loop().run_in_executor(None, _fetch_adv_dec_sync)

    if result:
        try:
            await redis.setex("briefing:adv_dec", 10 * 60, result)
        except Exception:
            pass

    return result


def _fetch_adv_dec_sync() -> str | None:
    data = _nse_get("/api/allIndices")
    if not data:
        return None
    try:
        for item in data.get("data", []):
            if item.get("index", "") == "NIFTY 50":
                adv = item.get("advances", "?")
                dec = item.get("declines", "?")
                unc = item.get("unchanged", "0")
                result = f"{adv}A / {dec}D / {unc}U"
                log.info("advance_decline.fetched", result=result)
                return result
    except Exception as e:
        log.warning("advance_decline.parse_failed", error=str(e))
    return None
