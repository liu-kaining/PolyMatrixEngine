import unittest

from app.core.exit_policy import plan_bounded_sell


class ExitPolicyTests(unittest.TestCase):
    def test_uses_only_visible_depth_inside_impact_and_loss_floors(self):
        intent = plan_bounded_sell(
            bids=[
                {"price": 0.60, "size": 3},
                {"price": 0.59, "size": 4},
                {"price": 0.55, "size": 100},
            ],
            requested_size=10,
            exposure=10,
            capital_used=5.0,
            max_book_impact=0.02,
            max_realized_loss_fraction=0.10,
        )
        self.assertIsNotNone(intent)
        self.assertEqual(intent.limit_price, 0.59)
        self.assertEqual(intent.size, 7)

    def test_refuses_exit_below_configured_loss_floor(self):
        intent = plan_bounded_sell(
            bids=[{"price": 0.60, "size": 100}],
            requested_size=10,
            exposure=10,
            capital_used=8.0,
            max_book_impact=0.02,
            max_realized_loss_fraction=0.10,
        )
        self.assertIsNone(intent)

    def test_refuses_dust_or_invalid_book(self):
        self.assertIsNone(
            plan_bounded_sell(
                bids=[{"price": 0.60, "size": 4}],
                requested_size=10,
                exposure=10,
                capital_used=5.0,
                max_book_impact=0.02,
                max_realized_loss_fraction=0.10,
            )
        )
        self.assertIsNone(
            plan_bounded_sell(
                bids=[{"price": 1.2, "size": 100}],
                requested_size=10,
                exposure=10,
                capital_used=5.0,
                max_book_impact=0.02,
                max_realized_loss_fraction=0.10,
            )
        )


if __name__ == "__main__":
    unittest.main()
