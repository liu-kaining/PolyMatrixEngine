import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.core.trading_safety import trading_safety
from app.risk.watchdog import (
    RiskMonitor,
    authenticated_balance_required,
    risk_limit_breached,
)


class RiskWatchdogSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        trading_safety.clear_halt_for_tests()

    async def test_active_breach_republishes_suspend_when_db_is_already_suspended(self):
        market = SimpleNamespace(status="suspended")
        query_result = SimpleNamespace(scalar_one_or_none=lambda: market)
        session = SimpleNamespace(
            execute=AsyncMock(return_value=query_result),
            commit=AsyncMock(),
        )

        with (
            patch(
                "app.risk.watchdog.redis_client.publish", new_callable=AsyncMock
            ) as publish,
            patch(
                "app.risk.watchdog.oms.cancel_market_orders",
                new_callable=AsyncMock,
                return_value=True,
            ) as cancel,
        ):
            await RiskMonitor().trigger_kill_switch("condition-1", session)

        publish.assert_awaited_once_with(
            "control:condition-1", {"action": "suspend"}
        )
        cancel.assert_awaited_once_with("condition-1")
        session.commit.assert_not_awaited()


class AuthenticatedBalanceSelectionTests(unittest.TestCase):
    def test_active_market_is_queried_even_when_public_and_local_are_zero(self):
        self.assertTrue(
            authenticated_balance_required(
                local_yes=0,
                local_no=0,
                discovered_yes=0,
                discovered_no=0,
                active=True,
                tolerance=0.01,
            )
        )

    def test_nonzero_or_negative_anomaly_is_queried(self):
        self.assertTrue(
            authenticated_balance_required(
                local_yes=-0.02,
                local_no=0,
                discovered_yes=0,
                discovered_no=0,
                active=False,
                tolerance=0.01,
            )
        )
        self.assertFalse(
            authenticated_balance_required(
                local_yes=0,
                local_no=0,
                discovered_yes=0.005,
                discovered_no=0,
                active=False,
                tolerance=0.01,
            )
        )

    def test_non_finite_balance_anomaly_is_always_queried(self):
        self.assertTrue(
            authenticated_balance_required(
                local_yes=float("nan"),
                local_no=0,
                discovered_yes=0,
                discovered_no=0,
                active=False,
                tolerance=0.01,
            )
        )


class RiskLimitValidationTests(unittest.TestCase):
    def test_exact_cap_is_safe_but_invalid_state_fails_closed(self):
        self.assertFalse(risk_limit_breached(10, 10))
        self.assertTrue(risk_limit_breached(10.01, 10))
        self.assertTrue(risk_limit_breached(float("nan"), 10))
        self.assertTrue(risk_limit_breached(1, float("inf")))
        self.assertTrue(risk_limit_breached(-1, 10))
        self.assertTrue(risk_limit_breached(0, 0))


if __name__ == "__main__":
    unittest.main()
