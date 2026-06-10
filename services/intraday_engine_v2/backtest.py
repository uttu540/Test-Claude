"""
services/intraday_engine_v2/backtest.py
─────────────────────────────────────────
Intraday V2 — two-sided trend engine on 5-min candles.

Fixes all 8 gaps identified in the V1 review:
  1. SHORT SIDE      — mirror setups: gap-down / breakdown shorts
  2. RS FILTER       — stock % change since open vs universe median (market proxy)
  3. TREND-DAY HOLD  — 13:30 check: above VWAP + breadth holding → ride to 15:10
  4. SCALE-OUT       — half off at 2R, stop to breakeven, trail rest (3R→+1R, 5R→+2R, 8R→+3R)
  5. CONFIRMATION ENTRY — enter next-bar OPEN after a confirming close
                          (no limit-at-level adverse selection)
  6. DAY SCORE       — breadth at 9:25 → A/B/C day → size 1.0 / 0.6 / skip
                       breadth ≥65% = A-long, ≥58% = B-long, ≤35% = A-short, ≤42% = B-short
  7. SECTOR CAP      — max 2 entries per sector per day (yfinance sector cache)
  8. CLEAN RVOL      — first-2-bar volume vs SAME stock's 20-day avg first-2-bar volume

Market context is derived INTERNALLY from the universe (no index data needed):
  market proxy = median % change since open across liquid universe
  breadth      = fraction of universe trading above its open

Setup (both directions, watching from 9:25, last entry 14:00):
  BOX — Darvas box: session extreme holds ≥ 6 bars (30 min), range < 1.2%.
        Confirming close beyond box edge + volume → enter next bar open.
        (ORB5 was tested and removed — 1 trade in all of 2024, no edge.)

Risk:
  stop distance capped at 2.0% of entry, min 0.25%
  min-room check: 2× risk must fit within 70% of 20-day avg daily range
  ₹1,00,000 per trade (full size), × 0.6 on B days
  max 5 entries/day, max 2 per sector, 1 trade per symbol per day

Data: ~/.cache/fvg_5min/{symbol}_{start}_{end}.pkl (Kite 5-min OHLCV, IST tz-aware)
"""
from __future__ import annotations

import json
import pickle
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import structlog

log = structlog.get_logger(__name__)

IST          = timezone(timedelta(hours=5, minutes=30))
_CACHE_5MIN  = Path.home() / ".cache" / "fvg_5min"
_SECTOR_FILE = Path.home() / ".cache" / "sector_map.json"

POSITION_INR   = 100_000
ENTRY_COST     = 0.0010   # 0.05% brokerage+stt + 0.05% slippage (market entry on confirmation)
EXIT_COST      = 0.0005

# Liquidity / quality
MIN_PRICE        = 50.0
MIN_AVG_DAY_VOL  = 100_000     # shares
MIN_RVOL_FIRST2  = 2.0         # fix 8: first-2-bar vol vs 20d avg first-2-bar vol

# Day scoring (fix 6)
BREADTH_A_LONG  = 0.65
BREADTH_B_LONG  = 0.58
BREADTH_A_SHORT = 0.35
BREADTH_B_SHORT = 0.42
SIZE_A, SIZE_B  = 1.0, 0.6

# RS filter (fix 2)
RS_MIN_ABS = 0.75   # stock must out/under-perform market proxy by ≥ 0.25% at signal bar

# Gap alignment — catalyst context: longs need a gap-up open, shorts a gap-down
MIN_GAP_ALIGN = 0.0   # % gap vs prev close, in the trade direction

# Setup params
VOL_CONFIRM_MULT = 1.5
BOX_MIN_BARS     = 6      # 30 min on 5-min bars
BOX_MAX_RANGE    = 0.012  # 1.2%
ENTRY_CUTOFF     = time(14, 0)
SCAN_START_BAR   = 2      # first scannable bar = 9:25 (bars 0,1 form the OR)

