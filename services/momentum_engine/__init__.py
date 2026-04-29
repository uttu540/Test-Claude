# services/momentum_engine — momentum/trend-following long strategy
# Separate from reversal-based signal_generator. Fires only in TRENDING_UP markets.
#
# Public API:
#   MomentumDetector      — detects Darvas, 52wk, volume thrust, EMA ribbon, bull momentum
#   MomentumBacktestEngine — runs the full backtest
#   score_momentum_confluence — confluence scorer calibrated for momentum

from services.momentum_engine.signals import (
    MomentumDetector,
    MomentumSignal,
    MomentumSignalType,
    MomentumConfluence,
    score_momentum_confluence,
    MIN_MOMENTUM_SCORE,
)
from services.momentum_engine.backtest import (
    MomentumBacktestEngine,
    MomentumBacktestResult,
    MomentumTrade,
)

__all__ = [
    "MomentumDetector",
    "MomentumSignal",
    "MomentumSignalType",
    "MomentumConfluence",
    "score_momentum_confluence",
    "MIN_MOMENTUM_SCORE",
    "MomentumBacktestEngine",
    "MomentumBacktestResult",
    "MomentumTrade",
]
