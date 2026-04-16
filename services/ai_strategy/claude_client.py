"""
services/ai_strategy/claude_client.py
───────────────────────────────────────
ClaudeStrategyClient — the AI brain of the trading bot.

Responsibilities:
  1. Receives a Signal + market context
  2. Assembles a structured SignalContext (indicators, news, regime)
  3. Calls Claude API with the signal prompt
  4. Parses and validates the JSON response into an AIDecision
  5. Logs every decision to the AIDecisionLog table for SEBI audit trail
  6. Returns AIDecision.skip() on any failure — never crashes the trade pipeline

Cost guard: skips the Claude call if signal confidence < 50 (weak signals
not worth the API cost or the latency).

Claude is NOT in the execution path — it only produces a decision object.
The RiskEngine and OrderManager execute independently.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime

import structlog
from anthropic import AsyncAnthropic, APIError, APITimeoutError
from tenacity import retry, stop_after_attempt, wait_exponential

from config.settings import settings
from database.connection import get_db_session, get_redis
from database.models import AIDecisionLog
from services.ai_strategy.prompts import (
    MARKET_BRIEFING_SYSTEM,
    SYSTEM_PROMPT,
    build_market_briefing_prompt,
    build_signal_prompt,
)
from services.ai_strategy.schemas import (
    AIDecision,
    NewsContext,
    SignalContext,
    TradeAction,
)
from services.data_ingestion.news_feed import get_news_service
from services.technical_engine.signal_generator import Direction, Signal

log = structlog.get_logger(__name__)

# Minimum technical confidence to justify a Claude API call
CONFIDENCE_THRESHOLD = 50


class ClaudeStrategyClient:
    """
    Wraps the Anthropic async client with trading-specific logic.
    Safe to instantiate once and reuse across the session.
    """

    def __init__(self) -> None:
        if not settings.anthropic_api_key:
            log.warning("claude_client.no_api_key", status="AI analysis disabled")
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key) if settings.anthropic_api_key else None

    # ── Public interface ──────────────────────────────────────────────────────

    async def analyse(self, signal: Signal) -> AIDecision:
        """
        Evaluate a signal and return an AIDecision.
        Never raises — returns AIDecision.skip() on any error.
        """
        if not self._client:
            return AIDecision.skip("Claude API key not configured")

        # Cost guard: skip weak signals
        if signal.confidence < CONFIDENCE_THRESHOLD:
            log.debug(
                "claude_client.skip_weak_signal",
                symbol=signal.trading_symbol,
                confidence=signal.confidence,
                threshold=CONFIDENCE_THRESHOLD,
            )
            return AIDecision.skip(f"Signal confidence {signal.confidence} below threshold {CONFIDENCE_THRESHOLD}")

        try:
            ctx      = await self._build_context(signal)
            decision = await self._call_claude(ctx)
            await self._log_decision(signal, ctx, decision)

            log.info(
                "claude_client.decision",
                symbol     = signal.trading_symbol,
                action     = decision.action.value,
                confidence = decision.confidence,
                flags      = decision.risk_flags,
            )
            return decision

        except Exception as e:
            log.error("claude_client.analyse_error", symbol=signal.trading_symbol, error=str(e))
            return AIDecision.skip(f"Analysis error: {e}")

    # ── Context assembly ──────────────────────────────────────────────────────

    async def _build_context(self, signal: Signal) -> SignalContext:
        """
        Assemble a SignalContext from the signal + Redis state + news DB.
        """
        ind = signal.indicators

        # Fetch live tick data from Redis for extra context
        redis     = get_redis()
        tick_raw  = await redis.get(f"market:tick:{signal.trading_symbol}")
        tick_data = json.loads(tick_raw) if tick_raw else {}

        regime    = await redis.get("market:regime") or "UNKNOWN"
        vix_raw   = await redis.get("market:tick:INDIA VIX")
        vix_data  = json.loads(vix_raw) if vix_raw else {}
        india_vix = vix_data.get("lp")

        # Multi-timeframe direction from Redis signal cache
        tf_alignment = await self._get_tf_alignment(signal.trading_symbol)

        # Recent news from DB
        news_service = get_news_service()
        raw_news     = await news_service.get_recent_news(signal.trading_symbol, hours=4)
        news_items   = [
            NewsContext(
                headline  = n["headline"],
                source    = n["source"],
                published = n["published"],
                sentiment = n["sentiment"],
            )
            for n in raw_news
        ]

        prev_close = tick_data.get("c", signal.price_at_signal)
        change_pct = ((signal.price_at_signal - prev_close) / prev_close * 100) if prev_close else 0.0

        return SignalContext(
            symbol             = signal.trading_symbol,
            timeframe          = signal.timeframe,
            signal_type        = signal.signal_type.value,
            signal_direction   = signal.direction.value,
            signal_confidence  = signal.confidence,
            current_price      = signal.price_at_signal,
            prev_close         = prev_close,
            change_pct         = round(change_pct, 2),
            rsi                = ind.get("rsi_14", 0.0),
            macd_hist          = ind.get("macd_hist", 0.0),
            ema_stack          = int(ind.get("ema_stack", 0)),
            above_200ema       = bool(ind.get("above_200ema", False)),
            atr_pct            = ind.get("atr_pct", 0.0),
            rvol               = ind.get("rvol", 1.0),
            bb_pct             = ind.get("bb_pct", 0.5),
            adx                = ind.get("adx", 0.0),
            tf_alignment       = tf_alignment,
            recent_news        = news_items,
            market_regime      = regime,
            india_vix          = india_vix,
        )

    async def _get_tf_alignment(self, symbol: str) -> dict[str, str]:
        """
        Read the most recent signal for each timeframe from Redis
        to build a multi-timeframe direction summary.
        """
        redis   = get_redis()
        result  = {}
        for tf in ["1day", "1hr", "15min", "5min"]:
            raw = await redis.get(f"signal:latest:{symbol}:{tf}")
            if raw:
                try:
                    data = json.loads(raw)
                    result[tf] = data.get("direction", "UNKNOWN")
                except Exception:
                    pass
        return result

    # ── Claude API call ───────────────────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=2, max=6),
        reraise=False,
    )
    async def _call_claude(self, ctx: SignalContext) -> AIDecision:
        """
        Call Claude API and parse the JSON response.
        Returns AIDecision.skip() if response is malformed.
        """
        user_prompt = build_signal_prompt(ctx)
        start_ms    = int(time.time() * 1000)

        try:
            response = await self._client.messages.create(
                model      = settings.claude_model,
                max_tokens = 512,
                system     = SYSTEM_PROMPT,
                messages   = [{"role": "user", "content": user_prompt}],
            )
        except APITimeoutError:
            log.warning("claude_client.timeout", symbol=ctx.symbol)
            return AIDecision.skip("Claude API timeout")
        except APIError as e:
            log.error("claude_client.api_error", symbol=ctx.symbol, error=str(e))
            return AIDecision.skip(f"Claude API error: {e}")

        latency_ms    = int(time.time() * 1000) - start_ms
        raw_text      = response.content[0].text.strip()
        input_tokens  = response.usage.input_tokens
        output_tokens = response.usage.output_tokens

        decision = self._parse_response(raw_text, ctx.symbol)
        decision.model_used    = settings.claude_model
        decision.input_tokens  = input_tokens
        decision.output_tokens = output_tokens
        decision.latency_ms    = latency_ms

        log.debug(
            "claude_client.api_call",
            symbol        = ctx.symbol,
            latency_ms    = latency_ms,
            input_tokens  = input_tokens,
            output_tokens = output_tokens,
        )
        return decision

    def _parse_response(self, raw: str, symbol: str) -> AIDecision:
        """
        Parse Claude's JSON response into an AIDecision.
        Returns AIDecision.skip() on any parse or validation error.
        """
        # Strip markdown code fences if present (Claude sometimes adds them)
        text = raw.strip()
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:])
        if text.endswith("```"):
            text = "\n".join(text.split("\n")[:-1])
        text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("claude_client.invalid_json", symbol=symbol, raw=raw[:200])
            return AIDecision.skip("Invalid JSON response from Claude")

        try:
            action = TradeAction(data.get("action", "SKIP").upper())
        except ValueError:
            log.warning("claude_client.invalid_action", symbol=symbol, action=data.get("action"))
            return AIDecision.skip("Invalid action in Claude response")

        try:
            confidence = float(data.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.0

        return AIDecision(
            action     = action,
            confidence = confidence,
            reasoning  = str(data.get("reasoning", ""))[:1000],
            risk_flags = [str(f) for f in data.get("risk_flags", []) if f][:10],
        )

    # ── Audit log ─────────────────────────────────────────────────────────────

    async def _log_decision(
        self,
        signal:   Signal,
        ctx:      SignalContext,
        decision: AIDecision,
    ) -> None:
        """
        Persist the full AI decision to AIDecisionLog table.
        Best-effort — never raises.
        """
        try:
            async for session in get_db_session():
                entry = AIDecisionLog(
                    id             = uuid.uuid4(),
                    decision_type  = "STRATEGY_EVAL",
                    input_context  = ctx.to_prompt_dict(),
                    raw_response   = json.dumps({
                        "action":     decision.action.value,
                        "confidence": decision.confidence,
                        "reasoning":  decision.reasoning,
                        "risk_flags": decision.risk_flags,
                    }),
                    parsed_output  = {
                        "action":     decision.action.value,
                        "confidence": decision.confidence,
                        "reasoning":  decision.reasoning,
                        "risk_flags": decision.risk_flags,
                    },
                    model_used     = decision.model_used,
                    input_tokens   = decision.input_tokens,
                    output_tokens  = decision.output_tokens,
                    latency_ms     = decision.latency_ms,
                    created_at     = datetime.now(),
                )
                try:
                    session.add(entry)
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise
        except Exception as e:
            log.error("claude_client.log_error", error=str(e))

    # ── Market briefing ───────────────────────────────────────────────────────

    async def get_market_briefing(
        self,
        nifty_change_pct: float,
        vix: float | None,
        regime: str,
        news_headlines: list[str],
        advance_decline: str = "N/A",
        fii_activity: str = "N/A",
        top_movers: list[str] | None = None,
    ) -> str:
        """
        Call Claude for a pre-market briefing.
        Returns a plain-text summary (3-4 sentences).
        Falls back to a canned string on any error — never raises.
        """
        if not self._client:
            return f"Market opens. Regime: {regime}. VIX: {vix or 'N/A'}."

        user_prompt = build_market_briefing_prompt(
            nifty_change_pct = nifty_change_pct,
            vix              = vix,
            regime           = regime,
            news_headlines   = news_headlines,
            advance_decline  = advance_decline,
            fii_activity     = fii_activity,
            top_movers       = top_movers or [],
        )

        try:
            response = await self._client.messages.create(
                model      = settings.claude_model,
                max_tokens = 256,
                system     = MARKET_BRIEFING_SYSTEM,
                messages   = [{"role": "user", "content": user_prompt}],
            )
            briefing = response.content[0].text.strip()
            log.info(
                "claude_client.briefing_done",
                tokens_in  = response.usage.input_tokens,
                tokens_out = response.usage.output_tokens,
            )
            return briefing
        except Exception as e:
            log.error("claude_client.briefing_error", error=str(e))
            return f"Market opens. Regime: {regime}. VIX: {vix or 'N/A'}."


# ─── Singleton ────────────────────────────────────────────────────────────────

_client: ClaudeStrategyClient | None = None


def get_claude_client() -> ClaudeStrategyClient:
    global _client
    if _client is None:
        _client = ClaudeStrategyClient()
    return _client
