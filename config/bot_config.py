"""
config/bot_config.py
────────────────────
Runtime-configurable bot parameters stored in Redis.
All values have hardcoded defaults — changes made via the dashboard
take effect on the next signal cycle with no restart needed.
"""
from __future__ import annotations

import json
import structlog

log = structlog.get_logger(__name__)

REDIS_KEY = "config:bot"

# Schema: every tunable parameter with type, default, and display metadata.
# Groups: execution | strategies | indicators | timeframes | regime_caps | regime_signals
CONFIG_SCHEMA: dict[str, dict] = {
    # ── Execution ─────────────────────────────────────────────────────────────
    "confidence_threshold": {
        "default": 65, "type": "int", "min": 40, "max": 100, "step": 1,
        "label": "Confidence Threshold",
        "desc": "Minimum signal confidence (0–100) required to place a trade.",
        "group": "execution",
    },
    "signal_min_confidence": {
        "default": 40, "type": "int", "min": 20, "max": 80, "step": 1,
        "label": "Signal Minimum Confidence",
        "desc": "Signals below this are dropped before regime filtering.",
        "group": "execution",
    },
    # ── Strategy on/off ───────────────────────────────────────────────────────
    "strategy_breakout": {
        "default": True, "type": "bool",
        "label": "Breakout", "desc": "20-period high/low breakout with volume.",
        "group": "strategies",
    },
    "strategy_ema": {
        "default": True, "type": "bool",
        "label": "EMA Crossover", "desc": "Fast EMA crossing slow EMA.",
        "group": "strategies",
    },
    "strategy_momentum": {
        "default": True, "type": "bool",
        "label": "Momentum (RSI + MACD)", "desc": "RSI extremes and MACD signal crosses.",
        "group": "strategies",
    },
    "strategy_volume": {
        "default": True, "type": "bool",
        "label": "High RVOL", "desc": "Volume spike above 2× 20-day average.",
        "group": "strategies",
    },
    "strategy_volatility": {
        "default": True, "type": "bool",
        "label": "Bollinger Bands", "desc": "BB squeeze detection and expansion breakout.",
        "group": "strategies",
    },
    "strategy_orb": {
        "default": True, "type": "bool",
        "label": "ORB (Opening Range Breakout)", "desc": "9:15–9:30 AM range break, valid until 1 PM.",
        "group": "strategies",
    },
    "strategy_vwap": {
        "default": True, "type": "bool",
        "label": "VWAP Reclaim / Rejection", "desc": "Price crossing VWAP with volume confirmation.",
        "group": "strategies",
    },
    # ── Per-signal minimum confidence overrides ────────────────────────────────
    # Backtest finding (90d, Nifty 50, Apr 2026): ORB WR=38%, VWAP WR=39% at
    # default thresholds. Raising their floors to 70+ filters low-quality setups.
    "orb_min_confidence": {
        "default": 70, "type": "int", "min": 50, "max": 100, "step": 5,
        "label": "ORB Min Confidence",
        "desc": "ORB signals below this are dropped regardless of global threshold. "
                "Backtest WR was 38% at default — raised to 70 to filter weak setups.",
        "group": "strategies",
    },
    "vwap_min_confidence": {
        "default": 70, "type": "int", "min": 50, "max": 100, "step": 5,
        "label": "VWAP Min Confidence",
        "desc": "VWAP_RECLAIM signals below this are dropped. "
                "Backtest WR was 39% at default — raised to 70 to require stronger confirmation.",
        "group": "strategies",
    },
    # ── EMA periods ───────────────────────────────────────────────────────────
    "ema_fast":  {"default": 8,   "type": "int", "min": 3,  "max": 50,  "step": 1, "label": "EMA Fast",        "group": "indicators"},
    "ema_mid":   {"default": 33,  "type": "int", "min": 5,  "max": 100, "step": 1, "label": "EMA Mid",         "group": "indicators"},
    "ema_slow":  {"default": 50,  "type": "int", "min": 10, "max": 200, "step": 1, "label": "EMA Slow",        "group": "indicators"},
    "ema_trend": {"default": 200, "type": "int", "min": 50, "max": 500, "step": 1, "label": "EMA Trend (200)", "group": "indicators"},
    # ── Momentum ──────────────────────────────────────────────────────────────
    "rsi_period":         {"default": 14, "type": "int", "min": 5,  "max": 50, "step": 1, "label": "RSI Period",   "group": "indicators"},
    "macd_fast":          {"default": 12, "type": "int", "min": 5,  "max": 30, "step": 1, "label": "MACD Fast",    "group": "indicators"},
    "macd_slow":          {"default": 26, "type": "int", "min": 10, "max": 60, "step": 1, "label": "MACD Slow",    "group": "indicators"},
    "macd_signal_period": {"default": 9,  "type": "int", "min": 3,  "max": 20, "step": 1, "label": "MACD Signal",  "group": "indicators"},
    # ── Volatility ────────────────────────────────────────────────────────────
    "bb_period": {"default": 20,  "type": "int",   "min": 5,   "max": 50, "step": 1,   "label": "BB Period",   "group": "indicators"},
    "bb_std":    {"default": 2.0, "type": "float", "min": 1.0, "max": 4.0,"step": 0.1, "label": "BB Std Dev",  "group": "indicators"},
    "atr_period":{"default": 14,  "type": "int",   "min": 5,   "max": 50, "step": 1,   "label": "ATR Period",  "group": "indicators"},
    # ── Timeframe confluence weights ──────────────────────────────────────────
    "tw_1min":  {"default": 0.5, "type": "float", "min": 0.0, "max": 5.0, "step": 0.5, "label": "1min",  "group": "timeframes"},
    "tw_5min":  {"default": 1.0, "type": "float", "min": 0.0, "max": 5.0, "step": 0.5, "label": "5min",  "group": "timeframes"},
    "tw_15min": {"default": 1.5, "type": "float", "min": 0.0, "max": 5.0, "step": 0.5, "label": "15min", "group": "timeframes"},
    "tw_1hr":   {"default": 2.0, "type": "float", "min": 0.0, "max": 5.0, "step": 0.5, "label": "1hr",   "group": "timeframes"},
    "tw_1day":  {"default": 3.0, "type": "float", "min": 0.0, "max": 5.0, "step": 0.5, "label": "1day",  "group": "timeframes"},
    # ── Regime confidence caps ─────────────────────────────────────────────────
    "regime_cap_trending_up":    {"default": 100, "type": "int", "min": 0, "max": 100, "step": 5, "label": "TRENDING_UP cap",     "group": "regime_caps"},
    "regime_cap_trending_down":  {"default": 100, "type": "int", "min": 0, "max": 100, "step": 5, "label": "TRENDING_DOWN cap",   "group": "regime_caps"},
    "regime_cap_ranging":        {"default": 80,  "type": "int", "min": 0, "max": 100, "step": 5, "label": "RANGING cap",         "group": "regime_caps"},
    "regime_cap_high_volatility":{"default": 60,  "type": "int", "min": 0, "max": 100, "step": 5, "label": "HIGH_VOLATILITY cap", "group": "regime_caps"},
    # ── Regime allowed signals (comma-separated signal type names) ─────────────
    "regime_trending_up_signals": {
        "default": "BREAKOUT_HIGH,EMA_CROSSOVER_UP,MACD_CROSS_UP,HIGH_RVOL,BB_EXPANSION,ABOVE_200_EMA,ORB_BREAKOUT,VWAP_RECLAIM",
        "type": "str", "label": "TRENDING_UP signals", "group": "regime_signals",
    },
    "regime_trending_down_signals": {
        "default": "BREAKOUT_LOW,EMA_CROSSOVER_DOWN,MACD_CROSS_DOWN,HIGH_RVOL,BB_EXPANSION,BELOW_200_EMA,ORB_BREAKOUT,VWAP_RECLAIM",
        "type": "str", "label": "TRENDING_DOWN signals", "group": "regime_signals",
    },
    "regime_ranging_signals": {
        "default": "RSI_OVERSOLD,RSI_OVERBOUGHT,BB_SQUEEZE,BB_EXPANSION,VWAP_RECLAIM,HIGH_RVOL",
        "type": "str", "label": "RANGING signals", "group": "regime_signals",
    },
    "regime_high_volatility_signals": {
        "default": "VWAP_RECLAIM",
        "type": "str", "label": "HIGH_VOLATILITY signals", "group": "regime_signals",
    },
}

