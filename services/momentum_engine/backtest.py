"""
services/momentum_engine/backtest.py
──────────────────────────────────────
Momentum engine backtester — long-only, TRENDING_UP Nifty markets.

Architecture mirrors the reversal backtest engine but with key differences:
  - Only fires when Nifty regime = TRENDING_UP (not RANGING / TRENDING_DOWN)
  - Uses MomentumDetector (Darvas, 52wk, volume thrust, EMA ribbon, bull momentum)
  - Momentum-calibrated confluence scoring (RSI 60-75 sweet spot)
  - Wider trailing stops (momentum trends run further than reversals)
  - Daily timeframe only (swing trades — momentum plays need time to develop)
  - Segment gates: MID_CAP blocked when midcap index TRENDING_DOWN
                   SMALL_CAP blocked when smallcap TRENDING_DOWN or RANGING

Stop / target (momentum-calibrated):
  - Stop:   1.5× ATR below entry (tighter than reversal's 2× — we're buying strength)
  - Target: 7× ATR above entry   (1:4.7 R:R — momentum trades run much further)

Trailing stop milestones (same as reversal but wider):
  1:2  → breakeven
  1:3  → +1R
  1:6  → +3R
  1:10 → +5R  (let the real winners run)

Usage:
    engine = MomentumBacktestEngine(
        symbols    = ["RELIANCE", "TCS", ...],
        start_date = date(2025, 1, 1),
        end_date   = date(2025, 6, 30),
    )
    result = await engine.run()
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import structlog
import yfinance as yf

from services.technical_engine.indicators import compute_all
from services.momentum_engine.signals import (
    MomentumDetector,
    MomentumSignalType,
    score_momentum_confluence,
)

log = structlog.get_logger(__name__)

# Signals disabled as standalone entries (still detected for confluence scoring):
#   BREAKOUT_52W: 25% WR, ₹-25,972 — buying at exact 52wk high = chasing extended move
#   EMA_RIBBON:   23.1% WR, ₹-13,320 — EMA fan alone = too early, trend not confirmed
# Both still boost multi_signal confluence when combined with DARVAS_BREAKOUT.
_ENTRY_DISABLED: frozenset[MomentumSignalType] = frozenset({
    MomentumSignalType.BREAKOUT_52W,
    MomentumSignalType.EMA_RIBBON,
    MomentumSignalType.BULL_MOMENTUM,
})

# Max hold on daily TF: 20 trading days (~4 calendar weeks)
# Momentum trends typically resolve or stall within this window.
MAX_HOLD_DAYS = 20

# Position sizing: matches config/settings.py — ₹1,00,000 capital, 2% risk = ₹2,000/trade
from config.settings import settings as _settings
NOTIONAL_CAPITAL = int(_settings.total_capital)   # ₹1,00,000
RISK_PCT         = _settings.max_risk_per_trade_pct / 100  # 2%


@dataclass
class MomentumTrade:
    symbol:           str
    signal_type:      str
    entry_date:       date
    entry_price:      float
    stop_loss:        float
    target:           float
    exit_price:       float       = 0.0
    exit_reason:      str         = "OPEN"  # TRAIL_STOP | STOP | MAX_HOLD
    pnl:              float       = 0.0
    pnl_pct:          float       = 0.0
    holding_days:     int         = 0
    position_size:    int         = 0
    regime:           str         = "UNKNOWN"
    confluence_score: int         = 0
    confluence_factors: dict      = field(default_factory=dict)
    # Raw signal stats
    rvol:             float       = 0.0
    rsi:              float       = 0.0
    adx:              float       = 0.0


@dataclass
class MomentumBacktestResult:
    trades:     list[MomentumTrade] = field(default_factory=list)
    symbols:    list[str]           = field(default_factory=list)
    start_date: date | None         = None
    end_date:   date | None         = None


class MomentumBacktestEngine:
    """
    Backtests the momentum engine over a symbol universe and date range.

    Key difference from reversal engine:
      - Entry ONLY when Nifty regime = TRENDING_UP
      - Signals from MomentumDetector (not SignalDetector)
      - All trades are LONG (momentum engine is long-only)
    """

    def __init__(
        self,
        symbols:              list[str],
        start_date:           date,
        end_date:             date,
        symbol_segments:      dict[str, str] | None = None,
        min_score:            int  = 8,    # Min confluence score to trade
        max_score:            int  = 8,    # Max confluence score — above this = overextended
        min_confidence:       int  = 65,   # Min signal confidence to trade
        enable_sector_filter: bool = False,
    ) -> None:
        self._symbols         = symbols
        self._start           = start_date
        self._end             = end_date
        self._symbol_segments = symbol_segments or {}
        self._min_score       = min_score
        self._max_score       = max_score
        self._min_confidence  = min_confidence

        self._detector = MomentumDetector()

        # Market-wide lookup tables (populated in run())
        self._nifty_regime_by_date:      dict[date, str]   = {}   # TRENDING_UP/DOWN/RANGING
        self._nifty_200ema_rising:       dict[date, bool]  = {}
        self._nifty_consec_up:           dict[date, int]   = {}   # consecutive TRENDING_UP days
        self._nifty_roc20_by_date:       dict[date, float] = {}   # 20-day ROC for RS calc
        self._midcap_regime_by_date:     dict[date, str]   = {}
        self._smallcap_regime_by_date:   dict[date, str]   = {}

        # Sector filter (opt-in)
        self._enable_sector_filter = enable_sector_filter
        self._sector_roc_by_date:  dict[str, dict[date, float]] = {}
        self._symbol_to_sector:    dict[str, str] = {}

    async def run(self) -> MomentumBacktestResult:
        result = MomentumBacktestResult(
            symbols    = self._symbols,
            start_date = self._start,
            end_date   = self._end,
        )

        await self._load_market_indices()

        for symbol in self._symbols:
            log.info("momentum_bt.symbol_start", symbol=symbol)
            try:
                trades = await self._backtest_symbol(symbol)
                result.trades.extend(trades)
                log.info("momentum_bt.symbol_done", symbol=symbol, trades=len(trades))
            except Exception as e:
                log.error("momentum_bt.symbol_error", symbol=symbol, error=str(e))

        log.info(
            "momentum_bt.complete",
            symbols = len(self._symbols),
            trades  = len(result.trades),
        )
        return result

    # ── Market index loading ─────────────────────────────────────────────────

    async def _load_market_indices(self) -> None:
        # 300 days back — enough for 200 EMA to be valid on day 1 of the backtest
        load_start = self._start - timedelta(days=300)
        load_end   = (self._end + timedelta(days=1)).isoformat()

        # Nifty 50 → regime + 200 EMA direction
        try:
            raw = yf.Ticker("^NSEI").history(
                start=load_start.isoformat(), end=load_end,
                interval="1d", auto_adjust=True,
            )
            if not raw.empty:
                raw.index = pd.to_datetime(raw.index)
                if raw.index.tz is not None:
                    raw.index = raw.index.tz_convert("Asia/Kolkata").tz_localize(None)
                raw = raw.rename(columns={
                    "Open": "open", "High": "high",
                    "Low":  "low",  "Close": "close", "Volume": "volume",
                })[["open", "high", "low", "close", "volume"]]

                nifty_df = compute_all(raw)

                for ts, row in nifty_df.iterrows():
                    d         = ts.date() if hasattr(ts, "date") else ts
                    adx       = row.get("adx")      if "adx"      in nifty_df.columns else None
                    ema_stack = row.get("ema_stack") if "ema_stack" in nifty_df.columns else None
                    if adx is not None and not pd.isna(adx):
                        if adx < 20:
                            self._nifty_regime_by_date[d] = "RANGING"
                        else:
                            self._nifty_regime_by_date[d] = (
                                "TRENDING_UP" if (ema_stack or 0) >= 0 else "TRENDING_DOWN"
                            )

                if "ema_200" in nifty_df.columns:
                    ema200_rising = nifty_df["ema_200"] > nifty_df["ema_200"].shift(10)
                    for ts, rising in ema200_rising.items():
                        if not pd.isna(rising):
                            d = ts.date() if hasattr(ts, "date") else ts
                            self._nifty_200ema_rising[d] = bool(rising)

                # Build consecutive TRENDING_UP counter per date
                # (sorted so we can walk forward in time)
                consec = 0
                for d in sorted(self._nifty_regime_by_date):
                    if self._nifty_regime_by_date[d] == "TRENDING_UP":
                        consec += 1
                    else:
                        consec = 0
                    self._nifty_consec_up[d] = consec

                # Nifty 20-day ROC — used for relative strength calculation
                # stock_roc20 - nifty_roc20 = how much the stock is outperforming
                nifty_roc20 = (nifty_df["close"] / nifty_df["close"].shift(20) - 1) * 100
                for ts, val in nifty_roc20.items():
                    if not pd.isna(val):
                        d = ts.date() if hasattr(ts, "date") else ts
                        self._nifty_roc20_by_date[d] = round(float(val), 2)

                log.info(
                    "momentum_bt.nifty_loaded",
                    trending_up   = sum(1 for v in self._nifty_regime_by_date.values() if v == "TRENDING_UP"),
                    trending_down = sum(1 for v in self._nifty_regime_by_date.values() if v == "TRENDING_DOWN"),
                    ranging       = sum(1 for v in self._nifty_regime_by_date.values() if v == "RANGING"),
                )
        except Exception as e:
            log.warning("momentum_bt.nifty_load_failed", error=str(e))

        # Midcap + Smallcap indices for segment gates
        for ticker, regime_dict, label in [
            ("^NSMIDCP",          self._midcap_regime_by_date,   "midcap"),
            ("NIFTYMIDCAP150.NS",  self._smallcap_regime_by_date, "smallcap"),
        ]:
            try:
                idx_raw = yf.Ticker(ticker).history(
                    start=load_start.isoformat(), end=load_end,
                    interval="1d", auto_adjust=True,
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
                            regime_dict[d] = (
                                "RANGING" if adx < 20 else
                                ("TRENDING_UP" if (ema_stack or 0) >= 0 else "TRENDING_DOWN")
                            )
                    log.info(f"momentum_bt.{label}_loaded",
                             trading_days=len(regime_dict))
            except Exception as e:
                log.warning(f"momentum_bt.{label}_load_failed", error=str(e))

        if self._enable_sector_filter:
            await self._load_sector_indices()

    async def _load_sector_indices(self) -> None:
        """
        Load sectoral index ROC-20 per date for the long-side headwind gate.
        Identical logic to the swing engine's sector loader.
        Only called when enable_sector_filter=True.
        """
        from services.data_ingestion.nifty500_instruments import (
            SECTOR_INDEX_MAP,
            get_symbol_sector_map,
        )

        self._symbol_to_sector = get_symbol_sector_map()

        load_start = self._start - timedelta(days=90)
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
                    continue

                raw.index = pd.to_datetime(raw.index)
                if raw.index.tz is not None:
                    raw.index = raw.index.tz_convert("Asia/Kolkata").tz_localize(None)

                closes = raw["Close"].ffill()
                roc20  = (closes / closes.shift(20) - 1) * 100

                roc_map: dict[date, float] = {}
                for ts, val in roc20.items():
                    if not pd.isna(val):
                        d = ts.date() if hasattr(ts, "date") else ts
                        roc_map[d] = round(float(val), 2)

                self._sector_roc_by_date[sector] = roc_map
                loaded += 1
            except Exception as e:
                log.warning("momentum_bt.sector_load_failed", sector=sector, error=str(e))

        log.info(
            "momentum_bt.sector_indices_loaded",
            loaded=loaded, total=len(SECTOR_INDEX_MAP),
            sectors=list(self._sector_roc_by_date.keys()),
        )

    # ── Per-symbol backtest ──────────────────────────────────────────────────

    async def _backtest_symbol(self, symbol: str) -> list[MomentumTrade]:
        # Download daily OHLCV
        load_start = self._start - timedelta(days=400)  # need 252 bars for 52wk high
        df = await self._fetch_daily(symbol, load_start, self._end)
        if df is None or df.empty or len(df) < 60:
            log.warning("momentum_bt.no_data", symbol=symbol)
            return []

        # ── Liquidity + micro-cap filter ─────────────────────────────────────
        # Applied on last 60 days of loaded data (recent liquidity matters more).
        # Avg volume < 1 lakh/day = illiquid (wide spreads, slippage kills edge).
        # Avg price < ₹20 = penny/micro-cap (manipulated, low float, unreliable).
        _recent = df.tail(60)
        _avg_vol   = float(_recent["volume"].mean()) if "volume" in _recent.columns else 0
        _avg_price = float(_recent["close"].mean())  if "close"  in _recent.columns else 0
        if _avg_vol < 100_000 or _avg_price < 20:
            log.debug(
                "momentum_bt.liquidity_skip",
                symbol=symbol,
                avg_vol=round(_avg_vol),
                avg_price=round(_avg_price, 1),
            )
            return []

        # Pre-compute indicators once
        try:
            df = compute_all(df)
        except Exception as e:
            log.warning("momentum_bt.indicator_failed", symbol=symbol, error=str(e))
            return []

        # Filter to backtest range for the main loop
        trade_df = df[(df.index.date >= self._start) & (df.index.date <= self._end)]
        if trade_df.empty:
            return []

        from dataclasses import replace as _dc_replace

        trades:     list[MomentumTrade] = []
        in_trade:   bool = False
        exit_after: int  = -1   # index in trade_df after which we're free

        seg = self._symbol_segments.get(symbol, "LARGE_CAP")

        for i in range(len(trade_df)):
            if in_trade:
                if i > exit_after:
                    in_trade = False
                else:
                    continue

            candle_ts   = trade_df.index[i]
            candle_date = candle_ts.date() if hasattr(candle_ts, "date") else candle_ts

            nifty_regime = self._nifty_regime_by_date.get(candle_date, "UNKNOWN")

            # ── Gate 1: Regime + Relative Strength ───────────────────────────
            # TRENDING_UP: fire freely (broad market tailwind).
            # RANGING:     only fire if stock outperforms Nifty by ≥3% (20d ROC).
            #              These are sector rotation plays: defense, sugar, capital
            #              markets etc. making new highs while index is flat.
            # TRENDING_DOWN: only fire if stock outperforms Nifty by ≥8% (20d ROC).
            #              Exceptional relative strength = genuine sector leader.
            # Firing on ALL stocks in RANGING/TRENDING_DOWN drowns good RS plays
            # in noise — RS filter cuts 80%+ of bad entries while keeping the ones
            # that actually have sector tailwind.
            _nifty_roc20 = self._nifty_roc20_by_date.get(candle_date, 0.0)
            _rs_threshold: float | None = None  # None = no RS check needed
            if nifty_regime == "RANGING":
                # RANGING market: too much noise even with RS filter (23% WR at RS>3%).
                # Skip entirely — only fire in TRENDING_UP or clear sector leaders
                # (TRENDING_DOWN with strong RS).
                continue
            elif nifty_regime == "TRENDING_DOWN":
                # TRENDING_DOWN + strong RS: genuine sector rotation leaders
                # (60% WR, avg +₹3,084 — exactly the defense/sugar/capital market plays)
                _rs_threshold = 8.0
            # TRENDING_UP and UNKNOWN: no RS threshold, proceed

            # ── Nifty context: confidence penalties ───────────────────────────
            _nifty_penalty = 0

            # Penalty 1: Nifty 200 EMA not rising → late-stage or declining market
            if not self._nifty_200ema_rising.get(candle_date, True):
                _nifty_penalty += 10

            # Penalty 2: fewer than 3 consecutive TRENDING_UP days → weak trend
            if self._nifty_consec_up.get(candle_date, 3) < 3:
                _nifty_penalty += 5

            # ── Gate 2: Segment-specific regime ──────────────────────────────
            if seg == "MID_CAP":
                mid_regime = self._midcap_regime_by_date.get(candle_date)
                if mid_regime == "TRENDING_DOWN":
                    continue
            elif seg == "SMALL_CAP":
                small_regime = self._smallcap_regime_by_date.get(candle_date)
                if small_regime in ("TRENDING_DOWN", "RANGING"):
                    continue

            # ── Gate 3: Sector headwind (opt-in via enable_sector_filter) ────────
            # Long into a sector with ROC-20 < -3% = buying against sector trend.
            # Penalise confidence by 15% rather than hard-blocking — lets strong
            # signals through while filtering borderline ones.
            _sector_headwind = False
            if self._enable_sector_filter:
                _sector = self._symbol_to_sector.get(symbol)
                if _sector:
                    _sector_roc = self._sector_roc_by_date.get(_sector, {}).get(candle_date)
                    if _sector_roc is not None and _sector_roc < -3.0:
                        _sector_headwind = True

            # ── Slice history up to current bar (no look-ahead) ───────────────
            cutoff = candle_ts
            hist   = df[df.index <= cutoff].tail(300)  # enough history for 52wk + indicators
            if len(hist) < 60:
                continue

            # ── Gate 1 (cont): Relative strength check for RANGING/TRENDING_DOWN ──
            # Computed here because we need the stock's price history.
            if _rs_threshold is not None:
                if len(hist) >= 21:
                    _stock_roc20 = float(
                        (hist["close"].iloc[-1] / hist["close"].iloc[-21] - 1) * 100
                    )
                else:
                    _stock_roc20 = 0.0
                _relative_strength = _stock_roc20 - _nifty_roc20
                if _relative_strength < _rs_threshold:
                    continue  # not outperforming enough — skip

            # ── Run momentum signal detection ─────────────────────────────────
            signals = self._detector.detect(hist, symbol)
            if not signals:
                continue

            # Apply Nifty context penalty before confidence filter
            if _nifty_penalty > 0:
                signals = [
                    _dc_replace(s, confidence=max(0, s.confidence - _nifty_penalty))
                    for s in signals
                ]

            # Filter by min confidence
            signals = [s for s in signals if s.confidence >= self._min_confidence]
            if not signals:
                continue

            # ── Confluence scoring (uses all detected signals incl. disabled-as-entry)
            confluence = score_momentum_confluence(signals)
            if not confluence.passed or confluence.total < self._min_score:
                log.debug(
                    "momentum_bt.confluence_failed",
                    symbol=symbol, score=confluence.total,
                    signals=[s.signal_type.value for s in signals],
                )
                continue
            if confluence.total > self._max_score:
                log.debug(
                    "momentum_bt.confluence_overextended",
                    symbol=symbol, score=confluence.total,
                    signals=[s.signal_type.value for s in signals],
                )
                continue

            # Strip signals that cannot be standalone entry triggers
            # (they remain in `signals` for confluence scoring above, but
            # the actual entry must come from an allowed signal type)
            entry_signals = [s for s in signals if s.signal_type not in _ENTRY_DISABLED]
            if not entry_signals:
                continue

            # Best signal (from entry-allowed list)
            top = max(entry_signals, key=lambda s: s.confidence)

            # ── Gate 3 penalty: sector headwind → -15% confidence ─────────────
            # Applied after selecting top so we penalise the actual entry signal.
            # Strong signals (confidence≥65÷0.85≈77) survive; borderline ones drop
            # below min_confidence and get filtered.
            if _sector_headwind:
                penalised_conf = int(top.confidence * 0.85)
                if penalised_conf < self._min_confidence:
                    log.debug(
                        "momentum_bt.sector_headwind_block",
                        symbol=symbol, original_conf=top.confidence,
                        penalised_conf=penalised_conf,
                    )
                    continue
                top = _dc_replace(
                    top,
                    confidence = penalised_conf,
                    notes      = (top.notes or "") + " | sector_headwind",
                )

            atr = top.atr
            if not atr:
                continue

            # ── Entry price = next candle's open (no look-ahead) ──────────────
            next_idx = i + 1
            if next_idx >= len(trade_df):
                continue

            entry_candle = trade_df.iloc[next_idx]
            entry_price  = float(entry_candle.get("open", 0) or 0)
            if entry_price <= 0:
                continue
            entry_date = trade_df.index[next_idx]
            entry_date = entry_date.date() if hasattr(entry_date, "date") else entry_date

            # ── Stop, target and hold — regime-calibrated ────────────────────
            # TRENDING_UP: tight stop (1.5× ATR), 20-day hold.
            #   Broad market tailwind means less noise; tighter stop is fine.
            # RANGING / TRENDING_DOWN (sector rotation play):
            #   Wider stop (2.0× ATR) — stock goes against market, more intraday
            #   noise even while the sector trend is intact. Tighter stop = whipsaw.
            #   Longer hold (40 days) — sector rotation themes run for months,
            #   not weeks. 20-day hold exits too early.
            if nifty_regime == "TRENDING_UP":
                _stop_atr_mult = 1.5
                _trade_max_hold = MAX_HOLD_DAYS          # 20 days
            else:
                _stop_atr_mult  = 2.0
                _trade_max_hold = 40                     # sector rotation hold

            stop_loss = round(entry_price - _stop_atr_mult * atr, 2)
            target    = round(entry_price + 7.0 * atr, 2)

            # ── Position size (fixed risk per trade) ─────────────────────────
            risk_per_share = entry_price - stop_loss
            if risk_per_share <= 0:
                continue
            risk_budget   = NOTIONAL_CAPITAL * RISK_PCT
            position_size = max(1, int(risk_budget / risk_per_share))

            # ── Simulate exit ─────────────────────────────────────────────────
            future = trade_df.iloc[next_idx + 1:]   # candles AFTER entry
            trade  = self._simulate_exit(
                symbol        = symbol,
                signal_type   = top.signal_type.value,
                entry_date    = entry_date,
                entry_price   = entry_price,
                stop_loss     = stop_loss,
                target        = target,
                position_size = position_size,
                future_df     = future,
                nifty_regime  = nifty_regime,
                max_hold      = _trade_max_hold,
                top           = top,
                confluence    = confluence,
            )
            if trade:
                trades.append(trade)
                in_trade   = True
                exit_after = next_idx + 1 + trade.holding_days

        return trades

    def _simulate_exit(
        self,
        symbol:        str,
        signal_type:   str,
        entry_date:    date,
        entry_price:   float,
        stop_loss:     float,
        target:        float,
        position_size: int,
        future_df:     pd.DataFrame,
        nifty_regime:  str,
        max_hold:      int,
        top,
        confluence,
    ) -> MomentumTrade | None:
        if future_df.empty:
            return None

        initial_risk  = entry_price - stop_loss
        trailing_stop = stop_loss
        exit_price    = entry_price
        exit_reason   = "OPEN"
        hold          = 0

        for idx, (ts, candle) in enumerate(future_df.iterrows()):
            hold += 1
            h = float(candle.get("high",  0) or 0)
            l = float(candle.get("low",   0) or 0)
            c = float(candle.get("close", 0) or 0)

            # Trailing stop update (wider milestones for momentum)
            if initial_risk > 0:
                move        = h - entry_price
                r_multiple  = move / initial_risk
                if r_multiple >= 10:
                    new_trail = entry_price + 5 * initial_risk
                elif r_multiple >= 6:
                    new_trail = entry_price + 3 * initial_risk
                elif r_multiple >= 3:
                    new_trail = entry_price + 1 * initial_risk
                elif r_multiple >= 2:
                    new_trail = entry_price   # breakeven
                else:
                    new_trail = stop_loss
                trailing_stop = max(trailing_stop, new_trail)

            # Check trailing stop hit
            if l <= trailing_stop:
                exit_price  = trailing_stop
                exit_reason = "STOP" if trailing_stop == stop_loss else "TRAIL_STOP"
                break

            # Max hold
            if hold >= max_hold:
                exit_price  = c
                exit_reason = "MAX_HOLD"
                break

        if exit_reason == "OPEN":
            return None

        pnl     = (exit_price - entry_price) * position_size
        pnl_pct = (exit_price - entry_price) / entry_price * 100

        return MomentumTrade(
            symbol             = symbol,
            signal_type        = signal_type,
            entry_date         = entry_date,
            entry_price        = round(entry_price, 2),
            stop_loss          = stop_loss,
            target             = target,
            exit_price         = round(exit_price, 2),
            exit_reason        = exit_reason,
            pnl                = round(pnl, 2),
            pnl_pct            = round(pnl_pct, 2),
            holding_days       = hold,
            position_size      = position_size,
            regime             = nifty_regime,
            confluence_score   = confluence.total,
            confluence_factors = {
                "signal_quality":  confluence.signal_quality,
                "volume":          confluence.volume,
                "trend_alignment": confluence.trend_alignment,
                "rsi_momentum":    confluence.rsi_momentum,
                "multi_signal":    confluence.multi_signal,
            },
            rvol               = top.rvol,
            rsi                = top.rsi,
            adx                = top.adx,
        )

    # ── Data fetch ────────────────────────────────────────────────────────────

    async def _fetch_daily(
        self, symbol: str, start: date, end: date
    ) -> pd.DataFrame | None:
        """Download daily OHLCV from yfinance (NSE ticker format)."""
        ticker = f"{symbol}.NS"
        try:
            raw = yf.Ticker(ticker).history(
                start       = start.isoformat(),
                end         = (end + timedelta(days=1)).isoformat(),
                interval    = "1d",
                auto_adjust = True,
            )
            if raw.empty:
                return None
            raw.index = pd.to_datetime(raw.index)
            if raw.index.tz is not None:
                raw.index = raw.index.tz_convert("Asia/Kolkata").tz_localize(None)
            return raw.rename(columns={
                "Open": "open", "High": "high",
                "Low":  "low",  "Close": "close", "Volume": "volume",
            })[["open", "high", "low", "close", "volume"]]
        except Exception as e:
            log.warning("momentum_bt.fetch_failed", symbol=symbol, error=str(e))
            return None