# Risk
MAX_STOP_PCT  = 0.012
MIN_STOP_PCT  = 0.0025
ROOM_FACTOR   = 0.70   # 2×risk must fit in 70% of 20d avg daily range
MAX_TRADES_PER_DAY = 5
MAX_PER_SECTOR     = 2

# Exits (fixes 3+4)
SCALE_R       = 1.5    # take half at 2R, stop → BE
TRAIL_LEVELS  = [(8.0, 3.0), (5.0, 2.0), (3.0, 1.0)]   # (peak_r ≥ X → lock +Y R)
NORMAL_EXIT   = time(13, 30)
TREND_EXIT    = time(15, 10)
HOLD_BREADTH_LONG  = 0.60   # 13:30 breadth still ≥ 60% → trend day, hold longs
HOLD_BREADTH_SHORT = 0.40


# ─── Data classes ────────────────────────────────────────────────────────────

@dataclass
class TradeV2:
    symbol:      str
    setup:       str        # ORB5 | BOX
    direction:   str        # LONG | SHORT
    trade_date:  date
    entry_time:  datetime
    entry_price: float
    stop_price:  float
    exit_price:  float      # blended (scale-out aware)
    exit_time:   datetime
    exit_reason: str
    day_grade:   str        # A | B
    size_mult:   float
    sector:      str
    rs_at_entry: float
    pnl_inr:     float
    pnl_pct:     float
    r_multiple:  float
    winner:      bool


@dataclass
class Candidate:
    symbol:      str
    setup:       str
    direction:   str
    entry_bar:   int        # bar index of ENTRY (next bar after confirmation)
    entry_time:  datetime
    entry_price: float
    stop_price:  float
    rs:          float
    score:       float      # for ranking when > day cap


# ─── Sector cache (fix 7) ────────────────────────────────────────────────────

def _load_sector_cache() -> dict[str, str]:
    if _SECTOR_FILE.exists():
        try:
            return json.loads(_SECTOR_FILE.read_text())
        except Exception:
            return {}
    return {}


def _fetch_sectors(symbols: list[str]) -> dict[str, str]:
    """Fetch sector for symbols via yfinance, persist to cache. UNKNOWN on failure."""
    cache = _load_sector_cache()
    missing = [s for s in symbols if s not in cache]
    if missing:
        import yfinance as yf

        def one(sym: str) -> tuple[str, str]:
            try:
                info = yf.Ticker(f"{sym}.NS").info
                return sym, info.get("sector") or "UNKNOWN"
            except Exception:
                return sym, "UNKNOWN"

        log.info("v2_bt.fetching_sectors", count=len(missing))
        with ThreadPoolExecutor(max_workers=8) as pool:
            for sym, sector in pool.map(one, missing):
                cache[sym] = sector
        try:
            _SECTOR_FILE.write_text(json.dumps(cache))
        except Exception:
            pass
    return cache


# ─── Setup detectors ─────────────────────────────────────────────────────────

def _avg_vol(vols: np.ndarray, idx: int, lookback: int = 20) -> float:
    start = max(0, idx - lookback)
    if idx <= start:
        return 0.0
    return float(vols[start:idx].mean())


def _compute_vwap(day_df: pd.DataFrame) -> np.ndarray:
    tp  = (day_df["high"].values + day_df["low"].values + day_df["close"].values) / 3
    vol = day_df["volume"].values.astype(float)
    cum_vol = np.cumsum(vol)
    cum_tpv = np.cumsum(tp * vol)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(cum_vol > 0, cum_tpv / cum_vol, tp)


