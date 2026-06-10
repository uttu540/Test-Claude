"""Param sweep for intraday V2 — loads 5-min data ONCE, sweeps combos in memory.

Usage:
    PYTHONPATH=. venv/bin/python scripts/tune_intraday_v2.py
"""
from __future__ import annotations

import itertools
import json
from datetime import date
from pathlib import Path

import services.intraday_engine_v2.backtest as bt

START, END = date(2024, 1, 1), date(2024, 6, 30)   # H1 2024 for tuning (H2 = holdout)

symbols = sorted({f.stem.rsplit("_", 2)[0] for f in bt._CACHE_5MIN.glob("*.pkl")})
eng = bt.IntradayV2Engine(symbols, START, END, workers=8)
preloaded = eng.load()

GRID = {
    "MAX_STOP_PCT":    [0.012, 0.020],
    "SCALE_R":         [1.5, 2.0],
    "RS_MIN_ABS":      [0.25, 0.75],
    "MIN_GAP_ALIGN":   [0.0, 0.5, 1.5],
    "MIN_RVOL_FIRST2": [2.0, 3.0],
}

results = []
keys = list(GRID)
for combo in itertools.product(*GRID.values()):
    params = dict(zip(keys, combo))
    for k, v in params.items():
        setattr(bt, k, v)
    trades = eng.run(preloaded=preloaded)
    stats  = bt.summarize(trades)
    row = {**params, **{k: stats.get(k) for k in
           ("trades", "win_rate", "pnl_inr", "profit_factor", "sharpe", "max_dd_inr", "long", "short")}}
    results.append(row)
    print(json.dumps(row, default=str))

results.sort(key=lambda r: (r.get("pnl_inr") or 0), reverse=True)
out = Path("results/intraday_v2_tune_h1_2024.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(results, indent=2, default=str))
print(f"\nTop 5 by PnL:")
for r in results[:5]:
    print(json.dumps(r, default=str))
print(f"Saved → {out}")
