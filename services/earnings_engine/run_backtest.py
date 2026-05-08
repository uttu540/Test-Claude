"""
services/earnings_engine/run_backtest.py
─────────────────────────────────────────
CLI runner for the earnings gap-and-go backtest.

Usage:
    # Default: scan mode, Nifty 500, 2022-present, new thresholds
    python -m services.earnings_engine.run_backtest

    # Announcement mode — only test on actual NSE earnings announcement dates
    python -m services.earnings_engine.run_backtest --mode announcement

    # Custom range and universe
    python -m services.earnings_engine.run_backtest \\
        --start 2022-01-01 --end 2026-04-30 \\
        --universe nifty500 \\
        --output results/earnings_backtest_4yr.json

    # Tune thresholds
    python -m services.earnings_engine.run_backtest --min-gap 3.0 --min-rvol 3.0

Options:
    --mode         scan (default) | announcement
    --universe     nifty50 | nifty500 (default) | all_nse
    --symbols      Space-separated NSE symbols (overrides --universe)
    --start        Start date YYYY-MM-DD (default: 4 years ago)
    --end          End date YYYY-MM-DD (default: today)
    --min-gap      Minimum gap % (default: 3.0)
    --max-gap      Maximum gap % — above this = circuit risk (default: 12.0)
    --min-rvol     Base RVOL floor; actual threshold = max(min_rvol, scaled) (default: 3.0)
    --stop-mult    ATR multiplier for stop (default: 1.5)
    --target-mult  ATR multiplier for target (default: 3.0)
    --max-hold     Max holding period in days (default: 15)
    --output       Save JSON results to this path
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import date, timedelta

from rich.console import Console
from rich.table import Table

from services.earnings_engine.backtest import EarningsBacktestEngine, EarningsBacktestTrade

console = Console()


# ── Universe helpers ──────────────────────────────────────────────────────────

def _get_symbols(universe: str) -> list[str]:
    from services.data_ingestion.nifty500_instruments import (
        NIFTY500, get_live_universe, get_nifty500_symbols
    )
    if universe == "nifty50":
        return [sym for sym, _, _ in NIFTY500[:50]]
    if universe == "nifty500":
        return get_nifty500_symbols()
    if universe == "all_nse":
        return get_live_universe()
    raise ValueError(f"Unknown universe: {universe}")


# ── NSE announcement calendar (async fetch) ───────────────────────────────────

async def _fetch_announcement_calendar(
    start: date,
    end:   date,
) -> dict[date, set[str]]:
    """
    Fetch NSE corporate announcement dates for the backtest period.
    Month-by-month (~48 calls for 4 years). Returns {date → {symbol, ...}}.
    """
    import calendar as _cal
    from services.earnings_engine.announcements import (
        _nse_session, NSE_ANN_URL, _RESULTS_SUBJECTS, _is_results_announcement
    )
    from datetime import datetime

    result: dict[date, set[str]] = {}
    current = start.replace(day=1)

    console.print(f"[cyan]Fetching NSE announcement calendar {start} → {end}[/cyan]")

    while current <= end:
        last_day  = _cal.monthrange(current.year, current.month)[1]
        month_end = min(end, date(current.year, current.month, last_day))
        from_str  = current.strftime("%d-%m-%Y")
        to_str    = month_end.strftime("%d-%m-%Y")

        try:
            client = await _nse_session()
            try:
                resp = await client.get(
                    NSE_ANN_URL,
                    params={"index": "equities", "from_date": from_str, "to_date": to_str},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        for item in data:
                            subject  = (item.get("subject") or item.get("desc") or "").lower()
                            sym      = (item.get("symbol") or "").strip().upper()
                            raw_date = (item.get("date") or item.get("bm_date") or
                                        item.get("filingDate") or "")
                            if not sym or not _is_results_announcement(subject):
                                continue
                            # sort_date is the reliable date field: "2024-10-19 19:51:57"
                            raw_date = (item.get("sort_date") or item.get("an_dt") or
                                        item.get("date") or item.get("bm_date") or "")
                            ann_date = None
                            for fmt in ("%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S",
                                        "%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y"):
                                try:
                                    ann_date = datetime.strptime(raw_date[:19], fmt).date()
                                    break
                                except (ValueError, TypeError):
                                    continue
                            if ann_date is None:
                                ann_date = current
                            # NSE results are almost always filed after market close.
                            # The gap appears next morning — shift to next calendar day.
                            # The price scan will naturally skip weekends/holidays (no bar).
                            ann_date = ann_date + timedelta(days=1)
                            result.setdefault(ann_date, set()).add(sym)
                else:
                    console.print(
                        f"[yellow]NSE API {resp.status_code} for {from_str}–{to_str}[/yellow]"
                    )
            finally:
                await client.aclose()
        except Exception as e:
            console.print(f"[yellow]NSE fetch error {from_str}: {e}[/yellow]")

        await asyncio.sleep(1.0)

        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)

    total_events = sum(len(v) for v in result.values())
    console.print(
        f"[green]Calendar fetched: {total_events} events across {len(result)} dates[/green]"
    )
    return result


# ── Reporting ─────────────────────────────────────────────────────────────────

def _print_report(
    trades: list[EarningsBacktestTrade],
    mode:   str,
    start:  date,
    end:    date,
    engine: EarningsBacktestEngine,
) -> None:
    if not trades:
        console.print("[red]No trades found. Try widening date range or lowering thresholds.[/red]")
        return

    wins   = [t for t in trades if t.is_win]
    losses = [t for t in trades if not t.is_win and t.exit_reason == "STOP"]
    holds  = [t for t in trades if t.exit_reason == "MAX_HOLD"]

    wr      = len(wins) / len(trades) * 100
    avg_pnl = sum(t.pnl_pct for t in trades) / len(trades)
    avg_win  = sum(t.pnl_pct for t in wins)   / max(len(wins), 1)
    avg_loss = sum(t.pnl_pct for t in losses)  / max(len(losses), 1)
    pf = abs(avg_win * len(wins) / (avg_loss * len(losses))) if losses and avg_loss != 0 else float("inf")

    console.print()
    console.print("[bold white]═══ Earnings Gap-and-Go Backtest ═══[/bold white]")
    console.print(f"  Period   : {start} → {end}")
    console.print(f"  Mode     : [cyan]{mode}[/cyan]")
    console.print(
        f"  Criteria : gap {engine.min_gap_pct}–{engine.max_gap_pct}%  "
        f"RVOL ≥ {engine.min_rvol}× (scaled)  "
        f"Stop {engine.stop_mult}×ATR (gap-floor)  "
        f"Target {engine.target_mult}×ATR  "
        f"MaxHold {engine.max_hold}d + trailing stop"
    )
    console.print()

    tbl = Table(show_header=True, header_style="bold cyan")
    tbl.add_column("Metric",    style="white")
    tbl.add_column("Value",     style="bold")
    tbl.add_row("Total signals",  str(len(trades)))
    tbl.add_row("Win rate",        f"{wr:.1f}%")
    tbl.add_row("Avg P&L/trade",   f"{avg_pnl:+.2f}%")
    tbl.add_row("Avg win",         f"{avg_win:+.2f}%")
    tbl.add_row("Avg loss",        f"{avg_loss:+.2f}%")
    tbl.add_row("Profit factor",   f"{pf:.2f}×")
    tbl.add_row("TARGET exits",    str(len(wins)))
    tbl.add_row("STOP exits",      str(len(losses)))
    tbl.add_row("MAX_HOLD exits",  f"{len(holds)} ({len(holds)/len(trades)*100:.0f}%)")
    console.print(tbl)

    # Confidence tier breakdown
    console.print()
    tier_table = Table(show_header=True, header_style="bold magenta", title="By Confidence Tier")
    tier_table.add_column("Tier")
    tier_table.add_column("Signals")
    tier_table.add_column("WR")
    tier_table.add_column("Avg P&L")
    for conf, label in [(68, "Base (68): gap 3–5%, RVOL 3–4×"),
                        (74, "Moderate (74): gap ≥5% OR RVOL ≥4×"),
                        (80, "Strong (80): gap ≥7% AND RVOL ≥5×")]:
        tier = [t for t in trades if t.confidence == conf]
        if not tier:
            continue
        tier_wr  = sum(1 for t in tier if t.is_win) / len(tier) * 100
        tier_pnl = sum(t.pnl_pct for t in tier) / len(tier)
        colour   = "green" if tier_wr >= 40 else "yellow" if tier_wr >= 30 else "red"
        tier_table.add_row(
            label, str(len(tier)),
            f"[{colour}]{tier_wr:.1f}%[/{colour}]",
            f"{tier_pnl:+.2f}%",
        )
    console.print(tier_table)

    # Gap bucket breakdown
    console.print()
    gap_table = Table(show_header=True, header_style="bold blue", title="By Gap Size")
    gap_table.add_column("Gap bucket")
    gap_table.add_column("Signals")
    gap_table.add_column("WR")
    gap_table.add_column("Avg P&L")
    buckets = [
        (3.0, 5.0,   "3.0–5.0%"),
        (5.0, 7.0,   "5.0–7.0%"),
        (7.0, 10.0,  "7.0–10.0%"),
        (10.0, 12.0, "10.0–12.0%"),
    ]
    for lo, hi, label in buckets:
        bucket = [t for t in trades if lo <= t.gap_pct < hi]
        if not bucket:
            continue
        bwr  = sum(1 for t in bucket if t.is_win) / len(bucket) * 100
        bpnl = sum(t.pnl_pct for t in bucket) / len(bucket)
        colour = "green" if bwr >= 40 else "yellow" if bwr >= 30 else "red"
        gap_table.add_row(
            label, str(len(bucket)),
            f"[{colour}]{bwr:.1f}%[/{colour}]",
            f"{bpnl:+.2f}%",
        )
    console.print(gap_table)

    # Exit day distribution (PEAD check)
    console.print()
    day_table = Table(show_header=True, header_style="bold yellow", title="Exit Day Distribution")
    day_table.add_column("Day")
    day_table.add_column("Exits")
    day_table.add_column("WR of exits")
    for day_bucket, label in [(range(1, 4), "Day 1–3"), (range(4, 8), "Day 4–7"),
                               (range(8, 12), "Day 8–11"), (range(12, 16), "Day 12–15")]:
        bucket = [t for t in trades if t.exit_day in day_bucket]
        if not bucket:
            continue
        bwr = sum(1 for t in bucket if t.is_win) / len(bucket) * 100
        colour = "green" if bwr >= 40 else "yellow" if bwr >= 30 else "red"
        day_table.add_row(label, str(len(bucket)), f"[{colour}]{bwr:.1f}%[/{colour}]")
    console.print(day_table)

    # Recommendation
    console.print()
    if wr >= 40 and avg_pnl > 0 and pf >= 1.5:
        console.print("[bold green]✓ Signal is profitable. Thresholds validated.[/bold green]")
    elif wr >= 35 and avg_pnl > 0:
        console.print("[yellow]⚠ Marginal edge. Add Claude fundamental gate to lift WR.[/yellow]")
    else:
        console.print("[bold red]✗ Weak signal. Add fundamental filter (EPS beat quality).[/bold red]")

    strong = [t for t in trades if t.confidence == 80]
    if strong:
        swr = sum(1 for t in strong if t.is_win) / len(strong) * 100
        colour = "green" if swr >= 50 else "yellow"
        console.print(
            f"  [{colour}]→ Strong tier (gap ≥7% + RVOL ≥5×): {swr:.0f}% WR "
            f"on {len(strong)} trades[/{colour}]"
        )


# ── CLI ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Earnings gap-and-go backtest",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--mode",        default="scan", choices=["scan", "announcement"])
    parser.add_argument("--universe",    default="nifty500",
                        choices=["nifty50", "nifty500", "all_nse"])
    parser.add_argument("--symbols",     nargs="+", default=None)
    parser.add_argument("--start",       default=None)
    parser.add_argument("--end",         default=None)
    parser.add_argument("--min-gap",     type=float, default=3.0)
    parser.add_argument("--max-gap",     type=float, default=12.0)
    parser.add_argument("--min-rvol",    type=float, default=3.0)
    parser.add_argument("--stop-mult",   type=float, default=1.5)
    parser.add_argument("--target-mult", type=float, default=3.0)
    parser.add_argument("--max-hold",    type=int,   default=15)
    parser.add_argument("--output",      default=None)
    args = parser.parse_args()

    end_dt   = date.fromisoformat(args.end)   if args.end   else date.today()
    start_dt = date.fromisoformat(args.start) if args.start else end_dt - timedelta(days=4*365)

    symbols = args.symbols if args.symbols else _get_symbols(args.universe)
    console.print(
        f"[bold]Earnings backtest[/bold] | mode=[cyan]{args.mode}[/cyan] | "
        f"symbols=[cyan]{len(symbols)}[/cyan] | {start_dt} → {end_dt}"
    )

    ann_dates = None
    if args.mode == "announcement":
        console.print("[yellow]Fetching NSE announcement calendar (~4 min for 4yr)...[/yellow]")
        ann_dates = await _fetch_announcement_calendar(start_dt, end_dt)

    engine = EarningsBacktestEngine(
        min_gap_pct  = args.min_gap,
        max_gap_pct  = args.max_gap,
        min_rvol     = args.min_rvol,
        stop_mult    = args.stop_mult,
        target_mult  = args.target_mult,
        max_hold     = args.max_hold,
    )

    console.print(f"\n[cyan]Scanning {len(symbols)} symbols...[/cyan]")
    loop = asyncio.get_running_loop()
    import functools
    trades = await loop.run_in_executor(
        None,
        functools.partial(engine.run, symbols, start_dt, end_dt, ann_dates, True)
    )

    _print_report(trades, args.mode, start_dt, end_dt, engine)

    if args.output and trades:
        import os
        os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else ".", exist_ok=True)
        data = {
            "meta": {
                "mode": args.mode, "start": str(start_dt), "end": str(end_dt),
                "symbols": len(symbols), "min_gap_pct": args.min_gap,
                "max_gap_pct": args.max_gap, "min_rvol": args.min_rvol,
                "stop_mult": args.stop_mult, "target_mult": args.target_mult,
                "max_hold": args.max_hold,
            },
            "summary": {
                "total": len(trades),
                "wins":  sum(1 for t in trades if t.is_win),
                "wr_pct": round(sum(1 for t in trades if t.is_win) / len(trades) * 100, 1) if trades else 0,
                "avg_pnl_pct": round(sum(t.pnl_pct for t in trades) / len(trades), 2) if trades else 0,
            },
            "trades": [asdict(t) | {"date": str(t.date)} for t in trades],
        }
        with open(args.output, "w") as f:
            json.dump(data, f, indent=2, default=str)
        console.print(f"\n[green]Results saved → {args.output}[/green]")


if __name__ == "__main__":
    asyncio.run(main())
