"""
services/earnings_engine/backtest.py
─────────────────────────────────────
Backtests the earnings gap-and-go signal on historical data.

Two modes:
  scan         — scan ALL days for gap+RVOL events regardless of earnings calendar.
                 Answers: "is the gap+RVOL signal itself predictive?"
  announcement — fetch NSE announcement dates, test ONLY on days symbols reported.
                 Answers: "does earnings context improve signal quality?"

Entry:   open price of signal day (gap already happened at open — no look-ahead)
Stop:    max(entry - stop_mult×ATR14, prev_close×1.01) — never inside the gap
Target:  entry + target_mult×ATR14
Exit:    check each forward bar's low vs trailing_stop, high vs target.
         Trailing stop tightens once price hits 1:1 (entry + stop_mult×ATR).
ATR:     14-day true range average computed from bars before signal date.

Filters vs original:
  • Gap 3–12%     (was 1.5%; <3% noise, >12% circuit risk)
  • RVOL scales   (3× for 3–7% gaps, 5× for >7% gaps)
  • Pre-trend cap (skip if stock up >25% in prior 60 bars — buy-the-rumor risk)
  • Stop = gap floor (never stop inside the gap on day 1)
  • Trailing stop after 1:1 hit
  • Max hold 15 days (was 5; PEAD drift peaks days 5–20)

Usage:
    python -m services.earnings_engine.run_backtest --start 2022-01-01 --end 2026-04-30
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

# ── Constants ─────────────────────────────────────────────────────────────────

MIN_GAP_PCT      = 3.0    # raised from 1.5 — sub-3% gaps are noise
MAX_GAP_PCT      = 12.0   # new — >12% risks circuit-breaker trap on NSE
STOP_ATR_MULT    = 1.5
TARGET_ATR_MULT  = 3.0
MAX_HOLD_DAYS    = 15     # raised from 5 — capture PEAD drift days 5–20
WARMUP_BARS      = 65     # need 60 for pre-trend check + 14 for ATR + buffer
PRETREND_LOOKBACK = 60    # bars to look back for overextension check
PRETREND_MAX_GAIN = 0.25  # skip if stock up >25% in prior 60 bars


def _rvol_threshold(gap_pct: float) -> float:
    """RVOL minimum scales with gap size — large gaps on thin volume reverse."""
    if gap_pct >= 7.0:
        return 5.0
    return 3.0   # 3–7% gap range


def _is_overextended(df: pd.DataFrame, i: int) -> bool:
    """True if stock gained >25% in prior 60 bars — buy-the-rumor risk."""
    if i < PRETREND_LOOKBACK:
        return False
    start_close = float(df.iloc[i - PRETREND_LOOKBACK]["close"])
    current_open = float(df.iloc[i]["open"])
    if start_close <= 0:
        return False
    return (current_open - start_close) / start_close > PRETREND_MAX_GAIN


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class EarningsBacktestTrade:
    symbol:       str
    date:         date
    gap_pct:      float
    rvol:         float
    confidence:   int        # 68 / 74 / 80
    entry:        float
    stop:         float
    target:       float
    exit_price:   float
    exit_reason:  str        # STOP / TARGET / MAX_HOLD
    exit_day:     int
    pnl_pct:      float
    is_win:       bool
    overextended: bool       # flagged but passed (for analysis)


# ── Core engine ───────────────────────────────────────────────────────────────

class EarningsBacktestEngine:
    """
    Scans historical daily OHLCV for earnings gap-and-go setups.
    Call run() with a list of symbols and a date range.
    """

    def __init__(
        self,
        min_gap_pct:   float = MIN_GAP_PCT,
        max_gap_pct:   float = MAX_GAP_PCT,
        min_rvol:      float = 3.0,       # base floor — overridden by _rvol_threshold()
        stop_mult:     float = STOP_ATR_MULT,
        target_mult:   float = TARGET_ATR_MULT,
        max_hold:      int   = MAX_HOLD_DAYS,
    ) -> None:
        self.min_gap_pct  = min_gap_pct
        self.max_gap_pct  = max_gap_pct
        self.min_rvol     = min_rvol
        self.stop_mult    = stop_mult
        self.target_mult  = target_mult
        self.max_hold     = max_hold

    def run(
        self,
        symbols:   list[str],
        start:     date,
        end:       date,
        ann_dates: dict[date, set[str]] | None = None,
        verbose:   bool = True,
    ) -> list[EarningsBacktestTrade]:
        """
        ann_dates: {date → {symbol, ...}} — if provided, only test on announcement days.
                   Pass None to scan ALL gap+RVOL days (mode=scan).
        """
        all_trades: list[EarningsBacktestTrade] = []
        failed = 0

        for i, symbol in enumerate(symbols):
            if verbose and (i % 50 == 0 or i == len(symbols) - 1):
                print(f"  [{i+1}/{len(symbols)}] {symbol}", flush=True)

            try:
                df = _fetch_ohlcv(symbol, start, end)
            except Exception:
                failed += 1
                continue

            if df is None or len(df) < WARMUP_BARS + max(self.max_hold, 1):
                failed += 1
                continue

            sym_ann_dates: set[date] | None = None
            if ann_dates is not None:
                sym_ann_dates = {d for d, syms in ann_dates.items() if symbol in syms}
                if not sym_ann_dates:
                    continue

            trades = self._scan_symbol(symbol, df, sym_ann_dates)
            all_trades.extend(trades)
            time.sleep(0.05)

        if verbose:
            print(f"  Done. {len(symbols) - failed}/{len(symbols)} symbols loaded. "
                  f"{len(all_trades)} signals found.")

        return all_trades

    def _scan_symbol(
        self,
        symbol:    str,
        df:        pd.DataFrame,
        ann_dates: set[date] | None,
    ) -> list[EarningsBacktestTrade]:
        """Scan one symbol's price history for gap+RVOL events."""
        df = _add_atr(df)
        trades: list[EarningsBacktestTrade] = []
        last_trade_end_idx = -1

        for i in range(WARMUP_BARS, len(df) - self.max_hold):
            if i <= last_trade_end_idx:
                continue

            bar_date = _to_date(df.index[i])
            if bar_date < date(2020, 1, 1) or bar_date > date.today():
                continue

            if ann_dates is not None and bar_date not in ann_dates:
                continue

            open_p  = float(df.iloc[i]["open"])
            prev_cl = float(df.iloc[i - 1]["close"])
            if prev_cl <= 0 or open_p <= 0:
                continue

            gap_pct = (open_p - prev_cl) / prev_cl * 100

            # Gap bounds: skip noise (<min) and circuit-risk (>max)
            if gap_pct < self.min_gap_pct or gap_pct > self.max_gap_pct:
                continue

            # RVOL: scales with gap size — large gaps need stronger institutional confirmation
            avg_vol = float(df.iloc[i - 20:i]["volume"].mean())
            day_vol = float(df.iloc[i]["volume"])
            rvol = day_vol / avg_vol if avg_vol > 0 else 0.0

            min_rvol = max(self.min_rvol, _rvol_threshold(gap_pct))
            if rvol < min_rvol:
                continue

            # Pre-trend filter: skip stocks already up >25% in 60 days
            overextended = _is_overextended(df, i)
            if overextended:
                continue

            # Confidence tier
            if gap_pct >= 7.0 and rvol >= 5.0:
                confidence = 80
            elif gap_pct >= 5.0 or rvol >= 4.0:
                confidence = 74
            else:
                confidence = 68

            atr = float(df.iloc[i - 1].get("atr_14", prev_cl * 0.02) or prev_cl * 0.02)
            if atr <= 0:
                atr = prev_cl * 0.02

            entry  = open_p
            # Stop never placed inside the gap — gap floor is prev_close + 1%
            gap_floor = prev_cl * 1.01
            stop   = round(max(entry - self.stop_mult * atr, gap_floor), 2)
            target = round(entry + self.target_mult * atr, 2)

            # 1:1 level — when price hits this, trail stop to entry (breakeven)
            one_r = round(entry + self.stop_mult * atr, 2)

            current_stop = stop
            trailing_activated = False
            exit_price  = None
            exit_reason = "MAX_HOLD"
            exit_day    = self.max_hold

            for j in range(1, self.max_hold + 1):
                if i + j >= len(df):
                    break
                future = df.iloc[i + j]
                f_high = float(future["high"])
                f_low  = float(future["low"])
                f_close = float(future["close"])

                # Activate trailing stop once price touches 1:1 level
                if not trailing_activated and f_high >= one_r:
                    trailing_activated = True
                    current_stop = max(current_stop, entry)  # move stop to breakeven

                # After trailing activated, trail 1.5×ATR below each day's high
                if trailing_activated:
                    trail_from_high = round(f_high - self.stop_mult * atr, 2)
                    if trail_from_high > current_stop:
                        current_stop = trail_from_high

                if f_low <= current_stop:
                    exit_price  = current_stop
                    exit_reason = "STOP"
                    exit_day    = j
                    break
                if f_high >= target:
                    exit_price  = target
                    exit_reason = "TARGET"
                    exit_day    = j
                    break

            if exit_price is None:
                end_idx    = min(i + self.max_hold, len(df) - 1)
                exit_price = float(df.iloc[end_idx]["close"])
                exit_day   = end_idx - i

            pnl_pct = (exit_price - entry) / entry * 100

            trades.append(EarningsBacktestTrade(
                symbol       = symbol,
                date         = bar_date,
                gap_pct      = round(gap_pct, 2),
                rvol         = round(rvol, 2),
                confidence   = confidence,
                entry        = round(entry, 2),
                stop         = stop,
                target       = target,
                exit_price   = round(exit_price, 2),
                exit_reason  = exit_reason,
                exit_day     = exit_day,
                pnl_pct      = round(pnl_pct, 2),
                is_win       = exit_reason == "TARGET",
                overextended = overextended,
            ))

            last_trade_end_idx = i + exit_day

        return trades


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fetch_ohlcv(symbol: str, start: date, end: date) -> pd.DataFrame | None:
    """Fetch daily OHLCV from yfinance. Returns flat-column DataFrame."""
    import yfinance as yf

    ticker = "^NSEI" if symbol == "NIFTY 50" else f"{symbol}.NS"
    fetch_start = start - timedelta(days=90)  # extra warmup for 60-bar pre-trend check

    ticker_obj = yf.Ticker(ticker)
    hist = ticker_obj.history(
        start=fetch_start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval="1d",
        auto_adjust=True,
    )
    if hist is None or hist.empty:
        return None

    hist.columns = [c.lower() for c in hist.columns]
    df = hist[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])
    df = df[df["close"] > 0]
    return df


def _add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Add atr_14 column using true range."""
    df = df.copy()
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr_14"] = tr.rolling(period, min_periods=period).mean()
    return df


def _to_date(ts) -> date:
    if hasattr(ts, "date"):
        return ts.date()
    return ts
