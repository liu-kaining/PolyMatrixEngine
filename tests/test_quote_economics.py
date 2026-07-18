import unittest

from app.core.quote_economics import evaluate_quote_economics


class QuoteEconomicsTests(unittest.TestCase):
    def test_rejects_book_edge_consumed_by_cost_and_adverse_selection(self):
        result = evaluate_quote_economics(
            side="BUY",
            limit_price=0.49,
            fair_value=0.50,
            execution_cost_buffer=0.002,
            adverse_selection_buffer=0.01,
            minimum_net_edge=0.005,
        )
        self.assertFalse(result.allowed)
        self.assertLess(result.net_edge, 0)

    def test_accepts_only_net_edge_above_minimum(self):
        result = evaluate_quote_economics(
            side="BUY",
            limit_price=0.45,
            fair_value=0.50,
            execution_cost_buffer=0.002,
            adverse_selection_buffer=0.01,
            minimum_net_edge=0.02,
        )
        self.assertTrue(result.allowed)
        self.assertAlmostEqual(result.net_edge, 0.038)

    def test_rewards_are_not_an_input_to_trade_admission(self):
        result = evaluate_quote_economics(
            side="BUY",
            limit_price=0.50,
            fair_value=0.50,
            execution_cost_buffer=0,
            adverse_selection_buffer=0,
            minimum_net_edge=0.001,
        )
        self.assertFalse(result.allowed)


if __name__ == "__main__":
    unittest.main()
