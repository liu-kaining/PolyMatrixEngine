import math
import unittest

from app.oms.validation import OrderValidationError, validate_order_intent


class OrderValidationTests(unittest.TestCase):
    def test_accepts_bounded_prediction_order(self):
        intent = validate_order_intent(
            condition_id="condition",
            token_id="token",
            side="BUY",
            price=0.42,
            size=10,
        )
        self.assertEqual(intent.side, "BUY")
        self.assertEqual(intent.price, 0.42)

        minimum = validate_order_intent(
            condition_id="condition",
            token_id="token",
            side="SELL",
            price=0.42,
            size=5,
        )
        self.assertEqual(minimum.size, 5)

    def test_rejects_non_finite_numbers(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaises(OrderValidationError):
                    validate_order_intent(
                        condition_id="condition",
                        token_id="token",
                        side="BUY",
                        price=value,
                        size=10,
                    )

    def test_rejects_invalid_bounds_and_identity(self):
        invalid = (
            {"condition_id": "", "token_id": "token", "side": "BUY", "price": 0.5, "size": 1},
            {"condition_id": "c", "token_id": "", "side": "BUY", "price": 0.5, "size": 1},
            {"condition_id": "c", "token_id": "t", "side": "HOLD", "price": 0.5, "size": 1},
            {"condition_id": "c", "token_id": "t", "side": "BUY", "price": 0.0, "size": 1},
            {"condition_id": "c", "token_id": "t", "side": "BUY", "price": 1.0, "size": 1},
            {"condition_id": "c", "token_id": "t", "side": "BUY", "price": 0.5, "size": 0},
            {"condition_id": "c", "token_id": "t", "side": "BUY", "price": 0.5, "size": 4.999},
        )
        for item in invalid:
            with self.subTest(item=item):
                with self.assertRaises(OrderValidationError):
                    validate_order_intent(**item)


if __name__ == "__main__":
    unittest.main()
