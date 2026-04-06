"""
services/technical_engine/indicators.py
─────────────────────────────────────────
Computes all technical indicators on a price DataFrame.

Input:  pd.DataFrame with columns [open, high, low, close, volume]
        indexed by datetime (UTC).

Output: same DataFrame with all indicator columns appended.

Design principles:
  - Pure functions: no I/O, no side effects, fully testable
  - pandas-ta for all indicators (pure Python, no C dependency)
  - Covers: Trend, Momentum, Volatility, Volume, Support/Resistance
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pandas_ta as ta


# ─── Configuration ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class IndicatorConfig:
    """Tweak indicator parameters here without touching computation code."""
    # Moving Averages
    ema_fast: int = 9
    ema_mid: int  = 21
    ema_slow: int = 50
    ema_trend: int = 200
    sma_20: int   = 20

    # Momentum
    rsi_period: int         = 14
    stoch_k: int            = 14
    stoch_d: int            = 3
    macd_fast: int          = 12
    macd_slow: int          = 26
    macd_signal: int        = 9
    cci_period: int         = 20
    williams_r_period: int  = 14
    mfi_period: int         = 14

    # Volatility
    bb_period: int    = 20
    bb_std: float     = 2.0
    atr_period: int   = 14
    kc_period: int    = 20

    # Trend
    adx_period: int       = 14
    supertrend_period: int = 10
    supertrend_mult: float = 3.0

    # Volume
    obv_signal: int = 10   # EMA period on OBV

    # Support / Resistance
    swing_lookback: int = 10


DEFAULT_CONFIG = IndicatorConfig()


# ─── Main Computation ─────────────────────────────────────────────────────────

def compute_all(
    df: pd.DataFrame,
    cfg: IndicatorConfig = DEFAULT_CONFIG,
) -> pd.DataFrame:
    """
    Compute the full indicator suite on the given OHLCV DataFrame.

    Returns a new DataFrame with all indicator columns added.
    Columns follow the naming convention:  indicator_param  (e.g. ema_9, rsi_14)
    """
    df = df.copy()
    df = _trend(df, cfg)
    df = _momentum(df, cfg)
    df = _volatility(df, cfg)
    df = _volume_indicators(df, cfg)
    df = _support_resistance(df, cfg)
    df = _derived(df, cfg)
    return df


# ─── Trend ────────────────────────────────────────────────────────────────────

def _trend(df: pd.DataFrame, cfg: IndicatorConfig) -> pd.DataFrame:
    # Exponential Moving Averages
    df[f"ema_{cfg.ema_fast}"]  = ta.ema(df["close"], length=cfg.ema_fast)
    df[f"ema_{cfg.ema_mid}"]   = ta.ema(df["close"], length=cfg.ema_mid)
    df[f"ema_{cfg.ema_slow}"]  = ta.ema(df["close"], length=cfg.ema_slow)
    df[f"ema_{cfg.ema_trend}"] = ta.ema(df["close"], length=cfg.ema_trend)

    # Simple Moving Average
    df[f"sma_{cfg.sma_20}"] = ta.sma(df["close"], length=cfg.sma_20)

    # VWAP (intraday only — resets each day; NaN for daily/weekly data)
    try:
        df["vwap"] = ta.vwap(df["high"], df["low"], df["close"], df["volume"])
    except Exception:
        df["vwap"] = np.nan

    # ADX (trend strength)
    adx = ta.adx(df["high"], df["low"], df["close"], length=cfg.adx_period)
    if adx is not None:
        df["adx"]   = adx[f"ADX_{cfg.adx_period}"]
        df["di_pos"] = adx[f"DMP_{cfg.adx_period}"]
        df["di_neg"] = adx[f"DMN_{cfg.adx_period}"]

    # Supertrend
    st = ta.supertrend(
        df["high"], df["low"], df["close"],
        length=cfg.supertrend_period,
        multiplier=cfg.supertrend_mult,
    )
    if st is not None:
        col = f"SUPERT_{cfg.supertrend_period}_{cfg.supertrend_mult}"
        df["supertrend"]       = st[col] if col in st.columns else np.nan
        dir_col = f"SUPERTd_{cfg.supertrend_period}_{cfg.supertrend_mult}"
        df["supertrend_dir"]   = st[dir_col] if dir_col in st.columns else np.nan

    # Parabolic SAR
    psar = ta.psar(df["high"], df["low"], df["close"])
    if psar is not None and not psar.empty:
        df["psar"] = psar.iloc[:, 0]

    return df


# ─── Momentum ─────────────────────────────────────────────────────────────────

def _momentum(df: pd.DataFrame, cfg: IndicatorConfig) -> pd.DataFrame:
    # RSI
    df[f"rsi_{cfg.rsi_period}"] = ta.rsi(df["close"], length=cfg.rsi_period)

    # Stochastic
    stoch = ta.stoch(df["high"], df["low"], df["close"], k=cfg.stoch_k, d=cfg.stoch_d)
    if stoch is not None:
        df["stoch_k"] = stoch[f"STOCHk_{cfg.stoch_k}_{cfg.stoch_d}_3"]
        df["stoch_d"] = stoch[f"STOCHd_{cfg.stoch_k}_{cfg.stoch_d}_3"]

    # MACD
    macd = ta.macd(
        df["close"],
        fast=cfg.macd_fast,
        slow=cfg.macd_slow,
        signal=cfg.macd_signal,
    )
    if macd is not None:
        df["macd"]        = macd[f"MACD_{cfg.macd_fast}_{cfg.macd_slow}_{cfg.macd_signal}"]
        df["macd_signal"] = macd[f"MACDs_{cfg.macd_fast}_{cfg.macd_slow}_{cfg.macd_signal}"]
        df["macd_hist"]   = macd[f"MACDh_{cfg.macd_fast}_{cfg.macd_slow}_{cfg.macd_signal}"]

    # CCI
    df[f"cci_{cfg.cci_period}"] = ta.cci(
        df["high"], df["low"], df["close"], length=cfg.cci_period
    )

    # Williams %R
    df["williams_r"] = ta.willr(
        df["high"], df["low"], df["close"], length=cfg.williams_r_period
    )

    # MFI (Money Flow Index — volume-weighted RSI)
    df[f"mfi_{cfg.mfi_period}"] = ta.mfi(
        df["high"], df["low"], df["close"], df["volume"], length=cfg.mfi_period
    )

    return df


# ─── Volatility ───────────────────────────────────────────────────────────────

def _volatility(df: pd.DataFrame, cfg: IndicatorConfig) -> pd.DataFrame:
    # Bollinger Bands
    bb = ta.bbands(df["close"], length=cfg.bb_period, std=cfg.bb_std)
    if bb is not None:
        df["bb_upper"]  = bb[f"BBU_{cfg.bb_period}_{cfg.bb_std}"]
        df["bb_mid"]    = bb[f"BBM_{cfg.bb_period}_{cfg.bb_std}"]
        df["bb_lower"]  = bb[f"BBL_{cfg.bb_period}_{cfg.bb_std}"]
        df["bb_width"]  = bb[f"BBB_{cfg.bb_period}_{cfg.bb_std}"]   # (upper-lower)/mid
        df["bb_pct"]    = bb[f"BBP_{cfg.bb_period}_{cfg.bb_std}"]   # 0=lower, 1=upper

    # ATR (Average True Range)
    df[f"atr_{cfg.atr_period}"] = ta.atr(
        df["high"], df["low"], df["close"], length=cfg.atr_period
    )

    # ATR as % of price (normalised volatility) — guard against zero/bad close prices
    safe_close = df["close"].replace(0, np.nan)
    df["atr_pct"] = df[f"atr_{cfg.atr_period}"] / safe_close * 100

    # Keltner Channels
    kc = ta.kc(df["high"], df["low"], df["close"], length=cfg.kc_period)
    if kc is not None and not kc.empty:
        df["kc_upper"] = kc.iloc[:, 0]
        df["kc_lower"] = kc.iloc[:, 2]

    return df


# ─── Volume Indicators ────────────────────────────────────────────────────────

def _volume_indicators(df: pd.DataFrame, cfg: IndicatorConfig) -> pd.DataFrame:
    # OBV (On-Balance Volume)
    df["obv"] = ta.obv(df["close"], df["volume"])
    df["obv_signal"] = ta.ema(df["obv"], length=cfg.obv_signal)

    # CMF (Chaikin Money Flow)
    df["cmf"] = ta.cmf(df["high"], df["low"], df["close"], df["volume"])

    # Relative Volume (RVOL): today's volume vs 20-day average volume
    vol_avg = df["volume"].rolling(20).mean()
    df["rvol"] = df["volume"] / vol_avg.replace(0, np.nan)

    return df


# ─── Support & Resistance ─────────────────────────────────────────────────────

def _support_resistance(df: pd.DataFrame, cfg: IndicatorConfig) -> pd.DataFrame:
    lb = cfg.swing_lookback

    # Swing highs and lows
    df["swing_high"] = df["high"].where(
        (df["high"] == df["high"].rolling(lb * 2 + 1, center=True).max()),
        other=np.nan,
    )
    df["swing_low"] = df["low"].where(
        (df["low"] == df["low"].rolling(lb * 2 + 1, center=True).min()),
        other=np.nan,
    )

    # Classic Pivot Points (daily — use prev day OHLC)
    df["pivot"]     = (df["high"].shift(1) + df["low"].shift(1) + df["close"].shift(1)) / 3
    df["r1"]        = 2 * df["pivot"] - df["low"].shift(1)
    df["s1"]        = 2 * df["pivot"] - df["high"].shift(1)
    df["r2"]        = df["pivot"] + (df["high"].shift(1) - df["low"].shift(1))
    df["s2"]        = df["pivot"] - (df["high"].shift(1) - df["low"].shift(1))

    return df


# ─── Derived / Composite ──────────────────────────────────────────────────────

def _derived(df: pd.DataFrame, cfg: IndicatorConfig) -> pd.DataFrame:
    """Derived columns that combine multiple indicators."""

    # EMA stack direction:  +1 = bullish (fast > mid > slow), -1 = bearish, 0 = mixed
    if all(c in df.columns for c in [f"ema_{cfg.ema_fast}", f"ema_{cfg.ema_mid}", f"ema_{cfg.ema_slow}"]):
        fast  = df[f"ema_{cfg.ema_fast}"]
        mid   = df[f"ema_{cfg.ema_mid}"]
        slow  = df[f"ema_{cfg.ema_slow}"]
        df["ema_stack"] = np.where(
            (fast > mid) & (mid > slow),  1,
            np.where((fast < mid) & (mid < slow), -1, 0),
        )

    # Price position relative to 200 EMA
    if f"ema_{cfg.ema_trend}" in df.columns:
        df["above_200ema"] = (df["close"] > df[f"ema_{cfg.ema_trend}"]).astype(int)

    # RSI zone: 0=oversold(<30), 1=neutral, 2=overbought(>70)
    rsi_col = f"rsi_{cfg.rsi_period}"
    if rsi_col in df.columns:
        df["rsi_zone"] = np.where(
            df[rsi_col] < 30, 0,
            np.where(df[rsi_col] > 70, 2, 1),
        )

    # MACD momentum direction: +1 = histogram growing, -1 = shrinking
    if "macd_hist" in df.columns:
        df["macd_momentum"] = np.sign(df["macd_hist"].diff())

    return df


# ─── Latest values helper ────────────────────────────────────────────────────

def get_latest(df: pd.DataFrame) -> dict:
    """
    Return a dictionary of the most recent indicator values.
    Useful for passing to the AI strategy engine.
    """
    if df.empty:
        return {}
    latest = df.iloc[-1].dropna().to_dict()
    # Round floats for cleaner JSON
    return {k: round(v, 4) if isinstance(v, float) else v for k, v in latest.items()}
