"""
services/orb_engine/live.py
────────────────────────────
ORB live scanner — called at 10:00 AM when the 9:45 candle closes.

Reads today's 15-min candles from the in-memory candle buffer and applies
the same rules validated in backtest:
  1. OR forms at 9:15–9:45 (first two 15-min candles)
  2. Nifty gate: Nifty's 9:45 candle must close above its OR high
  3. Entry: 9:45 candle closes above OR high with volume ≥ 1.5× OR avg
  4. Stop: OR low (passed as atr_14 = stop_distance / 2 so risk engine
     computes stop_loss = entry - 2.0×ATR = entry - stop_distance = or_low)
  5. Exit: 3:12 PM via job_square_off_intraday
"""
from __future__ import annotations

from collections import deque
from datetime import date, datetime, timezone, timedelta
from typing import Any

import structlog

from services.technical_engine.signal_generator import Direction, Signal, SignalType

log = structlog.get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ── Filters (same as backtest) ────────────────────────────────────────────────
VOLUME_MULT      = 1.5
OR_MIN_RANGE_PCT = 0.3
OR_MAX_RANGE_PCT = 2.5
MIN_PRICE        = 50.0
MIN_AVG_VOL      = 50_000
TRADE_COST_PCT   = 0.05   # one-way slippage + brokerage (%)


def _today_15min(buffer_entry: deque, today: date) -> list[dict]:
    """Return today's 15-min candles from a symbol's buffer, sorted oldest-first."""
    result = []
    for c in buffer_entry:
        ts = c["ts"]
        # Handle both tz-aware and tz-naive timestamps
        if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
            ts_ist = ts.astimezone(IST)
        else:
            ts_ist = ts
        if ts_ist.date() == today:
            result.append({**c, "_ts_ist": ts_ist})
    return sorted(result, key=lambda x: x["_ts_ist"])


def _nifty_trend_up(candle_buffer: dict[str, dict[str, deque]], today: date) -> bool:
    """
    Returns True if Nifty's 9:45 candle closed above its OR high — i.e., today
    is a genuine trend day, not a fake breakout or ranging session.
    """
    nifty_buf = candle_buffer.get("NIFTY 50", {}).get("15min")
    if not nifty_buf:
        # No Nifty data in buffer — default to True (don't block if unknown)
        log.warning("orb_live.nifty_missing", msg="No Nifty 15min data in buffer — assuming trend-up")
        return True

    candles = _today_15min(nifty_buf, today)

    # OR candles: minute 15 and 30 (hour 9)
    or_c = [c for c in candles
            if c["_ts_ist"].hour == 9 and 15 <= c["_ts_ist"].minute < 45]
    if len(or_c) < 2:
        log.info("orb_live.nifty_or_missing", bars=len(or_c))
        return False

    or_high = max(c["high"] for c in or_c)
    or_low  = min(c["low"]  for c in or_c)

    # Reject gap-chaos days
    if (or_high - or_low) / or_high * 100 > 2.0:
        log.info("orb_live.nifty_gap_chaos", or_range_pct=round((or_high - or_low) / or_high * 100, 2))
        return False

    # 9:45 candle (opens at 9:45)
    c945 = [c for c in candles if c["_ts_ist"].hour == 9 and c["_ts_ist"].minute == 45]
    if not c945:
        log.info("orb_live.nifty_c945_missing")
        return False

    result = float(c945[0]["close"]) > or_high
    log.info(
        "orb_live.nifty_gate",
        nifty_c945_close=round(c945[0]["close"], 2),
        nifty_or_high=round(or_high, 2),
        trend_up=result,
    )
    return result


