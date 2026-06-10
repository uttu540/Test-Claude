"""
scripts/backtest_200ema_gate_comparison.py
──────────────────────────────────────────
Compares momentum backtest results with:
  1. Baseline: hard above_200ema gate (current code — return [] if not above_200)
  2. Relaxed:  allow stocks within 10% below 200 EMA

Run from repo root:
    python scripts/backtest_200ema_gate_comparison.py

Date range: Jan 2026 – May 2026 (recent trending-down market period)
Universe:   Nifty 500
"""
from __future__ import annotations

import asyncio
import sys
import os
import types
from datetime import date

import numpy as np

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rich.console import Console
from rich.table import Table

console = Console()

START = date(2026, 1, 1)
END   = date(2026, 5, 27)


# ── Universe helpers ──────────────────────────────────────────────────────────

def _get_nifty500_symbols() -> list[str]:
    from services.data_ingestion.nifty500_instruments import NIFTY500
    return [sym for sym, _, _ in NIFTY500]


def _get_segments(symbols: list[str]) -> dict[str, str]:
    from services.data_ingestion.nifty500_instruments import NIFTY500
    nifty500_syms = [sym for sym, _, _ in NIFTY500]
    nifty50_set   = set(nifty500_syms[:50])
    next50_set    = set(nifty500_syms[50:100])
    midcap_set    = set(nifty500_syms[100:350])
    mapping: dict[str, str] = {}
    for sym in symbols:
        if sym in nifty50_set or sym in next50_set:
            mapping[sym] = "LARGE_CAP"
        elif sym in midcap_set:
            mapping[sym] = "MID_CAP"
        else:
            mapping[sym] = "SMALL_CAP"
    return mapping


# ── Patch / restore ───────────────────────────────────────────────────────────

def _patch_signals_relaxed() -> None:
    """
    Monkey-patch MomentumDetector.detect() to use the relaxed 200 EMA gate.

    Change:
      BEFORE: if not above_200: return []
              above_200 = bool(price >= ema200)
      AFTER:  if ema200 > 0 and price < ema200 * 0.90: return []
              above_200 = bool(price >= ema200 * 0.90)   # within 10% below OK
    """
    import services.momentum_engine.signals as _sig_mod
    detector_cls = _sig_mod.MomentumDetector

    # Stash original so we can restore later
    detector_cls._orig_detect = detector_cls.detect  # type: ignore[attr-defined]

    def _relaxed_detect(self, df, symbol: str = "") -> list:
        """Relaxed detect: allow stocks within 10% below 200 EMA."""
        import pandas as pd

        if len(df) < 60:
            return []

        latest = df.iloc[-1]
        price  = float(latest.get("close", 0) or 0)
        if price <= 0:
            return []

        atr_14    = float(latest.get("atr_14") or latest.get("atr") or 0)
        rsi       = float(latest.get("rsi_14") or latest.get("rsi") or 50)
        adx       = float(latest.get("adx") or 0)
        ema8      = float(latest.get("ema_8")   or latest.get("ema_fast") or price)
        ema21     = float(latest.get("ema_33")  or latest.get("ema_mid")  or price)
        ema50     = float(latest.get("ema_50")  or latest.get("ema_slow") or price)
        ema200    = float(latest.get("ema_200") or latest.get("ema_trend") or price)
        ema_stack = int(latest.get("ema_stack") or 0)

        # Volume
        vol     = float(latest.get("volume") or 0)
        avg_vol = float(df["volume"].tail(20).mean() or 1)
        rvol    = round(vol / avg_vol, 2) if avg_vol > 0 else 1.0

        # ── RELAXED GATE: allow within 10% below 200 EMA ──────────────────────
        if ema200 > 0 and price < ema200 * 0.90:
            return []
        above_200 = bool(price >= ema200 * 0.90)   # within 10% below still OK
        # ─────────────────────────────────────────────────────────────────────

        if adx < 20:
            return []

        signals: list = []
        signals += self._darvas_breakout(df, symbol, price, atr_14, rvol, rsi, adx, ema_stack, above_200)
        signals += self._breakout_52w(df, symbol, price, atr_14, rvol, rsi, adx, ema_stack, above_200)
        signals += self._volume_thrust(df, symbol, price, atr_14, rvol, rsi, adx, ema_stack, above_200)
        signals += self._ema_ribbon(df, symbol, price, atr_14, rvol, rsi, adx, ema_stack, above_200,
                                    ema8, ema21, ema50, ema200)
        signals += self._bull_momentum(df, symbol, price, atr_14, rvol, rsi, adx, ema_stack, above_200)
        return signals

    # Bind as an unbound function (Python 3 style)
    detector_cls.detect = _relaxed_detect  # type: ignore[method-assign]
    console.print("[yellow]  [PATCH] 200 EMA gate relaxed (within 10% below 200 EMA OK)[/yellow]")


