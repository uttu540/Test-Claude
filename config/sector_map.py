"""
config/sector_map.py
─────────────────────
Static NSE sector mapping for Nifty 500 stocks.
Used by TradeExecutor to enforce the max-2-positions-per-sector cap.

Source: NSE sectoral index constituents (Jan 2025).
Stocks not in this map → "MISC" (unlimited positions, no cap applied).

Update periodically when index rebalancing happens (Feb + Aug typically).
"""
from __future__ import annotations

# fmt: off
_SECTOR_MAP: dict[str, str] = {
    # ── Banking ───────────────────────────────────────────────────────────────
    "HDFCBANK": "Banking", "ICICIBANK": "Banking", "KOTAKBANK": "Banking",
    "AXISBANK": "Banking", "SBIN": "Banking", "BANKBARODA": "Banking",
    "PNB": "Banking", "CANBK": "Banking", "UNIONBANK": "Banking",
    "IDFCFIRSTB": "Banking", "FEDERALBNK": "Banking", "INDUSINDBK": "Banking",
    "YESBANK": "Banking", "RBLBANK": "Banking", "BANDHANBNK": "Banking",
    "AUBANK": "Banking", "KARURVYSYA": "Banking", "DCBBANK": "Banking",
    "CUB": "Banking", "MAHABANK": "Banking", "UCOBANK": "Banking",
    "INDIANB": "Banking", "IOB": "Banking", "J&KBANK": "Banking",

    # ── Non-Banking Finance (NBFC) ────────────────────────────────────────────
    "BAJFINANCE": "NBFC", "BAJAJFINSV": "NBFC", "MUTHOOTFIN": "NBFC",
    "CHOLAFIN": "NBFC", "M&MFIN": "NBFC", "SHRIRAMFIN": "NBFC",
    "MANAPPURAM": "NBFC", "POONAWALLA": "NBFC", "CREDITACC": "NBFC",
    "AAVAS": "NBFC", "HOMEFIRST": "NBFC", "APTUS": "NBFC",

    # ── Insurance ─────────────────────────────────────────────────────────────
    "SBILIFE": "Insurance", "HDFCLIFE": "Insurance", "ICICIPRULI": "Insurance",
    "LICI": "Insurance", "STARHEALTH": "Insurance", "NIACL": "Insurance",
    "GICRE": "Insurance", "ICICIGI": "Insurance",

    # ── Information Technology ────────────────────────────────────────────────
    "TCS": "IT", "INFY": "IT", "WIPRO": "IT", "HCLTECH": "IT",
    "TECHM": "IT", "LTIM": "IT", "MPHASIS": "IT", "PERSISTENT": "IT",
    "COFORGE": "IT", "OFSS": "IT", "KPITTECH": "IT", "TATAELXSI": "IT",
    "CYIENT": "IT", "MASTEK": "IT", "NIIT": "IT", "HEXAWARE": "IT",
    "BIRLASOFT": "IT", "SONATSOFTW": "IT", "ZENSAR": "IT", "FSL": "IT",
    "INTELLECT": "IT", "TANLA": "IT", "BSOFT": "IT",

    # ── Pharmaceuticals & Healthcare ──────────────────────────────────────────
    "SUNPHARMA": "Pharma", "DRREDDY": "Pharma", "CIPLA": "Pharma",
    "DIVISLAB": "Pharma", "LUPIN": "Pharma", "BIOCON": "Pharma",
    "AUROPHARMA": "Pharma", "TORNTPHARM": "Pharma", "ALKEM": "Pharma",
    "IPCALAB": "Pharma", "GLENMARK": "Pharma", "GRANULES": "Pharma",
    "NATCOPHARM": "Pharma", "SUDARSCHEM": "Pharma", "AJANTPHARM": "Pharma",
    "LAURUSLABS": "Pharma", "SOLARA": "Pharma", "SHILPAMED": "Pharma",
    "APOLLOHOSP": "Pharma", "MAXHEALTH": "Pharma", "METROPOLIS": "Pharma",
    "THYROCARE": "Pharma", "FORTIS": "Pharma", "NH": "Pharma",

    # ── Automobile ────────────────────────────────────────────────────────────
    "MARUTI": "Auto", "TATAMOTORS": "Auto", "M&M": "Auto",
    "BAJAJ-AUTO": "Auto", "EICHERMOT": "Auto", "HEROMOTOCO": "Auto",
    "TVSMOTORS": "Auto", "ASHOKLEY": "Auto", "ESCORTS": "Auto",
    "MOTHERSON": "Auto", "BOSCHLTD": "Auto", "BALKRISIND": "Auto",
    "MRF": "Auto", "APOLLOTYRE": "Auto", "CEATLTD": "Auto",
    "EXIDEIND": "Auto", "AMARARAJA": "Auto", "MINDAIND": "Auto",
    "SUNDRAMBRAK": "Auto", "SUPRAJIT": "Auto",

    # ── FMCG / Consumer Staples ───────────────────────────────────────────────
    "HINDUNILVR": "FMCG", "ITC": "FMCG", "NESTLEIND": "FMCG",
    "BRITANNIA": "FMCG", "DABUR": "FMCG", "MARICO": "FMCG",
    "GODREJCP": "FMCG", "COLPAL": "FMCG", "EMAMILTD": "FMCG",
    "JYOTHYLAB": "FMCG", "TATACONSUM": "FMCG", "VARUNBEV": "FMCG",
    "UBL": "FMCG", "RADICO": "FMCG", "MCDOWELL-N": "FMCG",
    "VBL": "FMCG", "PATANJALI": "FMCG",

    # ── Metals & Mining ───────────────────────────────────────────────────────
    "TATASTEEL": "Metals", "JSWSTEEL": "Metals", "HINDALCO": "Metals",
    "VEDL": "Metals", "SAIL": "Metals", "NMDC": "Metals",
    "HINDZINC": "Metals", "NATIONALUM": "Metals", "JINDALSTEL": "Metals",
    "JSPL": "Metals", "APLAPOLLO": "Metals", "RATNAMANI": "Metals",
    "WELSPUNIND": "Metals", "MOIL": "Metals", "GMDC": "Metals",
    "COALINDIA": "Metals",

    # ── Energy / Oil & Gas ────────────────────────────────────────────────────
    "RELIANCE": "Energy", "ONGC": "Energy", "IOC": "Energy",
    "BPCL": "Energy", "HPCL": "Energy", "GAIL": "Energy",
    "PETRONET": "Energy", "OIL": "Energy", "MGL": "Energy",
    "IGL": "Energy", "GUJGASLTD": "Energy", "GSPL": "Energy",
    "AEGASIND": "Energy",

    # ── Power & Utilities ─────────────────────────────────────────────────────
    "NTPC": "Power", "POWERGRID": "Power", "ADANIPOWER": "Power",
    "TATAPOWER": "Power", "CESC": "Power", "TORNTPOWER": "Power",
    "ADANIGREEN": "Power", "SJVN": "Power", "NHPC": "Power",
    "RPOWER": "Power", "JPPOWER": "Power", "INOXWIND": "Power",
    "SUZLON": "Power", "KPIGREEN": "Power",

    # ── Cement ────────────────────────────────────────────────────────────────
    "ULTRACEMCO": "Cement", "AMBUJACEM": "Cement", "ACC": "Cement",
    "SHREECEM": "Cement", "DALMIACEM": "Cement", "RAMCOCEM": "Cement",
    "HEIDELBERG": "Cement", "JKLAKSHMI": "Cement", "BIRLACORPN": "Cement",
    "NCLIND": "Cement", "ORIENTCEM": "Cement",

    # ── Real Estate ───────────────────────────────────────────────────────────
    "DLF": "Realty", "GODREJPROP": "Realty", "OBEROIRLTY": "Realty",
    "PRESTIGE": "Realty", "PHOENIXLTD": "Realty", "BRIGADE": "Realty",
    "SOBHA": "Realty", "MAHLIFE": "Realty", "SUNTECK": "Realty",
    "KOLTEPATIL": "Realty", "LODHA": "Realty", "SIGNATURE": "Realty",

    # ── Industrials / Capital Goods ───────────────────────────────────────────
    "LT": "Industrials", "ABB": "Industrials", "SIEMENS": "Industrials",
    "BEL": "Industrials", "HAL": "Industrials", "BHEL": "Industrials",
    "CUMMINSIND": "Industrials", "THERMAX": "Industrials",
    "VOLTAS": "Industrials", "HAVELLS": "Industrials", "CROMPTON": "Industrials",
    "POLYCAB": "Industrials", "KEI": "Industrials", "APARINDS": "Industrials",
    "GRINDWELL": "Industrials", "TIMKEN": "Industrials",
    "SCHAEFFLER": "Industrials", "SKFINDIA": "Industrials",

    # ── Telecom & Media ───────────────────────────────────────────────────────
    "BHARTIARTL": "Telecom", "IDEA": "Telecom", "TATACOMM": "Telecom",
    "HFCL": "Telecom", "ZEEL": "Media", "PVRINOX": "Media",
    "SUNTV": "Media", "NETWORK18": "Media", "TV18BRDCST": "Media",

    # ── Chemicals ────────────────────────────────────────────────────────────
    "PIDILITIND": "Chemicals", "SRF": "Chemicals", "ATUL": "Chemicals",
    "DEEPAKNITR": "Chemicals", "FINEORG": "Chemicals", "NAVINFLUOR": "Chemicals",
    "ASIANPAINT": "Chemicals", "BERGERPAINTS": "Chemicals", "KANSAINER": "Chemicals",
    "ALKYLAMINE": "Chemicals", "TATACHEM": "Chemicals", "GNFC": "Chemicals",
    "GUJALKALI": "Chemicals", "BALAJI-AMINES": "Chemicals",

    # ── Consumer Discretionary ────────────────────────────────────────────────
    "TITAN": "Consumer", "TRENT": "Consumer", "ABFRL": "Consumer",
    "PAGEIND": "Consumer", "VEDANT": "Consumer", "ADITBIRLANU": "Consumer",
    "BATAINDIA": "Consumer", "RELAXO": "Consumer", "WHIRLPOOL": "Consumer",
    "RAJESHEXPO": "Consumer", "SENCO": "Consumer", "KALYANKJIL": "Consumer",
    "CAMPUS": "Consumer", "DMART": "Consumer", "NYKAA": "Consumer",
    "ZOMATO": "Consumer", "SWIGGY": "Consumer", "INDHOTEL": "Consumer",
    "LEMONTREE": "Consumer", "CHALET": "Consumer", "EIHOTEL": "Consumer",

    # ── Logistics & Transport ─────────────────────────────────────────────────
    "ADANIPORTS": "Logistics", "CONCOR": "Logistics", "VRL": "Logistics",
    "BLUEDART": "Logistics", "GESHIP": "Logistics", "SEAMECLTD": "Logistics",
    "TCI": "Logistics", "MAHLOG": "Logistics", "DELHIVERY": "Logistics",

    # ── Agriculture ───────────────────────────────────────────────────────────
    "UPL": "Agri", "PI": "Agri", "DHANUKA": "Agri", "BAYER": "Agri",
    "RALLIS": "Agri", "KAVERI": "Agri", "CHAMBAL": "Agri",
}
# fmt: on


def get_sector(trading_symbol: str) -> str:
    """
    Return the GICS-style sector for a trading symbol.
    Returns "MISC" for unknown stocks — MISC is exempt from the sector cap
    so new/unlisted stocks don't get blocked without reason.
    """
    return _SECTOR_MAP.get(trading_symbol.upper(), "MISC")
