"""
services/intraday_engine_v2/run.py
────────────────────────────────────
CLI for the V2 intraday backtest.

Usage:
    venv/bin/python -m services.intraday_engine_v2.run --start 2024-01-01 --end 2024-12-31
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from services.intraday_engine_v2.backtest import IntradayV2Engine, summarize, _CACHE_5MIN


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 1))
    p.add_argument("--end",   type=date.fromisoformat, default=date(2024, 12, 31))
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--out", type=str, default="results/intraday_v2_backtest.json")
    args = p.parse_args()

    symbols = sorted({f.stem.rsplit("_", 2)[0] for f in _CACHE_5MIN.glob("*.pkl")})
    print(f"Universe: {len(symbols)} symbols | {args.start} → {args.end}")

    engine = IntradayV2Engine(symbols, args.start, args.end, workers=args.workers)
    trades = engine.run()
    stats  = summarize(trades)

    print(json.dumps(stats, indent=2, default=str))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "params": {"start": str(args.start), "end": str(args.end)},
        "stats":  stats,
        "trades": [
            {**t.__dict__, "trade_date": str(t.trade_date),
             "entry_time": str(t.entry_time), "exit_time": str(t.exit_time)}
            for t in trades
        ],
    }, indent=2, default=str))
    print(f"Saved → {out}")


if __name__ == "__main__":
    main()