def scan_orb_signals(
    candle_buffer: dict[str, dict[str, deque]],
    symbols: list[str],
    today: date | None = None,
) -> list[Signal]:
    """
    Scan all symbols for ORB setups on today's 9:45 candle close.

    Returns a list of Signal(ORB_BREAKOUT, BULLISH) objects ready to pass
    to TradeExecutor. Empty list means no setups (Nifty not trend-up or no
    stocks met the breakout criteria).
    """
    if today is None:
        today = datetime.now(IST).date()

    # ── Nifty trend-day gate ──────────────────────────────────────────────────
    if not _nifty_trend_up(candle_buffer, today):
        log.info("orb_live.blocked", reason="Nifty not trend-up — skipping all ORB scans")
        return []

    signals: list[Signal] = []
    skipped_no_data = skipped_filter = 0

    for symbol in symbols:
        buf = candle_buffer.get(symbol, {}).get("15min")
        if not buf:
            skipped_no_data += 1
            continue

        candles = _today_15min(buf, today)

        # OR: first two 15-min candles (9:15 and 9:30)
        or_c = [c for c in candles
                if c["_ts_ist"].hour == 9 and 15 <= c["_ts_ist"].minute < 45]
        if len(or_c) < 2:
            skipped_no_data += 1
            continue

        or_high    = max(c["high"]   for c in or_c)
        or_low     = min(c["low"]    for c in or_c)
        or_avg_vol = sum(c["volume"] for c in or_c) / len(or_c)
        or_range   = or_high - or_low
        or_range_pct = (or_range / or_high) * 100

        # Filters
        if or_range_pct < OR_MIN_RANGE_PCT or or_range_pct > OR_MAX_RANGE_PCT:
            skipped_filter += 1
            continue
        if or_avg_vol < MIN_AVG_VOL:
            skipped_filter += 1
            continue
        if or_high < MIN_PRICE:
            skipped_filter += 1
            continue

        # 9:45 entry candle
        c945 = [c for c in candles
                if c["_ts_ist"].hour == 9 and c["_ts_ist"].minute == 45]
        if not c945:
            skipped_no_data += 1
            continue

        entry_c = c945[0]
        close  = float(entry_c["close"])
        volume = float(entry_c["volume"])

        # Entry condition: close above OR high + volume surge
        if close <= or_high or volume < VOLUME_MULT * or_avg_vol:
            skipped_filter += 1
            continue

        # Entry price with slippage
        entry_price = round(close * (1 + TRADE_COST_PCT / 100), 2)

        # Stop = OR low. Trick the risk engine: set atr_14 so that
        #   stop_loss = entry - 2.0 × atr_14 = or_low
        #   → atr_14 = (entry - or_low) / 2.0
        stop_distance = entry_price - or_low
        if stop_distance <= 0:
            continue
        atr_proxy = stop_distance / 2.0   # ATR_STOP_MULTIPLIER = 2.0 in RiskEngine

        vol_ratio = round(volume / or_avg_vol, 2)

        sig = Signal(
            trading_symbol  = symbol,
            timeframe       = "15min",
            signal_type     = SignalType.ORB_BREAKOUT,
            direction       = Direction.BULLISH,
            confidence      = 82,   # Fixed confidence — ORB quality is binary (fires or not)
            price_at_signal = entry_price,
            indicators      = {
                "atr_14":       round(atr_proxy,    2),
                "or_high":      round(or_high,      2),
                "or_low":       round(or_low,       2),
                "or_range_pct": round(or_range_pct, 2),
                "or_avg_vol":   int(or_avg_vol),
                "breakout_vol": int(volume),
                "rvol":         vol_ratio,   # For confluence gate volume factor
                "stop_price":   round(or_low, 2),
            },
            notes = (
                f"ORB breakout | OR {round(or_low,1)}–{round(or_high,1)} "
                f"({or_range_pct:.1f}%) | vol {vol_ratio:.1f}× avg | stop={round(or_low,1)}"
            ),
        )
        signals.append(sig)
        log.info(
            "orb_live.setup",
            symbol       = symbol,
            entry        = entry_price,
            or_high      = round(or_high, 2),
            or_low       = round(or_low,  2),
            or_range_pct = round(or_range_pct, 2),
            vol_ratio    = vol_ratio,
        )

    log.info(
        "orb_live.scan_complete",
        total_symbols = len(symbols),
        setups_found  = len(signals),
        skipped_no_data = skipped_no_data,
        skipped_filter  = skipped_filter,
    )
    return signals
