"""
services/market_regime/session.py
───────────────────────────────────
Session regime: classifies what the market is doing TODAY using intraday data.

Evaluated at two fixed points:
  9:45 AM — Opening Range established (first 15min candle closed)
  10:15 AM — Confirmation: is the session direction holding?

Uses real Nifty 50 intraday data from yfinance regardless of paper/live mode.

SessionRegime values:
  BULLISH_SESSION  — gap up holding, price above ORB + VWAP
  BEARISH_SESSION  — gap down holding, price below ORB + VWAP
  NEUTRAL_SESSION  — mixed signals, no clear direction

Merge logic (applied against structural regime):
  Both agree          → use that regime (strong)
  Conflict            → RANGING (caution)
  RANGING + session   → session direction (session breaks the tie)
  HIGH_VOLATILITY     → never overridden by session
"""
from __future__ import annotations

import asyncio
from typing import Literal

import structlog

log = structlog.get_logger(__name__)

SessionRegime = Literal["BULLISH_SESSION", "BEARISH_SESSION", "NEUTRAL_SESSION", "UNKNOWN"]


async def fetch_nifty_intraday() -> "pd.DataFrame | None":
    """Fetch today's Nifty 50 intraday 15min data from yfinance."""
    return await asyncio.get_event_loop().run_in_executor(None, _fetch_intraday_sync)


def _fetch_intraday_sync():
    try:
        import yfinance as yf
        import pandas as pd
        t = yf.Ticker("^NSEI")
        df = t.history(period="1d", interval="15m", auto_adjust=True)
        if df is None or df.empty:
            return None
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        df = df.dropna()
        return df
    except Exception as e:
        log.warning("session_regime.fetch_failed", error=str(e))
        return None


def evaluate_session_regime(df) -> SessionRegime:
    """
    Compute session regime from Nifty 50 15min intraday DataFrame.
    Needs at least 2 candles (9:15-9:30 and 9:30-9:45).
    """
    if df is None or len(df) < 2:
        return "UNKNOWN"

    try:
        import numpy as np

        # Opening Range = first candle (9:15-9:30)
        orb_high = float(df.iloc[0]["high"])
        orb_low  = float(df.iloc[0]["low"])
        first_open = float(df.iloc[0]["open"])

        # Previous close = day before open (approximate from first open vs gap)
        # Use first candle open as proxy for today's open
        today_open = first_open

        # Current price = latest close
        current = float(df.iloc[-1]["close"])

        # VWAP = sum(typical_price * volume) / sum(volume)
        typical = (df["high"].astype(float) + df["low"].astype(float) + df["close"].astype(float)) / 3
        vol = df["volume"].astype(float)
        vwap = float((typical * vol).sum() / vol.sum()) if vol.sum() > 0 else current

        # Gap vs previous close (use 2-day data for better estimate)
        prev_close = None
        try:
            import yfinance as yf
            hist = yf.Ticker("^NSEI").history(period="2d", interval="1d", auto_adjust=True)
            if hist is not None and len(hist) >= 2:
                prev_close = float(hist["Close"].iloc[-2])
        except Exception:
            pass

        gap_pct = ((today_open - prev_close) / prev_close * 100) if prev_close else 0.0

        # Score bullish/bearish signals
        bullish = 0
        bearish = 0

        # 1. Gap direction (only meaningful gaps > 0.3%)
        if gap_pct > 0.3:
            bullish += 1
        elif gap_pct < -0.3:
            bearish += 1

        # 2. ORB position — has price broken the opening range?
        if current > orb_high:
            bullish += 1
        elif current < orb_low:
            bearish += 1

        # 3. VWAP position
        if current > vwap:
            bullish += 1
        else:
            bearish += 1

        log.info(
            "session_regime.evaluated",
            candles=len(df),
            gap_pct=round(gap_pct, 2),
            orb_high=round(orb_high, 2),
            orb_low=round(orb_low, 2),
            current=round(current, 2),
            vwap=round(vwap, 2),
            bullish_signals=bullish,
            bearish_signals=bearish,
        )

        # Need at least 2 of 3 signals to make a call
        if bullish >= 2:
            return "BULLISH_SESSION"
        elif bearish >= 2:
            return "BEARISH_SESSION"
        else:
            return "NEUTRAL_SESSION"

    except Exception as e:
        log.warning("session_regime.error", error=str(e))
        return "UNKNOWN"


def merge_regimes(structural: str, session: SessionRegime) -> str:
    """
    Combine structural (daily) and session (intraday) regimes.

    Rules:
      HIGH_VOLATILITY is never overridden
      Both agree        → use that regime
      Conflict          → RANGING (caution)
      RANGING + session → session breaks the tie
      NEUTRAL_SESSION   → keep structural
      UNKNOWN session   → keep structural
    """
    # HIGH_VOLATILITY is absolute — never downgraded
    if structural == "HIGH_VOLATILITY":
        return "HIGH_VOLATILITY"

    # No session data → keep structural
    if session in ("UNKNOWN", "NEUTRAL_SESSION"):
        return structural

    # Agreement
    if structural == "TRENDING_UP" and session == "BULLISH_SESSION":
        return "TRENDING_UP"
    if structural == "TRENDING_DOWN" and session == "BEARISH_SESSION":
        return "TRENDING_DOWN"

    # Conflict → RANGING
    if structural == "TRENDING_UP" and session == "BEARISH_SESSION":
        log.info("regime.conflict", structural=structural, session=session, merged="RANGING")
        return "RANGING"
    if structural == "TRENDING_DOWN" and session == "BULLISH_SESSION":
        log.info("regime.conflict", structural=structural, session=session, merged="RANGING")
        return "RANGING"

    # RANGING + session signal → session breaks the tie
    if structural == "RANGING":
        if session == "BULLISH_SESSION":
            return "TRENDING_UP"
        if session == "BEARISH_SESSION":
            return "TRENDING_DOWN"

    return structural
