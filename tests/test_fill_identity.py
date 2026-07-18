import unittest

from app.oms.fill_processor import derive_fill_event_id


class FillIdentityTests(unittest.TestCase):
    def test_replayed_exchange_trade_has_same_event_id(self):
        event = {"event_type": "trade", "id": "trade-123", "status": "MATCHED"}
        first = derive_fill_event_id(event, "order-1", "maker")
        second = derive_fill_event_id(dict(event), "order-1", "maker")
        self.assertEqual(first, second)

    def test_same_trade_is_scoped_per_order_and_role(self):
        event = {"trade_id": "trade-123"}
        maker_one = derive_fill_event_id(event, "order-1", "maker")
        maker_two = derive_fill_event_id(event, "order-2", "maker")
        taker_one = derive_fill_event_id(event, "order-1", "taker")
        self.assertEqual(len({maker_one, maker_two, taker_one}), 3)

    def test_payload_fallback_is_order_independent_and_deterministic(self):
        first_payload = {"b": 2, "a": 1}
        second_payload = {"a": 1, "b": 2}
        first = derive_fill_event_id(first_payload, "order-1", "maker")
        second = derive_fill_event_id(second_payload, "order-1", "maker")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
