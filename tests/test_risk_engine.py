"""
tests/test_risk_engine.py
──────────────────────────
Unit tests for RiskEngine position sizing and stop/target formulas.

Stop/target math is exercised as a pure formula (fast, no I/O). Position
sizing goes through the REAL RiskEngine.evaluate()/_evaluate_locked() path —
its DB-backed gating checks are monkeypatched to safe/neutral values so no
Postgres/Redis is required — so these tests fail loudly if production sizing,
gating, or the ATR_STOP_MULTIPLIER/RR_RATIO constants ever drift.
"""
import pytest

from config.settings import settings
from services.risk_engine.engine import RiskEngine


# ── Constants derived FROM the class — never hand-copy these ─────────────────
# (test_basic_sizing used to hardcode 66, the answer for a stale 1.5x
# multiplier; production is 2.0x. Deriving from the class means this file
# can never drift out of sync with RiskEngine again.)
ATR_STOP_MULT = RiskEngine.ATR_STOP_MULTIPLIER
RR_RATIO      = RiskEngine.RR_RATIO


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def fixed_settings(monkeypatch):
    """
    Pin the capital/risk settings RiskEngine reads at call time to known
    values. Without this, these tests would be at the mercy of whichever
    .env happens to be present locally (not committed, and absent in CI) —
    silently changing expected quantities depending on where the suite runs.
    """
    monkeypatch.setattr(settings, "total_capital", 100_000.0)
    monkeypatch.setattr(settings, "max_risk_per_trade_pct", 2.0)     # -> max_risk_per_trade_inr = 2,000
    monkeypatch.setattr(settings, "daily_loss_limit_pct", 2.0)       # -> daily_loss_limit_inr    = 2,000
    monkeypatch.setattr(settings, "max_position_size_pct", 10.0)     # -> max_position_size_inr   = 10,000
    monkeypatch.setattr(settings, "max_open_positions", 8)
    monkeypatch.setattr(settings, "max_aggregate_notional_pct", 95.0)
    return settings


@pytest.fixture
def engine(fixed_settings, monkeypatch):
    """
    A RiskEngine with all DB-backed gating checks monkeypatched to
    safe/neutral values (no PnL loss, no open positions, no prior trade
    today), so evaluate() exercises the REAL sizing/stop/target formula and
    the REAL gating logic without needing Postgres or Redis.
    """
    eng = RiskEngine()

    async def _todays_pnl():
        return 0.0

    async def _open_count():
        return 0

    async def _open_notional():
        return 0.0

    async def _has_open_position(symbol):
        return False

    async def _has_traded_today(symbol):
        return False

    monkeypatch.setattr(eng, "_get_todays_pnl", _todays_pnl)
    monkeypatch.setattr(eng, "_get_open_count", _open_count)
    monkeypatch.setattr(eng, "_get_open_notional", _open_notional)
    monkeypatch.setattr(eng, "_has_open_position", _has_open_position)
    monkeypatch.setattr(eng, "_has_traded_today", _has_traded_today)
    return eng


# ── Stop loss and target calculation (pure formula) ──────────────────────────

class TestStopAndTarget:

    def test_long_stop_below_entry(self):
        entry, atr = 1000.0, 20.0
        stop = entry - (atr * ATR_STOP_MULT)
        assert stop == pytest.approx(960.0)

    def test_long_target_above_entry(self):
        entry, atr = 1000.0, 20.0
        target = entry + (atr * ATR_STOP_MULT * RR_RATIO)
        assert target == pytest.approx(1080.0)

    def test_short_stop_above_entry(self):
        entry, atr = 1000.0, 20.0
        stop = entry + (atr * ATR_STOP_MULT)
        assert stop == pytest.approx(1040.0)

    def test_short_target_below_entry(self):
        entry, atr = 1000.0, 20.0
        target = entry - (atr * ATR_STOP_MULT * RR_RATIO)
        assert target == pytest.approx(920.0)

    def test_rr_ratio_is_2_to_1(self):
        entry, atr = 500.0, 10.0
        stop   = entry - (atr * ATR_STOP_MULT)
        target = entry + (atr * ATR_STOP_MULT * RR_RATIO)
        risk   = abs(entry - stop)
        reward = abs(target - entry)
        assert reward / risk == pytest.approx(RR_RATIO)

    def test_high_atr_wider_stop(self):
        entry = 1000.0
        low_atr_stop  = entry - (5.0  * ATR_STOP_MULT)
        high_atr_stop = entry - (50.0 * ATR_STOP_MULT)
        assert high_atr_stop < low_atr_stop

    def test_rr_2to1_regardless_of_price(self):
        for entry, atr in [(100.0, 2.0), (5000.0, 100.0), (250.0, 5.5)]:
            stop   = entry - (atr * ATR_STOP_MULT)
            target = entry + (atr * ATR_STOP_MULT * RR_RATIO)
            risk   = abs(entry - stop)
            reward = abs(target - entry)
            assert reward / risk == pytest.approx(RR_RATIO)


