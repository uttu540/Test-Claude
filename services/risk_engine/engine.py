"""
services/risk_engine/engine.py
───────────────────────────────
Pre-trade risk checks and position sizing.

Called before every order. All checks must pass before an order is placed.
Never throws — always returns a RiskDecision so the caller can log and act.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date

import structlog
from sqlalchemy import text

from config.settings import settings
from database.connection import get_db_session

# Per-symbol asyncio lock: prevents two concurrent coroutines from both
# passing the duplicate-position check for the same symbol when signals
# fire simultaneously (e.g. multiple ORB setups processed in the same batch).
_entry_locks: dict[str, asyncio.Lock] = {}

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
        explicit_stop: float | None = None,   # Override ATR-derived stop (e.g. ORB uses OR low)
    ) -> RiskDecision:
        """
        Full pre-trade evaluation. Returns approved=True with sizing if safe to trade.

        explicit_stop: when provided, used directly as stop_loss instead of entry ± 2×ATR.
        Target is then placed at entry + RR_RATIO × risk_per_share in the trade direction.
        atr must still be a real ATR value (used only for logging/analytics when explicit_stop set).

        Thread-safety: uses a per-symbol asyncio lock so two concurrent coroutines
        processing the same symbol simultaneously (e.g. ORB batch scan) cannot both
        pass the duplicate-position check before either trade is committed to DB.
        """
        # Acquire per-symbol lock — held until this evaluation completes.
        # Prevents duplicate entries when two coroutines race on the same symbol.
        if symbol not in _entry_locks:
            _entry_locks[symbol] = asyncio.Lock()
        async with _entry_locks[symbol]:
            return await self._evaluate_locked(symbol, direction, entry_price, atr, explicit_stop)

    async def _evaluate_locked(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        atr: float,
        explicit_stop: float | None = None,
    ) -> RiskDecision:
        """Inner evaluate — called only while holding the per-symbol lock."""
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

        # ── 3. Aggregate notional cap ─────────────────────────────────────────
        # Prevents total open position value from exceeding ~95% of capital.
        # Without this: 8 positions × 15% = 120% capital deployed on margin.
        open_notional = await self._get_open_notional()
        max_notional  = settings.total_capital * (settings.max_aggregate_notional_pct / 100)
        if open_notional >= max_notional:
            return RiskDecision(
                approved=False,
                reason=(
                    f"Aggregate open notional ₹{open_notional:,.0f} at/above cap "
                    f"₹{max_notional:,.0f} ({settings.max_aggregate_notional_pct:.0f}% of capital)"
                ),
            )

        # ── 3a. No duplicate position in symbol ───────────────────────────────
        if await self._has_open_position(symbol):
            return RiskDecision(
                approved=False,
                reason=f"Already have an open position in {symbol}",
            )

        # ── 3b. Max 1 trade per symbol per calendar day ──────────────────────
        if await self._has_traded_today(symbol):
            return RiskDecision(
                approved=False,
                reason=f"Already traded {symbol} today — one trade per symbol per day",
            )

        # ── 4. Calculate stop loss and target ────────────────────────────────
        # explicit_stop: caller (e.g. ORB) passes the real structure-based stop
        # directly so we don't have to reverse-engineer it from a fake ATR.
        if explicit_stop is not None:
            stop_loss = explicit_stop
            risk_per_share = abs(entry_price - stop_loss)
            if risk_per_share <= 0:
                return RiskDecision(approved=False, reason="explicit_stop equals entry price — zero risk_per_share")
            if direction == "BULLISH":
                target = entry_price + risk_per_share * self.RR_RATIO
            else:
                target = entry_price - risk_per_share * self.RR_RATIO
        else:
            if atr <= 0:
                return RiskDecision(approved=False, reason="ATR is zero — cannot size position")
            if direction == "BULLISH":
                stop_loss = entry_price - (atr * self.ATR_STOP_MULTIPLIER)
                target    = entry_price + (atr * self.ATR_STOP_MULTIPLIER * self.RR_RATIO)
            else:
                stop_loss = entry_price + (atr * self.ATR_STOP_MULTIPLIER)
                target    = entry_price - (atr * self.ATR_STOP_MULTIPLIER * self.RR_RATIO)

        risk_per_share = abs(entry_price - stop_loss)

        # ── 4b. Price-relative stop floor / cap (ATR path only) ──────────────
        # explicit_stop is structure-based (OR low, swing low) — caller owns it; skip.
        # ATR-derived stops:
        #   Floor: stop must risk ≥ 0.3% of price — prevents near-zero risk on flat ATR
        #          producing absurdly large qty that blows up notional.
        #   Cap:   stop can risk ≤ 5% of price — prevents ₹20k stop on a ₹400 stock,
        #          which would shrink qty to 1 and make brokerage eat the whole R.
        if explicit_stop is None:
            price_floor_pct = 0.003   # 0.3 %
            price_cap_pct   = 0.05    # 5.0 %
            min_risk = entry_price * price_floor_pct
            max_risk = entry_price * price_cap_pct
            if risk_per_share < min_risk:
                return RiskDecision(
                    approved=False,
                    reason=(
                        f"ATR stop too tight: risk/share ₹{risk_per_share:.2f} < floor "
                        f"₹{min_risk:.2f} (0.3% of ₹{entry_price:.0f}) — ATR unusually small"
                    ),
                )
            if risk_per_share > max_risk:
                return RiskDecision(
                    approved=False,
                    reason=(
                        f"ATR stop too wide: risk/share ₹{risk_per_share:.2f} > cap "
                        f"₹{max_risk:.2f} (5% of ₹{entry_price:.0f}) — stock too volatile"
                    ),
                )

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
        """
        Return today's total P&L including unrealized losses from open positions.

        Realized:   SUM(net_pnl) from CLOSED trades entered today.
        Unrealized: (ltp - entry_price) * qty for each OPEN trade from Redis tick.
                    No tick → treat as 0 (conservative for longs entering today).

        The original query only counted CLOSED trades — 5 open positions each
        down ₹3k showed ₹0, allowing more trades well past the real daily loss.
        """
        today = date.today()
        try:
            async with get_db_session() as session:
                closed_result = await session.execute(
                    text("SELECT COALESCE(SUM(net_pnl), 0) FROM trades WHERE DATE(entry_time AT TIME ZONE 'Asia/Kolkata') = :today AND status = 'CLOSED'"),
                    {"today": today},
                )
                realized = float(closed_result.scalar() or 0)

                open_result = await session.execute(
                    text("SELECT trading_symbol, entry_price, entry_quantity, direction FROM trades WHERE status = 'OPEN' AND DATE(entry_time AT TIME ZONE 'Asia/Kolkata') = :today"),
                    {"today": today},
                )
                open_rows = open_result.fetchall()

            if not open_rows:
                return realized

            redis = get_redis()
            unrealized = 0.0
            for row in open_rows:
                try:
                    import json as _json
                    raw = await redis.get(f"market:tick:{row.trading_symbol}")
                    if raw:
                        lp = float(_json.loads(raw).get("lp", 0) or 0)
                        if lp > 0:
                            sign = 1 if row.direction == "LONG" else -1
                            unrealized += sign * (lp - float(row.entry_price)) * int(row.entry_quantity)
                except Exception:
                    pass

            total = realized + unrealized
            if unrealized != 0:
                log.debug("risk.pnl_with_unrealized",
                          realized=round(realized, 2),
                          unrealized=round(unrealized, 2),
                          total=round(total, 2))
            return total

        except Exception as e:
            # Fail-open: return 0 so a transient DB/Redis blip doesn't permanently
            # block all trading for the rest of the day. Per-trade risk limits still
            # apply. The old -inf failsafe caused all same-day signals to be rejected
            # after any momentary exception (e.g. DB contention during ORB batch scan).
            log.warning("risk.db_error.daily_pnl", error=str(e),
                        msg="Could not read daily PnL — assuming 0, per-trade limits still enforced")
            return 0.0

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

    async def _get_open_notional(self) -> float:
        """
        Return sum of (entry_price × entry_quantity) for all OPEN trades.
        Used to enforce the aggregate notional cap — prevents deploying >95% of capital.
        Fails open (returns max float) so we never exceed the cap on DB error.
        """
        try:
            async with get_db_session() as session:
                result = await session.execute(
                    text("SELECT COALESCE(SUM(entry_price * entry_quantity), 0) FROM trades WHERE status = 'OPEN'")
                )
                return float(result.scalar() or 0)
        except Exception as e:
            log.error("risk.db_error.open_notional", error=str(e))
            return float("inf")  # Fail-safe: block new trades if we can't read notional

    async def _has_traded_today(self, symbol: str) -> bool:
        today = date.today()
        try:
            async with get_db_session() as session:
                result = await session.execute(
                    text(
                        "SELECT COUNT(*) FROM trades "
                        "WHERE trading_symbol = :sym "
                        "  AND status IN ('OPEN', 'CLOSED') "
                        "  AND DATE(entry_time AT TIME ZONE 'Asia/Kolkata') = :today"
                    ),
                    {"sym": symbol, "today": today},
                )
                return int(result.scalar() or 0) > 0
        except Exception as e:
            # Fail-safe: assume traded today to prevent duplicate entries.
            log.error("risk.db_error.traded_today", symbol=symbol, error=str(e))
            return True
