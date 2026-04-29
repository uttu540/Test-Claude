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
        # Nifty 50 + broad Nifty Midcap 150 + Nifty Smallcap 250 representative sample
        # Covers all major sectors: banking, IT, pharma, auto, FMCG, infra, energy,
        # chemicals, defence, real estate, media, textiles, agro, specialty chemicals
        base = _get_universe("nifty50")
        midcap_largecap_extra = [
            # Midcap 150 — financials
            "MUTHOOTFIN", "CHOLAFIN", "BAJAJHLDNG", "SUNDARMFIN", "LICHSGFIN",
            "MANAPPURAM", "PNBHOUSING", "AAVAS", "HOMEFIRST", "CREDITACC",
            # Midcap 150 — IT / tech
            "NAUKRI", "PERSISTENT", "LTTS", "COFORGE", "MPHASIS",
            "TATATECH", "KPITTECH", "ZENSARTECH", "MASTEK", "SONATSOFTW",
            # Midcap 150 — pharma / healthcare
            "ALKEM", "LALPATHLAB", "METROPOLIS", "IPCALAB", "JBCHEPHARM",
            "SYNGENE", "GRANULES", "GLAND", "NATCOPHARM", "ERIS",
            # Midcap 150 — consumer / FMCG
            "TATACONSUM", "GODREJCP", "MARICO", "DABUR", "EMAMILTD",
            "COLPAL", "PGHH", "VBL", "RADICO", "PATANJALI",
            # Midcap 150 — industrials / capital goods
            "SIEMENS", "ABB", "HAVELLS", "VOLTAS", "CUMMINSIND",
            "THERMAX", "BHEL", "GRINDWELL", "SCHAEFFLER", "TIMKEN",
            # Midcap 150 — auto ancillaries
            "MOTHERSON", "EXIDEIND", "AMARAJABAT", "SUNDRMFAST", "ENDURANCE",
            "SUBROS", "SUPRAJIT", "GABRIEL", "MINDA", "BORORENEW",
            # Midcap 150 — real estate / infra
            "OBEROIRLTY", "PRESTIGE", "GODREJPROP", "SOBHA", "BRIGADE",
            "PHOENIXLTD", "NESCO", "MAHSEAMLES", "CAPACITE", "KNRCON",
            # Midcap 150 — chemicals / specialty
            "PIDILITIND", "BERGEPAINT", "KANSAINER", "AKZOINDIA", "VINATIORGA",
            "DEEPAKNTR", "NAVINFLUOR", "AARTI", "SUDARSCHEM", "FINEORG",
            # Midcap 150 — consumer discretionary
            "IRCTC", "DMART", "NYKAA", "ZOMATO", "JUBLFOOD",
            "WESTLIFE", "SAPPHIRE", "DEVYANI", "BARBEQUE", "EIDPARRY",
            # Midcap 150 — energy / utilities
            "CESC", "TORNTPOWER", "JSWENERGY", "GREENPANEL", "RPOWER",
            "TATAPOWER", "ADANIGREEN", "ADANITRANS", "NHPC", "SJVN",
            # Smallcap — financials
            "UJJIVANSFB", "EQUITASBNK", "SURYODAY", "ESAFSFB", "UTKARSHBNK",
            "PAISALO", "IIFL", "MOTILALOS", "ANGELONE", "5PAISA",
            # Smallcap — IT services
            "RATEGAIN", "TANLA", "INTELLECT", "NEWGEN", "NUCLEUS",
            "SAKSOFT", "DATAMATICS", "VAKRANGEE", "INFOBEAN", "CYIENT",
            # Smallcap — pharma
            "SUVEN", "SOLARA", "SEQUENT", "LAURUS", "NUVOCO",
            "MARKSANS", "SHILPAMED", "POLYMED", "MEDICAMEN", "BLISSGVS",
            # Smallcap — industrials / defence
            "BEL", "HAL", "BEML", "MTAR", "PARAS",
            "DATAPATTNS", "ASTRALDTEX", "GREAVESCOT", "ELGIEQUIP", "KALYANKJIL",
            # Smallcap — textiles / agro
            "PAGEIND", "GOKEX", "RUPA", "NIITLTD", "GARFIBRES",
            "KRBL", "LTFOODS", "AVANTIFEED", "APEX", "GLOBUSSPR",
        ]
        return base + midcap_largecap_extra

    return []


