"""
services/execution/approval_gate.py
──────────────────────────────────────
Human-in-the-loop trade approval gate for semi-auto mode.

Flow:
  1. TradeExecutor calls request_approval() before placing orders
  2. A Telegram message is sent with trade details and ✅ / ❌ buttons
  3. request_approval() awaits an asyncio.Event (max APPROVAL_TIMEOUT_SECS)
  4. When the user taps a button, Telegram delivers a callback query
  5. The Telegram Application handler calls resolve_approval() which sets the event
  6. request_approval() returns True (approved) or False (rejected / timed out)

Security:
  - Only Telegram user IDs listed in TELEGRAM_AUTHORIZED_IDS can approve
  - Each approval_id is a UUID — not guessable
  - Expired/unknown approval_ids are silently dropped

This module is stateless except for the in-process _pending dict.
If the bot restarts while waiting, the trade is rejected (conservative).
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime

import structlog

from config.settings import settings

log = structlog.get_logger(__name__)

# In-process state: approval_id → [event, approved_result]
_pending: dict[str, list] = {}


@dataclass
class ApprovalRequest:
    """Trade details shown in the approval message."""
    approval_id:   str
    symbol:        str
    direction:     str
    entry_price:   float
    stop_loss:     float
    target:        float
    quantity:      int
    risk_inr:      float
    rr_ratio:      float
    strategy:      str
    signal_conf:   int
    ai_conf:       float
    ai_reasoning:  str


async def request_approval(req: ApprovalRequest) -> bool:
    """
    Send a Telegram approval request and wait for the user's response.

    Returns True if approved within timeout, False otherwise.
    Called from TradeExecutor — blocks until response or timeout.
    """
    event: asyncio.Event = asyncio.Event()
    _pending[req.approval_id] = [event, None]

    # Send the Telegram approval message with inline buttons
    from services.notifications.telegram_bot import get_notifier
    notifier = get_notifier()
    await notifier.send_approval_request(req)

    log.info(
        "approval.waiting",
        approval_id = req.approval_id,
        symbol      = req.symbol,
        timeout     = settings.approval_timeout_secs,
    )

    try:
        await asyncio.wait_for(event.wait(), timeout=float(settings.approval_timeout_secs))
        approved = _pending[req.approval_id][1] is True
        log.info(
            "approval.resolved",
            approval_id = req.approval_id,
            symbol      = req.symbol,
            approved    = approved,
        )
        return approved

    except asyncio.TimeoutError:
        log.warning(
            "approval.timeout",
            approval_id = req.approval_id,
            symbol      = req.symbol,
            timeout     = settings.approval_timeout_secs,
        )
        # Notify user that the window expired
        from services.notifications.telegram_bot import get_notifier
        await get_notifier().send_approval_expired(req.approval_id, req.symbol)
        return False

    finally:
        _pending.pop(req.approval_id, None)


async def resolve_approval(approval_id: str, approved: bool) -> bool:
    """
    Called by the Telegram callback handler when a button is pressed.
    Returns True if the approval_id was found and resolved, False if unknown/expired.
    """
    if approval_id not in _pending:
        log.warning("approval.unknown_id", approval_id=approval_id)
        return False

    _pending[approval_id][1] = approved
    _pending[approval_id][0].set()
    return True


def build_approval_request(
    *,
    trade_id:    str,
    symbol:      str,
    direction:   str,
    entry_price: float,
    stop_loss:   float,
    target:      float,
    quantity:    int,
    risk_inr:    float,
    strategy:    str,
    signal_conf: int,
    ai_conf:     float,
    ai_reasoning: str,
) -> ApprovalRequest:
    """Convenience constructor — builds ApprovalRequest from trade executor params."""
    rr = abs(target - entry_price) / abs(entry_price - stop_loss) if entry_price != stop_loss else 0
    return ApprovalRequest(
        approval_id  = trade_id,
        symbol       = symbol,
        direction    = direction,
        entry_price  = entry_price,
        stop_loss    = stop_loss,
        target       = target,
        quantity     = quantity,
        risk_inr     = risk_inr,
        rr_ratio     = round(rr, 2),
        strategy     = strategy,
        signal_conf  = signal_conf,
        ai_conf      = ai_conf,
        ai_reasoning = ai_reasoning,
    )
