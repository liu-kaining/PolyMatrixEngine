import unittest

from app.core.accounting import AccountingInvariantError, apply_fill_accounting


class AccountingTests(unittest.TestCase):
    def test_buy_increases_cost_without_realizing_loss(self):
        result = apply_fill_accounting(
            exposure=0,
            capital_used=0,
            realized_pnl=0,
            side="BUY",
            fill_size=10,
            fill_price=0.4,
        )
        self.assertEqual(result.exposure, 10)
        self.assertEqual(result.capital_used, 4)
        self.assertEqual(result.realized_pnl, 0)

    def test_profitable_sell_realizes_only_closed_lot_profit(self):
        result = apply_fill_accounting(
            exposure=10,
            capital_used=4,
            realized_pnl=0,
            side="SELL",
            fill_size=5,
            fill_price=0.6,
        )
        self.assertEqual(result.exposure, 5)
        self.assertEqual(result.capital_used, 2)
        self.assertEqual(result.realized_pnl, 1)

    def test_losing_full_exit_realizes_loss_and_clears_cost(self):
        result = apply_fill_accounting(
            exposure=10,
            capital_used=7,
            realized_pnl=2,
            side="SELL",
            fill_size=10,
            fill_price=0.5,
        )
        self.assertEqual(result.exposure, 0)
        self.assertEqual(result.capital_used, 0)
        self.assertEqual(result.realized_pnl, 0)

    def test_oversell_is_fail_closed(self):
        with self.assertRaises(AccountingInvariantError):
            apply_fill_accounting(
                exposure=4,
                capital_used=2,
                realized_pnl=0,
                side="SELL",
                fill_size=5,
                fill_price=0.5,
            )

    def test_invalid_fill_is_rejected(self):
        for side, size, price in (("BUY", 0, 0.5), ("BUY", 1, 1.1), ("X", 1, 0.5)):
            with self.subTest(side=side, size=size, price=price):
                with self.assertRaises(AccountingInvariantError):
                    apply_fill_accounting(
                        exposure=0,
                        capital_used=0,
                        realized_pnl=0,
                        side=side,
                        fill_size=size,
                        fill_price=price,
                    )

    def test_buy_fee_is_capitalized_and_sell_fee_reduces_realized_pnl(self):
        bought = apply_fill_accounting(
            exposure=0,
            capital_used=0,
            realized_pnl=0,
            side="BUY",
            fill_size=10,
            fill_price=0.4,
            fee_amount=0.2,
        )
        self.assertAlmostEqual(bought.capital_used, 4.2)
        sold = apply_fill_accounting(
            exposure=bought.exposure,
            capital_used=bought.capital_used,
            realized_pnl=bought.realized_pnl,
            side="SELL",
            fill_size=5,
            fill_price=0.6,
            fee_amount=0.1,
        )
        self.assertAlmostEqual(sold.capital_used, 2.1)
        self.assertAlmostEqual(sold.realized_pnl, 0.8)

    def test_non_finite_or_negative_fee_is_rejected(self):
        for fee in (-0.01, float("nan"), float("inf")):
            with self.subTest(fee=fee):
                with self.assertRaises(AccountingInvariantError):
                    apply_fill_accounting(
                        exposure=0,
                        capital_used=0,
                        realized_pnl=0,
                        side="BUY",
                        fill_size=1,
                        fill_price=0.5,
                        fee_amount=fee,
                    )


if __name__ == "__main__":
    unittest.main()