def _get_symbol_segment() -> dict[str, str]:
    """Return a mapping of symbol → segment string (LARGE_CAP / MID_CAP / SMALL_CAP).

    Classification logic:
      LARGE_CAP — the 50 Nifty50 constituents
      MID_CAP   — midcap additions from "MUTHOOTFIN" through the energy/utilities block
                  (ending with SJVN)
      SMALL_CAP — smallcap additions from "UJJIVANSFB" onwards
    """
    large_cap = _get_universe("nifty50")

    midcap = [
        # Midcap 150 — financials
        "MUTHOOTFIN", "CHOLAFIN", "BAJAJHLDNG", "SUNDARMFIN", "LICHSGFIN",
        "MANAPPURAM", "PNBHOUSING", "AAVAS", "HOMEFIRST", "CREDITACC",
        # Midcap 150 — IT / tech
        "NAUKRI", "PERSISTENT", "LTTS", "COFORGE", "MPHASIS",
        "TATATECH", "KPITTECH", "ZENSARTECH", "MASTEK", "SONATSOFTW",
        # Midcap 150 — pharma / healthcare
        "ALKEM", "LALPATHLAB", "METROPOLIS", "IPCALAB", "JBCHEPHARM",
        "SYNGENE", "GRANULES", "GLAND", "NATCOPHARM", "ERIS",
        # Midcap 150 — consumer / FMCG
        "TATACONSUM", "GODREJCP", "MARICO", "DABUR", "EMAMILTD",
        "COLPAL", "PGHH", "VBL", "RADICO", "PATANJALI",
        # Midcap 150 — industrials / capital goods
        "SIEMENS", "ABB", "HAVELLS", "VOLTAS", "CUMMINSIND",
        "THERMAX", "BHEL", "GRINDWELL", "SCHAEFFLER", "TIMKEN",
        # Midcap 150 — auto ancillaries
        "MOTHERSON", "EXIDEIND", "AMARAJABAT", "SUNDRMFAST", "ENDURANCE",
        "SUBROS", "SUPRAJIT", "GABRIEL", "MINDA", "BORORENEW",
        # Midcap 150 — real estate / infra
        "OBEROIRLTY", "PRESTIGE", "GODREJPROP", "SOBHA", "BRIGADE",
        "PHOENIXLTD", "NESCO", "MAHSEAMLES", "CAPACITE", "KNRCON",
        # Midcap 150 — chemicals / specialty
        "PIDILITIND", "BERGEPAINT", "KANSAINER", "AKZOINDIA", "VINATIORGA",
        "DEEPAKNTR", "NAVINFLUOR", "AARTI", "SUDARSCHEM", "FINEORG",
        # Midcap 150 — consumer discretionary
        "IRCTC", "DMART", "NYKAA", "ZOMATO", "JUBLFOOD",
        "WESTLIFE", "SAPPHIRE", "DEVYANI", "BARBEQUE", "EIDPARRY",
        # Midcap 150 — energy / utilities
        "CESC", "TORNTPOWER", "JSWENERGY", "GREENPANEL", "RPOWER",
        "TATAPOWER", "ADANIGREEN", "ADANITRANS", "NHPC", "SJVN",
    ]

    smallcap = [
        # Smallcap — financials
        "UJJIVANSFB", "EQUITASBNK", "SURYODAY", "ESAFSFB", "UTKARSHBNK",
        "PAISALO", "IIFL", "MOTILALOS", "ANGELONE", "5PAISA",
        # Smallcap — IT services
        "RATEGAIN", "TANLA", "INTELLECT", "NEWGEN", "NUCLEUS",
        "SAKSOFT", "DATAMATICS", "VAKRANGEE", "INFOBEAN", "CYIENT",
        # Smallcap — pharma
        "SUVEN", "SOLARA", "SEQUENT", "LAURUS", "NUVOCO",
        "MARKSANS", "SHILPAMED", "POLYMED", "MEDICAMEN", "BLISSGVS",
        # Smallcap — industrials / defence
        "BEL", "HAL", "BEML", "MTAR", "PARAS",
        "DATAPATTNS", "ASTRALDTEX", "GREAVESCOT", "ELGIEQUIP", "KALYANKJIL",
        # Smallcap — textiles / agro
        "PAGEIND", "GOKEX", "RUPA", "NIITLTD", "GARFIBRES",
        "KRBL", "LTFOODS", "AVANTIFEED", "APEX", "GLOBUSSPR",
    ]

    mapping: dict[str, str] = {}
    for sym in large_cap:
        mapping[sym] = "LARGE_CAP"
    for sym in midcap:
        mapping[sym] = "MID_CAP"
    for sym in smallcap:
        mapping[sym] = "SMALL_CAP"
    return mapping


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
    parser.add_argument(
        "--min-confidence", type=int, default=75,
        help="Minimum signal confidence to trade (default: 75)",
    )
    parser.add_argument(
        "--no-regime-align", action="store_true",
        help="Allow counter-trend trades (default: only trade with the trend)",
    )
    parser.add_argument(
        "--disabled-signals", nargs="+", default=[], metavar="SIG",
        help="Signal types to exclude e.g. VWAP_RECLAIM ORB_BREAKOUT",
    )
    parser.add_argument(
        "--min-signal-timeframes", type=int, default=2,
        help="Require signal direction to agree on this many TFs (default: 2)",
    )
    parser.add_argument(
        "--min-confirming-signals", type=int, default=1,
        help="Require this many distinct signal types in the same direction (default: 1). "
             "Set ≥ 2 to demand genuine multi-signal confluence before entering a trade.",
    )
    parser.add_argument(
        "--trading-mode", default="swing", choices=["intraday", "swing"],
        help=(
            "Trading mode (default: swing).\n"
            "  swing:    Setup=Daily bias → Trigger=1H entry → hold up to 5 days.\n"
            "  intraday: Setup=1H bias → Trigger=15min entry → EOD exit."
        ),
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

    # Segment mapping (only meaningful for nifty500; nifty50 → all LARGE_CAP)
    symbol_segments = _get_symbol_segment() if not args.symbols else None

    # Resolve dates
    end_date   = date.fromisoformat(args.end)
    start_date = (
        date.fromisoformat(args.start)
        if args.start
        else end_date - timedelta(days=args.days)
    )

    mode_desc = {
        "intraday": "Setup=1H → Trigger=15min → EOD exit",
        "swing":    "Setup=Daily → Trigger=1H → hold up to 5 days",
    }.get(args.trading_mode, args.trading_mode)

    console.print(
        f"\n[bold]Backtest[/bold]  "
        f"{len(symbols)} symbols  |  "
        f"{start_date} → {end_date}  |  "
        f"Mode: [cyan]{args.trading_mode.upper()}[/cyan] ({mode_desc})  |  "
        f"Regime filter: {'OFF' if args.no_regime_filter else 'ON'}  |  "
        f"Min confidence: {args.min_confidence}  |  "
        f"Min signals: {args.min_confirming_signals}\n"
    )

    engine = BacktestEngine(
        symbols                 = symbols,
        start_date              = start_date,
        end_date                = end_date,
        timeframes              = args.timeframes if args.timeframes != ["15min", "1hr", "1day"] else None,
        regime_aware            = not args.no_regime_filter,
        min_confidence          = args.min_confidence,
        regime_aligned_only     = not args.no_regime_align,
        disabled_signals        = args.disabled_signals or None,
        min_signal_timeframes   = args.min_signal_timeframes,
        min_confirming_signals  = args.min_confirming_signals,
        trading_mode            = args.trading_mode,
        symbol_segments         = symbol_segments,
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
