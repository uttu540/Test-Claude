"""
services/ai_strategy/schemas.py
────────────────────────────────
Pydantic models for all Claude AI inputs and outputs.

These are the contracts between the signal pipeline and the AI layer.
Strict validation ensures a malformed Claude response never reaches
the order manager.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ─── Action ───────────────────────────────────────────────────────────────────

class TradeAction(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"
    SKIP = "SKIP"   # Claude sees the signal but advises not to trade


# ─── AI Decision ──────────────────────────────────────────────────────────────

class AIDecision(BaseModel):
    """
    Structured output from Claude for a single signal evaluation.
    Returned by ClaudeStrategyClient.analyse().
    """
    action:      TradeAction = TradeAction.SKIP
    confidence:  float       = Field(default=0.0, ge=0.0, le=1.0)
    reasoning:   str         = ""
    risk_flags:  list[str]   = Field(default_factory=list)

    # Populated by the client — not from Claude's response directly
    model_used:    str = ""
    input_tokens:  int = 0
    output_tokens: int = 0
    latency_ms:    int = 0

    @field_validator("confidence")
    @classmethod
    def round_confidence(cls, v: float) -> float:
        return round(v, 2)

    @property
    def is_actionable(self) -> bool:
        """True if Claude recommends entering a trade with meaningful confidence."""
        return self.action != TradeAction.SKIP and self.confidence >= 0.55

    @classmethod
    def skip(cls, reason: str = "Skipped") -> "AIDecision":
        """Convenience factory for a safe no-action decision."""
        return cls(action=TradeAction.SKIP, confidence=0.0, reasoning=reason)


# ─── Signal Context ───────────────────────────────────────────────────────────

class NewsContext(BaseModel):
    """Recent news items for a symbol, summarised for the prompt."""
    headline:  str
    source:    str = ""
    published: str = ""   # ISO datetime string
    sentiment: float = 0.0  # -1.0 to +1.0


class SignalContext(BaseModel):
    """
    Full context object assembled from signals + indicators + news
    before being sent to Claude.
    """
    # Identity
    symbol:          str
    exchange:        str = "NSE"
    timeframe:       str

    # Signal
    signal_type:     str
    signal_direction: str   # BULLISH / BEARISH
    signal_confidence: int  # 0–100 from technical engine

    # Price
    current_price:   float
    prev_close:      float = 0.0
    change_pct:      float = 0.0

    # Key indicators (subset — not all 30+, just the most meaningful)
    rsi:             float = 0.0
    macd_hist:       float = 0.0
    ema_stack:       int   = 0    # +1 bullish, -1 bearish, 0 mixed
    above_200ema:    bool  = False
    atr_pct:         float = 0.0  # ATR as % of price (volatility)
    rvol:            float = 1.0  # Relative volume
    bb_pct:          float = 0.5  # Position in Bollinger Band (0=lower, 1=upper)
    adx:             float = 0.0  # Trend strength

    # Multi-timeframe summary
    tf_alignment:    dict[str, str] = Field(default_factory=dict)  # {tf: direction}

    # News
    recent_news:     list[NewsContext] = Field(default_factory=list)

    # Market context
    market_regime:   str = "UNKNOWN"
    india_vix:       float | None = None

    def to_prompt_dict(self) -> dict[str, Any]:
        """Serialise to a clean dict for prompt rendering."""
        return {
            "symbol":       self.symbol,
            "timeframe":    self.timeframe,
            "signal":       self.signal_type,
            "direction":    self.signal_direction,
            "confidence":   self.signal_confidence,
            "price":        round(self.current_price, 2),
            "change_pct":   round(self.change_pct, 2),
            "indicators": {
                "rsi":         round(self.rsi, 1),
                "macd_hist":   round(self.macd_hist, 3),
                "ema_stack":   self.ema_stack,
                "above_200ema": self.above_200ema,
                "atr_pct":     round(self.atr_pct, 2),
                "rvol":        round(self.rvol, 1),
                "bb_pct":      round(self.bb_pct, 2),
                "adx":         round(self.adx, 1),
            },
            "timeframe_alignment": self.tf_alignment,
            "market_regime": self.market_regime,
            "india_vix":     self.india_vix,
            "recent_news":   [
                {
                    "headline":  n.headline,
                    "source":    n.source,
                    "published": n.published,
                    "sentiment": n.sentiment,
                }
                for n in self.recent_news[:5]   # Cap at 5 headlines
            ],
        }
