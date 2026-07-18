import unittest
from unittest.mock import AsyncMock, patch

from app.core.trading_safety import trading_safety
from app.models.db_models import OrderSide
from app.oms.core import oms
from app.quoting.engine import QuotingEngine


class QuoteReplacementSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        trading_safety.clear_halt_for_tests()

    async def asyncTearDown(self):
        trading_safety.clear_halt_for_tests()

    async def test_failed_cancel_blocks_replacement_order(self):
        engine = QuotingEngine("market", "token")
        engine.active_orders = {
            "old-order": {
                "side": "BUY",
                "price": 0.40,
                "size": 5.0,
                "created_ts": 0.0,
            }
        }
        desired = [
            {
                "condition_id": "market",
                "token_id": "token",
                "side": OrderSide.BUY,
                "price": 0.39,
                "size": 5.0,
            }
        ]
        with (
            patch.object(oms, "cancel_order", AsyncMock(return_value=False)),
            patch.object(engine, "place_orders", AsyncMock()) as place_orders,
            patch.object(
                engine, "_update_pending_buy_notional", AsyncMock()
            ) as update_pending,
        ):
            await engine.sync_orders_diff(desired, fair_value=0.50)

        place_orders.assert_not_awaited()
        update_pending.assert_awaited()
        self.assertTrue(trading_safety.status()["halted"])
        self.assertIn("old-order", engine.active_orders)


if __name__ == "__main__":
    unittest.main()