def _restore_signals() -> None:
    """Restore the original detect() method."""
    import services.momentum_engine.signals as _sig_mod
    detector_cls = _sig_mod.MomentumDetector
    if hasattr(detector_cls, "_orig_detect"):
        detector_cls.detect = detector_cls._orig_detect  # type: ignore[method-assign]
        del detector_cls._orig_detect  # type: ignore[attr-defined]
        console.print("[green]  [RESTORE] Original detect() restored[/green]")


# ── Stats ─────────────────────────────────────────────────────────────────────

def _compute_stats(trades) -> dict:
    if not trades:
        return {
            "n_trades": 0, "win_rate": 0.0, "net_pnl": 0.0,
            "sharpe": 0.0, "avg_pnl": 0.0, "best": 0.0, "worst": 0.0,
        }
    pnls    = [t.pnl for t in trades]
    winners = [t for t in trades if t.pnl > 0]
    arr     = np.array(pnls)
    sharpe  = float((arr.mean() / arr.std()) * np.sqrt(252)) if arr.std() > 0 else 0.0
    return {
        "n_trades": len(trades),
        "win_rate": round(len(winners) / len(trades) * 100, 1),
        "net_pnl":  round(float(sum(pnls)), 2),
        "sharpe":   round(sharpe, 2),
        "avg_pnl":  round(float(arr.mean()), 2),
        "best":     round(float(max(pnls)), 2),
        "worst":    round(float(min(pnls)), 2),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

async def _run_baseline(symbols, seg_map):
    """Run with the current (patched) code state — baseline."""
    from services.momentum_engine.backtest import MomentumBacktestEngine
    engine = MomentumBacktestEngine(
        symbols               = symbols,
        start_date            = START,
        end_date              = END,
        symbol_segments       = seg_map,
        min_score             = 8,
        min_confidence        = 65,
        enable_sector_filter  = False,
    )
    return await engine.run()


async def _run_relaxed(symbols, seg_map):
    """Patch detect(), run, then unconditionally restore."""
    from services.momentum_engine.backtest import MomentumBacktestEngine
    _patch_signals_relaxed()
    try:
        engine = MomentumBacktestEngine(
            symbols               = symbols,
            start_date            = START,
            end_date              = END,
            symbol_segments       = seg_map,
            min_score             = 8,
            min_confidence        = 65,
            enable_sector_filter  = False,
        )
        return await engine.run()
    finally:
        _restore_signals()


async def main() -> None:
    symbols = _get_nifty500_symbols()
    seg_map = _get_segments(symbols)

    console.print(f"\n[bold cyan]Momentum Engine — 200 EMA Gate Comparison[/bold cyan]")
    console.print(f"Period  : {START} → {END}")
    console.print(f"Universe: Nifty 500 ({len(symbols)} symbols)\n")

    # ── Baseline ──────────────────────────────────────────────────────────────
    console.print("[bold white]Run 1/2 — BASELINE (hard above_200ema gate)[/bold white]")
    baseline_result = await _run_baseline(symbols, seg_map)
    baseline_stats  = _compute_stats(baseline_result.trades)
    baseline_syms   = {t.symbol for t in baseline_result.trades}
    console.print(f"  Done — {baseline_stats['n_trades']} trades\n")

    # ── Relaxed ───────────────────────────────────────────────────────────────
    console.print("[bold white]Run 2/2 — RELAXED (within 10% below 200 EMA OK)[/bold white]")
    relaxed_result = await _run_relaxed(symbols, seg_map)
    relaxed_stats  = _compute_stats(relaxed_result.trades)
    relaxed_syms   = {t.symbol for t in relaxed_result.trades}
    console.print(f"  Done — {relaxed_stats['n_trades']} trades\n")

    # ── Comparison table ──────────────────────────────────────────────────────
    console.print()
    console.rule("[bold cyan]Comparison Results[/bold cyan]")

    tbl = Table(box=None, padding=(0, 3))
    tbl.add_column("Metric",              style="bold white", no_wrap=True)
    tbl.add_column("Baseline",            justify="right")
    tbl.add_column("Relaxed",             justify="right")
    tbl.add_column("Delta",               justify="right")

    def _delta_fmt(a, b, higher_is_better=True, fmt="{:+.0f}"):
        d = b - a
        good = (d > 0) == higher_is_better
        col  = "green" if d > 0 and good else ("red" if d < 0 and not good else
               ("red" if d < 0 and good else ("green" if d < 0 and not good else "dim")))
        # Simpler: green if change is in the desired direction, red otherwise
        if d == 0:
            col = "dim"
        elif (d > 0) == higher_is_better:
            col = "green"
        else:
            col = "red"
        return f"[{col}]{fmt.format(d)}[/{col}]"

    bs, rx = baseline_stats, relaxed_stats

    tbl.add_row("Total Trades",
        str(bs["n_trades"]),
        str(rx["n_trades"]),
        _delta_fmt(bs["n_trades"], rx["n_trades"], higher_is_better=True, fmt="{:+d}"))
    tbl.add_row("Win Rate %",
        f"{bs['win_rate']:.1f}%",
        f"{rx['win_rate']:.1f}%",
        _delta_fmt(bs["win_rate"], rx["win_rate"], higher_is_better=True, fmt="{:+.1f}pp"))
    tbl.add_row("Net P&L",
        f"₹{bs['net_pnl']:,.0f}",
        f"₹{rx['net_pnl']:,.0f}",
        _delta_fmt(bs["net_pnl"], rx["net_pnl"], higher_is_better=True, fmt="₹{:+,.0f}"))
    tbl.add_row("Avg P&L / Trade",
        f"₹{bs['avg_pnl']:,.0f}",
        f"₹{rx['avg_pnl']:,.0f}",
        _delta_fmt(bs["avg_pnl"], rx["avg_pnl"], higher_is_better=True, fmt="₹{:+,.0f}"))
    tbl.add_row("Sharpe Ratio",
        f"{bs['sharpe']:.2f}",
        f"{rx['sharpe']:.2f}",
        _delta_fmt(bs["sharpe"], rx["sharpe"], higher_is_better=True, fmt="{:+.2f}"))
    tbl.add_row("Best Trade",
        f"₹{bs['best']:,.0f}",
        f"₹{rx['best']:,.0f}",
        "")
    tbl.add_row("Worst Trade",
        f"₹{bs['worst']:,.0f}",
        f"₹{rx['worst']:,.0f}",
        "")

    console.print(tbl)

    # ── Symbols only in relaxed ───────────────────────────────────────────────
    relaxed_only_syms = relaxed_syms - baseline_syms
    console.print()
    console.rule("[dim]Symbols fired in RELAXED but not in BASELINE[/dim]")
    console.print(f"  Total new symbols: [bold]{len(relaxed_only_syms)}[/bold]\n")

    if relaxed_only_syms:
        only_trades = [t for t in relaxed_result.trades if t.symbol in relaxed_only_syms]

        # Aggregate per symbol
        sym_pnl:    dict[str, float] = {}
        sym_count:  dict[str, int]   = {}
        sym_wins:   dict[str, int]   = {}
        for tr in only_trades:
            s = tr.symbol
            sym_pnl[s]   = sym_pnl.get(s, 0.0)   + tr.pnl
            sym_count[s] = sym_count.get(s, 0)    + 1
            sym_wins[s]  = sym_wins.get(s, 0)     + (1 if tr.pnl > 0 else 0)

        top5 = sorted(sym_pnl.items(), key=lambda x: -x[1])[:5]

        top5_tbl = Table(box=None, padding=(0, 3))
        top5_tbl.add_column("Symbol",    style="bold")
        top5_tbl.add_column("Trades",    justify="right")
        top5_tbl.add_column("Win Rate",  justify="right")
        top5_tbl.add_column("Total PnL", justify="right")

        for sym, pnl in top5:
            n   = sym_count[sym]
            wr  = sym_wins[sym] / n * 100
            col = "green" if pnl >= 0 else "red"
            top5_tbl.add_row(sym, str(n), f"{wr:.0f}%", f"[{col}]₹{pnl:,.0f}[/{col}]")

        console.print(top5_tbl)
    else:
        console.print("  [dim]No new symbols fired in the relaxed run.[/dim]")

    console.print()


if __name__ == "__main__":
    asyncio.run(main())
