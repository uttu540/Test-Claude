"""
services/backtesting/engine.py
────────────────────────────────
Replays historical OHLCV data through the signal → risk pipeline
to measure strategy performance without live trading.

Data sources (tried in order):
  1. TimescaleDB ohlcv_candles table (requires running DB)
  2. yfinance download (fallback, no API key needed)

No orders are placed. Trades are simulated by checking whether
stop-loss or target was hit in candles following the signal.

──────────────────────────────────────────────────────────────────
Top-Down Analysis (trading_mode)
──────────────────────────────────────────────────────────────────
Signals follow a two-step process — spot on the higher TF, confirm
on the lower TF, then execute.

  SWING mode (trading_mode="swing")
    Setup TF  : Daily  — determines trend direction / regime
    Trigger TF: 1H     — provides entry confirmation signal
    Hold      : up to 5 trading days (30 × 1H candles)
    Exit      : TARGET | STOP | MAX_HOLD  (no forced EOD exit)

  INTRADAY mode (trading_mode="intraday")
    Setup TF  : 1H     — determines intraday directional bias
    Trigger TF: 15min  — provides entry signal
    Hold      : up to 20 × 15min candles (~5 trading hours)
    Exit      : TARGET | STOP | EOD (forced close at 15:20 IST)

Exit logic (no look-ahead bias):
  - Entry:  next trigger-TF candle open after signal fires
  - Stop:   first candle where low ≤ stop_loss (LONG) or high ≥ stop_loss (SHORT)
  - Target: first candle where high ≥ target (LONG) or low ≤ target (SHORT)
  - EOD:    intraday only — closed at 15:20 candle close
  - Max hold: mode-dependent (see above)
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import AsyncIterator

import numpy as np
import pandas as pd
import structlog

from config.settings import settings
from services.market_regime.detector import MarketRegimeDetector
from services.risk_engine.engine import RiskDecision, RiskEngine
from services.technical_engine.indicators import compute_all
from services.technical_engine.signal_generator import (
    Direction,
    MultiTimeframeSignalEngine,
    Signal,
    SignalType,
)

log = structlog.get_logger(__name__)

# Maximum candles to hold a simulated position before forcing exit
MAX_HOLD_CANDLES = 20   # ~5 days on 15min

# ── Confluence scoring ────────────────────────────────────────────────────────
# Minimum total score (out of 10) required to enter a trade.
# Each of the 5 factors scores 0-2; need at least 3 factors to partially agree.
MIN_CONFLUENCE_SCORE = 8   # 3-month bear test: score_8 = 50% WR; score_9 over-filters
                           # (chases extended moves rather than catching fresh ones)

# Signals that are fundamentally intraday tools (VWAP resets daily, ORB is 9:15-9:30).
# They have no meaning on swing timeframes (1H multi-day holds) and consistently
# underperform: VWAP_RECLAIM had ₹-2,565 P&L before confluence filtering.
_INTRADAY_ONLY_SIGNALS: frozenset[str] = frozenset({"VWAP_RECLAIM", "ORB_BREAKOUT"})

# Signals disabled as standalone entries — no directional edge detected.
# HIGH_RVOL: direction assigned by single-candle OHLC (noise), 26.2% WR, ₹-8,467.
# BULL_FLAG / BEAR_FLAG: 0% and 16.7% WR respectively across all tests.
# SHOOTING_STAR: 9.1% WR, ₹-3,561. Weak context (only requires prior bullish candle).
# ENGULFING_BULL: 25-27% WR despite multiple hard gates — needs fundamental rework.
# These signals may still appear as confluence boosters (multi_signal factor) but
# cannot be the top-ranked signal that triggers a trade.
_DISABLED_AS_ENTRY: frozenset[str] = frozenset({
    "HIGH_RVOL",
    "BULL_FLAG", "BEAR_FLAG",
    "SHOOTING_STAR",
    "ENGULFING_BULL",
})

# Signal types considered "high quality" for the signal-strength factor
_HIGH_QUALITY_SIGNALS = {
    "BREAKOUT_HIGH", "BREAKOUT_LOW",
    "DOUBLE_BOTTOM",  "DOUBLE_TOP",
    "DARVAS_BREAKOUT",
    "ENGULFING_BULL", "ENGULFING_BEAR",
    "EVENING_STAR",   "MORNING_STAR",
    "BULL_FLAG",      "BEAR_FLAG",
    "EMA_CROSSOVER_UP", "EMA_CROSSOVER_DOWN",
}


@dataclass
class ConfluenceScore:
    """
    Scores a potential trade setup across 5 independent factors (max 10 pts).
    A setup must score >= MIN_CONFLUENCE_SCORE to be traded.

    Factors:
      signal_strength   : signal confidence + pattern quality (0-2)
      volume            : RVOL vs threshold                   (0-2)
      trend_alignment   : EMA stack + 200 EMA position        (0-2)
      momentum          : RSI in sweet-spot for direction      (0-2)
      multi_signal      : how many distinct signals agree      (0-2)
    """
    signal_strength: int = 0
    volume:          int = 0
    trend_alignment: int = 0
    momentum:        int = 0
    multi_signal:    int = 0

    @property
    def total(self) -> int:
        return self.signal_strength + self.volume + self.trend_alignment + self.momentum + self.multi_signal

    @property
    def passed(self) -> bool:
        return self.total >= MIN_CONFLUENCE_SCORE

    def to_dict(self) -> dict:
        return {
            "total":          self.total,
            "signal_strength": self.signal_strength,
            "volume":         self.volume,
            "trend_alignment": self.trend_alignment,
            "momentum":       self.momentum,
            "multi_signal":   self.multi_signal,
        }


@dataclass
class SimulatedTrade:
    symbol:           str
    signal_type:      str
    direction:        str
    timeframe:        str
    entry_date:       date
    entry_price:      float
    stop_loss:        float
    target:           float
    exit_price:       float      = 0.0
    exit_reason:      str        = "OPEN"   # TARGET | STOP | TRAIL_STOP | EOD | MAX_HOLD
    pnl:              float      = 0.0
    pnl_pct:          float      = 0.0
    holding_candles:  int        = 0
    signal_confidence: int       = 0
    regime:           str        = "UNKNOWN"
    risk_amount:      float      = 0.0
    position_size:    int        = 0
    confluence_score: int        = 0   # 0-10; breakdown in confluence_factors
    confluence_factors: dict     = field(default_factory=dict)


@dataclass
class BacktestResult:
    trades:        list[SimulatedTrade] = field(default_factory=list)
    symbols:       list[str]            = field(default_factory=list)
    start_date:    date | None          = None
    end_date:      date | None          = None
    timeframes:    list[str]            = field(default_factory=list)


class BacktestEngine:
    """
    Runs a full backtest over a set of symbols and date range.

    Usage:
        engine = BacktestEngine(
            symbols    = ["RELIANCE", "TCS", "INFY"],
            start_date = date(2024, 1, 1),
            end_date   = date(2024, 12, 31),
            timeframes = ["15min", "1hr", "1day"],
        )
        result = await engine.run()
    """

    # Per-mode config: (setup_tf, trigger_tf, max_hold_candles, eod_exit)
    _MODE_CONFIG: dict[str, tuple[str, str, int, bool]] = {
        #             setup     trigger   hold  eod
        "swing":     ("1day",  "1hr",    140,  False),  # 140×1H = ~7 trading days
        "intraday":  ("1hr",   "15min",  20,   True),
    }

    def __init__(
        self,
        symbols:                list[str],
        start_date:             date,
        end_date:               date,
        timeframes:             list[str] | None = None,
        regime_aware:           bool = True,
        min_confidence:         int  = 80,
        regime_aligned_only:    bool = True,
        disabled_signals:       list[str] | None = None,
        min_signal_timeframes:  int = 1,
        min_confirming_signals: int = 1,
        trading_mode:           str = "intraday",   # "intraday" | "swing"
        symbol_segments:        dict[str, str] | None = None,
        enable_sector_filter:   bool = False,
    ) -> None:
        self._symbols              = symbols
        self._start                = start_date
        self._end                  = end_date
        self._regime_aware         = regime_aware
        self._min_confidence       = min_confidence
        self._regime_aligned_only  = regime_aligned_only
        self._min_signal_tfs       = min_signal_timeframes
        # Require this many DISTINCT signal types in the same direction before entering.
        # Default 1 = any single signal can trigger (backward-compatible).
        # Set ≥ 2 to demand genuine confluence (e.g. EMA cross + Engulfing + RVOL).
        self._min_confirming_signals = min_confirming_signals
        # Signal types to exclude entirely (e.g. noisy intraday signals on daily TF)
        self._disabled_signals     = set(disabled_signals or [])

        # ── Trading mode: sets setup_tf, trigger_tf, max_hold, eod_exit ──────
        self._trading_mode = trading_mode
        mode_cfg = self._MODE_CONFIG.get(trading_mode, self._MODE_CONFIG["intraday"])
        self._setup_tf,  self._trigger_tf, self._max_hold, self._eod_exit = mode_cfg

        # Timeframes: caller can override, otherwise auto-set from mode.
        # Always ensure both setup_tf and trigger_tf are present.
        if timeframes:
            self._timeframes = timeframes
        else:
            self._timeframes = [self._trigger_tf, self._setup_tf]
        # Guarantee both setup and trigger TFs are in the list
        for tf in (self._setup_tf, self._trigger_tf):
            if tf not in self._timeframes:
                self._timeframes.append(tf)

        # Segment map: symbol → LARGE_CAP | MID_CAP | SMALL_CAP
        # Used for segment-aware RVOL / pattern quality gates.
        self._symbol_segments = symbol_segments

        # Sector filter: loads sectoral index ROC-20 per date.
        # Off by default — use enable_sector_filter=True to opt in.
        self._enable_sector_filter = enable_sector_filter
        # sector_name → {date → roc_20_pct}   e.g. "Financials" → {2025-01-15 → -2.3}
        self._sector_roc_by_date: dict[str, dict[date, float]] = {}
        # symbol → sector_name   e.g. "HDFCBANK" → "Financials"
        self._symbol_to_sector: dict[str, str] = {}

        self._signal_engine   = MultiTimeframeSignalEngine()
        self._risk_engine     = RiskEngine()
        self._regime_detector = MarketRegimeDetector()

        # Level 1 & 2 lookups — populated in run() before the symbol loop
        self._market_regime_by_date:     dict[date, str]  = {}   # date → TRENDING_UP/DOWN/RANGING
        self._vix_level_by_date:         dict[date, str]  = {}   # date → CALM/ELEVATED/HIGH/EXTREME
        # P1: True = Nifty 200 EMA is rising on that date (bull phase → block all shorts)
        self._nifty_200ema_rising_by_date: dict[date, bool] = {}
        # Segment-specific regime: mid/small cap index 200 EMA state per date
        # True = index above its own 200 EMA (healthy mid/small cap environment)
        self._midcap_regime_by_date:   dict[date, str]  = {}   # date → TRENDING_UP/DOWN/RANGING
        self._smallcap_regime_by_date: dict[date, str]  = {}   # date → TRENDING_UP/DOWN/RANGING

    async def run(self) -> BacktestResult:
        result = BacktestResult(
            symbols    = self._symbols,
            start_date = self._start,
            end_date   = self._end,
            timeframes = self._timeframes,
        )

        # ── Level 1 & 2: load market-wide data ONCE before the symbol loop ────
        await self._load_market_indices()

        for symbol in self._symbols:
            log.info("backtest.symbol_start", symbol=symbol)
            try:
                trades = await self._backtest_symbol(symbol)
                result.trades.extend(trades)
                log.info(
                    "backtest.symbol_done",
                    symbol = symbol,
                    trades = len(trades),
                )
            except Exception as e:
                log.error("backtest.symbol_error", symbol=symbol, error=str(e))

        log.info(
            "backtest.complete",
            symbols = len(self._symbols),
            trades  = len(result.trades),
        )
        return result

    # ── Market-wide data (Level 1 & 2) ───────────────────────────────────────

    async def _load_market_indices(self) -> None:
        """
        Load Nifty 50 index and India VIX daily data for the backtest period.
        Populates:
          _market_regime_by_date : date → TRENDING_UP | TRENDING_DOWN | RANGING
          _vix_level_by_date     : date → CALM | ELEVATED | HIGH | EXTREME
        Falls back to empty dicts silently — backtesting still runs, just
        without market-level filters (same as before).
        """
        import yfinance as yf

        load_start = self._start - timedelta(days=60)   # extra buffer for indicators
        load_end   = (self._end + timedelta(days=1)).isoformat()

        # ── Nifty 50 index → Level 1 market regime ───────────────────────────
        try:
            raw = yf.Ticker("^NSEI").history(
                start       = load_start.isoformat(),
                end         = load_end,
                interval    = "1d",
                auto_adjust = True,
            )
            if not raw.empty:
                raw.index = pd.to_datetime(raw.index)
                if raw.index.tz is not None:
                    raw.index = raw.index.tz_convert("Asia/Kolkata").tz_localize(None)
                raw = raw.rename(columns={
                    "Open": "open", "High": "high",
                    "Low": "low",   "Close": "close", "Volume": "volume",
                })[["open", "high", "low", "close", "volume"]]

                nifty_df = compute_all(raw)
                for ts, row in nifty_df.iterrows():
                    d         = ts.date() if hasattr(ts, "date") else ts
                    adx       = row.get("adx")       if "adx"       in nifty_df.columns else None
                    ema_stack = row.get("ema_stack")  if "ema_stack" in nifty_df.columns else None
                    if adx is not None and not pd.isna(adx):
                        if adx < 20:
                            self._market_regime_by_date[d] = "RANGING"
                        else:
                            self._market_regime_by_date[d] = (
                                "TRENDING_UP" if (ema_stack or 0) >= 0 else "TRENDING_DOWN"
                            )
                # P1: store whether Nifty 200 EMA is rising per date.
                # "Rising" = today's EMA_200 > EMA_200 from 10 trading days ago.
                # A rising 200 EMA = long-term bull phase → block all short trades.
                ema200_col = "ema_200"
                if ema200_col in nifty_df.columns:
                    ema200_rising = nifty_df[ema200_col] > nifty_df[ema200_col].shift(10)
                    for ts, is_rising in ema200_rising.items():
                        if not pd.isna(is_rising):
                            d = ts.date() if hasattr(ts, "date") else ts
                            self._nifty_200ema_rising_by_date[d] = bool(is_rising)

                log.info(
                    "backtest.nifty_loaded",
                    trading_days   = len(self._market_regime_by_date),
                    trending_up    = sum(1 for v in self._market_regime_by_date.values() if v == "TRENDING_UP"),
                    trending_down  = sum(1 for v in self._market_regime_by_date.values() if v == "TRENDING_DOWN"),
                    ranging        = sum(1 for v in self._market_regime_by_date.values() if v == "RANGING"),
                    bull_phase_days= sum(1 for v in self._nifty_200ema_rising_by_date.values() if v),
                )
        except Exception as e:
            log.warning("backtest.nifty_load_failed", error=str(e))

        # ── India VIX → Level 2 volatility gate ──────────────────────────────
        try:
            vix_raw = yf.Ticker("^INDIAVIX").history(
                start       = load_start.isoformat(),
                end         = load_end,
                interval    = "1d",
                auto_adjust = True,
            )
            if not vix_raw.empty:
                vix_raw.index = pd.to_datetime(vix_raw.index)
                if vix_raw.index.tz is not None:
                    vix_raw.index = vix_raw.index.tz_convert("Asia/Kolkata").tz_localize(None)
                for ts, row in vix_raw.iterrows():
                    d = ts.date() if hasattr(ts, "date") else ts
                    v = float(row.get("Close", row.get("close", 15)) or 15)
                    if v > 25:
                        self._vix_level_by_date[d] = "EXTREME"
                    elif v > 20:
                        self._vix_level_by_date[d] = "HIGH"
                    elif v > 15:
                        self._vix_level_by_date[d] = "ELEVATED"
                    else:
                        self._vix_level_by_date[d] = "CALM"
                log.info(
                    "backtest.vix_loaded",
                    trading_days = len(self._vix_level_by_date),
                    extreme_days = sum(1 for v in self._vix_level_by_date.values() if v == "EXTREME"),
                    high_days    = sum(1 for v in self._vix_level_by_date.values() if v == "HIGH"),
                )
        except Exception as e:
            log.warning("backtest.vix_load_failed", error=str(e))

        # ── Nifty Midcap 150 + Nifty Smallcap 250 regime ─────────────────────
        # Used to gate mid/small cap longs — if their index is in a downtrend,
        # individual mid/small cap long signals have much lower follow-through
        # even when Nifty 50 is fine. (e.g. 2025: Nifty recovered but midcap/
        # smallcap indices ground down for months).
        for index_ticker, regime_dict, label in [
            ("^CNXMDCP",  self._midcap_regime_by_date,   "midcap"),
            ("^CNXSC",    self._smallcap_regime_by_date, "smallcap"),
        ]:
            try:
                idx_raw = yf.Ticker(index_ticker).history(
                    start       = load_start.isoformat(),
                    end         = load_end,
                    interval    = "1d",
                    auto_adjust = True,
                )
                if not idx_raw.empty:
                    idx_raw.index = pd.to_datetime(idx_raw.index)
                    if idx_raw.index.tz is not None:
                        idx_raw.index = idx_raw.index.tz_convert("Asia/Kolkata").tz_localize(None)
                    idx_raw = idx_raw.rename(columns={
                        "Open": "open", "High": "high",
                        "Low":  "low",  "Close": "close", "Volume": "volume",
                    })[["open", "high", "low", "close", "volume"]]
                    idx_df = compute_all(idx_raw)
                    for ts, row in idx_df.iterrows():
                        d         = ts.date() if hasattr(ts, "date") else ts
                        adx       = row.get("adx")      if "adx"      in idx_df.columns else None
                        ema_stack = row.get("ema_stack") if "ema_stack" in idx_df.columns else None
                        if adx is not None and not pd.isna(adx):
                            if adx < 20:
                                regime_dict[d] = "RANGING"
                            else:
                                regime_dict[d] = (
                                    "TRENDING_UP" if (ema_stack or 0) >= 0 else "TRENDING_DOWN"
                                )
                    log.info(
                        f"backtest.{label}_loaded",
                        trading_days  = len(regime_dict),
                        trending_up   = sum(1 for v in regime_dict.values() if v == "TRENDING_UP"),
                        trending_down = sum(1 for v in regime_dict.values() if v == "TRENDING_DOWN"),
                        ranging       = sum(1 for v in regime_dict.values() if v == "RANGING"),
                    )
            except Exception as e:
                log.warning(f"backtest.{label}_load_failed", error=str(e))

        # ── Sector indices (optional) ─────────────────────────────────────────
        if self._enable_sector_filter:
            await self._load_sector_indices()

    async def _load_sector_indices(self) -> None:
        """
        Load sectoral index daily closes for the backtest period, compute
        20-day ROC per date, and cache in _sector_roc_by_date.

        Called only when enable_sector_filter=True.
        Failures are silent — if a sector fails to load, the filter is
        simply skipped for stocks in that sector (no false blocks).
        """
        import yfinance as yf
        from services.data_ingestion.nifty500_instruments import (
            SECTOR_INDEX_MAP,
            get_symbol_sector_map,
        )

        self._symbol_to_sector = get_symbol_sector_map()

        load_start = self._start - timedelta(days=90)   # extra buffer for ROC-20
        load_end   = (self._end + timedelta(days=1)).isoformat()

        loaded = 0
        for sector, yf_ticker in SECTOR_INDEX_MAP.items():
            try:
                raw = yf.Ticker(yf_ticker).history(
                    start       = load_start.isoformat(),
                    end         = load_end,
                    interval    = "1d",
                    auto_adjust = True,
                )
                if raw.empty:
                    log.warning("backtest.sector_no_data", sector=sector, ticker=yf_ticker)
                    continue

                raw.index = pd.to_datetime(raw.index)
                if raw.index.tz is not None:
                    raw.index = raw.index.tz_convert("Asia/Kolkata").tz_localize(None)

                closes = raw["Close"].ffill()

                # ROC-20: percentage change over 20 trading days
                roc20 = (closes / closes.shift(20) - 1) * 100

                roc_map: dict[date, float] = {}
                for ts, val in roc20.items():
                    if not pd.isna(val):
                        d = ts.date() if hasattr(ts, "date") else ts
                        roc_map[d] = round(float(val), 2)

                self._sector_roc_by_date[sector] = roc_map
                loaded += 1

            except Exception as e:
                log.warning("backtest.sector_load_failed", sector=sector, ticker=yf_ticker, error=str(e))

        log.info(
            "backtest.sector_indices_loaded",
            loaded  = loaded,
            total   = len(SECTOR_INDEX_MAP),
            sectors = list(self._sector_roc_by_date.keys()),
        )

    # ── Per-symbol ────────────────────────────────────────────────────────────

    async def _backtest_symbol(self, symbol: str) -> list[SimulatedTrade]:
        # Load data for all timeframes
        data: dict[str, pd.DataFrame] = {}
        for tf in self._timeframes:
            df = await self._load_data(symbol, tf)
            if df is not None and not df.empty:
                data[tf] = df

        if not data:
            log.warning("backtest.no_data", symbol=symbol)
            return []

        # ── Pre-compute indicators ONCE per timeframe (major speedup) ─────────
        precomputed: dict[str, pd.DataFrame] = {}
        for tf, df in data.items():
            try:
                precomputed[tf] = compute_all(df)
            except Exception as e:
                log.warning("backtest.indicator_failed", symbol=symbol, tf=tf, error=str(e))

        if not precomputed:
            return []

        # ── Trigger TF is the primary scanning frame ───────────────────────────
        # trigger_tf: the candle loop drives entry timing.
        # setup_tf  : provides directional bias — must confirm direction before
        #             a trigger signal is acted upon.
        trigger_tf = self._trigger_tf if self._trigger_tf in precomputed else list(precomputed.keys())[0]
        primary_df = precomputed[trigger_tf]

        # Filter to backtest date range
        primary_df = primary_df[
            (primary_df.index.date >= self._start) &
            (primary_df.index.date <= self._end)
        ]
        if primary_df.empty:
            return []

        trades:         list[SimulatedTrade] = []
        # Context window: daily needs fewer bars than intraday (indicators pre-computed
        # on full history so EMA-200 is always correct even in a small tail slice).
        window = 60 if trigger_tf == "1day" else 200

        # Position tracking: one open trade at a time per symbol.
        in_trade:       bool = False
        exit_after_idx: int  = -1

        # ── Dead Cat Bounce (DCB) state machine ───────────────────────────────
        # After a BREAKOUT_LOW breakdown, we often see a 1–3 bar bounce back up
        # (short covering) before the real move down continues. Entering at the
        # initial breakdown frequently gets stopped out by the bounce. Instead:
        #   State 0 (IDLE):    No active breakdown tracked.
        #   State 1 (BROKEN):  Breakdown bar seen. Track bounce high. Block entry.
        #   State 2 (BOUNCED): Price bounced ≥ 0.4× ATR above breakdown level.
        #                      Next bar that closes back below breakdown = entry.
        # State resets after 8 bars (bounce window expired) or a trade is entered.
        _dcb_state:          int   = 0     # 0=IDLE, 1=BROKEN, 2=BOUNCED
        _dcb_breakdown_price: float = 0.0
        _dcb_breakdown_atr:   float = 0.0
        _dcb_bounce_high:     float = 0.0
        _dcb_bar_count:       int   = 0    # bars since breakdown

        detector   = self._signal_engine._detector
        reg_filter = self._signal_engine._filter

        for i in range(window, len(primary_df)):
            # ── Release position once its exit candle is passed ───────────────
            if in_trade:
                if i > exit_after_idx:
                    in_trade = False
                else:
                    continue

            cutoff = primary_df.index[i]

            # Build per-timeframe slices (no recomputation, just tail slicing)
            snapshot: dict[str, pd.DataFrame] = {}
            for tf, df in precomputed.items():
                tf_slice = df[df.index <= cutoff].tail(window)
                if len(tf_slice) >= 50:
                    snapshot[tf] = tf_slice

            if not snapshot:
                continue

            candle_date = cutoff.date() if hasattr(cutoff, "date") else cutoff

            # ── Level 2: VIX gate — check before any computation ─────────────
            vix_level = self._vix_level_by_date.get(candle_date, "CALM")
            if vix_level == "EXTREME":
                continue   # No trading during extreme fear (VIX > 25)

            # ── Step 1: Regime from daily TF (setup_tf for swing, or 1day if present)
            regime = "UNKNOWN"
            regime_ref_tf = self._setup_tf if self._setup_tf in snapshot else (
                "1day" if "1day" in snapshot else None
            )
            if self._regime_aware and regime_ref_tf:
                day_latest = snapshot[regime_ref_tf].iloc[-1]
                adx       = day_latest.get("adx")       if "adx"       in snapshot[regime_ref_tf].columns else None
                ema_stack = day_latest.get("ema_stack")  if "ema_stack" in snapshot[regime_ref_tf].columns else None
                if adx is not None and not pd.isna(adx):
                    regime = "TRENDING_UP"   if (ema_stack or 0) >= 0 else "TRENDING_DOWN"
                    if adx < 20:
                        regime = "RANGING"

            # If VIX is HIGH (20–25), override regime → HIGH_VOLATILITY so only
            # the safest signals (VWAP_RECLAIM) are allowed by the regime filter.
            if vix_level == "HIGH":
                regime = "HIGH_VOLATILITY"

            if self._regime_aligned_only and regime in ("RANGING", "UNKNOWN", "HIGH_VOLATILITY"):
                continue

            _regime_threshold_boost = 0   # default; overridden in long-only BULLISH block

            # ── Level 1: Market regime (Nifty) must agree with stock regime ───
            # If Nifty is trending up but this stock is trending down (or vice
            # versa), the stock is fighting the market — skip it entirely.
            market_regime = self._market_regime_by_date.get(candle_date)
            if market_regime and market_regime not in ("RANGING", "UNKNOWN"):
                if regime not in ("RANGING", "UNKNOWN") and regime != market_regime:
                    continue   # Stock contradicts market direction → skip

            # ── Step 2: Setup TF directional bias ────────────────────────────
            setup_bias = self._get_setup_bias(snapshot, regime)
            if setup_bias is None:
                continue

            # ── System is long-only: drop all BEARISH signals ────────────────
            if setup_bias == Direction.BEARISH:
                continue

            # ── Sector ROC gate (Phase 1 — opt-in via enable_sector_filter) ────
            # Rule:
            #   SHORT + sector_roc_20 > +1%  → block (sector tailwind fights the short)
            #   LONG  + sector_roc_20 < -3%  → reduce confidence 15% (headwind; may
            #                                   drop below min_confidence threshold)
            # No sector data for this symbol/date = gate is skipped, not blocked.
            if self._enable_sector_filter:
                _sector = self._symbol_to_sector.get(symbol)
                if _sector:
                    _sector_roc = self._sector_roc_by_date.get(_sector, {}).get(candle_date)
                    if _sector_roc is not None:
                        if setup_bias == Direction.BEARISH and _sector_roc > 1.0:
                            # Sector trending up → shorting against sector tide → skip
                            log.debug(
                                "backtest.sector_filter_blocked_short",
                                symbol=symbol, sector=_sector, sector_roc=_sector_roc,
                            )
                            continue
                        # For longs fighting sector headwind: mark so confluence can use it
                        # (we'll reduce confidence in the signal after detection)
                        _sector_headwind = (
                            setup_bias == Direction.BULLISH and _sector_roc < -3.0
                        )
                    else:
                        _sector_headwind = False
                else:
                    _sector_headwind = False
            else:
                _sector_headwind = False

            # ── Long trades: allow across all regimes, stock-level check ─────
            # System is long-only. BULLISH trades allowed in any Nifty regime.
            # In RANGING / TRENDING_DOWN: stock must be in its own uptrend
            # (above 200 EMA or positive EMA stack) — no Nifty tailwind, so
            # the stock itself must carry the setup.
            if setup_bias == Direction.BULLISH:
                _nifty_regime = self._market_regime_by_date.get(candle_date)

                if _nifty_regime in ("RANGING", "TRENDING_DOWN"):
                    _ind    = {}
                    _ref_tf = self._setup_tf if self._setup_tf in snapshot else (
                              "1day" if "1day" in snapshot else None)
                    if _ref_tf:
                        _last = snapshot[_ref_tf].iloc[-1]
                        _ind  = {col: _last.get(col) for col in snapshot[_ref_tf].columns}
                    _above_200 = bool(_ind.get("above_200ema", False))
                    _ema_stack = int(_ind.get("ema_stack") or 0)
                    if not (_above_200 or _ema_stack > 0):
                        continue   # Stock not in own uptrend → skip

                # Regime-based confidence threshold boost
                _regime_threshold_boost = {
                    "TRENDING_UP":   0,
                    "RANGING":       5,
                    "TRENDING_DOWN": 12,
                    "UNKNOWN":       5,
                }.get(_nifty_regime or "UNKNOWN", 5)
            else:
                _regime_threshold_boost = 0

            # ── Segment-specific regime gate ──────────────────────────────────
            # Nifty 50 can be trending up while midcap/smallcap indices are
            # correcting (e.g. early 2025). Gate long trades per segment index
            # to avoid buying mid/small caps into a deteriorating tape.
            if setup_bias == Direction.BULLISH and self._symbol_segments:
                _seg = self._symbol_segments.get(symbol, "LARGE_CAP")
                if _seg == "MID_CAP":
                    _mid_regime = self._midcap_regime_by_date.get(candle_date)
                    if _mid_regime == "TRENDING_DOWN":
                        continue
                elif _seg == "SMALL_CAP":
                    _small_regime = self._smallcap_regime_by_date.get(candle_date)
                    if _small_regime in ("TRENDING_DOWN", "RANGING"):
                        continue

            # ── DCB state machine: advance every candle (before signal check) ──
            _candle = primary_df.iloc[i]
            _bar_high = float(_candle.get("high", 0) or 0)
            if _dcb_state in (1, 2):
                _dcb_bar_count += 1
                if _bar_high > _dcb_bounce_high:
                    _dcb_bounce_high = _bar_high
                # Transition 1→2: bounce ≥ 0.4× ATR above breakdown
                if _dcb_state == 1 and _dcb_breakdown_atr > 0:
                    if (_dcb_bounce_high - _dcb_breakdown_price) >= 0.4 * _dcb_breakdown_atr:
                        _dcb_state = 2
                # Expire after 8 bars with no retest
                if _dcb_bar_count >= 8:
                    _dcb_state = 0; _dcb_breakdown_price = 0.0
                    _dcb_bounce_high = 0.0; _dcb_bar_count = 0

            # ── Step 3: Trigger TF signals (entry confirmation) ──────────────
            # Only signals on the trigger TF that match the setup bias are considered.
            all_signals: list[Signal] = []
            if trigger_tf in snapshot:
                raw_sigs = detector.detect(snapshot[trigger_tf], symbol, trigger_tf, pre_computed=True)
                all_signals = [s for s in raw_sigs if s.direction == setup_bias]

            if not all_signals:
                continue

            # Drop disabled signal types
            if self._disabled_signals:
                all_signals = [s for s in all_signals if s.signal_type.value not in self._disabled_signals]
            if not all_signals:
                continue

            # Regime filter (removes signal types not suited for this regime)
            all_signals = reg_filter.apply(all_signals, regime)

            # Swing mode: strip intraday-only signals (VWAP/ORB have no meaning
            # across multi-day holds — VWAP resets at 9:15 each day, ORB is a
            # 9:15-9:30 construct). Consistently ₹-ve across all backtest runs.
            if self._trading_mode == "swing":
                all_signals = [
                    s for s in all_signals
                    if s.signal_type.value not in _INTRADAY_ONLY_SIGNALS
                ]

            # Strip signals with no standalone directional edge.
            # Keep full list for confluence multi_signal counting, but the
            # entry trigger must come from the filtered list only.
            # (data: HIGH_RVOL 26% WR ₹-8,467 | BULL_FLAG 0% | SHOOTING_STAR 9%)
            signals_for_confluence = all_signals   # includes disabled-as-entry (multi_signal boost)
            all_signals = [
                s for s in all_signals
                if s.signal_type.value not in _DISABLED_AS_ENTRY
            ]

            all_signals.sort(key=lambda s: s.confidence, reverse=True)

            if not all_signals:
                continue

            top = all_signals[0]

            # ── Segment-aware signal quality gate ────────────────────────────
            segment = (self._symbol_segments or {}).get(symbol, "LARGE_CAP")

            # 1. RVOL minimum by segment — research shows mid/small need higher confirmation
            atr_key_rvol = f"atr_{self._signal_engine._detector._cfg.atr_period}"
            top_rvol = top.indicators.get("rvol", 1.0) or 1.0
            min_rvol = {"LARGE_CAP": 0.0, "MID_CAP": 1.5, "SMALL_CAP": 2.0}.get(segment, 0.0)
            # Only apply RVOL gate to bullish signals (longs) — shorts handled separately
            if top.direction == Direction.BULLISH and top_rvol < min_rvol:
                continue

            # 2. Disable Double Top/Bottom on small caps — manipulation prone (41-50% WR)
            if segment == "SMALL_CAP" and top.signal_type in (
                SignalType.DOUBLE_TOP, SignalType.DOUBLE_BOTTOM
            ):
                continue

            # 3. Reversal patterns (DOUBLE_BOTTOM, MORNING_STAR) are knife-catches
            # when Nifty is in a long-term bear phase (200 EMA falling).
            # Gate: only trade these when the Nifty 200 EMA is rising (bull phase).
            # ADX-based regime is too noisy (flips on single days); the 200 EMA
            # direction is a stable, slow-moving indicator of the macro environment.
            if top.signal_type in (SignalType.DOUBLE_BOTTOM, SignalType.MORNING_STAR):
                _nifty_bull = self._nifty_200ema_rising_by_date.get(candle_date)
                if _nifty_bull is False:   # 200 EMA falling = bear phase → block
                    continue

            # ── Step 4: Quality gates ─────────────────────────────────────────
            # Sector headwind: long into sector with ROC-20 < -3% → confidence -15%
            # Applied before min_confidence check so the threshold naturally filters.
            if _sector_headwind:
                adjusted_confidence = int(top.confidence * 0.85)
                log.debug(
                    "backtest.sector_headwind_penalty",
                    symbol=symbol, original=top.confidence, adjusted=adjusted_confidence,
                )
                top = Signal(
                    trading_symbol  = top.trading_symbol,
                    timeframe       = top.timeframe,
                    signal_type     = top.signal_type,
                    direction       = top.direction,
                    confidence      = adjusted_confidence,
                    price_at_signal = top.price_at_signal,
                    indicators      = top.indicators,
                    notes           = (top.notes or "") + " | sector_headwind",
                )

            _effective_min_confidence = self._min_confidence + _regime_threshold_boost
            if top.confidence < _effective_min_confidence:
                continue

            # ── DCB intercept for BREAKOUT_LOW signals ────────────────────────
            if top.signal_type == SignalType.BREAKOUT_LOW:
                if _dcb_state == 0:
                    # First breakdown bar — record it and skip entry this candle
                    _dcb_state = 1
                    _dcb_breakdown_price = top.price_at_signal
                    _atr_key = f"atr_{self._signal_engine._detector._cfg.atr_period}"
                    _dcb_breakdown_atr = float(
                        top.indicators.get(_atr_key) or top.indicators.get("atr_14", 0) or 0
                    )
                    _dcb_bounce_high = _bar_high
                    _dcb_bar_count   = 0
                    continue   # Block entry on initial breakdown
                elif _dcb_state == 1:
                    # Still in breakdown phase, no bounce yet — still block
                    continue
                elif _dcb_state == 2:
                    # Bounce occurred and price is retesting below breakdown — allow!
                    # Give a +20 confidence bonus for the confirmed retest pattern
                    top = Signal(
                        trading_symbol  = top.trading_symbol,
                        timeframe       = top.timeframe,
                        signal_type     = top.signal_type,
                        direction       = top.direction,
                        confidence      = min(top.confidence + 20, 100),
                        price_at_signal = top.price_at_signal,
                        indicators      = top.indicators,
                        notes           = (top.notes or "") + " | DCB retest confirmed",
                    )
                    _dcb_state = 0   # Reset state — trade entered

            # ── Confluence scoring ─────────────────────────────────────────────
            # Require at least MIN_CONFLUENCE_SCORE (6/10) across 5 factors:
            # signal quality, volume, trend alignment, momentum, multi-signal.
            # This replaces the blunt min_confirming_signals check and ensures
            # we only trade when multiple independent factors agree.
            confluence = self._score_confluence(top, signals_for_confluence)
            if not confluence.passed:
                log.debug(
                    "backtest.confluence_failed",
                    symbol  = symbol,
                    signal  = top.signal_type.value,
                    score   = confluence.total,
                    factors = confluence.to_dict(),
                )
                continue

            # Legacy multi-signal confirmation (still honoured if set > 1)
            if self._min_confirming_signals > 1:
                confirming_types = {s.signal_type for s in all_signals}
                if len(confirming_types) < self._min_confirming_signals:
                    continue

            atr_key = f"atr_{self._signal_engine._detector._cfg.atr_period}"
            atr = top.indicators.get(atr_key) or top.indicators.get("atr_14", 0)
            if not atr:
                continue

            risk_dec = await self._risk_engine.evaluate(
                symbol      = symbol,
                direction   = top.direction.value,
                entry_price = top.price_at_signal,
                atr         = atr,
            )
            if not risk_dec.approved:
                continue

            # ── Swing mode: widen stop/target to 2×/6× ATR → 1:3 R:R ─────────
            # Default risk engine uses 1.5×/3× (1:2 R:R), calibrated for 15min.
            # Swing trades on 1H need more room to breathe over 2–5 days,
            # and a 1:3 target lets winners compensate for the wider stop.
            if self._trading_mode == "swing":
                is_long  = top.direction == Direction.BULLISH
                entry    = top.price_at_signal
                new_stop = round(entry - 2.0 * atr if is_long else entry + 2.0 * atr, 2)
                new_tgt  = round(entry + 6.0 * atr if is_long else entry - 6.0 * atr, 2)
                risk_dec = RiskDecision(
                    approved      = True,
                    reason        = "swing_1_3_rr",
                    position_size = risk_dec.position_size,
                    risk_amount   = risk_dec.risk_amount,
                    stop_loss     = new_stop,
                    target        = new_tgt,
                )

            # ── Intraday short: widen stop to 2×ATR / target to 5×ATR (1:2.5) ──
            # Default 1.5×/3× is too tight for intraday shorts — gap-up opens
            # blow through stops overnight. 2×/5× gives more breathing room
            # while still offering better than 1:2 R:R.
            elif self._trading_mode == "intraday" and top.direction == Direction.BEARISH:
                entry    = top.price_at_signal
                new_stop = round(entry + 2.0 * atr, 2)
                new_tgt  = round(entry - 5.0 * atr, 2)
                risk_dec = RiskDecision(
                    approved      = True,
                    reason        = "intraday_short_2_5_rr",
                    position_size = risk_dec.position_size,
                    risk_amount   = risk_dec.risk_amount,
                    stop_loss     = new_stop,
                    target        = new_tgt,
                )

            # ── Step 5: Simulate exit on raw OHLCV trigger-TF candles ─────────
            raw_trigger = data.get(trigger_tf, primary_df)
            future_raw  = raw_trigger[raw_trigger.index > cutoff]
            trade = self._simulate_exit(
                signal     = top,
                risk_dec   = risk_dec,
                future_df  = future_raw,
                regime     = regime,
                entry_date = cutoff.date() if hasattr(cutoff, "date") else cutoff,
            )
            if trade:
                trade.confluence_score   = confluence.total
                trade.confluence_factors = confluence.to_dict()
                trades.append(trade)
                in_trade       = True
                exit_after_idx = i + trade.holding_candles

        return trades

    def _score_confluence(
        self,
        top:        Signal,
        all_signals: list[Signal],
    ) -> ConfluenceScore:
        """
        Score the trade setup across 5 independent factors (max 10 points).
        Requires >= MIN_CONFLUENCE_SCORE to trade.

        Factor breakdown:
          signal_strength  : confidence level + pattern quality  (0-2)
          volume           : RVOL vs threshold                   (0-2)
          trend_alignment  : EMA stack + 200 EMA position        (0-2)
          momentum         : RSI in the sweet-spot for direction  (0-2)
          multi_signal     : distinct signal types agreeing       (0-2)
        """
        score = ConfluenceScore()
        ind   = top.indicators
        bull  = top.direction == Direction.BULLISH

        # ── Factor 1: Signal strength ──────────────────────────────────────
        conf     = top.confidence
        hq       = top.signal_type.value in _HIGH_QUALITY_SIGNALS
        if conf >= 75 and hq:
            score.signal_strength = 2
        elif conf >= 65:
            score.signal_strength = 1
        # else 0

        # ── Factor 2: Volume (RVOL) ────────────────────────────────────────
        rvol = float(ind.get("rvol") or 1.0)
        if rvol >= 2.5:
            score.volume = 2
        elif rvol >= 1.5:
            score.volume = 1
        # else 0

        # ── Factor 3: Trend alignment ──────────────────────────────────────
        # above_200ema: True/False (price vs 200 EMA)
        # ema_stack:    +1 = all fast EMAs bullishly stacked, -1 = bearishly stacked
        above_200 = bool(ind.get("above_200ema", False))
        ema_stack = int(ind.get("ema_stack") or 0)
        if bull:
            trend_aligned   = above_200
            stack_aligned   = ema_stack >= 0
        else:
            trend_aligned   = not above_200
            stack_aligned   = ema_stack <= 0

        if trend_aligned and stack_aligned:
            score.trend_alignment = 2
        elif trend_aligned or stack_aligned:
            score.trend_alignment = 1
        # else 0

        # ── Factor 4: Momentum (RSI sweet spot) ───────────────────────────
        # Ideal: not yet extended but momentum in our direction.
        # BULLISH sweet spot: 40–65 (room to run, not overbought)
        # BEARISH sweet spot: 35–60 (room to fall, not oversold)
        rsi = float(ind.get("rsi_14") or ind.get("rsi") or 50.0)
        if bull:
            if 40.0 <= rsi <= 65.0:
                score.momentum = 2
            elif (30.0 <= rsi < 40.0) or (65.0 < rsi <= 72.0):
                score.momentum = 1
            # else 0 — RSI > 72 (chasing overbought) or < 30 (knife-catch)
        else:
            if 35.0 <= rsi <= 60.0:
                score.momentum = 2
            elif (28.0 <= rsi < 35.0) or (60.0 < rsi <= 70.0):
                score.momentum = 1
            # else 0 — RSI < 28 (oversold, bounce risk) or > 70 (chasing)

        # ── Factor 5: Multi-signal agreement ──────────────────────────────
        # Count distinct signal types on the trigger TF that agree with direction
        distinct_types = len({s.signal_type for s in all_signals})
        if distinct_types >= 3:
            score.multi_signal = 2
        elif distinct_types == 2:
            score.multi_signal = 1
        # else 0 — single signal only

        return score

    def _get_setup_bias(
        self, snapshot: dict[str, pd.DataFrame], regime: str
    ) -> Direction | None:
        """
        Determine directional bias from the setup TF.

        For swing (setup=1day):
          - Regime TRENDING_UP  → BULLISH
          - Regime TRENDING_DOWN→ BEARISH
          - Ranging / Unknown   → check setup TF EMA stack as tiebreaker

        For intraday (setup=1hr):
          - Same logic, but EMA stack on 1hr drives the bias when regime is unclear.

        Returns None if no clear directional bias → trade skipped.
        """
        # Primary: regime is already derived from the setup TF
        if regime == "TRENDING_UP":
            return Direction.BULLISH
        if regime == "TRENDING_DOWN":
            return Direction.BEARISH

        # Tiebreaker: EMA stack on the setup TF
        if self._setup_tf in snapshot:
            setup_df = snapshot[self._setup_tf]
            if "ema_stack" in setup_df.columns:
                stack_val = setup_df["ema_stack"].iloc[-1]
                if not pd.isna(stack_val):
                    s = int(stack_val)
                    if s == 1:  return Direction.BULLISH
                    if s == -1: return Direction.BEARISH

            # Also check: are there strong directional signals on the setup TF?
            # (e.g. a Double Bottom or Bull Flag fired on the daily chart)
            setup_sigs = self._signal_engine._detector.detect(
                setup_df, "", self._setup_tf, pre_computed=True
            )
            if setup_sigs:
                bull = sum(1 for s in setup_sigs if s.direction == Direction.BULLISH)
                bear = sum(1 for s in setup_sigs if s.direction == Direction.BEARISH)
                if bull > bear:   return Direction.BULLISH
                if bear > bull:   return Direction.BEARISH

        return None  # No clear bias — skip this candle

    def _simulate_exit(
        self,
        signal,
        risk_dec,
        future_df:  pd.DataFrame,
        regime:     str,
        entry_date: date,
    ) -> SimulatedTrade | None:
        if future_df.empty:
            return None

        is_long    = signal.direction == Direction.BULLISH
        entry_price = future_df.iloc[0]["open"]   # Enter at next candle open
        stop_loss   = risk_dec.stop_loss
        target      = risk_dec.target

        exit_price  = entry_price
        exit_reason = "OPEN"
        hold        = 0

        # ── Trailing stop state ───────────────────────────────────────────────
        # Once price hits milestones, we trail the stop to lock in profits
        # rather than exiting hard at a fixed target. This lets winners run
        # to multibagger territory (1:5, 1:8+) while protecting capital.
        #
        # Milestones (based on initial risk = entry - stop_loss):
        #   Hit 1:2  → trail stop to breakeven (entry price)
        #   Hit 1:3  → trail stop to +1R (entry + 1× initial risk)
        #   Hit 1:5  → trail stop to +3R (entry + 3× initial risk)
        #   Hit 1:8  → trail stop to +5R (entry + 5× initial risk)
        #
        # No hard target — trade stays open until trailing stop is hit or
        # MAX_HOLD expires. The 'target' from risk engine is removed as a
        # hard exit — it becomes the first milestone trigger only.
        initial_risk = abs(entry_price - stop_loss)
        trailing_stop = stop_loss   # starts at original stop, moves up only

        for idx, (ts, candle) in enumerate(future_df.iterrows()):
            hold += 1

            if is_long:
                # Update trailing stop based on how far price has moved
                if initial_risk > 0:
                    move = candle["high"] - entry_price
                    r_multiple = move / initial_risk
                    if r_multiple >= 8:
                        new_trail = entry_price + 5 * initial_risk
                    elif r_multiple >= 5:
                        new_trail = entry_price + 3 * initial_risk
                    elif r_multiple >= 3:
                        new_trail = entry_price + 1 * initial_risk
                    elif r_multiple >= 2:
                        new_trail = entry_price   # breakeven
                    else:
                        new_trail = stop_loss
                    trailing_stop = max(trailing_stop, new_trail)

                # Check trailing stop hit
                if candle["low"] <= trailing_stop:
                    exit_price  = trailing_stop
                    exit_reason = "STOP" if trailing_stop == stop_loss else "TRAIL_STOP"
                    break
            else:
                # SHORT — trailing stop moves down
                if initial_risk > 0:
                    move = entry_price - candle["low"]
                    r_multiple = move / initial_risk
                    if r_multiple >= 8:
                        new_trail = entry_price - 5 * initial_risk
                    elif r_multiple >= 5:
                        new_trail = entry_price - 3 * initial_risk
                    elif r_multiple >= 3:
                        new_trail = entry_price - 1 * initial_risk
                    elif r_multiple >= 2:
                        new_trail = entry_price   # breakeven
                    else:
                        new_trail = stop_loss
                    trailing_stop = min(trailing_stop, new_trail)

                if candle["high"] >= trailing_stop:
                    exit_price  = trailing_stop
                    exit_reason = "STOP" if trailing_stop == stop_loss else "TRAIL_STOP"
                    break

            # EOD exit: intraday only — forced close at 15:20 IST.
            # Swing trades hold overnight so EOD is disabled.
            if self._eod_exit:
                if hasattr(ts, "time") and ts.time().hour == 15 and ts.time().minute >= 20:
                    exit_price  = candle["close"]
                    exit_reason = "EOD"
                    break

            # Max hold cap (mode-dependent: 20 candles intraday, 30 candles swing)
            if hold >= self._max_hold:
                exit_price  = candle["close"]
                exit_reason = "MAX_HOLD"
                break

        if exit_reason == "OPEN":
            return None   # Trade never resolved — skip

        multiplier = 1 if is_long else -1
        pnl        = (exit_price - entry_price) * multiplier * risk_dec.position_size
        pnl_pct    = (exit_price - entry_price) / entry_price * multiplier * 100

        return SimulatedTrade(
            symbol            = signal.trading_symbol,
            signal_type       = signal.signal_type.value,
            direction         = "LONG" if is_long else "SHORT",
            timeframe         = signal.timeframe,
            entry_date        = entry_date,
            entry_price       = round(entry_price, 2),
            stop_loss         = round(stop_loss, 2),
            target            = round(target, 2),
            exit_price        = round(exit_price, 2),
            exit_reason       = exit_reason,
            pnl               = round(pnl, 2),
            pnl_pct           = round(pnl_pct, 2),
            holding_candles   = hold,
            signal_confidence = signal.confidence,
            regime            = regime,
            risk_amount       = round(risk_dec.risk_amount, 2),
            position_size     = risk_dec.position_size,
        )

    # ── Data Loading ──────────────────────────────────────────────────────────

    async def _load_data(self, symbol: str, timeframe: str) -> pd.DataFrame | None:
        """Try TimescaleDB first, fall back to yfinance."""
        df = await self._load_from_db(symbol, timeframe)
        if df is not None and not df.empty:
            return df
        return await self._load_from_yfinance(symbol, timeframe)

    async def _load_from_db(self, symbol: str, timeframe: str) -> pd.DataFrame | None:
        try:
            from database.connection import get_db_session
            from sqlalchemy import text

            # Extra buffer days for indicator warm-up
            load_start = self._start - timedelta(days=60)

            async with get_db_session() as session:
                result = await session.execute(
                    text("""
                        SELECT ts, open, high, low, close, volume
                        FROM ohlcv_candles
                        WHERE trading_symbol = :sym
                          AND timeframe      = :tf
                          AND ts             >= :start
                          AND ts             <= :end
                        ORDER BY ts ASC
                    """),
                    {
                        "sym":   symbol,
                        "tf":    timeframe,
                        "start": datetime.combine(load_start, datetime.min.time()),
                        "end":   datetime.combine(self._end, datetime.max.time()),
                    },
                )
                rows = result.fetchall()
                if not rows:
                    return None
                df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
                df["ts"] = pd.to_datetime(df["ts"])
                df = df.set_index("ts")
                return df
        except Exception as e:
            log.debug("backtest.db_load_failed", symbol=symbol, tf=timeframe, error=str(e))
            return None

    async def _load_from_yfinance(
        self, symbol: str, timeframe: str
    ) -> pd.DataFrame | None:
        """
        Download from yfinance as a fallback.
        NSE symbols need '.NS' suffix; uses interval mapping for timeframes.
        """
        try:
            import yfinance as yf

            # yfinance interval codes
            interval_map = {
                "1min":  "1m",
                "5min":  "5m",
                "15min": "15m",
                "1hr":   "1h",
                "1day":  "1d",
            }
            yf_interval = interval_map.get(timeframe)
            if not yf_interval:
                return None

            yf_symbol = f"{symbol}.NS"

            # yfinance intraday limits: 1m/5m/15m → 60 days, 1h → 730 days
            load_start = self._start - timedelta(days=60)
            if yf_interval in ("1m", "5m", "15m"):
                load_start = max(load_start, date.today() - timedelta(days=59))
            elif yf_interval == "1h":
                load_start = max(load_start, date.today() - timedelta(days=729))

            log.info("backtest.yfinance_download", symbol=yf_symbol, tf=timeframe)
            ticker = yf.Ticker(yf_symbol)
            df = ticker.history(
                start    = load_start.isoformat(),
                end      = (self._end + timedelta(days=1)).isoformat(),
                interval = yf_interval,
                auto_adjust = True,
            )

            if df.empty:
                return None

            df.index = pd.to_datetime(df.index)
            if df.index.tz is not None:
                df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)

            df = df.rename(columns={
                "Open": "open", "High": "high",
                "Low": "low",   "Close": "close", "Volume": "volume",
            })
            return df[["open", "high", "low", "close", "volume"]]

        except Exception as e:
            log.warning("backtest.yfinance_failed", symbol=symbol, error=str(e))
            return None
