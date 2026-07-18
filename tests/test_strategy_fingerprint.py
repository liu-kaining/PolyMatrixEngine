import unittest
from types import SimpleNamespace

from app.core.strategy_fingerprint import (
    build_runtime_strategy_config,
    critical_source_sha256,
    runtime_strategy_config_sha256,
    runtime_strategy_config_errors,
)


def settings_fixture(**overrides):
    values = {
        "ALPHA_STRATEGY_ID": "maker-alpha-v2",
        "BASE_ORDER_SIZE": 10.0,
        "GRID_LEVELS": 2,
        "QUOTE_BASE_SPREAD": 0.02,
        "QUOTE_PRICE_OFFSET_THRESHOLD": 0.01,
        "QUOTE_BID_ONE_TICK_BELOW_TOUCH": True,
        "MIN_EXPECTED_NET_EDGE": 0.02,
        "EXECUTION_COST_BUFFER": 0.002,
        "ADVERSE_SELECTION_BUFFER": 0.01,
        "EXIT_MAX_BOOK_IMPACT": 0.02,
        "EXIT_MAX_REALIZED_LOSS_FRACTION": 0.10,
        "MAX_EXPOSURE_PER_MARKET": 50.0,
        "GLOBAL_MAX_BUDGET": 100.0,
        "MARKET_DATA_MAX_AGE_SEC": 5.0,
        "MARKET_DATA_MAX_FUTURE_SKEW_SEC": 2.0,
        "MARKET_DATA_REQUIRE_SEQUENCE_LIVE": True,
        "MARKET_DATA_REQUIRE_EXCHANGE_TIMESTAMP_LIVE": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class StrategyFingerprintTests(unittest.TestCase):
    def test_runtime_config_is_canonical_and_parameter_sensitive(self):
        base = settings_fixture()
        first = runtime_strategy_config_sha256(base)
        second = runtime_strategy_config_sha256(settings_fixture())
        changed = runtime_strategy_config_sha256(
            settings_fixture(MIN_EXPECTED_NET_EDGE=0.03)
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)
        self.assertEqual(
            build_runtime_strategy_config(base)["strategy_id"], "maker-alpha-v2"
        )

    def test_critical_source_bundle_has_a_stable_sha256_shape(self):
        digest = critical_source_sha256()
        self.assertEqual(len(digest), 64)
        int(digest, 16)

    def test_runtime_strategy_ranges_are_fail_closed(self):
        self.assertEqual(runtime_strategy_config_errors(settings_fixture()), [])
        errors = runtime_strategy_config_errors(
            settings_fixture(
                BASE_ORDER_SIZE=4.99,
                MAX_EXPOSURE_PER_MARKET=101.0,
                MARKET_DATA_REQUIRE_EXCHANGE_TIMESTAMP_LIVE=False,
            )
        )
        self.assertTrue(any("base_order_size" in item for item in errors))
        self.assertTrue(any("max_exposure_per_market" in item for item in errors))
        self.assertTrue(
            any("market_data_require_exchange_timestamp_live" in item for item in errors)
        )


if __name__ == "__main__":
    unittest.main()
