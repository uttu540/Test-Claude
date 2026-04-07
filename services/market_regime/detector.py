"""
services/market_regime/detector.py
────────────────────────────────────
Classifies the current market into one of four regimes using NIFTY 50
(or any representative index) candle data and India VIX.

Four regimes:
  TRENDING_UP     — ADX ≥ 25, EMA stack bullish, price above EMA-50
  TRENDING_DOWN   — ADX ≥ 25, EMA stack bearish, price below EMA-50
  RANGING         — ADX < 20, no clear directional trend
  HIGH_VOLATILITY — India VIX > 20 (overrides all trend classification)

Redis key written: `market:regime`  (TTL 20 min — covers one 15min cycle + buffer)
Redis key written: `market:regime:detail` — JSON with full indicator snapshot

Called from main.py on every 15min candle close for the market proxy symbol.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

import pandas as pd
import structlog

from database.connection import get_redis
from services.technical_engine.indicators import IndicatorConfig, compute_all, get_latest

log = structlog.get_logger(__name__)

Regime = Literal["TRENDING_UP", "TRENDING_DOWN", "RANGING", "HIGH_VOLATILITY", "UNKNOWN"]

# India VIX threshold for HIGH_VOLATILITY override
VIX_HIGH_THRESHOLD = 20.0

# ADX thresholds
ADX_TRENDING = 25.0
ADX_RANGING  = 20.0


class MarketRegimeDetector:
    """
    Classifies current market regime from OHLCV candle data.

    Usage (called every 15min candle close in main.py):
        detector = get_regime_detector()
        regime = await detector.detect_and_publish(nifty_df, india_vix=14.2)
    """

    def __init__(self, cfg: IndicatorConfig = IndicatorConfig()) -> None:
        self._cfg = cfg

    def detect(self, df: pd.DataFrame, india_vix: float | None = None) -> Regime:
        """
        Pure function: classify regime from a OHLCV DataFrame.
        Needs at least 50 rows for reliable ADX computation.
        """
        if df is None or len(df) < 50:
            log.debug("regime.insufficient_data", rows=len(df) if df is not None else 0)
            return "UNKNOWN"

        # VIX override — high fear environment trumps trend classification
        if india_vix is not None and india_vix > VIX_HIGH_THRESHOLD:
            log.info("regime.high_volatility", vix=india_vix)
            return "HIGH_VOLATILITY"

        try:
            df = compute_all(df, self._cfg)
        except Exception as e:
            log.warning("regime.indicator_error", error=str(e))
            return "UNKNOWN"

        latest = get_latest(df)
        if not latest:
            return "UNKNOWN"

        adx       = latest.get("adx") or 0.0
        ema_stack = latest.get("ema_stack") or 0
        close     = latest.get("close") or 0.0
        ema_50    = latest.get(f"ema_{self._cfg.ema_slow}") or 0.0
        ema_200   = latest.get(f"ema_{self._cfg.ema_trend}") or 0.0

        # Strong trend (ADX ≥ 25)
        if adx >= ADX_TRENDING:
            if ema_stack == 1 and close > ema_50:
                return "TRENDING_UP"
            if ema_stack == -1 and close < ema_50:
                return "TRENDING_DOWN"
            # ADX strong but mixed EMAs — use 200 EMA as tiebreaker
            if ema_200:
                return "TRENDING_UP" if close > ema_200 else "TRENDING_DOWN"
            return "TRENDING_UP" if ema_stack >= 0 else "TRENDING_DOWN"

        # Clear range (ADX < 20)
        if adx < ADX_RANGING:
            return "RANGING"

        # Transitional zone (ADX 20–25) — lean on EMA stack
        if ema_stack == 1:
            return "TRENDING_UP"
        if ema_stack == -1:
            return "TRENDING_DOWN"

        return "RANGING"

    async def publish(self, regime: Regime, detail: dict | None = None) -> None:
        """Write regime + optional indicator detail to Redis."""
        redis = get_redis()
        await redis.setex("market:regime", 1_200, regime)   # 20 min TTL

        if detail:
            await redis.setex(
                "market:regime:detail",
                1_200,
                json.dumps({**detail, "regime": regime, "ts": datetime.now().isoformat()}),
            )

        log.info("regime.published", regime=regime)

    async def detect_and_publish(
        self,
        df: pd.DataFrame,
        india_vix: float | None = None,
    ) -> Regime:
        """Detect regime, publish to Redis, and return the result."""
        regime = self.detect(df, india_vix)

        # Build a lightweight detail dict for diagnostics
        detail: dict = {"india_vix": india_vix}
        if df is not None and len(df) >= 50:
            try:
                enriched = compute_all(df, self._cfg)
                latest   = get_latest(enriched)
                detail.update({
                    "adx":       latest.get("adx"),
                    "ema_stack": latest.get("ema_stack"),
                    "close":     latest.get("close"),
                    "ema_50":    latest.get(f"ema_{self._cfg.ema_slow}"),
                    "ema_200":   latest.get(f"ema_{self._cfg.ema_trend}"),
                })
            except Exception:
                pass

        await self.publish(regime, detail)
        return regime


# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: MarketRegimeDetector | None = None


def get_regime_detector() -> MarketRegimeDetector:
    global _instance
    if _instance is None:
        _instance = MarketRegimeDetector()
    return _instance
