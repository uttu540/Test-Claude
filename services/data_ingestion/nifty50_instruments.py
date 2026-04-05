"""
services/data_ingestion/nifty50_instruments.py
───────────────────────────────────────────────
Nifty 50 stock universe with NSE trading symbols.

Kite instrument tokens are fetched dynamically from Kite Connect's
/instruments endpoint (tokens change on corporate actions/splits).
Run `python -m services.data_ingestion.nifty50_instruments` to
refresh tokens after getting your API key.
"""
from __future__ import annotations

# ─── Nifty 50 constituents (as of April 2025) ────────────────────────────────
# Format: (trading_symbol, company_name, sector)
NIFTY50 = [
    ("ADANIENT",    "Adani Enterprises Ltd",             "Energy"),
    ("ADANIPORTS",  "Adani Ports and Special Economic Zone Ltd", "Industrials"),
    ("APOLLOHOSP",  "Apollo Hospitals Enterprise Ltd",   "Healthcare"),
    ("ASIANPAINT",  "Asian Paints Ltd",                  "Materials"),
    ("AXISBANK",    "Axis Bank Ltd",                     "Financials"),
    ("BAJAJ-AUTO",  "Bajaj Auto Ltd",                    "Consumer Discretionary"),
    ("BAJAJFINSV",  "Bajaj Finserv Ltd",                 "Financials"),
    ("BAJFINANCE",  "Bajaj Finance Ltd",                 "Financials"),
    ("BHARTIARTL",  "Bharti Airtel Ltd",                 "Communication Services"),
    ("BPCL",        "Bharat Petroleum Corporation Ltd",  "Energy"),
    ("BRITANNIA",   "Britannia Industries Ltd",          "Consumer Staples"),
    ("CIPLA",       "Cipla Ltd",                         "Healthcare"),
    ("COALINDIA",   "Coal India Ltd",                    "Energy"),
    ("DIVISLAB",    "Divi's Laboratories Ltd",           "Healthcare"),
    ("DRREDDY",     "Dr. Reddy's Laboratories Ltd",      "Healthcare"),
    ("EICHERMOT",   "Eicher Motors Ltd",                 "Consumer Discretionary"),
    ("ETERNAL",     "Eternal Ltd (Zomato)",              "Consumer Discretionary"),
    ("GRASIM",      "Grasim Industries Ltd",             "Materials"),
    ("HCLTECH",     "HCL Technologies Ltd",              "Information Technology"),
    ("HDFC",        "HDFC Ltd",                          "Financials"),
    ("HDFCBANK",    "HDFC Bank Ltd",                     "Financials"),
    ("HDFCLIFE",    "HDFC Life Insurance Company Ltd",   "Financials"),
    ("HEROMOTOCO",  "Hero MotoCorp Ltd",                 "Consumer Discretionary"),
    ("HINDALCO",    "Hindalco Industries Ltd",           "Materials"),
    ("HINDUNILVR",  "Hindustan Unilever Ltd",            "Consumer Staples"),
    ("ICICIBANK",   "ICICI Bank Ltd",                    "Financials"),
    ("INDUSINDBK",  "IndusInd Bank Ltd",                 "Financials"),
    ("INFY",        "Infosys Ltd",                       "Information Technology"),
    ("ITC",         "ITC Ltd",                           "Consumer Staples"),
    ("JSWSTEEL",    "JSW Steel Ltd",                     "Materials"),
    ("KOTAKBANK",   "Kotak Mahindra Bank Ltd",           "Financials"),
    ("LT",          "Larsen & Toubro Ltd",               "Industrials"),
    ("M&M",         "Mahindra & Mahindra Ltd",           "Consumer Discretionary"),
    ("MARUTI",      "Maruti Suzuki India Ltd",           "Consumer Discretionary"),
    ("NESTLEIND",   "Nestle India Ltd",                  "Consumer Staples"),
    ("NTPC",        "NTPC Ltd",                          "Utilities"),
    ("ONGC",        "Oil & Natural Gas Corporation Ltd", "Energy"),
    ("POWERGRID",   "Power Grid Corporation of India Ltd", "Utilities"),
    ("RELIANCE",    "Reliance Industries Ltd",           "Energy"),
    ("SBILIFE",     "SBI Life Insurance Company Ltd",    "Financials"),
    ("SBIN",        "State Bank of India",               "Financials"),
    ("SHRIRAMFIN",  "Shriram Finance Ltd",               "Financials"),
    ("SUNPHARMA",   "Sun Pharmaceutical Industries Ltd", "Healthcare"),
    ("TATACONSUM",  "Tata Consumer Products Ltd",        "Consumer Staples"),
    ("TATAMOTORS",  "Tata Motors Ltd",                   "Consumer Discretionary"),
    ("TATASTEEL",   "Tata Steel Ltd",                    "Materials"),
    ("TCS",         "Tata Consultancy Services Ltd",     "Information Technology"),
    ("TECHM",       "Tech Mahindra Ltd",                 "Information Technology"),
    ("TITAN",       "Titan Company Ltd",                 "Consumer Discretionary"),
    ("ULTRACEMCO",  "UltraTech Cement Ltd",              "Materials"),
    ("WIPRO",       "Wipro Ltd",                         "Information Technology"),
]

# ─── Index instruments ───────────────────────────────────────────────────────
# These are for tracking only (cannot be traded directly as equities)
INDEX_INSTRUMENTS = [
    ("NIFTY 50",     "NSE:NIFTY 50",    256265),   # Kite token
    ("NIFTY BANK",   "NSE:NIFTY BANK",  260105),
    ("INDIA VIX",    "NSE:INDIA VIX",   264969),
    ("NIFTY IT",     "NSE:NIFTY IT",    259849),
    ("NIFTY FMCG",   "NSE:NIFTY FMCG", 257801),
]

# ─── Helper ───────────────────────────────────────────────────────────────────

def get_nifty50_symbols() -> list[str]:
    """Return just the trading symbols."""
    return [row[0] for row in NIFTY50]


def get_nifty50_by_sector() -> dict[str, list[str]]:
    """Return {sector: [symbols]} mapping."""
    result: dict[str, list[str]] = {}
    for symbol, _, sector in NIFTY50:
        result.setdefault(sector, []).append(symbol)
    return result


if __name__ == "__main__":
    # Run this after setting up Kite API key to fetch and print all tokens
    import os
    from kiteconnect import KiteConnect

    api_key = os.environ.get("KITE_API_KEY", "")
    access_token = os.environ.get("KITE_ACCESS_TOKEN", "")

    if not api_key or not access_token:
        print("Set KITE_API_KEY and KITE_ACCESS_TOKEN env vars first.")
        raise SystemExit(1)

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    instruments = kite.instruments("NSE")
    token_map = {i["tradingsymbol"]: i["instrument_token"] for i in instruments}

    print(f"\n{'Symbol':<15} {'Token'}")
    print("─" * 30)
    for symbol, name, _ in NIFTY50:
        token = token_map.get(symbol, "NOT FOUND")
        print(f"{symbol:<15} {token}")
