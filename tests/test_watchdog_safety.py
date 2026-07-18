import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.core.trading_safety import trading_safety
from app.risk.watchdog import RiskMonitor


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


if __name__ == "__main__":
    unittest.main()
