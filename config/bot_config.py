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
    "strategy_candlestick": {
        "default": True, "type": "bool",
        "label": "Candlestick Patterns",
        "desc": "Hammer, Shooting Star, Engulfing, Morning/Evening Star.",
        "group": "strategies",
    },
    "strategy_chart_patterns": {
        "default": True, "type": "bool",
        "label": "Chart Patterns",
        "desc": "Double Top/Bottom, Bull/Bear Flag, Darvas Box, NR7.",
        "group": "strategies",
    },
    "momentum_watchlist_enabled": {
        "default": False, "type": "bool",
        "label": "RS Watchlist Filter (Darvas Incubator)",
        "desc": (
            "Only run Darvas box detection on top-50 RS leaders (stocks outperforming "
            "Nifty on 20-day ROC, within 15% of 52wk high). Built daily at 4 PM. "
            "Flip off to revert instantly to scanning full universe."
        ),
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
        "default": (
            "BREAKOUT_HIGH,EMA_CROSSOVER_UP,MACD_CROSS_UP,HIGH_RVOL,BB_EXPANSION,"
            "ABOVE_200_EMA,ORB_BREAKOUT,VWAP_RECLAIM,"
            "HAMMER,ENGULFING_BULL,"
            "DOUBLE_BOTTOM,BULL_FLAG,DARVAS_BREAKOUT,NR7_SETUP,"
            "BREAKOUT_52W,VOLUME_THRUST"
        ),
        "type": "str", "label": "TRENDING_UP signals", "group": "regime_signals",
    },
    "regime_trending_down_signals": {
        "default": (
            "BREAKOUT_LOW,EMA_CROSSOVER_DOWN,MACD_CROSS_DOWN,HIGH_RVOL,BB_EXPANSION,"
            "BELOW_200_EMA,ORB_BREAKOUT,VWAP_RECLAIM,"
            "SHOOTING_STAR,ENGULFING_BEAR,EVENING_STAR,"
            "DOUBLE_TOP,BEAR_FLAG,NR7_SETUP"
        ),
        "type": "str", "label": "TRENDING_DOWN signals", "group": "regime_signals",
    },
    "regime_ranging_signals": {
        "default": (
            "RSI_OVERSOLD,RSI_OVERBOUGHT,BB_SQUEEZE,BB_EXPANSION,VWAP_RECLAIM,HIGH_RVOL,"
            "HAMMER,SHOOTING_STAR,ENGULFING_BULL,ENGULFING_BEAR,"
            "MORNING_STAR,EVENING_STAR,DOUBLE_BOTTOM,DOUBLE_TOP,NR7_SETUP"
        ),
        "type": "str", "label": "RANGING signals", "group": "regime_signals",
    },
    "regime_high_volatility_signals": {
        "default": "VWAP_RECLAIM",
        "type": "str", "label": "HIGH_VOLATILITY signals", "group": "regime_signals",
    },
}

DEFAULTS: dict = {k: v["default"] for k, v in CONFIG_SCHEMA.items()}


def _coerce_type(val, typ: str):
    """Coerce a raw (possibly stringified-from-Redis) value to its declared type.
    Raises ValueError/TypeError on failure — callers decide how to handle that."""
    if typ == "bool":
        # Redis stores booleans as "true"/"false" strings or Python bool
        return val if isinstance(val, bool) else str(val).lower() in ("true", "1")
    elif typ == "int":
        return int(val)
    elif typ == "float":
        return float(val)
    else:
        return str(val) if val is not None else ""


def _in_bounds(val, meta: dict) -> bool:
    """True if val respects the schema's declared min/max (no-op for non-numeric types)."""
    if meta["type"] not in ("int", "float"):
        return True
    lo, hi = meta.get("min"), meta.get("max")
    if lo is not None and val < lo:
        return False
    if hi is not None and val > hi:
        return False
    return True


async def get_bot_config() -> dict:
    """Read all config from Redis, falling back to defaults for missing keys.

    Defensive: a value already sitting in Redis that is out of range (e.g. from
    before bounds enforcement existed, or written directly) falls back to the
    schema default rather than being served to strategies as-is.
    """
    from database.connection import get_redis
    redis = get_redis()

    raw = await redis.get(REDIS_KEY)
    stored: dict = json.loads(raw) if raw else {}

    result: dict = {}
    for key, meta in CONFIG_SCHEMA.items():
        val = stored.get(key, meta["default"])
        try:
            coerced = _coerce_type(val, meta["type"])
            if not _in_bounds(coerced, meta):
                log.warning(
                    "bot_config.stored_value_out_of_range",
                    key=key, value=coerced, min=meta.get("min"), max=meta.get("max"),
                )
                coerced = meta["default"]
            result[key] = coerced
        except (ValueError, TypeError):
            result[key] = meta["default"]

    return result


_VALID_SIGNAL_NAMES: set[str] | None = None


def _get_valid_signal_names() -> set[str]:
    global _VALID_SIGNAL_NAMES
    if _VALID_SIGNAL_NAMES is None:
        from services.technical_engine.signal_generator import SignalType
        _VALID_SIGNAL_NAMES = {st.value for st in SignalType}
    return _VALID_SIGNAL_NAMES


async def set_bot_config(updates: dict) -> dict:
    """Merge updates into stored config, persist to Redis, and return the full config."""
    from database.connection import get_redis
    redis = get_redis()

    raw = await redis.get(REDIS_KEY)
    stored: dict = json.loads(raw) if raw else {}

    valid_signal_names = _get_valid_signal_names()
    for key, val in updates.items():
        if key not in CONFIG_SCHEMA:
            continue
        meta = CONFIG_SCHEMA[key]

        # Validate regime signal lists against SignalType enum
        if key.startswith("regime_") and key.endswith("_signals") and isinstance(val, str):
            names = [n.strip() for n in val.split(",") if n.strip()]
            bad = [n for n in names if n not in valid_signal_names]
            if bad:
                log.warning("bot_config.invalid_signal_names", key=key, unknown=bad)
                raise ValueError(f"Unknown signal type(s) in {key}: {bad}")

        # Coerce to the declared type, then enforce the declared min/max bounds.
        # CONFIG_SCHEMA declares min/max for every int/float knob (e.g.
        # confidence_threshold: 40-100) — skipping this let callers disable
        # signal-quality gates entirely (confidence_threshold=-999). Reject
        # rather than silently clamp, consistent with the signal-name check above.
        try:
            coerced = _coerce_type(val, meta["type"])
        except (ValueError, TypeError):
            raise ValueError(f"{key} must be of type {meta['type']} (got {val!r})")

        if not _in_bounds(coerced, meta):
            lo, hi = meta.get("min"), meta.get("max")
            log.warning("bot_config.out_of_range", key=key, value=coerced, min=lo, max=hi)
            raise ValueError(f"{key}={coerced} out of range (allowed: {lo}..{hi})")

        stored[key] = coerced

    await redis.set(REDIS_KEY, json.dumps(stored))
    log.info("bot_config.updated", keys=list(updates.keys()))

    return await get_bot_config()


def get_config_schema() -> dict:
    """Return the schema dict for frontend rendering (types, ranges, labels)."""
    return CONFIG_SCHEMA
