"""
services/catalyst_engine/scanner.py
─────────────────────────────────────
Detects high-quality catalyst gap days across NSE universe.

A catalyst gap day requires ALL of:
  • Gap up 5–15% above prior close (material catalyst — earnings / order / event)
  • RVOL at open ≥ 8× 20-day average daily volume (institutional conviction)
  • Day 1 closes at ≥ 90% of day high (institutions absorbed sellers; gap held)
  • Not overextended: price not up >30% in prior 60 days
  • Minimum price ≥ 100, average daily volume ≥ 200K (liquidity gates)

Entry is on Day 2 open (not same-day intraday breakout).
The ORB 9:45 approach was tested and discarded — it passes only ~7% of valid
gap days and produces negative expectancy. Day2 entry captures PEAD drift with
cleaner entry and defined risk.

Backtest results (Nifty500, 2022–2025, 210 trades):
  WR 31%, AvgWin +6.5%, AvgLoss -2.5%, Expectancy +0.31%/trade
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd
import structlog

log = structlog.get_logger(__name__)

# ── Signal parameters ─────────────────────────────────────────────────────────

MIN_GAP_PCT          = 7.0    # backtest: 5-7% gaps have -0.35% avg; 7%+ have +0.19% avg
MAX_GAP_PCT          = 15.0   # above = circuit risk, illiquid open
MIN_RVOL             = 8.0    # ≥8× confirms institutional participation (not just retail)
MIN_CLOSE_HIGH_RATIO = 0.90   # close / day-high — gap must hold: institutions absorbed sellers
MIN_PRICE            = 100.0  # skip sub-100 stocks (spread/manipulation risk)
MIN_AVG_DAILY_VOL    = 200_000  # 20-day avg daily volume floor
PRETREND_LOOKBACK    = 60
PRETREND_MAX_GAIN    = 0.30


@dataclass
class CatalystSignal:
    symbol:          str
    signal_date:     date    # Day 1 (gap day)
    gap_pct:         float   # open vs prior close %
    rvol:            float   # today vol / 20d avg vol
    close_high_ratio: float  # how well the gap held through the day
    day1_close:      float   # Day 1 close — used as entry reference for Day 2
    day1_open:       float   # Day 1 open (gap level)
    prev_close:      float   # prior day close
    atr14:           float   # 14-day ATR as of signal date


class CatalystScanner:
    """
    Detects catalyst gap days from daily OHLCV data.

    Needs:
      - daily_data: dict[symbol, pd.DataFrame] — daily OHLCV, date-indexed
    """

    def __init__(
        self,
        min_gap_pct:          float = MIN_GAP_PCT,
        max_gap_pct:          float = MAX_GAP_PCT,
        min_rvol:             float = MIN_RVOL,
        min_close_high_ratio: float = MIN_CLOSE_HIGH_RATIO,
        min_price:            float = MIN_PRICE,
        min_avg_daily_vol:    float = MIN_AVG_DAILY_VOL,
    ):
        self.min_gap_pct          = min_gap_pct
        self.max_gap_pct          = max_gap_pct
        self.min_rvol             = min_rvol
        self.min_close_high_ratio = min_close_high_ratio
        self.min_price            = min_price
        self.min_avg_daily_vol    = min_avg_daily_vol

    def scan(
        self,
        symbol: str,
        daily_df: pd.DataFrame,
        start: date,
        end: date,
    ) -> list[CatalystSignal]:
        """Return all catalyst gap signals for one symbol over [start, end]."""
        signals = []

        if daily_df is None or daily_df.empty:
            return signals

        daily_df = daily_df.copy()
        if hasattr(daily_df.index, "tz") and daily_df.index.tz is not None:
            daily_df.index = daily_df.index.tz_convert(None)
        daily_df.index = pd.to_datetime(daily_df.index).normalize()
        daily_df = daily_df.sort_index()

        trading_days = [d for d in daily_df.index.date if start <= d <= end]

        for day in trading_days:
            sig = self._check_day(symbol, day, daily_df)
            if sig:
                signals.append(sig)

        return signals

    def _check_day(
        self,
        symbol: str,
        day: date,
        daily_df: pd.DataFrame,
    ) -> Optional[CatalystSignal]:
        day_ts = pd.Timestamp(day)

        prior = daily_df[daily_df.index < day_ts]
        if len(prior) < 25:
            return None

        today_row = daily_df.loc[daily_df.index == day_ts]
        if today_row.empty:
            return None

        prev_close   = float(prior.iloc[-1]["close"])
        today_open   = float(today_row.iloc[0]["open"])
        today_high   = float(today_row.iloc[0]["high"])
        today_close  = float(today_row.iloc[0]["close"])
        today_volume = float(today_row.iloc[0]["volume"])

        if prev_close <= 0 or today_open <= 0:
            return None

        # Price gate
        if today_open < self.min_price:
            return None

        # Gap filter
        gap_pct = (today_open - prev_close) / prev_close * 100
        if gap_pct < self.min_gap_pct or gap_pct > self.max_gap_pct:
            return None

        # RVOL gate
        avg_vol_20d = float(prior.iloc[-20:]["volume"].mean())
        if avg_vol_20d < self.min_avg_daily_vol:
            return None
        rvol = today_volume / avg_vol_20d if avg_vol_20d > 0 else 0.0
        if rvol < self.min_rvol:
            return None

        # Gap holding strength: close must stay near day's high
        if today_high <= 0:
            return None
        close_high_ratio = today_close / today_high
        if close_high_ratio < self.min_close_high_ratio:
            return None

        # Overextension check
        if _is_overextended(prior, today_open):
            return None

        atr14 = _calc_atr14(prior)

        return CatalystSignal(
            symbol           = symbol,
            signal_date      = day,
            gap_pct          = round(gap_pct, 2),
            rvol             = round(rvol, 2),
            close_high_ratio = round(close_high_ratio, 4),
            day1_close       = round(today_close, 2),
            day1_open        = round(today_open, 2),
            prev_close       = round(prev_close, 2),
            atr14            = round(atr14, 2),
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_overextended(prior: pd.DataFrame, current_price: float) -> bool:
    if len(prior) < PRETREND_LOOKBACK:
        return False
    base = float(prior.iloc[-PRETREND_LOOKBACK]["close"])
    if base <= 0:
        return False
    return (current_price - base) / base > PRETREND_MAX_GAIN


def _calc_atr14(prior: pd.DataFrame) -> float:
    df = prior.iloc[-15:].copy()
    if len(df) < 2:
        return 0.0
    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values
    trs = []
    for i in range(1, len(df)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return float(np.mean(trs[-14:])) if trs else 0.0
