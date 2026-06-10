"""
services/momentum_engine/live.py
──────────────────────────────────
Live adapter for the momentum engine.

Converts MomentumSignal → Signal so TradeExecutor can handle it
without any changes to detection logic, scoring, or R:R.

Key design:
  - Daily TF only — reads "1day" candle buffer
  - One signal per symbol per calendar day (Redis cooldown key prevents
    the same Darvas setup firing on every 15min trigger throughout the day)
  - compute_all() called here, same as backtest engine does before detect()

Regime logic (RS-based):
  - TRENDING_UP   → fire freely
  - RANGING       → require stock RS > Nifty by ≥3% (20d ROC) — sector rotation
  - TRENDING_DOWN → require stock RS > Nifty by ≥8% (20d ROC) — strong sector leader

Nothing in MomentumDetector is changed. This is a pure translation layer.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import structlog

from services.momentum_engine.signals import MomentumDetector, MomentumSignal
from services.momentum_engine.watchlist import is_in_watchlist
from services.technical_engine.indicators import compute_all
from services.technical_engine.signal_generator import Direction, Signal, SignalType

log = structlog.get_logger(__name__)

# Minimum daily candles needed for reliable indicator warm-up
MIN_DAILY_BARS = 60

# Regime behaviour (V7 — validated 3yr backtest):
#   TRENDING_UP   → fire freely
#   RANGING       → require sector ROC-20 ≥ 5% AND stock RS ≥ 3% vs Nifty
#                   (sector rotation play: Nifty flat but sector trending)
#   TRENDING_DOWN → require stock RS ≥ 8% vs Nifty 20d ROC (genuine sector leaders only)
_RS_THRESHOLD = {
    "TRENDING_DOWN": 8.0,
    # RANGING: handled dynamically via sector ROC check below (not a simple RS threshold)
}
_REGIME_BLOCKED: set[str] = set()   # no hard block — RANGING handled via sector gate

# V7 RANGING thresholds (validated: 31.2% WR, +₹12,981 over 3yr)
_RANGING_SECTOR_ROC_MIN = 5.0   # sector index ROC-20 must be ≥ 5%
_RANGING_RS_MIN         = 3.0   # stock ROC-20 must beat Nifty by ≥ 3%


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
          - not enough daily history
          - already fired for this symbol today (cooldown)
          - regime is RANGING/TRENDING_DOWN and stock doesn't have sufficient
            relative strength vs Nifty (sector rotation filter)
        """
        if len(daily_df) < MIN_DAILY_BARS:
            log.info("momentum_live.insufficient_bars", symbol=symbol, bars=len(daily_df), required=MIN_DAILY_BARS)
            return []

        # ── Gate 1: Regime filter ─────────────────────────────────────────────
        # TRENDING_UP:   fire freely.
        # RANGING:       sector ROC-20 ≥ 5% AND stock RS ≥ 3% vs Nifty.
        #                Sector rotation play — Nifty flat, sector trending.
        # TRENDING_DOWN: stock RS ≥ 8% vs Nifty (genuine sector leaders only).
        if regime in _REGIME_BLOCKED:
            log.info("momentum_live.regime_blocked", symbol=symbol, regime=regime)
            return []

        # Compute stock ROC-20 and Nifty ROC-20 (needed for RANGING + TRENDING_DOWN)
        nifty_roc20 = 0.0
        stock_roc20 = 0.0
        if regime in ("RANGING", "TRENDING_DOWN"):
            nifty_roc20_raw = await redis.get("momentum:nifty_roc20")
            if nifty_roc20_raw is not None:
                try:
                    nifty_roc20 = float(nifty_roc20_raw)
                except ValueError:
                    pass

            if len(daily_df) >= 21:
                closes = daily_df["close"] if "close" in daily_df.columns else daily_df["Close"]
                base_close = float(closes.iloc[-21])
                stock_roc20 = float((closes.iloc[-1] / base_close - 1) * 100) if base_close > 0 else 0.0

        if regime == "RANGING":
            # Gate A: sector must be trending (ROC-20 ≥ 5%)
            # Only applies when the symbol has a known sector (Nifty500 coverage).
            # Non-N500 symbols (the other ~1200 in the full NSE universe) skip the
            # sector gate and fall through to Gate B (RS vs Nifty) only —
            # they don't get silently blocked just for being outside the N500 map.
            from services.data_ingestion.nifty500_instruments import get_symbol_sector_map
            _sector_map  = get_symbol_sector_map()
            _sector      = _sector_map.get(symbol)
            if _sector:
                _sector_roc_raw = await redis.get(f"momentum:sector_roc20:{_sector}")
                if _sector_roc_raw is None:
                    log.debug("momentum_live.ranging_sector_missing", symbol=symbol, sector=_sector)
                    return []   # sector ROC not yet computed — skip conservatively

                try:
                    _sector_roc = float(_sector_roc_raw)
                except ValueError:
                    return []

                if _sector_roc < _RANGING_SECTOR_ROC_MIN:
                    log.info("momentum_live.ranging_sector_skip",
                             symbol=symbol, sector=_sector,
                             sector_roc=round(_sector_roc, 1),
                             threshold=_RANGING_SECTOR_ROC_MIN)
                    return []
            else:
                log.debug("momentum_live.ranging_sector_unmapped",
                          symbol=symbol, note="non-N500 symbol — skipping sector gate, RS gate still applies")

            # Gate B: stock must outperform Nifty by ≥ 3%
            relative_strength = stock_roc20 - nifty_roc20
            if relative_strength < _RANGING_RS_MIN:
                log.info("momentum_live.ranging_rs_skip",
                         symbol=symbol, rs=round(relative_strength, 1),
                         threshold=_RANGING_RS_MIN)
                return []

        elif regime == "TRENDING_DOWN":
            rs_threshold      = _RS_THRESHOLD["TRENDING_DOWN"]
            relative_strength = stock_roc20 - nifty_roc20
            if relative_strength < rs_threshold:
                log.info(
                    "momentum_live.rs_skip",
                    symbol=symbol, regime=regime,
                    stock_roc20=round(stock_roc20, 1),
                    nifty_roc20=round(nifty_roc20, 1),
                    rs=round(relative_strength, 1),
                    threshold=rs_threshold,
                )
                return []

        # ── Gate 2: RS Watchlist filter (feature flag) ────────────────────────
        # When enabled, only run Darvas detection on top-50 RS leaders.
        # Built daily at 4 PM by job_watchlist_update. Flip flag off = instant revert.
        from config.bot_config import get_bot_config
        cfg = await get_bot_config()
        if cfg.get("momentum_watchlist_enabled", False):
            if not await is_in_watchlist(symbol):
                log.debug("momentum_live.watchlist_skip", symbol=symbol)
                return []

        # Per-symbol, per-day cooldown — prevents same setup re-firing every 15min
        cooldown_key = f"momentum_live:fired:{symbol}:{date.today().isoformat()}"
        already_fired = await redis.get(cooldown_key)
        if already_fired:
            log.info("momentum_live.cooldown_skip", symbol=symbol)
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

        # Convert MomentumSignal → Signal
        # Cooldown is set in main.py AFTER TradeExecutor succeeds — not here.
        # Setting it here would lock out a symbol even if risk/AI blocked the trade.
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
                regime     = regime,
                signal     = signal_type.value,
                confidence = ms.confidence,
                price      = ms.price,
                rvol       = ms.rvol,
                rsi        = ms.rsi,
            )

        return live_signals
