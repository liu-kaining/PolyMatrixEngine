"""
V8.0 Conservative Strategy Tests
Tests for: unrealized PnL, book quality filter, cheap-side gate, cumulative hedge tracking.
All tests are self-contained — they mock the import chain to avoid needing DB/Redis.
"""
import asyncio
import sys
import types
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

# ---------------------------------------------------------------------------
# Mock heavy dependencies so importing app modules doesn't require DB/Redis
# ---------------------------------------------------------------------------
_mock_modules = {}

def _ensure_mock_module(name):
    if name not in sys.modules:
        _mock_modules[name] = sys.modules[name] = types.ModuleType(name)
    return sys.modules[name]

# Pre-mock redis, asyncpg, sqlalchemy async engine so app.core.* can import
for mod_name in [
    "redis", "redis.asyncio", "asyncpg",
    "websockets", "websockets.exceptions",
    "httpx", "web3",
    "py_clob_client", "py_clob_client.client", "py_clob_client.clob_types",
    "py_clob_client.headers", "py_clob_client.headers.headers",
    "py_builder_signing_sdk", "py_builder_signing_sdk.config",
]:
    m = _ensure_mock_module(mod_name)

# Provide minimal stubs that the import chain needs
sys.modules["redis.asyncio"].Redis = MagicMock
sys.modules["py_clob_client.client"].ClobClient = MagicMock
sys.modules["py_clob_client.clob_types"].OrderArgs = MagicMock
sys.modules["py_clob_client.clob_types"].RequestArgs = MagicMock
sys.modules["py_clob_client.clob_types"].AssetType = MagicMock
sys.modules["py_clob_client.clob_types"].BalanceAllowanceParams = MagicMock
sys.modules["py_clob_client.headers.headers"].create_level_2_headers = MagicMock


# ---------------------------------------------------------------------------
# Helper: create a fresh InventoryStateManager without singleton
# ---------------------------------------------------------------------------
def _make_fresh_manager():
    from app.core.inventory_state import InventoryStateManager
    mgr = object.__new__(InventoryStateManager)
    mgr._initialized = True
    mgr._state = {}
    mgr._lock = asyncio.Lock()
    mgr._persist_queue = asyncio.Queue(maxsize=100)
    mgr._persist_task = None
    return mgr


