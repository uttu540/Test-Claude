"""
services/earnings_engine/fundamental_gate.py
─────────────────────────────────────────────
Claude-powered fundamental quality gate for earnings gap-and-go signals.

Flow:
  1. Fetch last 5 quarters of Revenue + PAT from screener.in
  2. Compute YoY and QoQ growth
  3. Ask Claude: is this result quality worth trading the gap?
  4. Return FundamentalGateResult(approve, confidence_adj, red_flags, reasoning)

Caching: Redis key earnings:fgate:{symbol}  TTL 8h
  Prevents re-fetching screener.in + re-calling Claude on every 15-min candle.

Fail-open: if screener.in or Claude are unavailable, gate approves so technical
  signal is not blocked by an API failure.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import NamedTuple

import httpx
import structlog

log = structlog.get_logger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────

_SCREENER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.screener.in/",
}

_FUNDAMENTAL_GATE_SYSTEM = """You are a fundamental analyst for an Indian equity trading bot.
Your job: evaluate whether an earnings gap-and-go trade has genuine fundamental backing.

A gap-and-go on earnings works when the market is rewarding REAL operational improvement.
It fails when the gap is driven by one-time gains, tax reversals, low base effects, asset
sales, or accounting adjustments.

## Output MUST be valid JSON only. No markdown, no explanation outside the JSON.

Required schema:
{
  "approve": true | false,
  "confidence_adj": <int -10 to +10>,
  "red_flags": ["<flag1>", "<flag2>"],
  "reasoning": "<2-3 sentences>"
}

## Rules
- approve=true  if revenue AND profit growth are both positive YoY
- approve=true  if data is insufficient (do not penalise missing data)
- approve=false if revenue is flat/declining YoY even if profit looks good
                (cost-cut or one-time driven — gap likely to fade)
- approve=false if PAT growth is 5x+ YoY while revenue growth is <10%
                (exceptional item red flag)
- confidence_adj: +5 to +10 for strong double-digit YoY rev+PAT growth
                  0 for moderate growth or missing data
                  -5 to -10 for declining revenue or suspicious profit spike
- red_flags: brief warnings only — "Revenue declining YoY", "Exceptional item suspected"
- When data unavailable: approve=true, confidence_adj=0"""


# ─── Result type ──────────────────────────────────────────────────────────────

class FundamentalGateResult(NamedTuple):
    approve:        bool
    confidence_adj: int        # -10 to +10 — applied to signal confidence
    red_flags:      list[str]
    reasoning:      str


# ─── Screener.in data fetch ───────────────────────────────────────────────────

def _normalize_symbol(symbol: str) -> str:
    """NSE symbol → screener.in URL slug."""
    return symbol.replace("&", "-").replace(" ", "-").upper()


def _parse_number(s: str) -> float | None:
    try:
        cleaned = re.sub(r"[^\d.\-]", "", s.replace(",", ""))
        return float(cleaned) if cleaned else None
    except (ValueError, TypeError):
        return None


def _parse_quarters(html: str, symbol: str) -> dict | None:
    """Parse the #quarters table from screener.in company page HTML."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")

        section = soup.find("section", {"id": "quarters"})
        if not section:
            log.debug("fundamental_gate.no_quarters_section", symbol=symbol)
            return None

        table = section.find("table")
        if not table:
            return None

        thead = table.find("thead")
        tbody = table.find("tbody")
        if not thead or not tbody:
            return None

        # Quarter labels (columns)
        headers   = [th.get_text(strip=True) for th in thead.find_all("th")]
        quarters  = headers[1:]   # skip empty first cell

        # Row data
        rows: dict[str, list[str]] = {}
        for tr in tbody.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 2:
                continue
            # Strip trailing "+", "?" link icons added by screener
            label  = re.sub(r"\s*[+?]\s*$", "", cells[0].get_text(strip=True)).strip().lower()
            values = [cells[i].get_text(strip=True) for i in range(1, len(cells))]
            rows[label] = values

        # Map to canonical row names (screener varies wording by company type)
        sales_row = (
            rows.get("sales")
            or rows.get("revenue")
            or rows.get("revenue from operations")
            or rows.get("net sales")
            or rows.get("total revenue")
        )
        pat_row = (
            rows.get("net profit")
            or rows.get("pat")
            or rows.get("profit after tax")
            or rows.get("net profit / loss")
            or rows.get("profit / loss after tax")
        )

        if not sales_row and not pat_row:
            log.debug(
                "fundamental_gate.no_key_rows",
                symbol=symbol,
                rows_found=list(rows.keys())[:12],
            )
            return None

        n = min(
            len(quarters),
            len(sales_row) if sales_row else 99,
            len(pat_row)   if pat_row   else 99,
            5,   # cap at 5 quarters
        )

        result = {"symbol": symbol, "quarters": []}
        for i in range(n):
            result["quarters"].append({
                "period":  quarters[i],
                "revenue": _parse_number(sales_row[i]) if sales_row else None,
                "pat":     _parse_number(pat_row[i])   if pat_row   else None,
            })

        return result if result["quarters"] else None

    except Exception as e:
        log.debug("fundamental_gate.parse_error", symbol=symbol, error=str(e))
        return None


