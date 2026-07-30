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
    """
    _check_exit_conditions looks up the trailing stop by trade["id"] (falling
    back to planned_stop_loss if no trailing stop has been set yet), so every
    trade dict here needs an "id" — reusing the long_trade/short_trade fixtures
    from conftest.py where the numbers fit naturally, and adding "id" to the
    inline dicts used for the missing-stop / missing-target edge cases.
    """

    def test_long_stop_hit_exactly(self, manager, long_trade):
        price, reason = manager._check_exit_conditions(long_trade, 970.0)
        assert price == 970.0
        assert reason == "STOP_LOSS"

    def test_long_stop_hit_below(self, manager, long_trade):
        price, reason = manager._check_exit_conditions(long_trade, 965.0)
        assert price == 970.0
        assert reason == "STOP_LOSS"

    def test_long_target_hit_exactly(self, manager, long_trade):
        price, reason = manager._check_exit_conditions(long_trade, 1060.0)
        assert price == 1060.0
        assert reason == "TARGET"

    def test_long_target_hit_above(self, manager, long_trade):
        price, reason = manager._check_exit_conditions(long_trade, 1075.0)
        assert price == 1060.0
        assert reason == "TARGET"

    def test_long_in_range_no_exit(self, manager, long_trade):
        price, reason = manager._check_exit_conditions(long_trade, 1010.0)
        assert price is None
        assert reason == ""

    def test_short_stop_hit_above(self, manager, short_trade):
        # short_trade: entry=3500, stop=3545, target=3410
        price, reason = manager._check_exit_conditions(short_trade, 3545.0)
        assert price == 3545.0
        assert reason == "STOP_LOSS"

    def test_short_target_hit_below(self, manager, short_trade):
        price, reason = manager._check_exit_conditions(short_trade, 3410.0)
        assert price == 3410.0
        assert reason == "TARGET"

    def test_short_in_range_no_exit(self, manager, short_trade):
        price, reason = manager._check_exit_conditions(short_trade, 3480.0)
        assert price is None
        assert reason == ""

    def test_missing_stop_no_exit(self, manager):
        trade = {
            "id": "00000000-0000-0000-0000-0000000000f1",
            "direction": "LONG",
            "planned_stop_loss": None,
            "planned_target_1": "1060.0",
        }
        price, reason = manager._check_exit_conditions(trade, 900.0)
        assert price is None

    def test_missing_target_no_exit(self, manager):
        trade = {
            "id": "00000000-0000-0000-0000-0000000000f2",
            "direction": "LONG",
            "planned_stop_loss": "970.0",
            "planned_target_1": None,
        }
        price, reason = manager._check_exit_conditions(trade, 1100.0)
        assert price is None

    # ── Trailing-stop branch (previously untested) ─────────────────────────────

    def test_uses_trailing_stop_when_set(self, manager, long_trade):
        # Trailing stop has ratcheted from the original 970 up to 1000
        # (breakeven) — exit detection must use the trailing level, not the
        # original planned_stop_loss.
        manager._trailing_stops[str(long_trade["id"])] = 1000.0
        price, reason = manager._check_exit_conditions(long_trade, 1000.0)
        assert price == 1000.0
        assert reason == "STOP_LOSS"

    def test_trailing_stop_does_not_affect_target(self, manager, long_trade):
        manager._trailing_stops[str(long_trade["id"])] = 1000.0
        price, reason = manager._check_exit_conditions(long_trade, 1060.0)
        assert price == 1060.0
        assert reason == "TARGET"

    def test_falls_back_to_planned_stop_when_no_trailing_stop_set(self, manager, long_trade):
        # No entry in self._trailing_stops for this trade id yet.
        assert str(long_trade["id"]) not in manager._trailing_stops
        price, reason = manager._check_exit_conditions(long_trade, 970.0)
        assert price == 970.0
        assert reason == "STOP_LOSS"


# ── Trailing-stop milestone updates (previously untested) ─────────────────────

