"""
tests/test_charges.py
──────────────────────
Unit tests for Zerodha intraday equity charge calculations.

All expected values verified against https://zerodha.com/charges/
"""
import pytest
from services.execution.charges import ChargeBreakdown, calculate_intraday_charges


# ── Helpers ───────────────────────────────────────────────────────────────────

def approx(value, rel=1e-3):
    """pytest.approx with a 0.1% relative tolerance (paise-level precision)."""
    return pytest.approx(value, rel=rel)


# ── LONG trade ────────────────────────────────────────────────────────────────

class TestLongTrade:
    """Buy 10 × ₹1,000 entry, sell 10 × ₹1,050 exit."""

    def setup_method(self):
        self.result = calculate_intraday_charges(
            entry_price=1000.0, exit_price=1050.0, quantity=10, direction="LONG"
        )

    def test_returns_charge_breakdown(self):
        assert isinstance(self.result, ChargeBreakdown)

    def test_brokerage(self):
        # buy side:  min(20, 10000 * 0.0003) = min(20, 3.0) = 3.00
        # sell side: min(20, 10500 * 0.0003) = min(20, 3.15) = 3.15
        assert self.result.brokerage == approx(6.15)

    def test_stt(self):
        # STT on sell only: 10500 * 0.00025 = 2.625
        assert self.result.stt == approx(2.625)

    def test_exchange_charges(self):
        # NSE: (10000 + 10500) * 0.0000345 = 0.70725
        assert self.result.exchange_charges == approx(0.707, rel=1e-2)

    def test_sebi_charges(self):
        # 20500 * 0.000001 = 0.0205
        assert self.result.sebi_charges == approx(0.0205)

    def test_stamp_duty(self):
        # Buy side only: 10000 * 0.00003 = 0.30
        assert self.result.stamp_duty == approx(0.30)

    def test_gst(self):
        # 18% on (brokerage + exchange + SEBI)
        base = 6.15 + 0.70725 + 0.0205
        expected = base * 0.18
        assert self.result.gst == approx(expected, rel=1e-2)

    def test_total_positive(self):
        assert self.result.total > 0

    def test_total_less_than_one_pct_of_turnover(self):
        # Charges should be < 1% of turnover for a normal trade
        turnover = 10000 + 10500
        assert self.result.total < turnover * 0.01

    def test_total_equals_sum_of_components(self):
        components = (
            self.result.brokerage
            + self.result.stt
            + self.result.exchange_charges
            + self.result.sebi_charges
            + self.result.gst
            + self.result.stamp_duty
        )
        assert self.result.total == approx(components)


# ── SHORT trade ───────────────────────────────────────────────────────────────

class TestShortTrade:
    """Short 10 × ₹1,000 entry, cover 10 × ₹950 exit (₹500 profit before charges)."""

    def setup_method(self):
        self.result = calculate_intraday_charges(
            entry_price=1000.0, exit_price=950.0, quantity=10, direction="SHORT"
        )

    def test_returns_charge_breakdown(self):
        assert isinstance(self.result, ChargeBreakdown)

    def test_stt_on_initial_sell(self):
        # SHORT: sell_value = entry * qty = 1000 * 10 = 10000
        # STT: 10000 * 0.00025 = 2.50
        assert self.result.stt == approx(2.50)

    def test_stamp_duty_on_cover_buy(self):
        # SHORT: buy_value = exit * qty = 950 * 10 = 9500
        # stamp: 9500 * 0.00003 = 0.285
        assert self.result.stamp_duty == approx(0.285)

    def test_total_positive(self):
        assert self.result.total > 0

    def test_total_equals_sum_of_components(self):
        components = (
            self.result.brokerage
            + self.result.stt
            + self.result.exchange_charges
            + self.result.sebi_charges
            + self.result.gst
            + self.result.stamp_duty
        )
        assert self.result.total == approx(components)


# ── Edge cases ────────────────────────────────────────────────────────────────

class TestEdgeCases:

    def test_brokerage_capped_at_20_per_side(self):
        # Very large trade: 1000 shares @ ₹5000 → buy_value = ₹50,00,000
        # 0.03% of 5,000,000 = 1,500 → capped at ₹20 per side
        result = calculate_intraday_charges(5000.0, 5100.0, 1000, "LONG")
        assert result.brokerage == approx(40.0)   # 20 + 20

    def test_tiny_trade_brokerage_not_capped(self):
        # Small trade: 1 share @ ₹100 → buy_value = ₹100
        # 0.03% of 100 = 0.03 → not capped at 20
        result = calculate_intraday_charges(100.0, 105.0, 1, "LONG")
        assert result.brokerage < 1.0

    def test_breakeven_trade_still_has_charges(self):
        # Buy and sell at same price — still incurs STT, exchange, brokerage
        result = calculate_intraday_charges(1000.0, 1000.0, 10, "LONG")
        assert result.total > 0
        assert result.stt > 0    # STT always charged
        assert result.gst > 0    # GST on brokerage

    def test_direction_affects_stt(self):
        # LONG: STT on sell (exit), SHORT: STT on sell (entry)
        # Both should produce STT > 0
        long_r  = calculate_intraday_charges(1000.0, 1050.0, 10, "LONG")
        short_r = calculate_intraday_charges(1000.0, 950.0,  10, "SHORT")
        assert long_r.stt  > 0
        assert short_r.stt > 0

    def test_long_short_symmetry_on_same_price(self):
        # LONG entry=1000 exit=1050 vs SHORT entry=1050 exit=1000
        # Turnover is the same, charges should be approximately equal
        long_r  = calculate_intraday_charges(1000.0, 1050.0, 10, "LONG")
        short_r = calculate_intraday_charges(1050.0, 1000.0, 10, "SHORT")
        assert long_r.total == approx(short_r.total, rel=1e-3)
