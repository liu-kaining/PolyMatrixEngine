import unittest

from app.core.position_reconciliation import (
    build_actual_inventory_from_positions,
    reconcile_capital_used,
)


class PositionReconciliationTests(unittest.TestCase):
    def test_groups_binary_positions_and_reported_cost(self):
        result = build_actual_inventory_from_positions(
            [
                {
                    "conditionId": "0xABC",
                    "outcomeIndex": 0,
                    "size": "5",
                    "avgPrice": "0.4",
                },
                {
                    "conditionId": "0xabc",
                    "outcome": "NO",
                    "size": "3",
                    "initialValue": "1.8",
                },
            ]
        )
        self.assertEqual(result["0xabc"]["yes"], 5)
        self.assertEqual(result["0xabc"]["yes_cost"], 2)
        self.assertEqual(result["0xabc"]["no"], 3)
        self.assertEqual(result["0xabc"]["no_cost"], 1.8)

    def test_position_discovered_from_zero_uses_worst_case_risk_capital(self):
        capital, discovered = reconcile_capital_used(
            actual_size=12,
            reported_cost=3,
            previous_size=0,
            previous_capital_used=0,
        )
        self.assertEqual(capital, 12)
        self.assertTrue(discovered)

    def test_existing_position_keeps_proportional_cost_basis(self):
        capital, discovered = reconcile_capital_used(
            actual_size=5,
            reported_cost=99,
            previous_size=10,
            previous_capital_used=4,
        )
        self.assertEqual(capital, 2)
        self.assertFalse(discovered)

    def test_unexplained_position_increase_uses_worst_case_increment(self):
        capital, discovered = reconcile_capital_used(
            actual_size=7,
            reported_cost=2,
            previous_size=5,
            previous_capital_used=2,
        )
        self.assertEqual(capital, 4)
        self.assertTrue(discovered)

    def test_nonzero_position_with_zero_cost_is_treated_as_untracked(self):
        capital, discovered = reconcile_capital_used(
            actual_size=5,
            reported_cost=2,
            previous_size=5,
            previous_capital_used=0,
        )
        self.assertEqual(capital, 5)
        self.assertTrue(discovered)

    def test_zero_position_clears_capital(self):
        capital, discovered = reconcile_capital_used(
            actual_size=0,
            reported_cost=0,
            previous_size=5,
            previous_capital_used=2,
        )
        self.assertEqual(capital, 0)
        self.assertFalse(discovered)


if __name__ == "__main__":
    unittest.main()
