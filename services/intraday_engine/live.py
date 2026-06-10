"""
services/intraday_engine/live.py
─────────────────────────────────
Live adapter for the IDARVAS intraday strategy.

Called from main.py handle_signal() at each 15-min candle close.

Strategy (validated 2024, all-NSE, 32 trades):
  Day filter : gap ≥ 1.5% + RVOL ≥ 2× (top-50 by gap×RVOL)
  Signal     : Darvas box = session high holds ≥5 candles (75 min), < 1.5% range
  Entry      : limit at box_top  (NOT candle close — keeps R:R tight)
  Stop       : box_low (lowest low during box formation)
  Time exit  : 13:30 IST (handled by trade lifecycle, not here)
  Perf 2024  : 32 trades, 68.75% WR, Sharpe 9.8, ₹26,920 PnL, Max DD ₹1,060
"""
from __future__ import annotations

from collections import deque
from datetime import date, datetime, time, timedelta, timezone

import pandas as pd
import structlog

from services.intraday_engine.backtest import (
    CANDLES_PER_DAY,
    MAX_STOP_PCT,
    SCANNER_MIN_GAP_PCT,
    TRADE_COST,
    detect_idarvas,
)
from services.technical_engine.signal_generator import Direction, Signal, SignalType

log = structlog.get_logger(__name__)

IST            = timezone(timedelta(hours=5, minutes=30))
MIN_15MIN_BARS = 8    # minimum candles needed for box detection
MIN_RVOL       = 2.0  # same as backtest WIDE_SCANNER_MIN_RVOL
ENTRY_CUT_TIME = time(13, 15)  # no new IDARVAS entries after 1:15 PM


class IntradayLiveEngine:
    """
    Live IDARVAS signal detector.

    Usage (called from main.py handle_signal()):
        engine = IntradayLiveEngine()
        sig    = await engine.detect(symbol, buf_15m, prev_close, daily_df, redis)
    """

    async def detect(
        self,
        symbol:     str,
        buf_15m:    deque,          # _candle_buffer[symbol]["15min"]
        prev_close: float,
        daily_df:   pd.DataFrame | None,
        redis,
    ) -> Signal | None:
        """
        Run IDARVAS detection on today's 15-min candles.
        Returns an INTRADAY_IDARVAS Signal or None.
        """
        if not buf_15m or prev_close <= 0:
            return None

        # Cooldown: one trade per symbol per day
        cooldown_key = f"idarvas:fired:{symbol}:{date.today()}"
        if await redis.get(cooldown_key):
            return None

        now_ist = datetime.now(IST)
        if now_ist.time() >= ENTRY_CUT_TIME:
            return None

        # ── Build today's 15-min DataFrame from buffer ────────────────────
        today = now_ist.date()
        rows  = []
        for c in buf_15m:
            ts = c.get("ts") or c.get("timestamp")
            if ts is None:
                continue
            if not hasattr(ts, "tzinfo") or ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            else:
                ts = ts.astimezone(IST)
            if ts.date() != today:
                continue
            rows.append({
                "timestamp": ts,
                "open":      float(c["open"]),
                "high":      float(c["high"]),
                "low":       float(c["low"]),
                "close":     float(c["close"]),
                "volume":    float(c["volume"]),
            })

        if len(rows) < MIN_15MIN_BARS:
            return None

        day_df = pd.DataFrame(rows).set_index("timestamp").sort_index()

        # ── Scanner pre-filter ─────────────────────────────────────────────
        open_price = float(day_df.iloc[0]["open"])
        gap_pct    = (open_price - prev_close) / prev_close * 100
        if gap_pct < SCANNER_MIN_GAP_PCT:
            return None

        if daily_df is None or len(daily_df) < 5:
            return None
        vol_col     = "Volume" if "Volume" in daily_df.columns else "volume"
        avg_dvol    = float(daily_df[vol_col].iloc[-20:].mean()) if len(daily_df) >= 20 \
                      else float(daily_df[vol_col].mean())
        if avg_dvol <= 0:
            return None

        first_2_vol = float(day_df.iloc[:2]["volume"].sum())
        rvol        = (first_2_vol * (CANDLES_PER_DAY / 2)) / avg_dvol
        if rvol < MIN_RVOL:
            return None

        # ── IDARVAS box detection ─────────────────────────────────────────
        sig = detect_idarvas(day_df, prev_close)
        if sig is None:
            return None

        await redis.setex(cooldown_key, 86400, "1")

        entry_price  = sig["entry_price"]
        stop_price   = sig["stop_price"]
        initial_risk = entry_price - stop_price
        target_price = entry_price + 3.5 * initial_risk   # 3.5R target (matches backtest avg)

        log.info(
            "idarvas_live.signal",
            symbol    = symbol,
            gap_pct   = round(gap_pct, 2),
            rvol      = round(rvol, 1),
            box_top   = sig.get("box_top"),
            box_low   = sig.get("box_low"),
            bars_held = sig.get("bars_held"),
            box_range = sig.get("box_range"),
            entry     = round(entry_price, 2),
            stop      = round(stop_price, 2),
            target    = round(target_price, 2),
        )

        return Signal(
            trading_symbol  = symbol,
            signal_type     = SignalType.INTRADAY_IDARVAS,
            direction       = Direction.BULLISH,
            confidence      = _confidence(gap_pct, rvol, sig),
            timeframe       = "15min",
            price_at_signal = round(entry_price, 2),
            entry_price     = round(entry_price, 2),
            stop_loss       = round(stop_price, 2),
            target          = round(target_price, 2),
            indicators      = {
                "gap_pct":      round(gap_pct, 2),
                "rvol":         round(rvol, 1),
                "box_top":      sig.get("box_top"),
                "box_low":      sig.get("box_low"),
                "bars_held":    sig.get("bars_held"),
                "box_range":    sig.get("box_range"),
                # Passed to RiskEngine as structure-based stop (bypasses ATR sizing).
                # box_low is the lowest low during box formation — natural invalidation level.
                "explicit_stop": round(stop_price, 2),
            },
        )


def _confidence(gap_pct: float, rvol: float, sig: dict) -> float:
    score = 65.0
    if gap_pct >= 5:
        score += 10
    elif gap_pct >= 3:
        score += 5
    if rvol >= 5:
        score += 10
    elif rvol >= 3:
        score += 5
    box_range = sig.get("box_range", 1.5)
    if box_range is not None:
        if box_range <= 0.5:
            score += 10
        elif box_range <= 1.0:
            score += 5
    return min(score, 92.0)
