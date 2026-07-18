import math
import unittest

from app.risk.reservations import evaluate_reservation, evaluate_sell_reservation


class ReservationDecisionTests(unittest.TestCase):
    def test_sell_reservation_cannot_reuse_inventory(self):
        admitted = evaluate_sell_reservation(
            requested_size=4,
            inventory_exposure=10,
            already_reserved_size=6,
        )
        self.assertTrue(admitted.allowed)
        rejected = evaluate_sell_reservation(
            requested_size=5,
            inventory_exposure=10,
            already_reserved_size=6,
        )
        self.assertFalse(rejected.allowed)

    def test_rejects_non_finite_and_negative_state(self):
        for requested, global_capital in ((math.nan, 0), (1, -1)):
            with self.subTest(requested=requested, global_capital=global_capital):
                decision = evaluate_reservation(
                    requested_notional=requested,
                    global_capital_used=global_capital,
                    global_reserved=0,
                    market_capital_used=0,
                    market_reserved=0,
                    global_cap=100,
                    market_cap=50,
                )
                self.assertFalse(decision.allowed)

    def test_admits_within_global_and_market_caps(self):
        decision = evaluate_reservation(
            requested_notional=5,
            global_capital_used=20,
            global_reserved=10,
            market_capital_used=5,
            market_reserved=2,
            global_cap=100,
            market_cap=20,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.global_after, 35)
        self.assertEqual(decision.market_after, 12)

    def test_rejects_global_oversubscription(self):
        decision = evaluate_reservation(
            requested_notional=11,
            global_capital_used=80,
            global_reserved=10,
            market_capital_used=0,
            market_reserved=0,
            global_cap=100,
            market_cap=50,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "global budget would be exceeded")

    def test_rejects_market_oversubscription(self):
        decision = evaluate_reservation(
            requested_notional=6,
            global_capital_used=0,
            global_reserved=0,
            market_capital_used=10,
            market_reserved=5,
            global_cap=100,
            market_cap=20,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "market budget would be exceeded")

    def test_exact_cap_is_allowed(self):
        decision = evaluate_reservation(
            requested_notional=5,
            global_capital_used=90,
            global_reserved=5,
            market_capital_used=10,
            market_reserved=5,
            global_cap=100,
            market_cap=20,
        )
        self.assertTrue(decision.allowed)


if __name__ == "__main__":
    unittest.main()
