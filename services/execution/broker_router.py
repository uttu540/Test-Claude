"""
services/execution/broker_router.py
─────────────────────────────────────
Factory that returns the correct broker based on APP_ENV.

All callers use get_broker() — they never import a concrete broker class
directly. This means adding a new broker only requires:
  1. Implementing BrokerInterface in a new module
  2. Updating get_broker() here

Current routing:
  live / semi-auto  →  ZerodhaOrderManager (real money)
  paper / dev       →  PaperBroker         (simulation)
  (future) groww    →  GrowwOrderManager    (Phase 6)
"""
from __future__ import annotations

from config.settings import settings
from services.execution.broker_interface import BrokerInterface


def get_broker() -> BrokerInterface:
    """
    Return the appropriate broker for the current APP_ENV.

    Called per-trade (stateless instantiation is intentional — brokers hold
    no mutable state beyond a cached Kite client, which is cheap to rebuild).
    """
    if settings.is_live or settings.is_semi_auto:
        from services.execution.zerodha.order_manager import ZerodhaOrderManager
        return ZerodhaOrderManager()

    from services.execution.paper_broker import PaperBroker
    return PaperBroker()
