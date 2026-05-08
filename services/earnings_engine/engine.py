"""
services/earnings_engine/engine.py
────────────────────────────────────
Earnings-catalyst signal detection.

Logic ("street will reward it"):
  A stock that reported good quarterly results should:
    1. Gap up at open (market approves)
    2. Have elevated relative volume (smart money buying)
    3. Break and hold above the opening range high (ORB confirmation)

Entry criteria (all required):
  Gap    3–12%  vs previous day close
         <3%: noise; >12%: circuit-breaker risk on NSE
  RVOL   scales with gap size (3× for 3–7%, 5× for >7%)
  ORB    price must break above first 15-min candle's high after 9:30 AM IST
         (confirms gap-and-go, filters gap-and-crap)
  Cooldown  72h per symbol — one entry per earnings event

EARNINGS_MISS: symmetric bearish signal (gap down + RVOL surge).
Long-only mode in paper trading means MISS signals are generated but
filtered by main.py's BULLISH-only filter.
"""
from __future__ import annotations

import json
from collections import deque
from datetime import date, datetime

import structlog

from services.technical_engine.signal_generator import Direction, Signal, SignalType

log = structlog.get_logger(__name__)

MIN_GAP_PCT      = 3.0    # raised from 1.5 — sub-3% gaps are noise
MAX_GAP_PCT      = 12.0   # >12% = circuit-breaker risk, skip
CONFIDENCE_BASE  = 68
CONFIDENCE_MID   = 74
CONFIDENCE_STRONG = 80    # gap ≥7% AND RVOL ≥5×


def _rvol_threshold(gap_pct: float) -> float:
    """RVOL minimum scales with gap — large gaps on thin volume tend to reverse."""
    if abs(gap_pct) >= 7.0:
        return 5.0
    return 3.0


