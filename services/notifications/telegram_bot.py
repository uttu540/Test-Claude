"""
services/notifications/telegram_bot.py
────────────────────────────────────────
Telegram notification service.

Sends real-time alerts for:
  - Trade entries and exits
  - Stop loss triggers / target hits
  - Daily P&L summary
  - System errors and kill switch events
  - Signal alerts (dev/paper/semi-auto mode)

Semi-auto mode additionally:
  - Sends approval request messages with inline ✅ / ❌ keyboard buttons
  - Runs a background Telegram Application to receive callback queries
  - Routes callback results to approval_gate.resolve_approval()
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

import structlog
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError

from config.settings import settings

if TYPE_CHECKING:
    from services.execution.approval_gate import ApprovalRequest

log = structlog.get_logger(__name__)


class AlertLevel(str, Enum):
    INFO    = "ℹ️"
    SUCCESS = "✅"
    WARNING = "⚠️"
    ERROR   = "🚨"
    TRADE   = "📊"
    PROFIT  = "💚"
    LOSS    = "🔴"
    SIGNAL  = "🎯"


class TelegramNotifier:
    """
    Async Telegram bot for trading alerts.
    Silently skips all messages if no token is configured (dev convenience).

    In semi-auto mode, also manages an Application instance for receiving
    callback queries (button presses) on approval messages.
    """

    def __init__(self) -> None:
        self._bot: Bot | None = None
        self._chat_id = settings.telegram_chat_id
        self._enabled = bool(settings.telegram_bot_token and settings.telegram_chat_id)
        # Tracks approval_id → message_id so we can edit after button press
        self._approval_message_ids: dict[str, int] = {}

        if self._enabled:
            self._bot = Bot(token=settings.telegram_bot_token)
            log.info("telegram.init", status="enabled", chat_id=self._chat_id)
        else:
            log.warning("telegram.init", status="disabled", reason="No token/chat_id in settings")

    # ── Trade Alerts ──────────────────────────────────────────────────────────

    async def trade_entry(
        self,
        symbol:     str,
        direction:  str,
        price:      float,
        quantity:   int,
        stop_loss:  float,
        target_1:   float,
        target_2:   float | None,
        strategy:   str,
        confidence: float,
        broker:     str = "ZERODHA",
    ) -> None:
        rr = (target_1 - price) / (price - stop_loss) if price != stop_loss else 0.0
        t2_line = f"Target 2:   ₹{target_2:.2f}\n" if target_2 else "Target 2:   —\n"
        msg = (
            f"📊 *TRADE ENTRY*\n"
            f"──────────────────\n"
            f"*{direction} {symbol}* @ ₹{price:.2f}\n"
            f"Qty: {quantity} shares\n"
            f"Stop Loss:  ₹{stop_loss:.2f}  (-{abs(price - stop_loss):.2f})\n"
            f"Target 1:   ₹{target_1:.2f}  (+{abs(target_1 - price):.2f})\n"
            + t2_line +
            f"\nR:R Ratio:  {rr:.1f}x\n"
            f"Strategy:   {strategy}\n"
            f"AI Conf:    {confidence:.0%}\n"
            f"Broker:     {broker}\n"
            f"Time:       {datetime.now().strftime('%H:%M:%S IST')}"
        )
        await self._send(msg, parse_mode="Markdown")

    async def trade_filled(
        self,
        symbol:    str,
        direction: str,
        price:     float,
        quantity:  int,
        slippage:  float,
    ) -> None:
        emoji = "✅" if abs(slippage) < 0.05 else "⚠️"
        msg = (
            f"{emoji} *ORDER FILLED*\n"
            f"{direction} {symbol} × {quantity} @ ₹{price:.2f}\n"
            f"Slippage: ₹{slippage:+.2f}"
        )
        await self._send(msg, parse_mode="Markdown")

    async def stop_loss_hit(
        self,
        symbol:      str,
        exit_price:  float,
        entry_price: float,
        quantity:    int,
        pnl:         float,
        r_multiple:  float,
    ) -> None:
        msg = (
            f"🔴 *STOP LOSS HIT*\n"
            f"──────────────────\n"
            f"*{symbol}*\n"
            f"Entry: ₹{entry_price:.2f} → Exit: ₹{exit_price:.2f}\n"
            f"Qty: {quantity}\n"
            f"P&L: ₹{pnl:+,.2f}\n"
            f"R:   {r_multiple:+.2f}R\n"
            f"Time: {datetime.now().strftime('%H:%M:%S IST')}"
        )
        await self._send(msg, parse_mode="Markdown")

    async def target_hit(
        self,
        symbol:      str,
        target_num:  int,
        exit_price:  float,
        entry_price: float,
        quantity:    int,
        pnl:         float,
        r_multiple:  float,
    ) -> None:
        msg = (
            f"💚 *TARGET {target_num} HIT*\n"
            f"──────────────────\n"
            f"*{symbol}*\n"
            f"Entry: ₹{entry_price:.2f} → Exit: ₹{exit_price:.2f}\n"
            f"Qty: {quantity}\n"
            f"P&L: ₹{pnl:+,.2f}\n"
            f"R:   {r_multiple:+.2f}R\n"
            f"Time: {datetime.now().strftime('%H:%M:%S IST')}"
        )
        await self._send(msg, parse_mode="Markdown")

    # ── Semi-auto Approval ────────────────────────────────────────────────────

    async def send_approval_request(self, req: "ApprovalRequest") -> None:
        """
        Send a trade approval message with ✅ Approve / ❌ Reject inline buttons.
        Stores message_id so we can edit it after the user responds.
        """
        if not self._enabled or not self._bot:
            log.debug("telegram.approval_skip", approval_id=req.approval_id)
            return

        direction_emoji = "🟢" if req.direction == "LONG" else "🔴"
        msg = (
            f"🔔 *TRADE APPROVAL REQUIRED*\n"
            f"──────────────────────────\n"
            f"{direction_emoji} *{req.direction} {req.symbol}* @ ₹{req.entry_price:.2f}\n"
            f"Stop:     ₹{req.stop_loss:.2f}  (-{abs(req.entry_price - req.stop_loss):.2f})\n"
            f"Target:   ₹{req.target:.2f}  (+{abs(req.target - req.entry_price):.2f})\n"
            f"Qty: {req.quantity}  |  Risk: ₹{req.risk_inr:.0f}  |  R:R {req.rr_ratio:.1f}x\n"
            f"──────────────────────────\n"
            f"Strategy: {req.strategy}  |  Signal: {req.signal_conf}%\n"
            f"AI ({req.ai_conf:.0%}): _{req.ai_reasoning[:120]}_\n"
            f"──────────────────────────\n"
            f"⏱ Expires in {settings.approval_timeout_secs}s"
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅  Approve", callback_data=f"APPROVE:{req.approval_id}"),
            InlineKeyboardButton("❌  Reject",  callback_data=f"REJECT:{req.approval_id}"),
        ]])

        try:
            sent = await self._bot.send_message(
                chat_id      = self._chat_id,
                text         = msg,
                parse_mode   = "Markdown",
                reply_markup = keyboard,
            )
            self._approval_message_ids[req.approval_id] = sent.message_id
        except TelegramError as e:
            log.error("telegram.approval_send_failed", approval_id=req.approval_id, error=str(e))

    async def send_approval_expired(self, approval_id: str, symbol: str) -> None:
        """Edit the approval message to show it timed out (removes buttons)."""
        if not self._enabled or not self._bot:
            return
        message_id = self._approval_message_ids.pop(approval_id, None)
        if not message_id:
            return
        try:
            await self._bot.edit_message_text(
                chat_id    = self._chat_id,
                message_id = message_id,
                text       = (
                    f"⏰ *APPROVAL EXPIRED* — {symbol}\n"
                    f"Auto-rejected after {settings.approval_timeout_secs}s."
                ),
                parse_mode = "Markdown",
            )
        except TelegramError:
            pass

    async def send_approval_result(
        self,
        approval_id: str,
        symbol:      str,
        approved:    bool,
        user_name:   str = "Unknown",
    ) -> None:
        """Edit the approval message to show the final decision."""
        if not self._enabled or not self._bot:
            return
        message_id = self._approval_message_ids.pop(approval_id, None)
        if not message_id:
            return
        status = "✅ APPROVED" if approved else "❌ REJECTED"
        try:
            await self._bot.edit_message_text(
                chat_id    = self._chat_id,
                message_id = message_id,
                text       = f"{status} — {symbol}\nBy: {user_name} at {datetime.now().strftime('%H:%M:%S IST')}",
                parse_mode = "Markdown",
            )
        except TelegramError:
            pass

    # ── Daily Summary ─────────────────────────────────────────────────────────

    async def daily_summary(
        self,
        trading_date:  str,
        total_trades:  int,
        winning:       int,
        losing:        int,
        net_pnl:       float,
        total_charges: float,
        market_regime: str,
    ) -> None:
        win_rate  = (winning / total_trades * 100) if total_trades else 0
        pnl_emoji = "💚" if net_pnl >= 0 else "🔴"
        msg = (
            f"📅 *DAILY SUMMARY — {trading_date}*\n"
            f"══════════════════════\n"
            f"{pnl_emoji} Net P&L:   ₹{net_pnl:+,.2f}\n"
            f"💸 Charges:  ₹{total_charges:.2f}\n"
            f"──────────────────────\n"
            f"📈 Trades:   {total_trades}  "
            f"(✅ {winning} wins  ❌ {losing} losses)\n"
            f"🎯 Win Rate: {win_rate:.0f}%\n"
            f"🌊 Regime:   {market_regime}\n"
            f"──────────────────────\n"
            f"Capital risk remaining: ₹{settings.daily_loss_limit_inr - abs(min(net_pnl, 0)):,.0f}"
        )
        await self._send(msg, parse_mode="Markdown")

    # ── System Alerts ─────────────────────────────────────────────────────────

    async def send(self, message: str, level: AlertLevel = AlertLevel.INFO) -> None:
        await self._send(f"{level.value} {message}")

    async def kill_switch_activated(self, reason: str) -> None:
        msg = (
            f"🚨 *KILL SWITCH ACTIVATED*\n"
            f"All new orders halted.\n"
            f"Reason: {reason}\n"
            f"Time: {datetime.now().strftime('%H:%M:%S IST')}\n"
            f"Manually resume in dashboard."
        )
        await self._send(msg, parse_mode="Markdown")

    async def system_error(self, component: str, error: str) -> None:
        await self.send(f"System error in *{component}*:\n`{error[:200]}`", AlertLevel.ERROR)

    async def daily_loss_limit_warning(self, current_loss: float, limit: float) -> None:
        pct = current_loss / limit * 100
        msg = (
            f"⚠️ *DAILY LOSS WARNING*\n"
            f"Current loss: ₹{current_loss:,.2f} ({pct:.0f}% of limit)\n"
            f"Limit: ₹{limit:,.2f}"
        )
        await self._send(msg, parse_mode="Markdown")

    async def signal_alert(
        self,
        symbol:     str,
        signal:     str,
        direction:  str,
        confidence: int,
        timeframe:  str,
        price:      float,
        notes:      str,
    ) -> None:
        """Only sent in non-live modes to monitor signal quality."""
        if settings.is_live:
            return
        msg = (
            f"🎯 *SIGNAL [{timeframe}]*\n"
            f"{symbol} — {signal}\n"
            f"Direction: {direction}  |  Conf: {confidence}%\n"
            f"Price: ₹{price:.2f}\n"
            f"{notes}"
        )
        await self._send(msg, parse_mode="Markdown")

    async def market_open(self, regime: str, vix: float | None, briefing: str) -> None:
        vix_str = f"India VIX: {vix:.1f}" if vix else ""
        msg = (
            f"🔔 *MARKET OPEN — {datetime.now().strftime('%d %b %Y')}*\n"
            f"Regime: {regime}  {vix_str}\n"
            f"──────────────────\n"
            f"{briefing[:500]}"
        )
        await self._send(msg, parse_mode="Markdown")

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _send(self, text: str, parse_mode: str | None = None) -> None:
        if not self._enabled or not self._bot:
            log.debug("telegram.skip", message=text[:80])
            return
        try:
            await self._bot.send_message(
                chat_id    = self._chat_id,
                text       = text,
                parse_mode = parse_mode,
            )
        except TelegramError as e:
            log.error("telegram.send_error", error=str(e))
        except Exception as e:
            log.error("telegram.unexpected_error", error=str(e))


# ── Semi-auto: Telegram Application for callback queries ─────────────────────

async def start_approval_polling() -> object | None:
    """
    Start a Telegram Application that receives callback queries (button presses).
    Call during bot startup when APP_ENV=semi-auto.
    Returns the Application instance — pass to stop_approval_polling on shutdown.
    """
    if not settings.telegram_bot_token:
        log.warning("telegram.approval_polling_skip", reason="No bot token configured")
        return None

    from telegram.ext import Application, CallbackQueryHandler
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CallbackQueryHandler(_handle_approval_callback))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=["callback_query"])
    log.info("telegram.approval_polling_started")
    return app


async def stop_approval_polling(app: object) -> None:
    """Gracefully stop the Telegram Application on bot shutdown."""
    if app is None:
        return
    try:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        log.info("telegram.approval_polling_stopped")
    except Exception as e:
        log.warning("telegram.approval_polling_stop_error", error=str(e))


async def _handle_approval_callback(update: object, context: object) -> None:
    """
    Telegram callback handler — fires when user taps ✅ or ❌.
    Validates authorization then resolves the pending approval event.
    """
    from telegram import Update
    from services.execution.approval_gate import resolve_approval

    if not isinstance(update, Update) or not update.callback_query:
        return

    query     = update.callback_query
    user_id   = str(query.from_user.id)
    user_name = query.from_user.full_name or user_id

    await query.answer()

    # Authorization check — empty list = anyone can approve (dev convenience)
    authorized = settings.authorized_telegram_ids
    if authorized and user_id not in authorized:
        log.warning("telegram.approval_unauthorized", user_id=user_id)
        await query.answer("⛔ You are not authorized to approve trades.", show_alert=True)
        return

    data = query.data or ""
    if ":" not in data:
        return

    action, approval_id = data.split(":", 1)
    approved = action == "APPROVE"

    resolved = await resolve_approval(approval_id, approved)
    if resolved:
        notifier = get_notifier()
        # Best-effort: extract symbol from the 4th word of the message text
        words  = (query.message.text or "").split()
        symbol = words[3] if len(words) > 3 else "?"
        await notifier.send_approval_result(approval_id, symbol, approved, user_name)
        log.info("telegram.approval_resolved", action=action, approval_id=approval_id, user=user_name)
    else:
        await query.answer("⏰ This approval request has already expired.", show_alert=True)


# ── Singleton ─────────────────────────────────────────────────────────────────

_notifier: TelegramNotifier | None = None


def get_notifier() -> TelegramNotifier:
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier
