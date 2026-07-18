import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.core.trading_safety import (
    SafetyInterlockError,
    TradingMode,
    TradingSafetyGate,
    build_live_arm_token,
)


FIXED_NOW = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)
FUNDER = "0x1111111111111111111111111111111111111111"


def make_settings(**overrides):
    expires_at = (FIXED_NOW + timedelta(hours=1)).isoformat()
    values = {
        "TRADING_MODE": "live",
        "LIVE_TRADING_ENABLED": True,
        "FUNDER_ADDRESS": FUNDER,
        "LIVE_ALLOWED_FUNDER_ADDRESSES": FUNDER,
        "LIVE_ARM_EXPIRES_AT": expires_at,
        "GLOBAL_MAX_BUDGET": 50.0,
        "LIVE_BUDGET_CAP_USD": 100.0,
        "LIVE_FEE_ACCOUNTING_VALIDATED": True,
        "APP_CODE_COMMIT": "f" * 40,
        "AUTO_TUNE_FOR_REWARDS": False,
        "SINGLE_SIDE_CHEAP_ONLY": False,
        "HEDGE_ON_FILL": False,
        "OFFLINE_VALIDATED_ALPHA_ENABLED": True,
        "ALPHA_STRATEGY_ID": "maker-alpha-v2",
        "ADMIN_API_TOKEN": "a" * 32,
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
        "MARKET_DATA_MAX_AGE_SEC": 5.0,
        "MARKET_DATA_MAX_FUTURE_SKEW_SEC": 2.0,
        "MARKET_DATA_REQUIRE_SEQUENCE_LIVE": True,
        "MARKET_DATA_REQUIRE_EXCHANGE_TIMESTAMP_LIVE": True,
    }
    values.update(overrides)
    if "LIVE_ARM_TOKEN" not in overrides:
        values["LIVE_ARM_TOKEN"] = build_live_arm_token(
            values["FUNDER_ADDRESS"],
            values["LIVE_ARM_EXPIRES_AT"],
            values["GLOBAL_MAX_BUDGET"],
        )
    return SimpleNamespace(**values)


class TradingSafetyGateTests(unittest.TestCase):
    def make_gate(self, **overrides):
        return TradingSafetyGate(
            make_settings(**overrides),
            now_fn=lambda: FIXED_NOW,
        )

    def mark_all_ready(self, gate):
        for component in gate.REQUIRED_LIVE_READINESS:
            gate.set_readiness(component, True, "test ready")

    def test_legacy_live_boolean_alone_cannot_unlock(self):
        gate = self.make_gate(TRADING_MODE="disabled", LIVE_TRADING_ENABLED=True)
        self.assertEqual(gate.mode, TradingMode.DISABLED)
        self.assertFalse(gate.can_submit_live_order())
        with self.assertRaises(SafetyInterlockError):
            gate.assert_engine_start_allowed()

    def test_paper_mode_allows_engine_but_never_exchange_submission(self):
        gate = self.make_gate(TRADING_MODE="paper")
        gate.assert_engine_start_allowed()
        self.assertFalse(gate.can_submit_live_order())
        self.assertFalse(gate.can_send_exchange_cancel())

    def test_valid_live_arm_still_requires_every_runtime_component(self):
        gate = self.make_gate()
        self.assertTrue(gate.is_static_live_armed())
        self.assertFalse(gate.can_submit_live_order())
        self.mark_all_ready(gate)
        self.assertTrue(gate.can_submit_live_order())

    def test_expired_arm_blocks_new_orders_but_not_live_cancellation(self):
        expired = (FIXED_NOW - timedelta(seconds=1)).isoformat()
        gate = self.make_gate(LIVE_ARM_EXPIRES_AT=expired)
        self.mark_all_ready(gate)
        self.assertFalse(gate.can_submit_live_order())
        self.assertTrue(gate.can_send_exchange_cancel())
        self.assertIn("live arm has expired", gate.static_live_errors())

    def test_budget_above_explicit_live_cap_is_rejected(self):
        gate = self.make_gate(GLOBAL_MAX_BUDGET=101.0, LIVE_BUDGET_CAP_USD=100.0)
        self.assertIn(
            "GLOBAL_MAX_BUDGET exceeds LIVE_BUDGET_CAP_USD",
            gate.static_live_errors(),
        )

    def test_wallet_must_be_allowlisted(self):
        gate = self.make_gate(
            LIVE_ALLOWED_FUNDER_ADDRESSES="0x2222222222222222222222222222222222222222"
        )
        self.assertIn(
            "FUNDER_ADDRESS is not in LIVE_ALLOWED_FUNDER_ADDRESSES",
            gate.static_live_errors(),
        )

    def test_reward_first_spread_override_is_rejected_for_live(self):
        gate = self.make_gate(AUTO_TUNE_FOR_REWARDS=True)
        self.assertIn(
            "removed AUTO_TUNE_FOR_REWARDS flag cannot be true",
            gate.static_live_errors(),
        )

    def test_unvalidated_v8_execution_flags_are_rejected_for_live(self):
        gate = self.make_gate(SINGLE_SIDE_CHEAP_ONLY=True, HEDGE_ON_FILL=True)
        rendered = " ".join(gate.static_live_errors())
        self.assertIn("SINGLE_SIDE_CHEAP_ONLY", rendered)
        self.assertIn("HEDGE_ON_FILL", rendered)

    def test_unvalidated_fee_accounting_blocks_live(self):
        gate = self.make_gate(LIVE_FEE_ACCOUNTING_VALIDATED=False)
        self.assertIn(
            "LIVE_FEE_ACCOUNTING_VALIDATED confirmation is false",
            gate.static_live_errors(),
        )

    def test_alpha_boolean_must_be_enabled_before_live_services(self):
        gate = self.make_gate(OFFLINE_VALIDATED_ALPHA_ENABLED=False)
        self.assertIn(
            "OFFLINE_VALIDATED_ALPHA_ENABLED is false",
            gate.static_live_errors(),
        )

    def test_build_commit_and_strategy_identity_are_mandatory(self):
        gate = self.make_gate(APP_CODE_COMMIT="main", ALPHA_STRATEGY_ID="")
        rendered = " ".join(gate.static_live_errors())
        self.assertIn("APP_CODE_COMMIT", rendered)
        self.assertIn("ALPHA_STRATEGY_ID", rendered)

    def test_invalid_strategy_parameters_block_live(self):
        gate = self.make_gate(
            BASE_ORDER_SIZE=1.0,
            QUOTE_BASE_SPREAD=-0.01,
            MARKET_DATA_REQUIRE_SEQUENCE_LIVE=False,
        )
        rendered = " ".join(gate.static_live_errors())
        self.assertIn("base_order_size", rendered)
        self.assertIn("quote_base_spread", rendered)
        self.assertIn("market_data_require_sequence_live", rendered)

    def test_halt_is_sticky_for_new_orders(self):
        gate = self.make_gate()
        self.mark_all_ready(gate)
        self.assertTrue(gate.can_submit_live_order())
        gate.halt("test emergency")
        self.assertFalse(gate.can_submit_live_order())
        self.assertTrue(gate.can_send_exchange_cancel())
        self.assertTrue(gate.can_submit_live_reduce_only())
        self.assertIn("safety halt active: test emergency", gate.runtime_order_blockers())

    def test_reward_ranked_router_is_always_blocked_live(self):
        gate = self.make_gate()
        with self.assertRaises(SafetyInterlockError):
            gate.assert_router_start_allowed()

        paper_gate = self.make_gate(TRADING_MODE="paper")
        paper_gate.assert_router_start_allowed()

    def test_status_does_not_expose_tokens(self):
        gate = self.make_gate()
        rendered = repr(gate.status())
        self.assertNotIn(gate._settings.LIVE_ARM_TOKEN, rendered)
        self.assertNotIn(gate._settings.ADMIN_API_TOKEN, rendered)


if __name__ == "__main__":
    unittest.main()