# ── Position sizing — via the real RiskEngine ─────────────────────────────────

class TestPositionSizing:
    """
    Formula (services/risk_engine/engine.py, _evaluate_locked):
        risk_per_share = atr * ATR_STOP_MULTIPLIER
        qty = int(max_risk_per_trade_inr / risk_per_share)
        capped so qty * entry_price <= max_position_size_inr

    Exercised through RiskEngine.evaluate() (DB gating checks monkeypatched
    via the `engine` fixture) rather than a hand-reimplemented copy, so a
    change to the production formula or constants breaks this suite.
    """

    @pytest.mark.asyncio
    async def test_basic_sizing(self, engine):
        # entry=1000, atr=20 -> risk_per_share = 20 * 2.0 = 40
        # uncapped qty = int(2000 / 40) = 50 -> position value 50,000 > max 10,000
        # -> capped to int(10,000 / 1000) = 10
        decision = await engine.evaluate("RELIANCE", "BULLISH", entry_price=1000.0, atr=20.0)
        assert decision.approved, decision.reason
        assert decision.position_size == 10
        assert decision.stop_loss == pytest.approx(1000.0 - 20.0 * ATR_STOP_MULT)
        assert decision.target == pytest.approx(1000.0 + 20.0 * ATR_STOP_MULT * RR_RATIO)
        assert decision.risk_amount == pytest.approx(10 * 20.0 * ATR_STOP_MULT)

    @pytest.mark.asyncio
    async def test_larger_atr_means_fewer_shares(self, engine, monkeypatch):
        # Raise the position-value cap so this test isolates the
        # atr -> risk_per_share -> qty relationship. With the default fixed
        # 10,000 cap, both a small and large ATR case get capped to the same
        # qty and the comparison is meaningless (this is exactly how the old
        # test asserted `10 < 10` and passed nobody's notice).
        monkeypatch.setattr(settings, "max_position_size_pct", 1000.0)  # -> 1,000,000 cap

        small = await engine.evaluate("RELIANCE", "BULLISH", entry_price=1000.0, atr=5.0)
        large = await engine.evaluate("RELIANCE", "BULLISH", entry_price=1000.0, atr=20.0)

        assert small.approved, small.reason
        assert large.approved, large.reason
        assert large.position_size < small.position_size

    @pytest.mark.asyncio
    async def test_position_cap_applied(self, engine):
        # entry=1000, atr=2.5 -> risk_per_share=5 (within the 0.3%-5% stop band)
        # uncapped qty = int(2000/5) = 400 -> position value 400,000, far above
        # max_position_size_inr (10,000 under fixed_settings) -> capped.
        decision = await engine.evaluate("RELIANCE", "BULLISH", entry_price=1000.0, atr=2.5)
        assert decision.approved, decision.reason
        assert decision.position_size * 1000.0 <= settings.max_position_size_inr
        assert decision.position_size == int(settings.max_position_size_inr / 1000.0)

    @pytest.mark.asyncio
    async def test_risk_amount_within_budget(self, engine, monkeypatch):
        # Raise the position cap so this isolates risk-budget truncation
        # (int() division can leave actual risk slightly under budget) from
        # the position-cap behaviour covered by test_position_cap_applied.
        monkeypatch.setattr(settings, "max_position_size_pct", 1000.0)
        decision = await engine.evaluate("RELIANCE", "BULLISH", entry_price=500.0, atr=7.0)
        assert decision.approved, decision.reason
        assert decision.risk_amount <= settings.max_risk_per_trade_inr

    @pytest.mark.asyncio
    async def test_zero_atr_rejected(self, engine):
        decision = await engine.evaluate("RELIANCE", "BULLISH", entry_price=1000.0, atr=0.0)
        assert not decision.approved
        assert decision.position_size == 0
        assert "ATR is zero" in decision.reason

    @pytest.mark.asyncio
    async def test_short_direction_sizing(self, engine):
        # entry=1000, atr=20, BEARISH -> stop above entry, target below entry
        decision = await engine.evaluate("RELIANCE", "BEARISH", entry_price=1000.0, atr=20.0)
        assert decision.approved, decision.reason
        assert decision.stop_loss == pytest.approx(1000.0 + 20.0 * ATR_STOP_MULT)
        assert decision.target == pytest.approx(1000.0 - 20.0 * ATR_STOP_MULT * RR_RATIO)


