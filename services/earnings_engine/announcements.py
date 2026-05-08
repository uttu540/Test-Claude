"""
services/earnings_engine/announcements.py
──────────────────────────────────────────
Fetches today's and recent corporate earnings announcements from NSE.

NSE corporate-announcements API requires a browser-like session (cookies).
We hit the NSE home first to get a valid session, then hit the API endpoint.
Results are cached in Redis for 6 hours to avoid hammering NSE.

Redis keys:
  earnings:results:{YYYY-MM-DD}  → JSON list of NSE symbols   TTL 6h
  earnings:recent_symbols         → JSON list of symbols with results in last 3 days   TTL 72h
  earnings:fetch_failed:{YYYY-MM-DD} → "1" if fetch returned empty/errored  TTL 2h
"""
from __future__ import annotations

import json
import asyncio
from datetime import date, timedelta

import httpx
import structlog

from database.connection import get_redis

log = structlog.get_logger(__name__)

NSE_HOME        = "https://www.nseindia.com"
NSE_ANN_URL     = "https://www.nseindia.com/api/corporate-announcements"
NSE_RESULTS_URL = "https://www.nseindia.com/api/event-calendar"

# NSE API uses "Financial Result Updates" as desc for actual quarterly/annual filings.
# "Reply to Clarification" and "Clarification - Financial Results" are exchange queries, NOT results.
_RESULTS_DESC_EXACT = {
    "financial result updates",
}

# Secondary keywords — broader fallback if NSE changes API desc wording
_RESULTS_SUBJECTS = {
    "financial results", "quarterly results", "half yearly results",
    "annual results", "unaudited financial results",
    "audited financial results", "standalone financial results",
    "consolidated financial results",
}

# Hard exclusions — take priority over any include match
_EXCLUDE_SUBJECTS = {
    "reply to clarification",
    "clarification",
    "reasons for delayed",
    "outcome of board meeting",   # too broad — catches ESOP grants, policy changes
    "board meeting", "agm", "annual general meeting",
    "extraordinary general meeting", "egm",
    "record date", "book closure", "dividend",
    "buyback", "rights issue", "allotment",
    "change in management", "press release",
    "investor presentation", "analyst meet",
    "credit rating", "loss of share certificate",
}

_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/",
    "Connection":      "keep-alive",
}


def _is_results_announcement(subject: str) -> bool:
    """
    True if the NSE announcement desc is a genuine quarterly/annual results filing.
    Primary: exact match on "Financial Result Updates" (NSE's actual filing desc).
    Fallback: keyword match for API changes.
    Exclusions always take priority.
    """
    sl = subject.lower().strip()
    if any(kw in sl for kw in _EXCLUDE_SUBJECTS):
        return False
    if sl in _RESULTS_DESC_EXACT:
        return True
    return any(kw in sl for kw in _RESULTS_SUBJECTS)


async def _nse_session() -> httpx.AsyncClient:
    """Return an httpx client primed with NSE session cookies."""
    client = httpx.AsyncClient(
        headers=_NSE_HEADERS,
        follow_redirects=True,
        timeout=20,
    )
    try:
        await client.get(NSE_HOME)
        await asyncio.sleep(0.5)
    except Exception as e:
        log.warning("earnings.nse_session_error", error=str(e))
    return client


async def fetch_results_for_date(target: date) -> list[str]:
    """
    Return NSE symbols that announced quarterly/annual results on `target`.
    Hits NSE corporate-announcements API, filters by subject.

    On API failure: returns [] and sets `earnings:fetch_failed:{date}` so
    job_fetch_earnings_calendar can detect and alert via Telegram.
    """
    date_str  = target.strftime("%d-%m-%Y")
    redis_key = f"earnings:results:{target.isoformat()}"
    fail_key  = f"earnings:fetch_failed:{target.isoformat()}"

    redis  = get_redis()
    cached = await redis.get(redis_key)
    if cached:
        return json.loads(cached)

    symbols: list[str] = []
    fetch_ok = False

    try:
        client = await _nse_session()
        try:
            resp = await client.get(
                NSE_ANN_URL,
                params={"index": "equities", "from_date": date_str, "to_date": date_str},
            )
            if resp.status_code == 200:
                fetch_ok = True
                data = resp.json()
                if isinstance(data, list):
                    for item in data:
                        subject = (item.get("desc") or item.get("subject") or "").lower()
                        sym     = (item.get("symbol") or "").strip().upper()
                        if sym and _is_results_announcement(subject):
                            symbols.append(sym)
                    seen: set[str] = set()
                    symbols = [s for s in symbols if not (s in seen or seen.add(s))]  # type: ignore[func-returns-value]
            else:
                log.warning("earnings.nse_ann_error", status=resp.status_code, date=date_str)
        finally:
            await client.aclose()
    except Exception as e:
        log.warning("earnings.nse_fetch_error", error=str(e))

    if not fetch_ok:
        # Flag failure so morning job can send Telegram alert
        await redis.setex(fail_key, 2 * 3600, "1")
        log.warning("earnings.fetch_failed_flagged", date=date_str)
        # Don't cache empty result on hard failure — retry on next call
        return []

    # Cache even empty list on success (valid market day with no results) — TTL 6h
    await redis.setex(redis_key, 6 * 3600, json.dumps(symbols))

    if symbols:
        log.info("earnings.results_fetched", date=date_str, count=len(symbols), symbols=symbols[:10])
    else:
        log.info("earnings.results_none", date=date_str)

    return symbols


async def get_recent_results_symbols(lookback_days: int = 3) -> list[str]:
    """
    Return all NSE symbols that reported results in the last `lookback_days`.
    Combines today + yesterday + day before yesterday.
    Caches combined list in Redis for fast per-candle lookups.
    """
    redis_key = "earnings:recent_symbols"
    redis = get_redis()
    cached = await redis.get(redis_key)
    if cached:
        return json.loads(cached)

    today = date.today()
    all_symbols: list[str] = []
    seen: set[str] = set()

    tasks = [
        fetch_results_for_date(today - timedelta(days=i))
        for i in range(lookback_days)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, list):
            for s in r:
                if s not in seen:
                    all_symbols.append(s)
                    seen.add(s)

    # Cache 4h — refreshed by morning job; short enough to pick up same-day results
    await redis.setex(redis_key, 4 * 3600, json.dumps(all_symbols))
    log.info("earnings.recent_symbols_cached", count=len(all_symbols), symbols=all_symbols[:15])
    return all_symbols


async def check_fetch_failures(target: date) -> bool:
    """
    Returns True if the NSE fetch failed for `target` (failure flag in Redis).
    Called by job_fetch_earnings_calendar to decide whether to send a Telegram alert.
    """
    redis = get_redis()
    return bool(await redis.get(f"earnings:fetch_failed:{target.isoformat()}"))


async def invalidate_recent_cache() -> None:
    """Force-refresh the recent symbols cache (call after fetching today's results)."""
    redis = get_redis()
    await redis.delete("earnings:recent_symbols")
