import unittest
from unittest.mock import patch

from app.oms.core import (
    CANCEL_ALREADY_CLOSED,
    CANCEL_CONFIRMED,
    CANCEL_MATCHED_UNKNOWN,
    CANCEL_UNKNOWN,
    OrderManagementSystem,
    classify_cancel_response,
)


class CancelResponseTests(unittest.TestCase):
    def test_requires_the_requested_order_identity(self):
        self.assertEqual(
            classify_cancel_response("target", {"canceled": ["target"]}),
            CANCEL_CONFIRMED,
        )
        self.assertEqual(
            classify_cancel_response("target", {"canceled": ["unrelated"]}),
            CANCEL_UNKNOWN,
        )
        self.assertEqual(
            classify_cancel_response("target", {"success": True}), CANCEL_UNKNOWN
        )

    def test_distinguishes_closed_from_matched_unknown(self):
        self.assertEqual(
            classify_cancel_response(
                "target", {"not_canceled": {"target": "already canceled"}}
            ),
            CANCEL_ALREADY_CLOSED,
        )
        self.assertEqual(
            classify_cancel_response(
                "target", {"not_canceled": {"target": "already matched"}}
            ),
            CANCEL_MATCHED_UNKNOWN,
        )


class LazyOmsInitializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_constructor_never_initializes_exchange_sdk(self):
        with patch.object(OrderManagementSystem, "_build_live_client") as build_client:
            instance = OrderManagementSystem()
            build_client.assert_not_called()
            await instance.aclose()


if __name__ == "__main__":
    unittest.main()