# ── Gating checks — via the real RiskEngine ───────────────────────────────────

class TestRiskGates:
    """
    Exercises the pre-sizing gates in _evaluate_locked by monkeypatching a
    single DB helper away from its neutral default per test. These were
    previously untestable because TestPositionSizing never called RiskEngine
    at all.
    """

    @pytest.mark.asyncio
    async def test_daily_loss_limit_blocks_trading(self, engine, monkeypatch):
        async def _breached_pnl():
            return -settings.daily_loss_limit_inr
        monkeypatch.setattr(engine, "_get_todays_pnl", _breached_pnl)

        decision = await engine.evaluate("RELIANCE", "BULLISH", entry_price=1000.0, atr=20.0)
        assert not decision.approved
        assert "Daily loss limit" in decision.reason

    @pytest.mark.asyncio
    async def test_max_open_positions_blocks_trading(self, engine, monkeypatch):
        async def _open_count():
            return settings.max_open_positions
        monkeypatch.setattr(engine, "_get_open_count", _open_count)

        decision = await engine.evaluate("RELIANCE", "BULLISH", entry_price=1000.0, atr=20.0)
        assert not decision.approved
        assert "Max open positions" in decision.reason

    @pytest.mark.asyncio
    async def test_duplicate_position_blocks_trading(self, engine, monkeypatch):
        async def _has_open(symbol):
            return True
        monkeypatch.setattr(engine, "_has_open_position", _has_open)

        decision = await engine.evaluate("RELIANCE", "BULLISH", entry_price=1000.0, atr=20.0)
        assert not decision.approved
        assert "Already have an open position" in decision.reason

    @pytest.mark.asyncio
    async def test_already_traded_today_blocks_trading(self, engine, monkeypatch):
        async def _traded_today(symbol):
            return True
        monkeypatch.setattr(engine, "_has_traded_today", _traded_today)

        decision = await engine.evaluate("RELIANCE", "BULLISH", entry_price=1000.0, atr=20.0)
        assert not decision.approved
        assert "Already traded" in decision.reason


# ── R-multiple calculation ────────────────────────────────────────────────────

class TestRMultiple:
    """
    r_multiple = gross_pnl / (risk_per_share * quantity)
    """

    def test_target_hit_long(self):
        entry, exit_, sl, qty = 1000.0, 1060.0, 970.0, 10
        risk_per_share = abs(entry - sl)
        gross_pnl      = (exit_ - entry) * qty
        r = round(gross_pnl / (risk_per_share * qty), 2)
        assert r == pytest.approx(2.0)

    def test_stop_hit_long(self):
        entry, exit_, sl, qty = 1000.0, 970.0, 970.0, 10
        risk_per_share = abs(entry - sl)
        gross_pnl      = (exit_ - entry) * qty
        r = round(gross_pnl / (risk_per_share * qty), 2)
        assert r == pytest.approx(-1.0)

    def test_r_multiple_positive_on_win(self):
        entry, exit_, sl, qty = 500.0, 550.0, 475.0, 20
        risk_per_share = abs(entry - sl)
        gross_pnl      = (exit_ - entry) * qty
        r = gross_pnl / (risk_per_share * qty)
        assert r > 0

    def test_r_multiple_negative_on_loss(self):
        entry, exit_, sl, qty = 500.0, 460.0, 475.0, 20
        risk_per_share = abs(entry - sl)
        gross_pnl      = (exit_ - entry) * qty
        r = gross_pnl / (risk_per_share * qty)
        assert r < 0
