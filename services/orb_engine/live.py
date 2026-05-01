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
TRADE_COST_PCT   = 0.15   # one-way slippage + brokerage (%)
# Increased from 0.05 → 0.15: ORB entry order lands at ~10:00 AM market price,
# not the 9:45 candle close. A live market order on a breakout stock routinely
# fills 0.1-0.2% above the breakout candle close. 0.05% was too optimistic.


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
        from config.settings import settings as _s
        if _s.uses_real_broker:
            log.warning("orb_live.nifty_missing", msg="No Nifty 15min data — blocking ORB in live mode")
            return False
        log.warning("orb_live.nifty_missing", msg="No Nifty 15min data in buffer — assuming trend-up (paper)")
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


def _backfill_today_from_yfinance(
    candle_buffer: dict[str, dict[str, deque]],
    symbols: list[str],
    today: date,
) -> None:
    """
    If bot restarted after 9:15 AM, live feed hasn't built today's candles yet.
    Fetch today's 15-min bars from yfinance for symbols missing today's data.
    Synchronous — called once at scan time, runs fast (only missing symbols).
    """
    import yfinance as yf

    missing = []
    for sym in symbols + ["NIFTY 50"]:
        buf = candle_buffer.get(sym, {}).get("15min")
        if not buf:
            missing.append(sym)
            continue
        has_today = any(
            (c["ts"].astimezone(IST) if hasattr(c["ts"], "tzinfo") and c["ts"].tzinfo else c["ts"]).date() == today
            for c in buf
        )
        if not has_today:
            missing.append(sym)

    if not missing:
        return

    log.info("orb_live.backfill_start", symbols=len(missing))
    for sym in missing:
        try:
            ticker = "^NSEI" if sym == "NIFTY 50" else f"{sym}.NS"
            df = yf.Ticker(ticker).history(period="1d", interval="15m", auto_adjust=True)
            if df is None or df.empty:
                continue
            df.columns = [c.lower() for c in df.columns]
            df = df[["open", "high", "low", "close", "volume"]].dropna()
            if sym not in candle_buffer:
                candle_buffer[sym] = {}
            if "15min" not in candle_buffer[sym]:
                candle_buffer[sym]["15min"] = deque(maxlen=300)
            for ts, row in df.iterrows():
                candle_buffer[sym]["15min"].append({
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]),   "close": float(row["close"]),
                    "volume": int(row["volume"]), "ts": ts,
                })
        except Exception as e:
            log.warning("orb_live.backfill_error", symbol=sym, error=str(e))
    log.info("orb_live.backfill_done", symbols=len(missing))


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

    # If bot restarted mid-morning, backfill today's candles from yfinance
    _backfill_today_from_yfinance(candle_buffer, symbols, today)

    # ── Nifty trend-day gate ──────────────────────────────────────────────────
    # Paper/dev: skip gate so trades execute end-to-end for validation.
    # Live/semi-auto: strict gate — ranging-day bypass has 30% WR in backtest.
    from config.settings import settings as _cfg
    nifty_ok = _nifty_trend_up(candle_buffer, today)
    if not nifty_ok:
        if _cfg.uses_real_broker:
            log.info("orb_live.blocked", reason="Nifty not trend-up — skipping all ORB scans")
            return []
        else:
            log.info("orb_live.nifty_gate_bypassed", reason="paper/dev mode — trading anyway")

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

        stop_distance = entry_price - or_low
        if stop_distance <= 0:
            continue

        # Real ATR from daily buffer (used for analytics/logging, NOT for stop sizing)
        real_atr = None
        daily_buf = candle_buffer.get(symbol, {}).get("1day")
        if daily_buf and len(daily_buf) >= 15:
            closes = [c["close"] for c in list(daily_buf)[-15:]]
            highs  = [c["high"]  for c in list(daily_buf)[-15:]]
            lows   = [c["low"]   for c in list(daily_buf)[-15:]]
            trs = [max(h - l, abs(h - pc), abs(l - pc))
                   for h, l, pc in zip(highs[1:], lows[1:], closes[:-1])]
            real_atr = round(sum(trs[-14:]) / 14, 2) if trs else None

        vol_ratio = round(volume / or_avg_vol, 2)

        # Dynamic confidence: base 72, scored on vol surge + OR range quality + ATR data.
        # All setups that reach here have already passed hard binary gates (range, volume,
        # Nifty trend-day). Confidence reflects *how good* the setup is, not whether it fires.
        _orb_conf = 72
        if vol_ratio >= 3.0:
            _orb_conf += 10    # exceptional volume surge
        elif vol_ratio >= 2.0:
            _orb_conf += 5     # strong surge
        # OR range in Goldilocks zone (0.5-1.5%): tight enough to avoid chaos days,
        # wide enough to give meaningful breakout momentum
        if 0.5 <= or_range_pct <= 1.5:
            _orb_conf += 8
        elif or_range_pct <= 0.8:
            _orb_conf -= 5     # very tight range — breakout may be noise
        if real_atr is not None:
            _orb_conf += 5     # real ATR available for proper stop sizing
        _orb_conf = max(65, min(_orb_conf, 95))

        sig = Signal(
            trading_symbol  = symbol,
            timeframe       = "15min",
            signal_type     = SignalType.ORB_BREAKOUT,
            direction       = Direction.BULLISH,
            confidence      = _orb_conf,
            price_at_signal = entry_price,
            indicators      = {
                "atr_14":       real_atr or round(stop_distance, 2),  # real ATR; fallback to range
                "explicit_stop": round(or_low, 2),  # RiskEngine reads this — no fake ATR needed
                "or_high":      round(or_high,      2),
                "or_low":       round(or_low,       2),
                "or_range_pct": round(or_range_pct, 2),
                "or_avg_vol":   int(or_avg_vol),
                "breakout_vol": int(volume),
                "rvol":         vol_ratio,
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