def detect_box5(
    day_df: pd.DataFrame, direction: str,
) -> Optional[tuple[int, float, float]]:
    """
    Darvas box on 5-min bars, both directions.
    LONG: session high holds ≥ BOX_MIN_BARS, compact box, confirming close above.
    SHORT: mirror on session low.
    Returns (confirm_bar_idx, entry_ref_price, stop_price) or None.
    """
    n = len(day_df)
    if n < BOX_MIN_BARS + 3:
        return None
    highs  = day_df["high"].values.astype(float)
    lows   = day_df["low"].values.astype(float)
    closes = day_df["close"].values.astype(float)
    vols   = day_df["volume"].values.astype(float)
    vwap   = _compute_vwap(day_df)

    box_top, box_low, box_start = highs[0], lows[0], 0

    for i in range(1, n - 1):
        ts = day_df.index[i]
        if ts.time() >= ENTRY_CUTOFF:
            return None
        bars_held = i - box_start

        if direction == "LONG":
            if highs[i] > box_top:
                if closes[i] > box_top and bars_held >= BOX_MIN_BARS:
                    box_range = (box_top - box_low) / box_top
                    av = float(np.median(vols[2:i])) if i > 2 else 0.0
                    if (box_range <= BOX_MAX_RANGE and av > 0
                            and vols[i] >= 1.3 * av
                            and closes[i] > float(vwap[i])):
                        return i, closes[i], box_low
                box_top, box_low, box_start = highs[i], lows[i], i
            else:
                if lows[i] < box_low * 0.993:
                    box_top, box_low, box_start = highs[i], lows[i], i
                else:
                    box_low = min(box_low, lows[i])
        else:
            if lows[i] < box_low:
                if closes[i] < box_low and bars_held >= BOX_MIN_BARS:
                    box_range = (box_top - box_low) / box_low
                    av = float(np.median(vols[2:i])) if i > 2 else 0.0
                    if (box_range <= BOX_MAX_RANGE and av > 0
                            and vols[i] >= 1.3 * av
                            and closes[i] < float(vwap[i])):
                        return i, closes[i], box_top
                box_top, box_low, box_start = highs[i], lows[i], i
            else:
                if highs[i] > box_top * 1.007:
                    box_top, box_low, box_start = highs[i], lows[i], i
                else:
                    box_top = max(box_top, highs[i])
    return None


# ─── Exit simulation (fixes 3 + 4) ───────────────────────────────────────────

