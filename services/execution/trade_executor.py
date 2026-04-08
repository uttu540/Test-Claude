"""
services/execution/trade_executor.py
──────────────────────────────────────
Converts a Signal into a live/paper/semi-auto trade:
  1. Risk Engine pre-trade checks + position sizing
  2. Claude AI strategy evaluation
  3. [semi-auto only] Human approval gate (Telegram ✅/❌)
  4. Entry order (MARKET)
  5. Trade record persisted to DB
  6. Stop-loss order (SL-M)
  7. Target order (LIMIT)
  8. Telegram notification

This is the only place that initiates new positions.
Broker is selected by get_broker() — never hardcoded here.
"""
from __future__ import annotations

import uuid
from datetime import datetime

import structlog

from config.settings import settings
from database.connection import get_db_session
from database.models import Trade
from services.ai_strategy.claude_client import get_claude_client
from services.ai_strategy.schemas import AIDecision
from services.execution.broker_router import get_broker
from services.notifications.telegram_bot import get_notifier
from services.risk_engine.engine import RiskDecision, RiskEngine
from services.technical_engine.signal_generator import Direction, Signal

log = structlog.get_logger(__name__)

EXCHANGE = "NSE"
PRODUCT  = "MIS"   # Intraday — auto square-off at 3:12 PM


