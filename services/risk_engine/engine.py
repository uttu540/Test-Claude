"""
services/risk_engine/engine.py
───────────────────────────────
Pre-trade risk checks and position sizing.

Called before every order. All checks must pass before an order is placed.
Never throws — always returns a RiskDecision so the caller can log and act.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import structlog
from sqlalchemy import text

from config.settings import settings
from database.connection import get_db_session

log = structlog.get_logger(__name__)


@dataclass
class RiskDecision:
    approved: bool
    reason: str
    position_size: int = 0
    risk_amount: float = 0.0
    stop_loss: float = 0.0
    target: float = 0.0


class RiskEngine:
    """
    Enforces all pre-trade rules:
      1. Daily loss limit not breached
      2. Max open positions not exceeded
      3. No duplicate position in same symbol
      4. Position sized to risk exactly max_risk_per_trade_inr
      5. Position value doesn't exceed max_position_size_inr
    """

    ATR_STOP_MULTIPLIER = 1.5   # Stop loss placed at 1.5x ATR from entry
    RR_RATIO = 2.0              # Target at 2:1 risk-reward

    async def evaluate(
        self,
        symbol: str,
        direction: str,        # "BULLISH" or "BEARISH"
        entry_price: float,
        atr: float,
    ) -> RiskDecision:
        """
        Full pre-trade evaluation. Returns approved=True with sizing if safe to trade.
        """
        # ── 1. Daily loss limit ───────────────────────────────────────────────
        daily_pnl = await self._get_todays_pnl()
        if daily_pnl <= -settings.daily_loss_limit_inr:
            return RiskDecision(
                approved=False,
                reason=f"Daily loss limit hit ₹{abs(daily_pnl):.0f} / ₹{settings.daily_loss_limit_inr:.0f} — trading halted",
            )

        # ── 2. Max open positions ─────────────────────────────────────────────
        open_count = await self._get_open_count()
        if open_count >= settings.max_open_positions:
            return RiskDecision(
                approved=False,
                reason=f"Max open positions reached ({open_count}/{settings.max_open_positions})",
            )

        # ── 3. No duplicate position in symbol ────────────────────────────────
        if await self._has_open_position(symbol):
            return RiskDecision(
                approved=False,
                reason=f"Already have an open position in {symbol}",
            )

        # ── 4. Calculate stop loss and target from ATR ────────────────────────
        if atr <= 0:
            return RiskDecision(approved=False, reason="ATR is zero — cannot size position")

        if direction == "BULLISH":
            stop_loss = entry_price - (atr * self.ATR_STOP_MULTIPLIER)
            target    = entry_price + (atr * self.ATR_STOP_MULTIPLIER * self.RR_RATIO)
        else:
            stop_loss = entry_price + (atr * self.ATR_STOP_MULTIPLIER)
            target    = entry_price - (atr * self.ATR_STOP_MULTIPLIER * self.RR_RATIO)

        risk_per_share = abs(entry_price - stop_loss)

        # ── 5. Position sizing ────────────────────────────────────────────────
        qty = int(settings.max_risk_per_trade_inr / risk_per_share)
        if qty <= 0:
            return RiskDecision(approved=False, reason="Computed quantity is 0 — ATR too large relative to risk budget")

        # Cap at max position size
        position_value = qty * entry_price
        if position_value > settings.max_position_size_inr:
            qty = int(settings.max_position_size_inr / entry_price)

        risk_amount = qty * risk_per_share

        log.info(
            "risk.approved",
            symbol=symbol,
            qty=qty,
            entry=round(entry_price, 2),
            stop=round(stop_loss, 2),
            target=round(target, 2),
            risk_inr=round(risk_amount, 2),
            daily_pnl=round(daily_pnl, 2),
            open_positions=open_count,
        )

        return RiskDecision(
            approved=True,
            reason="All checks passed",
            position_size=qty,
            risk_amount=round(risk_amount, 2),
            stop_loss=round(stop_loss, 2),
            target=round(target, 2),
        )

    # ── DB helpers ────────────────────────────────────────────────────────────

    async def _get_todays_pnl(self) -> float:
        today = date.today()
        async for session in get_db_session():
            result = await session.execute(
                text("SELECT COALESCE(SUM(net_pnl), 0) FROM trades WHERE DATE(entry_time) = :today AND status = 'CLOSED'"),
                {"today": today},
            )
            return float(result.scalar() or 0)
        return 0.0

    async def _get_open_count(self) -> int:
        async for session in get_db_session():
            result = await session.execute(
                text("SELECT COUNT(*) FROM trades WHERE status = 'OPEN'")
            )
            return int(result.scalar() or 0)
        return 0

    async def _has_open_position(self, symbol: str) -> bool:
        async for session in get_db_session():
            result = await session.execute(
                text("SELECT COUNT(*) FROM trades WHERE trading_symbol = :sym AND status = 'OPEN'"),
                {"sym": symbol},
            )
            return int(result.scalar() or 0) > 0
        return False
