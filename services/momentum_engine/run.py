"""
services/momentum_engine/run.py
─────────────────────────────────
CLI entrypoint for the momentum engine backtest.

Usage:
    # Nifty 50, last 90 days
    python -m services.momentum_engine.run

    # Full year 2025 (H1 bull run = ideal test period)
    python -m services.momentum_engine.run \\
        --universe nifty500 \\
        --start 2025-01-01 \\
        --end 2025-06-30 \\
        --output results/momentum_2025_h1.json

    # Custom symbols
    python -m services.momentum_engine.run \\
        --symbols RELIANCE TCS INFY \\
        --days 180

Options:
    --symbols     Space-separated NSE symbols (overrides --universe)
    --universe    nifty50 | nifty500 (default: nifty50)
    --days        Calendar days to look back (default: 90)
    --start       Start date YYYY-MM-DD (overrides --days)
    --end         End date YYYY-MM-DD (default: today)
    --output      Path to save JSON results
    --min-score   Min confluence score to trade (default: 7)
    --min-conf    Min signal confidence to trade (default: 65)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import date, timedelta

import numpy as np
import structlog
from rich.console import Console
from rich.table import Table

from services.momentum_engine.backtest import MomentumBacktestEngine, MomentumTrade

log     = structlog.get_logger(__name__)
console = Console()


# ── Universe ──────────────────────────────────────────────────────────────────

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

    if name == "nifty500":
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

    return []


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


# ── Report ────────────────────────────────────────────────────────────────────

def _print_report(trades: list[MomentumTrade], start: date, end: date, n_symbols: int) -> None:
    if not trades:
        console.print("[red]No trades generated.[/red]")
        return

    pnls    = [t.pnl for t in trades]
    winners = [t for t in trades if t.pnl > 0]
    losers  = [t for t in trades if t.pnl <= 0]
    net_pnl = sum(pnls)
    wr      = len(winners) / len(trades)

    gross_profit = sum(t.pnl for t in winners)
    gross_loss   = sum(t.pnl for t in losers)
    pf = abs(gross_profit / gross_loss) if gross_loss != 0 else float("inf")

    cum = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum)
    max_dd = float((cum - peak).min())

    pnl_arr = np.array(pnls)
    sharpe  = 0.0
    if pnl_arr.std() > 0:
        sharpe = float((pnl_arr.mean() / pnl_arr.std()) * np.sqrt(252))

    console.print()
    console.rule("[bold cyan]Momentum Engine Backtest Report[/bold cyan]")

    pnl_col = "green" if net_pnl >= 0 else "red"
    summary = Table(show_header=False, box=None, padding=(0, 2))
    summary.add_column(style="bold white", no_wrap=True)
    summary.add_column()

    summary.add_row("Period",        f"{start} → {end}")
    summary.add_row("Symbols",       str(n_symbols))
    summary.add_row("Total Trades",  str(len(trades)))
    summary.add_row("Win / Loss",
        f"[green]{len(winners)}[/green] / [red]{len(losers)}[/red]")
    summary.add_row("Win Rate",      f"{wr:.1%}")
    summary.add_row("Net P&L",
        f"[{pnl_col}]₹{net_pnl:,.2f}[/{pnl_col}]")
    summary.add_row("Avg P&L/Trade", f"₹{np.mean(pnls):,.2f}")
    summary.add_row("Best Trade",    f"[green]₹{max(pnls):,.2f}[/green]")
    summary.add_row("Worst Trade",   f"[red]₹{min(pnls):,.2f}[/red]")
    summary.add_row("Profit Factor", f"{pf:.2f}x")
    summary.add_row("Max Drawdown",  f"[red]₹{max_dd:,.2f}[/red]")
    summary.add_row("Sharpe Ratio",  f"{sharpe:.2f}")
    console.print(summary)

    # By signal type
    console.print()
    console.rule("[dim]By Signal Type[/dim]")
    by_sig: dict[str, list] = {}
    for t in trades:
        by_sig.setdefault(t.signal_type, []).append(t)

    st = Table(box=None, padding=(0, 2))
    st.add_column("Signal",   style="bold")
    st.add_column("Trades",   justify="right")
    st.add_column("Wins",     justify="right")
    st.add_column("Win Rate", justify="right")
    st.add_column("Net P&L",  justify="right")
    st.add_column("Avg P&L",  justify="right")

    for sig, grp in sorted(by_sig.items(), key=lambda x: -sum(t.pnl for t in x[1])):
        g_pnls = [t.pnl for t in grp]
        g_wins = [t for t in grp if t.pnl > 0]
        g_wr   = len(g_wins) / len(grp)
        g_net  = sum(g_pnls)
        wr_col = "green" if g_wr >= 0.5 else "red"
        pn_col = "green" if g_net >= 0 else "red"
        st.add_row(
            sig,
            str(len(grp)),
            str(len(g_wins)),
            f"[{wr_col}]{g_wr:.1%}[/{wr_col}]",
            f"[{pn_col}]₹{g_net:,.0f}[/{pn_col}]",
            f"₹{np.mean(g_pnls):,.0f}",
        )
    console.print(st)

    # By confluence score
    console.print()
    console.rule("[dim]By Confluence Score[/dim]")
    by_score: dict[int, list] = {}
    for t in trades:
        by_score.setdefault(t.confluence_score, []).append(t)

    ct = Table(box=None, padding=(0, 2))
    ct.add_column("Score",    style="bold")
    ct.add_column("Trades",   justify="right")
    ct.add_column("Win Rate", justify="right")
    ct.add_column("Net P&L",  justify="right")

    for sc, grp in sorted(by_score.items()):
        g_pnls = [t.pnl for t in grp]
        g_wr   = len([t for t in grp if t.pnl > 0]) / len(grp)
        g_net  = sum(g_pnls)
        wr_col = "green" if g_wr >= 0.5 else "red"
        pn_col = "green" if g_net >= 0 else "red"
        ct.add_row(
            str(sc),
            str(len(grp)),
            f"[{wr_col}]{g_wr:.1%}[/{wr_col}]",
            f"[{pn_col}]₹{g_net:,.0f}[/{pn_col}]",
        )
    console.print(ct)

    # Exit reasons
    console.print()
    console.rule("[dim]Exit Reasons[/dim]")
    by_exit: dict[str, int] = {}
    for t in trades:
        by_exit[t.exit_reason] = by_exit.get(t.exit_reason, 0) + 1

    et = Table(box=None, padding=(0, 2))
    et.add_column("Reason", style="bold")
    et.add_column("Count",  justify="right")
    et.add_column("Share",  justify="right")
    total_exits = sum(by_exit.values())
    for reason, count in sorted(by_exit.items(), key=lambda x: -x[1]):
        et.add_row(reason, str(count), f"{count/total_exits:.1%}")
    console.print(et)

    console.print()


def _save_json(
    trades: list[MomentumTrade], start: date, end: date, path: str
) -> None:
    pnls = [t.pnl for t in trades]
    wr   = len([t for t in trades if t.pnl > 0]) / len(trades) if trades else 0
    data = {
        "start_date":   str(start),
        "end_date":     str(end),
        "total_trades": len(trades),
        "win_rate":     round(wr, 4),
        "net_pnl":      round(sum(pnls), 2),
        "trades":       [
            {
                "symbol":           t.symbol,
                "signal_type":      t.signal_type,
                "entry_date":       str(t.entry_date),
                "entry_price":      t.entry_price,
                "exit_price":       t.exit_price,
                "exit_reason":      t.exit_reason,
                "pnl":              t.pnl,
                "pnl_pct":          t.pnl_pct,
                "holding_days":     t.holding_days,
                "confluence_score": t.confluence_score,
                "rvol":             t.rvol,
                "rsi":              t.rsi,
                "adx":              t.adx,
            }
            for t in trades
        ],
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    log.info("momentum_bt.saved", path=path)
    console.print(f"[dim]Results saved to {path}[/dim]\n")


# ── Args ──────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog        = "python -m services.momentum_engine.run",
        description = "Momentum engine backtest — long-only, TRENDING_UP markets.",
    )
    p.add_argument("--symbols",   nargs="+", metavar="SYM")
    p.add_argument("--universe",  default="nifty50", choices=["nifty50", "nifty500"])
    p.add_argument("--days",      type=int, default=90)
    p.add_argument("--start",     type=str)
    p.add_argument("--end",       type=str, default=date.today().isoformat())
    p.add_argument("--output",    type=str, default=None)
    p.add_argument("--min-score", type=int, default=8,
                   help="Min confluence score (default: 8)")
    p.add_argument("--min-conf",  type=int, default=65,
                   help="Min signal confidence (default: 65)")
    return p.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    args = _parse_args()

    symbols = args.symbols or _get_universe(args.universe)
    if not symbols:
        console.print("[red]No symbols. Use --symbols or --universe.[/red]")
        sys.exit(1)

    seg_map = _get_segments() if not args.symbols else None

    end_date   = date.fromisoformat(args.end)
    start_date = (
        date.fromisoformat(args.start)
        if args.start
        else end_date - timedelta(days=args.days)
    )

    console.print(
        f"\n[bold]Momentum Backtest[/bold]  "
        f"{len(symbols)} symbols  |  "
        f"{start_date} → {end_date}  |  "
        f"Min score: {args.min_score}  |  "
        f"Min confidence: {args.min_conf}\n"
    )

    engine = MomentumBacktestEngine(
        symbols         = symbols,
        start_date      = start_date,
        end_date        = end_date,
        symbol_segments = seg_map,
        min_score       = args.min_score,
        min_confidence  = args.min_conf,
    )

    result = await engine.run()
    _print_report(result.trades, start_date, end_date, len(symbols))

    if args.output:
        _save_json(result.trades, start_date, end_date, args.output)


if __name__ == "__main__":
    asyncio.run(main())
