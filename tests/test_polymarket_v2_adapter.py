import unittest
from types import SimpleNamespace

from app.oms.polymarket_v2 import (
    ExchangeContractError,
    PolymarketV2Adapter,
    is_definite_submit_rejection_status,
    normalize_open_order,
    normalize_order_post_response,
    normalize_sdk_stream_event,
)


class FakePaginator:
    def __init__(self, items):
        self.items = items

    async def iter_items(self):
        for item in self.items:
            yield item


class FakeClient:
    def __init__(self):
        self.credentials = SimpleNamespace(key="key", secret="secret", passphrase="pass")
        self.wallet = "0xabc"
        self._user_manager = SimpleNamespace(is_open=True)

    def list_open_orders(self, **kwargs):
        return FakePaginator(
            [
                {
                    "id": "order-1",
                    "market": "market-1",
                    "asset_id": "token-1",
                    "side": "BUY",
                    "price": "0.40",
                    "original_size": "10.000001",
                    "size_matched": "2.000001",
                    "status": "ORDER_STATUS_LIVE",
                }
            ]
        )

    async def get_order_book(self, *, token_id):
        return SimpleNamespace(
            token_id=token_id,
            condition_id="market-1",
            tick_size="0.001",
            min_order_size="5.5",
            neg_risk=False,
        )

    async def get_balance_allowance(self, *, asset_type, token_id=None):
        self.balance_asset_type = asset_type
        self.balance_token_id = token_id
        return SimpleNamespace(balance=1_234_567)

    def list_account_trades(self, **kwargs):
        self.trade_list_kwargs = kwargs
        return FakePaginator(
            [
                {
                    "id": "trade-1",
                    "market": "market-1",
                    "asset_id": "token-1",
                    "status": "TRADE_STATUS_CONFIRMED",
                    "taker_order_id": "order-1",
                    "maker_orders": [],
                },
                {
                    "id": "trade-2",
                    "market": "market-1",
                    "asset_id": "token-1",
                    "status": "TRADE_STATUS_CONFIRMED",
                    "taker_order_id": "someone-else",
                    "maker_orders": [],
                },
            ]
        )


class PolymarketV2NormalizationTests(unittest.TestCase):
    def test_only_unambiguous_http_statuses_release_submit_reservation(self):
        self.assertTrue(is_definite_submit_rejection_status(425))
        self.assertTrue(is_definite_submit_rejection_status(429))
        self.assertFalse(is_definite_submit_rejection_status(408))
        self.assertFalse(is_definite_submit_rejection_status(499))
        self.assertFalse(is_definite_submit_rejection_status(500))

    def test_order_post_union_is_explicit(self):
        accepted = normalize_order_post_response(
            {
                "ok": True,
                "order_id": "abc",
                "status": "matched",
                "making_amount": "1",
                "taking_amount": "2",
                "trade_ids": ["trade"],
            }
        )
        self.assertTrue(accepted["success"])
        self.assertEqual(accepted["status"], "MATCHED")
        self.assertEqual(accepted["orderID"], "abc")

        rejected = normalize_order_post_response(
            {"ok": False, "code": "post_only_would_cross", "message": "cross"}
        )
        self.assertFalse(rejected["success"])
        with self.assertRaises(ExchangeContractError):
            normalize_order_post_response({"success": True, "orderID": "ambiguous"})

    def test_open_order_declares_human_units_and_normalizes_status(self):
        normalized = normalize_open_order(
            {
                "id": "o",
                "market": "m",
                "asset_id": "t",
                "side": "SELL",
                "price": "0.51",
                "original_size": "5.000001",
                "size_matched": "0.000001",
                "status": "ORDER_STATUS_LIVE",
            }
        )
        self.assertEqual(normalized["status"], "LIVE")
        self.assertEqual(normalized["_size_encoding"], "human")
        self.assertEqual(normalized["original_size"], "5.000001")


class PolymarketV2AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_pagination_constraints_and_balance(self):
        client = FakeClient()
        adapter = PolymarketV2Adapter(client)
        orders = await adapter.get_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["status"], "LIVE")
        constraints = await adapter.get_market_constraints("token-1")
        self.assertEqual(str(constraints.tick_size), "0.001")
        self.assertEqual(str(constraints.min_order_size), "5.5")
        self.assertEqual(constraints.condition_id, "market-1")
        self.assertAlmostEqual(await adapter.get_balance(), 1.234567)
        self.assertAlmostEqual(await adapter.get_token_balance("token-1"), 1.234567)
        self.assertEqual(client.balance_asset_type, "CONDITIONAL")
        self.assertEqual(client.balance_token_id, "token-1")
        self.assertTrue(adapter.user_stream_is_open())

    async def test_order_filter_is_local_and_never_sent_as_trade_id(self):
        client = FakeClient()
        adapter = PolymarketV2Adapter(client)
        trades = await adapter.get_trades(market="market-1", order_id="order-1")
        self.assertEqual([trade["id"] for trade in trades], ["trade-1"])
        self.assertEqual(client.trade_list_kwargs, {"token_id": None, "market": "market-1"})

    async def test_sdk_stream_event_is_flattened_without_losing_identity(self):
        event = SimpleNamespace(
            model_dump=lambda **_: {
                "topic": "user",
                "type": "order",
                "payload": {
                    "id": "order-1",
                    "token_id": "token-1",
                    "order_event_type": "CANCELLATION",
                },
            }
        )
        normalized = normalize_sdk_stream_event(event)
        self.assertEqual(normalized["event_type"], "order")
        self.assertEqual(normalized["asset_id"], "token-1")
        self.assertEqual(normalized["type"], "CANCELLATION")


if __name__ == "__main__":
    unittest.main()
