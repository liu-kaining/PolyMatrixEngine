import unittest

from app.core.accounting import AccountingInvariantError
from app.core.cash_accounting import (
    build_fill_cash_fact,
    extract_explicit_fee_amount,
)


class CashAccountingTests(unittest.TestCase):
    def test_signed_cash_and_known_fee(self):
        buy = build_fill_cash_fact(side="BUY", price=0.4, size=10, fee_amount=0.2)
        sell = build_fill_cash_fact(side="SELL", price=0.6, size=5, fee_amount=0.1)
        self.assertAlmostEqual(buy.gross_cash_delta, -4.0)
        self.assertAlmostEqual(buy.net_cash_delta, -4.2)
        self.assertEqual(buy.fee_status, "KNOWN")
        self.assertAlmostEqual(sell.gross_cash_delta, 3.0)
        self.assertAlmostEqual(sell.net_cash_delta, 2.9)

    def test_missing_fee_is_unknown_not_zero(self):
        fact = build_fill_cash_fact(side="BUY", price=0.5, size=2, fee_amount=None)
        self.assertEqual(fact.gross_cash_delta, -1.0)
        self.assertIsNone(fact.fee_amount)
        self.assertIsNone(fact.net_cash_delta)
        self.assertEqual(fact.fee_status, "UNKNOWN")

    def test_only_explicit_absolute_fee_is_accepted(self):
        self.assertEqual(extract_explicit_fee_amount({"fee_amount": "0.125"}), 0.125)
        self.assertIsNone(extract_explicit_fee_amount({"fee_rate_bps": 10}))

    def test_conflicting_or_invalid_fee_aliases_fail_closed(self):
        payloads = (
            {"fee_amount": 0.1, "feePaid": 0.2},
            {"fee_amount": -0.1},
            {"fee_amount": float("nan")},
            {"fee_amount": True},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(AccountingInvariantError):
                    extract_explicit_fee_amount(payload)


if __name__ == "__main__":
    unittest.main()
