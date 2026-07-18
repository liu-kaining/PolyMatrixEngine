import unittest
from copy import deepcopy
from datetime import datetime, timezone

from app.core.alpha_evaluator import AlphaEvaluationError, evaluate_alpha_dataset
from app.core.alpha_evidence import AlphaEvidencePolicy, validate_alpha_evidence


GENERATED_AT = datetime(2026, 7, 18, tzinfo=timezone.utc)


def sample_dataset():
    return {
        "schema_version": "alpha-dataset-v1",
        "strategy_id": "maker-alpha-v2",
        "training_end_at": "2026-05-31T23:59:59+00:00",
        "strategy_capital_usd": 100.0,
        "fills": [
            {
                "event_id": "a-buy",
                "market_id": "market-a",
                "token_id": "yes-a",
                "executed_at": "2026-06-01T00:00:00+00:00",
                "side": "BUY",
                "price": 0.40,
                "size": 10.0,
                "fee_amount": 0.10,
                "mark_30s_at": "2026-06-01T00:00:30+00:00",
                "mark_30s_mid": 0.45,
            },
            {
                "event_id": "a-sell",
                "market_id": "market-a",
                "token_id": "yes-a",
                "executed_at": "2026-06-02T00:00:00+00:00",
                "side": "SELL",
                "price": 0.50,
                "size": 10.0,
                "fee_amount": 0.10,
                "mark_30s_at": "2026-06-02T00:00:30+00:00",
                "mark_30s_mid": 0.48,
            },
            {
                "event_id": "b-buy",
                "market_id": "market-b",
                "token_id": "yes-b",
                "executed_at": "2026-06-03T00:00:00+00:00",
                "side": "BUY",
                "price": 0.20,
                "size": 5.0,
                "fee_amount": 0.0,
                "mark_30s_at": "2026-06-03T00:00:30+00:00",
                "mark_30s_mid": 0.25,
            },
        ],
        "terminal_marks": [
            {
                "market_id": "market-b",
                "token_id": "yes-b",
                "marked_at": "2026-06-03T01:00:00+00:00",
                "mid": 0.30,
            }
        ],
        "equity_curve": [
            {
                "timestamp": "2026-05-31T23:59:59+00:00",
                "strategy_equity_usd": 100.0,
            },
            {
                "timestamp": "2026-06-01T12:00:00+00:00",
                "strategy_equity_usd": 102.0,
            },
            {
                "timestamp": "2026-06-02T12:00:00+00:00",
                "strategy_equity_usd": 95.0,
            },
            {
                "timestamp": "2026-06-03T00:00:01+00:00",
                "strategy_equity_usd": 101.0,
            },
        ],
    }


class AlphaEvaluatorTests(unittest.TestCase):
    def evaluate(self, dataset):
        return evaluate_alpha_dataset(
            dataset,
            source_data_sha256="a" * 64,
            config_sha256="b" * 64,
            runtime_source_sha256="e" * 64,
            code_commit="c" * 40,
            generated_at=GENERATED_AT,
            bootstrap_iterations=5000,
            bootstrap_seed=7,
        )

    def test_computes_fee_aware_reward_excluding_evidence(self):
        report = self.evaluate(sample_dataset())
        self.assertEqual(report["schema_version"], "alpha-evidence-v2")
        self.assertIs(report["generator"]["rewards_included"], False)
        self.assertAlmostEqual(
            report["results"]["net_trading_pnl_ex_rewards_usd"], 1.3
        )
        self.assertAlmostEqual(report["results"]["markout_30s_per_share"], 0.03)
        self.assertAlmostEqual(report["results"]["maximum_drawdown_fraction"], 0.07)
        self.assertGreater(
            report["results"]["net_trading_pnl_ex_rewards_95ci_lower_usd"], 0
        )
        self.assertGreaterEqual(
            report["results"]["markout_30s_per_share_95ci_lower"], 0
        )

        validation = validate_alpha_evidence(
            report,
            actual_sha256="d" * 64,
            expected_sha256="d" * 64,
            policy=AlphaEvidencePolicy(
                minimum_fills=3,
                minimum_markets=2,
                minimum_dataset_days=1,
                maximum_report_age_days=30,
                maximum_drawdown_fraction=0.25,
            ),
            now=GENERATED_AT,
        )
        self.assertTrue(validation.valid, validation.errors)

    def test_rejects_reward_fields_in_strict_source_schema(self):
        dataset = sample_dataset()
        dataset["fills"][0]["reward_amount"] = 1000
        with self.assertRaisesRegex(AlphaEvaluationError, "unknown fields"):
            self.evaluate(dataset)

    def test_rejects_duplicate_fills_and_lookahead_violations(self):
        duplicate = sample_dataset()
        duplicate["fills"][1]["event_id"] = "a-buy"
        with self.assertRaisesRegex(AlphaEvaluationError, "unique"):
            self.evaluate(duplicate)

        early_mark = sample_dataset()
        early_mark["fills"][0]["mark_30s_at"] = "2026-06-01T00:00:29+00:00"
        with self.assertRaisesRegex(AlphaEvaluationError, "30-35s"):
            self.evaluate(early_mark)

    def test_rejects_oversells_and_unmarked_open_inventory(self):
        oversell = sample_dataset()
        oversell["fills"][1]["size"] = 11.0
        with self.assertRaisesRegex(AlphaEvaluationError, "exceeds evaluated inventory"):
            self.evaluate(oversell)

        missing_mark = sample_dataset()
        missing_mark["terminal_marks"] = []
        with self.assertRaisesRegex(AlphaEvaluationError, "lacks terminal mark"):
            self.evaluate(missing_mark)

    def test_rejects_equity_curves_that_hide_capital_or_time(self):
        wrong_capital = sample_dataset()
        wrong_capital["equity_curve"][0]["strategy_equity_usd"] = 1000.0
        with self.assertRaisesRegex(AlphaEvaluationError, "must equal strategy_capital"):
            self.evaluate(wrong_capital)

        uncovered = deepcopy(sample_dataset())
        uncovered["equity_curve"][-1]["timestamp"] = "2026-06-02T23:59:59+00:00"
        with self.assertRaisesRegex(AlphaEvaluationError, "complete evaluated fill interval"):
            self.evaluate(uncovered)


if __name__ == "__main__":
    unittest.main()