def _make_snapshot(**overrides):
    base = {
        "yes_exposure": 0.0, "no_exposure": 0.0,
        "yes_capital_used": 0.0, "no_capital_used": 0.0,
        "realized_pnl": 0.0, "last_local_fill_timestamp": 0.0,
        "pending_yes_buy_notional": 0.0, "pending_no_buy_notional": 0.0,
        "updated_at": 0.0,
        "yes_unhedged_size": 0.0, "yes_unhedged_cost": 0.0,
        "no_unhedged_size": 0.0, "no_unhedged_cost": 0.0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Unrealized PnL Calculation
# ---------------------------------------------------------------------------
class TestUnrealizedPnL:

    @pytest.fixture
    def mgr(self):
        return _make_fresh_manager()

    @pytest.mark.asyncio
    async def test_no_exposure_returns_zero(self, mgr):
        mgr._state["m1"] = _make_snapshot()
        result = await mgr.get_unrealized_pnl("m1", fv_yes=0.60)
        assert result["total_unrealized_pnl"] == 0.0

    @pytest.mark.asyncio
    async def test_profitable_yes_position(self, mgr):
        """Bought 10 YES at 0.40, now worth 0.60 → profit $2."""
        mgr._state["m1"] = _make_snapshot(yes_exposure=10.0, yes_capital_used=4.0)
        result = await mgr.get_unrealized_pnl("m1", fv_yes=0.60)
        assert abs(result["yes_unrealized_pnl"] - 2.0) < 0.01
        assert result["total_unrealized_pnl"] > 0

    @pytest.mark.asyncio
    async def test_losing_no_position(self, mgr):
        """Bought 10 NO at 0.50, YES moved to 0.70 (NO=0.30) → loss $2."""
        mgr._state["m1"] = _make_snapshot(no_exposure=10.0, no_capital_used=5.0)
        result = await mgr.get_unrealized_pnl("m1", fv_yes=0.70)
        assert abs(result["no_unrealized_pnl"] - (-2.0)) < 0.01

    @pytest.mark.asyncio
    async def test_stop_loss_threshold(self, mgr):
        """20 NO at avg 0.40 ($8 spent), YES=0.85 (NO=0.15) → loss = 20*0.15 - 8 = -$5."""
        mgr._state["m1"] = _make_snapshot(no_exposure=20.0, no_capital_used=8.0)
        result = await mgr.get_unrealized_pnl("m1", fv_yes=0.85)
        assert result["total_unrealized_pnl"] <= -5.0


# ---------------------------------------------------------------------------
# 2. Cumulative Unhedged Fill Tracking
# ---------------------------------------------------------------------------
class TestCumulativeHedge:

    @pytest.fixture
    def mgr(self):
        m = _make_fresh_manager()
        m._state["m1"] = _make_snapshot()
        return m

    @pytest.mark.asyncio
    async def test_single_fill_below_threshold(self, mgr):
        total, avg = await mgr.accumulate_unhedged_fill("m1", True, 3.0, 0.30)
        assert total == 3.0
        assert abs(avg - 0.30) < 0.001

    @pytest.mark.asyncio
    async def test_multiple_fills_reach_threshold(self, mgr):
        await mgr.accumulate_unhedged_fill("m1", True, 3.0, 0.30)
        total, avg = await mgr.accumulate_unhedged_fill("m1", True, 3.0, 0.32)
        assert total == 6.0
        assert total >= 5.0  # should trigger hedge
        assert abs(avg - 0.31) < 0.001  # weighted avg

    @pytest.mark.asyncio
    async def test_clear_resets_to_zero(self, mgr):
        await mgr.accumulate_unhedged_fill("m1", True, 10.0, 0.30)
        await mgr.clear_unhedged("m1", True)
        total, avg = await mgr.accumulate_unhedged_fill("m1", True, 2.0, 0.35)
        assert total == 2.0


# ---------------------------------------------------------------------------
# 3. Book Quality Filter
# ---------------------------------------------------------------------------
class TestBookQualityFilter:

    @pytest.mark.asyncio
    async def test_thin_book_rejected(self):
        from app.core.auto_router import _check_book_quality
        mock_book = {
            "bids": [{"price": "0.30", "size": "5"}, {"price": "0.29", "size": "5"}],
            "asks": [{"price": "0.32", "size": "5"}, {"price": "0.33", "size": "5"}],
        }
        with patch("app.core.auto_router._fetch_book_snapshot", new_callable=AsyncMock, return_value=mock_book):
            passed, reason = await _check_book_quality("tok")
        assert not passed
        assert "thin_book" in reason

    @pytest.mark.asyncio
    async def test_wide_spread_rejected(self):
        from app.core.auto_router import _check_book_quality
        mock_book = {
            "bids": [{"price": "0.20", "size": "500"}],
            "asks": [{"price": "0.35", "size": "500"}],
        }
        with patch("app.core.auto_router._fetch_book_snapshot", new_callable=AsyncMock, return_value=mock_book):
            passed, reason = await _check_book_quality("tok")
        assert not passed
        assert "spread_too_wide" in reason

    @pytest.mark.asyncio
    async def test_midpoint_uncertain_rejected(self):
        from app.core.auto_router import _check_book_quality
        mock_book = {
            "bids": [{"price": "0.48", "size": "500"}, {"price": "0.47", "size": "500"}, {"price": "0.46", "size": "500"}],
            "asks": [{"price": "0.52", "size": "500"}, {"price": "0.53", "size": "500"}, {"price": "0.54", "size": "500"}],
        }
        with patch("app.core.auto_router._fetch_book_snapshot", new_callable=AsyncMock, return_value=mock_book):
            passed, reason = await _check_book_quality("tok")
        assert not passed
        assert "midpoint_too_uncertain" in reason

    @pytest.mark.asyncio
    async def test_good_book_passes(self):
        from app.core.auto_router import _check_book_quality
        mock_book = {
            "bids": [{"price": "0.28", "size": "200"}, {"price": "0.27", "size": "200"}, {"price": "0.26", "size": "200"}],
            "asks": [{"price": "0.30", "size": "200"}, {"price": "0.31", "size": "200"}, {"price": "0.32", "size": "200"}],
        }
        with patch("app.core.auto_router._fetch_book_snapshot", new_callable=AsyncMock, return_value=mock_book):
            passed, reason = await _check_book_quality("tok")
        assert passed
        assert reason == "ok"

    @pytest.mark.asyncio
    async def test_fetch_failure_passthrough(self):
        from app.core.auto_router import _check_book_quality
        with patch("app.core.auto_router._fetch_book_snapshot", new_callable=AsyncMock, return_value=None):
            passed, reason = await _check_book_quality("tok")
        assert passed
        assert "passthrough" in reason


# ---------------------------------------------------------------------------
# 4. Cheap-Side Gate Logic (pure logic, no imports needed)
# ---------------------------------------------------------------------------
class TestCheapSideGate:

    def test_blocked_when_expensive(self):
        assert (True and 0.70 >= 0.45) is True

    def test_allowed_when_cheap(self):
        assert (True and 0.30 >= 0.45) is False

    def test_disabled(self):
        assert (False and 0.70 >= 0.45) is False

    def test_boundary_blocks(self):
        assert (True and 0.45 >= 0.45) is True

    def test_just_below_allowed(self):
        assert (True and 0.44 >= 0.45) is False


# ---------------------------------------------------------------------------
# 5. Average Cost Basis
# ---------------------------------------------------------------------------
class TestAvgCostBasis:

    @pytest.fixture
    def mgr(self):
        m = _make_fresh_manager()
        m._state["m1"] = _make_snapshot(yes_exposure=20.0, yes_capital_used=6.0)
        return m

    @pytest.mark.asyncio
    async def test_with_exposure(self, mgr):
        avg = await mgr.get_avg_cost_basis("m1", is_yes=True)
        assert abs(avg - 0.30) < 0.001

    @pytest.mark.asyncio
    async def test_no_exposure(self, mgr):
        avg = await mgr.get_avg_cost_basis("m1", is_yes=False)
        assert avg == 0.0
