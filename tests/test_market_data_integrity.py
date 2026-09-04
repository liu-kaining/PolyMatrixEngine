import math
import unittest

from app.market_data.integrity import (
    BookIntegrityError,
    assess_snapshot,
    evaluate_cursor,
    extract_exchange_timestamp,
    validate_book_levels,
)
from app.market_data.gateway import LocalOrderbook


class MarketDataIntegrityTests(unittest.TestCase):
    def test_book_is_sorted_and_crossed_book_is_rejected(self):
        bids, asks = validate_book_levels(
            [{"price": "0.40", "size": "2"}, {"price": "0.42", "size": "3"}],
            [{"price": "0.47", "size": "4"}, {"price": "0.45", "size": "5"}],
        )
        self.assertEqual(float(bids[0]["price"]), 0.42)
        self.assertEqual(float(asks[0]["price"]), 0.45)
        with self.assertRaises(BookIntegrityError):
            validate_book_levels(
                [{"price": 0.5, "size": 1}],
                [{"price": 0.5, "size": 1}],
            )

    def test_non_finite_and_empty_levels_are_rejected(self):
        for bids, asks in (
            ([], [{"price": 0.6, "size": 1}]),
            ([{"price": math.nan, "size": 1}], [{"price": 0.6, "size": 1}]),
            ([{"price": 0.4, "size": 0}], [{"price": 0.6, "size": 1}]),
        ):
            with self.subTest(bids=bids, asks=asks):
                with self.assertRaises(BookIntegrityError):
                    validate_book_levels(bids, asks)

    def test_sequence_gap_requires_resync(self):
        self.assertTrue(evaluate_cursor(10, 11).accepted)
        self.assertFalse(evaluate_cursor(10, 10).accepted)
        gap = evaluate_cursor(10, 12)
        self.assertFalse(gap.accepted)
        self.assertTrue(gap.requires_resync)

    def test_timestamp_units_are_normalized(self):
        self.assertEqual(
            extract_exchange_timestamp({"timestamp": 1_700_000_000_000}),
            1_700_000_000,
        )

    def test_snapshot_age_and_required_metadata_fail_closed(self):
        base = {
            "valid": True,
            "received_at": 100.0,
            "exchange_timestamp": 99.9,
            "sequence": 5,
            "snapshot_id": "hash-1",
            "bids": [{"price": 0.4, "size": 1}],
            "asks": [{"price": 0.6, "size": 1}],
        }
        self.assertTrue(
            assess_snapshot(
                base,
                now=101,
                max_age_seconds=2,
                require_sequence=True,
                require_exchange_timestamp=True,
            ).healthy
        )
        self.assertFalse(
            assess_snapshot(
                base,
                now=103,
                max_age_seconds=2,
                require_sequence=True,
                require_exchange_timestamp=True,
            ).healthy
        )
        without_sequence = {**base, "sequence": None}
        self.assertFalse(
            assess_snapshot(
                without_sequence,
                now=101,
                max_age_seconds=2,
                require_sequence=True,
                require_exchange_timestamp=True,
            ).healthy
        )
        without_hash = {**base, "snapshot_id": None}
        self.assertFalse(
            assess_snapshot(
                without_hash,
                now=101,
                max_age_seconds=2,
                require_sequence=False,
                require_exchange_timestamp=True,
                require_snapshot_id=True,
            ).healthy
        )

    def test_tick_size_change_updates_constraints(self):
        book = LocalOrderbook()
        book.seed(
            "token",
            [{"price": 0.4, "size": 10}],
            [{"price": 0.6, "size": 10}],
            raw_metadata={
                "hash": "h1",
                "tick_size": "0.01",
                "min_order_size": "5",
                "neg_risk": False,
            },
        )
        original_received_at = book.snapshot("token")["received_at"]
        result = book.apply_event(
            {
                "event_type": "tick_size_change",
                "asset_id": "token",
                "new_tick_size": "0.001",
                "hash": "h2",
            }
        )
        self.assertEqual(result.updated_asset_ids, ["token"])
        self.assertEqual(book.snapshot("token")["tick_size"], 0.001)
        self.assertEqual(book.snapshot("token")["received_at"], original_received_at)

    def test_trade_print_never_refreshes_or_invalidates_executable_book(self):
        book = LocalOrderbook()
        book.seed(
            "token",
            [{"price": 0.4, "size": 10}],
            [{"price": 0.6, "size": 10}],
            raw_metadata={"hash": "h1"},
        )
        original_received_at = book.snapshot("token")["received_at"]
        accepted = book.apply_event(
            {
                "event_type": "last_trade_price",
                "asset_id": "token",
                "market": "market",
                "price": "0.41",
                "size": "3",
                "side": "BUY",
                "timestamp": 1_700_000_000_000,
            }
        )
        # The synthetic old timestamp is ignored as an auxiliary event and the
        # current book remains valid/freshness-neutral.
        self.assertEqual(accepted.invalid_assets, {})
        self.assertEqual(book.snapshot("token")["received_at"], original_received_at)

        accepted = book.apply_event(
            {
                "event_type": "last_trade_price",
                "asset_id": "token",
                "market": "market",
                "price": "0.41",
                "size": "3",
                "side": "BUY",
            }
        )
        self.assertEqual(accepted.updated_asset_ids, ["token"])
        self.assertEqual(book.snapshot("token")["received_at"], original_received_at)
        self.assertTrue(book.snapshot("token")["last_trade_id"])

    def test_invalid_delta_requires_a_new_full_snapshot(self):
        book = LocalOrderbook()
        first = book.apply_event(
            {
                "event_type": "book",
                "asset_id": "token",
                "sequence": 1,
                "bids": [{"price": 0.4, "size": 10}],
                "asks": [{"price": 0.6, "size": 10}],
            }
        )
        self.assertEqual(first.updated_asset_ids, ["token"])
        crossed = book.apply_event(
            {
                "event_type": "price_change",
                "sequence": 2,
                "price_changes": [
                    {"asset_id": "token", "side": "BUY", "price": 0.7, "size": 1}
                ],
            }
        )
        self.assertIn("token", crossed.invalid_assets)
        self.assertTrue(book.metadata["token"]["resync_required"])

        later_delta = book.apply_event(
            {
                "event_type": "price_change",
                "sequence": 3,
                "price_changes": [
                    {"asset_id": "token", "side": "BUY", "price": 0.3, "size": 1}
                ],
            }
        )
        self.assertEqual(later_delta.updated_asset_ids, [])
        self.assertTrue(book.metadata["token"]["resync_required"])


if __name__ == "__main__":
    unittest.main()
