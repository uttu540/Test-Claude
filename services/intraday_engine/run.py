"""
services/intraday_engine/run.py
────────────────────────────────
CLI runner for the gap-trading intraday backtest.

Usage:
    # Both strategies, all NSE, full 2024
    python -m services.intraday_engine.run --universe all_nse --start 2024-01-01 --end 2024-12-31

    # Single strategy
    python -m services.intraday_engine.run --strategies GAP_HOLD --universe nifty500

    # Custom symbols
    python -m services.intraday_engine.run --symbols RELIANCE TCS INFY --start 2024-01-01 --end 2024-12-31

Strategies:
    GAP_HOLD     — Strong first candle (top-50% close, holds 70%+ gap) → enter 9:30 open
    GAP_PULLBACK — Gap opens, dips below 9:15 high, reclaims with volume surge
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, timedelta

import numpy as np
from rich import box
from rich.console import Console
from rich.table import Table

from services.intraday_engine.backtest import IntradayBacktestEngine, IntradayTrade

console = Console()

ALL_STRATEGIES = ["GAP_HOLD", "GAP_PULLBACK", "FVG_ORB", "IDARVAS", "VWAP_RCL"]


def _get_universe(name: str) -> list[str]:
    if name == "nifty500":
        from services.data_ingestion.nifty500_instruments import NIFTY500
        return [s for s, _, _ in NIFTY500]
    if name == "all_nse":
        from services.data_ingestion.nifty500_instruments import get_live_universe
        return get_live_universe()
    # nifty50 fallback
    return [
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


# ── Report helpers ────────────────────────────────────────────────────────────

def _stats(trades: list[IntradayTrade]) -> dict:
    if not trades:
        return {}
    winners = [t for t in trades if t.winner]
    losers  = [t for t in trades if not t.winner]
    pnls_inr = [t.pnl_inr for t in trades]
    pnls_pct = [t.pnl_pct for t in trades]
    avg_win  = float(np.mean([t.pnl_inr for t in winners])) if winners else 0.0
    avg_loss = float(np.mean([t.pnl_inr for t in losers]))  if losers  else 0.0
    rr       = abs(avg_win / avg_loss) if avg_loss else float("inf")
    sharpe   = float(np.mean(pnls_pct) / np.std(pnls_pct) * np.sqrt(252)) \
               if len(pnls_pct) > 1 and np.std(pnls_pct) > 0 else 0.0

    # Max drawdown (cumulative)
    cum = np.cumsum(pnls_inr)
    peak = np.maximum.accumulate(cum)
    dd   = cum - peak
    max_dd = float(np.min(dd)) if len(dd) else 0.0

    return {
        "trades":   len(trades),
        "winners":  len(winners),
        "wr":       len(winners) / len(trades) * 100,
        "total_inr": sum(pnls_inr),
        "avg_win":   avg_win,
        "avg_loss":  avg_loss,
        "rr":        rr,
        "sharpe":    sharpe,
        "max_dd":    max_dd,
        "symbols":   len({t.symbol for t in trades}),
        "days":      len({t.trade_date for t in trades}),
    }


def _exit_breakdown(trades: list[IntradayTrade]) -> dict[str, dict]:
    by_reason: dict[str, list] = defaultdict(list)
    for t in trades:
        by_reason[t.exit_reason].append(t)
    out = {}
    for reason, ts in by_reason.items():
        w = [x for x in ts if x.winner]
        out[reason] = {
            "count": len(ts),
            "wr":    len(w) / len(ts) * 100,
            "avg_inr": float(np.mean([x.pnl_inr for x in ts])),
        }
    return out


# ── Print report ──────────────────────────────────────────────────────────────

def _print_report(trades: list[IntradayTrade], start: date, end: date) -> None:
    if not trades:
        console.print("[yellow]No trades generated.[/yellow]")
        return

    console.rule(f"[bold cyan]Intraday Gap Strategies — {start} → {end}[/bold cyan]")

    # Discover strategies present in results (dynamic — not hardcoded)
    active_strategies = sorted({t.strategy for t in trades})

    # ── Per-strategy summary table ────────────────────────────────────────────
    strat_table = Table(title="Strategy Breakdown", box=box.SIMPLE_HEAD)
    strat_table.add_column("Strategy")
    strat_table.add_column("Trades",   justify="right")
    strat_table.add_column("WR %",     justify="right")
    strat_table.add_column("Total ₹",  justify="right")
    strat_table.add_column("Avg Win ₹", justify="right")
    strat_table.add_column("Avg Loss ₹", justify="right")
    strat_table.add_column("R:R",      justify="right")
    strat_table.add_column("Sharpe",   justify="right")
    strat_table.add_column("Max DD ₹", justify="right")

    strategy_stats = {}
    for strat in active_strategies:
        st = [t for t in trades if t.strategy == strat]
        if not st:
            strat_table.add_row(strat, "0", "—", "—", "—", "—", "—", "—", "—")
            continue
        s = _stats(st)
        strategy_stats[strat] = s
        total_color = "green" if s["total_inr"] >= 0 else "red"
        dd_str      = f"[red]{s['max_dd']:,.0f}[/red]"
        strat_table.add_row(
            strat,
            str(s["trades"]),
            f"{s['wr']:.1f}%",
            f"[{total_color}]₹{s['total_inr']:+,.0f}[/{total_color}]",
            f"₹{s['avg_win']:,.0f}",
            f"₹{s['avg_loss']:,.0f}",
            f"{s['rr']:.2f}x",
            f"{s['sharpe']:.2f}",
            dd_str,
        )

    console.print(strat_table)

    # ── Combined overall ──────────────────────────────────────────────────────
    overall = _stats(trades)
    ov = Table(title="Overall (all strategies combined)", box=box.SIMPLE_HEAD, show_header=False)
    ov.add_column("Metric", style="dim")
    ov.add_column("Value",  style="bold")
    total_color = "green" if overall["total_inr"] >= 0 else "red"
    ov.add_row("Total trades",  str(overall["trades"]))
    ov.add_row("Win rate",      f"{overall['wr']:.1f}%")
    ov.add_row("Total PnL",     f"[{total_color}]₹{overall['total_inr']:+,.0f}[/{total_color}]")
    ov.add_row("Avg win",       f"₹{overall['avg_win']:+,.0f}")
    ov.add_row("Avg loss",      f"₹{overall['avg_loss']:+,.0f}")
    ov.add_row("R:R",           f"{overall['rr']:.2f}x")
    ov.add_row("Sharpe (ann.)", f"{overall['sharpe']:.2f}")
    ov.add_row("Max drawdown",  f"₹{overall['max_dd']:,.0f}")
    ov.add_row("Symbols hit",   str(overall["symbols"]))
    ov.add_row("Trading days",  str(overall["days"]))
    console.print(ov)

    # ── Exit reason per strategy ──────────────────────────────────────────────
    for strat in active_strategies:
        st = [t for t in trades if t.strategy == strat]
        if not st:
            continue
        br = _exit_breakdown(st)
        er = Table(title=f"{strat} — Exit breakdown", box=box.SIMPLE_HEAD)
        er.add_column("Reason")
        er.add_column("Count", justify="right")
        er.add_column("WR %",  justify="right")
        er.add_column("Avg ₹", justify="right")
        for reason, v in sorted(br.items(), key=lambda x: -x[1]["count"]):
            er.add_row(reason, str(v["count"]), f"{v['wr']:.0f}%", f"₹{v['avg_inr']:+,.0f}")
        console.print(er)

    # ── Top 10 trades overall ─────────────────────────────────────────────────
    top10 = sorted(trades, key=lambda t: -t.pnl_inr)[:10]
    t10 = Table(title="Top 10 trades", box=box.SIMPLE_HEAD)
    t10.add_column("Symbol")
    t10.add_column("Strategy")
    t10.add_column("Date")
    t10.add_column("Entry ₹")
    t10.add_column("Exit ₹")
    t10.add_column("PnL ₹",  justify="right")
    t10.add_column("R",      justify="right")
    t10.add_column("Reason")
    for t in top10:
        t10.add_row(
            t.symbol, t.strategy, str(t.trade_date),
            f"₹{t.entry_price:.1f}", f"₹{t.exit_price:.1f}",
            f"[green]₹{t.pnl_inr:+,.0f}[/green]",
            f"{t.r_multiple:.1f}R",
            t.exit_reason,
        )
    console.print(t10)

    # ── Worst 5 trades ────────────────────────────────────────────────────────
    bot5 = sorted(trades, key=lambda t: t.pnl_inr)[:5]
    b5 = Table(title="Worst 5 trades", box=box.SIMPLE_HEAD)
    b5.add_column("Symbol")
    b5.add_column("Strategy")
    b5.add_column("Date")
    b5.add_column("Entry ₹")
    b5.add_column("Stop ₹")
    b5.add_column("PnL ₹",  justify="right")
    for t in bot5:
        b5.add_row(
            t.symbol, t.strategy, str(t.trade_date),
            f"₹{t.entry_price:.1f}", f"₹{t.stop_price:.1f}",
            f"[red]₹{t.pnl_inr:+,.0f}[/red]",
        )
    console.print(b5)


# ── JSON save ────────────────────────────────────────────────────────────────

def _save_json(trades: list[IntradayTrade], path: str, start: date, end: date) -> None:
    by_strat = defaultdict(list)
    for t in trades:
        by_strat[t.strategy].append(t)

    summary_by_strat = {}
    for strat, st in by_strat.items():
        s = _stats(st)
        summary_by_strat[strat] = {
            "trades":     s["trades"],
            "win_rate":   round(s["wr"], 2),
            "total_inr":  round(s["total_inr"], 2),
            "avg_win":    round(s["avg_win"], 2),
            "avg_loss":   round(s["avg_loss"], 2),
            "rr":         round(s["rr"], 3),
            "sharpe":     round(s["sharpe"], 3),
            "max_dd_inr": round(s["max_dd"], 2),
        }

    all_s = _stats(trades)
    payload = {
        "strategy":  "intraday_multi_strategy",
        "start":     str(start),
        "end":       str(end),
        "summary": {
            "total_trades":  all_s.get("trades", 0),
            "win_rate_pct":  round(all_s.get("wr", 0), 2),
            "total_inr":     round(all_s.get("total_inr", 0), 2),
            "sharpe":        round(all_s.get("sharpe", 0), 3),
            "max_dd_inr":    round(all_s.get("max_dd", 0), 2),
        },
        "by_strategy": summary_by_strat,
        "trades": [
            {
                "symbol":       t.symbol,
                "strategy":     t.strategy,
                "date":         str(t.trade_date),
                "entry_time":   t.entry_time.isoformat(),
                "entry_price":  t.entry_price,
                "stop_price":   t.stop_price,
                "exit_price":   t.exit_price,
                "exit_time":    t.exit_time.isoformat(),
                "exit_reason":  t.exit_reason,
                "pnl_inr":      t.pnl_inr,
                "pnl_pct":      t.pnl_pct,
                "r_multiple":   t.r_multiple,
                "winner":       bool(t.winner),
            }
            for t in trades
        ],
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    console.print(f"\n[dim]Saved → {path}[/dim]")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Intraday multi-strategy backtest")
    parser.add_argument("--symbols",    nargs="+",  help="Override universe with specific symbols")
    parser.add_argument("--universe",   default="nifty500",
                        choices=["nifty50", "nifty500", "all_nse"])
    parser.add_argument("--strategies", nargs="+",  default=ALL_STRATEGIES,
                        choices=ALL_STRATEGIES,
                        help="Strategies to test (default: all)")
    parser.add_argument("--start",      help="YYYY-MM-DD (default: 365 days ago)")
    parser.add_argument("--end",        help="YYYY-MM-DD (default: today)")
    parser.add_argument("--days",       type=int, default=365)
    parser.add_argument("--workers",      type=int, default=8,
                        help="Parallel symbol workers (default 8)")
    parser.add_argument("--top-n",        type=int, default=15,
                        help="Gap scanner: top N stocks per day (default 15)")
    parser.add_argument("--wide-top-n",   type=int, default=50,
                        help="Wide scanner (IDARVAS/VWAP_RCL): top N per day (default 50)")
    parser.add_argument("--min-rvol",     type=float, default=5.0,
                        help="Scanner: min RVOL vs 20d avg (default 5.0)")
    parser.add_argument("--min-gap",      type=float, default=1.5,
                        help="Scanner: min gap %% (default 1.5)")
    parser.add_argument("--max-stop-pct", type=float, default=2.5,
                        help="Max stop distance from entry %% (default 2.5)")
    parser.add_argument("--no-nifty-gate", action="store_true",
                        help="Disable Nifty trend gate")
    parser.add_argument("--use-5min",  action="store_true",
                        help="Use 5-min cache (~/.cache/fvg_5min/) for FVG_ORB (run seed_5min_cache.py first)")
    parser.add_argument("--trail-start", type=float, default=2.0,
                        help="R-multiple at which trail begins (default 2.0 = 2R→BE)")
    parser.add_argument("--output",       help="JSON output path")
    args = parser.parse_args()

    end_date   = date.fromisoformat(args.end)   if args.end   else date.today()
    start_date = date.fromisoformat(args.start) if args.start else end_date - timedelta(days=args.days)

    symbols = args.symbols or _get_universe(args.universe)

    nifty_str = "[green]Nifty gate ON[/green]" if not args.no_nifty_gate else "[yellow]Nifty gate OFF[/yellow]"
    console.print(
        f"\n[bold]Intraday Gap Strategies[/bold]  |  "
        f"{len(symbols)} symbols  |  {start_date} → {end_date}  |  "
        f"Strategies: {', '.join(args.strategies)}  |  "
        f"Scanner top-{args.top_n}  |  Gap≥{args.min_gap}%  RVOL≥{args.min_rvol}×  StopMax≤{args.max_stop_pct}%  |  "
        f"{nifty_str}  |  {args.workers} workers"
    )

    # Apply CLI overrides to scanner constants
    import services.intraday_engine.backtest as _bt
    _bt.SCANNER_MIN_RVOL    = args.min_rvol
    _bt.SCANNER_MIN_GAP_PCT = args.min_gap
    _bt.MAX_STOP_PCT        = args.max_stop_pct / 100

    engine = IntradayBacktestEngine(
        symbols    = symbols,
        start_date = start_date,
        end_date   = end_date,
        strategies = args.strategies,
        workers    = args.workers,
        top_n      = args.top_n,
        wide_top_n    = args.wide_top_n,
        nifty_gate    = not args.no_nifty_gate,
        use_5min      = args.use_5min,
        trail_start_r = args.trail_start,
    )

    with console.status("[cyan]Running backtest...[/cyan]"):
        trades = engine.run()

    _print_report(trades, start_date, end_date)

    out = args.output or (
        f"results/intraday_{'_'.join(args.strategies)}_{start_date}_{end_date}.json"
    )
    if trades:
        _save_json(trades, out, start_date, end_date)


if __name__ == "__main__":
    main()
