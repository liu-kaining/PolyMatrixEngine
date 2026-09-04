import unittest

from app.core.cash_accounting import calculate_taker_fee_usdc, resolve_fee_amount
from app.core.geographic_eligibility import (
    GeographicEligibilityError,
    parse_geoblock_response,
)
from app.models.db_models import OrderSide
from app.oms.order_reconciliation import extract_fills_for_order
from app.oms.paper_execution import PaperOrder, decide_paper_fill
from app.quoting.engine import QuotingEngine


class LiveExecutionContractTests(unittest.TestCase):
    def test_documented_maker_and_taker_fees_are_exact(self):
        self.assertEqual(resolve_fee_amount({"fee_rate_bps": "25"}, "MAKER"), 0.0)
        self.assertEqual(
            resolve_fee_amount(
                {}, "TAKER", price="0.5", size="100", fee_rate_bps="700"
            ),
            1.75,
        )
        self.assertEqual(
            calculate_taker_fee_usdc(price="0.5", size="0.0004", fee_rate_bps="400"),
            0.0,
        )
        self.assertEqual(resolve_fee_amount({"fee_amount": "0.125"}, "TAKER"), 0.125)

    def test_geoblock_requires_explicit_boolean_and_country(self):
        result = parse_geoblock_response(
            {"blocked": False, "country": "US", "region": "CA"}
        )
        self.assertFalse(result.blocked)
        with self.assertRaises(GeographicEligibilityError):
            parse_geoblock_response({"blocked": "false", "country": "US"})

    def test_trade_recovery_uses_role_specific_rows_and_e6_units(self):
        trade = {
            "id": "trade-1",
            "status": "TRADE_STATUS_CONFIRMED",
            "taker_order_id": "taker-order",
            "asset_id": "token",
            "price": "0.4",
            "size": "5000000",
            "fee_rate_bps": "25",
            "maker_orders": [
                {
                    "order_id": "maker-order",
                    "asset_id": "token",
                    "price": "0.4",
                    "matched_amount": "2000000",
                    "fee_rate_bps": "0",
                }
            ],
            "_size_encoding": "e6",
        }
        taker = extract_fills_for_order([trade], "taker-order")[0]
        maker = extract_fills_for_order([trade], "maker-order")[0]
        self.assertEqual((taker.size, taker.liquidity_role), (5.0, "TAKER"))
        self.assertEqual((maker.size, maker.liquidity_role), (2.0, "MAKER"))

    def test_dynamic_tick_quantization_is_side_safe(self):
        engine = QuotingEngine("market", "token")
        self.assertTrue(
            engine._apply_market_constraints(
                {"tick_size": "0.001", "min_order_size": "5.5", "neg_risk": False}
            )
        )
        self.assertEqual(engine._quantize_price(0.4567, OrderSide.BUY), 0.456)
        self.assertEqual(engine._quantize_price(0.4561, OrderSide.SELL), 0.457)
        self.assertEqual(engine.min_order_size, 5.5)

    def test_paper_fills_are_event_driven_and_conservative(self):
        order = PaperOrder(
            "o",
            "m",
            "t",
            "BUY",
            0.4,
            10.0,
            True,
            1.0,
        )
        snapshot = {
            "valid": True,
            "last_trade_price": 0.39,
            "last_trade_size": 8.0,
            "last_trade_id": "trade-1",
        }
        decision = decide_paper_fill(order, snapshot)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.liquidity_role, "MAKER")
        self.assertEqual(decision.price, 0.4)
        self.assertGreater(decision.size, 0)


if __name__ == "__main__":
    unittest.main()
