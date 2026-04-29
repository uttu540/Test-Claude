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
    """
    nifty50   — Nifty 50 from nifty500_instruments.py
    nifty500  — Full 493-stock real Nifty 500 from nifty500_instruments.py
    all_nse   — Every equity listed on NSE, fetched live from NSE's public CSV
                (~1,800–2,000 stocks). Use for backtests only.
    """
    from services.data_ingestion.nifty500_instruments import NIFTY500

    # Nifty 50: first 50 entries in NIFTY500 (sorted by index membership)
    nifty50_syms = [sym for sym, _, _ in NIFTY500 if
                    any(sym == s for s in [
                        "ADANIENT","ADANIPORTS","APOLLOHOSP","ASIANPAINT","AXISBANK",
                        "BAJAJ-AUTO","BAJAJFINSV","BAJFINANCE","BHARTIARTL","BPCL",
                        "BRITANNIA","CIPLA","COALINDIA","DIVISLAB","DRREDDY",
                        "EICHERMOT","GRASIM","HCLTECH","HDFCBANK","HDFCLIFE",
                        "HEROMOTOCO","HINDALCO","HINDUNILVR","ICICIBANK","INDUSINDBK",
                        "INFY","IOC","ITC","JSWSTEEL","KOTAKBANK",
                        "LT","M&M","MARUTI","NESTLEIND","NTPC",
                        "ONGC","POWERGRID","RELIANCE","SBILIFE","SBIN",
                        "SUNPHARMA","TATAMOTORS","TATASTEEL","TCS","TECHM",
                        "TITAN","ULTRACEMCO","UPL","VEDL","WIPRO",
                    ])]

    if name == "nifty50":
        return nifty50_syms

    if name == "nifty500":
        return [sym for sym, _, _ in NIFTY500]

    if name == "all_nse":
        return _fetch_all_nse_symbols()

    return []


