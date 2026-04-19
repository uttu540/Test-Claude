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
        self._chat_ids: list[str] = settings.notification_chat_ids
        self._enabled = bool(settings.telegram_bot_token and self._chat_ids)
        # Tracks approval_id → (message_id, symbol) so we can edit after button press
        self._approval_message_ids: dict[str, tuple[int, str]] = {}

        if self._enabled:
            self._bot = Bot(token=settings.telegram_bot_token)
            log.info("telegram.init", status="enabled", chat_ids=self._chat_ids)
        else:
            log.warning("telegram.init", status="disabled",
                        reason="No token or no chat IDs configured")

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

        # Send to all chats; store message_id from first chat for later editing
        for chat_id in self._chat_ids:
            try:
                sent = await self._bot.send_message(
                    chat_id      = chat_id,
                    text         = msg,
                    parse_mode   = "Markdown",
                    reply_markup = keyboard,
                )
                if req.approval_id not in self._approval_message_ids:
                    self._approval_message_ids[req.approval_id] = (sent.message_id, req.symbol)
            except TelegramError as e:
                log.error("telegram.approval_send_failed",
                          approval_id=req.approval_id, chat_id=chat_id, error=str(e))

    async def send_approval_expired(self, approval_id: str, symbol: str) -> None:
        """Edit the approval message to show it timed out (removes buttons)."""
        if not self._enabled or not self._bot:
            return
        entry = self._approval_message_ids.pop(approval_id, None)
        if not entry:
            return
        message_id, _ = entry
        chat_id = self._chat_ids[0] if self._chat_ids else None
        if not chat_id:
            return
        try:
            await self._bot.edit_message_text(
                chat_id    = chat_id,
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
        entry = self._approval_message_ids.pop(approval_id, None)
        if not entry:
            return
        message_id, _ = entry
        chat_id = self._chat_ids[0] if self._chat_ids else None
        if not chat_id:
            return
        status = "✅ APPROVED" if approved else "❌ REJECTED"
        try:
            await self._bot.edit_message_text(
                chat_id    = chat_id,
                message_id = message_id,
                text       = f"{status} — {symbol}\nBy: {user_name} at {datetime.now().strftime('%H:%M:%S IST')}",
                parse_mode = "Markdown",
            )
        except TelegramError:
            pass

    def _get_approval_symbol(self, approval_id: str) -> str:
        """Return the symbol stored for this approval_id, or '?' if not found."""
        entry = self._approval_message_ids.get(approval_id)
        return entry[1] if entry else "?"

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
        for chat_id in self._chat_ids:
            try:
                await self._bot.send_message(
                    chat_id    = chat_id,
                    text       = text,
                    parse_mode = parse_mode,
                )
            except TelegramError as e:
                log.error("telegram.send_error", chat_id=chat_id, error=str(e))
            except Exception as e:
                log.error("telegram.unexpected_error", chat_id=chat_id, error=str(e))


# ── Telegram Application: commands + callback queries ────────────────────────

def _is_authorized(user_id: str) -> bool:
    """True if the user is allowed to use bot commands / approve trades."""
    authorized = settings.authorized_telegram_ids
    return not authorized or user_id in authorized


async def _cmd_help(update: object, context: object) -> None:
    """Handle /start and /help — show available commands."""
    from telegram import Update
    if not isinstance(update, Update) or not update.message:
        return
    if not _is_authorized(str(update.effective_user.id)):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return
    await update.message.reply_text(
        "🤖 *TradeBot Commands*\n"
        "──────────────────\n"
        "/status     — Bot mode, capital, regime\n"
        "/pnl        — Today's P&L summary\n"
        "/positions  — Open positions\n"
        "/help       — Show this message",
        parse_mode="Markdown",
    )


async def _cmd_status(update: object, context: object) -> None:
    """Handle /status — bot mode, capital, market regime."""
    from telegram import Update
    if not isinstance(update, Update) or not update.message:
        return
    if not _is_authorized(str(update.effective_user.id)):
        return
    try:
        from database.connection import get_redis
        redis  = get_redis()
        regime = await redis.get("market:regime") or "UNKNOWN"
    except Exception:
        regime = "UNKNOWN"

    mode = settings.app_env.value.upper()
    await update.message.reply_text(
        f"🤖 *TradeBot Status*\n"
        f"──────────────────\n"
        f"Mode:             `{mode}`\n"
        f"Capital:          ₹{settings.total_capital:,.0f}\n"
        f"Regime:           {regime}\n"
        f"Max risk/trade:   ₹{settings.max_risk_per_trade_inr:,.0f}\n"
        f"Daily loss limit: ₹{settings.daily_loss_limit_inr:,.0f}\n"
        f"Max positions:    {settings.max_open_positions}",
        parse_mode="Markdown",
    )


async def _cmd_pnl(update: object, context: object) -> None:
    """Handle /pnl — today's P&L summary from the database."""
    from telegram import Update
    if not isinstance(update, Update) or not update.message:
        return
    if not _is_authorized(str(update.effective_user.id)):
        return
    try:
        from datetime import date as _date
        from sqlalchemy import text
        from database.connection import get_db_session
        today = _date.today()
        async with get_db_session() as session:
            result = await session.execute(
                text("""
                    SELECT
                        COUNT(*)                                          AS total,
                        COUNT(*) FILTER (WHERE net_pnl > 0)              AS wins,
                        COUNT(*) FILTER (WHERE status = 'OPEN')          AS open_count,
                        COALESCE(SUM(net_pnl) FILTER (WHERE status='CLOSED'), 0) AS net_pnl
                    FROM trades WHERE DATE(entry_time) = :today
                """),
                {"today": today},
            )
            row = result.fetchone()
            total    = row.total or 0
            wins     = row.wins or 0
            open_cnt = row.open_count or 0
            net_pnl  = float(row.net_pnl or 0)
            win_rate = round(wins / total * 100) if total else 0
            pnl_emoji = "💚" if net_pnl >= 0 else "🔴"
            sign = "+" if net_pnl >= 0 else ""
            await update.message.reply_text(
                f"📅 *Today's P&L — {today.strftime('%d %b')}*\n"
                f"──────────────────\n"
                f"{pnl_emoji} Net P&L:   {sign}₹{abs(net_pnl):,.2f}\n"
                f"📊 Trades:   {total}  (✅ {wins}W  ❌ {total - wins - open_cnt}L  🟡 {open_cnt} open)\n"
                f"🎯 Win rate: {win_rate}%",
                parse_mode="Markdown",
            )
            return
    except Exception as e:
        await update.message.reply_text(f"⚠️ Could not fetch P&L: {e}")


async def _cmd_positions(update: object, context: object) -> None:
    """Handle /positions — list all open trades."""
    from telegram import Update
    if not isinstance(update, Update) or not update.message:
        return
    if not _is_authorized(str(update.effective_user.id)):
        return
    try:
        from sqlalchemy import text
        from database.connection import get_db_session
        async with get_db_session() as session:
            result = await session.execute(
                text("""
                    SELECT trading_symbol, direction, entry_price,
                           planned_stop_loss, planned_target_1, entry_time
                    FROM trades WHERE status = 'OPEN'
                    ORDER BY entry_time DESC
                """)
            )
            rows = result.fetchall()
            if not rows:
                await update.message.reply_text("📭 No open positions.")
                return
            lines = ["📊 *Open Positions*\n──────────────────"]
            for r in rows:
                arrow = "▲" if r.direction == "LONG" else "▼"
                lines.append(
                    f"{arrow} *{r.trading_symbol}* @ ₹{float(r.entry_price):.2f}\n"
                    f"   SL ₹{float(r.planned_stop_loss):.2f}  →  T ₹{float(r.planned_target_1):.2f}"
                )
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
            return
    except Exception as e:
        await update.message.reply_text(f"⚠️ Could not fetch positions: {e}")


async def start_telegram_polling() -> object | None:
    """
    Start a Telegram Application that handles:
      - Callback queries (✅/❌ approval buttons — semi-auto mode)
      - Bot commands (/status, /pnl, /positions, /help)

    Call during bot startup whenever a bot token is configured.
    Returns the Application instance — pass to stop_telegram_polling on shutdown.
    """
    if not settings.telegram_bot_token:
        log.warning("telegram.polling_skip", reason="No bot token configured")
        return None

    from telegram.ext import Application, CallbackQueryHandler, CommandHandler
    app = Application.builder().token(settings.telegram_bot_token).build()

    # Commands — available in all modes
    app.add_handler(CommandHandler("start",     _cmd_help))
    app.add_handler(CommandHandler("help",      _cmd_help))
    app.add_handler(CommandHandler("status",    _cmd_status))
    app.add_handler(CommandHandler("pnl",       _cmd_pnl))
    app.add_handler(CommandHandler("positions", _cmd_positions))

    # Inline button callbacks — semi-auto trade approvals
    app.add_handler(CallbackQueryHandler(_handle_approval_callback))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=["message", "callback_query"])
    log.info("telegram.polling_started",
             commands=["/status", "/pnl", "/positions", "/help"],
             semi_auto=settings.is_semi_auto)
    return app


# Keep old name as alias so existing imports don't break
start_approval_polling = start_telegram_polling


async def stop_telegram_polling(app: object) -> None:
    """Gracefully stop the Telegram Application on bot shutdown."""
    if app is None:
        return
    try:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        log.info("telegram.polling_stopped")
    except Exception as e:
        log.warning("telegram.polling_stop_error", error=str(e))


# Keep old name as alias
stop_approval_polling = stop_telegram_polling


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
        symbol = notifier._get_approval_symbol(approval_id)
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
