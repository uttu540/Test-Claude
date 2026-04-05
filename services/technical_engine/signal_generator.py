"""
services/technical_engine/signal_generator.py
───────────────────────────────────────────────
Detects trading signals from computed indicator DataFrames.

A "signal" is NOT a trade recommendation — it is an observation that
certain technical conditions are met. The AI strategy engine decides
whether to act on signals.

Signal confidence is scored 0–100 based on multi-timeframe confluence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

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


# ─── Per-timeframe Signal Detector ───────────────────────────────────────────

class SignalDetector:
    """
    Detects signals on a single timeframe DataFrame.
    Returns a list of Signal objects.
    """

    def __init__(self, cfg: IndicatorConfig = IndicatorConfig()):
        self._cfg = cfg

    def detect(self, df: pd.DataFrame, symbol: str, timeframe: str) -> list[Signal]:
        if len(df) < 50:   # Need enough history for reliable indicators
            return []

        df = compute_all(df, self._cfg)
        signals: list[Signal] = []
        latest = get_latest(df)
        price  = latest.get("close", 0)

        signals += self._breakout_signals(df, symbol, timeframe, price, latest)
        signals += self._ema_signals(df, symbol, timeframe, price, latest)
        signals += self._momentum_signals(df, symbol, timeframe, price, latest)
        signals += self._volume_signals(df, symbol, timeframe, price, latest)
        signals += self._volatility_signals(df, symbol, timeframe, price, latest)

        # Filter to only signals with confidence >= 40
        signals = [s for s in signals if s.confidence >= 40]

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
            confidence = 50
            if rvol > 1.5:   confidence += 15
            if rvol > 2.0:   confidence += 10
            if latest.get("ema_stack") == -1: confidence += 15
            if not latest.get("above_200ema"): confidence += 10
            signals.append(Signal(
                trading_symbol  = symbol,
                timeframe       = tf,
                signal_type     = SignalType.BREAKOUT_LOW,
                direction       = Direction.BEARISH,
                confidence      = min(confidence, 100),
                price_at_signal = price,
                indicators      = self._key_indicators(latest),
                notes           = f"Breaking {lookback}-period low {recent_low:.2f} | RVOL {rvol:.1f}x",
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

    @staticmethod
    def _key_indicators(latest: dict) -> dict:
        """Extract the most important indicator values for the signal snapshot."""
        keys = [
            "close", "ema_9", "ema_21", "ema_50", "ema_200",
            "rsi_14", "macd", "macd_signal", "macd_hist",
            "atr_14", "atr_pct", "rvol",
            "bb_pct", "bb_width", "adx",
            "ema_stack", "above_200ema", "rsi_zone",
        ]
        return {k: latest[k] for k in keys if k in latest}


# ─── Multi-Timeframe Confluence ───────────────────────────────────────────────

class MultiTimeframeSignalEngine:
    """
    Runs signal detection across multiple timeframes for a single symbol.
    Computes a confluence score: signals aligned across timeframes score higher.
    """

    TIMEFRAME_WEIGHTS = {
        "1min":  0.5,
        "5min":  1.0,
        "15min": 1.5,
        "1hr":   2.0,
        "1day":  3.0,
    }

    def __init__(self):
        self._detector = SignalDetector()

    def analyse(
        self,
        symbol: str,
        ohlcv_by_tf: dict[str, pd.DataFrame],
    ) -> list[Signal]:
        """
        Run detection on each timeframe and return a merged list of signals,
        with confidence scores adjusted for multi-timeframe alignment.
        """
        all_signals: list[Signal] = []
        directions_by_tf: dict[str, Direction] = {}

        for tf, df in ohlcv_by_tf.items():
            if df is None or df.empty:
                continue
            signals = self._detector.detect(df, symbol, tf)
            all_signals.extend(signals)
            if signals:
                # Dominant direction for this timeframe
                bull = sum(1 for s in signals if s.direction == Direction.BULLISH)
                bear = sum(1 for s in signals if s.direction == Direction.BEARISH)
                directions_by_tf[tf] = Direction.BULLISH if bull > bear else Direction.BEARISH

        # Boost confidence for signals whose direction aligns with higher timeframes
        all_signals = self._apply_confluence_boost(all_signals, directions_by_tf)
        all_signals.sort(key=lambda s: s.confidence, reverse=True)
        return all_signals

    def _apply_confluence_boost(
        self,
        signals: list[Signal],
        directions: dict[str, Direction],
    ) -> list[Signal]:
        higher_tfs = ["1day", "1hr", "15min"]

        for signal in signals:
            aligned_weight = 0.0
            total_weight   = 0.0

            for htf in higher_tfs:
                if htf == signal.timeframe or htf not in directions:
                    continue
                w = self.TIMEFRAME_WEIGHTS.get(htf, 1.0)
                total_weight += w
                if directions[htf] == signal.direction:
                    aligned_weight += w

            if total_weight > 0:
                confluence_ratio = aligned_weight / total_weight
                # Boost up to +20 points for full alignment
                boost = int(confluence_ratio * 20)
                signal.confidence = min(signal.confidence + boost, 100)

        return signals