def simulate_exit_v2(
    day_df:      pd.DataFrame,
    entry_bar:   int,
    entry_price: float,
    stop_price:  float,
    direction:   str,
    breadth_1330: float,
) -> tuple[float, datetime, str, float]:
    """
    Scale-out + milestone trail + trend-day hold.
    Returns (blended_exit_price, exit_time, reason, r_multiple_on_full_position).

    LONG (SHORT mirrors):
      half off at entry + 2R → stop to breakeven
      trail: peak ≥3R → +1R | ≥5R → +2R | ≥8R → +3R
      13:30: above VWAP + breadth ≥ 60% → hold to 15:10, else exit
    Stop checked before scale within the same bar (conservative).
    """
    sign = 1.0 if direction == "LONG" else -1.0
    risk = sign * (entry_price - stop_price)
    if risk <= 0:
        return entry_price, day_df.index[entry_bar], "INVALID", 0.0

    vwap = _compute_vwap(day_df)
    n    = len(day_df)

    scale_level = entry_price + sign * SCALE_R * risk
    scaled      = False
    scale_px    = 0.0
    trail_stop  = stop_price
    peak_r      = 0.0
    hold_mode   = None   # decided at 13:30

    def blended(exit_px: float) -> float:
        if scaled:
            return 0.5 * scale_px + 0.5 * exit_px
        return exit_px

    def r_mult(blend_px: float) -> float:
        return round(sign * (blend_px - entry_price) / risk, 2)

    for i in range(entry_bar, n):
        ts   = day_df.index[i]
        high = float(day_df["high"].iloc[i])
        low  = float(day_df["low"].iloc[i])
        op   = float(day_df["open"].iloc[i])
        cl   = float(day_df["close"].iloc[i])

        # ── time exits ────────────────────────────────────────────────────
        if hold_mode is None and ts.time() >= NORMAL_EXIT:
            above_vwap = cl > float(vwap[i]) if direction == "LONG" else cl < float(vwap[i])
            breadth_ok = (breadth_1330 >= HOLD_BREADTH_LONG if direction == "LONG"
                          else breadth_1330 <= HOLD_BREADTH_SHORT)
            if above_vwap and breadth_ok:
                hold_mode = True          # trend day — ride to 15:10
            else:
                px = op * (1 - sign * EXIT_COST)
                b  = blended(px)
                return b, ts, "TIME_EXIT_1330", r_mult(b)
        if hold_mode and ts.time() >= TREND_EXIT:
            px = op * (1 - sign * EXIT_COST)
            b  = blended(px)
            return b, ts, "TREND_DAY_EXIT_1510", r_mult(b)

        # ── stop (conservative: check before favourable fills) ───────────
        stop_hit = low <= trail_stop if direction == "LONG" else high >= trail_stop
        if stop_hit:
            px = trail_stop * (1 - sign * EXIT_COST)
            b  = blended(px)
            reason = "TRAIL_STOP" if sign * (trail_stop - stop_price) > 0 else "STOP"
            return b, ts, reason, r_mult(b)

        # ── scale-out at 2R ───────────────────────────────────────────────
        fav_extreme = high if direction == "LONG" else low
        if not scaled and sign * (fav_extreme - scale_level) >= 0:
            scaled    = True
            scale_px  = scale_level * (1 - sign * EXIT_COST)
            trail_stop = max(trail_stop, entry_price) if direction == "LONG" \
                         else min(trail_stop, entry_price)   # breakeven

        # ── milestone trail on remainder ──────────────────────────────────
        cur_r = sign * (fav_extreme - entry_price) / risk
        if cur_r > peak_r:
            peak_r = cur_r
            for lvl, lock in TRAIL_LEVELS:
                if peak_r >= lvl:
                    new_trail = entry_price + sign * lock * risk
                    trail_stop = max(trail_stop, new_trail) if direction == "LONG" \
                                 else min(trail_stop, new_trail)
                    break

    # EOD safety (shouldn't happen — time exits fire first)
    last_px = float(day_df["close"].iloc[-1]) * (1 - sign * EXIT_COST)
    b = blended(last_px)
    return b, day_df.index[-1], "EOD", r_mult(b)


# ─── Engine ──────────────────────────────────────────────────────────────────

