"""
services/data_ingestion/websocket_feed.py
──────────────────────────────────────────
Zerodha Kite Connect WebSocket feed.

Subscribes to live ticks for the Nifty 50 universe and pushes data to:
  1. Redis (real-time price cache — sub-millisecond reads)
  2. In-memory candle aggregator (builds OHLCV candles from raw ticks)
  3. TimescaleDB (persists completed candles)

Modes:
  - LIVE: Connects to Kite WebSocket with real API credentials
  - MOCK: Generates synthetic ticks for development (no API key needed)
"""
from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

import structlog
from kiteconnect import KiteTicker

from config.settings import settings
from database.connection import get_redis
from services.data_ingestion.nifty500_instruments import (
    INDEX_INSTRUMENTS,
    NIFTY500,
    get_nifty500_symbols,
)

log = structlog.get_logger(__name__)

# ─── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class Tick:
    """Normalised tick from any data source."""
    instrument_token: int
    trading_symbol: str
    last_price: float
    volume: int
    buy_quantity: int
    sell_quantity: int
    open: float
    high: float
    low: float
    close: float          # Previous day close
    change: float         # % change from prev close
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class OHLCVCandle:
    """A single OHLCV candle bar."""
    trading_symbol: str
    timeframe: str         # "1min", "5min", "15min", "1day" etc.
    open: float
    high: float
    low: float
    close: float
    volume: int
    timestamp: datetime    # Candle open time


# ─── Candle Aggregator ────────────────────────────────────────────────────────

class CandleAggregator:
    """
    Builds OHLCV candles from raw ticks in-memory.
    Emits completed candles via a callback.
    """
    TIMEFRAMES = {
        "1min":  timedelta(minutes=1),
        "5min":  timedelta(minutes=5),
        "15min": timedelta(minutes=15),
        "1hr":   timedelta(hours=1),
    }

    def __init__(self, on_candle_complete: Callable[[OHLCVCandle], None]):
        self._on_candle = on_candle_complete
        # {symbol: {timeframe: current_candle_data}}
        self._candles: dict[str, dict[str, dict]] = {}

    def process_tick(self, tick: Tick) -> None:
        sym = tick.trading_symbol
        if sym not in self._candles:
            self._candles[sym] = {}

        for tf, delta in self.TIMEFRAMES.items():
            # Bucket the tick into its candle period
            period_start = self._get_period_start(tick.timestamp, delta)

            if tf not in self._candles[sym]:
                # Start a new candle
                self._candles[sym][tf] = self._new_candle(sym, tf, period_start, tick)
                continue

            candle = self._candles[sym][tf]

            if candle["timestamp"] == period_start:
                # Update existing candle
                candle["high"]   = max(candle["high"],   tick.last_price)
                candle["low"]    = min(candle["low"],    tick.last_price)
                candle["close"]  = tick.last_price
                candle["volume"] += tick.volume
            else:
                # Candle period rolled over — emit the completed candle
                completed = OHLCVCandle(**candle)
                self._on_candle(completed)
                # Start fresh
                self._candles[sym][tf] = self._new_candle(sym, tf, period_start, tick)

    @staticmethod
    def _new_candle(symbol: str, tf: str, ts: datetime, tick: Tick) -> dict:
        return {
            "trading_symbol": symbol,
            "timeframe": tf,
            "open":    tick.last_price,
            "high":    tick.last_price,
            "low":     tick.last_price,
            "close":   tick.last_price,
            "volume":  tick.volume,
            "timestamp": ts,
        }

    @staticmethod
    def _get_period_start(ts: datetime, delta: timedelta) -> datetime:
        from config.market_hours import MARKET_OPEN
        epoch = datetime(ts.year, ts.month, ts.day, MARKET_OPEN.hour, MARKET_OPEN.minute)
        elapsed = (ts - epoch).total_seconds()
        bucket  = int(elapsed / delta.total_seconds()) * int(delta.total_seconds())
        return epoch + timedelta(seconds=bucket)