class TestTrailingStopUpdate:
    """
    Coverage for TradeLifecycleManager._update_trailing_stop — the milestone-
    based ratchet that _check_exit_conditions (and the live tick/broker poll
    loops) depend on. INTRADAY trails first at 2R (breakeven), then 3R (+1R),
    then 5R (+2R). SWING trails first at 4R (+1R). The stop only ever moves in
    the trade's favour.

    `is_swing` is derived from the persisted `strategy_mode` column
    (INTRADAY / SWING / POSITIONAL), which _load_open_trades() selects and
    trade_executor sets at entry. A missing/NULL strategy_mode is treated as
    INTRADAY, matching that query's intraday filter.
    """

    # long_trade: entry=1000, stop=970 → initial_risk=30

    def test_long_intraday_below_2r_no_trail(self, manager, long_trade):
        trade = {**long_trade, "strategy_mode": "INTRADAY"}
        trade_id = str(trade["id"])
        manager._update_trailing_stop(trade_id, trade, 1015.0)  # 0.5R
        assert manager._trailing_stops[trade_id] == pytest.approx(970.0)

    def test_long_intraday_breakeven_at_2r(self, manager, long_trade):
        trade = {**long_trade, "strategy_mode": "INTRADAY"}
        trade_id = str(trade["id"])
        manager._update_trailing_stop(trade_id, trade, 1060.0)  # 2R
        assert manager._trailing_stops[trade_id] == pytest.approx(1000.0)

    def test_long_intraday_plus_1r_at_3r(self, manager, long_trade):
        trade = {**long_trade, "strategy_mode": "INTRADAY"}
        trade_id = str(trade["id"])
        manager._update_trailing_stop(trade_id, trade, 1090.0)  # 3R
        assert manager._trailing_stops[trade_id] == pytest.approx(1030.0)

    def test_long_intraday_plus_2r_at_5r(self, manager, long_trade):
        trade = {**long_trade, "strategy_mode": "INTRADAY"}
        trade_id = str(trade["id"])
        manager._update_trailing_stop(trade_id, trade, 1150.0)  # 5R
        assert manager._trailing_stops[trade_id] == pytest.approx(1060.0)

    def test_long_trail_never_moves_backward(self, manager, long_trade):
        trade = {**long_trade, "strategy_mode": "INTRADAY"}
        trade_id = str(trade["id"])
        manager._update_trailing_stop(trade_id, trade, 1090.0)  # 3R → trail 1030
        manager._update_trailing_stop(trade_id, trade, 1015.0)  # pulls back to 0.5R
        assert manager._trailing_stops[trade_id] == pytest.approx(1030.0)

    # short_trade: entry=3500, stop=3545 → initial_risk=45

    def test_short_intraday_breakeven_at_2r(self, manager, short_trade):
        trade = {**short_trade, "strategy_mode": "INTRADAY"}
        trade_id = str(trade["id"])
        manager._update_trailing_stop(trade_id, trade, 3410.0)  # 2R below entry
        assert manager._trailing_stops[trade_id] == pytest.approx(3500.0)

    def test_short_trail_never_moves_backward(self, manager, short_trade):
        trade = {**short_trade, "strategy_mode": "INTRADAY"}
        trade_id = str(trade["id"])
        manager._update_trailing_stop(trade_id, trade, 3410.0)  # 2R → trail 3500
        manager._update_trailing_stop(trade_id, trade, 3480.0)  # pulls back
        assert manager._trailing_stops[trade_id] == pytest.approx(3500.0)

    # Regression guard: _update_trailing_stop used to read a "timeframe" key that
    # the trades table has no column for, so trade.get("timeframe", "1day") always
    # hit the default and every trade was treated as SWING — the INTRADAY
    # milestones above were unreachable in production. It now reads strategy_mode,
    # and a missing/NULL value falls back to INTRADAY (not SWING).

    def test_missing_strategy_mode_defaults_to_intraday_milestones(self, manager, long_trade):
        assert "strategy_mode" not in long_trade
        trade_id = str(long_trade["id"])
        manager._update_trailing_stop(trade_id, long_trade, 1090.0)  # 3R
        # INTRADAY: 3R → +1R = 1030. Under the old bug this stayed at 970.
        assert manager._trailing_stops[trade_id] == pytest.approx(1030.0)

    def test_stale_timeframe_key_does_not_drive_branch(self, manager, long_trade):
        # A leftover "timeframe" key must not resurrect the old behaviour.
        trade = {**long_trade, "timeframe": "1day"}
        trade_id = str(trade["id"])
        manager._update_trailing_stop(trade_id, trade, 1090.0)  # 3R
        assert manager._trailing_stops[trade_id] == pytest.approx(1030.0)

    # Swing (strategy_mode SWING) trails first at 4R, not 2R.

    def test_swing_no_trail_below_4r(self, manager):
        trade = {
            "id": "swing-no-trail",
            "direction": "LONG",
            "entry_price": "1000.0",
            "planned_stop_loss": "970.0",
            "strategy_mode": "SWING",
        }
        manager._update_trailing_stop(trade["id"], trade, 1090.0)  # 3R — below 4R swing floor
        assert manager._trailing_stops[trade["id"]] == pytest.approx(970.0)

    def test_swing_trails_at_4r(self, manager):
        trade = {
            "id": "swing-trails",
            "direction": "LONG",
            "entry_price": "1000.0",
            "planned_stop_loss": "970.0",
            "strategy_mode": "SWING",
        }
        manager._update_trailing_stop(trade["id"], trade, 1120.0)  # 4R → trail = +1R
        assert manager._trailing_stops[trade["id"]] == pytest.approx(1030.0)

    def test_swing_short_trails_at_4r(self, manager):
        trade = {
            "id": "swing-short-trails",
            "direction": "SHORT",
            "entry_price": "1000.0",
            "planned_stop_loss": "1030.0",
            "strategy_mode": "SWING",
        }
        manager._update_trailing_stop(trade["id"], trade, 880.0)  # 4R below entry → trail = -1R
        assert manager._trailing_stops[trade["id"]] == pytest.approx(970.0)


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
