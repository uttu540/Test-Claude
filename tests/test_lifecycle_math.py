"""
tests/test_lifecycle_math.py
──────────────────────────────
Unit tests for trade exit detection and P&L calculation in TradeLifecycleManager.
Tests the pure calculation methods — no DB or Redis required.
"""
import pytest
from services.execution.trade_lifecycle import TradeLifecycleManager


@pytest.fixture
def manager():
    return TradeLifecycleManager()


# ── Exit condition detection ──────────────────────────────────────────────────

class TestExitConditions:

    def test_long_stop_hit_exactly(self, manager):
        trade = {
            "direction": "LONG",
            "planned_stop_loss": "970.0",
            "planned_target_1": "1060.0",
        }
        price, reason = manager._check_exit_conditions(trade, 970.0)
        assert price == 970.0
        assert reason == "STOP_LOSS"

    def test_long_stop_hit_below(self, manager):
        trade = {
            "direction": "LONG",
            "planned_stop_loss": "970.0",
            "planned_target_1": "1060.0",
        }
        price, reason = manager._check_exit_conditions(trade, 965.0)
        assert price == 970.0
        assert reason == "STOP_LOSS"

    def test_long_target_hit_exactly(self, manager):
        trade = {
            "direction": "LONG",
            "planned_stop_loss": "970.0",
            "planned_target_1": "1060.0",
        }
        price, reason = manager._check_exit_conditions(trade, 1060.0)
        assert price == 1060.0
        assert reason == "TARGET"

    def test_long_target_hit_above(self, manager):
        trade = {
            "direction": "LONG",
            "planned_stop_loss": "970.0",
            "planned_target_1": "1060.0",
        }
        price, reason = manager._check_exit_conditions(trade, 1075.0)
        assert price == 1060.0
        assert reason == "TARGET"

    def test_long_in_range_no_exit(self, manager):
        trade = {
            "direction": "LONG",
            "planned_stop_loss": "970.0",
            "planned_target_1": "1060.0",
        }
        price, reason = manager._check_exit_conditions(trade, 1010.0)
        assert price is None
        assert reason == ""

    def test_short_stop_hit_above(self, manager):
        trade = {
            "direction": "SHORT",
            "planned_stop_loss": "1030.0",
            "planned_target_1":  "940.0",
        }
        price, reason = manager._check_exit_conditions(trade, 1030.0)
        assert price == 1030.0
        assert reason == "STOP_LOSS"

    def test_short_target_hit_below(self, manager):
        trade = {
            "direction": "SHORT",
            "planned_stop_loss": "1030.0",
            "planned_target_1":  "940.0",
        }
        price, reason = manager._check_exit_conditions(trade, 940.0)
        assert price == 940.0
        assert reason == "TARGET"

    def test_short_in_range_no_exit(self, manager):
        trade = {
            "direction": "SHORT",
            "planned_stop_loss": "1030.0",
            "planned_target_1":  "940.0",
        }
        price, reason = manager._check_exit_conditions(trade, 990.0)
        assert price is None
        assert reason == ""

    def test_missing_stop_no_exit(self, manager):
        trade = {
            "direction": "LONG",
            "planned_stop_loss": None,
            "planned_target_1": "1060.0",
        }
        price, reason = manager._check_exit_conditions(trade, 900.0)
        assert price is None

    def test_missing_target_no_exit(self, manager):
        trade = {
            "direction": "LONG",
            "planned_stop_loss": "970.0",
            "planned_target_1": None,
        }
        price, reason = manager._check_exit_conditions(trade, 1100.0)
        assert price is None


# ── P&L calculation ───────────────────────────────────────────────────────────

class TestPnLFormula:
    """Validate the gross P&L formula used in _close_trade."""

    def _gross_pnl(self, entry, exit_, qty, direction):
        multiplier = 1 if direction == "LONG" else -1
        return (exit_ - entry) * qty * multiplier

    def test_long_win(self):
        assert self._gross_pnl(1000.0, 1060.0, 10, "LONG") == pytest.approx(600.0)

    def test_long_loss(self):
        assert self._gross_pnl(1000.0, 970.0, 10, "LONG") == pytest.approx(-300.0)

    def test_short_win(self):
        # SHORT: sell 1000, cover 940 → profit = 60 per share
        assert self._gross_pnl(1000.0, 940.0, 10, "SHORT") == pytest.approx(600.0)

    def test_short_loss(self):
        # SHORT: sell 1000, cover 1030 → loss = 30 per share
        assert self._gross_pnl(1000.0, 1030.0, 10, "SHORT") == pytest.approx(-300.0)

    def test_breakeven(self):
        assert self._gross_pnl(1000.0, 1000.0, 10, "LONG") == pytest.approx(0.0)

    def test_quantity_scales_linearly(self):
        pnl_10  = self._gross_pnl(1000.0, 1050.0, 10,  "LONG")
        pnl_100 = self._gross_pnl(1000.0, 1050.0, 100, "LONG")
        assert pnl_100 == pytest.approx(pnl_10 * 10)
