import unittest

from app.core.accounting_integrity import (
    CashLedgerFact,
    InventoryAccountingFact,
    ProcessedFillFact,
    audit_accounting_facts,
)


MARKET = "market-1"


def inventory(**overrides):
    values = {
        "market_id": MARKET,
        "accounting_version": "v2",
        "state_version": 2,
        "yes_exposure": 5.0,
        "no_exposure": 0.0,
        "yes_capital_used": 2.1,
        "no_capital_used": 0.0,
        "net_realized_pnl": 0.8,
    }
    values.update(overrides)
    return InventoryAccountingFact(**values)


def fills():
    return [
        ProcessedFillFact(
            event_id="fill-buy",
            status="PROCESSED",
            market_id=MARKET,
            outcome="YES",
            side="BUY",
            price=0.4,
            size=10.0,
            accounting_state_version=1,
        ),
        ProcessedFillFact(
            event_id="fill-sell",
            status="PROCESSED",
            market_id=MARKET,
            outcome="YES",
            side="SELL",
            price=0.6,
            size=5.0,
            accounting_state_version=2,
        ),
    ]


def cash_entries():
    return [
        CashLedgerFact(
            event_id="fill-buy",
            market_id=MARKET,
            side="BUY",
            gross_cash_delta=-4.0,
            fee_amount=0.2,
            net_cash_delta=-4.2,
            fee_status="KNOWN",
        ),
        CashLedgerFact(
            event_id="fill-sell",
            market_id=MARKET,
            side="SELL",
            gross_cash_delta=3.0,
            fee_amount=0.1,
            net_cash_delta=2.9,
            fee_status="KNOWN",
        ),
    ]


class AccountingIntegrityTests(unittest.TestCase):
    def test_known_fee_fill_sequence_replays_exactly(self):
        report = audit_accounting_facts([inventory()], fills(), cash_entries())
        self.assertTrue(report.safe)
        self.assertEqual(report.blockers, ())

    def test_unknown_fee_blocks_net_accounting(self):
        rows = cash_entries()
        rows[0] = CashLedgerFact(
            event_id="fill-buy",
            market_id=MARKET,
            side="BUY",
            gross_cash_delta=-4.0,
            fee_amount=None,
            net_cash_delta=None,
            fee_status="UNKNOWN",
        )
        report = audit_accounting_facts([inventory()], fills(), rows)
        self.assertIn("UNKNOWN_EXECUTION_FEE", {item.code for item in report.blockers})

    def test_legacy_or_external_ledger_is_never_reported_safe(self):
        report = audit_accounting_facts(
            [inventory(accounting_version="unverified_external")],
            fills(),
            cash_entries(),
        )
        self.assertIn(
            "UNVERIFIED_ACCOUNTING_VERSION",
            {item.code for item in report.blockers},
        )

    def test_non_fill_mutation_creates_version_gap(self):
        report = audit_accounting_facts(
            [inventory(state_version=3)],
            fills(),
            cash_entries(),
        )
        self.assertIn("LEDGER_VERSION_GAP", {item.code for item in report.blockers})

    def test_cash_notional_mismatch_is_detected(self):
        rows = cash_entries()
        rows[1] = CashLedgerFact(
            event_id="fill-sell",
            market_id=MARKET,
            side="SELL",
            gross_cash_delta=2.5,
            fee_amount=0.1,
            net_cash_delta=2.4,
            fee_status="KNOWN",
        )
        report = audit_accounting_facts([inventory()], fills(), rows)
        self.assertIn("CASH_NOTIONAL_MISMATCH", {item.code for item in report.blockers})

    def test_unprocessed_fill_and_missing_cash_are_detected(self):
        pending = ProcessedFillFact(
            event_id="pending",
            status="RECEIVED",
            market_id=None,
            outcome=None,
            side=None,
            price=0.5,
            size=1.0,
            accounting_state_version=None,
        )
        report = audit_accounting_facts([], [pending], [])
        self.assertIn("UNPROCESSED_FILL", {item.code for item in report.blockers})

    def test_empty_database_is_safe(self):
        self.assertTrue(audit_accounting_facts([], [], []).safe)


if __name__ == "__main__":
    unittest.main()
