"""
services/execution/paper_broker.py
────────────────────────────────────
Paper trading broker — implements BrokerInterface with full simulation.

Simulates order fills at current market price with configurable slippage.
Never touches a real broker API. All orders are recorded in DB with
broker=PAPER so they're visible in the dashboard alongside live trades.

Used when APP_ENV=paper or APP_ENV=development.
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog

from config.settings import settings
from services.execution.broker_interface import BrokerInterface

log = structlog.get_logger(__name__)

# Simulated slippage as a fraction of price (0.0005 = 0.05%)
# Applied to market orders only; limit/SL orders fill at the stated price.
_SLIPPAGE_PCT = 0.0005


class PaperBroker(BrokerInterface):
    """
    Paper trading broker. Simulates fills without calling any real API.

    Slippage model:
      - MARKET orders: entry_price × (1 + slippage) for BUY,
                       entry_price × (1 - slippage) for SELL
      - LIMIT / SL / SL-M orders: fill at stated price (no slippage)

    Adding a real slippage model later:
      Override _apply_slippage() — the rest of the class stays unchanged.
    """

    BROKER = "PAPER"

    # ── Abstract method implementations ───────────────────────────────────────

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
        fill_price = self._apply_slippage(price or trigger_price, transaction_type, order_type)
        fake_id    = f"PAPER-{uuid.uuid4().hex[:8].upper()}"

        log.info(
            "paper.order_simulated",
            symbol     = symbol,
            direction  = transaction_type,
            qty        = quantity,
            order_type = order_type,
            fill_price = fill_price,
            order_id   = fake_id,
        )

        await self._record_order(
            internal_id      = str(uuid.uuid4()),
            broker_order_id  = fake_id,
            symbol           = symbol,
            exchange         = exchange,
            transaction_type = transaction_type,
            order_type       = order_type,
            product          = product,
            quantity         = quantity,
            price            = fill_price,
            trigger_price    = trigger_price,
            status           = "COMPLETE",
            tag              = tag or "PAPER",
            trade_id         = trade_id,
        )
        return fake_id

    async def cancel_order(self, broker_order_id: str) -> bool:
        log.info("paper.cancel_simulated", broker_order_id=broker_order_id)
        return True

    async def modify_order(
        self,
        broker_order_id: str,
        price:           float | None = None,
        trigger_price:   float | None = None,
        quantity:        int   | None = None,
    ) -> bool:
        log.info("paper.modify_simulated", broker_order_id=broker_order_id)
        return True

    async def get_positions(self) -> dict:
        # Paper positions are tracked in DB (trades table), not broker
        return {"net": [], "day": []}

    async def get_portfolio(self) -> list[dict]:
        return []

    async def get_open_orders(self) -> list[dict]:
        # Paper mode uses tick-based monitoring — no order-book polling needed
        return []

    async def square_off_all_intraday(self) -> None:
        # TradeLifecycleManager.close_all_open_trades() handles the DB side.
        # No broker call needed for paper.
        log.info("paper.square_off_all_intraday", note="handled_by_lifecycle_manager")

    # ── Slippage model ────────────────────────────────────────────────────────

    def _apply_slippage(
        self,
        price:            float,
        transaction_type: str,
        order_type:       str,
    ) -> float:
        """
        Apply simulated slippage to market orders.
        Override this to implement more realistic fill models.
        """
        if not price or order_type != "MARKET":
            return price
        direction = 1 if transaction_type == "BUY" else -1
        return round(price * (1 + direction * _SLIPPAGE_PCT), 4)
