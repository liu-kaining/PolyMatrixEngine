import unittest
from unittest.mock import AsyncMock, patch

from app.core.auto_router import _check_book_quality


class RouterBookQualityTests(unittest.IsolatedAsyncioTestCase):
    def settings_patch(self):
        return patch.multiple(
            "app.core.auto_router.settings",
            ROUTER_MIN_BOOK_DEPTH_USD=50.0,
            ROUTER_MAX_BOOK_SPREAD=0.08,
            ROUTER_AVOID_MIDPOINT_BAND=0.10,
        )

    async def assess(self, book):
        with (
            self.settings_patch(),
            patch(
                "app.core.auto_router._fetch_book_snapshot",
                new_callable=AsyncMock,
                return_value=book,
            ),
        ):
            return await _check_book_quality("token-1")

    async def test_missing_book_is_rejected(self):
        passed, reason = await self.assess(None)
        self.assertFalse(passed)
        self.assertEqual(reason, "book_unavailable")

    async def test_malformed_or_crossed_book_is_rejected(self):
        passed, reason = await self.assess(
            {"bids": [{"price": "nan", "size": "5"}], "asks": [{"price": "0.4", "size": "5"}]}
        )
        self.assertFalse(passed)
        self.assertEqual(reason, "non_finite_book")

        passed, reason = await self.assess(
            {"bids": [{"price": "0.5", "size": "5"}], "asks": [{"price": "0.4", "size": "5"}]}
        )
        self.assertFalse(passed)
        self.assertEqual(reason, "crossed_book")

    async def test_thin_or_wide_book_is_rejected(self):
        passed, reason = await self.assess(
            {"bids": [{"price": "0.30", "size": "5"}], "asks": [{"price": "0.32", "size": "5"}]}
        )
        self.assertFalse(passed)
        self.assertIn("thin_book", reason)

        passed, reason = await self.assess(
            {"bids": [{"price": "0.20", "size": "500"}], "asks": [{"price": "0.35", "size": "500"}]}
        )
        self.assertFalse(passed)
        self.assertIn("spread_too_wide", reason)

    async def test_midpoint_band_is_descriptive_policy_not_safety_claim(self):
        passed, reason = await self.assess(
            {"bids": [{"price": "0.48", "size": "500"}], "asks": [{"price": "0.52", "size": "500"}]}
        )
        self.assertFalse(passed)
        self.assertIn("midpoint_too_uncertain", reason)

    async def test_well_formed_book_passes(self):
        passed, reason = await self.assess(
            {
                "bids": [
                    {"price": "0.28", "size": "200"},
                    {"price": "0.27", "size": "200"},
                ],
                "asks": [
                    {"price": "0.30", "size": "200"},
                    {"price": "0.31", "size": "200"},
                ],
            }
        )
        self.assertTrue(passed)
        self.assertEqual(reason, "ok")


if __name__ == "__main__":
    unittest.main()