class EarningsSignalEngine:
    """
    Scans symbols with recent earnings for gap-and-go setups.

    Called:
      - At 9:30 AM via job_earnings_scan (bulk scan of all results stocks)
      - Per 15min candle in _run_signals if symbol is in recent_results list
        (catches delayed reactions — some stocks move hours after results)
    """

    async def scan_all(
        self,
        candle_buffer: dict[str, dict[str, deque]],
        redis,
    ) -> list[Signal]:
        """Bulk scan: called at 9:30 AM after open."""
        from services.earnings_engine.announcements import get_recent_results_symbols

        symbols = await get_recent_results_symbols(lookback_days=3)
        if not symbols:
            log.info("earnings_engine.no_results_symbols")
            return []

        log.info("earnings_engine.scan_start", symbols=len(symbols))
        signals: list[Signal] = []
        for sym in symbols:
            sig = await self.check_symbol(sym, candle_buffer, redis)
            if sig:
                signals.append(sig)

        log.info("earnings_engine.scan_done", signals=len(signals))
        return signals

    async def check_symbol(
        self,
        symbol: str,
        candle_buffer: dict[str, dict[str, deque]],
        redis,
    ) -> Signal | None:
        """
        Check a single symbol for an earnings gap-and-go setup.
        Returns Signal or None.
        """
        # 72h cooldown — same earnings event won't re-fire next morning
        if await redis.get(f"earnings:fired:{symbol}"):
            return None

        tick_raw = await redis.get(f"market:tick:{symbol}")
        if not tick_raw:
            return None

        try:
            tick = json.loads(tick_raw)
            current_price = float(tick.get("lp") or 0)
            prev_close    = float(tick.get("c")  or 0)
            open_price    = float(tick.get("o")  or current_price)
            cum_volume    = int(tick.get("vol")  or 0)
        except (ValueError, TypeError):
            return None

        if not current_price or not prev_close:
            return None

        gap_pct = (current_price - prev_close) / prev_close * 100

        is_beat = gap_pct >= MIN_GAP_PCT
        is_miss = gap_pct <= -MIN_GAP_PCT

        if not is_beat and not is_miss:
            return None

        # Circuit-breaker filter: >12% gap on NSE risks locked positions
        if abs(gap_pct) > MAX_GAP_PCT:
            log.info("earnings_engine.gap_too_large", symbol=symbol, gap_pct=round(gap_pct, 2))
            return None

        # RVOL: scale cum_vol to full-day equivalent
        avg_volume = self._get_avg_daily_volume(symbol, candle_buffer)
        if avg_volume > 0 and cum_volume > 0:
            from datetime import datetime as _dt
            from zoneinfo import ZoneInfo
            IST = ZoneInfo("Asia/Kolkata")
            now_ist = _dt.now(IST)
            mins_elapsed = max(1, (now_ist.hour * 60 + now_ist.minute) - (9 * 60 + 15))
            session_mins = 375  # 9:15 to 15:30
            projected_vol = cum_volume * (session_mins / mins_elapsed)
            rvol = projected_vol / avg_volume
        else:
            rvol = 0.0

        min_rvol = _rvol_threshold(gap_pct)
        if rvol < min_rvol:
            log.info(
                "earnings_engine.rvol_too_low",
                symbol=symbol, gap_pct=round(gap_pct, 2),
                rvol=round(rvol, 2), required=min_rvol,
            )
            return None

        # ORB confirmation: price must break above first 15-min candle's high
        # Only applies after 9:30 AM when at least 1 full candle has printed
        orb_high = self._get_orb_high(symbol, candle_buffer)
        if orb_high is not None:
            if is_beat and current_price < orb_high:
                log.info("earnings_engine.orb_not_broken",
                         symbol=symbol, price=current_price, orb_high=orb_high)
                return None
        else:
            # Pre-9:30 or no 15-min data yet — use legacy price-holding check
            if is_beat and open_price > 0 and current_price < open_price * 0.99:
                log.info("earnings_engine.price_fading", symbol=symbol, gap_pct=round(gap_pct, 2))
                return None

        # Confidence tier
        if is_beat:
            direction   = Direction.BULLISH
            signal_type = SignalType.EARNINGS_BEAT
            if gap_pct >= 7.0 and rvol >= 5.0:
                confidence = CONFIDENCE_STRONG
            elif gap_pct >= 5.0 or rvol >= 4.0:
                confidence = CONFIDENCE_MID
            else:
                confidence = CONFIDENCE_BASE
        else:
            direction   = Direction.BEARISH
            signal_type = SignalType.EARNINGS_MISS
            if abs(gap_pct) >= 7.0 and rvol >= 5.0:
                confidence = CONFIDENCE_STRONG
            elif abs(gap_pct) >= 5.0 or rvol >= 4.0:
                confidence = CONFIDENCE_MID
            else:
                confidence = CONFIDENCE_BASE

        sig = Signal(
            trading_symbol  = symbol,
            timeframe       = "1day",
            signal_type     = signal_type,
            direction       = direction,
            confidence      = confidence,
            price_at_signal = current_price,
            indicators      = {
                "gap_pct":    round(gap_pct, 2),
                "rvol":       round(rvol, 2),
                "prev_close": prev_close,
                "open":       open_price,
                "orb_high":   orb_high,
                "cum_volume": cum_volume,
                "avg_volume": int(avg_volume),
                "catalyst":   "EARNINGS",
            },
            notes=f"Earnings {'beat' if is_beat else 'miss'}: gap {gap_pct:+.1f}%, RVOL {rvol:.1f}x",
        )

        log.info(
            "earnings_engine.signal_found",
            symbol     = symbol,
            direction  = direction.value,
            gap_pct    = round(gap_pct, 2),
            rvol       = round(rvol, 2),
            confidence = confidence,
            orb_used   = orb_high is not None,
        )
        return sig

    @staticmethod
    def _get_orb_high(
        symbol: str,
        candle_buffer: dict[str, dict[str, deque]],
    ) -> float | None:
        """
        Return high of the first 15-min candle of today (9:15–9:30 AM IST).
        This is the Opening Range Breakout level — price must clear it to confirm
        the gap is holding and not reversing (gap-and-crap filter).
        Returns None if candle not yet available (before 9:30 AM).
        """
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
        today = _dt.now(IST).date()

        candles_15m = candle_buffer.get(symbol, {}).get("15min")
        if not candles_15m:
            return None

        for candle in candles_15m:
            ts = candle.get("timestamp") or candle.get("date")
            if ts is None:
                continue
            try:
                if isinstance(ts, str):
                    candle_dt = _dt.fromisoformat(ts).astimezone(IST)
                elif hasattr(ts, "astimezone"):
                    candle_dt = ts.astimezone(IST)
                else:
                    continue
                if (candle_dt.date() == today
                        and candle_dt.hour == 9
                        and candle_dt.minute == 15):
                    high = float(candle.get("high") or 0)
                    return high if high > 0 else None
            except (ValueError, TypeError):
                continue
        return None

    @staticmethod
    def _get_avg_daily_volume(
        symbol: str,
        candle_buffer: dict[str, dict[str, deque]],
        lookback: int = 20,
    ) -> float:
        """Compute 20-day average volume from the 1day candle buffer."""
        daily = candle_buffer.get(symbol, {}).get("1day")
        if not daily or len(daily) < 5:
            return 0.0
        bars = list(daily)[-lookback:]
        vols = [float(b.get("volume") or 0) for b in bars if b.get("volume")]
        return sum(vols) / len(vols) if vols else 0.0
