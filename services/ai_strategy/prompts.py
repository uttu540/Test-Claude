"""
services/ai_strategy/prompts.py
────────────────────────────────
All Claude prompt templates, versioned here in one place.

Design principles:
  - System prompt establishes Claude's role and strict JSON output contract
  - User prompt is assembled dynamically from the SignalContext
  - Output format is explicit — Claude must follow it for Pydantic validation to pass
  - Prompts are kept focused: Claude reasons about ONE signal at a time
"""
from __future__ import annotations

from services.ai_strategy.schemas import SignalContext

# ─── System Prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a senior quantitative trader and risk manager for an algorithmic trading system operating on the NSE (National Stock Exchange of India).

Your role is to evaluate a technical trading signal and decide whether to act on it, considering:
- Technical indicator alignment across multiple timeframes
- Current market regime and volatility conditions
- Recent news sentiment for the stock
- Risk/reward quality of the setup

## Your output MUST be valid JSON and nothing else. No markdown, no explanation outside the JSON.

Required JSON schema:
{
  "action": "BUY" | "SELL" | "SKIP",
  "confidence": <float 0.0 to 1.0>,
  "reasoning": "<2-3 sentences explaining the decision>",
  "risk_flags": ["<flag1>", "<flag2>"]   // empty list if none
}

## Rules
- action = BUY for bullish signals, SELL for bearish/short signals, SKIP to pass on the trade
- confidence must reflect genuine conviction — do not be overconfident on weak setups
- confidence > 0.75 only when multiple strong factors align
- risk_flags are brief warnings: e.g. "RSI overbought", "Earnings next week", "Low volume", "Broad market weak"
- If you are uncertain or data is insufficient, return SKIP with confidence 0.0
- Never recommend a trade that violates basic risk management (e.g. chasing a 5%+ move already made)
- This is an intraday/swing system with ₹1,00,000 capital and max ₹2,000 risk per trade

## Indian Market Context
- NSE trading hours: 9:15 AM – 3:30 PM IST
- India VIX > 20 = high volatility, reduce position sizing mentally
- FII activity and global cues (SGX Nifty, US futures) drive gap-up/gap-down opens
- Avoid trading in the first 15 minutes unless signal is exceptionally strong"""


# ─── User Prompt Builder ──────────────────────────────────────────────────────

def build_signal_prompt(ctx: SignalContext) -> str:
    """
    Build the user-turn prompt from a SignalContext.
    Renders a structured, readable breakdown for Claude to analyse.
    """
    d = ctx.to_prompt_dict()

    # ── Indicator block ───────────────────────────────────────────────────────
    ind = d["indicators"]
    ema_label = {1: "Bullish (fast > mid > slow)", -1: "Bearish (fast < mid < slow)", 0: "Mixed"}.get(ind["ema_stack"], "Unknown")
    above_ema_label = "Yes" if ind["above_200ema"] else "No"

    # ── Timeframe alignment block ─────────────────────────────────────────────
    tf_lines = "\n".join(
        f"  {tf}: {direction}"
        for tf, direction in (d["timeframe_alignment"] or {}).items()
    ) or "  No multi-timeframe data available"

    # ── News block ────────────────────────────────────────────────────────────
    news = d.get("recent_news", [])
    if news:
        news_lines = "\n".join(
            f"  [{n['source']}] {n['headline']} (sentiment: {n['sentiment']:+.1f})"
            for n in news
        )
    else:
        news_lines = "  No recent news found"

    # ── VIX block ─────────────────────────────────────────────────────────────
    vix_str = f"{d['india_vix']:.1f}" if d["india_vix"] else "N/A"

    return f"""Evaluate this trading signal and return your decision as JSON.

## Signal
- Symbol:     {d['symbol']} (NSE)
- Timeframe:  {d['timeframe']}
- Signal:     {d['signal']}
- Direction:  {d['direction']}
- Technical Confidence: {d['confidence']}/100

## Price
- Current:    ₹{d['price']:.2f}
- Change:     {d['change_pct']:+.2f}% from prev close

## Technical Indicators
- RSI (14):        {ind['rsi']:.1f}  {"⚠ Overbought" if ind['rsi'] > 70 else "⚠ Oversold" if ind['rsi'] < 30 else "✓ Neutral"}
- MACD Histogram:  {ind['macd_hist']:+.3f}  {"↑ Positive" if ind['macd_hist'] > 0 else "↓ Negative"}
- EMA Stack:       {ema_label}
- Above 200 EMA:   {above_ema_label}
- ATR% (volatility): {ind['atr_pct']:.2f}%  {"⚠ High vol" if ind['atr_pct'] > 3 else "✓ Normal"}
- Relative Volume: {ind['rvol']:.1f}x  {"✓ High conviction" if ind['rvol'] > 1.5 else "⚠ Low volume"}
- BB Position:     {ind['bb_pct']:.0%} (0%=lower band, 100%=upper band)
- ADX:             {ind['adx']:.1f}  {"✓ Trending" if ind['adx'] > 25 else "⚠ Weak trend"}

## Multi-Timeframe Alignment
{tf_lines}

## Market Context
- Regime:     {d['market_regime']}
- India VIX:  {vix_str}

## Recent News (last 4 hours)
{news_lines}

Respond with JSON only."""


# ─── Market Briefing Prompt ───────────────────────────────────────────────────

MARKET_BRIEFING_SYSTEM = """You are a concise market analyst for an Indian equity trader.
Summarise market conditions in 3-4 sentences. Focus on: overall trend, key risks today, and sectors to watch.
Return plain text only — no markdown, no headers."""


def build_market_briefing_prompt(
    nifty_change_pct: float,
    vix: float | None,
    advance_decline: str,
    fii_activity: str,
    top_movers: list[str],
) -> str:
    vix_str = f"{vix:.1f}" if vix else "N/A"
    movers_str = ", ".join(top_movers[:5]) if top_movers else "N/A"
    return (
        f"Nifty 50 change: {nifty_change_pct:+.2f}%\n"
        f"India VIX: {vix_str}\n"
        f"Advance/Decline: {advance_decline}\n"
        f"FII Activity: {fii_activity}\n"
        f"Top movers: {movers_str}\n\n"
        f"Give a brief market briefing for the trading session ahead."
    )