# ─── Live Zerodha Feed ────────────────────────────────────────────────────────

class ZerodhaFeed:
    """
    Connects to Kite WebSocket and streams live ticks.
    Requires a valid access_token in Redis (set by the authenticator daily).
    """

    def __init__(self, on_tick: Callable[[Tick], None]):
        self._on_tick = on_tick
        self._ticker: KiteTicker | None = None
        self._running = False

    async def start(self) -> None:
        redis = get_redis()
        access_token = await redis.get("kite:access_token")

        if not access_token:
            log.error("zerodha_feed.start", error="No access token in Redis. Run authenticator first.")
            raise RuntimeError("Kite access token missing. Run `make dev` after authentication.")

        self._ticker = KiteTicker(settings.kite_api_key, access_token)

        # Fetch instrument tokens from Redis (populated by historical_seed)
        token_json = await redis.get("kite:instrument_tokens")
        tokens: list[int] = json.loads(token_json) if token_json else []

        if not tokens:
            log.warning("zerodha_feed.start", warning="No instrument tokens found. Using index tokens only.")
            tokens = [t for _, _, t in INDEX_INSTRUMENTS]

        self._ticker.on_ticks         = self._on_ticks_raw
        self._ticker.on_connect       = self._on_connect
        self._ticker.on_close         = self._on_close
        self._ticker.on_error         = self._on_error
        self._ticker.on_reconnect     = self._on_reconnect
        self._ticker.on_noreconnect   = self._on_noreconnect

        self._running = True
        self._tokens = tokens

        # KiteTicker runs its own thread internally
        log.info("zerodha_feed.start", token_count=len(tokens))
        self._ticker.connect(threaded=True)

    def stop(self) -> None:
        self._running = False
        if self._ticker:
            self._ticker.close()
            log.info("zerodha_feed.stop", status="disconnected")

    # ── KiteTicker callbacks (run in ticker's thread) ─────────────────────────

    def _on_connect(self, ws, response) -> None:
        log.info("zerodha_feed.connected", token_count=len(self._tokens))
        ws.subscribe(self._tokens)
        ws.set_mode(ws.MODE_FULL, self._tokens)

    def _on_ticks_raw(self, ws, ticks: list[dict]) -> None:
        for raw in ticks:
            try:
                tick = self._normalise_tick(raw)
                if tick:
                    self._on_tick(tick)
            except Exception as e:
                log.warning("zerodha_feed.tick_parse_error", error=str(e), raw=raw)

    def _on_close(self, ws, code, reason) -> None:
        log.warning("zerodha_feed.closed", code=code, reason=reason)

    def _on_error(self, ws, code, reason) -> None:
        log.error("zerodha_feed.error", code=code, reason=reason)

    def _on_reconnect(self, ws, attempts) -> None:
        log.info("zerodha_feed.reconnect", attempt=attempts)

    def _on_noreconnect(self, ws) -> None:
        log.error("zerodha_feed.no_reconnect", status="max_retries_exceeded")

    @staticmethod
    def _normalise_tick(raw: dict) -> Tick | None:
        """Convert Kite raw tick dict to our normalised Tick dataclass."""
        try:
            prev_close = raw.get("ohlc", {}).get("close", 0) or 1
            last = raw.get("last_price", 0)
            change = ((last - prev_close) / prev_close) * 100 if prev_close else 0

            return Tick(
                instrument_token = raw["instrument_token"],
                trading_symbol   = raw.get("tradingsymbol", ""),
                last_price       = float(last),
                volume           = int(raw.get("volume_traded", 0)),
                buy_quantity     = int(raw.get("total_buy_quantity", 0)),
                sell_quantity    = int(raw.get("total_sell_quantity", 0)),
                open             = float(raw.get("ohlc", {}).get("open", last)),
                high             = float(raw.get("ohlc", {}).get("high", last)),
                low              = float(raw.get("ohlc", {}).get("low", last)),
                close            = float(prev_close),
                change           = round(change, 2),
                timestamp        = datetime.now(),
            )
        except (KeyError, TypeError, ValueError):
            return None


