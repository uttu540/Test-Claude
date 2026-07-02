"""
services/momentum_engine_v2/live.py
─────────────────────────────────────
Live adapter for the V2 momentum engine (relaxed-gate swing variant).

Differences vs V1 live (services/momentum_engine/live.py):

  1. V2 detector — 200 EMA gate relaxed to "within 8% below" (near_200) and
     ADX lookforward (ADX ≥ 20 any time in last 10 bars). Both validated:
     V2 beats V1 on trades/PnL/WR/Sharpe on N500 and full-NSE 3yr backtests.
     Purpose: enter during basing/early-recovery, the phase V1's hard gates
     miss when the market turns up.

  2. TRENDING_DOWN → hard block. V1 live had an RS ≥ 8 bypass that was never
     backtest-validated and passed 0.05% of stocks (p99 of live RS dist = 7.7).
     Backtest evidence for forcing long swing entries in a down market:
     2026 YTD runs: 0 trades (baseline), 7 trades 0% WR -₹12.5k (relaxed),
     4 trades 25% WR -₹2.5k (V2 gates). Conclusion: long Darvas breakouts in
     TRENDING_DOWN lose money — stay flat, let intraday V2 shorts work instead.
     This also kills ~7,500 rs_skip INFO log lines per day.

  TRENDING_UP and RANGING behaviour inherited unchanged from V1 (V7 validated).
"""
from __future__ import annotations

import structlog

from config.settings import settings
from services.momentum_engine.live import MomentumLiveEngine
from services.momentum_engine_v2.signals import MomentumDetector as V2MomentumDetector
from services.technical_engine.signal_generator import Signal

log = structlog.get_logger(__name__)


class MomentumV2LiveEngine(MomentumLiveEngine):
    """V1 live pipeline with the V2 detector and backtest-aligned regime gates."""

    def __init__(self) -> None:
        super().__init__()
        self._detector = V2MomentumDetector()

    async def detect(self, symbol, daily_df, regime, redis) -> list[Signal]:
        if regime == "TRENDING_DOWN":
            # Aligned with backtest: long swing entries in a downtrend are
            # -EV (see module docstring). debug level — fires for every symbol.
            log.debug("momentum_v2_live.trending_down_block", symbol=symbol)
            return []
        signals = await super().detect(symbol, daily_df, regime, redis)

        # ── ADX(14) entry floor ───────────────────────────────────────────────
        # Out-of-sample validated (2020-2025): ADX≥30 lifted WR ~8pts, avg R
        # +63%, and cut max drawdown ~40%. Strong-trend entries only.
        min_adx = settings.momentum_min_adx
        if min_adx > 0 and signals:
            kept = [s for s in signals if (getattr(s, "adx", 0) or 0) >= min_adx]
            if len(kept) != len(signals):
                log.info("momentum_v2_live.adx_filter", symbol=symbol,
                         min_adx=min_adx, before=len(signals), after=len(kept))
            signals = kept
        return signals
