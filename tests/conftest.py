"""
tests/conftest.py
──────────────────
Shared pytest fixtures.

Unit tests (charges, risk math, lifecycle math) run without a DB or Redis —
they test pure functions directly. No fixtures needed for those.

Add integration fixtures here when end-to-end tests are introduced.
"""
import pytest


# ── Shared test data ──────────────────────────────────────────────────────────

@pytest.fixture
def long_trade():
    """A representative LONG intraday trade dict (mirrors lifecycle load format)."""
    return {
        "id": "00000000-0000-0000-0000-000000000001",
        "trading_symbol": "RELIANCE",
        "direction": "LONG",
        "entry_price": "1000.0000",
        "entry_quantity": 10,
        "planned_stop_loss": "970.0000",
        "planned_target_1": "1060.0000",
    }


@pytest.fixture
def short_trade():
    """A representative SHORT intraday trade dict."""
    return {
        "id": "00000000-0000-0000-0000-000000000002",
        "trading_symbol": "TCS",
        "direction": "SHORT",
        "entry_price": "3500.0000",
        "entry_quantity": 5,
        "planned_stop_loss": "3545.0000",
        "planned_target_1": "3410.0000",
    }
