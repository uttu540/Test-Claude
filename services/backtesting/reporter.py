"""
services/backtesting/reporter.py
──────────────────────────────────
Computes performance metrics from a BacktestResult and renders
a human-readable report to the terminal.

Metrics computed:
  Overall:      total trades, win rate, net P&L, gross P&L, avg P&L/trade,
                profit factor, max drawdown, Sharpe ratio, best/worst trade
  By signal:    win rate + avg P&L per signal type
  By regime:    win rate + avg P&L per market regime
  By direction: LONG vs SHORT breakdown
  By exit:      TARGET / STOP / EOD / MAX_HOLD counts
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import numpy as np
import structlog
from rich.console import Console
from rich.table import Table
from rich.text import Text

from services.backtesting.engine import BacktestResult, SimulatedTrade

log     = structlog.get_logger(__name__)
console = Console()


@dataclass
class BacktestMetrics:
    # Period
    start_date:   date | None
    end_date:     date | None
    symbols:      int
    # Volume
    total_trades: int
    winning:      int
    losing:       int
    # P&L
    net_pnl:      float
    gross_pnl:    float
    avg_pnl:      float
    best_trade:   float
    worst_trade:  float
    profit_factor: float   # gross_profit / abs(gross_loss)
    # Risk
    max_drawdown:  float   # max peak-to-trough in cumulative P&L
    sharpe_ratio:  float   # annualised (assuming 252 trading days)
    win_rate:      float   # 0.0–1.0
    avg_rr:        float   # avg actual risk:reward
    # Breakdown dicts
    by_signal:      dict[str, dict]
    by_regime:      dict[str, dict]
    by_direction:   dict[str, dict]
    by_exit:        dict[str, int]
    by_confluence:  dict[str, dict]   # score bucket → {trades, wins, win_rate, net_pnl}


class BacktestReporter:
    """
    Computes metrics from a BacktestResult and renders the report.

    Usage:
        reporter = BacktestReporter()
        metrics  = reporter.compute(result)
        reporter.print(metrics)
        reporter.save_json(metrics, "backtest_results.json")
    """

    def compute(self, result: BacktestResult) -> BacktestMetrics:
        trades = result.trades
        if not trades:
            return self._empty_metrics(result)

        pnls    = [t.pnl for t in trades]
        winners = [t for t in trades if t.pnl > 0]
        losers  = [t for t in trades if t.pnl <= 0]

        gross_profit = sum(t.pnl for t in winners)
        gross_loss   = sum(t.pnl for t in losers)   # negative number
        profit_factor = (
            abs(gross_profit / gross_loss) if gross_loss != 0 else float("inf")
        )

        # Cumulative P&L for drawdown calculation
        cum_pnl  = np.cumsum(pnls)
        peak     = np.maximum.accumulate(cum_pnl)
        drawdown = cum_pnl - peak
        max_dd   = float(drawdown.min())

        # Sharpe ratio (daily returns approximation)
        pnl_arr   = np.array(pnls)
        sharpe    = 0.0
        if pnl_arr.std() > 0:
            sharpe = float((pnl_arr.mean() / pnl_arr.std()) * np.sqrt(252))

        # Avg actual R:R
        rr_list = []
        for t in trades:
            risk   = abs(t.entry_price - t.stop_loss)
            reward = abs(t.exit_price  - t.entry_price)
            if risk > 0:
                rr_list.append(reward / risk)
        avg_rr = float(np.mean(rr_list)) if rr_list else 0.0

        return BacktestMetrics(
            start_date    = result.start_date,
            end_date      = result.end_date,
            symbols       = len(result.symbols),
            total_trades  = len(trades),
            winning       = len(winners),
            losing        = len(losers),
            net_pnl       = round(sum(pnls), 2),
            gross_pnl     = round(gross_profit + gross_loss, 2),
            avg_pnl       = round(float(np.mean(pnls)), 2),
            best_trade    = round(max(pnls), 2),
            worst_trade   = round(min(pnls), 2),
            profit_factor = round(profit_factor, 2),
            max_drawdown  = round(max_dd, 2),
            sharpe_ratio  = round(sharpe, 2),
            win_rate      = round(len(winners) / len(trades), 4),
            avg_rr        = round(avg_rr, 2),
            by_signal     = self._breakdown(trades, "signal_type"),
            by_regime     = self._breakdown(trades, "regime"),
            by_direction  = self._breakdown(trades, "direction"),
            by_exit       = self._count_by(trades, "exit_reason"),
            by_confluence = self._confluence_breakdown(trades),
        )

    # ── Printing ──────────────────────────────────────────────────────────────

    def print(self, metrics: BacktestMetrics) -> None:
        """Render a formatted report to the terminal using Rich."""
        console.print()
        console.rule("[bold cyan]Backtest Report[/bold cyan]")

        # ── Summary ──────────────────────────────────────────────────────────
        pnl_colour = "green" if metrics.net_pnl >= 0 else "red"
        summary = Table(show_header=False, box=None, padding=(0, 2))
        summary.add_column(style="bold white", no_wrap=True)
        summary.add_column()

        summary.add_row("Period",
            f"{metrics.start_date} → {metrics.end_date}")
        summary.add_row("Symbols",        str(metrics.symbols))
        summary.add_row("Total Trades",   str(metrics.total_trades))
        summary.add_row("Win / Loss",
            f"[green]{metrics.winning}[/green] / [red]{metrics.losing}[/red]")
        summary.add_row("Win Rate",       f"{metrics.win_rate:.1%}")
        summary.add_row("Net P&L",
            f"[{pnl_colour}]₹{metrics.net_pnl:,.2f}[/{pnl_colour}]")
        summary.add_row("Avg P&L/Trade",
            f"₹{metrics.avg_pnl:,.2f}")
        summary.add_row("Best Trade",
            f"[green]₹{metrics.best_trade:,.2f}[/green]")
        summary.add_row("Worst Trade",
            f"[red]₹{metrics.worst_trade:,.2f}[/red]")
        summary.add_row("Profit Factor",  f"{metrics.profit_factor:.2f}x")
        summary.add_row("Max Drawdown",
            f"[red]₹{metrics.max_drawdown:,.2f}[/red]")
        summary.add_row("Sharpe Ratio",   f"{metrics.sharpe_ratio:.2f}")
        summary.add_row("Avg R:R",        f"{metrics.avg_rr:.2f}x")

        console.print(summary)

        # ── By signal type ────────────────────────────────────────────────────
        if metrics.by_signal:
            console.print()
            console.rule("[dim]By Signal Type[/dim]")
            t = self._breakdown_table(metrics.by_signal, "Signal Type")
            console.print(t)

        # ── By regime ────────────────────────────────────────────────────────
        if metrics.by_regime:
            console.print()
            console.rule("[dim]By Market Regime[/dim]")
            t = self._breakdown_table(metrics.by_regime, "Regime")
            console.print(t)

        # ── By direction ──────────────────────────────────────────────────────
        if metrics.by_direction:
            console.print()
            console.rule("[dim]Long vs Short[/dim]")
            t = self._breakdown_table(metrics.by_direction, "Direction")
            console.print(t)

        # ── Confluence score breakdown ────────────────────────────────────────
        if metrics.by_confluence:
            console.print()
            console.rule("[dim]By Confluence Score[/dim]")
            t = self._breakdown_table(metrics.by_confluence, "Score")
            console.print(t)

        # ── Exit reasons ──────────────────────────────────────────────────────
        if metrics.by_exit:
            console.print()
            console.rule("[dim]Exit Reasons[/dim]")
            exit_t = Table(box=None, padding=(0, 2))
            exit_t.add_column("Reason",  style="bold")
            exit_t.add_column("Count",   justify="right")
            exit_t.add_column("Share",   justify="right")
            total = sum(metrics.by_exit.values())
            for reason, count in sorted(metrics.by_exit.items(),
                                        key=lambda x: -x[1]):
                exit_t.add_row(reason, str(count), f"{count/total:.1%}")
            console.print(exit_t)

        console.print()

    def save_json(self, metrics: BacktestMetrics, path: str) -> None:
        """Save full metrics to a JSON file."""
        data = asdict(metrics)
        # Convert date objects for JSON serialisation
        data["start_date"] = str(data["start_date"])
        data["end_date"]   = str(data["end_date"])
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
        log.info("backtest.saved", path=path)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _confluence_breakdown(self, trades: list[SimulatedTrade]) -> dict[str, dict]:
        """Group trades by confluence score bucket (6, 7, 8, 9, 10)."""
        groups: dict[str, list[SimulatedTrade]] = {}
        for t in trades:
            key = f"score_{t.confluence_score}"
            groups.setdefault(key, []).append(t)
        result = {}
        for key, group in sorted(groups.items()):
            pnls = [t.pnl for t in group]
            wins = [t for t in group if t.pnl > 0]
            result[key] = {
                "trades":   len(group),
                "wins":     len(wins),
                "win_rate": round(len(wins) / len(group), 4),
                "net_pnl":  round(sum(pnls), 2),
                "avg_pnl":  round(float(np.mean(pnls)), 2),
            }
        return result

    def _breakdown(self, trades: list[SimulatedTrade], attr: str) -> dict[str, dict]:
        groups: dict[str, list[SimulatedTrade]] = {}
        for t in trades:
            key = getattr(t, attr, "UNKNOWN")
            groups.setdefault(key, []).append(t)

        result = {}
        for key, group in sorted(groups.items()):
            pnls    = [t.pnl for t in group]
            winners = [t for t in group if t.pnl > 0]
            result[key] = {
                "trades":   len(group),
                "wins":     len(winners),
                "win_rate": round(len(winners) / len(group), 4),
                "net_pnl":  round(sum(pnls), 2),
                "avg_pnl":  round(float(np.mean(pnls)), 2),
            }
        return result

    def _count_by(self, trades: list[SimulatedTrade], attr: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in trades:
            key = getattr(t, attr, "UNKNOWN")
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _breakdown_table(self, data: dict[str, dict], label: str) -> Table:
        t = Table(box=None, padding=(0, 2))
        t.add_column(label,      style="bold")
        t.add_column("Trades",   justify="right")
        t.add_column("Wins",     justify="right")
        t.add_column("Win Rate", justify="right")
        t.add_column("Net P&L",  justify="right")
        t.add_column("Avg P&L",  justify="right")

        for key, row in sorted(data.items(), key=lambda x: -x[1]["net_pnl"]):
            wr_colour = "green" if row["win_rate"] >= 0.5 else "red"
            pnl_colour = "green" if row["net_pnl"] >= 0 else "red"
            t.add_row(
                key,
                str(row["trades"]),
                str(row["wins"]),
                f"[{wr_colour}]{row['win_rate']:.1%}[/{wr_colour}]",
                f"[{pnl_colour}]₹{row['net_pnl']:,.0f}[/{pnl_colour}]",
                f"₹{row['avg_pnl']:,.0f}",
            )
        return t

    def _empty_metrics(self, result: BacktestResult) -> BacktestMetrics:
        return BacktestMetrics(
            start_date=result.start_date, end_date=result.end_date,
            symbols=len(result.symbols), total_trades=0,
            winning=0, losing=0, net_pnl=0, gross_pnl=0, avg_pnl=0,
            best_trade=0, worst_trade=0, profit_factor=0,
            max_drawdown=0, sharpe_ratio=0, win_rate=0, avg_rr=0,
            by_signal={}, by_regime={}, by_direction={}, by_exit={}, by_confluence={},
        )
