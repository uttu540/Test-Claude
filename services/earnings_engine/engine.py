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

Day 2 entry mode (earnings_day2_mode=True in settings):
  Day 1: gap detected → stored as pending in Redis (not fired yet)
  Day 2 9:30 AM: validate Day 1 close held ≥97% of open AND closed in top
  50% of Day 1 range (sustained buying, not gap-and-crap) → fire at Day 2 open.
  Backtest shows profit factor improves from 0.50× to 10×+ vs Day 1 entry.

EARNINGS_MISS: symmetric bearish signal (gap down + RVOL surge).
Long-only mode in paper trading means MISS signals are generated but
filtered by main.py's BULLISH-only filter.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import date, datetime, timedelta

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
        from config.settings import settings
        from services.earnings_engine.announcements import get_recent_results_symbols

        signals: list[Signal] = []

        # Day 2 mode: fire confirmed pending signals from yesterday first
        if settings.earnings_day2_mode:
            day2_signals = await self.check_pending_day2_signals(candle_buffer, redis)
            signals.extend(day2_signals)

        symbols = await get_recent_results_symbols(lookback_days=3)
        if not symbols:
            log.info("earnings_engine.no_results_symbols")
            return signals

        log.info("earnings_engine.scan_start", symbols=len(symbols))
        for sym in symbols:
            sig = await self.check_symbol(sym, candle_buffer, redis)
            if sig:
                signals.append(sig)

        log.info("earnings_engine.scan_done", signals=len(signals))
        return signals

    async def check_pending_day2_signals(
        self,
        candle_buffer: dict[str, dict[str, deque]],
        redis,
    ) -> list[Signal]:
        """
        9:30 AM Day 2: read yesterday's stored pending signals, validate Day 1
        close quality, fire at today's open price if confirmed.
        """
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
        today = datetime.now(IST).date()

        keys = await redis.keys("earnings:pending_day2:*")
        if not keys:
            return []

        signals: list[Signal] = []
        for key in keys:
            raw = await redis.get(key)
            if not raw:
                continue
            try:
                pending = json.loads(raw)
            except Exception:
                await redis.delete(key)
                continue

            symbol = pending["symbol"]

            if await redis.get(f"earnings:fired:{symbol}"):
                await redis.delete(key)
                continue

            # Must be from a previous trading day
            day1_date_str = pending.get("day1_date", "")
            try:
                day1_date = date.fromisoformat(day1_date_str)
                if day1_date >= today:
                    continue  # same day — not ready yet
            except ValueError:
                await redis.delete(key)
                continue

            # Get Day 2 open price from tick
            tick_raw = await redis.get(f"market:tick:{symbol}")
            if not tick_raw:
                continue
            try:
                tick = json.loads(tick_raw)
                day2_open = float(tick.get("o") or 0)
                if not day2_open:
                    continue
            except (ValueError, TypeError):
                continue

            # Validate Day 1 close quality via yfinance
            try:
                held, quality_ok = await asyncio.get_running_loop().run_in_executor(
                    None, self._validate_day1_close, symbol, pending
                )
            except Exception as e:
                log.warning("earnings_engine.day1_validate_error", symbol=symbol, error=str(e))
                held, quality_ok = True, True  # fail-open

            if not held:
                log.info("earnings_engine.day2_gap_crap", symbol=symbol,
                         day1_date=day1_date_str, gap_pct=pending.get("gap_pct"))
                await redis.delete(key)
                continue
            if not quality_ok:
                log.info("earnings_engine.day2_close_quality_fail", symbol=symbol,
                         day1_date=day1_date_str)
                await redis.delete(key)
                continue

            gap_pct      = pending["gap_pct"]
            rvol         = pending["rvol"]
            confidence   = pending["confidence"]
            fgate_adj    = pending.get("fgate_adj", 0)
            fgate_flags  = pending.get("fgate_flags", [])
            fgate_reason = pending.get("fgate_reasoning", "")
            prev_close   = pending.get("prev_close", 0)
            day1_open    = pending.get("day1_open", 0)

            sig = Signal(
                trading_symbol  = symbol,
                timeframe       = "1day",
                signal_type     = SignalType.EARNINGS_BEAT,
                direction       = Direction.BULLISH,
                confidence      = confidence,
                price_at_signal = day2_open,
                indicators      = {
                    "gap_pct":          round(gap_pct, 2),
                    "rvol":             round(rvol, 2),
                    "prev_close":       prev_close,
                    "open":             day1_open,
                    "day2_open":        day2_open,
                    "catalyst":         "EARNINGS_DAY2",
                    "fgate_adj":        fgate_adj,
                    "fgate_flags":      fgate_flags,
                    "fgate_reasoning":  fgate_reason,
                },
                notes=(
                    f"Earnings beat D2 entry: gap {gap_pct:+.1f}% on {day1_date_str}, "
                    f"RVOL {rvol:.1f}x, Day1 held + close quality OK"
                    + (f" | {fgate_reason[:80]}" if fgate_reason else "")
                ),
            )
            signals.append(sig)
            await redis.delete(key)
            log.info("earnings_engine.day2_signal_fired",
                     symbol=symbol, day2_open=day2_open,
                     gap_pct=gap_pct, rvol=rvol, confidence=confidence)

        return signals

    @staticmethod
    def _validate_day1_close(
        symbol:             str,
        pending:            dict,
        day1_hold_pct:      float = 0.97,
        day1_close_quality: float = 0.5,
    ) -> tuple[bool, bool]:
        """Fetch Day 1 OHLC via yfinance, validate hold + close-quality conditions."""
        import yfinance as yf

        day1_date_str = pending.get("day1_date")
        if not day1_date_str:
            return True, True

        day1_date  = date.fromisoformat(day1_date_str)
        fetch_start = (day1_date - timedelta(days=2)).isoformat()
        fetch_end   = (day1_date + timedelta(days=1)).isoformat()

        ticker = "^NSEI" if symbol == "NIFTY 50" else f"{symbol}.NS"
        hist = yf.Ticker(ticker).history(
            start=fetch_start, end=fetch_end, interval="1d", auto_adjust=True
        )
        if hist is None or hist.empty:
            return True, True  # fail-open

        hist.columns = [c.lower() for c in hist.columns]
        for idx in hist.index:
            bar_date = idx.date() if hasattr(idx, "date") else idx
            if bar_date != day1_date:
                continue
            bar        = hist.loc[idx]
            day1_open  = pending.get("day1_open") or float(bar["open"])
            day1_close = float(bar["close"])
            day1_high  = float(bar["high"])
            day1_low   = float(bar["low"])

            held = day1_close >= day1_open * day1_hold_pct

            day1_range = day1_high - day1_low
            if day1_range > 0:
                close_pos   = (day1_close - day1_low) / day1_range
                quality_ok  = close_pos >= day1_close_quality
            else:
                quality_ok = True

            return held, quality_ok

        return True, True  # bar not found — fail-open

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

        # Day 2 mode: if pending signal already stored for today, skip re-queuing
        if await redis.get(f"earnings:pending_day2:{symbol}"):
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

        # ── Fundamental gate (only for BEAT — long-only mode) ────────────────
        # Claude evaluates quarterly revenue + PAT quality from screener.in.
        # Gate is fail-open: approves if screener.in / Claude unavailable.
        fgate_flags: list[str] = []
        fgate_reasoning: str   = ""
        fgate_adj: int         = 0
        if is_beat:
            try:
                from services.earnings_engine.fundamental_gate import evaluate_fundamental_gate
                fgate = await evaluate_fundamental_gate(symbol, gap_pct, rvol)
                fgate_flags    = fgate.red_flags
                fgate_reasoning = fgate.reasoning
                fgate_adj      = fgate.confidence_adj
                if not fgate.approve:
                    log.info(
                        "earnings_engine.fundamental_gate_rejected",
                        symbol    = symbol,
                        gap_pct   = round(gap_pct, 2),
                        red_flags = fgate.red_flags,
                        reasoning = fgate.reasoning,
                    )
                    return None
            except Exception as _fge:
                log.warning("earnings_engine.fundamental_gate_error",
                            symbol=symbol, error=str(_fge))

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

        # Apply fundamental gate confidence adjustment (clamped to valid range)
        if fgate_adj != 0:
            confidence = max(0, min(100, confidence + fgate_adj))

        # ── Day 2 entry mode (BEAT only) ─────────────────────────────────────
        # Store pending signal for next-morning validation instead of firing now.
        # MISS signals always fire Day 1 (no Day 2 equivalent for short-side).
        if is_beat:
            from config.settings import settings
            if settings.earnings_day2_mode:
                from zoneinfo import ZoneInfo
                today_str = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
                pending = {
                    "symbol":          symbol,
                    "day1_date":       today_str,
                    "gap_pct":         round(gap_pct, 2),
                    "rvol":            round(rvol, 2),
                    "confidence":      confidence,
                    "prev_close":      prev_close,
                    "day1_open":       open_price,
                    "fgate_adj":       fgate_adj,
                    "fgate_flags":     fgate_flags,
                    "fgate_reasoning": fgate_reasoning,
                }
                await redis.setex(
                    f"earnings:pending_day2:{symbol}",
                    36 * 3600,  # expires in 36h — covers overnight + Day 2 morning
                    json.dumps(pending),
                )
                log.info(
                    "earnings_engine.day1_pending_stored",
                    symbol     = symbol,
                    gap_pct    = round(gap_pct, 2),
                    rvol       = round(rvol, 2),
                    confidence = confidence,
                )
                return None  # will fire at Day 2 open after validation

        sig = Signal(
            trading_symbol  = symbol,
            timeframe       = "1day",
            signal_type     = signal_type,
            direction       = direction,
            confidence      = confidence,
            price_at_signal = current_price,
            indicators      = {
                "gap_pct":          round(gap_pct, 2),
                "rvol":             round(rvol, 2),
                "prev_close":       prev_close,
                "open":             open_price,
                "orb_high":         orb_high,
                "cum_volume":       cum_volume,
                "avg_volume":       int(avg_volume),
                "catalyst":         "EARNINGS",
                "fgate_adj":        fgate_adj,
                "fgate_flags":      fgate_flags,
                "fgate_reasoning":  fgate_reasoning,
            },
            notes=(
                f"Earnings {'beat' if is_beat else 'miss'}: gap {gap_pct:+.1f}%, "
                f"RVOL {rvol:.1f}x"
                + (f" | Fundamental: {fgate_reasoning[:80]}" if fgate_reasoning else "")
            ),
        )

        log.info(
            "earnings_engine.signal_found",
            symbol         = symbol,
            direction      = direction.value,
            gap_pct        = round(gap_pct, 2),
            rvol           = round(rvol, 2),
            confidence     = confidence,
            fgate_adj      = fgate_adj,
            fgate_approved = not bool(fgate_flags and not is_beat),
            orb_used       = orb_high is not None,
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
            # Buffer stores candles with "ts" key (both WS and preseed path)
            ts = candle.get("ts") or candle.get("timestamp") or candle.get("date")
            if ts is None:
                continue
            try:
                if isinstance(ts, str):
                    candle_dt = _dt.fromisoformat(ts).astimezone(IST)
                elif hasattr(ts, "astimezone"):
                    candle_dt = ts.astimezone(IST)
                elif hasattr(ts, "to_pydatetime"):
                    candle_dt = ts.to_pydatetime().astimezone(IST)
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
