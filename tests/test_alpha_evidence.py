import unittest
from copy import deepcopy
from datetime import datetime, timezone

from app.core.alpha_evidence import (
    AlphaEvidencePolicy,
    validate_alpha_evidence,
)


NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)
REPORT_HASH = "c" * 64


def valid_report():
    return {
        "schema_version": "alpha-evidence-v2",
        "strategy_id": "maker-alpha-v2",
        "generated_at": "2026-07-17T00:00:00+00:00",
        "generator": {
            "name": "polymatrix-alpha-evaluator",
            "version": "1.1.0",
            "input_schema": "alpha-dataset-v1",
            "bootstrap_iterations": 5000,
            "bootstrap_seed": 20260718,
            "confidence_level": 0.95,
            "cluster_unit": "market_id",
            "pnl_method": "fee_aware_cash_plus_terminal_mark",
            "markout_horizon_seconds": 30,
            "rewards_included": False,
            "drawdown_method": "peak_to_trough_over_strategy_capital",
        },
        "dataset": {
            "start_at": "2026-05-01T00:00:00+00:00",
            "end_at": "2026-07-01T00:00:00+00:00",
            "fill_count": 1200,
            "market_count": 25,
            "fee_completeness": 1.0,
            "out_of_sample": True,
            "lookahead_checks_passed": True,
            "fill_event_uniqueness_passed": True,
            "market_data_integrity_passed": True,
            "source_data_sha256": "a" * 64,
        },
        "results": {
            "net_trading_pnl_ex_rewards_usd": 125.0,
            "net_trading_pnl_ex_rewards_95ci_lower_usd": 15.0,
            "markout_30s_per_share_95ci_lower": 0.001,
            "maximum_drawdown_fraction": 0.12,
        },
        "provenance": {
            "code_commit": "b" * 40,
            "config_sha256": "d" * 64,
            "runtime_source_sha256": "e" * 64,
        },
    }


class AlphaEvidenceTests(unittest.TestCase):
    def validate(self, report, **overrides):
        values = {
            "actual_sha256": REPORT_HASH,
            "expected_sha256": REPORT_HASH,
            "policy": AlphaEvidencePolicy(),
            "now": NOW,
        }
        values.update(overrides)
        return validate_alpha_evidence(report, **values)

    def test_complete_positive_out_of_sample_evidence_passes(self):
        result = self.validate(valid_report())
        self.assertTrue(result.valid)
        self.assertEqual(result.strategy_id, "maker-alpha-v2")

    def test_reward_only_or_statistically_uncertain_strategy_fails(self):
        for field, value in (
            ("net_trading_pnl_ex_rewards_usd", 0),
            ("net_trading_pnl_ex_rewards_95ci_lower_usd", -0.01),
            ("markout_30s_per_share_95ci_lower", -0.0001),
        ):
            report = valid_report()
            report["results"][field] = value
            with self.subTest(field=field):
                self.assertFalse(self.validate(report).valid)

    def test_incomplete_fees_or_failed_data_controls_fail(self):
        report = valid_report()
        report["dataset"]["fee_completeness"] = 0.999
        report["dataset"]["out_of_sample"] = False
        report["dataset"]["lookahead_checks_passed"] = False
        result = self.validate(report)
        rendered = " ".join(result.errors)
        self.assertIn("fee_completeness", rendered)
        self.assertIn("out_of_sample", rendered)
        self.assertIn("lookahead_checks_passed", rendered)

    def test_tiny_or_stale_report_fails(self):
        report = valid_report()
        report["generated_at"] = "2026-01-01T00:00:00+00:00"
        report["dataset"]["fill_count"] = 10
        report["dataset"]["market_count"] = 1
        result = self.validate(report)
        rendered = " ".join(result.errors)
        self.assertIn("stale", rendered)
        self.assertIn("fill_count", rendered)
        self.assertIn("market_count", rendered)

    def test_file_hash_and_provenance_are_mandatory(self):
        report = deepcopy(valid_report())
        report["provenance"]["code_commit"] = "main"
        result = self.validate(report, expected_sha256="e" * 64)
        rendered = " ".join(result.errors)
        self.assertIn("does not match", rendered)
        self.assertIn("full commit hash", rendered)

    def test_evidence_must_match_runtime_code_config_and_strategy(self):
        report = valid_report()
        result = self.validate(
            report,
            expected_strategy_id="different-strategy",
            expected_code_commit="c" * 40,
            expected_config_sha256="f" * 64,
            expected_runtime_source_sha256="0" * 64,
        )
        rendered = " ".join(result.errors)
        self.assertIn("armed runtime strategy", rendered)
        self.assertIn("this build", rendered)
        self.assertIn("runtime parameters", rendered)
        self.assertIn("runtime source", rendered)

    def test_drawdown_policy_is_enforced(self):
        report = valid_report()
        report["results"]["maximum_drawdown_fraction"] = 0.26
        self.assertFalse(self.validate(report).valid)

    def test_only_the_bundled_reward_excluding_evaluator_is_accepted(self):
        report = valid_report()
        report["generator"]["name"] = "spreadsheet"
        report["generator"]["rewards_included"] = True
        report["generator"]["bootstrap_iterations"] = 10
        rendered = " ".join(self.validate(report).errors)
        self.assertIn("generator.name", rendered)
        self.assertIn("rewards_included", rendered)
        self.assertIn("bootstrap_iterations", rendered)


if __name__ == "__main__":
    unittest.main()