# ─── Mock Feed (Development Mode) ─────────────────────────────────────────────

class MockFeed:
    """
    Generates realistic synthetic ticks for development.
    No Kite API key required.
    Uses a random walk with mean reversion to simulate price movement.
    Supports the full Nifty 500 universe — known prices seeded from
    approximate April 2025 values; remaining symbols default to 500.0.
    """

    # Known seed prices (approximate April 2025 values, NSE)
    _KNOWN_PRICES: dict[str, float] = {
        # Nifty 50
        "RELIANCE": 1280.0, "HDFCBANK": 1750.0, "ICICIBANK": 1320.0,
        "INFY": 1580.0, "TCS": 3400.0, "HINDUNILVR": 2400.0,
        "SBIN": 790.0, "BHARTIARTL": 1820.0, "ITC": 425.0,
        "KOTAKBANK": 2050.0, "LT": 3300.0, "AXISBANK": 1120.0,
        "HCLTECH": 1540.0, "WIPRO": 480.0, "SUNPHARMA": 1720.0,
        "MARUTI": 11500.0, "BAJFINANCE": 8900.0, "TITAN": 3200.0,
        "NTPC": 355.0, "ONGC": 275.0, "POWERGRID": 310.0,
        "TECHM": 1320.0, "ASIANPAINT": 2200.0, "NESTLEIND": 2350.0,
        "TATASTEEL": 155.0, "JSWSTEEL": 920.0, "HINDALCO": 660.0,
        "TATAMOTORS": 720.0, "M&M": 2900.0, "BAJAJ-AUTO": 8800.0,
        "HEROMOTOCO": 4300.0, "EICHERMOT": 4700.0, "DRREDDY": 1280.0,
        "CIPLA": 1490.0, "DIVISLAB": 5200.0, "APOLLOHOSP": 6900.0,
        "GRASIM": 2750.0, "ULTRACEMCO": 11800.0, "TATACONSUM": 930.0,
        "BRITANNIA": 5100.0, "COALINDIA": 395.0, "BPCL": 310.0,
        "HDFCLIFE": 630.0, "SBILIFE": 1620.0, "SHRIRAMFIN": 600.0,
        "BAJAJFINSV": 1970.0, "INDUSINDBK": 1000.0, "ADANIPORTS": 1200.0,
        "ADANIENT": 2350.0, "ETERNAL": 205.0,
        # Nifty Next 50 (approximate)
        "ABB": 5200.0, "AMBUJACEM": 620.0, "AUROPHARMA": 1100.0,
        "BANKBARODA": 240.0, "BERGEPAINT": 530.0, "BOSCHLTD": 32000.0,
        "CANBK": 110.0, "CHOLAFIN": 1200.0, "COLPAL": 2600.0,
        "CUMMINSIND": 3200.0, "DLF": 820.0, "GODREJCP": 1100.0,
        "GODREJPROP": 2200.0, "HAVELLS": 1700.0, "HINDZINC": 420.0,
        "ICICIGI": 1700.0, "ICICIPRULI": 680.0, "IDFCFIRSTB": 70.0,
        "IGL": 380.0, "INDUSTOWER": 330.0, "IOC": 165.0,
        "IRCTC": 750.0, "LUPIN": 1700.0, "MARICO": 580.0,
        "MCDOWELL-N": 1050.0, "MUTHOOTFIN": 1900.0, "NAUKRI": 6500.0,
        "NHPC": 90.0, "NMDC": 220.0, "OFSS": 11500.0,
        "PAGEIND": 40000.0, "PIDILITIND": 2900.0, "PIIND": 4000.0,
        "PNB": 110.0, "RECLTD": 500.0, "SIEMENS": 7500.0,
        "TRENT": 5000.0, "TORNTPHARM": 2800.0, "TVSMOTOR": 2400.0,
        "UNIONBANK": 130.0, "UPL": 520.0, "VEDL": 430.0,
        "VOLTAS": 1450.0, "ZYDUSLIFE": 950.0,
        # Midcap (approximate)
        "DMART": 4500.0, "LICI": 980.0, "MPHASIS": 2600.0,
        "PERSISTENT": 5500.0, "COFORGE": 8000.0, "LTIM": 5500.0,
        "LTTS": 5000.0, "TATAELXSI": 6500.0, "KPITTECH": 1500.0,
        "POLYCAB": 5500.0, "DIXON": 14000.0, "SAIL": 145.0,
        "JINDALSTEL": 900.0, "TATAPOWER": 400.0, "GAIL": 200.0,
        "PFC": 470.0, "IRFC": 190.0, "HUDCO": 230.0,
        "VBL": 600.0, "MAXHEALTH": 1000.0, "FORTIS": 620.0,
        "JUBLFOOD": 650.0, "JUBILANT": 700.0, "FEDERALBNK": 190.0,
        "BANDHANBNK": 185.0, "AUBANK": 600.0, "RBLBANK": 220.0,
        "CUB": 175.0, "KARURVYSYA": 200.0, "INDIANB": 540.0,
        "IDBI": 85.0, "IOB": 55.0, "PSB": 45.0,
        "OBEROIRLTY": 1750.0, "PHOENIXLTD": 1700.0, "PRESTIGE": 1600.0,
        "SOBHA": 1650.0, "BRIGADE": 950.0, "SUNTECK": 550.0,
        "BSE": 4500.0, "CDSL": 1600.0, "ANGELONE": 2900.0,
        "MOTILALOFS": 800.0, "ISEC": 700.0, "KFINTECH": 800.0,
        "STAR": 600.0, "MFSL": 1000.0, "NIACL": 180.0,
        "MRF": 125000.0, "CEATLTD": 2800.0, "BALKRISIND": 2700.0,
        "AMARAJABAT": 800.0, "EXIDEIND": 450.0, "TATACHEM": 1050.0,
        "DEEPAKNTR": 2200.0, "AARTIIND": 550.0, "VINATIORGA": 1900.0,
        "NAVINFLUOR": 3500.0, "CLEAN": 1500.0, "LXCHEM": 330.0,
        ("SOLARINDS"): 9000.0, "BHEL": 280.0, "BEL": 270.0,
        "CONCOR": 850.0, "NMDC": 220.0, "MOIL": 350.0,
        "NLCINDIA": 230.0, "SJVN": 120.0, "ADANIGREEN": 1800.0,
        "ADANIPOWER": 550.0, "JSWENERGY": 550.0, "TORNTPOWER": 1600.0,
        "SUZLON": 55.0, "INOXWIND": 190.0, "TATACOMM": 1750.0,
        "SUNTV": 600.0, "ZEEL": 140.0, "NETWORK18": 90.0,
        "TV18BRDCST": 55.0, "NAUKRI": 6500.0, "INDIAMART": 2800.0,
        "JUSTDIAL": 950.0, "ROUTE": 1600.0, "TANLA": 1000.0,
        "AFFLE": 1600.0, "MAPMYINDIA": 1500.0, "LATENTVIEW": 450.0,
        "HAPPSTMNDS": 750.0, "MASTEK": 2500.0,
        "ALKEM": 5000.0, "IPCA": 1450.0, "GLENMARK": 950.0,
        "BIOCON": 330.0, "WOCKPHARMA": 650.0, "NATCOPHARM": 1000.0,
        "GRANULES": 500.0, "IPCALAB": 1450.0, "LAURUSLABS": 450.0,
        "SEQUENT": 80.0, "SOLARA": 500.0, "SPARC": 350.0,
        "AJANTPHARM": 2500.0, "CAPLIPOINT": 900.0,
        "METROPOLIS": 1700.0, "LALPATHLAB": 2100.0, "THYROCARE": 650.0,
        "SYNGENE": 700.0, "MEDANTA": 1200.0,
        "DELHIVERY": 400.0, "TEAMLEASE": 2800.0, "QUESS": 550.0,
        "PVRINOX": 1500.0, "LEMONTREE": 130.0, "CHALET": 850.0,
        "WONDERLA": 700.0, "MHRIL": 340.0,
        "ASHOKLEY": 220.0, "ESCORTS": 3400.0, "MOTHERSON": 190.0,
        "UNOMINDA": 950.0, "MINDACORP": 450.0, "SONACOMS": 600.0,
        "BHARAT FORGE": 1300.0, "BHARATFORG": 1300.0, "TIINDIA": 3800.0,
        "SUNDRMFAST": 1300.0, "CRAFTSMAN": 5500.0, "RKFORGE": 900.0,
        "JBMA": 1700.0, "ENDURANCE": 2200.0, "VARROC": 600.0,
        "GABRIEL": 220.0, "MAHINDCIE": 500.0, "PPAP": 190.0,
        "PGIL": 450.0, "SUBROS": 330.0, "PARSONDES": 180.0,
        "RAMCOCEM": 890.0, "DALBHARAT": 2100.0, "JKCEMENT": 4300.0,
        "JKLAKSHMI": 820.0, "NUVOCO": 380.0, "STARCEMENT": 200.0,
        "PRSMJOHNSN": 165.0, "ORIENTCEM": 185.0, "SHREDIGCEM": 175.0,
        "SHREECEM": 24000.0, "AMBUJACEM": 620.0,
        "CENTURYPLY": 650.0, "GREENPLY": 230.0, "ASTRAL": 1900.0,
        "PRINCEPIPE": 700.0, "FINPIPE": 200.0,
        "KAJARIACER": 1200.0, "CERA": 8000.0, "ORIENTELEC": 290.0,
        "VGUARD": 450.0, "CROMPTON": 320.0, "HAVELLS": 1700.0,
        "POLYCAB": 5500.0, "FINOLEX": 900.0,
        "TITAGARH": 1200.0, "KALPATPOWR": 1200.0, "KPIL": 1200.0,
        "KEC": 800.0, "IRB": 70.0, "HGINFRA": 1200.0,
        "KNRCON": 700.0, "PATELENG": 60.0, "ITDCEM": 210.0,
        "JKIL": 700.0, "NBCC": 130.0, "EIL": 185.0,
        "SPMLINFRA": 50.0, "RAMKY": 135.0, "MNRE": 700.0,
        "RATNAMANI": 3200.0, "WELCORP": 650.0, "MAHSEAMLES": 700.0,
        "JSPL": 900.0, "SHYAMMETL": 600.0, "GPIL": 800.0,
        "TINPLATE": 350.0, "TATASTLBSL": 120.0, "SUNFLAG": 290.0,
        "HEG": 2000.0, "LLOYDSME": 700.0, "PCBL": 380.0,
        "SAIL": 145.0, "NMDC": 220.0,
        "GODREJAGRO": 700.0, "DHANUKA": 1000.0, "SHARDACROP": 600.0,
        "INSECTICIDE": 650.0, "BAYERCROP": 5500.0, "SUMICHEM": 480.0,
        "PIDILITIND": 2900.0, "ATUL": 7500.0, "VINATIORGA": 1900.0,
        "DCMSHRIRAM": 1000.0, "GNFC": 650.0, "CHAMBLFERT": 350.0,
        "FACT": 700.0, "EIDPARRY": 600.0, "KRBL": 350.0,
        "BALRAMCHIN": 420.0, "VENKEYS": 1900.0, "TASTYB": 10000.0,
        "TASTYBITE": 10000.0, "GODFRYPHLP": 5000.0, "VSTIND": 4000.0,
        "RADICO": 1600.0, "MCDOWELL-N": 1050.0,
        "EMAMILTD": 580.0, "MARICO": 580.0, "JYOTHYLAB": 360.0,
        "SPENCERS": 130.0, "SHOPERSTOP": 700.0, "VMART": 2000.0,
        "SAFARI": 2200.0, "VIPIND": 750.0, "TCNSBRANDS": 500.0,
        "TRENT": 5000.0, "WESTLIFE": 850.0, "PVRINOX": 1500.0,
        "DELTACORP": 200.0, "WONDERLA": 700.0,
        "VBL": 600.0, "PATANJALI": 1600.0, "PGHH": 16000.0,
        "COLPAL": 2600.0, "GODREJCP": 1100.0, "HINDUNILVR": 2400.0,
        "CASTROLIND": 220.0, "GULFOILLUB": 800.0,
        "OLECTRA": 1400.0, "JSWENERGY": 550.0, "SJVN": 120.0,
        "TATAPOWER": 400.0, "TORNTPOWER": 1600.0, "SUZLON": 55.0,
        "RPOWER": 25.0, "JPPOWER": 20.0, "INOXWIND": 190.0,
        "SWSOLAR": 350.0,
        "GESHIP": 1000.0, "CONCOR": 850.0, "BLUEDART": 7500.0,
        "SNOWMAN": 80.0, "DELHIVERY": 400.0,
        ("CGPOWER"): 700.0, "THERMAX": 3500.0, "CUMMINSIND": 3200.0,
        "GRINDWELL": 2000.0, "SKF": 4500.0, "SKFINDIA": 4500.0,
        "TIMKEN": 3300.0, "SCHAEFFLER": 3800.0, "KSB": 4000.0,
        "ELGIEQUIP": 700.0, "ELECON": 550.0, "ACE": 250.0,
        "ISGEC": 1100.0, "HARSHA": 600.0, "UNIPARTS": 500.0,
        "PRAJIND": 600.0, "BEML": 4500.0, "MTAR": 1800.0,
        "IDEAFORGE": 500.0, "KRISHNADEF": 650.0,
        "SOLARINDS": 9000.0, "ZENTEC": 800.0,
        "GMRINFRA": 80.0, "IRB": 70.0,
    }

    def __init__(self, on_tick: Callable[[Tick], None], symbols: list[str] | None = None):
        self._on_tick = on_tick
        # Build price dict from all N500 symbols; known prices seeded, rest default to 500
        from services.data_ingestion.nifty500_instruments import get_nifty500_symbols
        universe = symbols or get_nifty500_symbols()
        self._prices: dict[str, float] = {
            sym: self._KNOWN_PRICES.get(sym, 500.0) for sym in universe
        }
        self._volumes: dict[str, int] = {s: 0 for s in self._prices}
        self._running = False

    async def start(self) -> None:
        self._running = True
        log.info("mock_feed.start", mode="DEVELOPMENT", symbols=len(self._prices))
        asyncio.create_task(self._tick_loop())

    async def _tick_loop(self) -> None:
        token = 1000  # Fake token counter
        symbol_tokens = {sym: token + i for i, sym in enumerate(self._prices)}

        while self._running:
            for sym, price in list(self._prices.items()):
                # Random walk: ±0.05% per tick with mean reversion
                seed_price = self._KNOWN_PRICES.get(sym, 500.0)
                drift = (seed_price - price) * 0.0001   # mean reversion
                noise = random.gauss(drift, price * 0.0005)
                new_price = max(price + noise, price * 0.9)  # prevent extreme drops

                self._prices[sym] = round(new_price, 2)
                self._volumes[sym] += random.randint(100, 2000)

                tick = Tick(
                    instrument_token = symbol_tokens[sym],
                    trading_symbol   = sym,
                    last_price       = new_price,
                    volume           = self._volumes[sym],
                    buy_quantity     = random.randint(1000, 50000),
                    sell_quantity    = random.randint(1000, 50000),
                    open             = self.SEED_PRICES[sym],
                    high             = max(self.SEED_PRICES[sym], new_price),
                    low              = min(self.SEED_PRICES[sym], new_price),
                    close            = self.SEED_PRICES[sym],
                    change           = round((new_price - seed_price) / seed_price * 100, 2),
                )
                self._on_tick(tick)

            await asyncio.sleep(1)   # emit ticks every second

    def stop(self) -> None:
        self._running = False
        log.info("mock_feed.stop")


