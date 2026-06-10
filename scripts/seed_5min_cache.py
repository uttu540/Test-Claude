"""
scripts/seed_5min_cache.py
───────────────────────────
Seed 5-minute OHLCV data from Kite Connect into ~/.cache/fvg_5min/
for use by the FVG_ORB intraday backtest engine.

Kite provides 5-min intraday data back 2+ years — full 2024 coverage.

Usage:
    # Seed full NSE universe for 2024
    cd "/Users/utkarshagrawal/Documents/Test Claude"
    venv/bin/python scripts/seed_5min_cache.py --start 2024-01-01 --end 2024-12-31

    # Quick test: seed only top-500 stocks
    venv/bin/python scripts/seed_5min_cache.py --start 2024-01-01 --end 2024-12-31 --universe nifty500

Requirements:
    - Kite access token must be in Redis (bot must be running and auth'd)
    - Redis + Docker must be up
    - ~25 minutes for full 1800-symbol universe at Kite rate limit (3 req/s)

Output:
    ~/.cache/fvg_5min/{SYMBOL}_{start}_{end}.pkl
    Each pkl = pandas DataFrame with 5-min OHLCV, tz-aware IST index
"""
from __future__ import annotations

import argparse
import json
import pickle
import re
import sys
import time
import threading
from datetime import date, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import structlog

log = structlog.get_logger(__name__)

_CACHE_DIR  = Path.home() / ".cache" / "fvg_5min"
_KITE_RATE  = 3.0   # requests/second
_rate_last_t = 0.0
_rate_lock   = threading.Lock()

IST_OFFSET = "+05:30"


def _rate_limit() -> None:
    global _rate_last_t
    with _rate_lock:
        wait = _rate_last_t + (1.0 / _KITE_RATE) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _rate_last_t = time.monotonic()


def _get_kite():
    """Load Kite client from Redis access token (same pattern as ORB engine)."""
    try:
        import redis as redis_sync
        from kiteconnect import KiteConnect
        import os, sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from config.settings import get_settings

        settings = get_settings()
        r = redis_sync.Redis.from_url(settings.redis_url, decode_responses=True)
        access_token  = r.get("kite:access_token")
        token_map_raw = r.get("kite:token_map")
        r.close()

        if not access_token:
            print("ERROR: No Kite access token in Redis. Start bot + auth first.")
            return None, {}

        kite = KiteConnect(api_key=settings.kite_api_key)
        kite.set_access_token(access_token)
        token_map = json.loads(token_map_raw) if token_map_raw else {}
        print(f"Kite ready. Token map: {len(token_map)} symbols.")
        return kite, token_map

    except Exception as e:
        print(f"ERROR: Kite init failed: {e}")
        return None, {}


def _fetch_5min(kite, token_map: dict, symbol: str,
                start: date, end: date) -> pd.DataFrame | None:
    """
    Fetch 5-min OHLCV from Kite for given date range.
    Chunks into 200-day windows (Kite historical API limit).
    """
    token = token_map.get(symbol)
    if not token:
        return None

    all_records = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=99), end)   # Kite 5-min limit: 100 days
        _rate_limit()
        try:
            records = kite.historical_data(
                instrument_token=token,
                from_date=chunk_start,
                to_date=chunk_end,
                interval="5minute",
            )
            all_records.extend(records)
        except Exception as e:
            log.warning("seed_5min.chunk_error", symbol=symbol, chunk=str(chunk_start), error=str(e))
            break
        chunk_start = chunk_end + timedelta(days=1)

    if not all_records:
        return None

    df = pd.DataFrame(all_records).set_index("date")
    df.index = pd.to_datetime(df.index).tz_convert("Asia/Kolkata")
    df.columns = [c.lower() for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])
    return df if not df.empty else None


def _cache_path(symbol: str, start: date, end: date) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", symbol)
    return _CACHE_DIR / f"{safe}_{start}_{end}.pkl"


def _already_cached(symbol: str, start: date, end: date) -> bool:
    p = _cache_path(symbol, start, end)
    return p.exists() and p.stat().st_size > 1000


