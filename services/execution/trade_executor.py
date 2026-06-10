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
from database.connection import get_db_session, get_redis
from database.models import SignalRejectionLog, Trade
from services.ai_strategy.claude_client import get_claude_client
from services.ai_strategy.schemas import AIDecision
from services.execution.broker_router import get_broker
from services.notifications.telegram_bot import get_notifier
from services.risk_engine.engine import RiskDecision, RiskEngine
from services.technical_engine.signal_generator import Direction, Signal

log = structlog.get_logger(__name__)

EXCHANGE = "NSE"
# Product is determined per-signal at execution time (see _get_product).
# MIS = intraday (auto square-off 3:20 PM); CNC = delivery (holds overnight).
_SWING_TIMEFRAMES = {"1day", "1week"}


class TradeExecutor:
    """
    Orchestrates the full trade lifecycle from signal to open position.
    Stateless — safe to instantiate per signal.
    """

    def __init__(self) -> None:
        self._risk = RiskEngine()

    @staticmethod
    def _get_product(signal: Signal) -> str:
        """
        MIS (intraday) for short-timeframe signals — broker auto-squares off at 3:20 PM.
        CNC (delivery) for daily/weekly signals — position holds overnight as a swing trade.
        Paper/dev broker ignores this field, but it's used in order records and charge calc.
        """
        return "CNC" if signal.timeframe in _SWING_TIMEFRAMES else "MIS"

    async def execute(self, signal: Signal) -> Trade | None:
        """
        Attempt to open a position based on the signal.
        Returns the Trade record on success, None if rejected or failed.
        """
        atr = signal.indicators.get("atr_14", 0)

        # explicit_stop: ORB, IDARVAS, Catalyst and any caller that has a
        # structure-based stop can set this to bypass the ATR-derived stop.
        # When explicit_stop is provided, ATR is only used for logging — the
        # risk engine computes position size from (entry - explicit_stop).
        explicit_stop = signal.indicators.get("explicit_stop")

        if not atr and explicit_stop is None:
            log.warning("executor.no_atr", symbol=signal.trading_symbol, signal=signal.signal_type.value)
            return None

        # ── 1. Risk evaluation ────────────────────────────────────────────────
        decision = await self._risk.evaluate(
            symbol         = signal.trading_symbol,
            direction      = signal.direction.value,
            entry_price    = signal.price_at_signal,
            atr            = atr,
            explicit_stop  = explicit_stop,
        )

        if not decision.approved:
            log.info("executor.risk_blocked", symbol=signal.trading_symbol, reason=decision.reason)
            await self._record_rejection(signal, stage="RISK", reason=decision.reason)
            return None

        # ── 1a. Structural R:R gate ───────────────────────────────────────────
        # The risk engine computes a mathematical 2:1 target using ATR or explicit_stop.
        # But if that target lands inside a known resistance zone (prior swing high,
        # pivot R1/R2), the *achievable* R:R is much lower — price will stall there.
        # Reject if the effective (structure-adjusted) R:R falls below 1.5.
        rr_ok, rr_reason, adjusted_target = self._check_structural_rr(
            signal   = signal,
            decision = decision,
        )
        if not rr_ok:
            log.info("executor.structural_rr_blocked",
                     symbol=signal.trading_symbol, reason=rr_reason)
            await self._record_rejection(signal, stage="STRUCTURAL_RR", reason=rr_reason)
            return None
        if adjusted_target and adjusted_target != decision.target:
            log.info("executor.target_adjusted_for_resistance",
                     symbol=signal.trading_symbol,
                     original=decision.target, adjusted=adjusted_target)
            decision = type(decision)(
                approved       = decision.approved,
                reason         = decision.reason,
                position_size  = decision.position_size,
                risk_amount    = decision.risk_amount,
                stop_loss      = decision.stop_loss,
                target         = adjusted_target,
            )

        # ── 1b. Liquidity / circuit-limit check ──────────────────────────────
        liq_ok, liq_reason = await self._check_liquidity(
            symbol    = signal.trading_symbol,
            direction = signal.direction.value,
        )
        if not liq_ok:
            log.info("executor.liquidity_blocked", symbol=signal.trading_symbol, reason=liq_reason)
            await self._record_rejection(signal, stage="LIQUIDITY", reason=liq_reason)
            return None

        # ── 1c. Sector cap ────────────────────────────────────────────────────
        sector_ok, sector_reason = await self._check_sector_cap(signal.trading_symbol)
        if not sector_ok:
            log.info("executor.sector_blocked", symbol=signal.trading_symbol, reason=sector_reason)
            await self._record_rejection(signal, stage="SECTOR_CAP", reason=sector_reason)
            return None

        # ── 2. Claude AI evaluation ───────────────────────────────────────────
        # Strategies with their own multi-layer quality gates (OR range/volume/Nifty,
        # Darvas box/RVOL/gap, day1/day2 PEAD conditions) are exempt from AI eval —
        # they lack generic indicators (RSI/MACD/ATR%) which causes false "data
        # anomaly" SKIP decisions. The strategy gate IS the quality filter.
        from services.technical_engine.signal_generator import SignalType
        _AI_SKIP_SIGNALS = {
            SignalType.ORB_BREAKOUT,       # OR range + volume + Nifty gate
            SignalType.INTRADAY_IDARVAS,   # gap% + RVOL + Darvas box + compactness + VWAP
            SignalType.CATALYST_GAP_PEAD,  # day1 gap/RVOL/close-high + day2 open hold
        }
        if signal.signal_type not in _AI_SKIP_SIGNALS:
            ai_decision: AIDecision = await get_claude_client().analyse(signal)

            if not ai_decision.is_actionable:
                log.info(
                    "executor.ai_blocked",
                    symbol     = signal.trading_symbol,
                    action     = ai_decision.action.value,
                    confidence = ai_decision.confidence,
                    reasoning  = ai_decision.reasoning[:120],
                )
                await self._record_rejection(
                    signal,
                    stage  = "AI",
                    reason = f"{ai_decision.action.value}: {ai_decision.reasoning[:200]}",
                )
                return None
        else:
            from services.ai_strategy.schemas import TradeAction
            ai_decision = AIDecision(
                action     = TradeAction.BUY,
                confidence = signal.confidence / 100.0,
                reasoning  = f"{signal.signal_type.value}: AI evaluation skipped — strategy uses own quality gates",
            )

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
                await self._record_rejection(signal, stage="APPROVAL", reason="rejected_or_timed_out")
                return None

        # ── 4. Record trade in DB first ───────────────────────────────────────
        # Must happen BEFORE place_order so the FK constraint on orders.parent_trade_id
        # is satisfied when _record_order runs inside place_order / place_stop_loss.
        broker  = get_broker()
        product = self._get_product(signal)   # MIS for intraday, CNC for swing
        trade   = await self._record_trade(trade_id, signal, direction, decision, ai_decision, broker.BROKER)
        if trade is None:
            # DB write failed — abort; no order should be placed without a trade record
            log.error("executor.aborted_no_db_record", symbol=signal.trading_symbol)
            return None

        log.info("executor.product_selected", symbol=signal.trading_symbol,
                 timeframe=signal.timeframe, product=product)

        # ── 5. Entry order ────────────────────────────────────────────────────
        broker_id = await broker.place_order(
            symbol           = signal.trading_symbol,
            exchange         = EXCHANGE,
            transaction_type = side,
            quantity         = decision.position_size,
            order_type       = "MARKET",
            product          = product,
            tag              = f"BOT_{signal.signal_type.value[:8]}",
            trade_id         = str(trade_id),
        )

        if not broker_id:
            log.error("executor.entry_failed", symbol=signal.trading_symbol)
            # Mark orphan trade CANCELLED so lifecycle does not try to manage it
            await self._cancel_trade_record(trade_id)
            return None

        # Back-fill entry_order_id so reports can JOIN trades → orders
        await self._update_entry_order_id(trade_id, side)

        # ── 6. Stop-loss order ────────────────────────────────────────────────
        sl_id = await broker.place_stop_loss(
            symbol        = signal.trading_symbol,
            exchange      = EXCHANGE,
            quantity      = decision.position_size,
            trigger_price = decision.stop_loss,
            product       = product,
            direction     = direction,
            tag           = "BOT_SL",
            trade_id      = str(trade_id),
        )
        if not sl_id:
            log.error("executor.sl_failed", symbol=signal.trading_symbol,
                      note="position is naked — squaring off immediately")
            await broker.place_order(
                symbol=signal.trading_symbol, exchange=EXCHANGE,
                transaction_type="SELL" if side == "BUY" else "BUY",
                quantity=decision.position_size, order_type="MARKET",
                product=product, tag="BOT_EMERGENCY_SQ", trade_id=str(trade_id),
            )
            await self._cancel_trade_record(trade_id)
            return None

        # ── 7. Target order ───────────────────────────────────────────────────
        # Swing trades: skip broker LIMIT target — in-process trailing stop owns exits.
        # Intraday trades: place LIMIT at 1:2R so EOD square-off isn't the only exit.
        if product == "CNC":
            log.info("executor.swing_no_limit_target", symbol=signal.trading_symbol,
                     reason="trailing stop manages swing exits; broker limit would cap at 1:2R")
        else:
            await broker.place_target(
                symbol      = signal.trading_symbol,
                exchange    = EXCHANGE,
                quantity    = decision.position_size,
                limit_price = decision.target,
                product     = product,
                direction   = direction,
                tag         = "BOT_TGT",
                trade_id    = str(trade_id),
            )

        # ── 8. Telegram notification ──────────────────────────────────────────
        _trade_type = "SWING" if signal.timeframe in ("1day", "1week") else "INTRADAY"
        await get_notifier().trade_entry(
            symbol     = signal.trading_symbol,
            direction  = direction,
            price      = signal.price_at_signal,
            quantity   = decision.position_size,
            stop_loss  = decision.stop_loss,
            target_1   = decision.target,
            target_2   = None,
            strategy   = signal.signal_type.value,
            confidence = ai_decision.confidence,
            broker     = broker.BROKER,
            trade_type = _trade_type,
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

    # ── Pre-order checks ─────────────────────────────────────────────────────

    def _check_structural_rr(
        self,
        signal:   "Signal",
        decision: "RiskDecision",
    ) -> tuple[bool, str, float | None]:
        """
        Check if the mathematical target lands inside a known resistance zone.

        Uses swing_high, r1, r2 (pivot resistance) from signal.indicators.
        These are pre-computed by indicators.py and stored in the signal.

        Returns (ok, reason, adjusted_target):
          - ok=True, adjusted_target=None  → target is clean, no resistance in path
          - ok=True, adjusted_target=X     → resistance found but effective R:R ≥ 1.5;
                                             target adjusted to just below resistance
          - ok=False, reason=...           → resistance chokes R:R below 1.5; reject

        Only applied to BULLISH (long) trades — short-side resistance logic is inverse
        and currently disabled (short side not in production).
        Only applied when direction is BULLISH and target > entry.
        """
        MIN_RR = 1.5

        entry  = signal.price_at_signal
        stop   = decision.stop_loss
        target = decision.target
        ind    = signal.indicators or {}

        if signal.direction.value != "BULLISH" or target <= entry:
            return True, "ok", None   # Not applicable

        risk = entry - stop
        if risk <= 0:
            return True, "ok", None

        # Collect resistance levels between entry and target
        resistance_levels: list[float] = []

        swing_high = ind.get("swing_high")
        if swing_high and entry < float(swing_high) < target:
            resistance_levels.append(float(swing_high))

        r1 = ind.get("r1")
        if r1 and entry < float(r1) < target:
            resistance_levels.append(float(r1))

        r2 = ind.get("r2")
        if r2 and entry < float(r2) < target:
            resistance_levels.append(float(r2))

        if not resistance_levels:
            return True, "ok", None   # Clear path to target

        # Nearest resistance in the path
        nearest_resistance = min(resistance_levels)

        # Adjust target to just below resistance (give 0.3% buffer to avoid sitting right at it)
        effective_target = round(nearest_resistance * 0.997, 2)

        effective_rr = (effective_target - entry) / risk
        if effective_rr < MIN_RR:
            return False, (
                f"Resistance at ₹{nearest_resistance:.1f} chokes R:R to {effective_rr:.2f} "
                f"(min {MIN_RR}) — mathematical target ₹{target:.1f} unreachable"
            ), None

        # R:R still acceptable with adjusted target
        return True, "resistance_adjusted", effective_target

    async def _check_sector_cap(self, symbol: str) -> tuple[bool, str]:
        """
        Enforce max 2 open positions in any single sector.

        Multiple positions in the same sector = one macro bet with N× notional.
        When sector rotates, all positions close at SL simultaneously and wipe
        a week of gains in one session.

        MISC sector (unknown symbols) is exempt — we don't block new listings
        or stocks not in our static map.

        Fail-open on DB/lookup errors so infrastructure issues don't halt trading.
        """
        MAX_SECTOR_POSITIONS = 2

        from config.sector_map import get_sector
        sector = get_sector(symbol)

        if sector == "MISC":
            return True, "MISC sector exempt from cap"

        try:
            from sqlalchemy import text as _text
            async with get_db_session() as session:
                result = await session.execute(
                    _text("SELECT trading_symbol FROM trades WHERE status = 'OPEN'")
                )
                open_symbols = [row[0] for row in result.fetchall()]

            sector_count = sum(1 for s in open_symbols if get_sector(s) == sector)

            if sector_count >= MAX_SECTOR_POSITIONS:
                return False, (
                    f"Sector cap: already {sector_count} open positions in {sector} "
                    f"(max {MAX_SECTOR_POSITIONS}) — correlated risk too high"
                )

            log.debug("executor.sector_ok", symbol=symbol, sector=sector,
                      open_in_sector=sector_count)
            return True, "ok"

        except Exception as e:
            log.warning("executor.sector_cap_error", symbol=symbol, error=str(e))
            return True, f"check_error: {e}"   # fail-open

    async def _check_liquidity(self, symbol: str, direction: str) -> tuple[bool, str]:
        """
        Check that the stock is tradeable right now — not at a circuit limit and
        has reasonable intraday volume. Uses the live Redis tick; no broker API call.

        Rules (applied in all modes):
          • Day volume ≥ 200,000 shares — below this, MIS square-off near 3:20 can
            gap badly because there are too few counterparties.
          • BULLISH: sell_qty (sq) in order book must be > 0.
            sq == 0 means price is pinned at upper circuit — we cannot buy.
          • BEARISH: buy_qty (bq) in order book must be > 0.
            bq == 0 means price is at lower circuit — we cannot sell short.

        Fail-open on missing tick so we don't block trades during brief Redis outages.
        """
        MIN_DAY_VOLUME = 200_000

        try:
            import json as _json
            redis = get_redis()
            raw = await redis.get(f"market:tick:{symbol}")
            if not raw:
                # No tick in Redis — cannot check; allow trade (fail-open)
                log.warning("executor.liquidity_no_tick", symbol=symbol,
                            note="No tick in Redis — skipping liquidity check")
                return True, "no_tick_data"

            tick = _json.loads(raw)
            day_volume = int(tick.get("vol", 0) or 0)
            sell_qty   = int(tick.get("sq",  0) or 0)
            buy_qty    = int(tick.get("bq",  0) or 0)

            if day_volume < MIN_DAY_VOLUME:
                return False, (
                    f"Day volume {day_volume:,} < minimum {MIN_DAY_VOLUME:,} "
                    f"— thin market, MIS square-off risk"
                )

            if direction == "BULLISH" and sell_qty == 0:
                return False, (
                    f"Sell-side order book empty (sq=0) — {symbol} likely at upper circuit"
                )

            if direction == "BEARISH" and buy_qty == 0:
                return False, (
                    f"Buy-side order book empty (bq=0) — {symbol} likely at lower circuit"
                )

            log.debug("executor.liquidity_ok", symbol=symbol,
                      day_vol=day_volume, sell_qty=sell_qty, buy_qty=buy_qty)
            return True, "ok"

        except Exception as e:
            # Fail-open — don't block trades on Redis/parse error
            log.warning("executor.liquidity_check_error", symbol=symbol, error=str(e))
            return True, f"check_error: {e}"

    # ── DB ────────────────────────────────────────────────────────────────────

    async def _update_entry_order_id(self, trade_id: uuid.UUID, side: str) -> None:
        """Back-fill Trade.entry_order_id after the entry MARKET order is confirmed."""
        try:
            from sqlalchemy import text
            async with get_db_session() as session:
                result = await session.execute(
                    text("""
                        SELECT id FROM orders
                        WHERE parent_trade_id = :tid
                          AND transaction_type = :side
                          AND order_type = 'MARKET'
                        ORDER BY placed_at DESC LIMIT 1
                    """),
                    {"tid": str(trade_id), "side": side},
                )
                row = result.fetchone()
                if row:
                    await session.execute(
                        text("UPDATE trades SET entry_order_id = :oid WHERE id = :tid"),
                        {"oid": str(row.id), "tid": str(trade_id)},
                    )
                    await session.commit()
        except Exception as e:
            log.warning("executor.entry_order_id_failed", trade_id=str(trade_id), error=str(e))

    async def _cancel_trade_record(self, trade_id: uuid.UUID) -> None:
        """Mark a Trade row CANCELLED — used when entry order fails after DB insert."""
        try:
            from sqlalchemy import text
            async with get_db_session() as session:
                await session.execute(
                    text("UPDATE trades SET status='CANCELLED' WHERE id=:id"),
                    {"id": str(trade_id)},
                )
                await session.commit()
        except Exception as e:
            log.error("executor.cancel_record_failed", error=str(e), trade_id=str(trade_id))

    async def _record_rejection(
        self,
        signal: Signal,
        stage:  str,
        reason: str,
    ) -> None:
        """Persist a rejected signal to signal_rejection_log for strategy analysis."""
        try:
            redis  = get_redis()
            regime = await redis.get("market:regime") or "UNKNOWN"
            async with get_db_session() as session:
                row = SignalRejectionLog(
                    trading_symbol   = signal.trading_symbol,
                    signal_type      = signal.signal_type.value,
                    direction        = signal.direction.value,
                    confidence       = signal.confidence,
                    price_at_signal  = signal.price_at_signal,
                    indicators       = signal.indicators,
                    timeframe        = signal.timeframe,
                    rejection_stage  = stage,
                    rejection_reason = reason,
                    market_regime    = regime,
                )
                session.add(row)
                await session.commit()
        except Exception as e:
            log.warning("executor.rejection_log_failed", stage=stage, symbol=signal.trading_symbol, error=str(e))

    async def _record_trade(
        self,
        trade_id:    uuid.UUID,
        signal:      Signal,
        direction:   str,
        decision:    RiskDecision,
        ai_decision: AIDecision,
        broker_name: str,
    ) -> Trade:
        rr = (
            abs(decision.target - signal.price_at_signal)
            / abs(signal.price_at_signal - decision.stop_loss)
            if abs(signal.price_at_signal - decision.stop_loss) > 0
            else 0
        )

        redis  = get_redis()
        regime = await redis.get("market:regime") or "UNKNOWN"

        # Derive mode from timeframe: daily signals are swing, everything else intraday
        mode_label = "SWING" if signal.timeframe in ("1day", "1week") else "INTRADAY"

        trade = Trade(
            id                  = trade_id,
            trading_symbol      = signal.trading_symbol,
            exchange            = EXCHANGE,
            instrument_type     = "EQ",
            direction           = direction,
            strategy_name       = signal.signal_type.value,
            strategy_mode       = mode_label,
            broker              = broker_name,
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
            market_regime       = regime,
            status              = "OPEN",
        )

        try:
            async with get_db_session() as session:
                session.add(trade)
                await session.commit()
                await session.refresh(trade)
                return trade
        except Exception as e:
            log.error("executor.db_record_failed", error=str(e), trade_id=str(trade_id))
            return None