# ─── Redis Tick Writer ────────────────────────────────────────────────────────

class TickRedisWriter:
    """
    Writes ticks to Redis using a pipeline — one round-trip per batch.
    Key: market:tick:{symbol}  TTL: 60s
    Also maintains a sorted set of latest prices for fast bulk reads.
    """

    async def write_batch(self, ticks: list[Tick]) -> None:
        if not ticks:
            return
        try:
            redis = get_redis()
            async with redis.pipeline(transaction=False) as pipe:
                for tick in ticks:
                    data = json.dumps({
                        "lp":  tick.last_price,
                        "vol": tick.volume,
                        "chg": tick.change,
                        "ts":  tick.timestamp.isoformat(),
                        "o":   tick.open,
                        "h":   tick.high,
                        "l":   tick.low,
                        "c":   tick.close,
                        "bq":  tick.buy_quantity,
                        "sq":  tick.sell_quantity,
                    })
                    pipe.setex(f"market:tick:{tick.trading_symbol}", 60, data)
                    pipe.zadd("market:prices", {tick.trading_symbol: tick.last_price})
                await pipe.execute()
        except Exception as e:
            log.warning("redis_writer.batch_failed", count=len(ticks), error=str(e))


# ─── Main Feed Manager ────────────────────────────────────────────────────────