def _fetch_screener_sync(symbol: str) -> dict | None:
    """Synchronous screener.in fetch. Run via run_in_executor."""
    slug = _normalize_symbol(symbol)
    urls = [
        f"https://www.screener.in/company/{slug}/consolidated/",
        f"https://www.screener.in/company/{slug}/",
    ]

    for url in urls:
        try:
            with httpx.Client(
                headers=_SCREENER_HEADERS,
                follow_redirects=True,
                timeout=15,
            ) as client:
                resp = client.get(url)

            if resp.status_code != 200:
                log.debug("fundamental_gate.screener_http_error",
                          symbol=symbol, url=url, status=resp.status_code)
                continue

            parsed = _parse_quarters(resp.text, symbol)
            if parsed:
                log.info("fundamental_gate.screener_ok",
                         symbol=symbol, quarters=len(parsed["quarters"]))
                return parsed

        except Exception as e:
            log.debug("fundamental_gate.screener_request_error",
                      symbol=symbol, url=url, error=str(e))

    return None


# ─── Growth calculation ───────────────────────────────────────────────────────

def _compute_growth(quarters: list[dict]) -> dict:
    """
    Compute YoY and QoQ growth.
    Screener returns most-recent first.
    YoY: q[0] vs q[4] (same quarter last year).
    QoQ: q[0] vs q[1].
    """
    g: dict = {
        "rev_yoy": None, "rev_qoq": None,
        "pat_yoy": None, "pat_qoq": None,
    }
    if not quarters:
        return g

    latest = quarters[0]

    if len(quarters) >= 2:
        prev = quarters[1]
        if latest.get("revenue") and prev.get("revenue") and prev["revenue"] != 0:
            g["rev_qoq"] = round((latest["revenue"] - prev["revenue"]) / abs(prev["revenue"]) * 100, 1)
        if latest.get("pat") and prev.get("pat") and prev["pat"] != 0:
            g["pat_qoq"] = round((latest["pat"] - prev["pat"]) / abs(prev["pat"]) * 100, 1)

    if len(quarters) >= 5:
        yoy = quarters[4]
        if latest.get("revenue") and yoy.get("revenue") and yoy["revenue"] != 0:
            g["rev_yoy"] = round((latest["revenue"] - yoy["revenue"]) / abs(yoy["revenue"]) * 100, 1)
        if latest.get("pat") and yoy.get("pat") and yoy["pat"] != 0:
            g["pat_yoy"] = round((latest["pat"] - yoy["pat"]) / abs(yoy["pat"]) * 100, 1)

    return g


# ─── Claude evaluation ────────────────────────────────────────────────────────

def _build_prompt(symbol: str, gap_pct: float, rvol: float,
                  quarters: list[dict], growth: dict) -> str:
    """Build the user-turn prompt for Claude fundamental gate."""
    qlines = []
    for q in quarters:
        rev = f"₹{q['revenue']:,.0f}cr" if q.get("revenue") is not None else "N/A"
        pat = f"₹{q['pat']:,.0f}cr"     if q.get("pat")     is not None else "N/A"
        qlines.append(f"  {q['period']}: Revenue {rev} | PAT {pat}")
    quarters_text = "\n".join(qlines) if qlines else "  Data unavailable"

    glines = []
    for label, key in [("Revenue YoY", "rev_yoy"), ("PAT YoY", "pat_yoy"),
                       ("Revenue QoQ", "rev_qoq"), ("PAT QoQ", "pat_qoq")]:
        if growth.get(key) is not None:
            glines.append(f"  {label}: {growth[key]:+.1f}%")
    growth_text = "\n".join(glines) if glines else "  Insufficient quarters for growth calc"

    return f"""Evaluate this earnings announcement for gap-and-go trade approval.

Symbol: {symbol} (NSE)
Gap at open: {gap_pct:+.1f}% vs prev close
Relative Volume: {rvol:.1f}x

Quarterly results (most recent first, ₹ Crores):
{quarters_text}

Growth:
{growth_text}

Should we take a LONG trade on this earnings gap? Reply with JSON only."""


