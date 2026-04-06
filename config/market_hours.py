"""
config/market_hours.py
───────────────────────
NSE market hours, holidays, and the is_market_open() utility.

Usage:
    from config.market_hours import is_market_open, MARKET_OPEN, MARKET_CLOSE

NSE holiday list is maintained in config/nse_holidays.json.
Update it at the start of each year with NSE's official holiday circular.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog

log = structlog.get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Configurable market session times (NSE)
MARKET_OPEN:      time = time(9, 15)    # 9:15 AM IST
MARKET_CLOSE:     time = time(15, 30)   # 3:30 PM IST
PRE_OPEN_START:   time = time(9, 0)     # Pre-open session start
SQUAREOFF_CUTOFF: time = time(15, 20)   # Intraday square-off deadline

# Path to the holiday JSON file (sibling of this module)
_HOLIDAY_FILE = Path(__file__).parent / "nse_holidays.json"

# Cached holiday set (loaded once)
_nse_holidays: set[date] | None = None


def _load_holidays() -> set[date]:
    global _nse_holidays
    if _nse_holidays is not None:
        return _nse_holidays
    try:
        with open(_HOLIDAY_FILE) as f:
            raw: list[str] = json.load(f)
        _nse_holidays = {date.fromisoformat(d) for d in raw}
        log.info("market_hours.holidays_loaded", count=len(_nse_holidays))
    except FileNotFoundError:
        log.warning("market_hours.holiday_file_missing", path=str(_HOLIDAY_FILE))
        _nse_holidays = set()
    except Exception as e:
        log.error("market_hours.holiday_load_error", error=str(e))
        _nse_holidays = set()
    return _nse_holidays


def is_trading_day(dt: date | None = None) -> bool:
    """Return True if the given date is an NSE trading day (not weekend, not holiday)."""
    d = dt or date.today()
    if d.weekday() >= 5:   # Saturday=5, Sunday=6
        return False
    holidays = _load_holidays()
    if d in holidays:
        log.info("market_hours.holiday", date=d.isoformat())
        return False
    return True


def is_market_open(dt: datetime | None = None) -> bool:
    """
    Return True if the market is currently open for trading.
    Takes a timezone-aware datetime, or uses now() in IST if not provided.
    """
    now = dt or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)

    if not is_trading_day(now.date()):
        return False

    current_time = now.time().replace(tzinfo=None)
    return MARKET_OPEN <= current_time <= MARKET_CLOSE


def next_market_open() -> datetime:
    """Return the datetime of the next market open in IST."""
    today = date.today()
    candidate = today
    for _ in range(10):   # Look up to 10 days ahead
        if is_trading_day(candidate):
            return datetime(
                candidate.year, candidate.month, candidate.day,
                MARKET_OPEN.hour, MARKET_OPEN.minute,
                tzinfo=IST,
            )
        from datetime import timedelta
        candidate += timedelta(days=1)
    raise RuntimeError("Could not find next trading day within 10 days")
