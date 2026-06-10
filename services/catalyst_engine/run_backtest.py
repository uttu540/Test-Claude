"""
services/catalyst_engine/run_backtest.py
──────────────────────────────────────────
CLI runner for the catalyst gap-and-go swing backtest.

Usage:
    python -m services.catalyst_engine.run_backtest --universe nifty500 --start 2024-01-01 --end 2024-12-31
    python -m services.catalyst_engine.run_backtest --universe nifty500 --start 2022-01-01 --end 2026-01-01
    python -m services.catalyst_engine.run_backtest --symbols RELIANCE TCS INFY --start 2024-01-01
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta

import numpy as np
from rich import box
from rich.console import Console
from rich.table import Table

from services.catalyst_engine.backtest import CatalystBacktestEngine, CatalystTrade

console = Console()

NIFTY50_SYMS = [
    "ADANIENT","ADANIPORTS","APOLLOHOSP","ASIANPAINT","AXISBANK",
    "BAJAJ-AUTO","BAJAJFINSV","BAJFINANCE","BHARTIARTL","BPCL",
    "BRITANNIA","CIPLA","COALINDIA","DIVISLAB","DRREDDY",
    "EICHERMOT","GRASIM","HCLTECH","HDFCBANK","HDFCLIFE",
    "HEROMOTOCO","HINDALCO","HINDUNILVR","ICICIBANK","INDUSINDBK",
    "INFY","ITC","JSWSTEEL","KOTAKBANK","LT",
    "M&M","MARUTI","NESTLEIND","NTPC","ONGC",
    "POWERGRID","RELIANCE","SBILIFE","SBIN","SUNPHARMA",
    "TATAMOTORS","TATASTEEL","TCS","TECHM","TITAN",
    "ULTRACEMCO","WIPRO",
]


def _get_universe(name: str) -> list[str]:
    if name == "nifty50":
        return NIFTY50_SYMS
    from services.data_ingestion.nifty500_instruments import NIFTY500
    return [s for s, _, _ in NIFTY500]


def _print_report(trades: list[CatalystTrade], start: date, end: date) -> None:
    if not trades:
        console.print("[yellow]No trades generated.[/yellow]")
        return

    winners = [t for t in trades if t.winner]
    losers  = [t for t in trades if not t.winner]
    pnls    = [t.pnl_pct for t in trades]
    rs      = [t.pnl_r for t in trades]
    wr      = len(winners) / len(trades) * 100

    avg_win  = np.mean([t.pnl_pct for t in winners]) if winners else 0
    avg_loss = np.mean([t.pnl_pct for t in losers])  if losers  else 0
    avg_r    = np.mean(rs) if rs else 0
    rr       = abs(avg_win / avg_loss) if avg_loss else float("inf")
    sharpe   = (np.mean(pnls) / np.std(pnls) * np.sqrt(252)) if np.std(pnls) > 0 else 0
    expectancy = wr / 100 * avg_win + (1 - wr / 100) * avg_loss

    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

    console.rule(f"[bold cyan]Catalyst Gap-and-Go — {start} → {end}[/bold cyan]")

    summary = Table(box=box.SIMPLE_HEAD, show_header=False)
    summary.add_column("Metric", style="dim")
    summary.add_column("Value", style="bold")
    summary.add_row("Total trades",     str(len(trades)))
    summary.add_row("Winners",          f"{len(winners)}  ({wr:.1f}% WR)")
    summary.add_row("Losers",           str(len(losers)))
    summary.add_row("Avg win",          f"+{avg_win:.2f}%")
    summary.add_row("Avg loss",         f"{avg_loss:.2f}%")
    summary.add_row("Expectancy/trade", f"{expectancy:+.3f}%")
    summary.add_row("Avg R",            f"{avg_r:+.2f}R")
    summary.add_row("Actual R:R",       f"{rr:.2f}x")
    summary.add_row("Total PnL",        f"{sum(pnls):+.2f}%  (sum of per-trade %)")
    summary.add_row("Sharpe (ann.)",    f"{sharpe:.2f}")
    summary.add_row("Symbols traded",  str(len({t.symbol for t in trades})))
    summary.add_row("Trading days",    str(len({t.signal_date for t in trades})))
    summary.add_row("Avg hold days",   f"{np.mean([t.hold_days for t in trades]):.1f}")
    console.print(summary)

    # Exit reason breakdown
    er = Table(title="Exit reasons", box=box.SIMPLE_HEAD)
    er.add_column("Reason")
    er.add_column("Count", justify="right")
    er.add_column("Win %", justify="right")
    er.add_column("Avg PnL%", justify="right")
    er.add_column("Avg R", justify="right")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        sub  = [t for t in trades if t.exit_reason == reason]
        w    = sum(1 for t in sub if t.winner) / count * 100
        apnl = np.mean([t.pnl_pct for t in sub])
        ar   = np.mean([t.pnl_r for t in sub])
        er.add_row(reason, str(count), f"{w:.0f}%", f"{apnl:+.2f}%", f"{ar:+.2f}R")
    console.print(er)

    # Gap bucket breakdown
    buckets = [(5, 7), (7, 10), (10, 15)]
    gb = Table(title="By gap size", box=box.SIMPLE_HEAD)
    gb.add_column("Gap bucket")
    gb.add_column("Trades", justify="right")
    gb.add_column("Win %", justify="right")
    gb.add_column("Avg PnL%", justify="right")
    for lo, hi in buckets:
        sub = [t for t in trades if lo <= t.gap_pct < hi]
        if not sub:
            continue
        w    = sum(1 for t in sub if t.winner) / len(sub) * 100
        apnl = np.mean([t.pnl_pct for t in sub])
        gb.add_row(f"{lo}–{hi}%", str(len(sub)), f"{w:.0f}%", f"{apnl:+.2f}%")
    console.print(gb)

    # RVOL bucket breakdown
    rvol_buckets = [(8, 12), (12, 20), (20, 999)]
    rv = Table(title="By RVOL", box=box.SIMPLE_HEAD)
    rv.add_column("RVOL bucket")
    rv.add_column("Trades", justify="right")
    rv.add_column("Win %", justify="right")
    rv.add_column("Avg PnL%", justify="right")
    for lo, hi in rvol_buckets:
        sub = [t for t in trades if lo <= t.rvol < hi]
        if not sub:
            continue
        w    = sum(1 for t in sub if t.winner) / len(sub) * 100
        apnl = np.mean([t.pnl_pct for t in sub])
        rv.add_row(f"{lo}–{hi}×", str(len(sub)), f"{w:.0f}%", f"{apnl:+.2f}%")
    console.print(rv)

    # Top 10 trades
    top10 = sorted(trades, key=lambda t: -t.pnl_pct)[:10]
    t10 = Table(title="Top 10 trades", box=box.SIMPLE_HEAD)
    t10.add_column("Symbol")
    t10.add_column("Gap Day")
    t10.add_column("Gap%", justify="right")
    t10.add_column("RVOL", justify="right")
    t10.add_column("CR", justify="right")
    t10.add_column("Entry ₹")
    t10.add_column("Exit ₹")
    t10.add_column("PnL%", justify="right")
    t10.add_column("Hold")
    t10.add_column("Exit")
    for t in top10:
        t10.add_row(
            t.symbol, str(t.signal_date),
            f"{t.gap_pct:.1f}%", f"{t.rvol:.1f}×",
            f"{t.close_high_ratio:.2f}",
            f"₹{t.entry_price:.1f}", f"₹{t.exit_price:.1f}",
            f"[green]+{t.pnl_pct:.2f}%[/green]",
            f"{t.hold_days}d", t.exit_reason,
        )
    console.print(t10)

    # Worst 5 trades
    bot5 = sorted(trades, key=lambda t: t.pnl_pct)[:5]
    b5 = Table(title="Worst 5 trades", box=box.SIMPLE_HEAD)
    b5.add_column("Symbol")
    b5.add_column("Gap Day")
    b5.add_column("Gap%", justify="right")
    b5.add_column("RVOL", justify="right")
    b5.add_column("Entry ₹")
    b5.add_column("Stop ₹")
    b5.add_column("PnL%", justify="right")
    for t in bot5:
        b5.add_row(
            t.symbol, str(t.signal_date),
            f"{t.gap_pct:.1f}%", f"{t.rvol:.1f}×",
            f"₹{t.entry_price:.1f}", f"₹{t.stop_price:.1f}",
            f"[red]{t.pnl_pct:.2f}%[/red]",
        )
    console.print(b5)


def _save_json(trades: list[CatalystTrade], path: str, start: date, end: date) -> None:
    pnls    = [t.pnl_pct for t in trades]
    winners = [t for t in trades if t.winner]
    wr      = len(winners) / len(trades) * 100 if trades else 0
    sharpe  = float(np.mean(pnls) / np.std(pnls) * np.sqrt(252)) if len(pnls) > 1 and np.std(pnls) > 0 else 0

    payload = {
        "strategy": "catalyst_gap_pead_day2",
        "start": str(start), "end": str(end),
        "summary": {
            "total_trades":  len(trades),
            "win_rate_pct":  round(wr, 2),
            "total_pnl_pct": round(sum(pnls), 4),
            "avg_pnl_pct":   round(float(np.mean(pnls)), 4) if pnls else 0,
            "sharpe":        round(sharpe, 2),
            "symbols":       len({t.symbol for t in trades}),
        },
        "trades": [
            {
                "symbol":           t.symbol,
                "signal_date":      str(t.signal_date),
                "entry_date":       str(t.entry_date),
                "entry_price":      t.entry_price,
                "stop_price":       t.stop_price,
                "exit_price":       t.exit_price,
                "exit_date":        str(t.exit_date),
                "exit_reason":      t.exit_reason,
                "gap_pct":          t.gap_pct,
                "rvol":             t.rvol,
                "close_high_ratio": t.close_high_ratio,
                "atr14":            t.atr14,
                "hold_days":        t.hold_days,
                "pnl_pct":          t.pnl_pct,
                "pnl_r":            t.pnl_r,
                "winner":           t.winner,
            }
            for t in sorted(trades, key=lambda t: (t.signal_date, t.symbol))
        ],
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    console.print(f"\n[dim]Saved → {path}[/dim]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Catalyst PEAD gap-and-go backtest")
    parser.add_argument("--symbols",          nargs="+")
    parser.add_argument("--universe",         default="nifty500", choices=["nifty50", "nifty500"])
    parser.add_argument("--days",             type=int, default=365)
    parser.add_argument("--start",            help="YYYY-MM-DD")
    parser.add_argument("--end",              help="YYYY-MM-DD")
    parser.add_argument("--output",           help="JSON output path")
    parser.add_argument("--min-gap",          type=float, default=5.0)
    parser.add_argument("--max-gap",          type=float, default=15.0)
    parser.add_argument("--min-rvol",         type=float, default=8.0)
    parser.add_argument("--min-close-ratio",  type=float, default=0.90)
    parser.add_argument("--stop-pct",         type=float, default=2.5)
    parser.add_argument("--max-hold",         type=int,   default=5)
    parser.add_argument("--target-atr",       type=float, default=3.0)
    parser.add_argument("--workers",          type=int,   default=4)
    args = parser.parse_args()

    end   = date.fromisoformat(args.end)   if args.end   else date.today()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=args.days)

    symbols = args.symbols or _get_universe(args.universe)

    console.print(
        f"\n[bold]Catalyst PEAD[/bold]  |  {len(symbols)} symbols  |  "
        f"{start} → {end}  |  Gap {args.min_gap}–{args.max_gap}%  |  "
        f"RVOL ≥{args.min_rvol}×  |  CloseRatio ≥{args.min_close_ratio}  |  "
        f"Stop {args.stop_pct}%  |  MaxHold {args.max_hold}d  |  Target {args.target_atr}×ATR"
    )

    engine = CatalystBacktestEngine(
        min_gap_pct          = args.min_gap,
        max_gap_pct          = args.max_gap,
        min_rvol             = args.min_rvol,
        min_close_high_ratio = args.min_close_ratio,
        stop_pct             = args.stop_pct,
        max_hold_days        = args.max_hold,
        target_atr_mult      = args.target_atr,
        fetch_workers        = args.workers,
    )

    with console.status("[cyan]Running...[/cyan]"):
        trades = engine.run(symbols, start, end)

    _print_report(trades, start, end)

    out = args.output or f"results/catalyst_{args.universe}_{start}_{end}.json"
    if trades:
        _save_json(trades, out, start, end)


if __name__ == "__main__":
    main()