class TradeExecutor:
    """
    Orchestrates the full trade lifecycle from signal to open position.
    Stateless — safe to instantiate per signal.
    """

    def __init__(self) -> None:
        self._risk = RiskEngine()

    async def execute(self, signal: Signal) -> Trade | None:
        """
        Attempt to open a position based on the signal.
        Returns the Trade record on success, None if rejected or failed.
        """
        atr = signal.indicators.get("atr_14", 0)
        if not atr:
            log.warning("executor.no_atr", symbol=signal.trading_symbol, signal=signal.signal_type.value)
            return None

        # ── 1. Risk evaluation ────────────────────────────────────────────────
        decision = await self._risk.evaluate(
            symbol      = signal.trading_symbol,
            direction   = signal.direction.value,
            entry_price = signal.price_at_signal,
            atr         = atr,
        )

        if not decision.approved:
            log.info("executor.risk_blocked", symbol=signal.trading_symbol, reason=decision.reason)
            return None

        # ── 2. Claude AI evaluation ───────────────────────────────────────────
        ai_decision: AIDecision = await get_claude_client().analyse(signal)

        if not ai_decision.is_actionable:
            log.info(
                "executor.ai_blocked",
                symbol     = signal.trading_symbol,
                action     = ai_decision.action.value,
                confidence = ai_decision.confidence,
                reasoning  = ai_decision.reasoning[:120],
            )
            return None

        trade_id  = uuid.uuid4()
        direction = "LONG" if signal.direction == Direction.BULLISH else "SHORT"
        side      = "BUY"  if signal.direction == Direction.BULLISH else "SELL"

        # ── 3. Human approval gate (semi-auto only) ───────────────────────────
        if settings.is_semi_auto:
            from services.execution.approval_gate import build_approval_request, request_approval

            req = build_approval_request(
                trade_id     = str(trade_id),
                symbol       = signal.trading_symbol,
                direction    = direction,
                entry_price  = signal.price_at_signal,
                stop_loss    = decision.stop_loss,
                target       = decision.target,
                quantity     = decision.position_size,
                risk_inr     = decision.risk_amount,
                strategy     = signal.signal_type.value,
                signal_conf  = signal.confidence,
                ai_conf      = ai_decision.confidence,
                ai_reasoning = ai_decision.reasoning or "",
            )
            approved = await request_approval(req)
            if not approved:
                log.info("executor.approval_rejected", symbol=signal.trading_symbol, trade_id=str(trade_id))
                return None

        # ── 4. Entry order ────────────────────────────────────────────────────
        broker    = get_broker()
        broker_id = await broker.place_order(
            symbol           = signal.trading_symbol,
            exchange         = EXCHANGE,
            transaction_type = side,
            quantity         = decision.position_size,
            order_type       = "MARKET",
            product          = PRODUCT,
            tag              = f"BOT_{signal.signal_type.value[:8]}",
            trade_id         = str(trade_id),
        )

        if not broker_id:
            log.error("executor.entry_failed", symbol=signal.trading_symbol)
            return None

        # ── 5. Record trade in DB ─────────────────────────────────────────────
        trade = await self._record_trade(trade_id, signal, direction, decision, ai_decision)

        # ── 6. Stop-loss order ────────────────────────────────────────────────
        await broker.place_stop_loss(
            symbol        = signal.trading_symbol,
            exchange      = EXCHANGE,
            quantity      = decision.position_size,
            trigger_price = decision.stop_loss,
            product       = PRODUCT,
            direction     = direction,
            tag           = "BOT_SL",
            trade_id      = str(trade_id),
        )

        # ── 7. Target order ───────────────────────────────────────────────────
        await broker.place_target(
            symbol      = signal.trading_symbol,
            exchange    = EXCHANGE,
            quantity    = decision.position_size,
            limit_price = decision.target,
            product     = PRODUCT,
            direction   = direction,
            tag         = "BOT_TGT",
            trade_id    = str(trade_id),
        )

        # ── 8. Telegram notification ──────────────────────────────────────────
        rr      = abs(decision.target - signal.price_at_signal) / abs(signal.price_at_signal - decision.stop_loss)
        ai_note = f"\n🤖 AI: {ai_decision.confidence:.0%} | {ai_decision.reasoning[:80]}" if ai_decision.reasoning else ""
        await get_notifier().signal_alert(
            symbol     = signal.trading_symbol,
            signal     = signal.signal_type.value,
            direction  = signal.direction.value,
            confidence = signal.confidence,
            timeframe  = signal.timeframe,
            price      = signal.price_at_signal,
            notes=(
                f"📥 Entry: ₹{signal.price_at_signal:.2f}\n"
                f"🛡 Stop: ₹{decision.stop_loss:.2f}\n"
                f"🎯 Target: ₹{decision.target:.2f}\n"
                f"📦 Qty: {decision.position_size} | Risk: ₹{decision.risk_amount:.0f} | RR: {rr:.1f}x"
                f"{ai_note}"
            ),
        )

        log.info(
            "executor.trade_opened",
            symbol    = signal.trading_symbol,
            direction = direction,
            qty       = decision.position_size,
            entry     = signal.price_at_signal,
            sl        = decision.stop_loss,
            target    = decision.target,
            risk_inr  = decision.risk_amount,
            trade_id  = str(trade_id),
            broker    = broker.BROKER,
        )

        return trade

    # ── DB ────────────────────────────────────────────────────────────────────

    async def _record_trade(
        self,
        trade_id:    uuid.UUID,
        signal:      Signal,
        direction:   str,
        decision:    RiskDecision,
        ai_decision: AIDecision,
    ) -> Trade:
        rr = (
            abs(decision.target - signal.price_at_signal)
            / abs(signal.price_at_signal - decision.stop_loss)
            if abs(signal.price_at_signal - decision.stop_loss) > 0
            else 0
        )

        trade = Trade(
            id                  = trade_id,
            trading_symbol      = signal.trading_symbol,
            exchange            = EXCHANGE,
            instrument_type     = "EQ",
            direction           = direction,
            strategy_name       = signal.signal_type.value,
            strategy_mode       = "INTRADAY",
            broker              = get_broker().BROKER,
            mode                = settings.app_env.value,
            entry_price         = signal.price_at_signal,
            entry_quantity      = decision.position_size,
            entry_time          = datetime.now(),
            planned_stop_loss   = decision.stop_loss,
            planned_target_1    = decision.target,
            initial_risk_amount = decision.risk_amount,
            risk_reward_planned = round(rr, 2),
            signals_at_entry    = signal.to_dict(),
            ai_confidence       = ai_decision.confidence,
            ai_reasoning        = ai_decision.reasoning,
            status              = "OPEN",
        )

        try:
            async for session in get_db_session():
                session.add(trade)
                await session.commit()
                await session.refresh(trade)
                return trade
        except Exception as e:
            log.error("executor.db_record_failed", error=str(e), trade_id=str(trade_id))

        return trade
