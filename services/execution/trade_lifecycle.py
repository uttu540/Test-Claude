"""
services/execution/trade_lifecycle.py
───────────────────────────────────────
Monitors open trades and closes them when exit conditions are met.

Exit conditions:
  TARGET    — exit price reached the planned target
  STOP_LOSS — exit price hit the stop loss
  TIME_EXIT — forced close at 3:20 PM (intraday square-off)
  MANUAL    — triggered externally via kill switch / API

Two detection modes (selected automatically based on APP_ENV):

  LIVE / PAPER mode:
    Polls Kite Connect order book every 30s.
    When the SL-M or LIMIT target order shows status=COMPLETE,
    that is the authoritative exit signal.

  DEV mode:
    No real broker. Monitors Redis tick cache every 10s.
    Simulates exit when tick price crosses the planned SL or target.

On exit detection:
  1. Calculates gross P&L
  2. Calculates Zerodha charges (brokerage, STT, GST, etc.)
  3. Updates Trade record: exit_price, exit_time, exit_reason,
     gross_pnl, net_pnl, all charge fields, risk_reward_actual,
     r_multiple, status=CLOSED
  4. Sends Telegram notification (SL hit or target hit)
  5. Writes / updates DailyPnL aggregate row

Also called by main.py scheduler at 3:20 PM for EOD square-off,
and by the kill-switch endpoint for emergency closure.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, datetime
from decimal import Decimal

import structlog
from sqlalchemy import text, select, update

from config.settings import settings
from database.connection import get_db_session, get_redis
from database.models import DailyPnL, Trade
from services.execution.charges import calculate_intraday_charges
from services.notifications.telegram_bot import get_notifier

log = structlog.get_logger(__name__)

# Poll intervals
_LIVE_POLL_INTERVAL = 30    # seconds — Kite order book poll
_DEV_POLL_INTERVAL  = 10    # seconds — Redis tick check


class TradeLifecycleManager:
    """
    Background service that watches open trades and closes them on exit.

    Start it once on bot startup:
        manager = get_lifecycle_manager()
        asyncio.create_task(manager.run())
    """

    def __init__(self) -> None:
        self._running = False

    async def run(self) -> None:
        """Main monitoring loop — runs until stopped."""
        self._running = True
        interval = _DEV_POLL_INTERVAL if (settings.is_dev or settings.is_paper) else _LIVE_POLL_INTERVAL
        log.info("lifecycle.started", mode=settings.app_env.value, interval=interval)

        while self._running:
            try:
                if settings.is_dev or settings.is_paper:
                    await self._check_via_ticks()
                else:
                    await self._check_via_broker()
            except Exception as e:
                log.error("lifecycle.poll_error", error=str(e))

            await asyncio.sleep(interval)

    def stop(self) -> None:
        self._running = False

    # ── EOD / Kill-switch ─────────────────────────────────────────────────────

    async def close_all_open_trades(self, reason: str = "TIME_EXIT") -> int:
        """
        Force-close every OPEN trade at current market price.
        Called at 3:20 PM by scheduler, or on kill switch.
        Returns the number of trades closed.
        """
        open_trades = await self._load_open_trades()
        closed = 0
        for trade in open_trades:
            price = await self._get_current_price(trade["trading_symbol"])
            if price is None:
                # No live tick in Redis (feed not running or symbol unsubscribed).
                # Fall back to entry price so the trade is still closed at EOD
                # rather than silently left open.
                price = float(trade["entry_price"])
                log.warning(
                    "lifecycle.no_tick_fallback",
                    symbol=trade["trading_symbol"],
                    using_entry_price=price,
                )
            await self._close_trade(trade, exit_price=price, reason=reason)
            closed += 1
        log.info("lifecycle.force_close_all", count=closed, reason=reason)
        return closed

    # ── Dev mode: tick-based monitoring ──────────────────────────────────────

    async def _check_via_ticks(self) -> None:
        """
        Dev/paper: read latest tick from Redis and check if SL/target crossed.
        """
        open_trades = await self._load_open_trades()
        if not open_trades:
            return

        for trade in open_trades:
            price = await self._get_current_price(trade["trading_symbol"])
            if not price:
                continue

            exit_price, reason = self._check_exit_conditions(trade, price)
            if exit_price:
                await self._close_trade(trade, exit_price=exit_price, reason=reason)

    def _check_exit_conditions(
        self,
        trade: dict,
        current_price: float,
    ) -> tuple[float | None, str]:
        """
        Returns (exit_price, reason) if an exit condition is met, else (None, '').
        """
        stop   = float(trade["planned_stop_loss"] or 0)
        target = float(trade["planned_target_1"]  or 0)
        is_long = trade["direction"] == "LONG"

        if not stop or not target:
            return None, ""

        if is_long:
            if current_price <= stop:
                return stop, "STOP_LOSS"
            if current_price >= target:
                return target, "TARGET"
        else:
            if current_price >= stop:
                return stop, "STOP_LOSS"
            if current_price <= target:
                return target, "TARGET"

        return None, ""

    # ── Live mode: broker order book monitoring ───────────────────────────────

    async def _check_via_broker(self) -> None:
        """
        Live/paper: poll Kite order book for completed SL/target orders.
        Matches orders by parent_trade_id stored in our Order table.
        """
        try:
            from services.execution.zerodha.order_manager import OrderManager
            om = OrderManager()
            kite = await om._get_kite()
            kite_orders = kite.orders()
        except Exception as e:
            log.warning("lifecycle.broker_poll_failed", error=str(e))
            return

        # Build a map of broker_order_id → kite order
        kite_map = {o["order_id"]: o for o in kite_orders}

        open_trades = await self._load_open_trades()
        for trade in open_trades:
            await self._check_broker_orders(trade, kite_map)

    async def _check_broker_orders(self, trade: dict, kite_map: dict) -> None:
        """Check if any child orders for this trade have completed."""
        trade_id = trade["id"]

        async for session in get_db_session():
            result = await session.execute(
                text("""
                    SELECT broker_order_id, order_type, transaction_type, average_price
                    FROM orders
                    WHERE parent_trade_id = :tid
                      AND status != 'COMPLETE'
                      AND order_type IN ('SL-M', 'LIMIT')
                """),
                {"tid": str(trade_id)},
            )
            child_orders = result.fetchall()

        for order in child_orders:
            broker_id = order.broker_order_id
            if not broker_id or broker_id not in kite_map:
                continue

            kite_order = kite_map[broker_id]
            if kite_order["status"] != "COMPLETE":
                continue

            # Update Order record
            await self._mark_order_complete(broker_id, kite_order)

            # Determine exit reason
            if order.order_type == "SL-M":
                reason = "STOP_LOSS"
                exit_price = float(kite_order.get("average_price") or trade["planned_stop_loss"])
            else:
                reason = "TARGET"
                exit_price = float(kite_order.get("average_price") or trade["planned_target_1"])

            await self._close_trade(trade, exit_price=exit_price, reason=reason)

            # Cancel the other pending child order
            await self._cancel_sibling_order(str(trade_id), skip_broker_id=broker_id)
            return

    async def _mark_order_complete(self, broker_order_id: str, kite_order: dict) -> None:
        try:
            async for session in get_db_session():
                await session.execute(
                    text("""
                        UPDATE orders
                        SET status = 'COMPLETE',
                            average_price = :avg,
                            filled_quantity = :qty,
                            updated_at = NOW()
                        WHERE broker_order_id = :bid
                    """),
                    {
                        "avg": kite_order.get("average_price"),
                        "qty": kite_order.get("filled_quantity", 0),
                        "bid": broker_order_id,
                    },
                )
                await session.commit()
        except Exception as e:
            log.error("lifecycle.order_update_failed", broker_id=broker_order_id, error=str(e))

    async def _cancel_sibling_order(self, trade_id: str, skip_broker_id: str) -> None:
        """Cancel the other leg (SL if target hit, or target if SL hit)."""
        try:
            from services.execution.zerodha.order_manager import OrderManager
            om = OrderManager()

            async for session in get_db_session():
                result = await session.execute(
                    text("""
                        SELECT broker_order_id FROM orders
                        WHERE parent_trade_id = :tid
                          AND broker_order_id != :skip
                          AND status NOT IN ('COMPLETE', 'CANCELLED', 'REJECTED')
                    """),
                    {"tid": trade_id, "skip": skip_broker_id},
                )
                for row in result.fetchall():
                    if row.broker_order_id:
                        await om.cancel_order(row.broker_order_id)
        except Exception as e:
            log.warning("lifecycle.cancel_sibling_failed", trade_id=trade_id, error=str(e))

    # ── Core: close a trade ───────────────────────────────────────────────────

    async def _close_trade(
        self,
        trade:      dict,
        exit_price: float,
        reason:     str,
    ) -> None:
        """
        Close a trade: calculate P&L + charges, update DB, notify Telegram.
        """
        trade_id    = trade["id"]
        symbol      = trade["trading_symbol"]
        entry_price = float(trade["entry_price"])
        quantity    = int(trade["entry_quantity"])
        direction   = trade["direction"]

        # ── Gross P&L ─────────────────────────────────────────────────────────
        multiplier = 1 if direction == "LONG" else -1
        gross_pnl  = (exit_price - entry_price) * quantity * multiplier

        # ── Charges ───────────────────────────────────────────────────────────
        charges = calculate_intraday_charges(
            entry_price = entry_price,
            exit_price  = exit_price,
            quantity    = quantity,
            direction   = direction,
        )
        net_pnl = gross_pnl - charges.total

        # ── Risk metrics ──────────────────────────────────────────────────────
        risk         = abs(entry_price - float(trade["planned_stop_loss"] or entry_price))
        reward       = abs(exit_price  - entry_price)
        rr_actual    = round(reward / risk, 2)   if risk > 0 else 0.0
        r_multiple   = round(gross_pnl / (risk * quantity), 2) if risk > 0 and quantity > 0 else 0.0

        now = datetime.now()

        # ── Update Trade record ───────────────────────────────────────────────
        try:
            async for session in get_db_session():
                await session.execute(
                    text("""
                        UPDATE trades SET
                            status            = 'CLOSED',
                            exit_price        = :exit_price,
                            exit_quantity     = :qty,
                            exit_time         = :exit_time,
                            exit_reason       = :reason,
                            gross_pnl         = :gross_pnl,
                            brokerage         = :brokerage,
                            stt               = :stt,
                            exchange_charges  = :exchange_charges,
                            sebi_charges      = :sebi_charges,
                            gst               = :gst,
                            stamp_duty        = :stamp_duty,
                            net_pnl           = :net_pnl,
                            risk_reward_actual= :rr_actual,
                            r_multiple        = :r_multiple,
                            updated_at        = NOW()
                        WHERE id = :trade_id
                    """),
                    {
                        "exit_price":       exit_price,
                        "qty":              quantity,
                        "exit_time":        now,
                        "reason":           reason,
                        "gross_pnl":        round(gross_pnl, 4),
                        "brokerage":        charges.brokerage,
                        "stt":              charges.stt,
                        "exchange_charges": charges.exchange_charges,
                        "sebi_charges":     charges.sebi_charges,
                        "gst":              charges.gst,
                        "stamp_duty":       charges.stamp_duty,
                        "net_pnl":          round(net_pnl, 4),
                        "rr_actual":        rr_actual,
                        "r_multiple":       r_multiple,
                        "trade_id":         str(trade_id),
                    },
                )
                await session.commit()

            log.info(
                "lifecycle.trade_closed",
                symbol      = symbol,
                reason      = reason,
                entry       = entry_price,
                exit        = exit_price,
                gross_pnl   = round(gross_pnl, 2),
                charges     = round(charges.total, 2),
                net_pnl     = round(net_pnl, 2),
                r_multiple  = r_multiple,
            )

        except Exception as e:
            log.error("lifecycle.close_failed", trade_id=str(trade_id), error=str(e))
            return

        # ── Telegram notification ─────────────────────────────────────────────
        await self._notify_exit(
            symbol      = symbol,
            direction   = direction,
            reason      = reason,
            entry_price = entry_price,
            exit_price  = exit_price,
            quantity    = quantity,
            gross_pnl   = gross_pnl,
            net_pnl     = net_pnl,
            charges     = charges.total,
            r_multiple  = r_multiple,
        )

        # ── Update DailyPnL aggregate ─────────────────────────────────────────
        await self._upsert_daily_pnl(now.date())

    # ── DailyPnL aggregation ──────────────────────────────────────────────────

    async def _upsert_daily_pnl(self, trading_date: date) -> None:
        """Recompute and upsert today's DailyPnL from all closed trades."""
        try:
            redis  = get_redis()
            regime = await redis.get("market:regime") or "UNKNOWN"

            async for session in get_db_session():
                result = await session.execute(
                    text("""
                        SELECT
                            COUNT(*)                                           AS total,
                            COUNT(*) FILTER (WHERE net_pnl > 0)               AS wins,
                            COUNT(*) FILTER (WHERE net_pnl < 0)               AS losses,
                            COALESCE(SUM(gross_pnl), 0)                       AS gross_pnl,
                            COALESCE(SUM(brokerage + stt + exchange_charges
                                        + gst + sebi_charges + stamp_duty), 0) AS charges,
                            COALESCE(SUM(net_pnl), 0)                         AS net_pnl,
                            COALESCE(AVG(r_multiple), 0)                      AS avg_r
                        FROM trades
                        WHERE DATE(entry_time) = :today
                          AND status = 'CLOSED'
                    """),
                    {"today": trading_date},
                )
                row = result.fetchone()
                if not row or not row.total:
                    return

                total    = int(row.total)
                wins     = int(row.wins or 0)
                win_rate = round(wins / total * 100, 2) if total else 0

                await session.execute(
                    text("""
                        INSERT INTO daily_pnl
                            (trading_date, total_trades, winning_trades, losing_trades,
                             gross_pnl, total_charges, net_pnl, win_rate,
                             avg_r_multiple, market_regime)
                        VALUES
                            (:date, :total, :wins, :losses,
                             :gross_pnl, :charges, :net_pnl, :win_rate,
                             :avg_r, :regime)
                        ON CONFLICT (trading_date) DO UPDATE SET
                            total_trades   = EXCLUDED.total_trades,
                            winning_trades = EXCLUDED.winning_trades,
                            losing_trades  = EXCLUDED.losing_trades,
                            gross_pnl      = EXCLUDED.gross_pnl,
                            total_charges  = EXCLUDED.total_charges,
                            net_pnl        = EXCLUDED.net_pnl,
                            win_rate       = EXCLUDED.win_rate,
                            avg_r_multiple = EXCLUDED.avg_r_multiple,
                            market_regime  = EXCLUDED.market_regime
                    """),
                    {
                        "date":      trading_date,
                        "total":     total,
                        "wins":      wins,
                        "losses":    int(row.losses or 0),
                        "gross_pnl": float(row.gross_pnl),
                        "charges":   float(row.charges),
                        "net_pnl":   float(row.net_pnl),
                        "win_rate":  win_rate,
                        "avg_r":     float(row.avg_r or 0),
                        "regime":    regime,
                    },
                )
                await session.commit()

            log.info("lifecycle.daily_pnl_updated", date=str(trading_date))

        except Exception as e:
            log.error("lifecycle.daily_pnl_failed", error=str(e))

    # ── Telegram ──────────────────────────────────────────────────────────────

    async def _notify_exit(
        self,
        symbol:      str,
        direction:   str,
        reason:      str,
        entry_price: float,
        exit_price:  float,
        quantity:    int,
        gross_pnl:   float,
        net_pnl:     float,
        charges:     float,
        r_multiple:  float,
    ) -> None:
        try:
            notifier = get_notifier()
            if reason == "TARGET":
                await notifier.target_hit(
                    symbol      = symbol,
                    target_num  = 1,
                    exit_price  = exit_price,
                    entry_price = entry_price,
                    quantity    = quantity,
                    pnl         = net_pnl,
                    r_multiple  = r_multiple,
                )
            elif reason == "STOP_LOSS":
                await notifier.stop_loss_hit(
                    symbol      = symbol,
                    exit_price  = exit_price,
                    entry_price = entry_price,
                    quantity    = quantity,
                    pnl         = net_pnl,
                    r_multiple  = r_multiple,
                )
            else:
                # TIME_EXIT, MANUAL — generic alert
                pnl_str = f"₹{net_pnl:+.2f}"
                await notifier.system_error(
                    component = "TradeLifecycle",
                    message   = (
                        f"{reason}: {direction} {symbol} closed @ ₹{exit_price:.2f} | "
                        f"Net P&L: {pnl_str} | R: {r_multiple:.1f}x"
                    ),
                )
        except Exception as e:
            log.warning("lifecycle.notify_failed", symbol=symbol, error=str(e))

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _load_open_trades(self) -> list[dict]:
        """Load all OPEN trades from DB."""
        try:
            async for session in get_db_session():
                result = await session.execute(
                    text("""
                        SELECT id, trading_symbol, direction,
                               entry_price, entry_quantity,
                               planned_stop_loss, planned_target_1
                        FROM trades
                        WHERE status = 'OPEN'
                    """)
                )
                return [dict(row._mapping) for row in result.fetchall()]
        except Exception as e:
            log.error("lifecycle.load_trades_failed", error=str(e))
        return []

    async def _get_current_price(self, symbol: str) -> float | None:
        """Read latest tick price from Redis (dev/paper mode)."""
        try:
            redis = get_redis()
            raw   = await redis.get(f"market:tick:{symbol}")
            if raw:
                return float(json.loads(raw).get("lp", 0) or 0) or None
        except Exception:
            pass
        return None


# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: TradeLifecycleManager | None = None


def get_lifecycle_manager() -> TradeLifecycleManager:
    global _instance
    if _instance is None:
        _instance = TradeLifecycleManager()
    return _instance