DEFAULTS: dict = {k: v["default"] for k, v in CONFIG_SCHEMA.items()}


async def get_bot_config() -> dict:
    """Read all config from Redis, falling back to defaults for missing keys."""
    from database.connection import get_redis
    redis = get_redis()

    raw = await redis.get(REDIS_KEY)
    stored: dict = json.loads(raw) if raw else {}

    result: dict = {}
    for key, meta in CONFIG_SCHEMA.items():
        val = stored.get(key, meta["default"])
        typ = meta["type"]
        try:
            if typ == "bool":
                # Redis stores booleans as "true"/"false" strings or Python bool
                result[key] = val if isinstance(val, bool) else str(val).lower() in ("true", "1")
            elif typ == "int":
                result[key] = int(val)
            elif typ == "float":
                result[key] = float(val)
            else:
                result[key] = str(val) if val is not None else ""
        except (ValueError, TypeError):
            result[key] = meta["default"]

    return result


async def set_bot_config(updates: dict) -> dict:
    """Merge updates into stored config, persist to Redis, and return the full config."""
    from database.connection import get_redis
    redis = get_redis()

    raw = await redis.get(REDIS_KEY)
    stored: dict = json.loads(raw) if raw else {}

    for key, val in updates.items():
        if key in CONFIG_SCHEMA:
            stored[key] = val

    await redis.set(REDIS_KEY, json.dumps(stored))
    log.info("bot_config.updated", keys=list(updates.keys()))

    return await get_bot_config()


def get_config_schema() -> dict:
    """Return the schema dict for frontend rendering (types, ranges, labels)."""
    return CONFIG_SCHEMA
