"""
scripts/profit_max_sweep.py
────────────────────────────
Profit-maximization sweep harness for the Momentum V2 swing engine.

Runs the V2 backtest across a date range/universe with a given parameter set,
computes a full metrics block (WR, net P&L, avg R, profit factor, Sharpe, max
drawdown, exit-reason breakdown), and appends the result to a JSONL log so the
/loop can compare iterations over time.

Usage:
    python -m scripts.profit_max_sweep --label baseline \
        --universe nifty50 --start 2024-01-01 --end 2025-12-31 \
        --min-score 8 --max-score 8 --min-conf 65

Tunable knobs exposed for the sweep:
    --min-score / --max-score / --min-conf   (confluence + confidence gates)
    --sector-filter / --watchlist-filter     (opt-in universe gates)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from datetime import date, datetime
from pathlib import Path

from services.momentum_engine_v2.backtest import MomentumBacktestEngine


def _universe(name: str) -> list[str]:
    from services.data_ingestion.nifty500_instruments import NIFTY500
    n50 = {
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
    }
    if name == "nifty50":
        return [s for s, _, _ in NIFTY500 if s in n50]
    if name == "nifty500":
        return [s for s, _, _ in NIFTY500]
    if name == "all_nse":
        from services.momentum_engine.run import _fetch_all_nse_symbols
        return _fetch_all_nse_symbols()
    return [s for s, _, _ in NIFTY500 if s in n50]


def _metrics(trades: list) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0}
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    net = sum(pnls)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    # per-trade R multiple: pnl / initial risk (entry - stop) * size
    r_mults = []
    for t in trades:
        risk = (t.entry_price - t.stop_loss) * t.position_size
        if risk > 0:
            r_mults.append(t.pnl / risk)
    avg_r = sum(r_mults) / len(r_mults) if r_mults else 0.0
    # Sharpe on per-trade pnl (not annualized — comparative only)
    mean = net / n
    var = sum((p - mean) ** 2 for p in pnls) / n
    std = math.sqrt(var)
    sharpe = (mean / std) if std > 0 else 0.0
    # Max drawdown on cumulative equity curve (trades ordered by entry_date)
    ordered = sorted(trades, key=lambda t: t.entry_date)
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in ordered:
        equity += t.pnl
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    exit_breakdown: dict[str, int] = {}
    for t in trades:
        exit_breakdown[t.exit_reason] = exit_breakdown.get(t.exit_reason, 0) + 1
    return {
        "trades": n,
        "win_rate": round(len(wins) / n * 100, 1),
        "net_pnl": round(net, 0),
        "avg_pnl": round(mean, 0),
        "avg_r": round(avg_r, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "sharpe_per_trade": round(sharpe, 2),
        "max_drawdown": round(max_dd, 0),
        "avg_hold_days": round(sum(t.holding_days for t in trades) / n, 1),
        "exit_breakdown": exit_breakdown,
    }


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--label", required=True, help="Name for this parameter set")
    p.add_argument("--universe", default="nifty50")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--min-score", type=int, default=8)
    p.add_argument("--max-score", type=int, default=8)
    p.add_argument("--min-conf", type=int, default=65)
    p.add_argument("--sector-filter", action="store_true", default=False)
    p.add_argument("--watchlist-filter", action="store_true", default=False)
    p.add_argument("--watchlist-size", type=int, default=50)
    p.add_argument("--target-mult", type=float, default=None,
                   help="Override TARGET_ATR_MULT (default 7.0)")
    p.add_argument("--max-hold", type=int, default=None,
                   help="Override MAX_HOLD_DAYS TRENDING_UP hold (default 35)")
    p.add_argument("--enable-target-exit", action="store_true", default=False,
                   help="Turn on the fixed-target exit (default off)")
    p.add_argument("--min-adx", type=float, default=None,
                   help="ADX entry floor (default off)")
    p.add_argument("--min-rsi", type=float, default=None,
                   help="RSI entry floor (default off)")
    p.add_argument("--min-us-ret", type=float, default=None,
                   help="Overnight-US (S&P prev close) return floor, e.g. 0.0 (default off)")
    p.add_argument("--min-rvol", type=float, default=None,
                   help="RVOL entry floor (volume-thrust hypothesis, default off)")
    p.add_argument("--regime-stability", type=int, default=None,
                   help="Require N consecutive TRENDING_UP days before entry (default off)")
    p.add_argument("--risk-pct", type=float, default=None,
                   help="Override risk-per-trade fraction, e.g. 0.03 for 3%% (default 2%%)")
    p.add_argument("--out", default="results/profit_max_sweep.jsonl")
    a = p.parse_args()

    # Exit-logic knobs: patch module constants before the engine reads them.
    import services.momentum_engine_v2.backtest as bt
    if a.target_mult is not None:
        bt.TARGET_ATR_MULT = a.target_mult
    if a.max_hold is not None:
        bt.MAX_HOLD_DAYS = a.max_hold
    if a.enable_target_exit:
        bt.ENABLE_TARGET_EXIT = True
    if a.min_adx is not None:
        bt.MIN_ADX_FILTER = a.min_adx
    if a.min_rsi is not None:
        bt.MIN_RSI_FILTER = a.min_rsi
    if a.min_us_ret is not None:
        bt.MIN_US_OVERNIGHT_RET = a.min_us_ret
    if a.min_rvol is not None:
        bt.MIN_RVOL_FILTER = a.min_rvol
    if a.regime_stability is not None:
        bt.REGIME_STABILITY_DAYS = a.regime_stability
    if a.risk_pct is not None:
        bt.RISK_PCT = a.risk_pct

    syms = _universe(a.universe)
    engine = MomentumBacktestEngine(
        symbols=syms,
        start_date=date.fromisoformat(a.start),
        end_date=date.fromisoformat(a.end),
        min_score=a.min_score,
        max_score=a.max_score,
        min_confidence=a.min_conf,
        enable_sector_filter=a.sector_filter,
        watchlist_filter=a.watchlist_filter,
        watchlist_size=a.watchlist_size,
    )
    result = await engine.run()
    m = _metrics(result.trades)

    record = {
        "label": a.label,
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "universe": a.universe,
        "n_symbols": len(syms),
        "period": f"{a.start}..{a.end}",
        "params": {
            "min_score": a.min_score, "max_score": a.max_score,
            "min_conf": a.min_conf, "sector_filter": a.sector_filter,
            "watchlist_filter": a.watchlist_filter,
            "target_mult": bt.TARGET_ATR_MULT, "max_hold": bt.MAX_HOLD_DAYS,
            "min_adx": bt.MIN_ADX_FILTER, "min_rsi": bt.MIN_RSI_FILTER,
            "min_us_ret": bt.MIN_US_OVERNIGHT_RET,
            "min_rvol": bt.MIN_RVOL_FILTER, "regime_stability": bt.REGIME_STABILITY_DAYS,
        },
        "metrics": m,
    }
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        f.write(json.dumps(record) + "\n")

    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
