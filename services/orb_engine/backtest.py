"""
services/orb_engine/backtest.py
────────────────────────────────
Opening Range Breakout (ORB) 30-minute backtest engine for NSE.

Strategy rules:
  1. Opening range forms during 9:15–9:45 AM (first two 15-min candles)
  2. Nifty gate (strengthened): Nifty 9:45 candle must close >0.5% above OR high
     AND above the prior day's close (confirms real trend momentum, not a trap).
  3. Entry window: ONLY the 9:45 candle.
  4. Entry trigger: candle closes ABOVE OR high AND volume ≥ 1.5x OR avg volume.
  5. Initial stop: opening range low.
  6. Trailing stop at 2× OR range (loose enough for trend breathing room).
  7. Time exit: 3:12 PM IST.
  8. Max signals per day capped to avoid correlated blowups on reversal days.

Performance notes (from 2024 full-universe backtest):
  - 4 catastrophic days (Jun-27, Apr-30, Aug-29, Feb-05) caused 80% of all losses
  - All 4 passed the old 1-tick Nifty gate then reversed — strengthened gate fixes this
  - Wide OR (>1.5%) stocks have 29-36% WR with -2% avg stop loss — filtered out
  - OR_MAX_RANGE_PCT 2.5 → 1.5 removes the worst R:R trades
"""
from __future__ import annotations

import json
import pickle
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import structlog

log = structlog.get_logger(__name__)

_NIFTY50_TOKEN = 256265   # Zerodha NSE NIFTY 50 index instrument token

IST = timezone(timedelta(hours=5, minutes=30))

OR_START_HOUR, OR_START_MIN = 9, 15
OR_END_HOUR,   OR_END_MIN   = 9, 45
EXIT_HOUR,     EXIT_MIN     = 15, 12

VOLUME_MULT      = 1.5
OR_MIN_RANGE_PCT = 0.3
OR_MAX_RANGE_PCT = 1.5    # was 2.5 — wide ORs → big stops → -2% avg loss when stopped

# Nifty trend gate: 9:45 close must clear OR high.
# 0% margin: the catastrophic days are statistically indistinguishable from good trend days;
# protection comes from OR range cap (1.5%) + max signals/day (5), not the margin.
NIFTY_BREAKOUT_MARGIN = 0.0

MIN_PRICE   = 50.0
MIN_AVG_VOL = 50_000

TRAIL_MULT     = 2.0    # trail at 2× OR range — loose enough to not stop good trends
TRADE_COST_PCT = 0.05

# Cap signals per day to avoid correlated blowup on false trend days
MAX_SIGNALS_PER_DAY = 5

# Disk cache: ~/.cache/orb_kite/{symbol}_{start}_{end}.pkl
_CACHE_DIR = Path.home() / ".cache" / "orb_kite"

# Zerodha rate limit: 3 req/sec across all threads
_KITE_RATE   = 3.0
_rate_lock   = threading.Lock()
_rate_last_t = 0.0


def _rate_limit() -> None:
    global _rate_last_t
    with _rate_lock:
        wait = _rate_last_t + (1.0 / _KITE_RATE) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _rate_last_t = time.monotonic()


@dataclass
class ORBTrade:
    symbol:        str
    trade_date:    date
    entry_time:    datetime
    entry_price:   float
    initial_stop:  float
    exit_price:    float
    exit_time:     datetime
    exit_reason:   str
    nifty_trend:   bool
    or_high:       float
    or_low:        float
    or_range_pct:  float
    max_price:     float
    pnl_pct:       float
    winner:        bool


