"""
services/orb_engine/backtest.py
────────────────────────────────
Opening Range Breakout (ORB) 30-minute backtest engine for NSE.

Strategy rules:
  1. Opening range forms during 9:15–9:45 AM (first two 15-min candles)
  2. Nifty gate: Nifty's own 9:45 candle must close above its OR high
     (confirms a real trend day — not a false breakout on an individual stock).
  3. Entry window: ONLY the 9:45 candle (10:00 as one fallback).
     ORB is about immediate momentum. If price doesn't break out in the first
     1-2 candles after the OR closes, the setup is dead — skip the day.
  4. Entry trigger: candle closes ABOVE OR high AND volume ≥ 1.5x OR avg volume.
  5. Initial stop: opening range low.
  6. Trailing stop: once in the trade, trail stop = highest_high_since_entry
     minus OR range width. Stop only moves UP, never down. This lets winners
     run as long as momentum holds, while cutting losers at the OR low.
  7. Time exit: 2:30 PM IST. No new entries after 10:00 AM.
  8. One trade per symbol per day.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import structlog
import yfinance as yf

log = structlog.get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

OR_START_HOUR, OR_START_MIN = 9, 15
OR_END_HOUR,   OR_END_MIN   = 9, 45
EXIT_HOUR,     EXIT_MIN     = 15, 12   # Time exit: 3:12 PM IST

VOLUME_MULT      = 1.5   # Breakout candle volume ≥ 1.5x OR avg volume (trend days)
OR_MIN_RANGE_PCT = 0.3   # Min OR width % (filter dead/flat opens)
OR_MAX_RANGE_PCT = 2.5   # Max OR width % (filter gap-chaos days)

# Strong-breakout bypass: fire even on non-trend (ranging) days if the move is extreme.
# A news/earnings-driven stock doesn't care whether Nifty is trending.
# Thresholds tuned to catch only genuine catalyst moves, not random noise.
BYPASS_MARGIN_PCT = 1.5  # Close must be >1.5% above OR high (not just a tick)
BYPASS_VOL_MULT   = 3.0  # Volume must be ≥ 3× OR avg (explosive, not routine)

MIN_PRICE     = 50.0     # Skip penny/micro-cap stocks below ₹50
MIN_AVG_VOL   = 50_000   # Skip illiquid stocks (avg OR volume < 50k shares)

# Trail distance = OR range × multiplier.
# 1.5 = trail 1.5× OR-range below the highest high — gives winners room to breathe.
TRAIL_MULT = 1.5

TRADE_COST_PCT = 0.05    # One-way cost per side (slippage + brokerage)


@dataclass
class ORBTrade:
    symbol:        str
    trade_date:    date
    entry_time:    datetime
    entry_price:   float
    initial_stop:  float        # OR low
    exit_price:    float
    exit_time:     datetime
    exit_reason:   str          # "time_exit" | "stop" | "eod"
    nifty_trend:   bool        # True = normal trend day, False = bypass (catalyst move)
    or_high:       float
    or_low:        float
    or_range_pct:  float
    max_price:     float        # Highest high seen after entry (shows peak unrealised)
    pnl_pct:       float
    winner:        bool


class ORBBacktestEngine:

    def __init__(self, volume_mult: float = VOLUME_MULT, trail_mult: float = TRAIL_MULT):
        self.volume_mult = volume_mult
        self.trail_mult  = trail_mult
        self._nifty_trend_days: dict[date, bool] = {}

    # ── Public ────────────────────────────────────────────────────────────────

    def run(self, symbols: list[str], start: date, end: date) -> list[ORBTrade]:
        self._build_nifty_trend_days(start, end)
        trend_up = sum(v for v in self._nifty_trend_days.values())
        log.info("orb_bt.nifty_trend_days",
                 total=len(self._nifty_trend_days),
                 trend_up=trend_up,
                 ranging=len(self._nifty_trend_days) - trend_up)

        all_trades: list[ORBTrade] = []
        for i, sym in enumerate(symbols, 1):
            try:
                trades = self._backtest_symbol(sym, start, end)
                all_trades.extend(trades)
                if trades:
                    log.debug("orb_bt.done", symbol=sym, trades=len(trades), idx=f"{i}/{len(symbols)}")
            except Exception as e:
                log.warning("orb_bt.error", symbol=sym, error=str(e))
            if i % 20 == 0:
                time.sleep(1.0)

        return all_trades

    # ── Nifty trend-day gate ──────────────────────────────────────────────────

    def _build_nifty_trend_days(self, start: date, end: date) -> None:
        df = self._fetch("^NSEI")
        if df is None or df.empty:
            log.warning("orb_bt.nifty_missing", msg="No Nifty data — all days assumed trend")
            return

        df.index = df.index.tz_convert(IST)
        df = df[(df.index.date >= start) & (df.index.date <= end)]

        for day, ddf in df.groupby(df.index.date):
            or_c = ddf[(ddf.index.hour == 9) & (ddf.index.minute >= 15) & (ddf.index.minute < 45)]
            if len(or_c) < 2:
                self._nifty_trend_days[day] = False
                continue

            or_high = float(or_c["high"].max())
            or_low  = float(or_c["low"].min())
            if (or_high - or_low) / or_high * 100 > 2.0:   # gap-chaos day
                self._nifty_trend_days[day] = False
                continue

            c945 = ddf[(ddf.index.hour == 9) & (ddf.index.minute == 45)]
            self._nifty_trend_days[day] = (
                not c945.empty and float(c945.iloc[0]["close"]) > or_high
            )

    # ── Per-symbol backtest ───────────────────────────────────────────────────

    def _backtest_symbol(self, symbol: str, start: date, end: date) -> list[ORBTrade]:
        df = self._fetch(symbol)
        if df is None or df.empty:
            return []
        df.index = df.index.tz_convert(IST)
        df = df[(df.index.date >= start) & (df.index.date <= end)]
        if df.empty:
            return []

        trades = []
        for day, ddf in df.groupby(df.index.date):
            if not self._nifty_trend_days.get(day, True):
                continue
            t = self._process_day(symbol, day, ddf)
            if t:
                trades.append(t)
        return trades

    def _process_day(self, symbol: str, day: date, ddf: pd.DataFrame) -> Optional[ORBTrade]:
        # ── Opening range ─────────────────────────────────────────────────────
        or_c = ddf[(ddf.index.hour == 9) & (ddf.index.minute >= 15) & (ddf.index.minute < 45)]
        if len(or_c) < 2:
            return None

        or_high      = float(or_c["high"].max())
        or_low       = float(or_c["low"].min())
        or_range     = or_high - or_low
        or_avg_vol   = float(or_c["volume"].mean())
        or_range_pct = (or_range / or_high) * 100

        if or_range_pct < OR_MIN_RANGE_PCT or or_range_pct > OR_MAX_RANGE_PCT:
            return None
        if or_avg_vol < MIN_AVG_VOL:
            return None
        if or_high < MIN_PRICE:
            return None

        initial_stop = or_low

        # ── Entry: 9:45 candle ONLY — no fallback ────────────────────────────
        # 10:00 fallback had 30% WR and -24.93% total PnL in analysis.
        # ORB momentum is immediate — if it doesn't fire at 9:45, skip the day.
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

        # ── Exit: OR low as hard stop, hold to 3:12 PM ───────────────────────
        # No trailing stop — time exits had 65% WR vs trail stop's 37% WR.
        # Trail was stopping out good trades on normal intraday pullbacks.
        # Only exit early if price breaks back below the OR low.
        after_entry = ddf[ddf.index > entry_time]

        highest_high = entry_price
        exit_price = exit_time = exit_reason = None

        for ts, c in after_entry.iterrows():
            low  = float(c["low"])
            high = float(c["high"])

            if high > highest_high:
                highest_high = high

            # Time exit at 3:12 PM
            if ts.hour > EXIT_HOUR or (ts.hour == EXIT_HOUR and ts.minute >= EXIT_MIN):
                exit_price  = float(c["open"])
                exit_time   = ts
                exit_reason = "time_exit"
                break

            # Hard stop: price breaks back below OR low
            if low <= initial_stop:
                exit_price  = initial_stop
                exit_time   = ts
                exit_reason = "stop"
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

    # ── Fetch ─────────────────────────────────────────────────────────────────

    def _fetch(self, symbol: str) -> Optional[pd.DataFrame]:
        ticker = symbol if symbol.startswith("^") else f"{symbol}.NS"
        try:
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
            log.debug("orb_bt.fetch_error", symbol=symbol, error=str(e))
            return None