def seed_symbol(kite, token_map: dict, symbol: str,
                start: date, end: date, force: bool = False) -> str:
    """Returns 'cached' | 'seeded' | 'failed'"""
    if not force and _already_cached(symbol, start, end):
        return "cached"

    df = _fetch_5min(kite, token_map, symbol, start, end)
    if df is None or df.empty:
        return "failed"

    path = _cache_path(symbol, start, end)
    with open(path, "wb") as f:
        pickle.dump(df, f, protocol=4)
    return "seeded"


def main():
    parser = argparse.ArgumentParser(description="Seed 5-min Kite cache for FVG_ORB backtest")
    parser.add_argument("--start",     default="2024-01-01")
    parser.add_argument("--end",       default="2024-12-31")
    parser.add_argument("--universe",  default="all_nse",
                        choices=["all_nse", "nifty500", "nifty50"])
    parser.add_argument("--workers",   type=int, default=3,
                        help="Parallel workers (keep ≤ 3 to respect Kite rate limit)")
    parser.add_argument("--force",     action="store_true",
                        help="Re-seed even if cache exists")
    parser.add_argument("--symbols",   nargs="+", help="Specific symbols to seed")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end)

    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Cache dir: {_CACHE_DIR}")

    # Load symbol universe
    if args.symbols:
        symbols = args.symbols
    elif args.universe == "all_nse":
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from services.data_ingestion.nifty500_instruments import get_live_universe
        symbols = get_live_universe()
    elif args.universe == "nifty500":
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from services.data_ingestion.nifty500_instruments import NIFTY500
        symbols = [s for s, _, _ in NIFTY500]
    else:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        symbols = ["ADANIENT","ADANIPORTS","APOLLOHOSP","ASIANPAINT","AXISBANK",
                   "BAJAJ-AUTO","BAJFINANCE","BHARTIARTL","CIPLA","COALINDIA",
                   "DRREDDY","HCLTECH","HDFCBANK","HINDALCO","ICICIBANK",
                   "INFY","ITC","JSWSTEEL","KOTAKBANK","LT",
                   "M&M","MARUTI","NTPC","ONGC","RELIANCE",
                   "SBIN","SUNPHARMA","TATAMOTORS","TATASTEEL","TCS"]

    print(f"Universe: {len(symbols)} symbols | {start} → {end}")

    # Skip already-cached
    to_seed  = [s for s in symbols if not _already_cached(s, start, end)] if not args.force else symbols
    n_cached = len(symbols) - len(to_seed)
    print(f"Already cached: {n_cached} | To seed: {len(to_seed)}")

    if not to_seed:
        print("All symbols already cached.")
        return

    # Estimate time
    calls     = len(to_seed) * 4   # ~4 chunks per symbol for 1 year (100-day limit)
    est_min   = calls / (_KITE_RATE * 60)
    print(f"Estimated time: ~{est_min:.0f} min at {_KITE_RATE} req/s")

    kite, token_map = _get_kite()
    if kite is None:
        sys.exit(1)

    missing_tokens = [s for s in to_seed if s not in token_map]
    if missing_tokens:
        print(f"WARNING: {len(missing_tokens)} symbols have no Kite token (will be skipped): "
              f"{missing_tokens[:10]}{'...' if len(missing_tokens) > 10 else ''}")
        to_seed = [s for s in to_seed if s in token_map]

    print(f"Seeding {len(to_seed)} symbols with {args.workers} workers...\n")

    seeded = failed = 0
    t0 = time.monotonic()

    # Single-threaded with progress (Kite rate limit is per connection, not per thread)
    for i, sym in enumerate(to_seed, 1):
        result = seed_symbol(kite, token_map, sym, start, end, args.force)
        if result == "seeded":
            seeded += 1
        elif result == "failed":
            failed += 1
            log.warning("seed_5min.failed", symbol=sym)

        if i % 50 == 0 or i == len(to_seed):
            elapsed  = time.monotonic() - t0
            rate     = i / elapsed
            eta_sec  = (len(to_seed) - i) / rate if rate > 0 else 0
            print(f"  [{i:4d}/{len(to_seed)}] seeded={seeded} failed={failed} | "
                  f"elapsed={elapsed/60:.1f}m ETA={eta_sec/60:.1f}m")

    elapsed = time.monotonic() - t0
    print(f"\nDone in {elapsed/60:.1f} min. Seeded={seeded} Failed={failed} Cached={n_cached}")
    print(f"Cache: {_CACHE_DIR}")


if __name__ == "__main__":
    main()
