import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RemovedUnsafePathTests(unittest.TestCase):
    def test_wallet_wide_hard_reset_cannot_return_silently(self):
        runtime_source = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in (
                "app/quoting/engine.py",
                "app/oms/core.py",
                "app/core/config.py",
                "docker-compose.yml",
            )
        )
        for forbidden in (
            "physical_clob_cancel_all_for_hard_reset",
            "cancel_all_orders(force_evict=True)",
            "PERIODIC_HARD_RESET_ENABLED",
            "HARD_RESET_CLOB_CANCEL_ALL_ENABLED",
            "POST_RESET_RECONCILE_FREEZE",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, runtime_source)

    def test_unbounded_liquidation_price_is_absent_from_runtime(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "app").rglob("*.py")
        )
        self.assertNotIn("liquidation_price = 0.01", source)
        self.assertNotIn("price=0.01", source)

    def test_reward_inputs_cannot_tune_execution_or_unlock_live_router(self):
        runtime_source = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in (
                "app/quoting/engine.py",
                "app/core/config.py",
                "app/core/trading_safety.py",
                ".env.example",
                "docker-compose.yml",
            )
        )
        self.assertNotIn("target_spread = self.rewards_max_spread", runtime_source)
        self.assertNotIn("target_size = max(self.base_size, rewards_target)", runtime_source)
        self.assertNotIn("kept_for_rewards_band", runtime_source)

    def test_memory_first_fill_persistence_path_is_removed(self):
        source = (ROOT / "app/core/inventory_state.py").read_text(encoding="utf-8")
        self.assertNotIn("async def apply_fill(", source)
        self.assertNotIn("_persist_queue", source)
        self.assertNotIn("_persist_worker", source)

    def test_reward_ranked_dashboard_cannot_launch_execution(self):
        source = (ROOT / "dashboard/app.py").read_text(encoding="utf-8")
        self.assertNotIn("start_from_screener", source)
        self.assertNotIn("pending_screener_start_cid", source)
        self.assertNotIn("recommendation_score", source)

    def test_unvalidated_v8_execution_paths_are_removed(self):
        runtime_source = "\n".join(
            (ROOT / path).read_text(encoding="utf-8")
            for path in (
                "app/quoting/engine.py",
                "app/market_data/user_stream.py",
                "app/core/inventory_state.py",
                "app/risk/watchdog.py",
                "app/main.py",
            )
        )
        for forbidden in (
            "accumulate_unhedged_fill",
            "hedge_sell_pending",
            "CHEAP_SIDE_GATE",
            "rewards_ceil",
            "PER_MARKET_STOP_LOSS_USD",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, runtime_source)


if __name__ == "__main__":
    unittest.main()
