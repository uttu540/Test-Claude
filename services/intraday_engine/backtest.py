"""
services/intraday_engine/backtest.py
──────────────────────────────────────
Multi-strategy intraday backtest engine — Gap Trading Edition.

Strategies (designed for gap+RVOL scanner profile):
  1. GAP_HOLD     — Strong first candle (top-50% close, holds 70%+ gap) → enter 9:30 open
  2. GAP_PULLBACK — Gap opens, dips below 9:15 high (shakeout), reclaims with volume surge

Scanner pre-filter (daily, before strategies run):
  gap ≥ 3%  |  RVOL ≥ 3×  |  gap holds ≥ 60%  |  top-15 by gap×RVOL score

Exit logic:
  R-milestone trail: <2R hold stop | 2R→BE | 3R→+1R | 5R→+2R | 8R→+3R
  Hard time exit: 13:30 IST  (small/mid cap momentum dies afternoon)

Data source: ~/.cache/orb_kite/{symbol}_{start}_{end}.pkl (Kite 15min OHLCV)
Position size: ₹1,00,000 per trade
"""
from __future__ import annotations

import pickle
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional

import bisect

import numpy as np
import pandas as pd
import structlog

log = structlog.get_logger(__name__)

IST           = timezone(timedelta(hours=5, minutes=30))
_CACHE_DIR    = Path.home() / ".cache" / "orb_kite"      # 15-min cache
_CACHE_5MIN   = Path.home() / ".cache" / "fvg_5min"      # 5-min cache for FVG_ORB
POSITION_INR = 100_000     # ₹1L per trade
TRADE_COST   = 0.0005      # 0.05% each side
TIME_EXIT    = time(13, 30)  # hard exit 1:30 PM — catalyst momentum sustains 3-4h

# Volume lookback for "average volume" baseline
VOL_LOOKBACK_CANDLES = 20

# Scanner constants — gap strategies
SCANNER_TOP_N       = 15     # stocks to trade per day
SCANNER_MIN_GAP_PCT = 1.5    # min gap % — catalyst gaps can be small (Dixon: 1.15%)
SCANNER_MIN_RVOL    = 5.0    # min RVOL — high bar = proxy for catalyst/event
SCANNER_GAP_HOLD    = 0.50   # first candle must hold 50% of gap (relaxed for catalyst)
CANDLES_PER_DAY     = 26     # 15-min candles 9:15-15:30
MAX_STOP_PCT        = 0.025  # max stop distance from entry: 2.5% (catalyst stocks wider)

# Wide scanner constants — IDARVAS / VWAP_RCL (no gap needed)
WIDE_SCANNER_MIN_RVOL = 1.5  # any elevated-volume day
WIDE_TOP_N            = 50   # wider pool — these strategies are more selective

# Strategy routing: which strategies use gap scanner vs wide scanner
# IDARVAS and VWAP_RCL are gap strategies — they need the catalyst gap context;
# the gap scanner provides pre-filtered high-quality gap stocks where boxes form cleanly.
GAP_STRATEGIES  = {"GAP_HOLD", "GAP_PULLBACK", "FVG_ORB", "IDARVAS", "VWAP_RCL"}
WIDE_STRATEGIES: set[str] = set()   # reserved for future non-gap strategies

# ─── Data classes ────────────────────────────────────────────────────────────

@dataclass
class IntradayTrade:
    symbol:      str
    strategy:    str
    trade_date:  date
    entry_time:  datetime
    entry_price: float
    stop_price:  float
    exit_price:  float
    exit_time:   datetime
    exit_reason: str      # STOP | TRAIL_STOP | TARGET | TIME_EXIT
    pnl_inr:     float
    pnl_pct:     float
    r_multiple:  float    # final R achieved at exit
    winner:      bool


# ─── Signal detectors ────────────────────────────────────────────────────────

def _avg_vol(df: pd.DataFrame, idx: int) -> float:
    """Average volume over last VOL_LOOKBACK_CANDLES candles before idx."""
    start = max(0, idx - VOL_LOOKBACK_CANDLES)
    return float(df["volume"].iloc[start:idx].mean()) if idx > 0 else 0.0