class IntradayV2Engine:
    """
    Day-centric two-sided engine.

    Per day:
      1. Universe context at 9:25: breadth + median move (market proxy)
      2. Day grade A/B/C → direction bias + size multiplier (C = no trades)
      3. Candidates: liquidity + RVOL(first-2-bar vs 20d same-window avg) + RS
      4. Detect ORB5 / BOX setups in the biased direction only
      5. Caps: 5/day, 2/sector, 1/symbol — chronological, then by RS strength
      6. Simulate scale-out + trail + trend-day-hold exits
    """

    def __init__(
        self,
        symbols:    list[str],
        start_date: date,
        end_date:   date,
        workers:    int = 8,
    ) -> None:
        self._symbols = symbols
        self._start   = start_date
        self._end     = end_date
        self._workers = workers

    # ── Data loading ──────────────────────────────────────────────────────

    def _find_cache(self, symbol: str) -> Path | None:
        files = list(_CACHE_5MIN.glob(f"{symbol}_*.pkl"))
        return files[0] if files else None

    def _load_symbol(self, symbol: str):
        cache = self._find_cache(symbol)
        if not cache:
            return None
        try:
            with open(cache, "rb") as f:
                df = pickle.load(f)
            if df.empty or len(df) < 100:
                return None
            if df.index.tz is None:
                df.index = df.index.tz_localize("Asia/Kolkata")
            else:
                df.index = df.index.tz_convert("Asia/Kolkata")

            grouped   = df.groupby(df.index.date)
            all_dates = sorted(grouped.groups.keys())

            daily_vol   = df["volume"].groupby(df.index.date).sum()
            day_ranges  = (df["high"].groupby(df.index.date).max()
                           - df["low"].groupby(df.index.date).min())
            day_closes  = df["close"].groupby(df.index.date).last()

            # first-2-bar volume per day (fix 8 baseline)
            first2 = df.groupby(df.index.date)["volume"].apply(lambda s: float(s.iloc[:2].sum()))

            meta: dict[date, dict] = {}
            for i, d in enumerate(all_dates):
                if i < 6:
                    continue
                lb = slice(max(0, i - 20), i)
                meta[d] = {
                    "avg_day_vol":   float(daily_vol.iloc[lb].mean()),
                    "avg_day_range": float(day_ranges.iloc[lb].mean()),
                    "avg_first2":    float(first2.iloc[lb].mean()),
                    "prev_close":    float(day_closes.iloc[i - 1]),
                }

            day_dfs: dict[date, pd.DataFrame] = {}
            for d in all_dates:
                if self._start <= d <= self._end and d in meta:
                    g = grouped.get_group(d)
                    if len(g) >= 30:
                        day_dfs[d] = g
            if not day_dfs:
                return None
            return symbol, day_dfs, meta
        except Exception:
            return None

    # ── Run ───────────────────────────────────────────────────────────────

    def load(self) -> tuple[dict, dict]:
        """Load all symbol data once — reusable across param sweeps."""
        log.info("v2_bt.loading", symbols=len(self._symbols))
        data:  dict[str, dict[date, pd.DataFrame]] = {}
        metas: dict[str, dict[date, dict]]          = {}
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            for res in pool.map(self._load_symbol, self._symbols):
                if res:
                    sym, day_dfs, meta = res
                    data[sym]  = day_dfs
                    metas[sym] = meta
        log.info("v2_bt.loaded", count=len(data))
        return data, metas

    def run(self, preloaded: tuple[dict, dict] | None = None) -> list[TradeV2]:
        data, metas = preloaded if preloaded else self.load()

        all_dates: set[date] = set()
        for dd in data.values():
            all_dates.update(dd.keys())
        trading_days = sorted(all_dates)

        # Phase 1: collect candidates per day
        day_candidates: dict[date, list[Candidate]] = {}
        day_context:    dict[date, dict]            = {}

        for tdate in trading_days:
            cands, ctx = self._process_day(tdate, data, metas)
            if cands:
                day_candidates[tdate] = cands
                day_context[tdate]    = ctx

        # Phase 2: sectors for signal symbols only
        sig_syms = sorted({c.symbol for cl in day_candidates.values() for c in cl})
        sectors  = _fetch_sectors(sig_syms) if sig_syms else {}

        # Phase 3: apply caps chronologically, simulate exits
        trades: list[TradeV2] = []
        for tdate, cands in sorted(day_candidates.items()):
            ctx = day_context[tdate]
            cands.sort(key=lambda c: (c.entry_time, -c.score))
            taken: list[Candidate] = []
            sector_count: dict[str, int] = {}
            for c in cands:
                if len(taken) >= MAX_TRADES_PER_DAY:
                    break
                sec = sectors.get(c.symbol, "UNKNOWN")
                if sector_count.get(sec, 0) >= MAX_PER_SECTOR:
                    continue
                sector_count[sec] = sector_count.get(sec, 0) + 1
                taken.append(c)

            for c in taken:
                day_df = data[c.symbol][tdate]
                exit_px, exit_ts, reason, r_mul = simulate_exit_v2(
                    day_df, c.entry_bar, c.entry_price, c.stop_price,
                    c.direction, ctx["breadth_1330"],
                )
                size = POSITION_INR * ctx["size_mult"]
                qty  = int(size / c.entry_price)
                if qty < 1:
                    continue
                sign = 1.0 if c.direction == "LONG" else -1.0
                pnl  = sign * (exit_px - c.entry_price) * qty
                trades.append(TradeV2(
                    symbol      = c.symbol,
                    setup       = c.setup,
                    direction   = c.direction,
                    trade_date  = tdate,
                    entry_time  = c.entry_time,
                    entry_price = round(c.entry_price, 2),
                    stop_price  = round(c.stop_price, 2),
                    exit_price  = round(exit_px, 2),
                    exit_time   = exit_ts,
                    exit_reason = reason,
                    day_grade   = ctx["grade"],
                    size_mult   = ctx["size_mult"],
                    sector      = sectors.get(c.symbol, "UNKNOWN"),
                    rs_at_entry = round(c.rs, 2),
                    pnl_inr     = round(pnl, 2),
                    pnl_pct     = round(sign * (exit_px - c.entry_price) / c.entry_price * 100, 3),
                    r_multiple  = r_mul,
                    winner      = pnl > 0,
                ))
        return sorted(trades, key=lambda t: (t.trade_date, t.entry_time))

    # ── Per-day processing ────────────────────────────────────────────────

    def _process_day(
        self,
        tdate: date,
        data:  dict[str, dict[date, pd.DataFrame]],
        metas: dict[str, dict[date, dict]],
    ) -> tuple[list[Candidate], dict]:
        # Universe snapshot: % change since open at bar 1 close (9:25) and 13:30
        chg_925:  dict[str, float] = {}
        chg_1330: dict[str, float] = {}
        eligible: dict[str, dict]  = {}

        for sym, day_dfs in data.items():
            day_df = day_dfs.get(tdate)
            if day_df is None:
                continue
            m = metas[sym].get(tdate)
            if not m or m["avg_day_vol"] < MIN_AVG_DAY_VOL:
                continue
            op = float(day_df["open"].iloc[0])
            if op < MIN_PRICE or op <= 0:
                continue
            c925 = float(day_df["close"].iloc[1])
            chg_925[sym] = (c925 - op) / op * 100

            # 13:30 snapshot for breadth-hold decision
            try:
                pos_1330 = day_df.index.searchsorted(
                    day_df.index[0].replace(hour=13, minute=30))
                pos_1330 = min(pos_1330, len(day_df) - 1)
                c1330 = float(day_df["close"].iloc[pos_1330])
                chg_1330[sym] = (c1330 - op) / op * 100
            except Exception:
                pass

            # RVOL fix 8: first-2-bar vol vs 20d avg first-2-bar vol
            first2 = float(day_df["volume"].iloc[:2].sum())
            if m["avg_first2"] <= 0 or first2 / m["avg_first2"] < MIN_RVOL_FIRST2:
                continue
            eligible[sym] = m

        if len(chg_925) < 100:     # not enough universe data to trust breadth
            return [], {}

        arr_925  = np.array(list(chg_925.values()))
        breadth  = float((arr_925 > 0).mean())
        market_925 = float(np.median(arr_925))
        arr_1330 = np.array(list(chg_1330.values())) if chg_1330 else arr_925
        breadth_1330 = float((arr_1330 > 0).mean())

        # Day grade (fix 6)
        if breadth >= BREADTH_A_LONG:
            grade, size_mult, direction = "A", SIZE_A, "LONG"
        elif breadth >= BREADTH_B_LONG:
            grade, size_mult, direction = "B", SIZE_B, "LONG"
        elif breadth <= BREADTH_A_SHORT:
            grade, size_mult, direction = "A", SIZE_A, "SHORT"
        elif breadth <= BREADTH_B_SHORT:
            grade, size_mult, direction = "B", SIZE_B, "SHORT"
        else:
            return [], {}          # C day — no edge, stand aside

        ctx = {
            "grade": grade, "size_mult": size_mult, "direction": direction,
            "breadth": breadth, "breadth_1330": breadth_1330,
            "market_925": market_925,
        }

        # Candidates: RS-ranked, biased direction only (fix 2 + fix 1)
        cands: list[Candidate] = []
        for sym, m in eligible.items():
            day_df = data[sym][tdate]
            rs_925 = chg_925.get(sym, 0.0) - market_925
            if direction == "LONG" and rs_925 < RS_MIN_ABS:
                continue
            if direction == "SHORT" and rs_925 > -RS_MIN_ABS:
                continue

            # Gap alignment: catalyst context in the trade direction
            op0 = float(day_df["open"].iloc[0])
            gap_pct = (op0 - m["prev_close"]) / m["prev_close"] * 100 if m["prev_close"] > 0 else 0.0
            if direction == "LONG" and gap_pct < MIN_GAP_ALIGN:
                continue
            if direction == "SHORT" and gap_pct > -MIN_GAP_ALIGN:
                continue

            for setup, detector in (("BOX", detect_box5),):
                hit = detector(day_df, direction)
                if hit is None:
                    continue
                confirm_bar, _ref_px, stop_raw = hit
                entry_bar = confirm_bar + 1
                if entry_bar >= len(day_df):
                    continue
                ts_entry = day_df.index[entry_bar]
                if ts_entry.time() >= ENTRY_CUTOFF:
                    continue

                sign  = 1.0 if direction == "LONG" else -1.0
                entry = float(day_df["open"].iloc[entry_bar]) * (1 + sign * ENTRY_COST)
                stop  = stop_raw * (1 - sign * 0.0005)
                risk  = sign * (entry - stop)
                if risk <= 0:
                    continue
                stop_pct = risk / entry
                if stop_pct > MAX_STOP_PCT or stop_pct < MIN_STOP_PCT:
                    continue
                # Min-room check: 2× risk must fit in 70% of avg daily range
                if 2 * risk > ROOM_FACTOR * m["avg_day_range"]:
                    continue

                cands.append(Candidate(
                    symbol      = sym,
                    setup       = setup,
                    direction   = direction,
                    entry_bar   = entry_bar,
                    entry_time  = ts_entry,
                    entry_price = entry,
                    stop_price  = stop,
                    rs          = rs_925,
                    score       = abs(rs_925),
                ))
                break   # one setup per symbol per day — first hit wins

        return cands, ctx


