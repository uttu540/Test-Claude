"""
scripts/correlation_discovery.py
──────────────────────────────────
Correlation-discovery research for the profit-max loop. Finds which global
markets / cross-asset series correlate with — and ideally LEAD — Nifty daily
returns, then asks gpt-oss to turn the lead-lag structure into testable NSE
entry-signal hypotheses.

The lead-lag view is the actionable one: US and European markets close before
NSE opens, so "yesterday's close → today's Nifty" is a genuine predictor with
no look-ahead. Contemporaneous correlations are shown too (for risk/clustering).

    python -m scripts.correlation_discovery --start 2020-01-01 --end 2025-12-31

Requires NVIDIA_API_KEY in .env for the hypothesis step (correlations still
print without it).
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

from services.ai_strategy.nvidia_client import get_nvidia_client

# Series to test against Nifty. Yahoo tickers.
DRIVERS = {
    "NIFTY":   "^NSEI",     # target (India)
    "SP500":   "^GSPC",     # US — closes ~02:00 IST, before NSE open
    "NASDAQ":  "^IXIC",     # US
    "DOW":     "^DJI",      # US
    "VIX_US":  "^VIX",      # US fear gauge
    "NIKKEI":  "^N225",     # Japan — opens before NSE
    "HANGSENG":"^HSI",      # HK — opens before NSE
    "FTSE":    "^FTSE",     # UK
    "USDINR":  "USDINR=X",  # rupee
    "BRENT":   "BZ=F",      # crude (India imports oil)
    "GOLD":    "GC=F",
}


def _returns(start: str, end: str) -> pd.DataFrame:
    cols = {}
    for name, ticker in DRIVERS.items():
        try:
            h = yf.Ticker(ticker).history(start=start, end=end)
            if not h.empty:
                s = h["Close"].copy()
                s.index = s.index.tz_localize(None)
                cols[name] = s
        except Exception as e:  # noqa
            print(f"  fetch failed {name} ({ticker}): {e}")
    px = pd.DataFrame(cols).sort_index().ffill()
    return px.pct_change().dropna(how="all")


def _analyse(rets: pd.DataFrame) -> dict:
    target = "NIFTY"
    contemp, lead = {}, {}
    for col in rets.columns:
        if col == target:
            continue
        pair = rets[[target, col]].dropna()
        if len(pair) < 60:
            continue
        # Contemporaneous (same day)
        contemp[col] = round(pair[target].corr(pair[col]), 3)
        # Lead: yesterday's driver return vs today's Nifty return (no look-ahead)
        lagged = pd.DataFrame({
            "nifty_today": pair[target],
            "driver_prev": pair[col].shift(1),
        }).dropna()
        if len(lagged) >= 60:
            lead[col] = round(lagged["nifty_today"].corr(lagged["driver_prev"]), 3)
    return {
        "contemporaneous_vs_nifty": dict(sorted(contemp.items(), key=lambda x: -abs(x[1]))),
        "lead_prevday_vs_nifty_today": dict(sorted(lead.items(), key=lambda x: -abs(x[1]))),
        "n_days": len(rets),
    }


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2020-01-01")
    p.add_argument("--end", default="2025-12-31")
    p.add_argument("--out-dir", default="results")
    a = p.parse_args()

    print("Fetching global series ...")
    rets = _returns(a.start, a.end)
    result = _analyse(rets)
    print(json.dumps(result, indent=2))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = Path(a.out_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"correlations_{ts}.json").write_text(json.dumps(result, indent=2))

    client = get_nvidia_client()
    if not client.enabled:
        print("\n[NVIDIA disabled — correlations saved; skipping hypothesis step]")
        return

    system = (
        "You are a quantitative researcher. Turn correlation structure into "
        "concrete, backtestable NSE intraday/swing entry hypotheses. Only use "
        "lead (prev-day) relationships for predictive signals — same-day "
        "correlations cannot be traded without look-ahead. Be explicit about "
        "effect size; a |corr| below ~0.15 is likely too weak to trade."
    )
    prompt = f"""Nifty daily-return correlations with global drivers ({result['n_days']} days, {a.start}..{a.end}):

LEAD (yesterday's driver → today's Nifty — tradable, US/EU close before NSE opens):
{json.dumps(result['lead_prevday_vs_nifty_today'], indent=2)}

CONTEMPORANEOUS (same day — risk/clustering only, NOT tradable):
{json.dumps(result['contemporaneous_vs_nifty'], indent=2)}

Produce:
1. Which LEAD relationships are strong enough to build a signal from, and which are noise?
2. 3-4 concrete testable rules combining a lead signal with the existing swing engine
   (long-only, ADX≥30 filter, TRENDING_UP/RANGING only). e.g. gating entries on overnight
   US direction. State the exact rule and the metric it should move.
3. Any regime/caveat where these correlations likely break down (e.g. crisis clustering)."""

    print("\nAsking gpt-oss for correlation-based signal hypotheses ...\n")
    content, _ = await client.complete(prompt, system=system, max_tokens=3072, temperature=0.6)
    (outdir / f"correlation_hypotheses_{ts}.md").write_text(
        f"# Correlation signal hypotheses — {ts}\n\n"
        f"```json\n{json.dumps(result, indent=2)}\n```\n\n## Model output\n\n{content}\n"
    )
    print(content)
    print(f"\n[saved → {outdir}/correlation_hypotheses_{ts}.md]")


if __name__ == "__main__":
    asyncio.run(main())