async def _call_claude_gate(prompt: str, symbol: str) -> FundamentalGateResult:
    """Call Claude for the fundamental gate decision."""
    from config.settings import settings
    from anthropic import AsyncAnthropic, APIError, APITimeoutError

    if not settings.anthropic_api_key:
        return FundamentalGateResult(
            approve=True, confidence_adj=0,
            red_flags=["Claude API key not configured"],
            reasoning="Approving — no API key for fundamental check.",
        )

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    try:
        resp = await client.messages.create(
            model      = settings.claude_model,
            max_tokens = 256,
            system     = _FUNDAMENTAL_GATE_SYSTEM,
            messages   = [{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()

        # Strip markdown fences if Claude wraps in code block
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
        if raw.endswith("```"):
            raw = "\n".join(raw.split("\n")[:-1])
        raw = raw.strip()

        data = json.loads(raw)
        return FundamentalGateResult(
            approve        = bool(data.get("approve", True)),
            confidence_adj = int(max(-10, min(10, data.get("confidence_adj", 0)))),
            red_flags      = [str(f) for f in data.get("red_flags", []) if f][:5],
            reasoning      = str(data.get("reasoning", ""))[:500],
        )

    except (APIError, APITimeoutError) as e:
        log.warning("fundamental_gate.claude_api_error", symbol=symbol, error=str(e))
        return FundamentalGateResult(
            approve=True, confidence_adj=0,
            red_flags=["Claude API unavailable"],
            reasoning=f"API error: {e}. Approving on technical signal.",
        )
    except json.JSONDecodeError as e:
        log.warning("fundamental_gate.json_parse_error", symbol=symbol, error=str(e))
        return FundamentalGateResult(
            approve=True, confidence_adj=0,
            red_flags=["Malformed Claude response"],
            reasoning="JSON parse failed. Approving on technical signal.",
        )
    except Exception as e:
        log.warning("fundamental_gate.unexpected_error", symbol=symbol, error=str(e))
        return FundamentalGateResult(
            approve=True, confidence_adj=0,
            red_flags=["Fundamental check error"],
            reasoning=f"Unexpected error: {e}. Approving on technical signal.",
        )


# ─── Public interface ─────────────────────────────────────────────────────────

async def evaluate_fundamental_gate(
    symbol:  str,
    gap_pct: float,
    rvol:    float,
) -> FundamentalGateResult:
    """
    Main entry point. Fetches financials, calls Claude, caches result.

    Always approves (fail-open) if screener.in or Claude are unavailable.
    Cached in Redis for 8h — result quality doesn't change intraday.
    """
    from database.connection import get_redis

    redis     = get_redis()
    cache_key = f"earnings:fgate:{symbol}"

    # Return cached result if available
    cached = await redis.get(cache_key)
    if cached:
        try:
            data = json.loads(cached)
            log.debug("fundamental_gate.cache_hit", symbol=symbol)
            return FundamentalGateResult(
                approve        = data["approve"],
                confidence_adj = data["confidence_adj"],
                red_flags      = data["red_flags"],
                reasoning      = data["reasoning"],
            )
        except Exception:
            pass

    # Fetch quarterly financials from screener.in
    fin_data = await asyncio.get_running_loop().run_in_executor(
        None, _fetch_screener_sync, symbol
    )

    if not fin_data or not fin_data.get("quarters"):
        log.info("fundamental_gate.no_financial_data",
                 symbol=symbol, action="approve_default")
        result = FundamentalGateResult(
            approve        = True,
            confidence_adj = 0,
            red_flags      = ["Financial data unavailable — cannot verify result quality"],
            reasoning      = "No quarterly data on screener.in. Approving on technical signal only.",
        )
        await redis.setex(cache_key, 4 * 3600, json.dumps(result._asdict()))
        return result

    quarters = fin_data["quarters"]
    growth   = _compute_growth(quarters)
    prompt   = _build_prompt(symbol, gap_pct, rvol, quarters, growth)

    result = await _call_claude_gate(prompt, symbol)

    log.info(
        "fundamental_gate.evaluated",
        symbol        = symbol,
        gap_pct       = round(gap_pct, 2),
        approve       = result.approve,
        confidence_adj = result.confidence_adj,
        red_flags     = result.red_flags,
        rev_yoy       = growth.get("rev_yoy"),
        pat_yoy       = growth.get("pat_yoy"),
    )

    # Cache 8h
    await redis.setex(cache_key, 8 * 3600, json.dumps(result._asdict()))
    return result