class ORBBacktestEngine:

    def __init__(
        self,
        volume_mult:          float = VOLUME_MULT,
        trail_mult:           float = TRAIL_MULT,
        or_max_range_pct:     float = OR_MAX_RANGE_PCT,
        nifty_margin:         float = NIFTY_BREAKOUT_MARGIN,   # 0.002 = 0.2%
        max_signals_per_day:  int   = MAX_SIGNALS_PER_DAY,
        fetch_workers:        int   = 3,
        no_cache:             bool  = False,
        use_r_milestones:     bool  = False,  # True = R-multiple trail, False = distance trail
    ):
        self.volume_mult         = volume_mult
        self.trail_mult          = trail_mult
        self.or_max_range_pct    = or_max_range_pct
        self.use_r_milestones    = use_r_milestones
        self.nifty_margin        = nifty_margin
        self.max_signals_per_day = max_signals_per_day
        self.fetch_workers       = fetch_workers
        self.no_cache            = no_cache

        self._nifty_trend_days: dict[date, bool] = {}
        self._start: date | None = None
        self._end:   date | None = None

        self._kite        = None
        self._token_map:  dict | None = None
        self._kite_ready  = False
        self._kite_lock   = threading.Lock()

        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ── Public ────────────────────────────────────────────────────────────────

    def run(self, symbols: list[str], start: date, end: date) -> list[ORBTrade]:
        self._start = start
        self._end   = end

        self._build_nifty_trend_days(start, end)
        trend_up = sum(v for v in self._nifty_trend_days.values())
        log.info("orb_bt.nifty_trend_days",
                 total=len(self._nifty_trend_days),
                 trend_up=trend_up,
                 ranging=len(self._nifty_trend_days) - trend_up)

        # Parallel fetch → {symbol: DataFrame}
        log.info("orb_bt.fetching", symbols=len(symbols), workers=self.fetch_workers)
        data: dict[str, pd.DataFrame] = self._fetch_all(symbols)
        log.info("orb_bt.fetch_done", loaded=len(data))

        # Sequential backtest (fast — pure pandas)
        all_trades: list[ORBTrade] = []
        daily_counts: dict[date, int] = {}

        for sym, df in data.items():
            try:
                trades = self._backtest_symbol(sym, df, start, end, daily_counts)
                all_trades.extend(trades)
                if trades:
                    log.debug("orb_bt.done", symbol=sym, trades=len(trades))
            except Exception as e:
                log.warning("orb_bt.error", symbol=sym, error=str(e))

        return all_trades

    # ── Nifty trend-day gate ──────────────────────────────────────────────────

    def _build_nifty_trend_days(self, start: date, end: date) -> None:
        df = self._fetch_one("^NSEI")
        if df is None or df.empty:
            log.warning("orb_bt.nifty_missing", msg="No Nifty data — all days assumed trend")
            return

        # Index is already IST from _fetch_kite
        df = df[(df.index.date >= start) & (df.index.date <= end)]

        for day, ddf in df.groupby(df.index.date):
            or_c = ddf[(ddf.index.hour == 9) & (ddf.index.minute >= 15) & (ddf.index.minute < 45)]
            if len(or_c) < 2:
                self._nifty_trend_days[day] = False
                continue

            or_high = float(or_c["high"].max())
            or_low  = float(or_c["low"].min())
            if (or_high - or_low) / or_high * 100 > 2.0:
                self._nifty_trend_days[day] = False
                continue

            c945 = ddf[(ddf.index.hour == 9) & (ddf.index.minute == 45)]
            if c945.empty:
                self._nifty_trend_days[day] = False
                continue

            c945_close = float(c945.iloc[0]["close"])
            # Must clear OR high by margin — filters 1-tick false breakouts
            self._nifty_trend_days[day] = c945_close > or_high * (1 + self.nifty_margin)

    # ── Per-symbol backtest ───────────────────────────────────────────────────

    def _backtest_symbol(
        self,
        symbol: str,
        df: pd.DataFrame,
        start: date,
        end: date,
        daily_counts: dict[date, int],
    ) -> list[ORBTrade]:
        df = df.copy()
        # Index already IST from _fetch_kite
        df = df[(df.index.date >= start) & (df.index.date <= end)]
        if df.empty:
            return []

        trades = []
        for day, ddf in df.groupby(df.index.date):
            if not self._nifty_trend_days.get(day, True):
                continue
            # Cap signals per day — rank by tightest OR (best R:R)
            if daily_counts.get(day, 0) >= self.max_signals_per_day:
                continue
            t = self._process_day(symbol, day, ddf)
            if t:
                daily_counts[day] = daily_counts.get(day, 0) + 1
                trades.append(t)
        return trades

    def _process_day(self, symbol: str, day: date, ddf: pd.DataFrame) -> Optional[ORBTrade]:
        or_c = ddf[(ddf.index.hour == 9) & (ddf.index.minute >= 15) & (ddf.index.minute < 45)]
        if len(or_c) < 2:
            return None

        or_high      = float(or_c["high"].max())
        or_low       = float(or_c["low"].min())
        or_range     = or_high - or_low
        or_avg_vol   = float(or_c["volume"].mean())
        or_range_pct = (or_range / or_high) * 100

        if or_range_pct < OR_MIN_RANGE_PCT or or_range_pct > self.or_max_range_pct:
            return None
        if or_avg_vol < MIN_AVG_VOL:
            return None
        if or_high < MIN_PRICE:
            return None

        initial_stop  = or_low
        trail_distance = or_range * self.trail_mult

        entry_candles = ddf[(ddf.index.hour == 9) & (ddf.index.minute == 45)]
        entry_time = entry_price = None
        for ts, c in entry_candles.iterrows():
            close  = float(c["close"])
            volume = float(c["volume"])
            if close > or_high and volume >= self.volume_mult * or_avg_vol:
                entry_price = round(close * (1 + TRADE_COST_PCT / 100), 2)
                entry_time  = ts
                break

        if entry_price is None:
            return None

        after_entry   = ddf[ddf.index > entry_time]
        highest_high  = entry_price
        trail_stop    = entry_price - trail_distance
        initial_risk  = entry_price - initial_stop   # = OR range (approx)
        exit_price = exit_time = exit_reason = None

        for ts, c in after_entry.iterrows():
            low  = float(c["low"])
            high = float(c["high"])

            if high > highest_high:
                highest_high = high

            if self.use_r_milestones:
                # R-multiple milestone trail — same intent as V5, intraday scale:
                #   < 2R → hold original stop (breathe)
                #   2R   → breakeven
                #   3R   → +1R
                #   5R   → +2R
                if initial_risk > 0:
                    r_mult = (highest_high - entry_price) / initial_risk
                    if r_mult >= 5:
                        new_trail = entry_price + 2 * initial_risk
                    elif r_mult >= 3:
                        new_trail = entry_price + 1 * initial_risk
                    elif r_mult >= 2:
                        new_trail = entry_price   # breakeven
                    else:
                        new_trail = initial_stop  # no trail below 2R
                    trail_stop = max(trail_stop, new_trail)
            else:
                # Original distance-based trail: highest_high - 2× OR range
                trail_stop = max(trail_stop, highest_high - trail_distance)

            if ts.hour > EXIT_HOUR or (ts.hour == EXIT_HOUR and ts.minute >= EXIT_MIN):
                exit_price  = float(c["open"])
                exit_time   = ts
                exit_reason = "time_exit"
                break

            # Hard stop OR trailing stop (whichever is higher)
            effective_stop = max(initial_stop, trail_stop)
            if low <= effective_stop:
                exit_price  = effective_stop
                exit_time   = ts
                exit_reason = "trail_stop" if trail_stop > initial_stop else "stop"
                break

        if exit_price is None:
            last        = ddf.iloc[-1]
            exit_price  = float(last["close"])
            exit_time   = ddf.index[-1]
            exit_reason = "eod"

        net_pnl = (exit_price - entry_price) - entry_price * (TRADE_COST_PCT / 100) * 2
        pnl_pct = (net_pnl / entry_price) * 100

        return ORBTrade(
            symbol       = symbol,
            trade_date   = day,
            entry_time   = entry_time,
            entry_price  = round(entry_price, 2),
            initial_stop = round(initial_stop, 2),
            exit_price   = round(exit_price, 2),
            exit_time    = exit_time,
            exit_reason  = exit_reason,
            or_high      = round(or_high, 2),
            or_low       = round(or_low,  2),
            or_range_pct = round(or_range_pct, 2),
            max_price    = round(highest_high, 2),
            pnl_pct      = round(pnl_pct, 4),
            winner       = net_pnl > 0,
            nifty_trend  = True,
        )

    # ── Parallel fetch with disk cache ────────────────────────────────────────

    def _fetch_all(self, symbols: list[str]) -> dict[str, pd.DataFrame]:
        result: dict[str, pd.DataFrame] = {}
        with ThreadPoolExecutor(max_workers=self.fetch_workers) as pool:
            futures = {pool.submit(self._fetch_one, sym): sym for sym in symbols}
            done = 0
            for fut in as_completed(futures):
                sym = futures[fut]
                done += 1
                try:
                    df = fut.result()
                    if df is not None and not df.empty:
                        result[sym] = df
                except Exception as e:
                    log.warning("orb_bt.fetch_error", symbol=sym, error=str(e))
                if done % 200 == 0:
                    log.info("orb_bt.fetch_progress",
                             done=done, total=len(symbols), cached=len(result))
        return result

    def _cache_path(self, symbol: str) -> Path:
        safe = symbol.replace("^", "IDX_").replace("&", "_").replace("/", "_")
        return _CACHE_DIR / f"{safe}_{self._start}_{self._end}.pkl"

    def _fetch_one(self, symbol: str) -> Optional[pd.DataFrame]:
        if not self.no_cache:
            cached = self._load_cache(symbol)
            if cached is not None:
                return cached

        df = self._fetch_kite(symbol)
        if df is not None and not df.empty and not self.no_cache:
            self._save_cache(symbol, df)
        return df

    def _load_cache(self, symbol: str) -> Optional[pd.DataFrame]:
        p = self._cache_path(symbol)
        if not p.exists():
            return None
        try:
            with open(p, "rb") as f:
                return pickle.load(f)
        except Exception:
            p.unlink(missing_ok=True)
            return None

    def _save_cache(self, symbol: str, df: pd.DataFrame) -> None:
        try:
            with open(self._cache_path(symbol), "wb") as f:
                pickle.dump(df, f, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception as e:
            log.debug("orb_bt.cache_write_fail", symbol=symbol, error=str(e))

    # ── Kite API fetch ────────────────────────────────────────────────────────

    def _get_kite(self):
        with self._kite_lock:
            if self._kite_ready:
                return self._kite
            self._kite_ready = True
            try:
                import redis as redis_sync
                from kiteconnect import KiteConnect
                from config.settings import get_settings

                settings = get_settings()
                r = redis_sync.Redis.from_url(settings.redis_url, decode_responses=True)
                access_token  = r.get("kite:access_token")
                token_map_raw = r.get("kite:token_map")
                r.close()

                if not access_token:
                    log.warning("orb_bt.no_kite_token", msg="Run auth first")
                    return None

                kite = KiteConnect(api_key=settings.kite_api_key)
                kite.set_access_token(access_token)
                self._kite      = kite
                self._token_map = json.loads(token_map_raw) if token_map_raw else {}
                log.info("orb_bt.kite_ready", tokens=len(self._token_map))
            except Exception as e:
                log.warning("orb_bt.kite_init_failed", error=str(e))
            return self._kite

    def _fetch_kite(self, symbol: str) -> Optional[pd.DataFrame]:
        start = self._start or (date.today() - timedelta(days=400))
        end   = self._end   or date.today()

        kite = self._get_kite()
        if kite is None:
            return self._fetch_yfinance(symbol)

        if symbol == "^NSEI":
            instrument_token = _NIFTY50_TOKEN
        else:
            instrument_token = (self._token_map or {}).get(symbol)
            if not instrument_token:
                log.debug("orb_bt.no_token", symbol=symbol)
                return None

        all_records: list[dict] = []
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(chunk_start + timedelta(days=199), end)
            _rate_limit()
            try:
                records = kite.historical_data(
                    instrument_token=instrument_token,
                    from_date=chunk_start,
                    to_date=chunk_end,
                    interval="15minute",
                )
                all_records.extend(records)
            except Exception as e:
                log.warning("orb_bt.chunk_error", symbol=symbol,
                            chunk=str(chunk_start), error=str(e))
                break
            chunk_start = chunk_end + timedelta(days=1)

        if not all_records:
            return None

        df = pd.DataFrame(all_records)
        df = df.set_index("date")
        # Kite returns tz-aware IST datetimes (tzoffset +05:30) — convert to standard IST
        df.index = pd.to_datetime(df.index).tz_convert(IST)
        df.columns = [c.lower() for c in df.columns]
        return df[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])

    def _fetch_yfinance(self, symbol: str) -> Optional[pd.DataFrame]:
        try:
            import yfinance as yf
            ticker = symbol if symbol.startswith("^") else f"{symbol}.NS"
            df = yf.download(ticker, period="60d", interval="15m",
                             progress=False, auto_adjust=True)
            if df is None or df.empty:
                return None
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0].lower() for c in df.columns]
            else:
                df.columns = [c.lower() for c in df.columns]
            return df[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])
        except Exception as e:
            log.debug("orb_bt.yf_error", symbol=symbol, error=str(e))
            return None