# ─── Report ──────────────────────────────────────────────────────────────────

def summarize(trades: list[TradeV2]) -> dict:
    if not trades:
        return {"trades": 0}
    pnls    = np.array([t.pnl_inr for t in trades])
    wins    = pnls[pnls > 0]
    losses  = pnls[pnls <= 0]
    daily: dict[date, float] = {}
    for t in trades:
        daily[t.trade_date] = daily.get(t.trade_date, 0.0) + t.pnl_inr
    dvals  = np.array(list(daily.values()))
    sharpe = float(dvals.mean() / dvals.std() * np.sqrt(252)) if dvals.std() > 0 else 0.0
    eq     = np.cumsum(pnls)
    dd     = float((np.maximum.accumulate(eq) - eq).max())
    return {
        "trades":     len(trades),
        "win_rate":   round(len(wins) / len(trades) * 100, 1),
        "pnl_inr":    round(float(pnls.sum()), 0),
        "avg_win":    round(float(wins.mean()), 0) if len(wins) else 0,
        "avg_loss":   round(float(losses.mean()), 0) if len(losses) else 0,
        "profit_factor": round(float(wins.sum() / -losses.sum()), 2) if losses.sum() < 0 else float("inf"),
        "sharpe":     round(sharpe, 2),
        "max_dd_inr": round(dd, 0),
        "long":       sum(1 for t in trades if t.direction == "LONG"),
        "short":      sum(1 for t in trades if t.direction == "SHORT"),
        "by_setup":   {s: sum(1 for t in trades if t.setup == s) for s in {"ORB5", "BOX"}},
        "by_exit":    {r: sum(1 for t in trades if t.exit_reason == r)
                       for r in sorted({t.exit_reason for t in trades})},
    }
