"""
services/market_regime/detector.py
────────────────────────────────────
Classifies the current market into one of four regimes using NIFTY 50
(or any representative index) candle data, India VIX, GIFT Nifty gap,
and aggregate news sentiment.

Four regimes:
  TRENDING_UP     — ADX ≥ 25, EMA stack bullish, price above EMA-50
  TRENDING_DOWN   — ADX ≥ 25, EMA stack bearish, price below EMA-50
  RANGING         — ADX < 20, no clear directional trend
  HIGH_VOLATILITY — India VIX > 20 (overrides all trend classification)

Override logic (applied after technical base):
  GIFT Nifty gap ≥ +1.5% on RANGING       → promote to TRENDING_UP
  GIFT Nifty gap ≤ -1.5% on RANGING       → promote to TRENDING_DOWN
  GIFT Nifty gap ≤ -2.5% on any regime    → HIGH_VOLATILITY (panic open)
  Strong negative news (< -0.5) on RANGING → promote to TRENDING_DOWN
  Strong positive news (> +0.5) on RANGING → promote to TRENDING_UP

Redis key written: `market:regime`       (TTL 20 min)
Redis key written: `market:regime:detail` — JSON with full indicator snapshot

Called from main.py at startup and on daily candle close.
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

    def detect(
        self,
        df: pd.DataFrame,
        india_vix: float | None = None,
        gift_nifty_pct: float | None = None,
        news_sentiment: float | None = None,
    ) -> Regime:
        """
        Classify regime from OHLCV DataFrame plus optional real-time signals.

        Args:
            df:              NIFTY 50 daily OHLCV — needs ≥ 50 rows.
            india_vix:       India VIX current level.
            gift_nifty_pct:  GIFT Nifty % change from prev close (pre-market cue).
            news_sentiment:  Aggregate news sentiment in [-1.0, +1.0].
        """
        if df is None or len(df) < 50:
            log.debug("regime.insufficient_data", rows=len(df) if df is not None else 0)
            return "UNKNOWN"

        # ── Hard overrides (checked before technicals) ────────────────────────

        # Panic open: GIFT Nifty gapping down hard → HIGH_VOLATILITY regardless
        if gift_nifty_pct is not None and gift_nifty_pct <= -2.5:
            log.info("regime.gift_panic", gift_pct=gift_nifty_pct)
            return "HIGH_VOLATILITY"

        # VIX override — high fear environment trumps trend classification
        if india_vix is not None and india_vix > VIX_HIGH_THRESHOLD:
            log.info("regime.high_volatility", vix=india_vix)
            return "HIGH_VOLATILITY"

        # ── Technical base regime ─────────────────────────────────────────────
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
                base = "TRENDING_UP"
            elif ema_stack == -1 and close < ema_50:
                base = "TRENDING_DOWN"
            elif ema_200:
                base = "TRENDING_UP" if close > ema_200 else "TRENDING_DOWN"
            else:
                base = "TRENDING_UP" if ema_stack >= 0 else "TRENDING_DOWN"

        # Clear range (ADX < 20)
        elif adx < ADX_RANGING:
            base = "RANGING"

        # Transitional zone (ADX 20–25) — lean on EMA stack
        elif ema_stack == 1:
            base = "TRENDING_UP"
        elif ema_stack == -1:
            base = "TRENDING_DOWN"
        else:
            base = "RANGING"

        # ── Soft overrides: GIFT Nifty + news nudge RANGING only ─────────────
        # We only upgrade RANGING — strong trends aren't reversed by pre-market data.
        if base == "RANGING":
            base = self._apply_soft_overrides(base, gift_nifty_pct, news_sentiment)

        log.info(
            "regime.detected",
            base=base,
            adx=round(adx, 1),
            ema_stack=ema_stack,
            gift_pct=gift_nifty_pct,
            news=news_sentiment,
            vix=india_vix,
        )
        return base

    @staticmethod
    def _apply_soft_overrides(
        base: Regime,
        gift_nifty_pct: float | None,
        news_sentiment: float | None,
    ) -> Regime:
        """
        Nudge a RANGING base regime using pre-market and news signals.

        GIFT Nifty thresholds (± 1.5%) are deliberately conservative —
        intraday noise can easily cause ±0.5% gaps that mean nothing.
        """
        GIFT_BULL_THRESHOLD  =  1.5   # % gap up  → lean TRENDING_UP
        GIFT_BEAR_THRESHOLD  = -1.5   # % gap down → lean TRENDING_DOWN
        NEWS_BULL_THRESHOLD  =  0.5   # sentiment score → lean TRENDING_UP
        NEWS_BEAR_THRESHOLD  = -0.5   # sentiment score → lean TRENDING_DOWN

        bullish_signals = 0
        bearish_signals = 0

        if gift_nifty_pct is not None:
            if gift_nifty_pct >= GIFT_BULL_THRESHOLD:
                bullish_signals += 1
            elif gift_nifty_pct <= GIFT_BEAR_THRESHOLD:
                bearish_signals += 1

        if news_sentiment is not None:
            if news_sentiment >= NEWS_BULL_THRESHOLD:
                bullish_signals += 1
            elif news_sentiment <= NEWS_BEAR_THRESHOLD:
                bearish_signals += 1

        # Require at least one signal to override; ties stay RANGING
        if bullish_signals > bearish_signals:
            return "TRENDING_UP"
        if bearish_signals > bullish_signals:
            return "TRENDING_DOWN"

        return base

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
        gift_nifty_pct: float | None = None,
        news_sentiment: float | None = None,
    ) -> Regime:
        """Detect regime, publish to Redis, and return the result."""
        regime = self.detect(df, india_vix, gift_nifty_pct, news_sentiment)

        # Build a lightweight detail dict for diagnostics
        detail: dict = {
            "india_vix":     india_vix,
            "gift_nifty_pct": gift_nifty_pct,
            "news_sentiment": news_sentiment,
        }
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
