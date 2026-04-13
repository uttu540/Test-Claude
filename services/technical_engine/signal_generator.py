"""
services/technical_engine/signal_generator.py
───────────────────────────────────────────────
Detects trading signals from computed indicator DataFrames.

A "signal" is NOT a trade recommendation — it is an observation that
certain technical conditions are met. The AI strategy engine decides
whether to act on signals.

Signal confidence is scored 0–100 based on multi-timeframe confluence.

Phase 3 additions:
  - ORB_BREAKOUT:  Opening Range Breakout (9:15–9:30 AM range, valid until 1 PM)
  - VWAP_RECLAIM:  Price reclaims / breaks VWAP with volume confirmation
  - RegimeFilter:  Gates signals by market regime before they reach Claude
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import Literal

import numpy as np
import pandas as pd
import structlog

from services.technical_engine.indicators import IndicatorConfig, compute_all, get_latest

log = structlog.get_logger(__name__)


class Direction(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class SignalType(str, Enum):
    # Breakout signals
    BREAKOUT_HIGH      = "BREAKOUT_HIGH"       # Price breaks above recent swing high
    BREAKOUT_LOW       = "BREAKOUT_LOW"        # Price breaks below recent swing low
    # Trend signals
    EMA_CROSSOVER_UP   = "EMA_CROSSOVER_UP"    # Fast EMA crosses above slow EMA
    EMA_CROSSOVER_DOWN = "EMA_CROSSOVER_DOWN"
    ABOVE_200_EMA      = "ABOVE_200_EMA"       # Price re-enters above 200 EMA
    BELOW_200_EMA      = "BELOW_200_EMA"
    # Momentum signals
    RSI_OVERSOLD       = "RSI_OVERSOLD"        # RSI < 30 turning up
    RSI_OVERBOUGHT     = "RSI_OVERBOUGHT"      # RSI > 70 turning down
    MACD_CROSS_UP      = "MACD_CROSS_UP"
    MACD_CROSS_DOWN    = "MACD_CROSS_DOWN"
    # Volume signals
    HIGH_RVOL          = "HIGH_RVOL"           # Volume > 2x 20-day average
    # Volatility signals
    BB_SQUEEZE         = "BB_SQUEEZE"          # Bollinger Band width < 20th percentile
    BB_EXPANSION       = "BB_EXPANSION"        # BB width expanding after squeeze
    # Phase 3 — Indian market strategies
    ORB_BREAKOUT       = "ORB_BREAKOUT"        # Opening Range Breakout (9:15–9:30 range)
    VWAP_RECLAIM       = "VWAP_RECLAIM"        # Price reclaims / breaks VWAP with volume
    # Phase 4 — Candlestick patterns
    HAMMER             = "HAMMER"              # Long lower wick reversal after downtrend
    SHOOTING_STAR      = "SHOOTING_STAR"       # Long upper wick reversal after uptrend
    ENGULFING_BULL     = "ENGULFING_BULL"      # Bullish candle engulfs prior bearish body
    ENGULFING_BEAR     = "ENGULFING_BEAR"      # Bearish candle engulfs prior bullish body
    MORNING_STAR       = "MORNING_STAR"        # 3-candle bullish reversal
    EVENING_STAR       = "EVENING_STAR"        # 3-candle bearish reversal
    # Phase 4 — Chart patterns
    DOUBLE_BOTTOM      = "DOUBLE_BOTTOM"       # W pattern — two lows at similar price
    DOUBLE_TOP         = "DOUBLE_TOP"          # M pattern — two highs at similar price
    BULL_FLAG          = "BULL_FLAG"           # Tight consolidation after sharp rally
    BEAR_FLAG          = "BEAR_FLAG"           # Tight consolidation after sharp decline
    DARVAS_BREAKOUT    = "DARVAS_BREAKOUT"     # Price breaks above Darvas box top
    NR7_SETUP          = "NR7_SETUP"           # Narrowest range in 7 bars — breakout imminent


@dataclass
class Signal:
    """A single detected technical signal."""
    trading_symbol:  str
    timeframe:       str
    signal_type:     SignalType
    direction:       Direction
    confidence:      int            # 0–100
    price_at_signal: float
    indicators:      dict           # Snapshot of key indicator values
    timestamp:       datetime       = field(default_factory=datetime.now)
    notes:           str            = ""

    def to_dict(self) -> dict:
        return {
            "symbol":        self.trading_symbol,
            "timeframe":     self.timeframe,
            "signal":        self.signal_type.value,
            "direction":     self.direction.value,
            "confidence":    self.confidence,
            "price":         self.price_at_signal,
            "timestamp":     self.timestamp.isoformat(),
            "notes":         self.notes,
            "indicators":    self.indicators,
        }


# ─── Regime-Based Signal Filter ──────────────────────────────────────────────

# Maps each market regime to the set of signal types that are valid in it.
# None means "allow all" (used for UNKNOWN so we don't block during startup).
_REGIME_ALLOWED: dict[str, set[str] | None] = {
    "TRENDING_UP": {
        "BREAKOUT_HIGH", "EMA_CROSSOVER_UP", "MACD_CROSS_UP",
        "HIGH_RVOL", "BB_EXPANSION", "ABOVE_200_EMA",
        "ORB_BREAKOUT", "VWAP_RECLAIM",
        # Candlestick continuation / momentum
        "HAMMER", "ENGULFING_BULL", "MORNING_STAR",
        # Chart patterns
        "DOUBLE_BOTTOM", "BULL_FLAG", "DARVAS_BREAKOUT", "NR7_SETUP",
    },
    "TRENDING_DOWN": {
        "BREAKOUT_LOW", "EMA_CROSSOVER_DOWN", "MACD_CROSS_DOWN",
        "HIGH_RVOL", "BB_EXPANSION", "BELOW_200_EMA",
        "ORB_BREAKOUT", "VWAP_RECLAIM",
        # Candlestick reversal / continuation
        "SHOOTING_STAR", "ENGULFING_BEAR", "EVENING_STAR",
        # Chart patterns
        "DOUBLE_TOP", "BEAR_FLAG", "NR7_SETUP",
    },
    "RANGING": {
        "RSI_OVERSOLD", "RSI_OVERBOUGHT",
        "BB_SQUEEZE", "BB_EXPANSION",
        "VWAP_RECLAIM", "HIGH_RVOL",
        # Candlestick reversals work well in ranging markets
        "HAMMER", "SHOOTING_STAR",
        "ENGULFING_BULL", "ENGULFING_BEAR",
        "MORNING_STAR", "EVENING_STAR",
        "DOUBLE_BOTTOM", "DOUBLE_TOP", "NR7_SETUP",
    },
    "HIGH_VOLATILITY": {
        "VWAP_RECLAIM",   # Only safest signal in high-fear environment
    },
    "UNKNOWN": None,      # Allow all — safe default during startup
}

# Maximum confidence cap per regime (prevents over-confidence in bad conditions)
_REGIME_CONFIDENCE_CAP: dict[str, int] = {
    "TRENDING_UP":    100,
    "TRENDING_DOWN":  100,
    "RANGING":        80,
    "HIGH_VOLATILITY": 60,
    "UNKNOWN":        100,
}


class RegimeFilter:
    """
    Gates signals by current market regime before they reach the AI layer.
    Removes signals whose strategy type doesn't suit the current conditions,
    and caps confidence in high-risk regimes.

    Accepts an optional config dict (from bot_config) — if present, uses the
    configured allowed-signals list and confidence cap instead of hardcoded defaults.
    """

    def __init__(self, config: dict | None = None):
        self._config = config or {}

    def apply(self, signals: list[Signal], regime: str) -> list[Signal]:
        # Allowed signals: config override → hardcoded default → None (allow all)
        cfg_sig_key = f"regime_{regime.lower()}_signals"
        cfg_sig_val = self._config.get(cfg_sig_key)
        if cfg_sig_val is not None:
            allowed = set(cfg_sig_val.split(",")) if cfg_sig_val else None
        else:
            allowed = _REGIME_ALLOWED.get(regime)

        # Confidence cap: config override → hardcoded default
        cfg_cap_key = f"regime_cap_{regime.lower()}"
        cap = int(self._config.get(cfg_cap_key, _REGIME_CONFIDENCE_CAP.get(regime, 100)))

        filtered: list[Signal] = []
        for sig in signals:
            if allowed is not None and sig.signal_type.value not in allowed:
                log.debug(
                    "regime_filter.blocked",
                    symbol    = sig.trading_symbol,
                    signal    = sig.signal_type.value,
                    regime    = regime,
                )
                continue
            # Apply confidence cap for unfavourable regimes
            if sig.confidence > cap:
                sig.confidence = cap
            filtered.append(sig)

        removed = len(signals) - len(filtered)
        if removed:
            log.info(
                "regime_filter.applied",
                regime  = regime,
                total   = len(signals),
                removed = removed,
                kept    = len(filtered),
            )
        return filtered


# ─── Per-timeframe Signal Detector ───────────────────────────────────────────

class SignalDetector:
    """
    Detects signals on a single timeframe DataFrame.
    Returns a list of Signal objects.
    """

    def __init__(self, cfg: IndicatorConfig = IndicatorConfig()):
        self._cfg = cfg

    def detect(
        self,
        df: pd.DataFrame,
        symbol: str,
        timeframe: str,
        enabled_strategies: set[str] | None = None,
        min_confidence: int = 40,
        pre_computed: bool = False,
    ) -> list[Signal]:
        if len(df) < 50:   # Need enough history for reliable indicators
            return []

        # Default: all strategies enabled
        enabled = enabled_strategies or {
            "breakout", "ema", "momentum", "volume", "volatility",
            "orb", "vwap", "candlestick", "chart_patterns",
        }

        # Skip indicator computation if caller already pre-computed them (e.g. backtesting)
        if not pre_computed:
            df = compute_all(df, self._cfg)
        signals: list[Signal] = []
        latest = get_latest(df)
        price  = latest.get("close", 0)

        if "breakout"       in enabled: signals += self._breakout_signals(df, symbol, timeframe, price, latest)
        if "ema"            in enabled: signals += self._ema_signals(df, symbol, timeframe, price, latest)
        if "momentum"       in enabled: signals += self._momentum_signals(df, symbol, timeframe, price, latest)
        if "volume"         in enabled: signals += self._volume_signals(df, symbol, timeframe, price, latest)
        if "volatility"     in enabled: signals += self._volatility_signals(df, symbol, timeframe, price, latest)
        if "orb"            in enabled: signals += self._orb_signals(df, symbol, timeframe, price, latest)
        if "vwap"           in enabled: signals += self._vwap_signals(df, symbol, timeframe, price, latest)
        if "candlestick"    in enabled: signals += self._candlestick_signals(df, symbol, timeframe, price, latest)
        if "chart_patterns" in enabled: signals += self._chart_pattern_signals(df, symbol, timeframe, price, latest)

        # Filter to only signals above the minimum confidence threshold
        signals = [s for s in signals if s.confidence >= min_confidence]

        if signals:
            log.debug(
                "signal_detector.found",
                symbol=symbol,
                timeframe=timeframe,
                count=len(signals),
                types=[s.signal_type.value for s in signals],
            )

        return signals

    # ── Breakout ──────────────────────────────────────────────────────────────

    def _breakout_signals(
        self, df: pd.DataFrame, symbol: str, tf: str, price: float, latest: dict
    ) -> list[Signal]:
        signals = []
        lookback = 20

        if len(df) < lookback + 1:
            return []

        recent_high = df["high"].iloc[-(lookback + 1):-1].max()
        recent_low  = df["low"].iloc[-(lookback + 1):-1].min()
        rvol        = latest.get("rvol", 1.0)

        # Breakout above 20-period high
        if price > recent_high:
            confidence = 50
            if rvol > 1.5:   confidence += 15
            if rvol > 2.0:   confidence += 10
            if latest.get("ema_stack") == 1:  confidence += 15
            if latest.get("above_200ema"):     confidence += 10
            signals.append(Signal(
                trading_symbol  = symbol,
                timeframe       = tf,
                signal_type     = SignalType.BREAKOUT_HIGH,
                direction       = Direction.BULLISH,
                confidence      = min(confidence, 100),
                price_at_signal = price,
                indicators      = self._key_indicators(latest),
                notes           = f"Breaking {lookback}-period high {recent_high:.2f} | RVOL {rvol:.1f}x",
            ))

        # Breakdown below 20-period low
        if price < recent_low:
            confidence = 40   # lower base — needs multiple confirmations to trade

            # Volume: institutional distribution shows as high RVOL (≥1.8×).
            # Low-volume breakdowns are retail panic into institutional support — traps.
            if rvol > 2.5:        confidence += 20
            elif rvol > 1.8:      confidence += 12
            # below 1.8× → no bonus; breakdown likely retail, not institutional

            if latest.get("ema_stack") == -1: confidence += 15
            if not latest.get("above_200ema"): confidence += 10

            # Consolidation width: wide base breakdowns have 67% continuation,
            # narrow range breakdowns only 43% (Bulkowski, 60,000+ setups).
            atr_val = latest.get(f"atr_{self._cfg.atr_period}") or latest.get("atr_14", 0)
            if atr_val:
                if (recent_high - recent_low) >= 2.0 * atr_val:
                    confidence += 15   # wide base — genuine support failure
                else:
                    confidence -= 10   # narrow range — likely noise

            # RSI filter: entering an already-oversold breakdown risks a bounce
            # before continuation. RSI < 30 on trigger TF = bounce likely.
            rsi_col = f"rsi_{self._cfg.rsi_period}"
            if rsi_col in df.columns:
                curr_rsi = df[rsi_col].iloc[-1]
                if not pd.isna(curr_rsi):
                    if float(curr_rsi) < 30:   confidence -= 20   # oversold, bounce risk
                    elif float(curr_rsi) < 40: confidence -= 10   # getting oversold

            # Round number support trap: ₹100/₹500/₹1000 round numbers attract
            # support buying in India (retail + LIC). Breakdown near them often fails.
            if recent_low > 0:
                round_level = round(recent_low / 100) * 100
                if round_level > 0 and abs(recent_low - round_level) / recent_low < 0.005:
                    confidence -= 15   # too close to round number support

            signals.append(Signal(
                trading_symbol  = symbol,
                timeframe       = tf,
                signal_type     = SignalType.BREAKOUT_LOW,
                direction       = Direction.BEARISH,
                confidence      = min(max(confidence, 0), 100),
                price_at_signal = price,
                indicators      = self._key_indicators(latest),
                notes           = (
                    f"Breaking {lookback}-period low {recent_low:.2f} | "
                    f"RVOL {rvol:.1f}x | range {(recent_high-recent_low):.2f}"
                ),
            ))

        return signals

    # ── EMA Signals ───────────────────────────────────────────────────────────

    def _ema_signals(
        self, df: pd.DataFrame, symbol: str, tf: str, price: float, latest: dict
    ) -> list[Signal]:
        signals = []
        cfg = self._cfg

        fast_col = f"ema_{cfg.ema_fast}"
        slow_col = f"ema_{cfg.ema_slow}"

        if fast_col not in df.columns or slow_col not in df.columns or len(df) < 3:
            return signals

        # EMA crossover (current bar vs previous bar)
        curr_fast = df[fast_col].iloc[-1]
        curr_slow = df[slow_col].iloc[-1]
        prev_fast = df[fast_col].iloc[-2]
        prev_slow = df[slow_col].iloc[-2]

        if prev_fast <= prev_slow and curr_fast > curr_slow:
            confidence = 55
            if latest.get("above_200ema"):     confidence += 15
            if latest.get("rsi_zone") == 1:    confidence += 10   # neutral RSI = not overbought
            signals.append(Signal(
                trading_symbol  = symbol,
                timeframe       = tf,
                signal_type     = SignalType.EMA_CROSSOVER_UP,
                direction       = Direction.BULLISH,
                confidence      = min(confidence, 100),
                price_at_signal = price,
                indicators      = self._key_indicators(latest),
                notes           = f"EMA{cfg.ema_fast} crossed above EMA{cfg.ema_slow}",
            ))

        if prev_fast >= prev_slow and curr_fast < curr_slow:
            confidence = 55
            if not latest.get("above_200ema"):  confidence += 15
            if latest.get("rsi_zone") == 1:     confidence += 10
            signals.append(Signal(
                trading_symbol  = symbol,
                timeframe       = tf,
                signal_type     = SignalType.EMA_CROSSOVER_DOWN,
                direction       = Direction.BEARISH,
                confidence      = min(confidence, 100),
                price_at_signal = price,
                indicators      = self._key_indicators(latest),
                notes           = f"EMA{cfg.ema_fast} crossed below EMA{cfg.ema_slow}",
            ))

        return signals

    # ── Momentum Signals ──────────────────────────────────────────────────────

    def _momentum_signals(
        self, df: pd.DataFrame, symbol: str, tf: str, price: float, latest: dict
    ) -> list[Signal]:
        signals = []
        rsi_col = f"rsi_{self._cfg.rsi_period}"

        if rsi_col in df.columns and len(df) >= 3:
            curr_rsi = df[rsi_col].iloc[-1]
            prev_rsi = df[rsi_col].iloc[-2]

            # RSI turning up from oversold
            if prev_rsi < 30 and curr_rsi > prev_rsi:
                signals.append(Signal(
                    trading_symbol  = symbol,
                    timeframe       = tf,
                    signal_type     = SignalType.RSI_OVERSOLD,
                    direction       = Direction.BULLISH,
                    confidence      = 60,
                    price_at_signal = price,
                    indicators      = self._key_indicators(latest),
                    notes           = f"RSI {curr_rsi:.1f} turning up from oversold",
                ))

            # RSI turning down from overbought
            if prev_rsi > 70 and curr_rsi < prev_rsi:
                signals.append(Signal(
                    trading_symbol  = symbol,
                    timeframe       = tf,
                    signal_type     = SignalType.RSI_OVERBOUGHT,
                    direction       = Direction.BEARISH,
                    confidence      = 60,
                    price_at_signal = price,
                    indicators      = self._key_indicators(latest),
                    notes           = f"RSI {curr_rsi:.1f} turning down from overbought",
                ))

        # MACD cross
        if "macd" in df.columns and "macd_signal" in df.columns and len(df) >= 3:
            curr_m = df["macd"].iloc[-1]
            curr_s = df["macd_signal"].iloc[-1]
            prev_m = df["macd"].iloc[-2]
            prev_s = df["macd_signal"].iloc[-2]

            if prev_m <= prev_s and curr_m > curr_s:
                signals.append(Signal(
                    trading_symbol  = symbol,
                    timeframe       = tf,
                    signal_type     = SignalType.MACD_CROSS_UP,
                    direction       = Direction.BULLISH,
                    confidence      = 55,
                    price_at_signal = price,
                    indicators      = self._key_indicators(latest),
                    notes           = "MACD crossed above signal line",
                ))

            if prev_m >= prev_s and curr_m < curr_s:
                signals.append(Signal(
                    trading_symbol  = symbol,
                    timeframe       = tf,
                    signal_type     = SignalType.MACD_CROSS_DOWN,
                    direction       = Direction.BEARISH,
                    confidence      = 55,
                    price_at_signal = price,
                    indicators      = self._key_indicators(latest),
                    notes           = "MACD crossed below signal line",
                ))

        return signals

    # ── Volume Signals ────────────────────────────────────────────────────────

    def _volume_signals(
        self, df: pd.DataFrame, symbol: str, tf: str, price: float, latest: dict
    ) -> list[Signal]:
        signals = []
        rvol = latest.get("rvol", 1.0)

        if rvol and rvol > 2.0:
            direction = Direction.BULLISH if df["close"].iloc[-1] > df["open"].iloc[-1] else Direction.BEARISH
            signals.append(Signal(
                trading_symbol  = symbol,
                timeframe       = tf,
                signal_type     = SignalType.HIGH_RVOL,
                direction       = direction,
                confidence      = min(50 + int(rvol * 5), 85),
                price_at_signal = price,
                indicators      = self._key_indicators(latest),
                notes           = f"Volume {rvol:.1f}x above 20-day average",
            ))

        return signals

    # ── Volatility Signals ────────────────────────────────────────────────────

    def _volatility_signals(
        self, df: pd.DataFrame, symbol: str, tf: str, price: float, latest: dict
    ) -> list[Signal]:
        signals = []

        if "bb_width" not in df.columns or len(df) < 30:
            return signals

        bb_width     = df["bb_width"].iloc[-1]
        bb_width_pct = df["bb_width"].rank(pct=True).iloc[-1]   # 0=tightest, 1=widest
        prev_pct     = df["bb_width"].rank(pct=True).iloc[-2]

        # Squeeze: bandwidth in bottom 20%
        if bb_width_pct < 0.2:
            signals.append(Signal(
                trading_symbol  = symbol,
                timeframe       = tf,
                signal_type     = SignalType.BB_SQUEEZE,
                direction       = Direction.NEUTRAL,
                confidence      = 50,
                price_at_signal = price,
                indicators      = self._key_indicators(latest),
                notes           = f"BB squeeze: width percentile {bb_width_pct:.0%}",
            ))

        # Expansion: just exited squeeze (was <20%, now >20%)
        if prev_pct < 0.2 and bb_width_pct > 0.2:
            direction = Direction.BULLISH if df["close"].iloc[-1] > df["bb_mid"].iloc[-1] else Direction.BEARISH
            signals.append(Signal(
                trading_symbol  = symbol,
                timeframe       = tf,
                signal_type     = SignalType.BB_EXPANSION,
                direction       = direction,
                confidence      = 65,
                price_at_signal = price,
                indicators      = self._key_indicators(latest),
                notes           = "Bollinger Band expansion after squeeze",
            ))

        return signals

    # ── Opening Range Breakout ────────────────────────────────────────────────

    def _orb_signals(
        self, df: pd.DataFrame, symbol: str, tf: str, price: float, latest: dict
    ) -> list[Signal]:
        """
        Opening Range Breakout — 9:15 to 9:30 AM range.
        Only fires on 15min timeframe and between 9:30 AM–1:00 PM IST.
        The first 15min candle of the day (9:15 open) establishes the range.
        """
        if tf != "15min":
            return []

        # Index must be datetime to check time-of-day
        if not hasattr(df.index, "time"):
            return []

        try:
            current_ts   = df.index[-1]
            current_time = current_ts.time()
        except Exception:
            return []

        # Only valid during 9:30 AM – 1:00 PM IST
        if not (time(9, 30) <= current_time <= time(13, 0)):
            return []

        today        = current_ts.date()
        today_df     = df[df.index.date == today]
        if len(today_df) < 2:
            return []   # Need at least 2 candles (ORB candle + current)

        orb_candle = today_df.iloc[0]   # 9:15–9:30 candle
        orb_high   = orb_candle["high"]
        orb_low    = orb_candle["low"]
        rvol       = latest.get("rvol", 1.0)

        signals = []

        # Bullish ORB breakout
        if price > orb_high:
            confidence = 65
            if rvol > 1.5:                          confidence += 10
            if rvol > 2.0:                          confidence += 10
            if latest.get("above_200ema"):          confidence += 10
            if latest.get("ema_stack") == 1:        confidence += 5
            signals.append(Signal(
                trading_symbol  = symbol,
                timeframe       = tf,
                signal_type     = SignalType.ORB_BREAKOUT,
                direction       = Direction.BULLISH,
                confidence      = min(confidence, 100),
                price_at_signal = price,
                indicators      = self._key_indicators(latest),
                notes           = (
                    f"ORB breakout above {orb_high:.2f} | "
                    f"Range: {orb_high - orb_low:.2f} | RVOL {rvol:.1f}x"
                ),
            ))

        # Bearish ORB breakdown
        elif price < orb_low:
            confidence = 65
            if rvol > 1.5:                          confidence += 10
            if rvol > 2.0:                          confidence += 10
            if not latest.get("above_200ema"):      confidence += 10
            if latest.get("ema_stack") == -1:       confidence += 5
            signals.append(Signal(
                trading_symbol  = symbol,
                timeframe       = tf,
                signal_type     = SignalType.ORB_BREAKOUT,
                direction       = Direction.BEARISH,
                confidence      = min(confidence, 100),
                price_at_signal = price,
                indicators      = self._key_indicators(latest),
                notes           = (
                    f"ORB breakdown below {orb_low:.2f} | "
                    f"Range: {orb_high - orb_low:.2f} | RVOL {rvol:.1f}x"
                ),
            ))

        return signals

    # ── VWAP Reclaim / Rejection ──────────────────────────────────────────────

    def _vwap_signals(
        self, df: pd.DataFrame, symbol: str, tf: str, price: float, latest: dict
    ) -> list[Signal]:
        """
        VWAP Reclaim (bullish) — price crosses above VWAP with volume.
        VWAP Rejection (bearish) — price crosses below VWAP with volume.

        Only fires on intraday timeframes (1min, 5min, 15min) — VWAP is
        not meaningful on daily/weekly data.
        """
        if tf not in ("1min", "5min", "15min"):
            return []

        if "vwap" not in df.columns or len(df) < 3:
            return []

        vwap      = df["vwap"].iloc[-1]
        prev_vwap = df["vwap"].iloc[-2]
        prev_close = df["close"].iloc[-2]
        rvol      = latest.get("rvol", 1.0)

        # Skip if VWAP is NaN (outside market hours or insufficient data)
        if not vwap or pd.isna(vwap) or not prev_vwap or pd.isna(prev_vwap):
            return []

        signals = []

        # Bullish: previous close was below VWAP, current close is above
        if prev_close < prev_vwap and price > vwap:
            confidence = 60
            if rvol and rvol > 1.2:                 confidence += 10
            if rvol and rvol > 1.8:                 confidence += 10
            if latest.get("above_200ema"):          confidence += 10
            if latest.get("rsi_zone") == 1:         confidence += 5   # neutral RSI
            signals.append(Signal(
                trading_symbol  = symbol,
                timeframe       = tf,
                signal_type     = SignalType.VWAP_RECLAIM,
                direction       = Direction.BULLISH,
                confidence      = min(confidence, 100),
                price_at_signal = price,
                indicators      = self._key_indicators(latest),
                notes           = f"VWAP reclaim @ {vwap:.2f} | RVOL {rvol:.1f}x",
            ))

        # Bearish: previous close was above VWAP, current close is below
        elif prev_close > prev_vwap and price < vwap:
            confidence = 60
            if rvol and rvol > 1.2:                 confidence += 10
            if rvol and rvol > 1.8:                 confidence += 10
            if not latest.get("above_200ema"):      confidence += 10
            if latest.get("rsi_zone") == 1:         confidence += 5
            signals.append(Signal(
                trading_symbol  = symbol,
                timeframe       = tf,
                signal_type     = SignalType.VWAP_RECLAIM,
                direction       = Direction.BEARISH,
                confidence      = min(confidence, 100),
                price_at_signal = price,
                indicators      = self._key_indicators(latest),
                notes           = f"VWAP rejection below {vwap:.2f} | RVOL {rvol:.1f}x",
            ))

        return signals

    # ── Candlestick Patterns ──────────────────────────────────────────────────

    def _candlestick_signals(
        self, df: pd.DataFrame, symbol: str, tf: str, price: float, latest: dict
    ) -> list[Signal]:
        """
        Detects single and multi-candle Japanese candlestick reversal patterns.
        Patterns: Hammer, Shooting Star, Bullish/Bearish Engulfing, Morning/Evening Star.
        """
        if len(df) < 5:
            return []

        signals = []
        c  = df.iloc[-1]   # current candle
        p  = df.iloc[-2]   # previous candle
        p2 = df.iloc[-3]   # two bars ago (for 3-candle patterns)

        def _body(row):    return abs(row["close"] - row["open"])
        def _range(row):   return row["high"] - row["low"]
        def _upper_wick(row): return row["high"] - max(row["open"], row["close"])
        def _lower_wick(row): return min(row["open"], row["close"]) - row["low"]

        c_body  = _body(c);  c_range  = _range(c)
        p_body  = _body(p);  p_range  = _range(p)
        p2_body = _body(p2); p2_range = _range(p2)

        rvol = latest.get("rvol", 1.0) or 1.0

        # ── Hammer ────────────────────────────────────────────────────────────
        # Small body near top, lower shadow ≥ 2× body, tiny upper shadow.
        # Context: appears after a declining move (prior candle bearish).
        if c_range > 0 and c_body > 0:
            lower_wick = _lower_wick(c)
            upper_wick = _upper_wick(c)
            body_ratio = c_body / c_range

            is_hammer = (
                lower_wick >= 2.0 * c_body       and   # long lower shadow
                upper_wick <= 0.4 * c_body        and   # little upper shadow
                0.05 <= body_ratio <= 0.40         and   # has a body (not doji)
                p["close"] < p["open"]                   # prior candle bearish
            )
            if is_hammer:
                conf = 65
                if rvol > 1.5:                    conf += 10
                rsi_col = f"rsi_{self._cfg.rsi_period}"
                prev_rsi = df[rsi_col].iloc[-2] if rsi_col in df.columns else None
                if prev_rsi is not None and not pd.isna(prev_rsi) and prev_rsi < 38:
                    conf += 10   # prior oversold RSI strengthens the reversal
                if latest.get("ema_stack") == -1: conf += 5  # in downtrend = valid reversal
                signals.append(Signal(
                    trading_symbol  = symbol,
                    timeframe       = tf,
                    signal_type     = SignalType.HAMMER,
                    direction       = Direction.BULLISH,
                    confidence      = min(conf, 100),
                    price_at_signal = price,
                    indicators      = self._key_indicators(latest),
                    notes           = f"Hammer | lower wick {lower_wick:.2f} | body {c_body:.2f}",
                ))

        # ── Shooting Star ─────────────────────────────────────────────────────
        # Small body near bottom, upper shadow ≥ 2× body, tiny lower shadow.
        # Context: appears after a rising move (prior candle bullish).
        if c_range > 0 and c_body > 0:
            lower_wick = _lower_wick(c)
            upper_wick = _upper_wick(c)
            body_ratio = c_body / c_range

            is_shooting_star = (
                upper_wick >= 2.0 * c_body        and
                lower_wick <= 0.4 * c_body         and
                0.05 <= body_ratio <= 0.40          and
                p["close"] > p["open"]                   # prior candle bullish
            )
            if is_shooting_star:
                conf = 65
                if rvol > 1.5:                     conf += 10
                rsi_col = f"rsi_{self._cfg.rsi_period}"
                prev_rsi = df[rsi_col].iloc[-2] if rsi_col in df.columns else None
                if prev_rsi is not None and not pd.isna(prev_rsi) and prev_rsi > 62:
                    conf += 10   # prior overbought RSI strengthens the reversal
                if latest.get("ema_stack") == 1:  conf += 5   # in uptrend = valid reversal
                signals.append(Signal(
                    trading_symbol  = symbol,
                    timeframe       = tf,
                    signal_type     = SignalType.SHOOTING_STAR,
                    direction       = Direction.BEARISH,
                    confidence      = min(conf, 100),
                    price_at_signal = price,
                    indicators      = self._key_indicators(latest),
                    notes           = f"Shooting Star | upper wick {_upper_wick(c):.2f} | body {c_body:.2f}",
                ))

        # ── Bullish Engulfing ─────────────────────────────────────────────────
        # Current bullish candle's body completely engulfs previous bearish body.
        bullish_engulf = (
            p["close"] < p["open"]          and   # prior bearish
            c["close"] > c["open"]          and   # current bullish
            c["open"]  <= p["close"]        and   # opens at or below prior close
            c["close"] >= p["open"]               # closes at or above prior open
        )
        if bullish_engulf:
            conf = 70
            if rvol > 1.5:                        conf += 10
            if latest.get("above_200ema"):         conf += 5
            # Stronger if engulfing a large prior candle
            if p_body > 0 and c_body >= 1.5 * p_body: conf += 5
            signals.append(Signal(
                trading_symbol  = symbol,
                timeframe       = tf,
                signal_type     = SignalType.ENGULFING_BULL,
                direction       = Direction.BULLISH,
                confidence      = min(conf, 100),
                price_at_signal = price,
                indicators      = self._key_indicators(latest),
                notes           = f"Bullish Engulfing | body ratio {c_body/p_body:.1f}x" if p_body > 0 else "Bullish Engulfing",
            ))

        # ── Bearish Engulfing ─────────────────────────────────────────────────
        bearish_engulf = (
            p["close"] > p["open"]          and   # prior bullish
            c["close"] < c["open"]          and   # current bearish
            c["open"]  >= p["close"]        and   # opens at or above prior close
            c["close"] <= p["open"]               # closes at or below prior open
        )
        if bearish_engulf:
            conf = 70
            if rvol > 1.5:                        conf += 10
            if not latest.get("above_200ema"):     conf += 5
            if p_body > 0 and c_body >= 1.5 * p_body: conf += 5
            signals.append(Signal(
                trading_symbol  = symbol,
                timeframe       = tf,
                signal_type     = SignalType.ENGULFING_BEAR,
                direction       = Direction.BEARISH,
                confidence      = min(conf, 100),
                price_at_signal = price,
                indicators      = self._key_indicators(latest),
                notes           = f"Bearish Engulfing | body ratio {c_body/p_body:.1f}x" if p_body > 0 else "Bearish Engulfing",
            ))

        # ── Morning Star (3-candle bullish reversal) ──────────────────────────
        # c1 (p2): large bearish candle
        # c2 (p):  small body (indecision / star) — can be bullish or bearish
        # c3 (c):  large bullish candle closing above c1's midpoint
        if p2_range > 0 and c_range > 0:
            c1_mid = (p2["open"] + p2["close"]) / 2
            morning_star = (
                p2["close"] < p2["open"]                  and   # c1 bearish
                p2_body / p2_range > 0.45                 and   # c1 substantial body
                p_body < 0.35 * p2_body                   and   # c2 small body (star)
                c["close"]  > c["open"]                   and   # c3 bullish
                c_body / c_range > 0.45                   and   # c3 substantial body
                c["close"]  > c1_mid                            # c3 closes above c1 midpoint
            )
            if morning_star:
                conf = 75
                if rvol > 1.5:                    conf += 10
                if latest.get("rsi_zone") == 0:  conf += 5   # oversold context
                signals.append(Signal(
                    trading_symbol  = symbol,
                    timeframe       = tf,
                    signal_type     = SignalType.MORNING_STAR,
                    direction       = Direction.BULLISH,
                    confidence      = min(conf, 100),
                    price_at_signal = price,
                    indicators      = self._key_indicators(latest),
                    notes           = f"Morning Star | c3 closed {((c['close']-c1_mid)/c1_mid*100):.1f}% above c1 mid",
                ))

        # ── Evening Star (3-candle bearish reversal) ──────────────────────────
        if p2_range > 0 and c_range > 0:
            c1_mid = (p2["open"] + p2["close"]) / 2
            evening_star = (
                p2["close"] > p2["open"]                  and   # c1 bullish
                p2_body / p2_range > 0.45                 and   # c1 substantial body
                p_body < 0.35 * p2_body                   and   # c2 small body (star)
                c["close"]  < c["open"]                   and   # c3 bearish
                c_body / c_range > 0.45                   and   # c3 substantial body
                c["close"]  < c1_mid                            # c3 closes below c1 midpoint
            )
            if evening_star:
                conf = 75
                if rvol > 1.5:                    conf += 10
                if latest.get("rsi_zone") == 2:  conf += 5   # overbought context
                signals.append(Signal(
                    trading_symbol  = symbol,
                    timeframe       = tf,
                    signal_type     = SignalType.EVENING_STAR,
                    direction       = Direction.BEARISH,
                    confidence      = min(conf, 100),
                    price_at_signal = price,
                    indicators      = self._key_indicators(latest),
                    notes           = f"Evening Star | c3 closed {((c1_mid-c['close'])/c1_mid*100):.1f}% below c1 mid",
                ))

        return signals

    # ── Chart Patterns ────────────────────────────────────────────────────────

    def _chart_pattern_signals(
        self, df: pd.DataFrame, symbol: str, tf: str, price: float, latest: dict
    ) -> list[Signal]:
        """
        Detects multi-bar chart patterns:
        Double Bottom (W), Double Top (M), Bull/Bear Flag, Darvas Box, NR7.
        """
        signals = []
        if len(df) < 30:
            return signals

        signals += self._double_pattern(df, symbol, tf, price, latest)
        signals += self._flag_pattern(df, symbol, tf, price, latest)
        signals += self._darvas_box(df, symbol, tf, price, latest)
        signals += self._nr7_setup(df, symbol, tf, price, latest)
        return signals

    def _double_pattern(
        self, df: pd.DataFrame, symbol: str, tf: str, price: float, latest: dict
    ) -> list[Signal]:
        """Double Bottom (W) and Double Top (M) using swing high/low columns."""
        signals = []
        rvol = latest.get("rvol", 1.0) or 1.0

        # ── Double Bottom ─────────────────────────────────────────────────────
        if "swing_low" in df.columns:
            recent = df["swing_low"].iloc[-80:].dropna()
            if len(recent) >= 2:
                l1_val, l2_val = recent.iloc[-2], recent.iloc[-1]
                l1_idx, l2_idx = recent.index[-2], recent.index[-1]
                sep = (df.index.get_loc(l2_idx) - df.index.get_loc(l1_idx))  # bars apart
                price_diff = abs(l1_val - l2_val) / l1_val if l1_val else 1

                if sep >= 8 and price_diff < 0.025:   # close lows, 8+ bars apart
                    # Neckline = highest high between the two lows
                    between_mask = (df.index >= l1_idx) & (df.index <= l2_idx)
                    neckline = df.loc[between_mask, "high"].max()
                    if price > neckline:               # neckline broken
                        conf = 70
                        if rvol > 1.5:                 conf += 10
                        if latest.get("above_200ema"): conf += 5
                        conf = min(conf + max(0, int((1 - price_diff / 0.025) * 10)), 100)
                        signals.append(Signal(
                            trading_symbol  = symbol,
                            timeframe       = tf,
                            signal_type     = SignalType.DOUBLE_BOTTOM,
                            direction       = Direction.BULLISH,
                            confidence      = conf,
                            price_at_signal = price,
                            indicators      = self._key_indicators(latest),
                            notes           = f"Double Bottom W | lows {l1_val:.2f}/{l2_val:.2f} | neckline {neckline:.2f}",
                        ))

        # ── Double Top ────────────────────────────────────────────────────────
        if "swing_high" in df.columns:
            recent = df["swing_high"].iloc[-80:].dropna()
            if len(recent) >= 2:
                h1_val, h2_val = recent.iloc[-2], recent.iloc[-1]
                h1_idx, h2_idx = recent.index[-2], recent.index[-1]
                sep = (df.index.get_loc(h2_idx) - df.index.get_loc(h1_idx))
                price_diff = abs(h1_val - h2_val) / h1_val if h1_val else 1

                if sep >= 8 and price_diff < 0.020:   # tightened from 2.5% → 2.0%
                    between_mask = (df.index >= h1_idx) & (df.index <= h2_idx)
                    neckline = df.loc[between_mask, "low"].min()
                    if price < neckline:
                        conf = 65   # reduced base from 70
                        # RVOL on neckline break — Bulkowski: no-volume breaks fail 71%
                        if rvol > 1.5:    conf += 15
                        else:             conf -= 15
                        # Bear structure confirmation
                        if not latest.get("above_200ema"):      conf += 5
                        if latest.get("ema_stack") == -1:       conf += 10
                        conf = min(conf + max(0, int((1 - price_diff / 0.020) * 10)), 100)
                        # RSI divergence: right peak should have lower RSI than left peak
                        rsi_col = f"rsi_{self._cfg.rsi_period}"
                        rsi_note = ""
                        if rsi_col in df.columns:
                            h1_iloc = df.index.get_loc(h1_idx)
                            h2_iloc = df.index.get_loc(h2_idx)
                            rsi_at_h1 = float(df[rsi_col].iloc[h1_iloc]) if not pd.isna(df[rsi_col].iloc[h1_iloc]) else None
                            rsi_at_h2 = float(df[rsi_col].iloc[h2_iloc]) if not pd.isna(df[rsi_col].iloc[h2_iloc]) else None
                            if rsi_at_h1 is not None and rsi_at_h2 is not None:
                                if rsi_at_h2 < rsi_at_h1:
                                    conf += 15   # bearish RSI divergence confirmed
                                    rsi_note = f" | RSI div {rsi_at_h1:.0f}→{rsi_at_h2:.0f}"
                                else:
                                    conf -= 10   # no divergence, pattern is weaker
                        signals.append(Signal(
                            trading_symbol  = symbol,
                            timeframe       = tf,
                            signal_type     = SignalType.DOUBLE_TOP,
                            direction       = Direction.BEARISH,
                            confidence      = min(max(conf, 0), 100),
                            price_at_signal = price,
                            indicators      = self._key_indicators(latest),
                            notes           = f"Double Top M | highs {h1_val:.2f}/{h2_val:.2f} | neckline {neckline:.2f}{rsi_note}",
                        ))

        return signals

    def _flag_pattern(
        self, df: pd.DataFrame, symbol: str, tf: str, price: float, latest: dict
    ) -> list[Signal]:
        """
        Bull/Bear Flag: sharp move (pole) followed by tight consolidation, then breakout.
        Pole: ≥3% move over 3–10 bars. Flag: 5–15 bar tight range (< 50% pole retracement).
        """
        if len(df) < 25:
            return []

        signals = []
        rvol = latest.get("rvol", 1.0) or 1.0

        # Flag consolidation: last 5–10 bars
        flag_bars   = 7
        flag_df     = df.iloc[-(flag_bars + 1):-1]
        flag_high   = flag_df["high"].max()
        flag_low    = flag_df["low"].min()
        flag_range  = flag_high - flag_low

        # Pole: the move just before the flag
        pole_bars   = 8
        pole_df     = df.iloc[-(flag_bars + pole_bars + 1):-(flag_bars + 1)]
        if pole_df.empty:
            return []
        pole_start  = pole_df.iloc[0]["close"]
        pole_end    = pole_df.iloc[-1]["close"]
        pole_move   = pole_end - pole_start
        pole_pct    = abs(pole_move) / pole_start if pole_start else 0

        # Pole must be ≥ 3% and flag must be tight (< 60% of pole range)
        pole_range  = abs(pole_df["high"].max() - pole_df["low"].min())

        # ── Bull Flag ─────────────────────────────────────────────────────────
        if pole_move > 0 and pole_pct >= 0.03:
            tight = flag_range < 0.60 * pole_range
            if tight and price > flag_high:     # breakout above flag
                conf = 65
                if rvol > 1.5:                   conf += 10
                if rvol > 2.0:                   conf += 5
                if latest.get("ema_stack") == 1: conf += 10
                signals.append(Signal(
                    trading_symbol  = symbol,
                    timeframe       = tf,
                    signal_type     = SignalType.BULL_FLAG,
                    direction       = Direction.BULLISH,
                    confidence      = min(conf, 100),
                    price_at_signal = price,
                    indicators      = self._key_indicators(latest),
                    notes           = f"Bull Flag | pole {pole_pct*100:.1f}% | flag range {flag_range:.2f}",
                ))

        # ── Bear Flag ─────────────────────────────────────────────────────────
        elif pole_move < 0 and pole_pct >= 0.03:
            tight = flag_range < 0.60 * pole_range
            if tight and price < flag_low:      # breakdown below flag
                conf = 65
                if rvol > 1.5:                    conf += 10
                if rvol > 2.0:                    conf += 5
                if latest.get("ema_stack") == -1: conf += 10
                signals.append(Signal(
                    trading_symbol  = symbol,
                    timeframe       = tf,
                    signal_type     = SignalType.BEAR_FLAG,
                    direction       = Direction.BEARISH,
                    confidence      = min(conf, 100),
                    price_at_signal = price,
                    indicators      = self._key_indicators(latest),
                    notes           = f"Bear Flag | pole {pole_pct*100:.1f}% drop | flag range {flag_range:.2f}",
                ))

        return signals

    def _darvas_box(
        self, df: pd.DataFrame, symbol: str, tf: str, price: float, latest: dict
    ) -> list[Signal]:
        """
        Darvas Box: price consolidates in a box (top and bottom hold for N bars),
        then breaks out above box top. Works best on 1day / 1hr timeframes.
        """
        if len(df) < 25:
            return []

        rvol = latest.get("rvol", 1.0) or 1.0
        box_period = 15   # bars forming the box
        lookback   = df.iloc[-(box_period + 1):-1]

        box_top    = lookback["high"].max()
        box_bottom = lookback["low"].min()
        box_range  = box_top - box_bottom

        # Box is valid if the last 5 bars stayed within the box (no new highs)
        last_5 = df.iloc[-6:-1]
        box_intact = last_5["high"].max() <= box_top * 1.005   # 0.5% tolerance

        # A proper box needs meaningful range (> 1% of price) and price was ranging
        has_range = box_range / box_top > 0.01 if box_top > 0 else False

        if box_intact and has_range and price > box_top:
            conf = 70
            if rvol > 2.0:                        conf += 15
            elif rvol > 1.5:                      conf += 8
            if latest.get("above_200ema"):        conf += 10
            adx = latest.get("adx")
            if adx and not pd.isna(adx) and adx > 25: conf += 5
            return [Signal(
                trading_symbol  = symbol,
                timeframe       = tf,
                signal_type     = SignalType.DARVAS_BREAKOUT,
                direction       = Direction.BULLISH,
                confidence      = min(conf, 100),
                price_at_signal = price,
                indicators      = self._key_indicators(latest),
                notes           = f"Darvas Box breakout | box {box_bottom:.2f}–{box_top:.2f} | RVOL {rvol:.1f}x",
            )]

        return []

    def _nr7_setup(
        self, df: pd.DataFrame, symbol: str, tf: str, price: float, latest: dict
    ) -> list[Signal]:
        """
        NR7: today's candle has the narrowest range of the past 7 bars.
        Signals volatility contraction → imminent expansion/breakout.
        Direction determined by whether price is above or below the candle's midpoint.
        """
        if len(df) < 8:
            return []

        curr_range  = df.iloc[-1]["high"] - df.iloc[-1]["low"]
        prior_ranges = [(df.iloc[-(i+1)]["high"] - df.iloc[-(i+1)]["low"]) for i in range(1, 7)]

        if curr_range <= 0 or curr_range >= min(prior_ranges):
            return []

        midpoint  = (df.iloc[-1]["high"] + df.iloc[-1]["low"]) / 2
        direction = Direction.BULLISH if price >= midpoint else Direction.BEARISH

        conf = 55
        if "bb_width" in df.columns:
            bb_pct = df["bb_width"].rank(pct=True).iloc[-1]
            if bb_pct < 0.25:   conf += 10   # BB squeeze confirms NR7 contraction

        return [Signal(
            trading_symbol  = symbol,
            timeframe       = tf,
            signal_type     = SignalType.NR7_SETUP,
            direction       = direction,
            confidence      = min(conf, 100),
            price_at_signal = price,
            indicators      = self._key_indicators(latest),
            notes           = f"NR7 | range {curr_range:.2f} < min of last 6 ({min(prior_ranges):.2f})",
        )]

    def _key_indicators(self, latest: dict) -> dict:
        """Extract the most important indicator values for the signal snapshot."""
        cfg = self._cfg
        keys = [
            "close",
            f"ema_{cfg.ema_fast}", f"ema_{cfg.ema_mid}",
            f"ema_{cfg.ema_slow}", f"ema_{cfg.ema_trend}",
            f"rsi_{cfg.rsi_period}", "macd", "macd_signal", "macd_hist",
            f"atr_{cfg.atr_period}", "atr_pct", "rvol", "vwap",
            "bb_pct", "bb_width", "adx",
            "ema_stack", "above_200ema", "rsi_zone",
        ]
        return {k: latest[k] for k in keys if k in latest}


# ─── Multi-Timeframe Confluence ───────────────────────────────────────────────

class MultiTimeframeSignalEngine:
    """
    Runs signal detection across multiple timeframes for a single symbol.
    Computes a confluence score: signals aligned across timeframes score higher.
    Applies RegimeFilter before returning — only regime-appropriate signals pass.

    Accepts an optional config dict (from bot_config) so all parameters can be
    changed at runtime via the dashboard without restarting the bot.
    """

    # Default weights — overridden per-instance when config is provided
    _DEFAULT_TF_WEIGHTS = {
        "1min":  0.5,
        "5min":  1.0,
        "15min": 1.5,
        "1hr":   2.0,
        "1day":  3.0,
    }

    def __init__(self, config: dict | None = None):
        self._config = config or {}

        # Build IndicatorConfig from config, falling back to defaults
        from services.technical_engine.indicators import IndicatorConfig
        ind_cfg = IndicatorConfig(
            ema_fast          = int(self._config.get("ema_fast",          9)),
            ema_mid           = int(self._config.get("ema_mid",           21)),
            ema_slow          = int(self._config.get("ema_slow",          50)),
            ema_trend         = int(self._config.get("ema_trend",         200)),
            rsi_period        = int(self._config.get("rsi_period",        14)),
            macd_fast         = int(self._config.get("macd_fast",         12)),
            macd_slow         = int(self._config.get("macd_slow",         26)),
            macd_signal       = int(self._config.get("macd_signal_period", 9)),
            bb_period         = int(self._config.get("bb_period",         20)),
            bb_std            = float(self._config.get("bb_std",          2.0)),
            atr_period        = int(self._config.get("atr_period",        14)),
        )

        self._detector = SignalDetector(cfg=ind_cfg)
        self._filter   = RegimeFilter(config=self._config)

    @property
    def _timeframe_weights(self) -> dict[str, float]:
        return {
            "1min":  float(self._config.get("tw_1min",  0.5)),
            "5min":  float(self._config.get("tw_5min",  1.0)),
            "15min": float(self._config.get("tw_15min", 1.5)),
            "1hr":   float(self._config.get("tw_1hr",   2.0)),
            "1day":  float(self._config.get("tw_1day",  3.0)),
        }

    @property
    def _enabled_strategies(self) -> set[str]:
        names = [
            "breakout", "ema", "momentum", "volume", "volatility",
            "orb", "vwap", "candlestick", "chart_patterns",
        ]
        return {n for n in names if self._config.get(f"strategy_{n}", True)}

    def analyse(
        self,
        symbol: str,
        ohlcv_by_tf: dict[str, pd.DataFrame],
        regime: str = "UNKNOWN",
    ) -> list[Signal]:
        """
        Run detection on each timeframe and return a merged list of signals,
        with confidence scores adjusted for multi-timeframe alignment.
        Applies regime filter: removes signals unsuitable for current market conditions.
        """
        min_conf  = int(self._config.get("signal_min_confidence", 40))
        enabled   = self._enabled_strategies
        all_signals: list[Signal] = []
        directions_by_tf: dict[str, Direction] = {}

        for tf, df in ohlcv_by_tf.items():
            if df is None or df.empty:
                continue
            signals = self._detector.detect(
                df, symbol, tf,
                enabled_strategies=enabled,
                min_confidence=min_conf,
            )
            all_signals.extend(signals)
            if signals:
                bull = sum(1 for s in signals if s.direction == Direction.BULLISH)
                bear = sum(1 for s in signals if s.direction == Direction.BEARISH)
                directions_by_tf[tf] = Direction.BULLISH if bull > bear else Direction.BEARISH

        # Boost confidence for signals whose direction aligns with higher timeframes
        all_signals = self._apply_confluence_boost(all_signals, directions_by_tf)

        # Apply regime filter — remove signals that don't suit current conditions
        all_signals = self._filter.apply(all_signals, regime)

        # Apply per-signal-type minimum confidence overrides (from backtest findings)
        orb_min  = int(self._config.get("orb_min_confidence",  70))
        vwap_min = int(self._config.get("vwap_min_confidence", 70))
        all_signals = [
            s for s in all_signals
            if not (s.signal_type == SignalType.ORB_BREAKOUT  and s.confidence < orb_min)
            and not (s.signal_type == SignalType.VWAP_RECLAIM and s.confidence < vwap_min)
        ]

        all_signals.sort(key=lambda s: s.confidence, reverse=True)
        return all_signals

    def _apply_confluence_boost(
        self,
        signals: list[Signal],
        directions: dict[str, Direction],
    ) -> list[Signal]:
        higher_tfs = ["1day", "1hr", "15min"]
        weights    = self._timeframe_weights

        for signal in signals:
            aligned_weight = 0.0
            total_weight   = 0.0

            for htf in higher_tfs:
                if htf == signal.timeframe or htf not in directions:
                    continue
                w = weights.get(htf, 1.0)
                total_weight += w
                if directions[htf] == signal.direction:
                    aligned_weight += w

            if total_weight > 0:
                confluence_ratio = aligned_weight / total_weight
                boost = int(confluence_ratio * 20)
                signal.confidence = min(signal.confidence + boost, 100)

        return signals
