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

    ATR_STOP_MULTIPLIER = 2.0   # Stop loss placed at 2.0x ATR from entry
    # 1.5x was too tight — normal intraday noise on NSE spans 1-2 ATR,
    # causing legitimate positions to be stopped out before the move.
    RR_RATIO = 2.0              # Target at 2:1 risk-reward (4x ATR from entry)

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

        # ── 3b. Max 1 trade per symbol per calendar day ───────────────────────
        if await self._has_traded_today(symbol):
            return RiskDecision(
                approved=False,
                reason=f"Already traded {symbol} today — one trade per symbol per day",
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
            if qty <= 0:
                return RiskDecision(approved=False, reason="Position capped to 0 shares — price too high for max_position_size_inr")

        # Reject if position value too small — not worth brokerage cost
        MIN_POSITION_VALUE = 5_000
        if qty * entry_price < MIN_POSITION_VALUE:
            return RiskDecision(approved=False, reason=f"Position value ₹{qty*entry_price:.0f} below minimum ₹{MIN_POSITION_VALUE}")

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
        try:
            async with get_db_session() as session:
                result = await session.execute(
                    text("SELECT COALESCE(SUM(net_pnl), 0) FROM trades WHERE DATE(entry_time) = :today AND status = 'CLOSED'"),
                    {"today": today},
                )
                return float(result.scalar() or 0)
        except Exception as e:
            # Fail-safe: treat DB error as if daily limit is hit — never allow trades when
            # we can't verify the loss limit. Returning 0 would silently bypass the check.
            log.error("risk.db_error.daily_pnl", error=str(e))
            return -float("inf")

    async def _get_open_count(self) -> int:
        try:
            async with get_db_session() as session:
                result = await session.execute(
                    text("SELECT COUNT(*) FROM trades WHERE status = 'OPEN'")
                )
                return int(result.scalar() or 0)
        except Exception as e:
            # Fail-safe: report max positions so the check blocks trading.
            log.error("risk.db_error.open_count", error=str(e))
            return settings.max_open_positions

    async def _has_open_position(self, symbol: str) -> bool:
        try:
            async with get_db_session() as session:
                result = await session.execute(
                    text("SELECT COUNT(*) FROM trades WHERE trading_symbol = :sym AND status = 'OPEN'"),
                    {"sym": symbol},
                )
                return int(result.scalar() or 0) > 0
        except Exception as e:
            # Fail-safe: assume open position exists so we don't enter a duplicate.
            log.error("risk.db_error.open_position", symbol=symbol, error=str(e))
            return True

    async def _has_traded_today(self, symbol: str) -> bool:
        today = date.today()
        try:
            async with get_db_session() as session:
                result = await session.execute(
                    text(
                        "SELECT COUNT(*) FROM trades "
                        "WHERE trading_symbol = :sym "
                        "  AND status IN ('OPEN', 'CLOSED') "
                        "  AND DATE(entry_time) = :today"
                    ),
                    {"sym": symbol, "today": today},
                )
                return int(result.scalar() or 0) > 0
        except Exception as e:
            # Fail-safe: assume traded today to prevent duplicate entries.
            log.error("risk.db_error.traded_today", symbol=symbol, error=str(e))
            return True