def _fetch_all_nse_symbols() -> list[str]:
    """
    Fetches the complete list of NSE-listed equities from NSE's public CSV.
    Falls back to the full NIFTY500 list if the download fails.

    Filters applied here (universe-level, before any price data is loaded):
      - EQ series ONLY: BE = T2T (no intraday), BZ = suspended/illiquid
      - No special characters beyond hyphen (warrants, DVRs, rights shares)

    Additional filters applied in MomentumBacktestEngine._backtest_symbol():
      - Avg daily volume ≥ 1 lakh shares (liquidity gate)
      - Avg closing price ≥ ₹20 (micro-cap penny stock filter)
      - These are runtime filters because they need actual price data.
    """
    import re
    import io
    try:
        import requests
        console.print("[dim]Fetching full NSE equity list...[/dim]")
        resp = requests.get(
            "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        resp.raise_for_status()
        import csv
        reader = csv.DictReader(io.StringIO(resp.text))
        symbols = []
        for row in reader:
            sym    = (row.get("SYMBOL") or row.get("Symbol") or "").strip()
            # NSE CSV has a leading space in the SERIES column header
            series = (row.get(" SERIES") or row.get("SERIES") or row.get("Series") or "").strip()
            # EQ only: BE = Trade-to-Trade (no intraday), BZ = illiquid/suspended
            if not sym or series != "EQ":
                continue
            # Skip symbols with spaces or weird chars; allow letters, digits, hyphen, &
            if not re.match(r'^[A-Z0-9&\-]+$', sym):
                continue
            symbols.append(sym)
        console.print(f"[dim]NSE EQ-series universe: {len(symbols)} stocks[/dim]")
        return symbols
    except Exception as e:
        console.print(f"[yellow]NSE fetch failed ({e}), falling back to Nifty 500[/yellow]")
        from services.data_ingestion.nifty500_instruments import NIFTY500
        return [sym for sym, _, _ in NIFTY500]


def _get_segments(symbols: list[str]) -> dict[str, str]:
    """
    Derive segment from NIFTY500 metadata where available.
    Anything not in NIFTY500 gets SMALL_CAP (conservative default).
    """
    from services.data_ingestion.nifty500_instruments import NIFTY500

    nifty50_set = set(_get_universe("nifty50"))
    # Nifty Next 50 = positions 51-100 in NIFTY500 (approx large cap extension)
    nifty500_syms = [sym for sym, _, _ in NIFTY500]
    nifty_next50  = set(nifty500_syms[50:100])
    midcap_set    = set(nifty500_syms[100:350])

    mapping: dict[str, str] = {}
    for sym in symbols:
        if sym in nifty50_set or sym in nifty_next50:
            mapping[sym] = "LARGE_CAP"
        elif sym in midcap_set:
            mapping[sym] = "MID_CAP"
        else:
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

    # By regime
    console.print()
    console.rule("[dim]By Nifty Regime[/dim]")
    by_regime: dict[str, list] = {}
    for t in trades:
        by_regime.setdefault(t.regime, []).append(t)

    rt = Table(box=None, padding=(0, 2))
    rt.add_column("Regime",   style="bold")
    rt.add_column("Trades",   justify="right")
    rt.add_column("Win Rate", justify="right")
    rt.add_column("Net P&L",  justify="right")
    rt.add_column("Avg P&L",  justify="right")

    for reg, grp in sorted(by_regime.items(), key=lambda x: -sum(t.pnl for t in x[1])):
        g_pnls = [t.pnl for t in grp]
        g_wr   = len([t for t in grp if t.pnl > 0]) / len(grp)
        g_net  = sum(g_pnls)
        wr_col = "green" if g_wr >= 0.5 else "red"
        pn_col = "green" if g_net >= 0 else "red"
        rt.add_row(
            reg,
            str(len(grp)),
            f"[{wr_col}]{g_wr:.1%}[/{wr_col}]",
            f"[{pn_col}]₹{g_net:,.0f}[/{pn_col}]",
            f"₹{np.mean(g_pnls):,.0f}",
        )
    console.print(rt)

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
                "regime":           t.regime,
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
    p.add_argument("--universe",  default="nifty50", choices=["nifty50", "nifty500", "all_nse"])
    p.add_argument("--days",      type=int, default=90)
    p.add_argument("--start",     type=str)
    p.add_argument("--end",       type=str, default=date.today().isoformat())
    p.add_argument("--output",    type=str, default=None)
    p.add_argument("--min-score",     type=int, default=8,
                   help="Min confluence score (default: 8)")
    p.add_argument("--min-conf",      type=int, default=65,
                   help="Min signal confidence (default: 65)")
    p.add_argument("--sector-filter", action="store_true", default=False,
                   help="Enable sector ROC-20 headwind filter (default: OFF)")
    return p.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    args = _parse_args()

    symbols = args.symbols or _get_universe(args.universe)
    if not symbols:
        console.print("[red]No symbols. Use --symbols or --universe.[/red]")
        sys.exit(1)

    seg_map = _get_segments(symbols) if not args.symbols else None

    end_date   = date.fromisoformat(args.end)
    start_date = (
        date.fromisoformat(args.start)
        if args.start
        else end_date - timedelta(days=args.days)
    )

    sector_label = "[green]ON[/green]" if args.sector_filter else "[dim]OFF[/dim]"
    console.print(
        f"\n[bold]Momentum Backtest[/bold]  "
        f"{len(symbols)} symbols  |  "
        f"{start_date} → {end_date}  |  "
        f"Min score: {args.min_score}  |  "
        f"Min confidence: {args.min_conf}  |  "
        f"Sector filter: {sector_label}\n"
    )

    engine = MomentumBacktestEngine(
        symbols               = symbols,
        start_date            = start_date,
        end_date              = end_date,
        symbol_segments       = seg_map,
        min_score             = args.min_score,
        min_confidence        = args.min_conf,
        enable_sector_filter  = args.sector_filter,
    )

    result = await engine.run()
    _print_report(result.trades, start_date, end_date, len(symbols))

    if args.output:
        _save_json(result.trades, start_date, end_date, args.output)


if __name__ == "__main__":
    asyncio.run(main())
