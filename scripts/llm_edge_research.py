"""
scripts/llm_edge_research.py
──────────────────────────────
Phase-2 profit-max research: use the NVIDIA gpt-oss model to generate TESTABLE
edge hypotheses from the Momentum V2 backtest, then a human/loop encodes each as
a sweep variant and lets the backtest be the judge.

The LLM never touches live trading. It only reads aggregated backtest stats and
the engine description, and returns hypotheses + a code-logic critique.

    python -m scripts.llm_edge_research --universe nifty50 \
        --start 2024-01-01 --end 2025-12-31

Requires NVIDIA_API_KEY in .env (see services/ai_strategy/nvidia_client.py).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from datetime import date, datetime
from pathlib import Path

from services.ai_strategy.nvidia_client import get_nvidia_client
from services.momentum_engine_v2.backtest import MomentumBacktestEngine
from scripts.profit_max_sweep import _universe, _metrics


def _dist(vals: list[float]) -> dict:
    vals = [v for v in vals if v is not None]
    if not vals:
        return {}
    return {
        "n": len(vals),
        "min": round(min(vals), 2),
        "median": round(statistics.median(vals), 2),
        "mean": round(statistics.mean(vals), 2),
        "max": round(max(vals), 2),
    }


def _feature_summary(trades: list) -> dict:
    """Compare winners vs losers across the features the engine records."""
    winners = [t for t in trades if t.pnl > 0]
    losers = [t for t in trades if t.pnl <= 0]

    def side(ts: list) -> dict:
        return {
            "count": len(ts),
            "rvol": _dist([t.rvol for t in ts]),
            "rsi": _dist([t.rsi for t in ts]),
            "adx": _dist([t.adx for t in ts]),
            "confluence_score": _dist([t.confluence_score for t in ts]),
            "holding_days": _dist([t.holding_days for t in ts]),
            "max_gain_pct": _dist([t.max_gain_pct for t in ts]),
            "regimes": {r: sum(1 for t in ts if t.regime == r)
                        for r in {t.regime for t in ts}},
            "signal_types": {s: sum(1 for t in ts if t.signal_type == s)
                             for s in {t.signal_type for t in ts}},
        }

    return {"winners": side(winners), "losers": side(losers)}


ENGINE_BRIEF = """
Momentum V2 swing engine (NSE Nifty large-caps, long-only, daily timeframe):
- Fires only when Nifty regime is TRENDING_UP or RANGING (blocks TRENDING_DOWN).
- Entry signals: DARVAS_BREAKOUT (primary), plus 52W-high / EMA-ribbon / volume
  thrust as confluence boosters (not standalone entries).
- Confluence score 0-10; only score==8 trades fire (tight band — validated).
- Stop: 1.5x ATR (TRENDING_UP) or 2x ATR (RANGING/DOWN). Target: 7x ATR but a
  fixed-target exit is DISABLED — winners ride via trailing stop + 35-day MAX_HOLD.
- Trailing stop milestones: 4R->+1R, 5R->+2R, 8R->+4R, 12R->+6R.
- Position size: fixed 2% risk (Rs2,000) per trade on Rs1,00,000 capital.

Backtest facts (nifty50, 2024-2025): 10 trades, 60% WR, +Rs26,780, avg R 1.34,
profit factor 4.46, max drawdown only -Rs2,594. LEVERS ALREADY TESTED AND
REJECTED (all lost to baseline): loosening the confluence gate, expanding the
universe to nifty500/all-NSE (edge is large-cap concentrated), adding a fixed
target exit, extending MAX_HOLD. Conclusion so far: it is a low-FREQUENCY,
high-QUALITY edge and is well-tuned on these knobs.
""".strip()


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--universe", default="nifty50")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--out-dir", default="results")
    a = p.parse_args()

    client = get_nvidia_client()
    if not client.enabled:
        print("NVIDIA client disabled — set NVIDIA_API_KEY in .env.")
        return

    syms = _universe(a.universe)
    engine = MomentumBacktestEngine(
        symbols=syms,
        start_date=date.fromisoformat(a.start),
        end_date=date.fromisoformat(a.end),
    )
    result = await engine.run()
    metrics = _metrics(result.trades)
    features = _feature_summary(result.trades)

    system = (
        "You are a quantitative trading researcher. You propose ONLY concrete, "
        "backtestable hypotheses grounded in the data provided. No hand-waving, "
        "no generic advice. Every hypothesis must name the exact rule change and "
        "the metric it should move. Be skeptical of overfitting on small samples."
    )
    prompt = f"""{ENGINE_BRIEF}

Aggregate metrics:
{json.dumps(metrics, indent=2)}

Winner-vs-loser feature distributions:
{json.dumps(features, indent=2)}

Given ALL of the above, produce:
1. WINNER/LOSER DISCRIMINATORS — what in the feature distributions separates
   winning from losing trades? Flag small-sample caveats.
2. TESTABLE HYPOTHESES — 3-5 specific rule changes (entry filter, stop/trail,
   sizing, regime handling). For each: the exact change, the metric it targets,
   and why the data supports it. Rank by expected impact.
3. CODE/LOGIC CRITIQUE — given the engine description, where might edge be
   leaking or risk be mismeasured?
Keep it tight and specific."""

    print("Calling gpt-oss-120b for edge hypotheses ...\n")
    content, reasoning = await client.complete(
        prompt, system=system, max_tokens=4096, temperature=0.7,
    )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = Path(a.out_dir) / f"llm_hypotheses_{ts}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        f"# LLM edge hypotheses — {ts}\n\n"
        f"Universe: {a.universe} ({len(syms)} symbols) | Period: {a.start}..{a.end}\n\n"
        f"## Metrics\n```json\n{json.dumps(metrics, indent=2)}\n```\n\n"
        f"## Model output\n\n{content}\n"
    )
    print(content)
    print(f"\n[saved → {out}]")


if __name__ == "__main__":
    asyncio.run(main())
