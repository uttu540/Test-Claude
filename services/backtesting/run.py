"""
services/backtesting/run.py
─────────────────────────────
CLI entrypoint for running backtests.

Usage:
    # Backtest Nifty 50 for the last 90 days
    python -m services.backtesting.run

    # Custom symbols and date range
    python -m services.backtesting.run \\
        --symbols RELIANCE TCS INFY HDFCBANK \\
        --days 180

    # Full Nifty 50, save results to JSON
    python -m services.backtesting.run \\
        --universe nifty50 \\
        --days 90 \\
        --output results/backtest_q1.json

    # Disable regime filtering to compare
    python -m services.backtesting.run --no-regime-filter

Options:
    --symbols       Space-separated list of NSE symbols (overrides --universe)
    --universe      nifty50 | nifty500 (default: nifty50)
    --days          Number of calendar days to look back (default: 90)
    --start         Start date YYYY-MM-DD (overrides --days)
    --end           End date YYYY-MM-DD (default: today)
    --timeframes    Space-separated list e.g. 15min 1hr 1day
    --output        Path to save JSON results
    --no-regime-filter  Disable regime-based signal gating
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta

import structlog
from rich.console import Console

from services.backtesting.engine import BacktestEngine
from services.backtesting.reporter import BacktestReporter

log     = structlog.get_logger(__name__)
console = Console()


# ── Universe definitions ──────────────────────────────────────────────────────

def _get_universe(name: str) -> list[str]:
    """Return a list of NSE symbols for a named universe."""
    if name == "nifty50":
        # Core Nifty 50 constituents (as of 2025)
        return [
            "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
            "HINDUNILVR", "ITC", "SBIN", "BAJFINANCE", "BHARTIARTL",
            "KOTAKBANK", "LT", "HCLTECH", "ASIANPAINT", "AXISBANK",
            "MARUTI", "SUNPHARMA", "TITAN", "ULTRACEMCO", "NESTLEIND",
            "WIPRO", "ONGC", "NTPC", "POWERGRID", "COALINDIA",
            "TATAMOTORS", "TATASTEEL", "JSWSTEEL", "ADANIENT", "ADANIPORTS",
            "BAJAJFINSV", "BAJAJ-AUTO", "HEROMOTOCO", "EICHERMOT", "M&M",
            "CIPLA", "DRREDDY", "DIVISLAB", "APOLLOHOSP", "HDFCLIFE",
            "SBILIFE", "ICICIPRULI", "TECHM", "INDUSINDBK", "GRASIM",
            "HINDALCO", "VEDL", "BPCL", "IOC", "UPL",
        ]

    if name == "nifty500":
        # For nifty500 we use nifty50 + a subset of mid-caps for practicality
        # Full 500-stock list would require a separate data file
        base = _get_universe("nifty50")
        midcap_sample = [
            "TATACONSUM", "PIDILITIND", "SIEMENS", "ABB", "HAVELLS",
            "VOLTAS", "MUTHOOTFIN", "CHOLAFIN", "BAJAJHLDNG", "BRITANNIA",
            "GODREJCP", "MARICO", "DABUR", "BERGEPAINT", "COLPAL",
            "PGHH", "NAUKRI", "IRCTC", "DMART", "ZOMATO",
            "PAYTM", "NYKAA", "POLICYBZR", "STARTRC", "TATATECH",
        ]
        return base + midcap_sample

    return []


# ── Argument parsing ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog        = "python -m services.backtesting.run",
        description = "Run a backtest on NSE symbols using the trading bot's signal engine.",
    )
    parser.add_argument(
        "--symbols", nargs="+", metavar="SYM",
        help="NSE symbols to backtest (overrides --universe)",
    )
    parser.add_argument(
        "--universe", default="nifty50", choices=["nifty50", "nifty500"],
        help="Predefined symbol universe (default: nifty50)",
    )
    parser.add_argument(
        "--days", type=int, default=90,
        help="Calendar days to look back from --end (default: 90)",
    )
    parser.add_argument(
        "--start", type=str,
        help="Start date YYYY-MM-DD (overrides --days)",
    )
    parser.add_argument(
        "--end", type=str, default=date.today().isoformat(),
        help="End date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--timeframes", nargs="+", default=["15min", "1hr", "1day"],
        metavar="TF",
        help="Timeframes to analyse (default: 15min 1hr 1day)",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Path to save JSON results (optional)",
    )
    parser.add_argument(
        "--no-regime-filter", action="store_true",
        help="Disable regime-based signal gating (useful for comparison)",
    )
    return parser.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    args = _parse_args()

    # Resolve symbols
    symbols = args.symbols or _get_universe(args.universe)
    if not symbols:
        console.print("[red]No symbols found. Use --symbols or --universe.[/red]")
        sys.exit(1)

    # Resolve dates
    end_date   = date.fromisoformat(args.end)
    start_date = (
        date.fromisoformat(args.start)
        if args.start
        else end_date - timedelta(days=args.days)
    )

    console.print(
        f"\n[bold]Backtest[/bold]  "
        f"{len(symbols)} symbols  |  "
        f"{start_date} → {end_date}  |  "
        f"Timeframes: {', '.join(args.timeframes)}  |  "
        f"Regime filter: {'OFF' if args.no_regime_filter else 'ON'}\n"
    )

    engine = BacktestEngine(
        symbols       = symbols,
        start_date    = start_date,
        end_date      = end_date,
        timeframes    = args.timeframes,
        regime_aware  = not args.no_regime_filter,
    )

    result  = await engine.run()
    reporter = BacktestReporter()
    metrics  = reporter.compute(result)
    reporter.print(metrics)

    if args.output:
        reporter.save_json(metrics, args.output)
        console.print(f"[dim]Results saved to {args.output}[/dim]\n")


if __name__ == "__main__":
    asyncio.run(main())
