"""
services/catalyst_engine/backtest.py
──────────────────────────────────────
Catalyst gap-and-go PEAD backtest engine.

Strategy (validated 2022–2025, Nifty500):
  Signal — Day 1: gap 5–15% + RVOL ≥8× + close/high ≥90% (gap held)
  Entry  — Day 2 open + 0.1% slippage (enter only if D2 open ≥99% D1 close)
  Stop   — 2.5% below entry (hard stop; no OR-low guessing)
  Target — entry + 3×ATR14 (ATR as of Day 1)
  MaxHold— 5 trading days then exit at open

Why Day2 entry instead of same-day 9:45 ORB:
  Same-day 9:45 breakout passes only ~7% of valid gap days and produced -19
  Sharpe in testing. Day2 entry captures PEAD (Post-Earnings Announcement
  Drift): institutional re-pricing happens over days, not minutes.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import structlog
import yfinance as yf

from services.catalyst_engine.scanner import CatalystScanner, CatalystSignal

log = structlog.get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

STOP_PCT        = 2.5   # hard stop % below Day2 entry
TARGET_ATR_MULT = 3.0   # target = entry + N×ATR14
MAX_HOLD_DAYS   = 5
COMMISSION_PCT  = 0.05  # round-trip

_CACHE_DIR   = Path.home() / ".cache" / "catalyst_kite"
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
class CatalystTrade:
    symbol:          str
    signal_date:     date    # Day 1 (gap day)
    entry_date:      date    # Day 2
    entry_price:     float
    stop_price:      float
    exit_price:      float
    exit_date:       date
    exit_reason:     str     # stop / target / max_hold
    gap_pct:         float
    rvol:            float
    close_high_ratio: float
    atr14:           float
    hold_days:       int
    pnl_pct:         float
    pnl_r:           float   # PnL / initial_risk
    winner:          bool


class CatalystBacktestEngine:

    def __init__(
        self,
        min_gap_pct:          float = 7.0,
        max_gap_pct:          float = 15.0,
        min_rvol:             float = 8.0,
        min_close_high_ratio: float = 0.90,
        stop_pct:             float = STOP_PCT,
        target_atr_mult:      float = TARGET_ATR_MULT,
        max_hold_days:        int   = MAX_HOLD_DAYS,
        fetch_workers:        int   = 4,
    ):
        self.scanner = CatalystScanner(
            min_gap_pct          = min_gap_pct,
            max_gap_pct          = max_gap_pct,
            min_rvol             = min_rvol,
            min_close_high_ratio = min_close_high_ratio,
        )
        self.stop_pct        = stop_pct
        self.target_atr_mult = target_atr_mult
        self.max_hold_days   = max_hold_days
        self.fetch_workers   = fetch_workers

        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ── Public ────────────────────────────────────────────────────────────────

    def run(
        self,
        symbols: list[str],
        start: date,
        end: date,
    ) -> list[CatalystTrade]:
        self._start = start
        self._end   = end

        log.info("catalyst_bt.start", symbols=len(symbols),
                 start=str(start), end=str(end))

        # Fetch daily OHLCV for all symbols
        log.info("catalyst_bt.fetch_daily")
        daily: dict[str, pd.DataFrame] = self._fetch_all_daily(symbols, start, end)
        log.info("catalyst_bt.daily_done", loaded=len(daily))

        # Scan for gap signals
        all_signals: list[CatalystSignal] = []
        for sym in symbols:
            if sym not in daily:
                continue
            try:
                sigs = self.scanner.scan(sym, daily[sym], start, end)
                all_signals.extend(sigs)
            except Exception as e:
                log.debug("catalyst_bt.scan_error", symbol=sym, error=str(e))

        log.info("catalyst_bt.signals_found", count=len(all_signals))

        # Simulate trades (Day 2 open entry)
        trades: list[CatalystTrade] = []
        for sig in sorted(all_signals, key=lambda s: s.signal_date):
            daily_df = daily.get(sig.symbol)
            if daily_df is None:
                continue
            t = self._simulate_trade(sig, daily_df)
            if t:
                trades.append(t)

        log.info("catalyst_bt.complete", trades=len(trades))
        return trades

    # ── Trade simulation ──────────────────────────────────────────────────────

    def _simulate_trade(
        self,
        sig: CatalystSignal,
        daily_df: pd.DataFrame,
    ) -> Optional[CatalystTrade]:
        daily_df = daily_df.copy()
        if hasattr(daily_df.index, "tz") and daily_df.index.tz is not None:
            daily_df.index = daily_df.index.tz_convert(None)
        daily_df.index = pd.to_datetime(daily_df.index).normalize()
        daily_df = daily_df.sort_index()

        signal_ts = pd.Timestamp(sig.signal_date)
        # Day 2 = next trading day after signal
        forward = daily_df[daily_df.index > signal_ts]
        if forward.empty:
            return None

        # Entry condition: Day 2 open must hold gap (≥99% of Day1 close)
        d2_row   = forward.iloc[0]
        d2_open  = float(d2_row["open"])
        if d2_open < sig.day1_close * 0.99:
            return None  # gap faded overnight — skip

        entry        = d2_open * 1.001  # 0.1% slippage
        stop         = entry * (1 - self.stop_pct / 100)
        initial_risk = entry - stop

        atr   = sig.atr14 if sig.atr14 > 0 else initial_risk
        target = entry + self.target_atr_mult * atr

        # Simulate hold period (Day2 onwards, max N days)
        hold_window = forward.head(self.max_hold_days)
        exit_price = exit_date = exit_reason = None

        for ts, bar in hold_window.iterrows():
            day_low  = float(bar["low"])
            day_high = float(bar["high"])

            if day_low <= stop:
                exit_price  = stop * (1 - COMMISSION_PCT / 200)
                exit_date   = ts.date()
                exit_reason = "stop"
                break

            if day_high >= target:
                exit_price  = target * (1 - COMMISSION_PCT / 200)
                exit_date   = ts.date()
                exit_reason = "target"
                break

        if exit_price is None:
            last_bar    = hold_window.iloc[-1]
            exit_price  = float(last_bar["open"]) * (1 - COMMISSION_PCT / 200)
            exit_date   = hold_window.index[-1].date()
            exit_reason = "max_hold"

        hold_days = len(hold_window[hold_window.index <= pd.Timestamp(exit_date)])
        pnl_pct   = (exit_price - entry) / entry * 100
        pnl_r     = (exit_price - entry) / initial_risk if initial_risk > 0 else 0.0

        return CatalystTrade(
            symbol           = sig.symbol,
            signal_date      = sig.signal_date,
            entry_date       = forward.index[0].date(),
            entry_price      = round(entry, 2),
            stop_price       = round(stop, 2),
            exit_price       = round(exit_price, 2),
            exit_date        = exit_date,
            exit_reason      = exit_reason,
            gap_pct          = sig.gap_pct,
            rvol             = sig.rvol,
            close_high_ratio = sig.close_high_ratio,
            atr14            = sig.atr14,
            hold_days        = hold_days,
            pnl_pct          = round(pnl_pct, 4),
            pnl_r            = round(pnl_r, 3),
            winner           = pnl_pct > 0,
        )

    # ── Data fetching ─────────────────────────────────────────────────────────

    def _fetch_all_daily(
        self,
        symbols: list[str],
        start: date,
        end: date,
    ) -> dict[str, pd.DataFrame]:
        result: dict[str, pd.DataFrame] = {}
        fetch_start = (start - timedelta(days=90)).strftime("%Y-%m-%d")
        fetch_end   = (end + timedelta(days=MAX_HOLD_DAYS + 5)).strftime("%Y-%m-%d")

        batch_size = 50
        for i in range(0, len(symbols), batch_size):
            batch   = symbols[i : i + batch_size]
            tickers = [f"{s}.NS" for s in batch]
            try:
                raw = yf.download(
                    tickers,
                    start=fetch_start,
                    end=fetch_end,
                    interval="1d",
                    progress=False,
                    auto_adjust=True,
                    group_by="ticker",
                )
                for sym, ticker in zip(batch, tickers):
                    try:
                        df = raw[ticker].copy()
                        df.columns = [c.lower() for c in df.columns]
                        df = df.dropna(subset=["close"])
                        if not df.empty:
                            result[sym] = df
                    except Exception:
                        pass
            except Exception as e:
                log.warning("catalyst_bt.yf_batch_error",
                            batch_start=i, error=str(e))

        return result
