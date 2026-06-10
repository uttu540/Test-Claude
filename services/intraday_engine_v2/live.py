"""
services/intraday_engine_v2/live.py
─────────────────────────────────────
Live adapter for the V2 intraday engine (two-sided 5-min Darvas box).

Backtest-validated 2024 (all-NSE, honest next-bar-open fills):
  H1 (tuned):   19 trades, 57.9% WR, PF 3.12, Sharpe 7.2
  H2 (holdout): 24 trades, 50.0% WR, PF 1.39, Sharpe 2.2
  Short side carried H2 — engine prints in down markets.

Live flow:
  1. job_intraday_v2_context (9:26 IST, main.py):
     computes universe breadth + market median from the first two 5-min candles,
     grades the day A/B/C, stores context in Redis. C day = engine off.
     Also appends each symbol's first-2-bar volume to its rolling RVOL baseline.
  2. detect() — called on each 5-min candle close (main.py):
     reads day context, applies RS / RVOL / room filters, runs detect_box5,
     returns an INTRADAY_V2_BOX Signal routed directly to TradeExecutor
     (bypasses the long-only swing pipeline — shorts allowed).

Live caps mirror backtest: max 5 trades/day, max 2/sector, 1/symbol/day.

Exit handling (phase 1): explicit_stop → RiskEngine; trade_lifecycle milestone
trail + 15:12 square-off. Backtest's scale-out-at-1.5R and 13:30 conditional
exit are phase 2 (needs partial-close support in the broker layer).
"""
from __future__ import annotations

import json
from collections import deque
from datetime import date, datetime, time, timedelta, timezone

import numpy as np
import pandas as pd
import structlog

from services.intraday_engine_v2.backtest import (
    BREADTH_A_LONG, BREADTH_A_SHORT, BREADTH_B_LONG, BREADTH_B_SHORT,
    ENTRY_CUTOFF, MAX_PER_SECTOR, MAX_STOP_PCT, MAX_TRADES_PER_DAY,
    MIN_AVG_DAY_VOL, MIN_PRICE, MIN_RVOL_FIRST2, MIN_STOP_PCT,
    ROOM_FACTOR, RS_MIN_ABS, SCALE_R, SIZE_A, SIZE_B,
    detect_box5, _load_sector_cache,
)
from services.technical_engine.signal_generator import Direction, Signal, SignalType

log = structlog.get_logger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

CONTEXT_KEY   = "intraday_v2:context"          # day context JSON (TTL to EOD)
COUNT_KEY     = "intraday_v2:count"            # trades fired today
SECTOR_KEY    = "intraday_v2:sector"           # per-sector counter hash
COOLDOWN_KEY  = "intraday_v2:fired"            # per-symbol per-day
BASELINE_KEY  = "intraday_v2:first2"           # rolling first-2-bar volume list
MIN_UNIVERSE  = 100                            # min symbols for trustworthy breadth


def _today_5min_df(buf_5m: deque, today: date) -> pd.DataFrame | None:
    """Build today's 5-min DataFrame from the candle buffer."""
    rows = []
    for c in buf_5m:
        ts = c.get("ts")
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
            "open":  float(c["open"]),  "high":   float(c["high"]),
            "low":   float(c["low"]),   "close":  float(c["close"]),
            "volume": float(c["volume"]),
        })
    if not rows:
        return None
    return pd.DataFrame(rows).set_index("timestamp").sort_index()