class FeedManager:
    """
    Orchestrates the feed, candle aggregator, and Redis writer.
    In LIVE/PAPER mode: uses ZerodhaFeed.
    In DEVELOPMENT mode: uses MockFeed.

    Ticks are buffered per event-loop cycle and flushed as a single pipeline
    write — avoids spawning one Redis connection per tick symbol.
    """

    def __init__(self):
        self._redis_writer = TickRedisWriter()
        self._candle_aggregator = CandleAggregator(self._on_candle_complete)
        self._candle_callbacks: list[Callable[[OHLCVCandle], None]] = []
        self._tick_batch: list[Tick] = []
        self._flush_scheduled: bool = False
        self._total_ticks: int = 0

        if settings.uses_simulated_broker:   # dev + paper → no Kite token needed
            self._feed = MockFeed(self._on_tick)
        else:
            self._feed = ZerodhaFeed(self._on_tick)

    def add_candle_listener(self, callback: Callable[[OHLCVCandle], None]) -> None:
        """Register a callback to receive completed OHLCV candles."""
        self._candle_callbacks.append(callback)

    def _on_tick(self, tick: Tick) -> None:
        """Buffer tick; schedule a single batch flush at end of this event-loop turn."""
        self._tick_batch.append(tick)
        self._candle_aggregator.process_tick(tick)
        self._total_ticks += 1

        # Print every 500 ticks (~10s at 50 symbols/sec) so we know the feed is alive
        if self._total_ticks % 500 == 0:
            from datetime import datetime
            print(f"[{datetime.now().strftime('%H:%M:%S')}] feed.ticks_processed total={self._total_ticks}", flush=True)

        if not self._flush_scheduled:
            self._flush_scheduled = True
            asyncio.create_task(self._flush_ticks())

    async def _flush_ticks(self) -> None:
        """Drain the tick buffer and write to Redis in one pipeline call."""
        # yield once so all synchronous _on_tick calls in this loop turn complete
        await asyncio.sleep(0)
        batch, self._tick_batch = self._tick_batch, []
        self._flush_scheduled = False
        await self._redis_writer.write_batch(batch)

    def _on_candle_complete(self, candle: OHLCVCandle) -> None:
        """Called when a candle period closes."""
        for cb in self._candle_callbacks:
            try:
                cb(candle)
            except Exception as e:
                log.warning("feed_manager.candle_callback_error", error=str(e))

    async def start(self) -> None:
        await self._feed.start()
        log.info("feed_manager.started", env=settings.app_env.value)

    async def stop(self) -> None:
        self._feed.stop()
        log.info("feed_manager.stopped")