def _scanner_score(
    day_df:     pd.DataFrame,
    prev_close: float,
    avg_daily_vol: float,   # pre-computed 20d avg daily total volume
) -> Optional[float]:
    """
    Daily scanner: score = gap_pct × rvol_factor.
    Returns None if hard filters not met.

    Hard filters:
      - gap ≥ SCANNER_MIN_GAP_PCT (2%)
      - first candle holds ≥ 60% of gap (gap strength)
      - RVOL: today's first 2 candles annualised vs 20d avg daily vol ≥ 3×
    """
    if day_df.empty or prev_close <= 0 or len(day_df) < 3 or avg_daily_vol <= 0:
        return None

    open_price = float(day_df.iloc[0]["open"])
    gap_pct    = (open_price - prev_close) / prev_close * 100
    if gap_pct < SCANNER_MIN_GAP_PCT:
        return None

    # Gap hold: first candle low must not fill >40% of gap
    gap_size  = open_price - prev_close
    first_low = float(day_df.iloc[0]["low"])
    gap_fill  = open_price - first_low
    if gap_size > 0 and gap_fill > (1 - SCANNER_GAP_HOLD) * gap_size:
        return None

    # RVOL: annualise first 2 candles to full day, compare vs 20d avg daily vol
    first_2_vol     = float(day_df.iloc[:2]["volume"].sum())
    annualised_vol  = first_2_vol * (CANDLES_PER_DAY / 2)
    rvol            = annualised_vol / avg_daily_vol
    if rvol < SCANNER_MIN_RVOL:
        return None

    return round(gap_pct * rvol, 3)


def _scanner_score_wide(
    day_df: pd.DataFrame,
    avg_daily_vol: float,
) -> Optional[float]:
    """
    Wide scanner for IDARVAS/VWAP_RCL: RVOL-only, no gap needed.
    Returns RVOL score or None if below WIDE_SCANNER_MIN_RVOL.
    """
    if day_df.empty or avg_daily_vol <= 0 or len(day_df) < 3:
        return None
    first_2_vol    = float(day_df.iloc[:2]["volume"].sum())
    annualised_vol = first_2_vol * (CANDLES_PER_DAY / 2)
    rvol           = annualised_vol / avg_daily_vol
    if rvol < WIDE_SCANNER_MIN_RVOL:
        return None
    return round(rvol, 3)


def _compute_vwap(day_df: pd.DataFrame) -> np.ndarray:
    """Rolling VWAP from session start. Returns array aligned to day_df index."""
    tp      = (day_df["high"].values + day_df["low"].values + day_df["close"].values) / 3
    vol     = day_df["volume"].values.astype(float)
    cum_vol = np.cumsum(vol)
    cum_tpv = np.cumsum(tp * vol)
    with np.errstate(invalid="ignore", divide="ignore"):
        vwap = np.where(cum_vol > 0, cum_tpv / cum_vol, tp)
    return vwap


def _get_nifty_trend(trade_date: date) -> bool:
    """
    True if Nifty is trending up on this date.
    9:30 candle close > 9:15 candle high → bullish trend.
    Uses yfinance ^NSEI 15-min data (cached in memory via module-level dict).
    """
    return _nifty_trend_cache.get(trade_date, True)  # default True if unavailable


_nifty_trend_cache: dict[date, bool] = {}