async def compute_day_context(candle_buffer: dict[str, dict[str, deque]], redis) -> dict | None:
    """
    9:26 IST job — universe breadth + market median from first two 5-min bars.
    Grades the day, stores context in Redis. Also rolls each symbol's
    first-2-bar volume into its 20-day RVOL baseline.
    """
    today = datetime.now(IST).date()
    changes: list[float] = []

    for sym, tfs in candle_buffer.items():
        if sym.startswith("NIFTY") or sym.endswith("VIX"):
            continue
        buf = tfs.get("5min")
        if not buf:
            continue
        day_df = _today_5min_df(buf, today)
        if day_df is None or len(day_df) < 2:
            continue
        op = float(day_df["open"].iloc[0])
        if op < MIN_PRICE or op <= 0:
            continue
        c925 = float(day_df["close"].iloc[1])
        changes.append((c925 - op) / op * 100)

        # Roll first-2-bar volume into the RVOL baseline (keep last 20 days)
        first2 = float(day_df["volume"].iloc[:2].sum())
        key = f"{BASELINE_KEY}:{sym}"
        try:
            await redis.rpush(key, first2)
            await redis.ltrim(key, -20, -1)
            await redis.expire(key, 40 * 86400)
        except Exception:
            pass

    if len(changes) < MIN_UNIVERSE:
        log.warning("v2_live.context_insufficient", universe=len(changes))
        return None

    arr      = np.array(changes)
    breadth  = float((arr > 0).mean())
    market   = float(np.median(arr))

    if breadth >= BREADTH_A_LONG:
        grade, size_mult, direction = "A", SIZE_A, "LONG"
    elif breadth >= BREADTH_B_LONG:
        grade, size_mult, direction = "B", SIZE_B, "LONG"
    elif breadth <= BREADTH_A_SHORT:
        grade, size_mult, direction = "A", SIZE_A, "SHORT"
    elif breadth <= BREADTH_B_SHORT:
        grade, size_mult, direction = "B", SIZE_B, "SHORT"
    else:
        grade, size_mult, direction = "C", 0.0, "NONE"

    ctx = {
        "date":       today.isoformat(),
        "breadth":    round(breadth, 3),
        "market_925": round(market, 3),
        "grade":      grade,
        "size_mult":  size_mult,
        "direction":  direction,
        "universe":   len(changes),
    }
    # TTL to 15:35 IST
    now = datetime.now(IST)
    eod = now.replace(hour=15, minute=35, second=0, microsecond=0)
    ttl = max(60, int((eod - now).total_seconds()))
    await redis.setex(CONTEXT_KEY, ttl, json.dumps(ctx))
    log.info("v2_live.context", **ctx)
    return ctx


