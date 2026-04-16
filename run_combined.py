"""
run_combined.py
───────────────
Runs both the Momentum Engine (long-only, trending-up markets) and the
Swing/Normal Engine (long + short, all regimes) on the exact same timeline
and universe, then prints a side-by-side comparison report.

Idea: the two engines are complementary —
  • Momentum Engine fires when Nifty is TRENDING_UP  → captures bull-run longs
  • Swing Engine   fires in all regimes              → captures ranging / down-market shorts

Running them together on one timeline lets you see how they divide the work
across different market phases.

Usage:
    # 2024 full year (bull H1 + ranging H2) — Nifty 50
    python run_combined.py --start 2024-01-01 --end 2024-12-31

    # Nifty 500, wider window
    python run_combined.py --universe nifty500 --start 2024-01-01 --end 2024-12-31

    # Save combined JSON
    python run_combined.py --start 2024-01-01 --end 2024-12-31 \\
        --output results/combined_2024.json

Options:
    --universe      nifty50 | nifty500   (default: nifty50)
    --start         YYYY-MM-DD           (default: 365 days ago)
    --end           YYYY-MM-DD           (default: today)
    --output        path/to/output.json
    --min-score     momentum min confluence score   (default: 8)
    --min-conf      momentum min confidence         (default: 65)
    --swing-conf    swing engine min confidence     (default: 75)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import date, timedelta

import numpy as np
import structlog
from rich.console import Console
from rich.table import Table

from services.backtesting.engine import BacktestEngine, SimulatedTrade
from services.backtesting.reporter import BacktestReporter
from services.momentum_engine.backtest import MomentumBacktestEngine, MomentumTrade

log     = structlog.get_logger(__name__)
console = Console()


# ── Universe (mirrors both run.py files) ──────────────────────────────────────

def _get_universe(name: str) -> list[str]:
    nifty50 = [
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
    if name == "nifty50":
        return nifty50

    midcap = [
        "MUTHOOTFIN", "CHOLAFIN", "BAJAJHLDNG", "SUNDARMFIN", "LICHSGFIN",
        "MANAPPURAM", "PNBHOUSING", "AAVAS", "HOMEFIRST", "CREDITACC",
        "NAUKRI", "PERSISTENT", "LTTS", "COFORGE", "MPHASIS",
        "TATATECH", "KPITTECH", "ZENSARTECH", "MASTEK", "SONATSOFTW",
        "ALKEM", "LALPATHLAB", "METROPOLIS", "IPCALAB", "JBCHEPHARM",
        "SYNGENE", "GRANULES", "GLAND", "NATCOPHARM", "ERIS",
        "TATACONSUM", "GODREJCP", "MARICO", "DABUR", "EMAMILTD",
        "COLPAL", "PGHH", "VBL", "RADICO", "PATANJALI",
        "SIEMENS", "ABB", "HAVELLS", "VOLTAS", "CUMMINSIND",
        "THERMAX", "BHEL", "GRINDWELL", "SCHAEFFLER", "TIMKEN",
        "MOTHERSON", "EXIDEIND", "AMARAJABAT", "SUNDRMFAST", "ENDURANCE",
        "SUBROS", "SUPRAJIT", "GABRIEL", "MINDA", "BORORENEW",
        "OBEROIRLTY", "PRESTIGE", "GODREJPROP", "SOBHA", "BRIGADE",
        "PHOENIXLTD", "NESCO", "MAHSEAMLES", "CAPACITE", "KNRCON",
        "PIDILITIND", "BERGEPAINT", "KANSAINER", "AKZOINDIA", "VINATIORGA",
        "DEEPAKNTR", "NAVINFLUOR", "AARTI", "SUDARSCHEM", "FINEORG",
        "IRCTC", "DMART", "NYKAA", "ZOMATO", "JUBLFOOD",
        "WESTLIFE", "SAPPHIRE", "DEVYANI", "BARBEQUE", "EIDPARRY",
        "CESC", "TORNTPOWER", "JSWENERGY", "GREENPANEL", "RPOWER",
        "TATAPOWER", "ADANIGREEN", "ADANITRANS", "NHPC", "SJVN",
    ]
    smallcap = [
        "UJJIVANSFB", "EQUITASBNK", "SURYODAY", "ESAFSFB", "UTKARSHBNK",
        "PAISALO", "IIFL", "MOTILALOS", "ANGELONE", "5PAISA",
        "RATEGAIN", "TANLA", "INTELLECT", "NEWGEN", "NUCLEUS",
        "SAKSOFT", "DATAMATICS", "VAKRANGEE", "INFOBEAN", "CYIENT",
        "SUVEN", "SOLARA", "SEQUENT", "LAURUS", "NUVOCO",
        "MARKSANS", "SHILPAMED", "POLYMED", "MEDICAMEN", "BLISSGVS",
        "BEL", "HAL", "BEML", "MTAR", "PARAS",
        "DATAPATTNS", "ASTRALDTEX", "GREAVESCOT", "ELGIEQUIP", "KALYANKJIL",
        "PAGEIND", "GOKEX", "RUPA", "NIITLTD", "GARFIBRES",
        "KRBL", "LTFOODS", "AVANTIFEED", "APEX", "GLOBUSSPR",
    ]
    return nifty50 + midcap + smallcap


def _get_segments() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for sym in _get_universe("nifty50"):
        mapping[sym] = "LARGE_CAP"
    for sym in [
        "MUTHOOTFIN", "CHOLAFIN", "BAJAJHLDNG", "SUNDARMFIN", "LICHSGFIN",
        "MANAPPURAM", "PNBHOUSING", "AAVAS", "HOMEFIRST", "CREDITACC",
        "NAUKRI", "PERSISTENT", "LTTS", "COFORGE", "MPHASIS",
        "TATATECH", "KPITTECH", "ZENSARTECH", "MASTEK", "SONATSOFTW",
        "ALKEM", "LALPATHLAB", "METROPOLIS", "IPCALAB", "JBCHEPHARM",
        "SYNGENE", "GRANULES", "GLAND", "NATCOPHARM", "ERIS",
        "TATACONSUM", "GODREJCP", "MARICO", "DABUR", "EMAMILTD",
        "COLPAL", "PGHH", "VBL", "RADICO", "PATANJALI",
        "SIEMENS", "ABB", "HAVELLS", "VOLTAS", "CUMMINSIND",
        "THERMAX", "BHEL", "GRINDWELL", "SCHAEFFLER", "TIMKEN",
        "MOTHERSON", "EXIDEIND", "AMARAJABAT", "SUNDRMFAST", "ENDURANCE",
        "SUBROS", "SUPRAJIT", "GABRIEL", "MINDA", "BORORENEW",
        "OBEROIRLTY", "PRESTIGE", "GODREJPROP", "SOBHA", "BRIGADE",
        "PHOENIXLTD", "NESCO", "MAHSEAMLES", "CAPACITE", "KNRCON",
        "PIDILITIND", "BERGEPAINT", "KANSAINER", "AKZOINDIA", "VINATIORGA",
        "DEEPAKNTR", "NAVINFLUOR", "AARTI", "SUDARSCHEM", "FINEORG",
        "IRCTC", "DMART", "NYKAA", "ZOMATO", "JUBLFOOD",
        "WESTLIFE", "SAPPHIRE", "DEVYANI", "BARBEQUE", "EIDPARRY",
        "CESC", "TORNTPOWER", "JSWENERGY", "GREENPANEL", "RPOWER",
        "TATAPOWER", "ADANIGREEN", "ADANITRANS", "NHPC", "SJVN",
    ]:
        mapping[sym] = "MID_CAP"
    for sym in [
        "UJJIVANSFB", "EQUITASBNK", "SURYODAY", "ESAFSFB", "UTKARSHBNK",
        "PAISALO", "IIFL", "MOTILALOS", "ANGELONE", "5PAISA",
        "RATEGAIN", "TANLA", "INTELLECT", "NEWGEN", "NUCLEUS",
        "SAKSOFT", "DATAMATICS", "VAKRANGEE", "INFOBEAN", "CYIENT",
        "SUVEN", "SOLARA", "SEQUENT", "LAURUS", "NUVOCO",
        "MARKSANS", "SHILPAMED", "POLYMED", "MEDICAMEN", "BLISSGVS",
        "BEL", "HAL", "BEML", "MTAR", "PARAS",
        "DATAPATTNS", "ASTRALDTEX", "GREAVESCOT", "ELGIEQUIP", "KALYANKJIL",
        "PAGEIND", "GOKEX", "RUPA", "NIITLTD", "GARFIBRES",
        "KRBL", "LTFOODS", "AVANTIFEED", "APEX", "GLOBUSSPR",
    ]:
        mapping[sym] = "SMALL_CAP"
    return mapping


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ym(d: date) -> str:
    """Return 'YYYY-MM' for a date."""
    return d.strftime("%Y-%m")


def _metrics(pnls: list[float]) -> dict:
    if not pnls:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "net_pnl": 0.0,
                "avg_pnl": 0.0, "max_dd": 0.0, "sharpe": 0.0, "profit_factor": 0.0}
    arr      = np.array(pnls)
    winners  = [p for p in pnls if p > 0]
    losers   = [p for p in pnls if p <= 0]
    gp       = sum(winners)
    gl       = sum(losers)
    pf       = abs(gp / gl) if gl != 0 else float("inf")
    cum      = np.cumsum(arr)
    peak     = np.maximum.accumulate(cum)
    max_dd   = float((cum - peak).min())
    sharpe   = float((arr.mean() / arr.std()) * np.sqrt(252)) if arr.std() > 0 else 0.0
    return {
        "trades":        len(pnls),
        "wins":          len(winners),
        "win_rate":      round(len(winners) / len(pnls), 4),
        "net_pnl":       round(sum(pnls), 2),
        "avg_pnl":       round(float(arr.mean()), 2),
        "max_dd":        round(max_dd, 2),
        "sharpe":        round(sharpe, 2),
        "profit_factor": round(pf, 2),
    }


# ── Combined report ───────────────────────────────────────────────────────────

def _print_combined_report(
    m_trades: list[MomentumTrade],
    s_trades: list[SimulatedTrade],
    start: date,
    end: date,
    n_symbols: int,
) -> None:
    console.print()
    console.rule("[bold magenta]Combined Engine Report[/bold magenta]")
    console.print(
        f"[dim]Period: {start} → {end}   |   Universe: {n_symbols} symbols[/dim]\n"
    )

    m_pnls = [t.pnl for t in m_trades]
    s_pnls = [t.pnl for t in s_trades]
    all_pnls = m_pnls + s_pnls

    m = _metrics(m_pnls)
    s = _metrics(s_pnls)
    c = _metrics(all_pnls)

    # ── Side-by-side summary ──────────────────────────────────────────────────
    tbl = Table(box=None, padding=(0, 3))
    tbl.add_column("Metric",          style="bold white",  no_wrap=True)
    tbl.add_column("Momentum (Long)", style="cyan",        justify="right")
    tbl.add_column("Swing (All)",     style="yellow",      justify="right")
    tbl.add_column("Combined",        style="bold",        justify="right")

    def _pnl_str(v: float) -> str:
        col = "green" if v >= 0 else "red"
        return f"[{col}]₹{v:,.2f}[/{col}]"

    def _wr_str(v: float) -> str:
        col = "green" if v >= 0.5 else "red"
        return f"[{col}]{v:.1%}[/{col}]"

    tbl.add_row("Trades",
        str(m["trades"]), str(s["trades"]), str(c["trades"]))
    tbl.add_row("Wins",
        f"[green]{m['wins']}[/green]",
        f"[green]{s['wins']}[/green]",
        f"[green]{c['wins']}[/green]")
    tbl.add_row("Win Rate",
        _wr_str(m["win_rate"]), _wr_str(s["win_rate"]), _wr_str(c["win_rate"]))
    tbl.add_row("Net P&L",
        _pnl_str(m["net_pnl"]), _pnl_str(s["net_pnl"]), _pnl_str(c["net_pnl"]))
    tbl.add_row("Avg P&L / Trade",
        f"₹{m['avg_pnl']:,.2f}", f"₹{s['avg_pnl']:,.2f}", f"₹{c['avg_pnl']:,.2f}")
    tbl.add_row("Profit Factor",
        f"{m['profit_factor']:.2f}x", f"{s['profit_factor']:.2f}x", f"{c['profit_factor']:.2f}x")
    tbl.add_row("Max Drawdown",
        f"[red]₹{m['max_dd']:,.2f}[/red]",
        f"[red]₹{s['max_dd']:,.2f}[/red]",
        f"[red]₹{c['max_dd']:,.2f}[/red]")
    tbl.add_row("Sharpe Ratio",
        f"{m['sharpe']:.2f}", f"{s['sharpe']:.2f}", f"{c['sharpe']:.2f}")

    console.print(tbl)

    # ── Swing direction breakdown ─────────────────────────────────────────────
    if s_trades:
        console.print()
        console.rule("[dim]Swing Engine — Long vs Short[/dim]")
        by_dir: dict[str, list[float]] = defaultdict(list)
        for t in s_trades:
            by_dir[t.direction].append(t.pnl)

        dt = Table(box=None, padding=(0, 3))
        dt.add_column("Direction", style="bold")
        dt.add_column("Trades",    justify="right")
        dt.add_column("Win Rate",  justify="right")
        dt.add_column("Net P&L",   justify="right")
        dt.add_column("Avg P&L",   justify="right")
        for direction, pnls in sorted(by_dir.items()):
            dm = _metrics(pnls)
            dt.add_row(
                direction,
                str(dm["trades"]),
                _wr_str(dm["win_rate"]),
                _pnl_str(dm["net_pnl"]),
                f"₹{dm['avg_pnl']:,.2f}",
            )
        console.print(dt)

    # ── Monthly P&L breakdown ─────────────────────────────────────────────────
    console.print()
    console.rule("[dim]Monthly P&L Breakdown[/dim]")

    months: dict[str, dict[str, float]] = defaultdict(lambda: {"momentum": 0.0, "swing": 0.0})
    for t in m_trades:
        months[_ym(t.entry_date)]["momentum"] += t.pnl
    for t in s_trades:
        months[_ym(t.entry_date)]["swing"] += t.pnl

    mt = Table(box=None, padding=(0, 3))
    mt.add_column("Month",    style="bold white", no_wrap=True)
    mt.add_column("Momentum", justify="right")
    mt.add_column("Swing",    justify="right")
    mt.add_column("Combined", justify="right")

    for ym in sorted(months):
        mv = months[ym]["momentum"]
        sv = months[ym]["swing"]
        cv = mv + sv
        mt.add_row(ym, _pnl_str(mv), _pnl_str(sv), _pnl_str(cv))

    console.print(mt)
    console.print()


# ── JSON save ─────────────────────────────────────────────────────────────────

def _save_json(
    m_trades: list[MomentumTrade],
    s_trades: list[SimulatedTrade],
    start: date,
    end: date,
    path: str,
) -> None:
    m_pnls   = [t.pnl for t in m_trades]
    s_pnls   = [t.pnl for t in s_trades]
    all_pnls = m_pnls + s_pnls

    data = {
        "start_date": str(start),
        "end_date":   str(end),
        "combined": _metrics(all_pnls),
        "momentum_engine": {
            **_metrics(m_pnls),
            "trades": [
                {
                    "engine":           "momentum",
                    "symbol":           t.symbol,
                    "signal_type":      t.signal_type,
                    "direction":        "LONG",
                    "entry_date":       str(t.entry_date),
                    "entry_price":      t.entry_price,
                    "exit_price":       t.exit_price,
                    "exit_reason":      t.exit_reason,
                    "pnl":              t.pnl,
                    "pnl_pct":          t.pnl_pct,
                    "holding_days":     t.holding_days,
                    "confluence_score": t.confluence_score,
                    "regime":           t.regime,
                }
                for t in m_trades
            ],
        },
        "swing_engine": {
            **_metrics(s_pnls),
            "trades": [
                {
                    "engine":           "swing",
                    "symbol":           t.symbol,
                    "signal_type":      t.signal_type,
                    "direction":        t.direction,
                    "entry_date":       str(t.entry_date),
                    "entry_price":      t.entry_price,
                    "exit_price":       t.exit_price,
                    "exit_reason":      t.exit_reason,
                    "pnl":              t.pnl,
                    "pnl_pct":          t.pnl_pct,
                    "holding_days":     getattr(t, "holding_candles", 0),
                    "confluence_score": t.confluence_score,
                    "regime":           t.regime,
                }
                for t in s_trades
            ],
        },
    }

    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    console.print(f"[dim]Combined results saved → {path}[/dim]\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog        = "python run_combined.py",
        description = "Run Momentum + Swing engines on the same timeline.",
    )
    p.add_argument("--universe",   default="nifty50", choices=["nifty50", "nifty500"])
    p.add_argument("--start",      type=str,
                   default=(date.today() - timedelta(days=365)).isoformat())
    p.add_argument("--end",        type=str, default=date.today().isoformat())
    p.add_argument("--output",     type=str, default=None)
    p.add_argument("--min-score",  type=int, default=8,
                   help="Momentum engine min confluence score (default: 8)")
    p.add_argument("--min-conf",   type=int, default=65,
                   help="Momentum engine min signal confidence (default: 65)")
    p.add_argument("--swing-conf", type=int, default=75,
                   help="Swing engine min signal confidence (default: 75)")
    return p.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    args = _parse_args()

    symbols  = _get_universe(args.universe)
    seg_map  = _get_segments()
    start    = date.fromisoformat(args.start)
    end      = date.fromisoformat(args.end)

    if not symbols:
        console.print("[red]No symbols found.[/red]")
        sys.exit(1)

    console.print(
        f"\n[bold]Combined Backtest[/bold]  "
        f"{len(symbols)} symbols  |  "
        f"[cyan]{start} → {end}[/cyan]  |  "
        f"Universe: {args.universe.upper()}\n"
    )
    console.print(
        f"  [cyan]Momentum Engine[/cyan]  long-only, TRENDING_UP  "
        f"(min score: {args.min_score}, min conf: {args.min_conf})\n"
        f"  [yellow]Swing Engine[/yellow]    all regimes, long+short  "
        f"(min conf: {args.swing_conf})\n"
    )

    # Run both engines concurrently on the same timeline
    momentum_engine = MomentumBacktestEngine(
        symbols         = symbols,
        start_date      = start,
        end_date        = end,
        symbol_segments = seg_map,
        min_score       = args.min_score,
        min_confidence  = args.min_conf,
    )
    swing_engine = BacktestEngine(
        symbols                = symbols,
        start_date             = start,
        end_date               = end,
        regime_aware           = True,
        min_confidence         = args.swing_conf,
        regime_aligned_only    = True,
        trading_mode           = "swing",
        symbol_segments        = seg_map,
        min_signal_timeframes  = 2,
        min_confirming_signals = 1,
    )

    console.print("[dim]Running both engines in parallel...[/dim]\n")
    m_result, s_result = await asyncio.gather(
        momentum_engine.run(),
        swing_engine.run(),
    )

    # Print individual engine reports
    console.rule("[bold cyan]Momentum Engine Report[/bold cyan]")
    from services.momentum_engine.run import _print_report as _m_print
    _m_print(m_result.trades, start, end, len(symbols))

    console.rule("[bold yellow]Swing Engine Report[/bold yellow]")
    reporter = BacktestReporter()
    metrics  = reporter.compute(s_result)
    reporter.print(metrics)

    # Print combined comparison
    _print_combined_report(m_result.trades, s_result.trades, start, end, len(symbols))

    if args.output:
        _save_json(m_result.trades, s_result.trades, start, end, args.output)


if __name__ == "__main__":
    asyncio.run(main())
