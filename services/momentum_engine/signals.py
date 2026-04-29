"""
services/momentum_engine/signals.py
─────────────────────────────────────
Momentum/trend-following signal detector for TRENDING_UP markets.

Opposite philosophy to the reversal engine:
  - Buys STRENGTH not exhaustion
  - RSI sweet spot: 60–75 (momentum in play, not yet overbought)
  - ADX > 25 required (real trend, not noise)
  - Price ABOVE 200 EMA, all EMAs bullishly stacked
  - Volume EXPANDING into breakouts (RVOL ≥ 2.5×)

Signals detected:
  DARVAS_BREAKOUT  — 4-bar Darvas box top break with volume
  BREAKOUT_52W     — 52-week high breakout with heavy volume
  VOLUME_THRUST    — 3× average volume on a strong bullish candle
  EMA_RIBBON       — EMA 8/21/50 fanning upward = trend acceleration
  BULL_MOMENTUM    — ADX >30 + RSI 60-72 + EMA stack = trend continuation

Confluence scoring (momentum-calibrated, max 10):
  signal_quality   : signal type + pattern cleanness    (0-2)
  volume           : RVOL vs threshold                  (0-2)
  trend_alignment  : EMA stack + above 200 EMA + ADX    (0-2)
  rsi_momentum     : RSI in 55-72 sweet spot            (0-2)
  multi_signal     : distinct agreeing signals           (0-2)

Minimum score to trade: 7 (lower than reversal engine's 8
because momentum signals are inherently self-confirming
— a 52wk breakout with 3× volume needs less "confluence"
than a reversal pattern fighting the tape).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import NamedTuple

import numpy as np
import pandas as pd
import structlog

log = structlog.get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

MIN_MOMENTUM_SCORE = 7   # Out of 10; momentum signals self-confirm more than reversals

# Bars used to build a Darvas box (Darvas's original: 4 bars for high, 2 for low)
DARVAS_BOX_BARS = 4

# 52-week lookback in trading days
YEAR_BARS = 252


class MomentumSignalType(str, Enum):
    DARVAS_BREAKOUT  = "DARVAS_BREAKOUT"   # Price breaks Darvas box top with vol
    BREAKOUT_52W     = "BREAKOUT_52W"      # New 52-week high + RVOL ≥ 3×
    VOLUME_THRUST    = "VOLUME_THRUST"     # 3× avg vol on strong bullish candle
    EMA_RIBBON       = "EMA_RIBBON"        # EMA 8/21/50 expanding upward
    BULL_MOMENTUM    = "BULL_MOMENTUM"     # ADX+RSI+EMA continuation


@dataclass
class MomentumSignal:
    symbol:          str
    signal_type:     MomentumSignalType
    price:           float
    atr:             float
    rvol:            float
    rsi:             float
    adx:             float
    ema_stack:       int        # +1 = bullish stack, 0 = mixed, -1 = bearish
    above_200ema:    bool
    confidence:      int        # 0–100
    indicators:      dict       = field(default_factory=dict)
    timestamp:       datetime   = field(default_factory=datetime.now)
    notes:           str        = ""


@dataclass
class MomentumConfluence:
    signal_quality:   int = 0   # 0-2
    volume:           int = 0   # 0-2
    trend_alignment:  int = 0   # 0-2
    rsi_momentum:     int = 0   # 0-2
    multi_signal:     int = 0   # 0-2

    @property
    def total(self) -> int:
        return (self.signal_quality + self.volume +
                self.trend_alignment + self.rsi_momentum + self.multi_signal)

    @property
    def passed(self) -> bool:
        return self.total >= MIN_MOMENTUM_SCORE


class MomentumDetector:
    """
    Scans a pre-computed (indicators already appended) daily DataFrame
    and returns all momentum signals at the latest bar.

    Caller must pass a DataFrame with indicators already computed via
    services.technical_engine.indicators.compute_all().
    """

    def detect(
        self,
        df:     pd.DataFrame,
        symbol: str,
    ) -> list[MomentumSignal]:
        """
        Returns all momentum signals firing at the last bar of df.
        df must have at least 60 rows with full indicator columns.
        """
        if len(df) < 60:
            return []

        latest   = df.iloc[-1]
        price    = float(latest.get("close", 0) or 0)
        if price <= 0:
            return []

        # Core indicators at latest bar
        atr_14   = float(latest.get("atr_14") or latest.get("atr") or 0)
        rsi      = float(latest.get("rsi_14") or latest.get("rsi") or 50)
        adx      = float(latest.get("adx") or 0)
        ema8     = float(latest.get("ema_8")   or latest.get("ema_fast") or price)
        ema21    = float(latest.get("ema_33")  or latest.get("ema_mid")  or price)  # cfg uses 33 as mid
        ema50    = float(latest.get("ema_50")  or latest.get("ema_slow") or price)
        ema200   = float(latest.get("ema_200") or latest.get("ema_trend") or price)
        ema_stack = int(latest.get("ema_stack") or 0)
        above_200 = bool(latest.get("above_200ema", price > ema200))

        # Volume
        vol      = float(latest.get("volume") or 0)
        avg_vol  = float(df["volume"].tail(20).mean() or 1)
        rvol     = round(vol / avg_vol, 2) if avg_vol > 0 else 1.0

        # Hard gate: must be above 200 EMA and have positive ADX for any signal
        if not above_200:
            return []
        if adx < 20:
            return []

        signals: list[MomentumSignal] = []

        signals += self._darvas_breakout(df, symbol, price, atr_14, rvol, rsi, adx, ema_stack, above_200)
        signals += self._breakout_52w(df, symbol, price, atr_14, rvol, rsi, adx, ema_stack, above_200)
        signals += self._volume_thrust(df, symbol, price, atr_14, rvol, rsi, adx, ema_stack, above_200)
        signals += self._ema_ribbon(df, symbol, price, atr_14, rvol, rsi, adx, ema_stack, above_200,
                                    ema8, ema21, ema50, ema200)
        signals += self._bull_momentum(df, symbol, price, atr_14, rvol, rsi, adx, ema_stack, above_200)

        return signals

    # ── Individual signal detectors ───────────────────────────────────────────

    def _darvas_breakout(
        self, df, symbol, price, atr, rvol, rsi, adx, ema_stack, above_200
    ) -> list[MomentumSignal]:
        """
        Darvas Box breakout:
          - Identify box top: highest high in last DARVAS_BOX_BARS bars
            where the high was NOT exceeded in the 2 bars immediately following it
          - Current candle closes above that box top
          - Volume ≥ 2× average on breakout candle
          - Box must have lasted ≥ 3 bars (avoid 1-bar fakeouts)
          - ATR contraction in box: max(last 5 bars range) < 1.5× ATR
            (tight consolidation, not a volatile chop zone)
        """
        if len(df) < DARVAS_BOX_BARS + 5:
            return []

        # Find the most recent valid Darvas box top.
        # A valid box high = a bar whose high was not exceeded in the next 2 bars.
        highs  = df["high"].values
        lows   = df["low"].values
        closes = df["close"].values
        n = len(highs)

        box_top    = None
        box_bottom = None
        box_start  = None

        # Scan backwards from 3 bars ago (need 2 bars after the candidate)
        for i in range(n - 3, n - DARVAS_BOX_BARS - 10, -1):
            if i < 2:
                break
            candidate_high = highs[i]
            # Not exceeded in the 2 bars following
            if highs[i + 1] < candidate_high and highs[i + 2] < candidate_high:
                # We have a valid box top — now find box bottom (lowest low since box formed)
                box_end   = n - 1
                box_lows  = lows[i:box_end]
                if len(box_lows) < 3:
                    continue
                candidate_bottom = float(np.min(box_lows))
                box_height       = candidate_high - candidate_bottom
                # Box must have some depth (at least 0.5× ATR) but not too wide (not a trend)
                if atr > 0 and box_height < 0.3 * atr:
                    continue   # Too shallow — not a real box
                if atr > 0 and box_height > 8 * atr:
                    continue   # Too deep — this is a crash, not consolidation
                box_top    = candidate_high
                box_bottom = candidate_bottom
                box_start  = i
                break

        if box_top is None:
            return []

        # Current close must break above box top
        current_close = closes[-1]
        if current_close <= box_top:
            return []

        # Volume gate: breakout candle must have volume ≥ 2× average
        if rvol < 2.0:
            return []

        # ATR contraction in box: recent candle ranges should be tighter
        box_slice     = df.iloc[box_start:-1]
        if len(box_slice) >= 3:
            box_ranges    = (box_slice["high"] - box_slice["low"]).values
            avg_box_range = float(np.mean(box_ranges))
            if atr > 0 and avg_box_range > 1.5 * atr:
                return []   # Choppy box — not a clean consolidation

        # RSI gate: momentum in play but not overbought
        if rsi < 45 or rsi > 80:
            return []

        # Confidence: higher when ADX strong, RVOL high, RSI in 55-72 zone
        conf = 65
        if adx > 30: conf += 10
        if rvol >= 3.0: conf += 10
        if 55 <= rsi <= 72: conf += 5
        if ema_stack == 1: conf += 5
        conf = min(conf, 95)

        box_pct = round((current_close - box_top) / box_top * 100, 2)
        return [MomentumSignal(
            symbol       = symbol,
            signal_type  = MomentumSignalType.DARVAS_BREAKOUT,
            price        = price,
            atr          = atr,
            rvol         = rvol,
            rsi          = rsi,
            adx          = adx,
            ema_stack    = ema_stack,
            above_200ema = above_200,
            confidence   = conf,
            indicators   = {"rvol": rvol, "rsi_14": rsi, "adx": adx,
                            "box_top": round(box_top, 2), "box_bottom": round(box_bottom, 2),
                            "atr_14": atr, "ema_stack": ema_stack, "above_200ema": above_200},
            notes        = f"Darvas breakout +{box_pct}% above box_top={box_top:.2f}",
        )]

    def _breakout_52w(
        self, df, symbol, price, atr, rvol, rsi, adx, ema_stack, above_200
    ) -> list[MomentumSignal]:
        """
        52-week high breakout:
          - Current close makes a new 52-week high (or within 0.5% of it)
          - RVOL ≥ 3× (institutional accumulation signal)
          - RSI 55–75 (momentum, not parabolic)
          - ADX > 25 (genuine trend breakout)
          - Stock has been in uptrend: close > 50-day EMA
        """
        if len(df) < 60:
            return []

        lookback   = min(YEAR_BARS, len(df) - 1)
        hist_highs = df["high"].iloc[-lookback:-1]
        year_high  = float(hist_highs.max())

        # Must be within 0.5% of or exceeding the 52-week high
        if price < year_high * 0.995:
            return []

        # Volume gate: institutions must be buying
        if rvol < 3.0:
            return []

        # RSI gate
        if rsi < 55 or rsi > 78:
            return []

        # ADX gate
        if adx < 25:
            return []

        # EMA confirmation: price must be above 50 EMA
        ema50 = float(df.iloc[-1].get("ema_50") or df.iloc[-1].get("ema_slow") or 0)
        if ema50 > 0 and price < ema50:
            return []

        # How far above prior 52wk high
        new_high_pct = round((price - year_high) / year_high * 100, 2) if year_high > 0 else 0

        conf = 70
        if adx > 35: conf += 10
        if rvol >= 5.0: conf += 10
        if 60 <= rsi <= 72: conf += 5
        if new_high_pct > 1.0: conf += 5   # Decisive breakout, not a touch
        conf = min(conf, 98)

        return [MomentumSignal(
            symbol       = symbol,
            signal_type  = MomentumSignalType.BREAKOUT_52W,
            price        = price,
            atr          = atr,
            rvol         = rvol,
            rsi          = rsi,
            adx          = adx,
            ema_stack    = ema_stack,
            above_200ema = above_200,
            confidence   = conf,
            indicators   = {"rvol": rvol, "rsi_14": rsi, "adx": adx,
                            "year_high": round(year_high, 2), "new_high_pct": new_high_pct,
                            "atr_14": atr, "ema_stack": ema_stack, "above_200ema": above_200},
            notes        = f"52wk breakout: price={price:.2f} year_high={year_high:.2f} +{new_high_pct}%",
        )]

    def _volume_thrust(
        self, df, symbol, price, atr, rvol, rsi, adx, ema_stack, above_200
    ) -> list[MomentumSignal]:
        """
        Volume thrust (institutional accumulation):
          - Today's volume ≥ 3× 20-day average
          - Today's candle is strongly bullish: close in top 30% of range
            AND range ≥ 1.5× ATR (big candle, not a doji)
          - Close > open (green candle)
          - Price above 20 EMA (trend context)
          - RSI not overbought (< 78)

        Rationale: A 3× volume day with a big green candle = institutional
        buying. These often precede multi-day continuation moves.
        """
        if len(df) < 25:
            return []

        candle = df.iloc[-1]
        o, h, l, c = (float(candle.get(k, 0) or 0) for k in ["open", "high", "low", "close"])

        # Green candle
        if c <= o:
            return []

        candle_range = h - l
        # Big candle: range ≥ 1.5× ATR
        if atr > 0 and candle_range < 1.5 * atr:
            return []

        # Close in top 30% of candle's range (strong close)
        if candle_range > 0:
            close_position = (c - l) / candle_range
            if close_position < 0.70:
                return []

        # Volume gate
        if rvol < 3.0:
            return []

        # RSI gate (not parabolic)
        if rsi > 78:
            return []

        # Price above 20-period SMA
        sma20 = float(df["close"].tail(20).mean() or 0)
        if sma20 > 0 and price < sma20:
            return []

        body_pct = round((c - o) / o * 100, 2)
        conf = 65
        if rvol >= 5.0: conf += 15
        elif rvol >= 4.0: conf += 10
        if adx > 30: conf += 10
        if ema_stack == 1: conf += 5
        conf = min(conf, 95)

        return [MomentumSignal(
            symbol       = symbol,
            signal_type  = MomentumSignalType.VOLUME_THRUST,
            price        = price,
            atr          = atr,
            rvol         = rvol,
            rsi          = rsi,
            adx          = adx,
            ema_stack    = ema_stack,
            above_200ema = above_200,
            confidence   = conf,
            indicators   = {"rvol": rvol, "rsi_14": rsi, "adx": adx,
                            "candle_range": round(candle_range, 2), "body_pct": body_pct,
                            "atr_14": atr, "ema_stack": ema_stack, "above_200ema": above_200},
            notes        = f"Volume thrust: RVOL={rvol:.1f}x body={body_pct:.1f}%",
        )]

    def _ema_ribbon(
        self, df, symbol, price, atr, rvol, rsi, adx, ema_stack, above_200,
        ema8, ema21, ema50, ema200
    ) -> list[MomentumSignal]:
        """
        EMA ribbon expansion (trend acceleration):
          - All three EMAs bullishly ordered: ema8 > ema21 > ema50
          - All three EMAs rising (today > 3 bars ago)
          - Ribbon width EXPANDING: spread between ema8 and ema50 is
            wider now than 5 bars ago (acceleration, not deceleration)
          - Price above all EMAs
          - ADX ≥ 25 and rising
          - RSI 55–75

        This fires at the early stage of a trend acceleration — good entry
        point before the move becomes obvious to everyone.
        """
        if len(df) < 20:
            return []

        # EMA stack check
        if not (ema8 > ema21 > ema50 > 0):
            return []

        if price < ema8:
            return []

        # All EMAs rising
        lag = min(3, len(df) - 1)
        try:
            prev = df.iloc[-(lag + 1)]
            ema8_prev  = float(prev.get("ema_8")  or prev.get("ema_fast") or 0)
            ema21_prev = float(prev.get("ema_33") or prev.get("ema_mid")  or 0)
            ema50_prev = float(prev.get("ema_50") or prev.get("ema_slow") or 0)
        except Exception:
            return []

        if not (ema8 > ema8_prev and ema21 > ema21_prev and ema50 > ema50_prev):
            return []

        # Ribbon expanding — measure over last 3 bars (recency gate)
        # We want the ribbon to have STARTED expanding recently, not been
        # wide for 20 bars already (that's a late/exhausted signal).
        current_spread = ema8 - ema50
        lag5 = min(5, len(df) - 1)
        lag3 = min(3, len(df) - 1)
        try:
            prev5     = df.iloc[-(lag5 + 1)]
            e8_p5     = float(prev5.get("ema_8")  or prev5.get("ema_fast") or 0)
            e50_p5    = float(prev5.get("ema_50") or prev5.get("ema_slow") or 0)
            past_spread_5 = e8_p5 - e50_p5

            prev3     = df.iloc[-(lag3 + 1)]
            e8_p3     = float(prev3.get("ema_8")  or prev3.get("ema_fast") or 0)
            e50_p3    = float(prev3.get("ema_50") or prev3.get("ema_slow") or 0)
            past_spread_3 = e8_p3 - e50_p3
        except Exception:
            return []

        if past_spread_5 <= 0 or current_spread <= past_spread_5 * 1.05:
            return []   # Ribbon not expanding by at least 5% over 5 bars

        # Freshness gate: ribbon must have accelerated in the last 3 bars
        # (spread 3 bars ago < current spread by at least 10%)
        # If spread has barely changed in last 3 bars → late-stage trend, skip
        if past_spread_3 > 0 and current_spread < past_spread_3 * 1.08:
            return []   # Ribbon expansion stalling — late entry risk

        # ADX gate
        if adx < 25:
            return []

        # RSI sweet spot for momentum
        if rsi < 55 or rsi > 78:
            return []

        spread_pct = round((current_spread / price) * 100, 2)
        conf = 60
        if adx > 35: conf += 10
        if ema_stack == 1: conf += 10
        if 60 <= rsi <= 72: conf += 10
        if rvol >= 2.0: conf += 5
        conf = min(conf, 90)

        return [MomentumSignal(
            symbol       = symbol,
            signal_type  = MomentumSignalType.EMA_RIBBON,
            price        = price,
            atr          = atr,
            rvol         = rvol,
            rsi          = rsi,
            adx          = adx,
            ema_stack    = ema_stack,
            above_200ema = above_200,
            confidence   = conf,
            indicators   = {"rvol": rvol, "rsi_14": rsi, "adx": adx,
                            "ema8": round(ema8, 2), "ema21": round(ema21, 2),
                            "ema50": round(ema50, 2), "spread_pct": spread_pct,
                            "atr_14": atr, "ema_stack": ema_stack, "above_200ema": above_200},
            notes        = f"EMA ribbon expanding: spread={spread_pct:.1f}% ADX={adx:.1f}",
        )]

    def _bull_momentum(
        self, df, symbol, price, atr, rvol, rsi, adx, ema_stack, above_200
    ) -> list[MomentumSignal]:
        """
        Bull momentum continuation:
          - ADX > 30 (strong trend)
          - RSI 60–72 (momentum zone, not overbought)
          - EMA stack = +1 (all EMAs bullishly ordered)
          - Price > 20-bar high (breakout of recent range)
          - RVOL ≥ 1.5× (volume supporting the move)
          - Not already parabolic: candle body < 3× ATR

        This is a "trend continuation" signal — the trend is in full
        force, conditions are healthy, and price is breaking to new highs.
        Lowest bar of the five signals but high probability when combined
        with Darvas or 52wk breakout as a multi-signal confirmer.
        """
        if len(df) < 25:
            return []

        # ADX must be strong
        if adx < 30:
            return []

        # RSI in momentum zone
        if not (60 <= rsi <= 72):
            return []

        # EMA stack must be fully bullish
        if ema_stack != 1:
            return []

        # Price must break 20-bar high
        bar20_high = float(df["high"].iloc[-21:-1].max() or 0)
        if bar20_high > 0 and price <= bar20_high:
            return []

        # Volume confirmation
        if rvol < 1.5:
            return []

        # Not parabolic: today's body < 3× ATR
        candle  = df.iloc[-1]
        o, c    = float(candle.get("open", price) or price), price
        body    = abs(c - o)
        if atr > 0 and body > 3 * atr:
            return []

        conf = 65
        if adx > 40: conf += 10
        if rvol >= 2.5: conf += 10
        if 62 <= rsi <= 70: conf += 5
        conf = min(conf, 90)

        return [MomentumSignal(
            symbol       = symbol,
            signal_type  = MomentumSignalType.BULL_MOMENTUM,
            price        = price,
            atr          = atr,
            rvol         = rvol,
            rsi          = rsi,
            adx          = adx,
            ema_stack    = ema_stack,
            above_200ema = above_200,
            confidence   = conf,
            indicators   = {"rvol": rvol, "rsi_14": rsi, "adx": adx,
                            "bar20_high": round(bar20_high, 2),
                            "atr_14": atr, "ema_stack": ema_stack, "above_200ema": above_200},
            notes        = f"Bull momentum: ADX={adx:.1f} RSI={rsi:.1f} RVOL={rvol:.1f}x",
        )]


# ── Confluence scorer ─────────────────────────────────────────────────────────

# Higher quality signals (stronger directional confirmation)
_HIGH_QUALITY_MOMENTUM = {
    MomentumSignalType.DARVAS_BREAKOUT,
    MomentumSignalType.BREAKOUT_52W,
}


def score_momentum_confluence(signals: list[MomentumSignal]) -> MomentumConfluence:
    """
    Score the best signal against 5 factors.
    Returns a MomentumConfluence object with .total and .passed.

    Calibrated for momentum (opposite of reversal):
      - RSI 60-75 = GOOD (momentum in play)
      - RSI <55 or >78 = BAD (not trending / parabolic)
      - ADX > 30 scores higher
      - RVOL 2.5× threshold (momentum breakouts need more volume than reversals)
    """
    if not signals:
        return MomentumConfluence()

    # Best signal by confidence
    top   = max(signals, key=lambda s: s.confidence)
    score = MomentumConfluence()

    # ── Factor 1: Signal quality ──────────────────────────────────────────
    hq   = top.signal_type in _HIGH_QUALITY_MOMENTUM
    conf = top.confidence
    if conf >= 80 and hq:
        score.signal_quality = 2
    elif conf >= 70 or hq:
        score.signal_quality = 1

    # ── Factor 2: Volume ──────────────────────────────────────────────────
    rvol = top.rvol
    if rvol >= 3.0:
        score.volume = 2
    elif rvol >= 1.8:
        score.volume = 1

    # ── Factor 3: Trend alignment ─────────────────────────────────────────
    # above_200ema + fully bullish EMA stack + strong ADX
    above_200 = top.above_200ema
    stack_ok  = top.ema_stack == 1
    adx_ok    = top.adx >= 25

    if above_200 and stack_ok and adx_ok:
        score.trend_alignment = 2
    elif (above_200 and stack_ok) or (above_200 and adx_ok):
        score.trend_alignment = 1

    # ── Factor 4: RSI momentum sweet spot ────────────────────────────────
    # Momentum calibrated: 60-75 is ideal for a trending stock
    rsi = top.rsi
    if 60.0 <= rsi <= 72.0:
        score.rsi_momentum = 2
    elif (55.0 <= rsi < 60.0) or (72.0 < rsi <= 78.0):
        score.rsi_momentum = 1

    # ── Factor 5: Multi-signal agreement ─────────────────────────────────
    n = len(signals)
    if n >= 3:
        score.multi_signal = 2
    elif n == 2:
        score.multi_signal = 1

    return score
