import math
import unittest

from app.oms.order_reconciliation import (
    ExchangeOrderParseError,
    LocalOrderFact,
    extract_fills_for_order,
    extract_unsettled_size_for_order,
    normalize_exchange_order,
    OrderReconciliationService,
    reconcile_order_facts,
)


def exchange_payload(**overrides):
    payload = {
        "id": "ex-1",
        "market": "market-1",
        "asset_id": "token-1",
        "side": "BUY",
        "price": "0.40",
        "original_size": "10",
        "size_matched": "2",
        "status": "LIVE",
        "_size_encoding": "human",
    }
    payload.update(overrides)
    return payload


def local_fact(**overrides):
    values = {
        "local_order_id": "local-1",
        "exchange_order_id": "ex-1",
        "market_id": "market-1",
        "token_id": "token-1",
        "side": "BUY",
        "price": 0.40,
        "original_size": 10.0,
        "locally_filled_size": 2.0,
        "status": "OPEN",
        "reservation_id": "reservation-1",
        "reservation_market_id": "market-1",
        "reservation_token_id": "token-1",
        "reservation_side": "BUY",
        "reservation_limit_price": 0.40,
        "reservation_original_size": 10.0,
        "reservation_status": "PARTIAL",
        "reservation_remaining_size": 8.0,
        "reservation_notional": 3.2,
    }
    values.update(overrides)
    return LocalOrderFact(**values)


class OrderReconciliationTests(unittest.TestCase):
    def test_open_order_with_matching_fills_is_confirmed(self):
        exchange = normalize_exchange_order(exchange_payload())
        report = reconcile_order_facts([local_fact()], [exchange], {})
        self.assertTrue(report.safe)
        self.assertEqual(report.actions[0].kind, "OPEN_CONFIRMED")
        self.assertEqual(report.actions[0].exchange_remaining_size, 8.0)

    def test_absence_from_open_list_requires_terminal_detail(self):
        report = reconcile_order_facts([local_fact()], [], {})
        self.assertFalse(report.safe)
        self.assertEqual(report.actions[0].kind, "UNKNOWN")

        canceled = normalize_exchange_order(exchange_payload(status="CANCELED"))
        report = reconcile_order_facts([local_fact()], [], {"ex-1": canceled})
        self.assertTrue(report.safe)
        self.assertEqual(report.actions[0].kind, "CANCELED_CONFIRMED")

    def test_missing_fill_is_never_released_or_guessed(self):
        exchange = normalize_exchange_order(exchange_payload(size_matched="3"))
        report = reconcile_order_facts([local_fact()], [exchange], {})
        self.assertFalse(report.safe)
        self.assertEqual(report.actions[0].kind, "MISSING_FILLS")

    def test_unsettled_fill_temporarily_blocks_without_sticky_incident(self):
        exchange = normalize_exchange_order(exchange_payload(size_matched="3"))
        report = reconcile_order_facts(
            [local_fact()], [exchange], {}, unsettled_sizes={"ex-1": 1.0}
        )
        self.assertFalse(report.safe)
        self.assertEqual(report.actions[0].kind, "SETTLEMENT_PENDING")
        self.assertFalse(report.actions[0].sticky)
        self.assertEqual(report.sticky_blockers, ())

    def test_only_confirmed_trade_becomes_recovered_fill(self):
        trade = {
            "id": "trade-1",
            "status": "MINED",
            "taker_order_id": "ex-1",
            "asset_id": "token-1",
            "size": "1.25",
            "price": "0.4",
            "fee_rate_bps": "700",
            "maker_orders": [],
            "_size_encoding": "human",
        }
        self.assertEqual(extract_fills_for_order([trade], "ex-1"), [])
        self.assertEqual(extract_unsettled_size_for_order([trade], "ex-1"), 1.25)
        trade["status"] = "CONFIRMED"
        self.assertEqual(len(extract_fills_for_order([trade], "ex-1")), 1)
        self.assertEqual(extract_unsettled_size_for_order([trade], "ex-1"), 0.0)

    def test_external_open_order_and_identity_conflict_block(self):
        external = normalize_exchange_order(exchange_payload(id="orphan"))
        orphan_report = reconcile_order_facts([], [external], {})
        self.assertEqual(orphan_report.actions[0].kind, "EXTERNAL_ORPHAN")
        self.assertFalse(orphan_report.safe)

        conflict = normalize_exchange_order(exchange_payload(asset_id="different"))
        conflict_report = reconcile_order_facts([local_fact()], [conflict], {})
        self.assertEqual(conflict_report.actions[0].kind, "CONFLICT")
        self.assertFalse(conflict_report.safe)

        reservation_conflict = reconcile_order_facts(
            [local_fact(reservation_limit_price=0.39)],
            [normalize_exchange_order(exchange_payload())],
            {},
        )
        self.assertEqual(reservation_conflict.actions[0].kind, "CONFLICT")

    def test_full_fill_requires_local_accounting_and_zero_reservation(self):
        filled = normalize_exchange_order(
            exchange_payload(size_matched="10", status="MATCHED")
        )
        matching_local = local_fact(
            locally_filled_size=10,
            reservation_remaining_size=0,
            reservation_notional=0,
            reservation_status="FILLED",
        )
        report = reconcile_order_facts([matching_local], [], {"ex-1": filled})
        self.assertTrue(report.safe)
        self.assertEqual(report.actions[0].kind, "FILLED_CONFIRMED")

    def test_invalid_exchange_numbers_fail_closed(self):
        for value in (math.nan, math.inf, -1):
            with self.subTest(value=value):
                with self.assertRaises(ExchangeOrderParseError):
                    normalize_exchange_order(exchange_payload(price=value))


class FakeExchangeAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_terminal_detail_only_when_order_is_not_open(self):
        class FakeClient:
            def __init__(self):
                self.detail_calls = []

            def get_orders(self):
                return [exchange_payload(id="ex-open")]

            def get_order(self, order_id):
                self.detail_calls.append(order_id)
                return exchange_payload(id=order_id, status="CANCELED")

        open_local = local_fact(
            local_order_id="local-open", exchange_order_id="ex-open"
        )
        closed_local = local_fact(
            local_order_id="local-closed", exchange_order_id="ex-closed"
        )
        client = FakeClient()
        service = OrderReconciliationService()
        open_facts, details, raw_open, raw_details = await service._fetch_exchange_facts(
            client, [open_local, closed_local]
        )
        self.assertEqual([fact.exchange_order_id for fact in open_facts], ["ex-open"])
        self.assertEqual(set(details), {"ex-closed"})
        self.assertEqual(client.detail_calls, ["ex-closed"])
        self.assertEqual(len(raw_open), 1)
        self.assertEqual(set(raw_details), {"ex-closed"})

    async def test_malformed_fake_exchange_response_fails_closed(self):
        class FakeClient:
            def get_orders(self):
                return {"data": [], "next_cursor": "more-pages-remain"}

        with self.assertRaises(ExchangeOrderParseError):
            await OrderReconciliationService()._fetch_exchange_facts(FakeClient(), [])


if __name__ == "__main__":
    unittest.main()
