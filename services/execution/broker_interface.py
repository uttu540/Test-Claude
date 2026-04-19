"""
services/execution/broker_interface.py
────────────────────────────────────────
Abstract base class for all broker implementations.

Every broker (Zerodha, Groww, Paper) must implement this interface.
TradeExecutor and TradeLifecycleManager only depend on this ABC —
never on a concrete broker class.

Adding Groww (Phase 6):
  1. Create services/execution/groww/order_manager.py
  2. Subclass BrokerInterface
  3. Implement the 6 abstract methods
  4. Register in broker_router.py

Concrete helpers (place_stop_loss, place_target, _record_order) are
implemented here once and inherited by all brokers.
"""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Any

import structlog

from database.connection import get_db_session
from database.models import Order

log = structlog.get_logger(__name__)


class BrokerInterface(ABC):
    """
    Contract every broker must satisfy.

    Abstract methods (broker-specific):
      place_order, cancel_order, modify_order,
      get_positions, get_portfolio, get_open_orders, square_off_all_intraday

    Concrete methods (shared, call place_order internally):
      place_stop_loss, place_target, _record_order
    """

    # Subclasses set this at class level: BROKER = "ZERODHA" / "PAPER" / "GROWW"
    BROKER: str = "UNKNOWN"

    # ── Abstract: broker-specific ─────────────────────────────────────────────

    @abstractmethod
    async def place_order(
        self,
        symbol:           str,
        exchange:         str,
        transaction_type: str,
        quantity:         int,
        order_type:       str,
        product:          str,
        price:            float = 0,
        trigger_price:    float = 0,
        tag:              str   = "",
        trade_id:         str | None = None,
    ) -> str | None:
        """Place an order. Returns broker_order_id on success, None on failure."""
        ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel an open order. Returns True on success."""
        ...

    @abstractmethod
    async def modify_order(
        self,
        broker_order_id: str,
        price:           float | None = None,
        trigger_price:   float | None = None,
        quantity:        int   | None = None,
    ) -> bool:
        """Modify an open order (e.g., trail stop loss)."""
        ...

    @abstractmethod
    async def get_positions(self) -> dict:
        """Fetch current open positions from the broker."""
        ...

    @abstractmethod
    async def get_portfolio(self) -> list[dict]:
        """Fetch holdings (delivery / CNC positions)."""
        ...

    @abstractmethod
    async def get_open_orders(self) -> list[dict]:
        """
        Fetch today's order list from the broker.
        Used by TradeLifecycleManager to detect SL/target fills.
        Returns a list of dicts with at minimum: order_id, status, average_price.
        """
        ...

    @abstractmethod
    async def square_off_all_intraday(self) -> None:
        """Emergency square-off: close all open intraday (MIS) positions."""
        ...

    # ── Concrete: shared helpers (no need to override) ───────────────────────

    async def place_stop_loss(
        self,
        symbol:        str,
        exchange:      str,
        quantity:      int,
        trigger_price: float,
        product:       str,
        direction:     str = "LONG",
        tag:           str = "",
        trade_id:      str | None = None,
    ) -> str | None:
        """Place a SL-M order to close the position on stop trigger."""
        transaction_type = "SELL" if direction == "LONG" else "BUY"
        return await self.place_order(
            symbol           = symbol,
            exchange         = exchange,
            transaction_type = transaction_type,
            quantity         = quantity,
            order_type       = "SL-M",
            product          = product,
            trigger_price    = trigger_price,
            tag              = tag,
            trade_id         = trade_id,
        )

    async def place_target(
        self,
        symbol:      str,
        exchange:    str,
        quantity:    int,
        limit_price: float,
        product:     str,
        direction:   str = "LONG",
        tag:         str = "",
        trade_id:    str | None = None,
    ) -> str | None:
        """Place a LIMIT take-profit order to close the position at target."""
        transaction_type = "SELL" if direction == "LONG" else "BUY"
        return await self.place_order(
            symbol           = symbol,
            exchange         = exchange,
            transaction_type = transaction_type,
            quantity         = quantity,
            order_type       = "LIMIT",
            product          = product,
            price            = limit_price,
            tag              = tag,
            trade_id         = trade_id,
        )

    async def _record_order(self, **kwargs: Any) -> None:
        """
        Persist an order record to the database.
        Best-effort — never blocks or raises (just logs on failure).
        Called by both live and paper brokers.
        """
        try:
            async with get_db_session() as session:
                order = Order(
                    id               = uuid.UUID(kwargs["internal_id"]),
                    broker           = self.BROKER,
                    broker_order_id  = kwargs.get("broker_order_id"),
                    trading_symbol   = kwargs["symbol"],
                    exchange         = kwargs["exchange"],
                    transaction_type = kwargs["transaction_type"],
                    order_type       = kwargs["order_type"],
                    product          = kwargs["product"],
                    quantity         = kwargs["quantity"],
                    price            = kwargs.get("price") or None,
                    trigger_price    = kwargs.get("trigger_price") or None,
                    status           = kwargs.get("status", "PENDING"),
                    tag              = kwargs.get("tag"),
                    parent_trade_id  = (
                        uuid.UUID(kwargs["trade_id"]) if kwargs.get("trade_id") else None
                    ),
                    rejection_reason = kwargs.get("rejection_reason"),
                )
                try:
                    session.add(order)
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
        except Exception as e:
            log.error(
                "order.record_failed",
                symbol          = kwargs.get("symbol"),
                broker_order_id = kwargs.get("broker_order_id"),
                error           = str(e),
            )
