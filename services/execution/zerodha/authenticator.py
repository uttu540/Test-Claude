"""
services/execution/zerodha/authenticator.py
────────────────────────────────────────────
Automates Zerodha's daily TOTP-based login.

Zerodha requires a fresh access token every day (SEBI mandate).
This authenticator uses Playwright to drive the browser login flow and
pyotp to generate the TOTP automatically.

The resulting access_token is stored in Redis with a 24h TTL.
A scheduler job calls this at 8:30 AM IST every weekday.
"""
from __future__ import annotations

import json
import time
from datetime import datetime

import pyotp
import structlog
from kiteconnect import KiteConnect
from tenacity import retry, stop_after_attempt, wait_fixed

from config.settings import settings
from database.connection import get_redis
from services.notifications.telegram_bot import get_notifier

log = structlog.get_logger(__name__)

REDIS_ACCESS_TOKEN_KEY   = "kite:access_token"
REDIS_TOKEN_MAP_KEY      = "kite:token_map"       # symbol → instrument_token
REDIS_INSTRUMENTS_KEY    = "kite:instrument_tokens"
TOKEN_TTL_SECONDS        = 86_400                  # 24 hours


class ZerodhaAuthenticator:
    """
    Handles daily Zerodha authentication.
    Requires KITE_API_KEY, KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET in .env
    """

    def __init__(self):
        if not settings.kite_api_key:
            raise RuntimeError(
                "KITE_API_KEY not set. Get your API key from https://kite.trade"
            )
        self._kite = KiteConnect(api_key=settings.kite_api_key)

    async def authenticate(self) -> str:
        """
        Full login flow → returns access_token.
        Also stores it in Redis and fetches instrument tokens.
        """
        log.info("zerodha_auth.start", user_id=settings.kite_user_id)

        request_token = await self._get_request_token_via_playwright()
        access_token  = self._exchange_for_access_token(request_token)
        await self._store_in_redis(access_token)
        await self._fetch_and_cache_instruments(access_token)

        notifier = get_notifier()
        await notifier.send(
            f"Zerodha authenticated ✅ | Token valid until midnight",
        )
        log.info("zerodha_auth.complete", token_prefix=access_token[:8] + "...")
        return access_token

    async def get_stored_token(self) -> str | None:
        """Return token from Redis if it exists and hasn't expired."""
        redis = get_redis()
        return await redis.get(REDIS_ACCESS_TOKEN_KEY)

    # ── Playwright Browser Login ──────────────────────────────────────────────

    async def _get_request_token_via_playwright(self) -> str:
        """
        Uses a headless Chromium browser to log into Zerodha's OAuth page
        and extract the request_token from the redirect URL.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError("Playwright not installed. Run: playwright install chromium")

        login_url = self._kite.login_url()
        log.info("zerodha_auth.browser_login", url=login_url)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()

                # Go to Kite login page
                await page.goto(login_url, timeout=30_000)
                await page.wait_for_load_state("networkidle", timeout=15_000)

                # Enter user ID and password
                await page.fill('input[type="text"]', settings.kite_user_id, timeout=10_000)
                await page.fill('input[type="password"]', settings.kite_password, timeout=10_000)
                await page.click('button[type="submit"]', timeout=10_000)
                await page.wait_for_load_state("networkidle", timeout=15_000)
                await page.wait_for_timeout(2000)

                # Handle TOTP page
                totp_code = pyotp.TOTP(settings.kite_totp_secret).now()
                log.debug("zerodha_auth.totp_generated", totp_generated=True)  # Never log the code itself

                # Zerodha's TOTP input is a series of individual digit boxes
                # Try filling the combined input first, then individual boxes
                try:
                    totp_input = page.locator('input[label="External TOTP"]').first
                    if await totp_input.count() > 0:
                        await totp_input.fill(totp_code, timeout=10_000)
                    else:
                        # Individual digit inputs
                        inputs = page.locator('input[type="number"]')
                        for i, digit in enumerate(totp_code):
                            await inputs.nth(i).fill(digit, timeout=5_000)
                except Exception:
                    # Fallback: fill whatever input is visible
                    await page.fill('input[type="number"]', totp_code, timeout=10_000)

                await page.wait_for_timeout(3000)

                # Wait for redirect to our redirect URL containing request_token
                current_url = page.url
                request_token = self._extract_request_token(current_url)

                if not request_token:
                    # Sometimes takes a moment
                    await page.wait_for_timeout(2000)
                    current_url   = page.url
                    request_token = self._extract_request_token(current_url)

            except Exception as e:
                log.error("zerodha_auth.browser_error", error=str(e), cleaning_up=True)
                raise
            finally:
                await browser.close()

        if not request_token:
            raise RuntimeError(
                f"Could not extract request_token from URL: {current_url[:100]}"
            )

        log.info("zerodha_auth.request_token_obtained")
        return request_token

    @staticmethod
    def _extract_request_token(url: str) -> str | None:
        """Parse request_token from Kite's OAuth redirect URL."""
        if "request_token=" not in url:
            return None
        try:
            token_part = url.split("request_token=")[1]
            return token_part.split("&")[0]
        except (IndexError, AttributeError):
            return None

    # ── Token Exchange ────────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
    def _exchange_for_access_token(self, request_token: str) -> str:
        """Exchange request_token for access_token via Kite API."""
        data = self._kite.generate_session(
            request_token, api_secret=settings.kite_api_secret
        )
        return data["access_token"]

    # ── Redis Storage ─────────────────────────────────────────────────────────

    async def _store_in_redis(self, access_token: str) -> None:
        redis = get_redis()
        await redis.setex(REDIS_ACCESS_TOKEN_KEY, TOKEN_TTL_SECONDS, access_token)
        log.info("zerodha_auth.token_stored", ttl_hours=TOKEN_TTL_SECONDS // 3600)

    # ── Instrument Token Cache ────────────────────────────────────────────────

    async def _fetch_and_cache_instruments(self, access_token: str) -> None:
        """
        Fetch all NSE instrument tokens from Kite and cache in Redis.
        This maps trading_symbol → instrument_token (needed for WebSocket subscriptions).
        """
        from services.data_ingestion.nifty50_instruments import NIFTY50, INDEX_INSTRUMENTS

        self._kite.set_access_token(access_token)
        instruments = self._kite.instruments("NSE")

        # Build symbol → token map
        token_map: dict[str, int] = {i["tradingsymbol"]: i["instrument_token"] for i in instruments}

        # Filter to Nifty 50
        nifty50_symbols = [sym for sym, _, _ in NIFTY50]
        nifty50_tokens  = [token_map[sym] for sym in nifty50_symbols if sym in token_map]

        # Add index tokens
        index_tokens = [token for _, _, token in INDEX_INSTRUMENTS]
        all_tokens   = list(set(nifty50_tokens + index_tokens))

        redis = get_redis()
        await redis.setex(REDIS_TOKEN_MAP_KEY,      TOKEN_TTL_SECONDS, json.dumps(token_map))
        await redis.setex(REDIS_INSTRUMENTS_KEY,    TOKEN_TTL_SECONDS, json.dumps(all_tokens))

        missing = [sym for sym in nifty50_symbols if sym not in token_map]
        if missing:
            log.warning("zerodha_auth.missing_tokens", symbols=missing)

        log.info(
            "zerodha_auth.instruments_cached",
            nifty50=len(nifty50_tokens),
            total=len(all_tokens),
        )


# ─── CLI helper ──────────────────────────────────────────────────────────────

async def run_auth() -> None:
    """Standalone authentication runner. Called by scheduler at 8:30 AM IST."""
    auth = ZerodhaAuthenticator()
    try:
        token = await auth.authenticate()
        print(f"✅ Authentication successful. Token: {token[:8]}...")
    except Exception as e:
        notifier = get_notifier()
        await notifier.system_error("ZerodhaAuthenticator", str(e))
        log.error("zerodha_auth.failed", error=str(e))
        raise


if __name__ == "__main__":
    import asyncio
    asyncio.run(run_auth())
