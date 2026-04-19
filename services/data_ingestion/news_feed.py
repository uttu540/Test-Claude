"""
services/data_ingestion/news_feed.py
──────────────────────────────────────
News ingestion pipeline for Nifty 50 stocks.

Polls NewsAPI every 15 minutes for headlines related to each symbol.
Stores articles in the NewsItem table (deduped by URL).
Exposes get_recent_news() for the AI strategy engine to consume.

Free NewsAPI tier: 100 requests/day.
Strategy: batch all 50 symbols into ~10 queries using OR operators,
so we stay within limits even with 15-min polling.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import structlog
from sqlalchemy import select, text

# Lazy VADER import — installed via: pip install vaderSentiment
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer as _VaderAnalyzer
    _vader = _VaderAnalyzer()
except ImportError:
    _vader = None

from config.settings import settings
from database.connection import get_db_session
from database.models import NewsItem
from services.data_ingestion.nifty500_instruments import NIFTY500

log = structlog.get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Map trading symbol → company name keywords for better news search
SYMBOL_KEYWORDS: dict[str, str] = {sym: name for sym, name, _ in NIFTY500}

# Batch size: how many symbols per NewsAPI query
# Free tier = 100 req/day. 50 symbols ÷ 25 = 2 batches/cycle.
# At 60-min interval: 2 × 24 = 48 req/day — well within limit.
QUERY_BATCH_SIZE = 25

# NewsAPI base URL
NEWSAPI_URL = "https://newsapi.org/v2/everything"


class NewsFeedService:
    """
    Polls NewsAPI on a configurable interval and stores articles
    in the NewsItem table, deduplicated by URL.
    """

    def __init__(self, poll_interval_minutes: int = 60):
        self._interval = poll_interval_minutes * 60
        self._running  = False
        self._client: httpx.AsyncClient | None = None
        self._enabled  = bool(settings.news_api_key)

        if not self._enabled:
            log.warning("news_feed.disabled", reason="NEWS_API_KEY not set in .env")

    async def start(self) -> None:
        """Start the polling loop as a background task."""
        if not self._enabled:
            return
        self._running = True
        self._client  = httpx.AsyncClient(timeout=30)
        log.info("news_feed.started", interval_min=self._interval // 60)
        asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False
        if self._client:
            await self._client.aclose()
        log.info("news_feed.stopped")

    # ── Polling loop ──────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._fetch_and_store_all()
            except Exception as e:
                log.error("news_feed.poll_error", error=str(e))
            await asyncio.sleep(self._interval)

    async def _fetch_and_store_all(self) -> None:
        """Fetch news for all Nifty 50 symbols in batches."""
        symbols = list(SYMBOL_KEYWORDS.keys())

        # Batch into groups to reduce API calls
        batches = [
            symbols[i: i + QUERY_BATCH_SIZE]
            for i in range(0, len(symbols), QUERY_BATCH_SIZE)
        ]

        total_stored = 0
        for batch in batches:
            stored = await self._fetch_batch(batch)
            total_stored += stored
            await asyncio.sleep(1)   # Small delay between API calls

        log.info("news_feed.cycle_complete", articles_stored=total_stored)

    async def _fetch_batch(self, symbols: list[str]) -> int:
        """
        Fetch news for a batch of symbols.
        Builds an OR query from company names for better recall.
        Returns number of new articles stored.
        """
        # Build query: "Reliance Industries" OR "HDFC Bank" OR ...
        query_terms = [
            f'"{SYMBOL_KEYWORDS[sym]}"' for sym in symbols
            if sym in SYMBOL_KEYWORDS
        ]
        query = " OR ".join(query_terms)

        # Only fetch articles from the last 24 hours
        from_dt = (datetime.now(IST) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")

        try:
            resp = await self._client.get(
                NEWSAPI_URL,
                params={
                    "q":        query,
                    "from":     from_dt,
                    "language": "en",
                    "sortBy":   "publishedAt",
                    "pageSize": 20,
                    "apiKey":   settings.news_api_key,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                log.warning("news_feed.rate_limited", retry_after="15min")
            else:
                log.error("news_feed.http_error", status=e.response.status_code, error=str(e))
            return 0
        except Exception as e:
            log.error("news_feed.fetch_error", error=str(e))
            return 0

        articles = data.get("articles", [])
        if not articles:
            return 0

        stored = 0
        for article in articles:
            # Match article to a symbol
            matched_symbol = self._match_symbol(
                article.get("title", "") + " " + article.get("description", ""),
                symbols,
            )
            if await self._store_article(article, matched_symbol):
                stored += 1

        return stored

    def _match_symbol(self, text: str, symbols: list[str]) -> str | None:
        """
        Find which symbol the article is about by checking
        if the company name appears in the article text.
        Returns the first match, or None for market-wide news.
        """
        text_lower = text.lower()
        for sym in symbols:
            company = SYMBOL_KEYWORDS.get(sym, "")
            # Check both company name and trading symbol
            if company.lower() in text_lower or sym.lower() in text_lower:
                return sym
        return None   # Market-wide news

    @staticmethod
    def _score_sentiment(headline: str, description: str) -> tuple[float | None, str | None]:
        """
        Return (score, label) using VADER compound score on headline + description.
        Score is in [-1.0, +1.0].  Returns (None, None) if VADER unavailable.
        """
        if _vader is None:
            return None, None

        text = f"{headline}. {description}".strip(". ")
        compound = _vader.polarity_scores(text)["compound"]
        score = round(compound, 2)

        if score >= 0.05:
            label = "BULLISH"
        elif score <= -0.05:
            label = "BEARISH"
        else:
            label = "NEUTRAL"

        return score, label

    async def _store_article(self, article: dict, symbol: str | None) -> bool:
        """
        Store a single article. Returns True if new, False if duplicate.
        Deduplicates by URL.  Scores sentiment inline via VADER.
        """
        url       = article.get("url") or ""
        headline  = article.get("title") or ""
        if not headline or not url:
            return False

        description = article.get("description") or ""

        # Parse published_at
        published_at = None
        raw_date = article.get("publishedAt")
        if raw_date:
            try:
                published_at = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            except ValueError:
                pass

        sentiment_score, sentiment_label = self._score_sentiment(headline, description)

        try:
            async with get_db_session() as session:
                # Check for duplicate
                exists = await session.execute(
                    select(NewsItem.id).where(NewsItem.url == url)
                )
                if exists.scalar():
                    return False

                item = NewsItem(
                    trading_symbol  = symbol,
                    headline        = headline[:500],
                    summary         = description[:1000],
                    url             = url[:1000],
                    source          = (article.get("source") or {}).get("name", "")[:100],
                    published_at    = published_at,
                    sentiment_score = sentiment_score,
                    sentiment_label = sentiment_label,
                    processed       = sentiment_score is not None,   # True once scored
                )
                try:
                    session.add(item)
                    await session.commit()
                    return True
                except Exception:
                    await session.rollback()
                    raise
        except Exception as e:
            log.error("news_feed.store_error", headline=headline[:60], error=str(e))
            return False

    # ── Query interface for AI strategy engine ────────────────────────────────

    async def get_recent_news(
        self,
        symbol: str,
        hours: int = 4,
    ) -> list[dict]:
        """
        Return recent news articles for a symbol within the given time window.
        Used by ClaudeStrategyClient to build signal context.

        Returns a list of dicts with: headline, source, published_at, sentiment_score.
        """
        since = datetime.now(IST) - timedelta(hours=hours)

        try:
            async with get_db_session() as session:
                result = await session.execute(
                    text("""
                        SELECT headline, source, published_at, sentiment_score
                        FROM news_items
                        WHERE (trading_symbol = :sym OR trading_symbol IS NULL)
                          AND published_at >= :since
                        ORDER BY published_at DESC
                        LIMIT 10
                    """),
                    {"sym": symbol, "since": since},
                )
                rows = result.fetchall()
                return [
                    {
                        "headline":    r.headline,
                        "source":      r.source or "",
                        "published":   r.published_at.isoformat() if r.published_at else "",
                        "sentiment":   float(r.sentiment_score or 0.0),
                    }
                    for r in rows
                ]
        except Exception as e:
            log.error("news_feed.query_error", symbol=symbol, error=str(e))
            return []


# ─── Singleton ────────────────────────────────────────────────────────────────

_news_service: NewsFeedService | None = None


def get_news_service() -> NewsFeedService:
    global _news_service
    if _news_service is None:
        _news_service = NewsFeedService()
    return _news_service
