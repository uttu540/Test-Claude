"""
services/execution/zerodha/order_manager.py
────────────────────────────────────────────
Zerodha Kite Connect broker implementation.

Implements BrokerInterface for live and semi-auto modes.
This module never contains paper/dev simulation logic — that lives in
PaperBroker. Call get_broker() from broker_router instead of instantiating
this class directly.

Kite Connect order types used:
  LIMIT   — standard limit order
  MARKET  — market order (fast fills; used for entry)
  SL-M    — stop-loss market (guaranteed execution at stop trigger)
  SL      — stop-loss limit (limit price at stop trigger)
"""
from __future__ import annotations

import uuid
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
from database.connection import get_redis
from services.execution.broker_interface import BrokerInterface
from services.notifications.telegram_bot import get_notifier

log = structlog.get_logger(__name__)


class ZerodhaOrderManager(BrokerInterface):
    """
    Live broker: Zerodha Kite Connect v5.

    Inherits from BrokerInterface:
      - place_stop_loss()  — calls place_order(SL-M)
      - place_target()     — calls place_order(LIMIT)
      - _record_order()    — persists to DB

    Implements abstract methods:
      - place_order, cancel_order, modify_order
      - get_positions, get_portfolio, get_open_orders
      - square_off_all_intraday
    """

    BROKER = "ZERODHA"

    def __init__(self) -> None:
        self._kite: KiteConnect | None = None

    # ── Auth ──────────────────────────────────────────────────────────────────

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

    # ── BrokerInterface: place_order ──────────────────────────────────────────

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
        validity:         str   = "DAY",
        trade_id:         str | None = None,
    ) -> str | None:
        """
        Place a live order via Kite Connect.
        Returns broker_order_id on success, None on failure.
        Records the attempt in DB regardless of outcome.
        """
        order_db_id = str(uuid.uuid4())

        try:
            kite = await self._get_kite()

            params: dict[str, Any] = {
                "tradingsymbol":    symbol,
                "exchange":         exchange,
                "transaction_type": transaction_type,
                "quantity":         quantity,
                "order_type":       order_type,
                "product":          product,
                "validity":         validity,
                "tag":              tag or "TRADING_BOT",  # SEBI: algo orders must be tagged
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
                symbol    = symbol,
                direction = transaction_type,
                qty       = quantity,
                price     = price or "MARKET",
                broker_id = broker_order_id,
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
            await get_notifier().system_error("OrderManager", f"Order rejected: {symbol} | {e}")
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

    # ── BrokerInterface: order management ─────────────────────────────────────

    async def cancel_order(self, broker_order_id: str) -> bool:
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
        price:           float | None = None,
        trigger_price:   float | None = None,
        quantity:        int   | None = None,
    ) -> bool:
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

    # ── BrokerInterface: portfolio ─────────────────────────────────────────────

    async def get_positions(self) -> dict:
        kite = await self._get_kite()
        return kite.positions()

    async def get_portfolio(self) -> list[dict]:
        kite = await self._get_kite()
        return kite.holdings()

    async def get_open_orders(self) -> list[dict]:
        """Return today's full order list from Kite (used by lifecycle manager)."""
        try:
            kite = await self._get_kite()
            return kite.orders()
        except Exception as e:
            log.warning("order.fetch_failed", error=str(e))
            return []

    # ── BrokerInterface: square off ───────────────────────────────────────────

    async def square_off_all_intraday(self) -> None:
        """Market-sell all open MIS positions. Called at 3:12 PM or on kill switch."""
        log.warning("order.square_off_all_intraday", reason="called")
        positions = await self.get_positions()

        for pos in positions.get("day", []):
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


# ── Backwards-compatible alias (used in authenticator + old imports) ──────────
OrderManager = ZerodhaOrderManager
