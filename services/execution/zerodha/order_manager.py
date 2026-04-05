"""
services/execution/zerodha/order_manager.py
────────────────────────────────────────────
Zerodha Kite Connect order placement and management.

All order placements go through the Risk Engine first.
This module ONLY handles the broker communication layer.

Important Kite Connect order types used:
  - LIMIT:  Standard limit order
  - MARKET: Market order (fast fills, use sparingly)
  - SL-M:   Stop-loss market (guaranteed execution at stop trigger)
  - SL:     Stop-loss limit (limit price at stop trigger)
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

import structlog
from kiteconnect import KiteConnect
from kiteconnect.exceptions import (
    InputException,
    NetworkException,
    OrderException,
    TokenException,
)
from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import settings
from database.connection import get_db_session, get_redis
from database.models import Order
from services.notifications.telegram_bot import get_notifier

log = structlog.get_logger(__name__)


class OrderManager:
    """
    Handles order placement, modification, and cancellation via Kite Connect.
    Logs every order to database and Redis regardless of success/failure.
    """

    BROKER = "ZERODHA"

    def __init__(self):
        self._kite: KiteConnect | None = None

    async def _get_kite(self) -> KiteConnect:
        """Return authenticated KiteConnect instance (reads token from Redis)."""
        if self._kite is None:
            self._kite = KiteConnect(api_key=settings.kite_api_key)

        redis = get_redis()
        token = await redis.get("kite:access_token")
        if not token:
            raise RuntimeError("No Kite access token in Redis. Run authenticator first.")
        self._kite.set_access_token(token)
        return self._kite

    # ── Order Placement ───────────────────────────────────────────────────────

    async def place_order(
        self,
        symbol:           str,
        exchange:         str,
        transaction_type: str,        # "BUY" or "SELL"
        quantity:         int,
        order_type:       str,        # "LIMIT", "MARKET", "SL", "SL-M"
        product:          str,        # "MIS" (intraday), "CNC" (delivery), "NRML" (F&O)
        price:            float = 0,  # For LIMIT orders
        trigger_price:    float = 0,  # For SL/SL-M orders
        tag:              str   = "",
        validity:         str   = "DAY",
        trade_id:         str | None = None,
    ) -> str | None:
        """
        Place an order. Returns broker_order_id on success, None on failure.
        Records the attempt in database regardless of outcome.
        """
        # In dev mode: simulate the order
        if settings.is_dev:
            return await self._simulate_order(
                symbol, exchange, transaction_type, quantity,
                order_type, product, price, trigger_price, tag, trade_id
            )

        order_db_id = str(uuid.uuid4())

        try:
            kite = await self._get_kite()

            params: dict[str, Any] = {
                "tradingsymbol":   symbol,
                "exchange":        exchange,
                "transaction_type": transaction_type,
                "quantity":        quantity,
                "order_type":      order_type,
                "product":         product,
                "validity":        validity,
                "tag":             tag or "TRADING_BOT",     # SEBI: all algo orders must be tagged
            }
            if order_type in ("LIMIT", "SL"):
                params["price"] = price
            if order_type in ("SL", "SL-M"):
                params["trigger_price"] = trigger_price

            broker_order_id = self._place_with_retry(kite, params)

            await self._record_order(
                internal_id      = order_db_id,
                broker_order_id  = broker_order_id,
                symbol           = symbol,
                exchange         = exchange,
                transaction_type = transaction_type,
                order_type       = order_type,
                product          = product,
                quantity         = quantity,
                price            = price,
                trigger_price    = trigger_price,
                status           = "OPEN",
                tag              = tag,
                trade_id         = trade_id,
            )

            log.info(
                "order.placed",
                symbol=symbol,
                direction=transaction_type,
                qty=quantity,
                price=price or "MARKET",
                broker_id=broker_order_id,
            )
            return broker_order_id

        except (InputException, OrderException) as e:
            log.error("order.rejected", symbol=symbol, error=str(e))
            await self._record_order(
                internal_id      = order_db_id,
                broker_order_id  = None,
                symbol           = symbol,
                exchange         = exchange,
                transaction_type = transaction_type,
                order_type       = order_type,
                product          = product,
                quantity         = quantity,
                price            = price,
                trigger_price    = trigger_price,
                status           = "REJECTED",
                tag              = tag,
                trade_id         = trade_id,
                rejection_reason = str(e),
            )
            notifier = get_notifier()
            await notifier.system_error("OrderManager", f"Order rejected: {symbol} | {e}")
            return None

        except TokenException:
            log.error("order.token_expired", symbol=symbol)
            raise RuntimeError("Kite access token expired. Re-authentication required.")

        except NetworkException as e:
            log.error("order.network_error", symbol=symbol, error=str(e))
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        reraise=True,
    )
    def _place_with_retry(self, kite: KiteConnect, params: dict) -> str:
        """Place order with automatic retry on transient network errors."""
        return kite.place_order(variety=kite.VARIETY_REGULAR, **params)

    # ── Stop Loss & Target Orders ─────────────────────────────────────────────

    async def place_stop_loss(
        self,
        symbol:        str,
        exchange:      str,
        quantity:      int,
        trigger_price: float,
        product:       str,
        tag:           str = "",
        trade_id:      str | None = None,
    ) -> str | None:
        """
        Place a Stop-Loss Market (SL-M) order.
        SL-M guarantees execution (sells at market price when trigger is hit).
        Preferred over SL-L for stop losses to avoid getting stuck.
        """
        return await self.place_order(
            symbol           = symbol,
            exchange         = exchange,
            transaction_type = "SELL",
            quantity         = quantity,
            order_type       = "SL-M",
            product          = product,
            trigger_price    = trigger_price,
            tag              = tag,
            trade_id         = trade_id,
        )

    async def place_target(
        self,
        symbol:        str,
        exchange:      str,
        quantity:      int,
        limit_price:   float,
        product:       str,
        tag:           str = "",
        trade_id:      str | None = None,
    ) -> str | None:
        """Place a LIMIT sell order for take-profit."""
        return await self.place_order(
            symbol           = symbol,
            exchange         = exchange,
            transaction_type = "SELL",
            quantity         = quantity,
            order_type       = "LIMIT",
            product          = product,
            price            = limit_price,
            tag              = tag,
            trade_id         = trade_id,
        )

    # ── Order Management ──────────────────────────────────────────────────────

    async def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel an open order. Returns True on success."""
        if settings.is_dev:
            log.info("order.cancel_simulated", broker_order_id=broker_order_id)
            return True
        try:
            kite = await self._get_kite()
            kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=broker_order_id)
            log.info("order.cancelled", broker_order_id=broker_order_id)
            return True
        except Exception as e:
            log.error("order.cancel_failed", broker_order_id=broker_order_id, error=str(e))
            return False

    async def modify_order(
        self,
        broker_order_id: str,
        price: float | None = None,
        trigger_price: float | None = None,
        quantity: int | None = None,
    ) -> bool:
        """Modify an open order (e.g., move stop loss)."""
        if settings.is_dev:
            log.info("order.modify_simulated", broker_order_id=broker_order_id)
            return True
        try:
            kite   = await self._get_kite()
            params = {"order_id": broker_order_id, "variety": kite.VARIETY_REGULAR}
            if price is not None:         params["price"]         = price
            if trigger_price is not None: params["trigger_price"] = trigger_price
            if quantity is not None:      params["quantity"]      = quantity
            kite.modify_order(**params)
            log.info("order.modified", broker_order_id=broker_order_id)
            return True
        except Exception as e:
            log.error("order.modify_failed", broker_order_id=broker_order_id, error=str(e))
            return False

    async def get_positions(self) -> dict:
        """Fetch current open positions from Kite."""
        if settings.is_dev:
            return {"net": [], "day": []}
        kite = await self._get_kite()
        return kite.positions()

    async def get_portfolio(self) -> list[dict]:
        """Fetch holdings (delivery/CNC positions)."""
        if settings.is_dev:
            return []
        kite = await self._get_kite()
        return kite.holdings()

    # ── Square Off All (Emergency) ────────────────────────────────────────────

    async def square_off_all_intraday(self) -> None:
        """
        Market-sell all open MIS (intraday) positions.
        Called automatically at 3:20 PM or on kill switch.
        """
        log.warning("order.square_off_all_intraday", reason="called")
        positions = await self.get_positions()
        day_positions = positions.get("day", [])

        for pos in day_positions:
            if pos["product"] == "MIS" and pos["quantity"] != 0:
                qty  = abs(pos["quantity"])
                side = "SELL" if pos["quantity"] > 0 else "BUY"
                await self.place_order(
                    symbol           = pos["tradingsymbol"],
                    exchange         = pos["exchange"],
                    transaction_type = side,
                    quantity         = qty,
                    order_type       = "MARKET",
                    product          = "MIS",
                    tag              = "SQUARE_OFF",
                )
                log.info("order.squared_off", symbol=pos["tradingsymbol"], qty=qty)

    # ── Dev Simulation ────────────────────────────────────────────────────────

    async def _simulate_order(
        self, symbol, exchange, tx_type, qty, order_type, product,
        price, trigger_price, tag, trade_id
    ) -> str:
        """Simulate an order fill in dev/paper mode."""
        fake_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
        log.info(
            "order.simulated",
            symbol=symbol,
            direction=tx_type,
            qty=qty,
            price=price or "MARKET",
            order_id=fake_id,
        )
        await self._record_order(
            internal_id      = str(uuid.uuid4()),
            broker_order_id  = fake_id,
            symbol           = symbol,
            exchange         = exchange,
            transaction_type = tx_type,
            order_type       = order_type,
            product          = product,
            quantity         = qty,
            price            = price,
            trigger_price    = trigger_price,
            status           = "COMPLETE",
            tag              = tag or "PAPER",
            trade_id         = trade_id,
        )
        return fake_id

    # ── Database Recording ────────────────────────────────────────────────────

    async def _record_order(self, **kwargs) -> None:
        """Persist order record to database (best-effort — never blocks execution)."""
        try:
            async for session in get_db_session():
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
                    parent_trade_id  = uuid.UUID(kwargs["trade_id"]) if kwargs.get("trade_id") else None,
                    rejection_reason = kwargs.get("rejection_reason"),
                )
                session.add(order)
                await session.commit()
        except Exception as e:
            log.error("order.record_failed", error=str(e))
