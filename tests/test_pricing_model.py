import unittest

from app.core.pricing_model import (
    PricingModelError,
    calculate_binary_fair_value,
    calculate_book_signal,
    pair_time_skew_seconds,
)


class PricingModelTests(unittest.TestCase):
    def setUp(self):
        self.yes_bids = [
            {"price": "0.49", "size": "100"},
            {"price": "0.48", "size": "50"},
        ]
        self.yes_asks = [
            {"price": "0.51", "size": "100"},
            {"price": "0.52", "size": "50"},
        ]
        self.no_bids = [
            {"price": "0.49", "size": "100"},
            {"price": "0.48", "size": "50"},
        ]
        self.no_asks = [
            {"price": "0.51", "size": "100"},
            {"price": "0.52", "size": "50"},
        ]

    def _calculate(self, **overrides):
        values = {
            "yes_bids": self.yes_bids,
            "yes_asks": self.yes_asks,
            "no_bids": self.no_bids,
            "no_asks": self.no_asks,
            "base_spread": 0.02,
            "depth_levels": 2,
            "depth_decay": 0.5,
            "max_parity_error": 0.03,
            "inventory_cap": 100.0,
            "max_inventory_skew": 0.02,
        }
        values.update(overrides)
        return calculate_binary_fair_value(**values)

    def test_balanced_complementary_books_anchor_at_half(self):
        result = self._calculate()
        self.assertAlmostEqual(result.yes_fair_value, 0.5)
        self.assertAlmostEqual(result.parity_error, 0.0)
        self.assertGreaterEqual(result.dynamic_spread, 0.02)

    def test_inventory_skew_reduces_the_overweight_side(self):
        balanced = self._calculate().yes_fair_value
        skewed = self._calculate(
            yes_inventory_value=100.0, no_inventory_value=0.0
        ).yes_fair_value
        self.assertAlmostEqual(balanced - skewed, 0.02)

    def test_cross_book_parity_divergence_is_rejected(self):
        with self.assertRaises(PricingModelError):
            self._calculate(
                no_bids=[{"price": "0.29", "size": "100"}],
                no_asks=[{"price": "0.31", "size": "100"}],
            )

    def test_crossed_book_and_pair_time_skew_are_explicit(self):
        with self.assertRaises(PricingModelError):
            calculate_book_signal(
                [{"price": 0.6, "size": 1}],
                [{"price": 0.5, "size": 1}],
                depth_levels=1,
                depth_decay=1.0,
            )
        self.assertEqual(
            pair_time_skew_seconds(
                {"exchange_timestamp": 100.0}, {"exchange_timestamp": 101.5}
            ),
            1.5,
        )


if __name__ == "__main__":
    unittest.main()
