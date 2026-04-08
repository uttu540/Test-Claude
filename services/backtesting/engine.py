"""
services/backtesting/engine.py
────────────────────────────────
Replays historical OHLCV data through the signal → risk pipeline
to measure strategy performance without live trading.

Data sources (tried in order):
  1. TimescaleDB ohlcv_candles table (requires running DB)
  2. yfinance download (fallback, no API key needed)

No orders are placed. Trades are simulated by checking whether
stop-loss or target was hit in candles following the signal.

Exit logic (no look-ahead bias):
  - Entry:  next candle's open after signal fires
  - Stop:   first candle where low ≤ stop_loss (LONG) or high ≥ stop_loss (SHORT)
  - Target: first candle where high ≥ target (LONG) or low ≤ target (SHORT)
  - EOD:    position closed at 3:20 PM candle close if neither hit
  - Max hold: 5 days (swing trade cap)
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import AsyncIterator

import numpy as np
import pandas as pd
import structlog

from config.settings import settings
from services.market_regime.detector import MarketRegimeDetector
from services.risk_engine.engine import RiskEngine
from services.technical_engine.indicators import compute_all
from services.technical_engine.signal_generator import (
    Direction,
    MultiTimeframeSignalEngine,
    Signal,
)

log = structlog.get_logger(__name__)

# Maximum candles to hold a simulated position before forcing exit
MAX_HOLD_CANDLES = 20   # ~5 days on 15min


@dataclass
class SimulatedTrade:
    symbol:           str
    signal_type:      str
    direction:        str
    timeframe:        str
    entry_date:       date
    entry_price:      float
    stop_loss:        float
    target:           float
    exit_price:       float      = 0.0
    exit_reason:      str        = "OPEN"   # TARGET | STOP | EOD | MAX_HOLD
    pnl:              float      = 0.0
    pnl_pct:          float      = 0.0
    holding_candles:  int        = 0
    signal_confidence: int       = 0
    regime:           str        = "UNKNOWN"
    risk_amount:      float      = 0.0
    position_size:    int        = 0


@dataclass
class BacktestResult:
    trades:        list[SimulatedTrade] = field(default_factory=list)
    symbols:       list[str]            = field(default_factory=list)
    start_date:    date | None          = None
    end_date:      date | None          = None
    timeframes:    list[str]            = field(default_factory=list)


class BacktestEngine:
    """
    Runs a full backtest over a set of symbols and date range.

    Usage:
        engine = BacktestEngine(
            symbols    = ["RELIANCE", "TCS", "INFY"],
            start_date = date(2024, 1, 1),
            end_date   = date(2024, 12, 31),
            timeframes = ["15min", "1hr", "1day"],
        )
        result = await engine.run()
    """

    def __init__(
        self,
        symbols:    list[str],
        start_date: date,
        end_date:   date,
        timeframes: list[str] | None = None,
        regime_aware: bool = True,
    ) -> None:
        self._symbols      = symbols
        self._start        = start_date
        self._end          = end_date
        self._timeframes   = timeframes or ["15min", "1hr", "1day"]
        self._regime_aware = regime_aware
        self._signal_engine = MultiTimeframeSignalEngine()
        self._risk_engine   = RiskEngine()
        self._regime_detector = MarketRegimeDetector()

    async def run(self) -> BacktestResult:
        result = BacktestResult(
            symbols    = self._symbols,
            start_date = self._start,
            end_date   = self._end,
            timeframes = self._timeframes,
        )

        for symbol in self._symbols:
            log.info("backtest.symbol_start", symbol=symbol)
            try:
                trades = await self._backtest_symbol(symbol)
                result.trades.extend(trades)
                log.info(
                    "backtest.symbol_done",
                    symbol = symbol,
                    trades = len(trades),
                )
            except Exception as e:
                log.error("backtest.symbol_error", symbol=symbol, error=str(e))

        log.info(
            "backtest.complete",
            symbols = len(self._symbols),
            trades  = len(result.trades),
        )
        return result

    # ── Per-symbol ────────────────────────────────────────────────────────────

    async def _backtest_symbol(self, symbol: str) -> list[SimulatedTrade]:
        # Load data for all timeframes
        data: dict[str, pd.DataFrame] = {}
        for tf in self._timeframes:
            df = await self._load_data(symbol, tf)
            if df is not None and not df.empty:
                data[tf] = df

        if not data:
            log.warning("backtest.no_data", symbol=symbol)
            return []

        # ── Pre-compute indicators ONCE per timeframe (major speedup) ─────────
        # Indicators are computed on the full df; rolling functions are causal
        # (only use past data), so no look-ahead bias is introduced.
        precomputed: dict[str, pd.DataFrame] = {}
        for tf, df in data.items():
            try:
                precomputed[tf] = compute_all(df)
            except Exception as e:
                log.warning("backtest.indicator_failed", symbol=symbol, tf=tf, error=str(e))

        if not precomputed:
            return []

        # Use 15min as the primary timeframe for signal scanning
        primary_tf = "15min" if "15min" in precomputed else list(precomputed.keys())[0]
        primary_df = precomputed[primary_tf]

        # Filter to backtest date range
        primary_df = primary_df[
            (primary_df.index.date >= self._start) &
            (primary_df.index.date <= self._end)
        ]
        if primary_df.empty:
            return []

        trades:         list[SimulatedTrade] = []
        window          = 200           # lookback rows passed to signal detector
        in_trade:       bool = False    # one-at-a-time position tracking
        exit_after_idx: int  = -1      # index after which we're free to trade again

        detector = self._signal_engine._detector
        reg_filter = self._signal_engine._filter

        for i in range(window, len(primary_df)):
            # Skip while we're in an open position
            if in_trade and i <= exit_after_idx:
                continue

            cutoff = primary_df.index[i]

            # Build per-timeframe slices from pre-computed data (no recomputation)
            snapshot: dict[str, pd.DataFrame] = {}
            for tf, df in precomputed.items():
                tf_slice = df[df.index <= cutoff].tail(window)
                if len(tf_slice) >= 50:
                    snapshot[tf] = tf_slice

            if not snapshot:
                continue

            # Determine regime from pre-computed daily data (last row only)
            regime = "UNKNOWN"
            if self._regime_aware and "1day" in snapshot:
                day_latest = snapshot["1day"].iloc[-1]
                adx = day_latest.get("adx") if hasattr(day_latest, "get") else day_latest["adx"] if "adx" in snapshot["1day"].columns else None
                ema_stack = day_latest.get("ema_stack") if hasattr(day_latest, "get") else day_latest["ema_stack"] if "ema_stack" in snapshot["1day"].columns else None
                if adx is not None and not pd.isna(adx):
                    if adx >= 25:
                        regime = "TRENDING_UP" if (ema_stack or 0) >= 0 else "TRENDING_DOWN"
                    elif adx < 20:
                        regime = "RANGING"
                    else:
                        regime = "TRENDING_UP" if (ema_stack or 0) >= 0 else "TRENDING_DOWN"

            # Detect signals using pre-computed data (skip compute_all)
            all_signals = []
            for tf, tf_df in snapshot.items():
                sigs = detector.detect(
                    tf_df, symbol, tf,
                    pre_computed=True,
                )
                all_signals.extend(sigs)

            if not all_signals:
                continue

            # Confluence boost + regime filter (same logic as MultiTimeframeSignalEngine)
            directions_by_tf = {}
            for s in all_signals:
                directions_by_tf[s.timeframe] = s.direction
            all_signals = self._signal_engine._apply_confluence_boost(all_signals, directions_by_tf)
            all_signals = reg_filter.apply(all_signals, regime)
            all_signals.sort(key=lambda s: s.confidence, reverse=True)

            if not all_signals:
                continue

            top = all_signals[0]
            if top.confidence < 65:
                continue

            atr = top.indicators.get("atr_14", 0)
            if not atr:
                continue

            risk_dec = await self._risk_engine.evaluate(
                symbol      = symbol,
                direction   = top.direction.value,
                entry_price = top.price_at_signal,
                atr         = atr,
            )
            if not risk_dec.approved:
                continue

            # Simulate exit on raw (uncomputed) candles to avoid indicator columns
            raw_primary = data.get(primary_tf, primary_df)
            future_raw = raw_primary[raw_primary.index > cutoff]
            trade = self._simulate_exit(
                signal       = top,
                risk_dec     = risk_dec,
                future_df    = future_raw,
                regime       = regime,
                entry_date   = cutoff.date() if hasattr(cutoff, "date") else cutoff,
            )
            if trade:
                trades.append(trade)
                in_trade = True
                # Mark roughly how many candles until the trade resolves
                exit_after_idx = i + trade.holding_candles

            in_trade = False  # reset for next signal

        return trades

    def _simulate_exit(
        self,
        signal,
        risk_dec,
        future_df:  pd.DataFrame,
        regime:     str,
        entry_date: date,
    ) -> SimulatedTrade | None:
        if future_df.empty:
            return None

        is_long    = signal.direction == Direction.BULLISH
        entry_price = future_df.iloc[0]["open"]   # Enter at next candle open
        stop_loss   = risk_dec.stop_loss
        target      = risk_dec.target

        exit_price  = entry_price
        exit_reason = "OPEN"
        hold        = 0

        for idx, (ts, candle) in enumerate(future_df.iterrows()):
            hold += 1

            if is_long:
                # Check stop hit
                if candle["low"] <= stop_loss:
                    exit_price  = stop_loss
                    exit_reason = "STOP"
                    break
                # Check target hit
                if candle["high"] >= target:
                    exit_price  = target
                    exit_reason = "TARGET"
                    break
            else:
                # SHORT
                if candle["high"] >= stop_loss:
                    exit_price  = stop_loss
                    exit_reason = "STOP"
                    break
                if candle["low"] <= target:
                    exit_price  = target
                    exit_reason = "TARGET"
                    break

            # EOD exit: last candle of the trading day
            if hasattr(ts, "time") and ts.time().hour == 15 and ts.time().minute >= 20:
                exit_price  = candle["close"]
                exit_reason = "EOD"
                break

            # Max hold cap
            if hold >= MAX_HOLD_CANDLES:
                exit_price  = candle["close"]
                exit_reason = "MAX_HOLD"
                break

        if exit_reason == "OPEN":
            return None   # Trade never resolved — skip

        multiplier = 1 if is_long else -1
        pnl        = (exit_price - entry_price) * multiplier * risk_dec.position_size
        pnl_pct    = (exit_price - entry_price) / entry_price * multiplier * 100

        return SimulatedTrade(
            symbol            = signal.trading_symbol,
            signal_type       = signal.signal_type.value,
            direction         = "LONG" if is_long else "SHORT",
            timeframe         = signal.timeframe,
            entry_date        = entry_date,
            entry_price       = round(entry_price, 2),
            stop_loss         = round(stop_loss, 2),
            target            = round(target, 2),
            exit_price        = round(exit_price, 2),
            exit_reason       = exit_reason,
            pnl               = round(pnl, 2),
            pnl_pct           = round(pnl_pct, 2),
            holding_candles   = hold,
            signal_confidence = signal.confidence,
            regime            = regime,
            risk_amount       = round(risk_dec.risk_amount, 2),
            position_size     = risk_dec.position_size,
        )

    # ── Data Loading ──────────────────────────────────────────────────────────

    async def _load_data(self, symbol: str, timeframe: str) -> pd.DataFrame | None:
        """Try TimescaleDB first, fall back to yfinance."""
        df = await self._load_from_db(symbol, timeframe)
        if df is not None and not df.empty:
            return df
        return await self._load_from_yfinance(symbol, timeframe)

    async def _load_from_db(self, symbol: str, timeframe: str) -> pd.DataFrame | None:
        try:
            from database.connection import get_db_session
            from sqlalchemy import text

            # Extra buffer days for indicator warm-up
            load_start = self._start - timedelta(days=60)

            async for session in get_db_session():
                result = await session.execute(
                    text("""
                        SELECT ts, open, high, low, close, volume
                        FROM ohlcv_candles
                        WHERE trading_symbol = :sym
                          AND timeframe      = :tf
                          AND ts             >= :start
                          AND ts             <= :end
                        ORDER BY ts ASC
                    """),
                    {
                        "sym":   symbol,
                        "tf":    timeframe,
                        "start": datetime.combine(load_start, datetime.min.time()),
                        "end":   datetime.combine(self._end, datetime.max.time()),
                    },
                )
                rows = result.fetchall()
                if not rows:
                    return None
                df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
                df["ts"] = pd.to_datetime(df["ts"])
                df = df.set_index("ts")
                return df
        except Exception as e:
            log.debug("backtest.db_load_failed", symbol=symbol, tf=timeframe, error=str(e))
            return None

    async def _load_from_yfinance(
        self, symbol: str, timeframe: str
    ) -> pd.DataFrame | None:
        """
        Download from yfinance as a fallback.
        NSE symbols need '.NS' suffix; uses interval mapping for timeframes.
        """
        try:
            import yfinance as yf

            # yfinance interval codes
            interval_map = {
                "1min":  "1m",
                "5min":  "5m",
                "15min": "15m",
                "1hr":   "1h",
                "1day":  "1d",
            }
            yf_interval = interval_map.get(timeframe)
            if not yf_interval:
                return None

            yf_symbol = f"{symbol}.NS"

            # yfinance caps intraday history at 60 days
            load_start = self._start - timedelta(days=60)
            if yf_interval in ("1m", "5m", "15m", "1h"):
                load_start = max(load_start, date.today() - timedelta(days=59))

            log.info("backtest.yfinance_download", symbol=yf_symbol, tf=timeframe)
            ticker = yf.Ticker(yf_symbol)
            df = ticker.history(
                start    = load_start.isoformat(),
                end      = (self._end + timedelta(days=1)).isoformat(),
                interval = yf_interval,
                auto_adjust = True,
            )

            if df.empty:
                return None

            df.index = pd.to_datetime(df.index)
            if df.index.tz is not None:
                df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)

            df = df.rename(columns={
                "Open": "open", "High": "high",
                "Low": "low",   "Close": "close", "Volume": "volume",
            })
            return df[["open", "high", "low", "close", "volume"]]

        except Exception as e:
            log.warning("backtest.yfinance_failed", symbol=symbol, error=str(e))
            return None
