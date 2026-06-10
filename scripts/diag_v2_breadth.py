"""Diagnostic: breadth distribution + filter funnel for intraday V2 (Jan 2024)."""
from __future__ import annotations

import pickle
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import numpy as np

from services.intraday_engine_v2.backtest import (
    _CACHE_5MIN, MIN_AVG_DAY_VOL, MIN_PRICE, MIN_RVOL_FIRST2, RS_MIN_ABS,
    IntradayV2Engine,
)

START, END = date(2024, 1, 1), date(2024, 3, 31)

symbols = sorted({f.stem.rsplit("_", 2)[0] for f in _CACHE_5MIN.glob("*.pkl")})
eng = IntradayV2Engine(symbols, START, END, workers=8)

data, metas = {}, {}
with ThreadPoolExecutor(max_workers=8) as pool:
    for res in pool.map(eng._load_symbol, symbols):
        if res:
            sym, day_dfs, meta = res
            data[sym] = day_dfs
            metas[sym] = meta
print(f"loaded {len(data)} symbols")

all_dates = sorted({d for dd in data.values() for d in dd})
grades = Counter()
breadths = []
funnel = Counter()

for tdate in all_dates:
    chg = {}
    n_liq = n_rvol = 0
    for sym, dd in data.items():
        day_df = dd.get(tdate)
        if day_df is None:
            continue
        m = metas[sym].get(tdate)
        if not m or m["avg_day_vol"] < MIN_AVG_DAY_VOL:
            continue
        op = float(day_df["open"].iloc[0])
        if op < MIN_PRICE:
            continue
        n_liq += 1
        c925 = float(day_df["close"].iloc[1])
        chg[sym] = (c925 - op) / op * 100
        first2 = float(day_df["volume"].iloc[:2].sum())
        if m["avg_first2"] > 0 and first2 / m["avg_first2"] >= MIN_RVOL_FIRST2:
            n_rvol += 1
    if len(chg) < 100:
        continue
    arr = np.array(list(chg.values()))
    b = float((arr > 0).mean())
    breadths.append(b)
    funnel["liquid"] += n_liq
    funnel["rvol_pass"] += n_rvol
    if b >= 0.65: grades["A_LONG"] += 1
    elif b >= 0.58: grades["B_LONG"] += 1
    elif b <= 0.35: grades["A_SHORT"] += 1
    elif b <= 0.42: grades["B_SHORT"] += 1
    else: grades["C_SKIP"] += 1

breadths = np.array(breadths)
print(f"\ndays={len(breadths)}")
print(f"breadth: min={breadths.min():.2f} p25={np.percentile(breadths,25):.2f} "
      f"med={np.median(breadths):.2f} p75={np.percentile(breadths,75):.2f} max={breadths.max():.2f}")
print(f"grades: {dict(grades)}")
print(f"avg liquid/day: {funnel['liquid']//max(len(breadths),1)}, "
      f"avg rvol-pass/day: {funnel['rvol_pass']//max(len(breadths),1)}")
