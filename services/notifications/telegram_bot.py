"""
services/notifications/telegram_bot.py
────────────────────────────────────────
Telegram notification service.

Sends real-time alerts for:
  - Trade entries and exits
  - Stop loss triggers
  - Target hits
  - Daily P&L summary
  - System errors and kill switch events
  - Signal alerts (dev/paper mode)
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from decimal import Decimal
from enum import Enum

import structlog
from telegram import Bot
from telegram.error import TelegramError

from config.settings import settings

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
    Silently skips if no token is configured (dev convenience).
    """

    def __init__(self):
        self._bot: Bot | None = None
        self._chat_id = settings.telegram_chat_id
        self._enabled = bool(settings.telegram_bot_token and settings.telegram_chat_id)

        if self._enabled:
            self._bot = Bot(token=settings.telegram_bot_token)
            log.info("telegram.init", status="enabled", chat_id=self._chat_id)
        else:
            log.warning("telegram.init", status="disabled", reason="No token/chat_id in settings")

    async def send(self, message: str, level: AlertLevel = AlertLevel.INFO) -> None:
        """Send a raw message. Adds level emoji prefix."""
        await self._send(f"{level.value} {message}")

    # ── Trade Alerts ──────────────────────────────────────────────────────────

    async def trade_entry(
        self,
        symbol:       str,
        direction:    str,        # BUY / SELL
        price:        float,
        quantity:     int,
        stop_loss:    float,
        target_1:     float,
        target_2:     float | None,
        strategy:     str,
        confidence:   float,
        broker:       str = "ZERODHA",
    ) -> None:
        if price == stop_loss:
            log.warning("telegram.rr_zero_division", symbol=symbol, price=price, stop_loss=stop_loss)
        rr = (target_1 - price) / (price - stop_loss) if price != stop_loss else 0.0
        msg = (
            f"📊 *TRADE ENTRY*\n"
            f"──────────────────\n"
            f"*{direction} {symbol}* @ ₹{price:.2f}\n"
            f"Qty: {quantity} shares\n"
            f"Stop Loss:  ₹{stop_loss:.2f}  (-{abs(price - stop_loss):.2f})\n"
            f"Target 1:   ₹{target_1:.2f}  (+{abs(target_1 - price):.2f})\n"
            f"Target 2:   ₹{target_2:.2f}" if target_2 else f"Target 2:   —\n"
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

    # ── Daily Summary ─────────────────────────────────────────────────────────

    async def daily_summary(
        self,
        trading_date:   str,
        total_trades:   int,
        winning:        int,
        losing:         int,
        net_pnl:        float,
        total_charges:  float,
        market_regime:  str,
    ) -> None:
        win_rate = (winning / total_trades * 100) if total_trades else 0
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
            f"Capital risk remaining: "
            f"₹{settings.daily_loss_limit_inr - abs(min(net_pnl, 0)):,.0f}"
        )
        await self._send(msg, parse_mode="Markdown")

    # ── System Alerts ─────────────────────────────────────────────────────────

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
        """Only sent in dev/paper mode to monitor signal quality."""
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


# ─── Singleton ────────────────────────────────────────────────────────────────

_notifier: TelegramNotifier | None = None


def get_notifier() -> TelegramNotifier:
    global _notifier
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier
