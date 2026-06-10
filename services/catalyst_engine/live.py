"""
services/catalyst_engine/live.py
──────────────────────────────────
Live adapter for the Catalyst PEAD engine.

Detects Day1 catalyst gap conditions from daily OHLCV, then on Day2 open
verifies the gap is holding before firing a CATALYST_GAP_PEAD signal.

Strategy (validated 2022–2025, 40 trades, 40% WR, Sharpe 2.28):
  Day1 gap: open ≥7% above prior close + RVOL ≥8× + close/high ≥90%
  Day2 entry: today's market open ≥99% of yesterday's close
  Stop:  2.5% below Day2 open
  Target: 3×ATR14 above entry

Called from main.py handle_signal() at 9:15 AM (first candle close).
"""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import structlog

from services.catalyst_engine.scanner import CatalystScanner
from services.technical_engine.signal_generator import Direction, Signal, SignalType

log = structlog.get_logger(__name__)

MIN_DAILY_BARS = 25   # need 20d avg vol + 5 buffer


class CatalystLiveEngine:
    """
    Checks whether today is a Day2 PEAD entry for a given symbol.

    Usage (called from main.py handle_signal):
        engine = CatalystLiveEngine()
        sig    = await engine.detect(symbol, daily_df, candle_buffer, redis)
    """

    def __init__(self) -> None:
        self._scanner = CatalystScanner()

    async def detect(
        self,
        symbol:       str,
        daily_df:     pd.DataFrame,
        today_open:   float | None,   # 9:15 candle open price (today)
        redis,
    ) -> Signal | None:
        """
        Returns a CATALYST_GAP_PEAD Signal or None.

        today_open: first tick / 9:15 candle open. If None, falls back to
        latest available close as a rough proxy (paper mode only).
        """
        if daily_df is None or len(daily_df) < MIN_DAILY_BARS:
            return None

        # One signal per symbol per day
        cooldown_key = f"catalyst:fired:{symbol}:{date.today()}"
        if await redis.get(cooldown_key):
            return None

        df = daily_df.copy()
        if hasattr(df.index, "tz") and df.index.tz is not None:
            df.index = df.index.tz_convert(None)
        df.index = pd.to_datetime(df.index).normalize()
        df = df.sort_index()

        today_ts     = pd.Timestamp(date.today())
        yesterday_ts = today_ts - timedelta(days=1)

        # Find yesterday's bar (most recent bar before today)
        prior_to_today = df[df.index < today_ts]
        if prior_to_today.empty:
            return None
        yesterday_bar = prior_to_today.iloc[-1]
        yesterday_date = prior_to_today.index[-1].date()

        # Must be yesterday (within 3 calendar days to handle weekends/holidays)
        if (date.today() - yesterday_date).days > 3:
            return None

        # Scan yesterday as a potential catalyst gap day
        signals = self._scanner.scan(
            symbol    = symbol,
            daily_df  = df,
            start     = yesterday_date,
            end       = yesterday_date,
        )
        if not signals:
            return None

        sig = signals[0]

        # Day2 entry condition: today's open must hold ≥99% of yesterday's close
        if today_open is not None:
            if today_open < sig.day1_close * 0.99:
                log.debug(
                    "catalyst_live.gap_faded",
                    symbol    = symbol,
                    d1_close  = sig.day1_close,
                    d2_open   = today_open,
                )
                return None
            open_price = today_open
        else:
            # Paper fallback: use yesterday's close as proxy (can't verify gap held)
            open_price = sig.day1_close

        entry = open_price * 1.001
        stop  = entry * 0.975   # 2.5% hard stop
        atr   = sig.atr14 if sig.atr14 > 0 else entry * 0.02
        target = entry + 3.0 * atr

        await redis.setex(cooldown_key, 86400, "1")

        log.info(
            "catalyst_live.signal",
            symbol           = symbol,
            gap_pct          = sig.gap_pct,
            rvol             = sig.rvol,
            close_high_ratio = sig.close_high_ratio,
            entry            = round(entry, 2),
            stop             = round(stop, 2),
            target           = round(target, 2),
            atr14            = sig.atr14,
        )

        return Signal(
            trading_symbol  = symbol,
            signal_type     = SignalType.CATALYST_GAP_PEAD,
            direction       = Direction.BULLISH,
            confidence      = _confidence(sig),
            timeframe       = "1day",
            price_at_signal = round(entry, 2),
            entry_price     = round(entry, 2),
            stop_loss       = round(stop, 2),
            target          = round(target, 2),
            indicators      = {
                "gap_pct":          sig.gap_pct,
                "rvol":             sig.rvol,
                "close_high_ratio": sig.close_high_ratio,
                "atr_14":           sig.atr14,   # renamed: executor reads "atr_14"
                "day1_close":       sig.day1_close,
                # 2.5% hard stop from entry — passed as explicit_stop so RiskEngine
                # sizes position from real structure risk, not ATR-derived.
                "explicit_stop":    round(stop, 2),
            },
        )


def _confidence(sig) -> float:
    """Score 60-95 based on gap %, RVOL, and close/high ratio."""
    score = 60.0
    # Gap contribution (7-10%: +5, 10-15%: +15)
    if sig.gap_pct >= 10:
        score += 15
    elif sig.gap_pct >= 7:
        score += 5
    # RVOL contribution (8-12×: +5, 12-20×: +10, 20×+: +15)
    if sig.rvol >= 20:
        score += 15
    elif sig.rvol >= 12:
        score += 10
    elif sig.rvol >= 8:
        score += 5
    # Close/high ratio (0.95+: +5)
    if sig.close_high_ratio >= 0.95:
        score += 5
    return min(score, 95.0)