class IntradayV2LiveEngine:
    """
    Live V2 detector — called on each 5-min candle close.

    Usage (from main.py):
        sig = await IntradayV2LiveEngine().detect(symbol, buf_5m, daily_buf, redis)
    """

    async def detect(
        self,
        symbol:    str,
        buf_5m:    deque,
        daily_buf: deque | None,
        redis,
    ) -> Signal | None:
        now_ist = datetime.now(IST)
        if now_ist.time() >= ENTRY_CUTOFF:
            return None

        # Day context — C day or missing = engine off
        ctx_raw = await redis.get(CONTEXT_KEY)
        if not ctx_raw:
            return None
        ctx = json.loads(ctx_raw)
        today = now_ist.date()
        if ctx.get("date") != today.isoformat() or ctx.get("grade") == "C":
            return None
        direction = ctx["direction"]

        # Cooldown: one trade per symbol per day
        if await redis.get(f"{COOLDOWN_KEY}:{symbol}:{today}"):
            return None

        # Day cap
        count_raw = await redis.get(f"{COUNT_KEY}:{today}")
        if count_raw and int(count_raw) >= MAX_TRADES_PER_DAY:
            return None

        day_df = _today_5min_df(buf_5m, today)
        if day_df is None or len(day_df) < 9:   # need ≥ box(6) + confirm + entry bars
            return None

        # Liquidity + daily stats from the daily candle buffer
        if not daily_buf or len(daily_buf) < 10:
            return None
        dlist = list(daily_buf)[-21:]
        avg_day_vol   = float(np.mean([c["volume"] for c in dlist]))
        avg_day_range = float(np.mean([c["high"] - c["low"] for c in dlist]))
        if avg_day_vol < MIN_AVG_DAY_VOL:
            return None

        op = float(day_df["open"].iloc[0])
        if op < MIN_PRICE:
            return None

        # RVOL (fix 8): rolling same-window baseline; fallback to daily-vol annualisation
        first2 = float(day_df["volume"].iloc[:2].sum())
        baseline_raw = await redis.lrange(f"{BASELINE_KEY}:{symbol}", 0, -1)
        if baseline_raw and len(baseline_raw) >= 5:
            base = float(np.mean([float(x) for x in baseline_raw[:-1] or baseline_raw]))
            rvol = first2 / base if base > 0 else 0.0
        else:
            rvol = (first2 * 37.5) / avg_day_vol if avg_day_vol > 0 else 0.0
        if rvol < MIN_RVOL_FIRST2:
            return None

        # RS at 9:25 vs market median (fix 2)
        if len(day_df) < 2:
            return None
        c925   = float(day_df["close"].iloc[1])
        rs_925 = (c925 - op) / op * 100 - ctx["market_925"]
        if direction == "LONG" and rs_925 < RS_MIN_ABS:
            return None
        if direction == "SHORT" and rs_925 > -RS_MIN_ABS:
            return None

        # Box detection — only fire on a confirmation that JUST happened
        # (last closed bar), otherwise we'd chase stale intrabar breaks.
        hit = detect_box5(day_df, direction)
        if hit is None:
            return None
        confirm_bar, _ref, stop_raw = hit
        if confirm_bar != len(day_df) - 1:
            return None   # confirmation is stale — happened on an earlier bar

        sign  = 1.0 if direction == "LONG" else -1.0
        entry = float(day_df["close"].iloc[-1])   # executor fills at market ≈ next bar open
        stop  = stop_raw
        risk  = sign * (entry - stop)
        if risk <= 0:
            return None
        stop_pct = risk / entry
        if stop_pct > MAX_STOP_PCT or stop_pct < MIN_STOP_PCT:
            return None
        if 2 * risk > ROOM_FACTOR * avg_day_range:
            return None

        # Sector cap (fix 7) — cached map only; unknown sectors share one bucket
        sector = _load_sector_cache().get(symbol, "UNKNOWN")
        sec_count = await redis.hget(f"{SECTOR_KEY}:{today}", sector)
        if sec_count and int(sec_count) >= MAX_PER_SECTOR:
            return None

        # Reserve slots atomically-ish before returning the signal
        pipe_day = await redis.incr(f"{COUNT_KEY}:{today}")
        await redis.expire(f"{COUNT_KEY}:{today}", 86400)
        if pipe_day > MAX_TRADES_PER_DAY:
            return None
        await redis.hincrby(f"{SECTOR_KEY}:{today}", sector, 1)
        await redis.expire(f"{SECTOR_KEY}:{today}", 86400)
        await redis.setex(f"{COOLDOWN_KEY}:{symbol}:{today}", 86400, "1")

        target = entry + sign * SCALE_R * risk
        conf   = _confidence(ctx, rs_925, rvol)

        log.info(
            "v2_live.signal",
            symbol=symbol, direction=direction, grade=ctx["grade"],
            rs=round(rs_925, 2), rvol=round(rvol, 1),
            entry=round(entry, 2), stop=round(stop, 2), target=round(target, 2),
        )

        return Signal(
            trading_symbol  = symbol,
            timeframe       = "5min",
            signal_type     = SignalType.INTRADAY_V2_BOX,
            direction       = Direction.BULLISH if direction == "LONG" else Direction.BEARISH,
            confidence      = conf,
            price_at_signal = round(entry, 2),
            entry_price     = round(entry, 2),
            stop_loss       = round(stop, 2),
            target          = round(target, 2),
            indicators      = {
                "explicit_stop": round(stop, 2),
                "rs_925":        round(rs_925, 2),
                "rvol":          round(rvol, 1),
                "day_grade":     ctx["grade"],
                "size_mult":     ctx["size_mult"],
                "breadth":       ctx["breadth"],
                "sector":        sector,
                "box_stop_pct":  round(stop_pct * 100, 2),
            },
            notes = (
                f"V2 BOX {direction} | grade {ctx['grade']} | RS {rs_925:+.1f} "
                f"| RVOL {rvol:.1f}× | stop {stop_pct*100:.1f}%"
            ),
        )


def _confidence(ctx: dict, rs: float, rvol: float) -> int:
    score = 68
    if ctx["grade"] == "A":
        score += 8
    if abs(rs) >= 2.0:
        score += 8
    elif abs(rs) >= 1.25:
        score += 4
    if rvol >= 5:
        score += 8
    elif rvol >= 3:
        score += 4
    return min(score, 92)
