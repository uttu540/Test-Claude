"""
services/orb_engine/run.py
───────────────────────────
CLI runner for the ORB 30-min backtest (trailing stop version).

Usage:
    python -m services.orb_engine.run --universe nifty50
    python -m services.orb_engine.run --universe nifty500 --output results/orb_n500.json
    python -m services.orb_engine.run --symbols RELIANCE TCS INFY --trail-mult 0.8
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta

import numpy as np
from rich import box
from rich.console import Console
from rich.table import Table

from services.orb_engine.backtest import ORBBacktestEngine, ORBTrade

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
    if name == "all_nse":
        from services.data_ingestion.nifty500_instruments import get_live_universe
        return get_live_universe()
    from services.data_ingestion.nifty500_instruments import NIFTY500
    return [s for s, _, _ in NIFTY500]


# ── Report ────────────────────────────────────────────────────────────────────

def _print_report(trades: list[ORBTrade], start: date, end: date) -> None:
    if not trades:
        console.print("[yellow]No trades generated.[/yellow]")
        return

    winners  = [t for t in trades if t.winner]
    losers   = [t for t in trades if not t.winner]
    pnls     = [t.pnl_pct for t in trades]
    win_rate = len(winners) / len(trades) * 100

    avg_win  = np.mean([t.pnl_pct for t in winners]) if winners else 0
    avg_loss = np.mean([t.pnl_pct for t in losers])  if losers  else 0
    rr       = abs(avg_win / avg_loss) if avg_loss else float("inf")
    sharpe   = (np.mean(pnls) / np.std(pnls) * np.sqrt(252)) if np.std(pnls) > 0 else 0

    # Avg max excursion (how high did winners get before exit)
    avg_max_win = np.mean([(t.max_price - t.entry_price) / t.entry_price * 100
                           for t in winners]) if winners else 0

    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

    trend_trades  = [t for t in trades if t.nifty_trend]
    bypass_trades = [t for t in trades if not t.nifty_trend]

    console.rule(f"[bold cyan]ORB 30-min — {start} → {end}[/bold cyan]")

    summary = Table(box=box.SIMPLE_HEAD, show_header=False)
    summary.add_column("Metric", style="dim")
    summary.add_column("Value",  style="bold")
    summary.add_row("Total trades",       str(len(trades)))
    summary.add_row("  Trend-day trades", f"{len(trend_trades)}")
    summary.add_row("  Bypass trades",    f"{len(bypass_trades)}  (ranging day catalyst)")
    summary.add_row("Winners",            f"{len(winners)}  ({win_rate:.1f}% WR)")
    summary.add_row("Losers",             str(len(losers)))
    summary.add_row("Avg win",            f"+{avg_win:.2f}%")
    summary.add_row("Avg max excursion",  f"+{avg_max_win:.2f}%  (peak before stop)")
    summary.add_row("Avg loss",           f"{avg_loss:.2f}%")
    summary.add_row("Actual R:R",         f"{rr:.2f}x")
    summary.add_row("Total PnL",          f"{sum(pnls):+.2f}%  (sum of per-trade %)")
    summary.add_row("Sharpe (ann.)",      f"{sharpe:.2f}")
    summary.add_row("Symbols traded",     str(len({t.symbol for t in trades})))
    console.print(summary)

    # Bypass-only breakdown (ranging day catalyst trades)
    if bypass_trades:
        bp_winners = [t for t in bypass_trades if t.winner]
        bp_wr      = len(bp_winners) / len(bypass_trades) * 100
        bp_pnls    = [t.pnl_pct for t in bypass_trades]
        bp = Table(title="Bypass trades (ranging-day catalysts)", box=box.SIMPLE_HEAD, show_header=False)
        bp.add_column("Metric", style="dim")
        bp.add_column("Value",  style="bold")
        bp.add_row("Count",     str(len(bypass_trades)))
        bp.add_row("Win rate",  f"{bp_wr:.1f}%")
        bp.add_row("Total PnL", f"{sum(bp_pnls):+.2f}%")
        bp.add_row("Avg PnL",   f"{np.mean(bp_pnls):+.2f}%")
        console.print(bp)

    # Exit reason breakdown
    er = Table(title="Exit reasons", box=box.SIMPLE_HEAD)
    er.add_column("Reason")
    er.add_column("Count", justify="right")
    er.add_column("Win %", justify="right")
    er.add_column("Avg PnL%", justify="right")
    for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
        sub  = [t for t in trades if t.exit_reason == reason]
        wr   = sum(1 for t in sub if t.winner) / count * 100
        apnl = np.mean([t.pnl_pct for t in sub])
        er.add_row(reason, str(count), f"{wr:.0f}%", f"{apnl:+.2f}%")
    console.print(er)

    # Top 10
    top10 = sorted(trades, key=lambda t: -t.pnl_pct)[:10]
    t10 = Table(title="Top 10 trades", box=box.SIMPLE_HEAD)
    t10.add_column("Symbol")
    t10.add_column("Date")
    t10.add_column("Entry ₹")
    t10.add_column("Peak ₹")
    t10.add_column("Exit ₹")
    t10.add_column("PnL%", justify="right")
    t10.add_column("Reason")
    for t in top10:
        t10.add_row(
            t.symbol, str(t.trade_date),
            f"₹{t.entry_price:.1f}", f"₹{t.max_price:.1f}", f"₹{t.exit_price:.1f}",
            f"[green]+{t.pnl_pct:.2f}%[/green]", t.exit_reason,
        )
    console.print(t10)

    # Worst 5
    bot5 = sorted(trades, key=lambda t: t.pnl_pct)[:5]
    b5 = Table(title="Worst 5 trades", box=box.SIMPLE_HEAD)
    b5.add_column("Symbol")
    b5.add_column("Date")
    b5.add_column("OR%", justify="right")
    b5.add_column("Entry ₹")
    b5.add_column("Stop ₹")
    b5.add_column("PnL%", justify="right")
    for t in bot5:
        b5.add_row(
            t.symbol, str(t.trade_date), f"{t.or_range_pct:.2f}%",
            f"₹{t.entry_price:.1f}", f"₹{t.initial_stop:.1f}",
            f"[red]{t.pnl_pct:.2f}%[/red]",
        )
    console.print(b5)


def _save_json(trades: list[ORBTrade], path: str, start: date, end: date) -> None:
    pnls    = [t.pnl_pct for t in trades]
    winners = [t for t in trades if t.winner]
    wr      = len(winners) / len(trades) * 100 if trades else 0
    sharpe  = float(np.mean(pnls) / np.std(pnls) * np.sqrt(252)) if len(pnls) > 1 and np.std(pnls) > 0 else 0

    payload = {
        "strategy": "ORB_30min_trailing_stop",
        "start": str(start), "end": str(end),
        "summary": {
            "total_trades":  len(trades),
            "win_rate_pct":  round(wr, 2),
            "total_pnl_pct": round(sum(pnls), 4),
            "avg_pnl_pct":   round(float(np.mean(pnls)), 4) if pnls else 0,
            "sharpe":        round(sharpe, 2),
            "symbols":       len({t.symbol for t in trades}),
            "trading_days":  len({t.trade_date for t in trades}),
        },
        "trades": [
            {
                "symbol":       t.symbol,
                "date":         str(t.trade_date),
                "entry_time":   t.entry_time.isoformat(),
                "entry_price":  t.entry_price,
                "initial_stop": t.initial_stop,
                "exit_price":   t.exit_price,
                "exit_time":    t.exit_time.isoformat(),
                "exit_reason":  t.exit_reason,
                "or_high":      t.or_high,
                "or_low":       t.or_low,
                "or_range_pct": t.or_range_pct,
                "max_price":    t.max_price,
                "pnl_pct":      t.pnl_pct,
                "winner":       t.winner,
                "nifty_trend":  t.nifty_trend,
            }
            for t in sorted(trades, key=lambda t: (t.trade_date, t.symbol))
        ],
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    console.print(f"\n[dim]Saved → {path}[/dim]")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="ORB 30-min backtest — trailing stop")
    parser.add_argument("--symbols",    nargs="+")
    parser.add_argument("--universe",   default="nifty50", choices=["nifty50", "nifty500", "all_nse"])
    parser.add_argument("--days",       type=int, default=55)
    parser.add_argument("--start",      help="YYYY-MM-DD")
    parser.add_argument("--end",        help="YYYY-MM-DD")
    parser.add_argument("--output",     help="JSON output path")
    parser.add_argument("--trail-mult", type=float, default=1.0,
                        help="Trail distance = OR range × mult (default 1.0)")
    parser.add_argument("--vol-mult",   type=float, default=1.5,
                        help="Volume multiplier for breakout candle (default 1.5)")
    args = parser.parse_args()

    end   = date.fromisoformat(args.end)   if args.end   else date.today()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=args.days)

    earliest = date.today() - timedelta(days=59)
    if start < earliest:
        console.print(f"[yellow]Clamping start to {earliest} (yfinance 15-min limit)[/yellow]")
        start = earliest

    symbols = args.symbols or _get_universe(args.universe)
    console.print(
        f"\n[bold]ORB 30-min — Trailing Stop[/bold]  |  "
        f"{len(symbols)} symbols  |  {start} → {end}  |  "
        f"Trail {args.trail_mult}× OR-range  |  Vol {args.vol_mult}×"
    )

    engine = ORBBacktestEngine(volume_mult=args.vol_mult, trail_mult=args.trail_mult)
    with console.status("[cyan]Running...[/cyan]"):
        trades = engine.run(symbols, start, end)

    _print_report(trades, start, end)

    out = args.output or f"results/orb_{args.universe}_trail{args.trail_mult}_{start}_{end}.json"
    if trades:
        _save_json(trades, out, start, end)


if __name__ == "__main__":
    main()
