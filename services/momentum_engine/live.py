"""
services/momentum_engine/live.py
──────────────────────────────────
Live adapter for the momentum engine.

Converts MomentumSignal → Signal so TradeExecutor can handle it
without any changes to detection logic, scoring, or R:R.

Key design:
  - Only fires when Nifty regime = TRENDING_UP (same gate as backtest)
  - Daily TF only — reads "1day" candle buffer
  - One signal per symbol per calendar day (Redis cooldown key prevents
    the same Darvas setup firing on every 15min trigger throughout the day)
  - compute_all() called here, same as backtest engine does before detect()

Nothing in MomentumDetector is changed. This is a pure translation layer.
"""
from __future__ import annotations

import asyncio
from datetime import date

import pandas as pd
import structlog

from services.momentum_engine.signals import MomentumDetector, MomentumSignal
from services.technical_engine.indicators import compute_all
from services.technical_engine.signal_generator import Direction, Signal, SignalType

log = structlog.get_logger(__name__)

# Minimum daily candles needed for reliable indicator warm-up
MIN_DAILY_BARS = 60


class MomentumLiveEngine:
    """
    Wraps MomentumDetector for use in the live candle loop.

    Usage (called from main.py._run_signals):
        engine  = MomentumLiveEngine()
        signals = await engine.detect(symbol, daily_df, regime, redis)
    """

    def __init__(self) -> None:
        self._detector = MomentumDetector()

    async def detect(
        self,
        symbol:    str,
        daily_df:  pd.DataFrame,
        regime:    str,
        redis,                     # aioredis client for cooldown check
    ) -> list[Signal]:
        """
        Run momentum detection on the latest daily candle.
        Returns [] immediately if:
          - regime != TRENDING_UP
          - not enough daily history
          - already fired for this symbol today (cooldown)
        """
        if regime != "TRENDING_UP":
            return []

        if len(daily_df) < MIN_DAILY_BARS:
            log.debug("momentum_live.insufficient_bars", symbol=symbol, bars=len(daily_df))
            return []

        # Per-symbol, per-day cooldown — prevents same setup re-firing every 15min
        cooldown_key = f"momentum_live:fired:{symbol}:{date.today().isoformat()}"
        already_fired = await redis.get(cooldown_key)
        if already_fired:
            return []

        # compute_all() mirrors what MomentumBacktestEngine does before calling detect()
        try:
            df_with_indicators = compute_all(daily_df.copy())
        except Exception as e:
            log.warning("momentum_live.indicator_error", symbol=symbol, error=str(e))
            return []

        if len(df_with_indicators) < MIN_DAILY_BARS:
            return []

        # Run detection — zero changes to MomentumDetector logic
        try:
            momentum_signals: list[MomentumSignal] = self._detector.detect(
                df_with_indicators, symbol
            )
        except Exception as e:
            log.warning("momentum_live.detect_error", symbol=symbol, error=str(e))
            return []

        if not momentum_signals:
            return []

        # Set cooldown for today so this symbol doesn't re-fire until tomorrow
        await redis.setex(cooldown_key, 86_400, "1")

        # Convert MomentumSignal → Signal (format translation only)
        live_signals: list[Signal] = []
        for ms in momentum_signals:
            try:
                signal_type = SignalType(ms.signal_type.value)
            except ValueError:
                log.warning(
                    "momentum_live.unknown_signal_type",
                    symbol=symbol,
                    type=ms.signal_type.value,
                )
                continue

            sig = Signal(
                trading_symbol  = ms.symbol,
                timeframe       = "1day",
                signal_type     = signal_type,
                direction       = Direction.BULLISH,   # momentum engine is long-only
                confidence      = ms.confidence,
                price_at_signal = ms.price,
                indicators      = {
                    "atr_14":       ms.atr,
                    "rvol":         ms.rvol,
                    "rsi_14":       ms.rsi,
                    "adx":          ms.adx,
                    "ema_stack":    ms.ema_stack,
                    "above_200ema": ms.above_200ema,
                    **ms.indicators,
                },
            )
            live_signals.append(sig)
            log.info(
                "momentum_live.signal",
                symbol     = symbol,
                signal     = signal_type.value,
                confidence = ms.confidence,
                price      = ms.price,
                rvol       = ms.rvol,
                rsi        = ms.rsi,
            )

        return live_signals