def _load_nifty_trend(start: date, end: date) -> None:
    """
    Pre-load Nifty trend for date range using yfinance daily data.
    Trend day = Nifty daily close > previous day close AND daily range > 0.3%.
    Daily data available for any historical period (unlike 15m which caps at 60d).
    """
    global _nifty_trend_cache
    try:
        import yfinance as yf
        df = yf.download(
            "^NSEI",
            start=(start - timedelta(days=5)).isoformat(),  # extra for prev close
            end=(end + timedelta(days=1)).isoformat(),
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
        if df.empty:
            return

        closes = df["Close"].squeeze()
        highs  = df["High"].squeeze()
        lows   = df["Low"].squeeze()

        for i in range(1, len(df)):
            tdate      = df.index[i].date()
            prev_close = float(closes.iloc[i - 1])
            today_close = float(closes.iloc[i])
            today_high  = float(highs.iloc[i])
            today_low   = float(lows.iloc[i])
            day_range   = (today_high - today_low) / prev_close * 100
            # Trend up = close higher than prev close AND range > 0.3% (not a flat day)
            _nifty_trend_cache[tdate] = (today_close > prev_close) and (day_range > 0.3)

        log.info("nifty_trend_loaded", days=len(_nifty_trend_cache))
    except Exception as e:
        log.warning("nifty_trend_load_failed", error=str(e))


def detect_gap_hold(day_df: pd.DataFrame, prev_close: float) -> Optional[dict]:
    """
    GAP_HOLD (Catalyst Sustained Volume): gap + two-candle sustained buying.

    Scanner ensures gap ≥ 1.5% + RVOL ≥ 5×. Strategy checks *quality*:
    1. First candle (9:15): bullish (close in top 50% of range), gap holds ≥ 60%
    2. Second candle (9:30): VOLUME still elevated (≥ 60% of c0 volume) AND
       close ≥ c0_close (no reversal) — "sustained accumulation" not a spike
    3. Entry at 9:30 close. Stop: 9:15 candle LOW.

    Sustained volume is the key catalyst signal:
    - One-candle RVOL spike → often panic buy / retail chase → fades
    - Two candles of high vol → institutional accumulation → continues
    Dixon-type earnings gaps show 5-8× RVOL in BOTH 9:15 and 9:30 candles.
    """
    if len(day_df) < 2 or prev_close <= 0:
        return None

    c0 = day_df.iloc[0]   # 9:15 candle
    c1 = day_df.iloc[1]   # 9:30 candle

    open_price = float(c0["open"])
    gap_size   = open_price - prev_close
    if gap_size <= 0:
        return None

    c0_high   = float(c0["high"])
    c0_low    = float(c0["low"])
    c0_close  = float(c0["close"])
    c0_volume = float(c0["volume"])

    # Rule 1: bullish first candle — close in top 50% of the candle's range
    c0_range = c0_high - c0_low
    if c0_range > 0 and c0_close < (c0_low + 0.5 * c0_range):
        return None   # bearish / doji first candle — gap losing steam

    # Rule 2: gap still alive — first candle close holds ≥ 60% of gap
    gap_remaining = c0_close - prev_close
    if gap_remaining < 0.60 * gap_size:
        return None   # gap filling too fast

    # Rule 3: sustained volume — 9:30 candle volume ≥ 60% of 9:15 candle volume.
    # One-candle spikes (c1_vol << c0_vol) = retail chasing open, not real catalyst.
    # Two-candle elevated volume = institutional accumulation continues = catalyst.
    c1_close  = float(c1["close"])
    c1_volume = float(c1["volume"])
    if c0_volume > 0 and c1_volume < 0.60 * c0_volume:
        return None   # volume dried up — no sustained buying, skip

    # Rule 4: 9:30 candle must hold (close ≥ c0_close — no reversal)
    if c1_close < c0_close:
        return None   # 9:30 candle reversed below 9:15 close — gap fading

    entry_price = c1_close * (1 + TRADE_COST)
    stop_price  = c0_low   * (1 - TRADE_COST)

    if entry_price <= stop_price:
        return None

    return {
        "entry_price":  entry_price,
        "stop_price":   stop_price,
        "entry_time":   day_df.index[1],
        "gap_pct":      round(gap_size / prev_close * 100, 2),
        "c0_hold_pct":  round(gap_remaining / gap_size * 100, 1),
    }


def detect_gap_pullback(day_df: pd.DataFrame, prev_close: float) -> Optional[dict]:
    """
    GAP_PULLBACK: gap opens, shakes out weak hands, reclaims with volume surge.

    Scanner already ensured gap ≥ 3% + RVOL ≥ 3×. Strategy checks pattern:
    1. After 9:15 candle, stock DIPS: any candle closes BELOW 9:15 high
       (weak-hand shakeout — this is the setup trigger)
    2. Track lowest low during dip phase (becomes the stop)
    3. RECLAIM: a candle closes back ABOVE 9:15 high with volume ≥ 1.5× avg
       (strong hands stepping in — strong confirmation)
    4. Entry window: must reclaim before 11:00 (late reclaims = weak/failed)
    5. Stop: lowest low during dip (invalidation if broken again)

    Design rationale: gaps often dip back to test the open/9:15 level. Stocks
    that reclaim with volume surge show real accumulation. Stop is tight (just
    below the pullback low), giving excellent R:R on a clean setup.
    """
    if len(day_df) < 4 or prev_close <= 0:
        return None

    c0        = day_df.iloc[0]
    c0_close  = float(c0["close"])   # first candle's close = reference level

    open_price = float(c0["open"])
    gap_pct    = (open_price - prev_close) / prev_close * 100
    if gap_pct <= 0:
        return None

    closes = day_df["close"].values
    lows   = day_df["low"].values
    vols   = day_df["volume"].values
    n      = len(day_df)

    dipped       = False
    pullback_low = float("inf")

    for i in range(1, n):
        ts = day_df.index[i]
        if ts.hour >= 12:
            break   # too late — gap momentum has decayed by noon

        if not dipped:
            # Dip: close below 9:15 candle CLOSE (stock pulled back from where it ended)
            if float(closes[i]) < c0_close:
                dipped       = True
                pullback_low = float(lows[i])
            continue

        # In dip phase: track lowest low
        pullback_low = min(pullback_low, float(lows[i]))

        # Reclaim: close back above 9:15 CLOSE with at least light volume confirmation
        if float(closes[i]) > c0_close:
            avg_v = _avg_vol(day_df, i)
            if avg_v > 0 and float(vols[i]) < 1.5 * avg_v:
                # Weak reclaim (low vol) — not convinced, reset and keep watching
                dipped       = False
                pullback_low = float("inf")
                continue

            # Quality filter: pullback depth must be < 0.5% of c0_close.
            # Deep pullbacks (>0.5%) mean wide stop → poor R:R.
            # Shallow pullbacks = tight stop = genuine momentum dip, not reversal.
            pullback_depth_pct = (c0_close - pullback_low) / c0_close
            if pullback_depth_pct > 0.007:
                dipped       = False
                pullback_low = float("inf")
                continue

            entry_price = float(closes[i]) * (1 + TRADE_COST)
            stop_price  = pullback_low * (1 - TRADE_COST)

            if entry_price <= stop_price:
                continue   # stop too close / inverted

            return {
                "entry_price":      entry_price,
                "stop_price":       stop_price,
                "entry_time":       ts,
                "gap_pct":          round(gap_pct, 2),
                "c0_close":         round(c0_close, 2),
                "pullback_low":     round(pullback_low, 2),
                "pullback_depth_pct": round(pullback_depth_pct * 100, 3),
            }

    return None


def detect_fvg_orb(day_df: pd.DataFrame, prev_close: float) -> Optional[dict]:
    """
    FVG_ORB: Opening Range breakout confirmed by ICT Fair Value Gap.

    Works on 15-min candles (adapted from 5-min ICT strategy).

    Rules:
      1. c0 (9:15) = Opening Range — mark OR_high, OR_low
      2. BREAKOUT: first candle (9:30+) that closes above OR_high (bullish)
         Must have volume ≥ 1.5× avg (real conviction, not noise)
         Must happen before 11:00 AM (early session momentum only)
      3. FVG CONFIRMATION: after breakout candle B, wait for next candle C:
         Check: c[B-1].high < c[C].low → FVG zone = [c[B-1].high, c[C].low]
         FVG must be 0.15–2.0% of price (meaningful, not noise, not too wild)
      4. RETEST ENTRY: first subsequent candle whose low ≤ FVG_high AND
         close ≥ FVG_low (price entered FVG zone and rejected = bullish)
         Entry = FVG_high (limit order at top of gap) or candle close if it
         opened below FVG_high (gap-down into zone)
      5. Stop = FVG_low (below the entire gap)
         Retest must happen before 12:00 PM

    Why this works: The FVG is an imbalance zone — price moved so fast that
    buyers left untraded levels behind. When price returns to the gap, those
    same buyers re-enter (defend their cost basis). Tight stop = the FVG size.
    Catalyst stocks (earnings/sector) create clean FVGs that hold well.
    """
    if len(day_df) < 4:
        return None

    c0     = day_df.iloc[0]
    or_high = float(c0["high"])
    n       = len(day_df)

    for i in range(1, n - 1):      # B = breakout candle; need i+1 for candle C
        c_b      = day_df.iloc[i]
        b_close  = float(c_b["close"])
        b_volume = float(c_b["volume"])
        b_ts     = day_df.index[i]

        # Breakout: close above OR high, before 11 AM
        if b_close <= or_high:
            continue
        if b_ts.hour >= 11:
            break

        # Volume: breakout must have conviction
        avg_v = _avg_vol(day_df, i)
        if avg_v > 0 and b_volume < 1.5 * avg_v:
            continue

        # FVG: candle A (i-1), candle B (i), candle C (i+1)
        c_a    = day_df.iloc[i - 1]
        c_c    = day_df.iloc[i + 1]
        a_high = float(c_a["high"])
        c_low  = float(c_c["low"])

        if a_high >= c_low:
            # Candles A and C overlap — no gap, no FVG
            break   # first breakout found; if no FVG here, stop (take first only)

        fvg_high = c_low    # top of FVG zone  = C's low
        fvg_low  = a_high   # bottom of FVG zone = A's high
        fvg_pct  = (fvg_high - fvg_low) / fvg_high * 100

        # FVG size filter: tight gaps only — meaningful but not too wide
        # Tight FVG = tight stop = good R:R. Wide FVG = weak signal.
        if fvg_pct < 0.15 or fvg_pct > 1.0:
            break

        # Scan from i+2 for the retest (price pulling back into FVG zone)
        for j in range(i + 2, n):
            r_c     = day_df.iloc[j]
            j_ts    = day_df.index[j]
            r_open  = float(r_c["open"])
            r_low   = float(r_c["low"])
            r_close = float(r_c["close"])
            r_high  = float(r_c["high"])

            # Entry window: must retest before noon
            if j_ts.hour >= 12:
                break

            # STRONG retest: candle dipped into FVG zone AND closed BACK above FVG_high
            # "Touched the gap then fully rejected" = strong institutional defence.
            # Weak condition (r_close >= fvg_low only) causes too many false entries.
            if r_low <= fvg_high and r_close >= fvg_high:
                # Entry at FVG_high — the top of the gap (price rejected from here)
                entry_price = fvg_high * (1 + TRADE_COST)
                stop_price  = fvg_low  * (1 - TRADE_COST)

                if entry_price <= stop_price:
                    break

                return {
                    "entry_price": entry_price,
                    "stop_price":  stop_price,
                    "entry_time":  j_ts,
                    "or_high":     round(or_high, 2),
                    "fvg_high":    round(fvg_high, 2),
                    "fvg_low":     round(fvg_low, 2),
                    "fvg_pct":     round(fvg_pct, 3),
                }

            # FVG mitigated — price closed below FVG lower edge → setup invalid
            if r_close < fvg_low:
                break

        # Only take first valid breakout — if FVG confirmed or failed, stop scanning
        break

    return None


def detect_idarvas(day_df: pd.DataFrame, prev_close: float) -> Optional[dict]:
    """
    IDARVAS: Intraday Darvas box breakout on gap stocks.

    Designed for gap stocks (already filtered by gap scanner).

    Box — tracks the ROLLING SESSION HIGH:
      1. Session high tracked from 9:15; every new session high resets the box
      2. Box confirmed: session high holds for ≥ MIN_BOX_BARS candles
      3. Box quality: (box_top − box_low) / box_top < MAX_BOX_RANGE (compact range)
      4. Entry trigger: candle HIGH exceeds box_top + close above box_top (no wick-only break)
         Entry = box_top × (1 + TRADE_COST)  ← limit at box_top, NOT candle close
      5. Stop = box_low × (1 − TRADE_COST)
      6. Volume ≥ 1.3× 20-bar avg at trigger candle
      7. VWAP: close > VWAP (trending up, not reversing)
      8. Gap still intact: close > session open (gap not filling below open)

    Limit-order entry is key: buying AT box_top (not at inflated candle close)
    keeps the stop/risk ratio tight, which is what drives the high R:R.
    """
    n = len(day_df)
    if n < 10 or prev_close <= 0:
        return None

    open_price = float(day_df.iloc[0]["open"])
    if open_price <= prev_close:
        return None

    vwap   = _compute_vwap(day_df)
    highs  = day_df["high"].values.astype(float)
    lows   = day_df["low"].values.astype(float)
    closes = day_df["close"].values.astype(float)
    vols   = day_df["volume"].values.astype(float)

    MIN_BOX_BARS  = 5     # box must hold ≥5 candles (75 min)
    MAX_BOX_RANGE = 0.015 # box must be compact: < 1.5% range

    box_top   = highs[0]
    box_low   = lows[0]
    box_start = 0

    for i in range(1, n):
        ts = day_df.index[i]
        if ts.time() >= TIME_EXIT:
            break

        bars_held = i - box_start

        if highs[i] > box_top:
            # Candle broke above session high
            if (closes[i] > box_top                    # close above (not just wick)
                    and bars_held >= MIN_BOX_BARS):
                # Check box compactness
                box_range = (box_top - box_low) / box_top
                if box_range <= MAX_BOX_RANGE:
                    avg_v    = _avg_vol(day_df, i)
                    vwap_val = float(vwap[i])
                    # Volume + VWAP + gap still intact (close above session open)
                    if (avg_v > 0 and vols[i] >= 1.3 * avg_v
                            and closes[i] > vwap_val
                            and closes[i] > open_price):
                        entry_price = box_top * (1 + TRADE_COST)   # limit at box_top
                        stop_price  = box_low * (1 - TRADE_COST)
                        if (entry_price > stop_price
                                and (entry_price - stop_price) / entry_price <= MAX_STOP_PCT):
                            return {
                                "entry_price": entry_price,
                                "stop_price":  stop_price,
                                "entry_time":  ts,
                                "box_top":     round(box_top, 2),
                                "box_low":     round(box_low, 2),
                                "bars_held":   bars_held,
                                "box_range":   round(box_range * 100, 2),
                            }
            # Reset box from new session high
            box_top   = highs[i]
            box_low   = lows[i]
            box_start = i
        else:
            new_low = lows[i]
            if new_low < box_low * 0.993:
                # Significant breakdown — reset
                box_top   = highs[i]
                box_low   = new_low
                box_start = i
            else:
                box_low = min(box_low, new_low)

    return None


def detect_vwap_rcl(day_df: pd.DataFrame, prev_close: float) -> Optional[dict]:
    """
    VWAP_RCL: Price pulls back to VWAP then reclaims it with volume.

    Conditions:
      1. Gap up day: open > prev_close (intraday momentum context)
      2. First 2 candles trade ABOVE VWAP (uptrend established)
      3. Pullback: a candle closes BELOW VWAP (shakeout) — must happen before 11:30
      4. Reclaim: subsequent candle closes ABOVE VWAP with vol ≥ 1.5× avg
      5. Entry = reclaim close; Stop = lowest low during pullback phase
      6. Pullback must be shallow: pullback_low > entry * (1 - MAX_STOP_PCT)
      7. Max 1 trade per day (first valid setup)
    """
    n = len(day_df)
    if n < 5 or prev_close <= 0:
        return None

    vwap   = _compute_vwap(day_df)
    closes = day_df["close"].values.astype(float)
    lows   = day_df["low"].values.astype(float)
    vols   = day_df["volume"].values.astype(float)

    # Gap up: open ≥ 1.5% above prev_close (gap stock context)
    open_price = float(day_df.iloc[0]["open"])
    if prev_close > 0 and (open_price - prev_close) / prev_close < 0.015:
        return None

    # First 2 candles must be above VWAP (early uptrend established)
    if closes[0] <= vwap[0] or closes[1] <= vwap[1]:
        return None

    in_pullback  = False
    pullback_low = float("inf")

    for i in range(2, n):
        ts       = day_df.index[i]
        vwap_val = float(vwap[i])

        if ts.time() >= TIME_EXIT:
            break

        if not in_pullback:
            # Pullback must start before 11:30 (early-session shakeout)
            if ts.hour >= 12:
                break
            if closes[i] < vwap_val:
                in_pullback  = True
                pullback_low = lows[i]
        else:
            pullback_low = min(pullback_low, lows[i])

            if closes[i] > vwap_val:
                avg_v = _avg_vol(day_df, i)
                if avg_v <= 0 or vols[i] >= 1.5 * avg_v:
                    entry_price = closes[i] * (1 + TRADE_COST)
                    stop_price  = pullback_low * (1 - TRADE_COST)
                    if (entry_price > stop_price
                            and (entry_price - stop_price) / entry_price <= MAX_STOP_PCT):
                        return {
                            "entry_price":   entry_price,
                            "stop_price":    stop_price,
                            "entry_time":    ts,
                            "vwap_at_entry": round(vwap_val, 2),
                            "pullback_low":  round(pullback_low, 2),
                        }
                # Weak reclaim — reset, keep scanning for better setup
                in_pullback  = False
                pullback_low = float("inf")

    return None


# ─── Exit simulation ─────────────────────────────────────────────────────────

def _simulate_exit(
    day_df:        pd.DataFrame,
    entry_time:    datetime,
    entry_price:   float,
    stop_price:    float,
    trail_start_r: float = 2.0,   # R-multiple at which trail begins (default: 2R→BE)
) -> tuple[float, datetime, str, float]:
    """
    Simulate exit using R-milestone trail + time exit at 14:30.

    Returns: (exit_price, exit_time, exit_reason, r_multiple)

    R-milestones (intraday — validated vs ORB 2024):
      <2R  → hold original stop
      2R   → trail to breakeven
      3R   → trail to +1R
      5R   → trail to +2R
    """
    after      = day_df[day_df.index > entry_time]
    init_risk  = entry_price - stop_price
    if init_risk <= 0:
        return entry_price, entry_time, "INVALID", 0.0

    trail_stop  = stop_price
    highest     = entry_price
    peak_r      = 0.0

    for ts, row in after.iterrows():
        high = float(row["high"])
        low  = float(row["low"])

        # Time exit
        if ts.time() >= TIME_EXIT:
            exit_p = float(row["open"])
            r_mul  = (exit_p - entry_price) / init_risk
            return exit_p * (1 - TRADE_COST), ts, "TIME_EXIT", round(r_mul, 2)

        # Update trailing stop from new high
        if high > highest:
            highest = high
            r_mul   = (highest - entry_price) / init_risk
            ts0 = trail_start_r           # e.g. 2.0 (default) or 3.0
            if r_mul >= ts0 + 6:
                new_trail = entry_price + 3 * init_risk   # big runner → lock +3R
            elif r_mul >= ts0 + 3:
                new_trail = entry_price + 2 * init_risk
            elif r_mul >= ts0 + 1:
                new_trail = entry_price + 1 * init_risk
            elif r_mul >= ts0:
                new_trail = entry_price   # breakeven
            else:
                new_trail = stop_price
            trail_stop = max(trail_stop, new_trail)
            peak_r     = max(peak_r, r_mul)

        # Check stop hit
        if low <= trail_stop:
            reason = "TRAIL_STOP" if trail_stop > stop_price else "STOP"
            exit_p = trail_stop * (1 - TRADE_COST)
            r_mul  = (exit_p - entry_price) / init_risk
            return exit_p, ts, reason, round(r_mul, 2)

    # EOD
    last   = after.iloc[-1] if not after.empty else None
    if last is None:
        return entry_price, entry_time, "EOD", 0.0
    exit_p = float(last["close"]) * (1 - TRADE_COST)
    r_mul  = (exit_p - entry_price) / init_risk
    return exit_p, after.index[-1], "EOD", round(r_mul, 2)


# ─── Engine ──────────────────────────────────────────────────────────────────

class IntradayBacktestEngine:
    """
    Date-centric backtest engine with daily scanner gate.

    Flow each day:
      1. Score all available symbols via _scanner_score()
      2. Filter: gap ≥ 3%, RVOL ≥ 3×, gap holds ≥ 60%
      3. Nifty trend gate: skip bearish days (optional)
      4. Take top SCANNER_TOP_N by score (gap% × RVOL)
      5. Run GAP_HOLD + GAP_PULLBACK on those symbols only
    """

    def __init__(
        self,
        symbols:     list[str],
        start_date:  date,
        end_date:    date,
        strategies:  list[str] | None = None,
        workers:     int = 8,
        top_n:       int = SCANNER_TOP_N,
        wide_top_n:  int = WIDE_TOP_N,
        nifty_gate:    bool = True,
        use_5min:      bool = False,   # use 5-min cache for FVG_ORB (needs seed_5min_cache.py)
        trail_start_r: float = 2.0,   # R at which trail kicks in (2=default, 3=give more room)
    ) -> None:
        self._symbols      = symbols
        self._start        = start_date
        self._end          = end_date
        self._strategies   = strategies or ["GAP_HOLD", "GAP_PULLBACK"]
        self._workers      = workers
        self._top_n        = top_n
        self._wide_top_n   = wide_top_n
        self._nifty_gate   = nifty_gate
        self._cache_dir    = _CACHE_5MIN if use_5min else _CACHE_DIR
        self._trail_start_r = trail_start_r

        # Split strategies by scanner type
        self._gap_strategies  = [s for s in self._strategies if s in GAP_STRATEGIES]
        self._wide_strategies = [s for s in self._strategies if s in WIDE_STRATEGIES]

    def _find_cache(self, symbol: str) -> Path | None:
        for p in self._cache_dir.glob(f"{symbol}_*.pkl"):
            parts = p.stem.split("_")
            if len(parts) >= 3:
                try:
                    c_start = date.fromisoformat(parts[-2])
                    c_end   = date.fromisoformat(parts[-1])
                    if c_start <= self._start and c_end >= self._end:
                        return p
                except ValueError:
                    pass
        files = list(self._cache_dir.glob(f"{symbol}_*.pkl"))
        return files[0] if files else None

    def _load_symbol(
        self, symbol: str
    ) -> tuple[str, dict[date, pd.DataFrame], dict[date, float], list[date]] | None:
        """
        Load symbol pkl + precompute everything needed for O(1) hot-loop access:
          - day_dfs:        {date → day_df}          — O(1) lookup per day
          - avg_vol_by_date:{date → 20d avg daily vol} — O(1) RVOL calc
          - trading_dates:  sorted list of dates in backtest range

        All expensive ops (groupby, rolling, boolean masks) happen ONCE here,
        not 246 times inside the day loop.
        """
        cache = self._find_cache(symbol)
        if not cache:
            return None
        try:
            with open(cache, "rb") as f:
                df = pickle.load(f)
            if df.empty or len(df) < 20:
                return None
            if df.index.tz is not None:
                df.index = df.index.tz_convert("Asia/Kolkata")

            # Group by date ONCE → dict for O(1) day access
            grouped   = df.groupby(df.index.date)
            all_dates = sorted(grouped.groups.keys())

            # Rolling 20d avg daily volume (pre-range, needed for lookback)
            daily_vol = df["volume"].groupby(df.index.date).sum()
            avg_vol_by_date: dict[date, float] = {}
            for i, d in enumerate(all_dates):
                if i < 5:
                    continue
                lookback = daily_vol.iloc[max(0, i - 20): i]
                avg_vol_by_date[d] = float(lookback.mean())

            # Build day_dfs only for backtest range
            day_dfs: dict[date, pd.DataFrame] = {}
            trading_dates: list[date] = []
            for d in all_dates:
                if d < self._start or d > self._end:
                    continue
                day_df = grouped.get_group(d)
                if len(day_df) >= 8:
                    day_dfs[d] = day_df
                    trading_dates.append(d)

            if not day_dfs:
                return None

            return (symbol, day_dfs, avg_vol_by_date, trading_dates)
        except Exception:
            return None

    def _execute_strategy(
        self,
        symbol:    str,
        tdate:     date,
        day_df:    pd.DataFrame,
        prev_close: float,
        strategies: list[str] | None = None,
    ) -> list[IntradayTrade]:
        trades = []
        strat_list = strategies if strategies is not None else self._strategies
        for strat in strat_list:
            sig = None
            if strat == "GAP_HOLD" and prev_close > 0:
                sig = detect_gap_hold(day_df, prev_close)
            elif strat == "GAP_PULLBACK" and prev_close > 0:
                sig = detect_gap_pullback(day_df, prev_close)
            elif strat == "FVG_ORB":
                sig = detect_fvg_orb(day_df, prev_close)
            elif strat == "IDARVAS":
                sig = detect_idarvas(day_df, prev_close)
            elif strat == "VWAP_RCL":
                sig = detect_vwap_rcl(day_df, prev_close)

            if sig is None:
                continue

            entry_price = sig["entry_price"]
            stop_price  = sig["stop_price"]
            entry_time  = sig["entry_time"]

            if entry_price <= stop_price or entry_price <= 0:
                continue

            # Max stop cap: skip if stop > 2.5% below entry (risk too wide)
            if (entry_price - stop_price) / entry_price > MAX_STOP_PCT:
                continue

            qty = int(POSITION_INR / entry_price)
            if qty < 1:
                continue

            exit_price, exit_time, exit_reason, r_mul = _simulate_exit(
                day_df, entry_time, entry_price, stop_price,
                trail_start_r=self._trail_start_r,
            )

            pnl_inr = (exit_price - entry_price) * qty
            pnl_pct = (exit_price - entry_price) / entry_price * 100

            trades.append(IntradayTrade(
                symbol      = symbol,
                strategy    = strat,
                trade_date  = tdate,
                entry_time  = entry_time,
                entry_price = round(entry_price, 2),
                stop_price  = round(stop_price, 2),
                exit_price  = round(exit_price, 2),
                exit_time   = exit_time,
                exit_reason = exit_reason,
                pnl_inr     = round(pnl_inr, 2),
                pnl_pct     = round(pnl_pct, 4),
                r_multiple  = r_mul,
                winner      = pnl_inr > 0,
            ))
        return trades

    def run(self) -> list[IntradayTrade]:
        # ── Step 1: load all symbol data in parallel ───────────────────────
        log.info("intraday_bt.loading_data", symbols=len(self._symbols))
        # symbol_data: sym → {date → day_df}  — O(1) day access, no hot-loop masking
        symbol_data:   dict[str, dict[date, pd.DataFrame]] = {}
        symbol_avgvol: dict[str, dict[date, float]]        = {}
        symbol_dates:  dict[str, list[date]]               = {}
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            for result in pool.map(self._load_symbol, self._symbols):
                if result:
                    sym, day_dfs, avg_vol_by_date, trad_dates = result
                    symbol_data[sym]   = day_dfs
                    symbol_avgvol[sym] = avg_vol_by_date
                    symbol_dates[sym]  = trad_dates

        log.info("intraday_bt.loaded", count=len(symbol_data))

        # ── Step 2: load Nifty trend ───────────────────────────────────────
        if self._nifty_gate:
            log.info("intraday_bt.loading_nifty_trend")
            _load_nifty_trend(self._start, self._end)

        # ── Step 3: collect all trading days ──────────────────────────────
        all_dates: set[date] = set()
        for day_dfs in symbol_data.values():
            all_dates.update(day_dfs.keys())   # keys are already dates
        trading_days = sorted(all_dates)        # already filtered to range during load

        # ── Step 4: process day by day with scanner gate ───────────────────
        all_trades: list[IntradayTrade] = []

        for tdate in trading_days:
            # Nifty trend gate
            if self._nifty_gate and not _get_nifty_trend(tdate):
                continue

            # Score all symbols for today (both gap and wide scanners)
            gap_scores:   dict[str, float] = {}
            wide_scores:  dict[str, float] = {}
            prev_closes:  dict[str, float] = {}

            for sym, dates_in_df in symbol_dates.items():
                day_df = symbol_data[sym].get(tdate)
                if day_df is None:
                    continue

                # Prev close via binary search on sorted dates
                day_idx    = bisect.bisect_left(dates_in_df, tdate)
                prev_close = 0.0
                if day_idx > 0:
                    prev_date   = dates_in_df[day_idx - 1]
                    prev_day_df = symbol_data[sym].get(prev_date)
                    if prev_day_df is not None and not prev_day_df.empty:
                        prev_close = float(prev_day_df["close"].iloc[-1])

                avg_daily_vol = symbol_avgvol[sym].get(tdate, 0.0)
                if avg_daily_vol <= 0:
                    continue

                prev_closes[sym] = prev_close

                if self._gap_strategies:
                    score = _scanner_score(day_df, prev_close, avg_daily_vol)
                    if score is not None:
                        gap_scores[sym] = score

                if self._wide_strategies:
                    wscore = _scanner_score_wide(day_df, avg_daily_vol)
                    if wscore is not None:
                        wide_scores[sym] = wscore

            # ── Gap strategies: top-N by gap×RVOL ─────────────────────────
            if self._gap_strategies and gap_scores:
                top_gap = sorted(gap_scores, key=gap_scores.__getitem__, reverse=True)[:self._top_n]
                for sym in top_gap:
                    day_df = symbol_data[sym].get(tdate)
                    if day_df is None:
                        continue
                    pc     = prev_closes.get(sym, 0.0)
                    trades = self._execute_strategy(sym, tdate, day_df.copy(), pc,
                                                    strategies=self._gap_strategies)
                    all_trades.extend(trades)

            # ── Wide strategies: top-N by RVOL only ───────────────────────
            if self._wide_strategies and wide_scores:
                top_wide = sorted(wide_scores, key=wide_scores.__getitem__, reverse=True)[:self._wide_top_n]
                for sym in top_wide:
                    day_df = symbol_data[sym].get(tdate)
                    if day_df is None:
                        continue
                    pc     = prev_closes.get(sym, 0.0)
                    trades = self._execute_strategy(sym, tdate, day_df.copy(), pc,
                                                    strategies=self._wide_strategies)
                    all_trades.extend(trades)

        return sorted(all_trades, key=lambda t: (t.trade_date, t.symbol, t.strategy))
