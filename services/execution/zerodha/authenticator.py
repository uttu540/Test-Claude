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


def _ttl_until_midnight_ist() -> int:
    """Zerodha tokens expire at midnight IST regardless of when they were issued.
    Return seconds remaining until then, minus a 60s safety buffer."""
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    now = datetime.now(IST)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    secs = int((midnight - now).total_seconds()) - 60
    return max(secs, 300)   # at least 5 min in edge cases


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

        TOTP strategies tried in order (Kite UI changes periodically):
          1. input[placeholder*="TOTP/OTP/authenticator"] — new Kite single-field UI
          2. input[autocomplete="one-time-code"]
          3. Individual input[type="number"] digit boxes — old Kite UI
          4. Any visible text/tel/number input as last resort

        After filling TOTP, waits for redirect URL containing request_token
        rather than sleeping blindly. Falls back to clicking submit if no
        auto-submit (new UI auto-submits on 6th digit; old UI requires click).
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError("Playwright not installed. Run: playwright install chromium")

        login_url = self._kite.login_url()
        log.info("zerodha_auth.browser_login", url=login_url)

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            current_url = login_url   # fallback if exception fires before page.url is read
            try:
                page = await browser.new_page()

                # Listen for all network requests to capture request_token.
                # Kite redirects to redirect_url (127.0.0.1) after auth — browser errors,
                # but the request event fires before the error, giving us the token URL.
                _captured_token: list[str] = []

                def _on_request(request):
                    token = self._extract_request_token(request.url)
                    if token:
                        _captured_token.append(token)

                page.on("request", _on_request)

                # ── Step 1: Load login page ───────────────────────────────────
                # Use domcontentloaded — Kite SPA has background polling that
                # prevents networkidle from ever resolving cleanly.
                await page.goto(login_url, timeout=30_000)
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                await page.wait_for_timeout(1000)

                # ── Step 2: Fill user ID + password ──────────────────────────
                await page.fill('input[type="text"]', settings.kite_user_id, timeout=10_000)
                await page.fill('input[type="password"]', settings.kite_password, timeout=10_000)
                await page.click('button[type="submit"]', timeout=10_000)
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                await page.wait_for_timeout(1500)

                # ── Step 3: Fill TOTP ─────────────────────────────────────────
                # NOTE: use keyboard.type() not fill() — Kite SPA counts per-keystroke
                # input events and auto-submits on the 6th digit. fill() sets the value
                # in bulk (single input event), so auto-submit never fires.
                totp_filled = False

                # Strategy A: single input field — try known selectors in priority order.
                # Kite 2024+ TOTP input is type="tel" with no TOTP-related placeholder.
                # Regenerate TOTP right before each click so code is never stale.
                for sel in [
                    'input[placeholder*="TOTP" i]',
                    'input[placeholder*="totp" i]',
                    'input[placeholder*="OTP" i]',
                    'input[placeholder*="authenticator" i]',
                    'input[autocomplete="one-time-code"]',
                    'input[type="tel"]',       # Kite 2024+ TOTP field (no placeholder)
                    'input[type="number"]:not([step])',  # single-box number input
                ]:
                    try:
                        loc = page.locator(sel).first
                        if await loc.count() > 0 and await loc.is_visible(timeout=2_000):
                            await loc.click(timeout=3_000)
                            totp_code = pyotp.TOTP(settings.kite_totp_secret).now()
                            await page.keyboard.type(totp_code, delay=50)
                            totp_filled = True
                            log.info("zerodha_auth.totp_filled", strategy=sel)
                            break
                    except Exception:
                        pass

                # Strategy B: individual digit boxes (old Kite UI)
                if not totp_filled:
                    try:
                        digit_inputs = page.locator('input[type="number"]')
                        count = await digit_inputs.count()
                        if count >= 6:
                            totp_code = pyotp.TOTP(settings.kite_totp_secret).now()
                            for i, digit in enumerate(totp_code):
                                await digit_inputs.nth(i).fill(digit, timeout=3_000)
                            totp_filled = True
                            log.info("zerodha_auth.totp_filled", strategy="digit_boxes")
                    except Exception:
                        pass

                # Strategy C: any single visible text/tel/number input (last resort)
                if not totp_filled:
                    try:
                        fallback = page.locator(
                            'input[type="text"]:visible, input[type="tel"]:visible, '
                            'input[type="number"]:visible'
                        ).first
                        if await fallback.count() > 0:
                            await fallback.click(timeout=3_000)
                            totp_code = pyotp.TOTP(settings.kite_totp_secret).now()
                            await page.keyboard.type(totp_code, delay=50)
                            totp_filled = True
                            log.info("zerodha_auth.totp_filled", strategy="visible_fallback")
                    except Exception:
                        pass

                if not totp_filled:
                    log.warning("zerodha_auth.totp_fill_skipped",
                                url=page.url, msg="No TOTP input found — page may have changed")

                # ── Step 4: Wait for redirect containing request_token ────────
                # New Kite UI auto-submits on 6th TOTP digit (keyboard.type triggers this).
                # Old Kite UI requires explicit submit click.
                try:
                    await page.wait_for_url("**/request_token=**", timeout=6_000)
                except Exception:
                    # Auto-submit didn't fire — try multiple button text variants
                    try:
                        submit_btn = page.locator(
                            'button[type="submit"], '
                            'button:has-text("Continue"), button:has-text("Verify"), '
                            'button:has-text("Proceed"), button:has-text("Submit")'
                        ).first
                        if await submit_btn.count() > 0:
                            await submit_btn.click(timeout=5_000)
                        await page.wait_for_url("**/request_token=**", timeout=8_000)
                    except Exception:
                        # URL may be 127.0.0.1 (browser errors but token captured via request event)
                        await page.wait_for_timeout(3_000)

                # ── Step 5: Authorize page (first-time / consent) ────────────
                if "connect/authorize" in page.url:
                    try:
                        authorize_btn = page.locator(
                            'button:has-text("Authorize"), button:has-text("Authorise"), '
                            'button:has-text("I Allow"), button:has-text("Allow")'
                        ).first
                        if await authorize_btn.count() > 0:
                            try:
                                async with page.expect_navigation(timeout=10_000):
                                    await authorize_btn.click(timeout=10_000)
                            except Exception:
                                pass
                            await page.wait_for_timeout(2000)
                    except Exception as _e:
                        log.warning("zerodha_auth.authorize_click_failed", error=str(_e))

                request_token = _captured_token[0] if _captured_token else None
                current_url = page.url
                if not request_token:
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
        ttl = _ttl_until_midnight_ist()
        await redis.setex(REDIS_ACCESS_TOKEN_KEY, ttl, access_token)
        log.info("zerodha_auth.token_stored", ttl_secs=ttl, expires_at="midnight_IST")

    # ── Instrument Token Cache ────────────────────────────────────────────────

    async def _fetch_and_cache_instruments(self, access_token: str) -> None:
        """
        Fetch all NSE instrument tokens from Kite and cache in Redis.
        This maps trading_symbol → instrument_token (needed for WebSocket subscriptions).
        """
        from services.data_ingestion.nifty500_instruments import INDEX_INSTRUMENTS, get_live_universe

        self._kite.set_access_token(access_token)
        instruments = self._kite.instruments("NSE")

        # Build symbol → token map
        token_map: dict[str, int] = {i["tradingsymbol"]: i["instrument_token"] for i in instruments}

        # Use full NSE EQ universe (1700–2200 stocks); falls back to Nifty500 if fetch fails
        universe_symbols = get_live_universe()
        universe_tokens  = [token_map[sym] for sym in universe_symbols if sym in token_map]

        # Add index tokens
        index_tokens = [token for _, _, token in INDEX_INSTRUMENTS]
        all_tokens   = list(set(universe_tokens + index_tokens))

        redis = get_redis()
        ttl   = _ttl_until_midnight_ist()
        await redis.setex(REDIS_TOKEN_MAP_KEY,   ttl, json.dumps(token_map))
        await redis.setex(REDIS_INSTRUMENTS_KEY, ttl, json.dumps(all_tokens))

        missing = [sym for sym in universe_symbols if sym not in token_map]
        if missing:
            log.warning("zerodha_auth.missing_tokens", count=len(missing), symbols=missing[:20])

        log.info(
            "zerodha_auth.instruments_cached",
            universe=len(universe_symbols),
            subscribed=len(universe_tokens),
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
