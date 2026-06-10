"""
scripts/test_claude_fundamental_gate.py
────────────────────────────────────────
Evaluate Claude's fundamental gate against 2024 backtest trades.

Takes the most recent N signals from announcement-mode backtest (2024),
runs Claude's fundamental gate on each, and reports:
  - Did Claude approve the winners?
  - Did Claude reject the losers?
  - What's the WR on Claude-approved vs Claude-rejected subset?

Usage:
    PYTHONPATH=. python3 scripts/test_claude_fundamental_gate.py --n 30
    PYTHONPATH=. python3 scripts/test_claude_fundamental_gate.py --n 30 --year 2023
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path

# ── Setup ─────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Load .env before importing settings
os.chdir(ROOT)
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from rich.console import Console
from rich.table import Table

console = Console()


# ── Fetch backtest trades ──────────────────────────────────────────────────────

async def get_backtest_trades(year: int, n: int) -> list[dict]:
    """
    Run announcement-mode backtest for the given year and return
    the raw EarningsBacktestTrade list (as dicts), capped at n trades.
    """
    from services.earnings_engine.backtest import EarningsBacktestEngine
    from services.earnings_engine.run_backtest import (
        _get_symbols, _fetch_announcement_calendar,
    )

    start = date(year, 1, 1)
    end   = date(year, 12, 31)
    symbols = _get_symbols("nifty500")

    console.print(f"[bold]Fetching NSE announcement calendar {year}...[/bold]")
    ann_map = await _fetch_announcement_calendar(start, end)
    console.print(f"Calendar: {sum(len(v) for v in ann_map.values())} events")

    engine = EarningsBacktestEngine(
        min_gap_pct  = 3.0,
        max_gap_pct  = 12.0,
        min_rvol     = 3.0,
        stop_mult    = 1.5,
        target_mult  = 3.0,
        max_hold     = 15,
    )

    console.print(f"[bold]Running backtest for {year} ({len(symbols)} symbols)...[/bold]")
    trades = engine.run(
        symbols   = symbols,
        start     = start,
        end       = end,
        ann_dates = ann_map,
    )

    console.print(f"Found [cyan]{len(trades)}[/cyan] signals. Taking last {n}.")

    # Sort by date, take last N (most recent signals)
    trades.sort(key=lambda t: t.date)
    sample = trades[-n:]

    return [
        {
            "symbol":      t.symbol,
            "date":        t.date.isoformat(),
            "gap_pct":     t.gap_pct,
            "rvol":        t.rvol,
            "confidence":  t.confidence,
            "pnl_pct":     t.pnl_pct,
            "is_win":      t.is_win,
            "exit_reason": t.exit_reason,
            "exit_day":    t.exit_day,
        }
        for t in sample
    ]


# ── Claude gate evaluation ─────────────────────────────────────────────────────

async def evaluate_with_claude(trades: list[dict]) -> list[dict]:
    """Run fundamental gate on each trade and attach result."""
    from services.earnings_engine.fundamental_gate import evaluate_fundamental_gate

    results = []
    total = len(trades)

    for i, t in enumerate(trades, 1):
        sym = t["symbol"]
        console.print(
            f"  [{i}/{total}] {sym:20s} gap={t['gap_pct']:+.1f}%  "
            f"RVOL={t['rvol']:.1f}x  "
            f"actual={'WIN' if t['is_win'] else 'LOSS':4s}  ...",
            end="",
        )

        try:
            gate = await evaluate_fundamental_gate(sym, t["gap_pct"], t["rvol"])
            approved  = gate.approve
            adj       = gate.confidence_adj
            flags_str = " | ".join(gate.red_flags[:2]) or "clean"
        except Exception as e:
            approved  = True
            adj       = 0
            flags_str = f"ERROR: {e}"

        result = {**t, "claude_approve": approved, "confidence_adj": adj, "flags": flags_str}
        results.append(result)

        label = "[green]APPROVE[/green]" if approved else "[red]REJECT [/red]"
        console.print(f" → {label}  adj={adj:+d}  {flags_str[:60]}")

        # Small delay to avoid hammering screener.in + Anthropic API
        await asyncio.sleep(1.5)

    return results


# ── Report ─────────────────────────────────────────────────────────────────────

def print_report(results: list[dict]) -> None:
    console.print()
    console.rule("[bold]Claude Fundamental Gate — Evaluation Report[/bold]")

    approved  = [r for r in results if r["claude_approve"]]
    rejected  = [r for r in results if not r["claude_approve"]]

    approved_wins  = [r for r in approved if r["is_win"]]
    approved_losses = [r for r in approved if not r["is_win"]]
    rejected_wins  = [r for r in rejected if r["is_win"]]
    rejected_losses = [r for r in rejected if not r["is_win"]]

    overall_wr = sum(1 for r in results if r["is_win"]) / len(results) * 100 if results else 0
    approved_wr = len(approved_wins) / len(approved) * 100 if approved else 0
    rejected_wr = len(rejected_wins) / len(rejected) * 100 if rejected else 0

    # ── Summary table ──────────────────────────────────────────────────────────
    t = Table(title="Overall Summary", show_header=True)
    t.add_column("Subset",        style="bold")
    t.add_column("Trades",        justify="right")
    t.add_column("Win Rate",      justify="right")
    t.add_column("Wins",          justify="right")
    t.add_column("Losses",        justify="right")

    t.add_row("All trades",          str(len(results)),  f"{overall_wr:.1f}%",
              str(sum(1 for r in results if r["is_win"])),
              str(sum(1 for r in results if not r["is_win"])))
    t.add_row("Claude APPROVED",     str(len(approved)), f"{approved_wr:.1f}%",
              str(len(approved_wins)), str(len(approved_losses)))
    t.add_row("Claude REJECTED",     str(len(rejected)), f"{rejected_wr:.1f}%",
              str(len(rejected_wins)), str(len(rejected_losses)))
    console.print(t)

    # ── Confusion matrix ───────────────────────────────────────────────────────
    console.print()
    cm = Table(title="Confusion Matrix", show_header=True)
    cm.add_column("Claude \\ Actual",     style="bold")
    cm.add_column("Actual WIN",           justify="right", style="green")
    cm.add_column("Actual LOSS",          justify="right", style="red")

    cm.add_row("Claude APPROVED",  str(len(approved_wins)),  str(len(approved_losses)))
    cm.add_row("Claude REJECTED",  str(len(rejected_wins)),  str(len(rejected_losses)))
    console.print(cm)

    # Key metrics
    if results:
        true_pos  = len(approved_wins)    # approved a winner
        false_pos = len(approved_losses)  # approved a loser (bad)
        true_neg  = len(rejected_losses)  # rejected a loser (good)
        false_neg = len(rejected_wins)    # rejected a winner (bad)

        precision = true_pos / (true_pos + false_pos) * 100 if (true_pos + false_pos) > 0 else 0
        recall    = true_pos / (true_pos + false_neg) * 100 if (true_pos + false_neg) > 0 else 0

        console.print()
        console.print(f"  Precision (approved→wins):  [bold]{precision:.1f}%[/bold]  "
                      f"(of Claude's approvals, how many were wins)")
        console.print(f"  Recall    (found winners):   [bold]{recall:.1f}%[/bold]  "
                      f"(of all actual wins, how many did Claude approve)")
        console.print(f"  WR lift:  [bold]{approved_wr - overall_wr:+.1f}pp[/bold]  "
                      f"({overall_wr:.1f}% → {approved_wr:.1f}% on approved trades)")

    # ── Per-trade table ────────────────────────────────────────────────────────
    console.print()
    pt = Table(title="Per-Trade Detail", show_header=True)
    pt.add_column("Symbol",     style="bold", width=14)
    pt.add_column("Date",       width=10)
    pt.add_column("Gap",        justify="right", width=7)
    pt.add_column("RVOL",       justify="right", width=6)
    pt.add_column("Claude",     width=8)
    pt.add_column("Adj",        justify="right", width=5)
    pt.add_column("Actual",     width=6)
    pt.add_column("P&L",        justify="right", width=8)
    pt.add_column("Exit",       width=8)
    pt.add_column("Day",        justify="right", width=4)
    pt.add_column("Flags",      width=40)

    for r in results:
        actual_style = "green" if r["is_win"] else "red"
        gate_style   = "green" if r["claude_approve"] else "red"
        pt.add_row(
            r["symbol"],
            r["date"],
            f"{r['gap_pct']:+.1f}%",
            f"{r['rvol']:.1f}x",
            f"[{gate_style}]{'OK' if r['claude_approve'] else 'SKIP'}[/{gate_style}]",
            f"{r['confidence_adj']:+d}",
            f"[{actual_style}]{'WIN' if r['is_win'] else 'LOSS'}[/{actual_style}]",
            f"{r['pnl_pct']:+.2f}%",
            r["exit_reason"],
            str(r["exit_day"]),
            r["flags"][:38],
        )
    console.print(pt)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(description="Test Claude fundamental gate on backtest trades")
    parser.add_argument("--n",    type=int, default=30, help="Number of trades to test")
    parser.add_argument("--year", type=int, default=2024, help="Year to backtest (default 2024)")
    parser.add_argument("--save", type=str, help="Save results to JSON path")
    args = parser.parse_args()

    # Validate API key
    from config.settings import settings
    if not settings.anthropic_api_key:
        console.print("[red]ANTHROPIC_API_KEY not set — Claude gate will approve everything[/red]")

    console.rule(f"[bold]Claude Gate Test — {args.n} trades from {args.year}[/bold]")

    trades  = await get_backtest_trades(args.year, args.n)
    console.print(f"\n[bold]Running Claude gate on {len(trades)} trades...[/bold]\n")
    results = await evaluate_with_claude(trades)

    print_report(results)

    if args.save:
        Path(args.save).write_text(json.dumps(results, indent=2))
        console.print(f"\nSaved to {args.save}")


if __name__ == "__main__":
    asyncio.run(main())
