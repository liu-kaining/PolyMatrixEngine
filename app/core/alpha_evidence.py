"""Strict local evidence gate for enabling a strategy that can add live risk."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from app.core.alpha_evaluator import (
    DATASET_SCHEMA_VERSION,
    EVALUATOR_NAME,
    EVALUATOR_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    MIN_BOOTSTRAP_ITERATIONS,
)
from app.core.config import settings
from app.core.strategy_fingerprint import (
    critical_source_sha256,
    runtime_strategy_config_sha256,
)
from app.core.trading_safety import trading_safety


SCHEMA_VERSION = EVIDENCE_SCHEMA_VERSION
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_REPORT_BYTES = 1_000_000


@dataclass(frozen=True)
class AlphaEvidencePolicy:
    minimum_fills: int = 1000
    minimum_markets: int = 20
    minimum_dataset_days: float = 30.0
    maximum_report_age_days: float = 30.0
    maximum_drawdown_fraction: float = 0.25


@dataclass(frozen=True)
class AlphaEvidenceResult:
    valid: bool
    errors: tuple[str, ...]
    report_sha256: Optional[str] = None
    strategy_id: Optional[str] = None


def _timestamp(value: Any, field: str, errors: list[str]) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field} must be an ISO-8601 timestamp")
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{field} must be an ISO-8601 timestamp")
        return None
    if parsed.tzinfo is None:
        errors.append(f"{field} must include a timezone")
        return None
    return parsed.astimezone(timezone.utc)


def _number(
    mapping: Mapping[str, Any], field: str, errors: list[str]
) -> Optional[float]:
    raw = mapping.get(field)
    if isinstance(raw, bool):
        errors.append(f"{field} must be a finite number")
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        errors.append(f"{field} must be a finite number")
        return None
    if not math.isfinite(value):
        errors.append(f"{field} must be a finite number")
        return None
    return value


def _integer(
    mapping: Mapping[str, Any], field: str, errors: list[str]
) -> Optional[int]:
    value = _number(mapping, field, errors)
    if value is None:
        return None
    if value < 0 or not value.is_integer():
        errors.append(f"{field} must be a non-negative integer")
        return None
    return int(value)


def validate_alpha_evidence(
    report: Mapping[str, Any],
    *,
    actual_sha256: str,
    expected_sha256: str,
    policy: AlphaEvidencePolicy,
    now: Optional[datetime] = None,
    expected_strategy_id: Optional[str] = None,
    expected_code_commit: Optional[str] = None,
    expected_config_sha256: Optional[str] = None,
    expected_runtime_source_sha256: Optional[str] = None,
) -> AlphaEvidenceResult:
    """Validate evidence without trusting a self-declared profitable flag."""
    errors: list[str] = []
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    actual_hash = str(actual_sha256 or "").strip().lower()
    expected_hash = str(expected_sha256 or "").strip().lower()
    if not SHA256_RE.fullmatch(expected_hash):
        errors.append("ALPHA_VALIDATION_REPORT_SHA256 must be a lowercase SHA-256")
    elif actual_hash != expected_hash:
        errors.append("alpha evidence file hash does not match the configured SHA-256")

    if report.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must equal {SCHEMA_VERSION}")
    strategy_id = report.get("strategy_id")
    if not isinstance(strategy_id, str) or not strategy_id.strip():
        errors.append("strategy_id is required")
        strategy_id = None
    elif expected_strategy_id is not None and strategy_id.strip() != str(
        expected_strategy_id
    ).strip():
        errors.append("strategy_id does not match the armed runtime strategy")

    generated_at = _timestamp(report.get("generated_at"), "generated_at", errors)
    if generated_at is not None:
        age_days = (current - generated_at).total_seconds() / 86400.0
        if age_days < -1e-9:
            errors.append("generated_at cannot be in the future")
        elif age_days > policy.maximum_report_age_days:
            errors.append("alpha evidence report is stale")

    generator = report.get("generator")
    if not isinstance(generator, Mapping):
        errors.append("generator must be an object")
        generator = {}
    expected_generator_values = {
        "name": EVALUATOR_NAME,
        "version": EVALUATOR_VERSION,
        "input_schema": DATASET_SCHEMA_VERSION,
        "cluster_unit": "market_id",
        "pnl_method": "fee_aware_cash_plus_terminal_mark",
        "drawdown_method": "peak_to_trough_over_strategy_capital",
    }
    for field, expected in expected_generator_values.items():
        if generator.get(field) != expected:
            errors.append(f"generator.{field} must equal {expected}")
    bootstrap_iterations = _integer(generator, "bootstrap_iterations", errors)
    _integer(generator, "bootstrap_seed", errors)
    confidence_level = _number(generator, "confidence_level", errors)
    markout_horizon = _integer(generator, "markout_horizon_seconds", errors)
    if (
        bootstrap_iterations is not None
        and bootstrap_iterations < MIN_BOOTSTRAP_ITERATIONS
    ):
        errors.append("generator.bootstrap_iterations is below evaluator minimum")
    if confidence_level is not None and abs(confidence_level - 0.95) > 1e-12:
        errors.append("generator.confidence_level must equal 0.95")
    if markout_horizon is not None and markout_horizon != 30:
        errors.append("generator.markout_horizon_seconds must equal 30")
    if generator.get("rewards_included") is not False:
        errors.append("generator.rewards_included must be false")

    dataset = report.get("dataset")
    if not isinstance(dataset, Mapping):
        errors.append("dataset must be an object")
        dataset = {}
    start_at = _timestamp(dataset.get("start_at"), "dataset.start_at", errors)
    end_at = _timestamp(dataset.get("end_at"), "dataset.end_at", errors)
    if start_at is not None and end_at is not None:
        duration_days = (end_at - start_at).total_seconds() / 86400.0
        if duration_days < policy.minimum_dataset_days:
            errors.append("out-of-sample dataset duration is below policy minimum")
        if generated_at is not None and end_at > generated_at:
            errors.append("dataset.end_at cannot be later than generated_at")

    fill_count = _integer(dataset, "fill_count", errors)
    market_count = _integer(dataset, "market_count", errors)
    fee_completeness = _number(dataset, "fee_completeness", errors)
    if fill_count is not None and fill_count < policy.minimum_fills:
        errors.append("fill_count is below policy minimum")
    if market_count is not None and market_count < policy.minimum_markets:
        errors.append("market_count is below policy minimum")
    if fee_completeness is not None and fee_completeness < 1.0 - 1e-12:
        errors.append("fee_completeness must be 1.0")
    for field in (
        "out_of_sample",
        "lookahead_checks_passed",
        "fill_event_uniqueness_passed",
        "market_data_integrity_passed",
    ):
        if dataset.get(field) is not True:
            errors.append(f"dataset.{field} must be true")
    source_hash = str(dataset.get("source_data_sha256") or "").lower()
    if not SHA256_RE.fullmatch(source_hash):
        errors.append("dataset.source_data_sha256 must be a lowercase SHA-256")

    results = report.get("results")
    if not isinstance(results, Mapping):
        errors.append("results must be an object")
        results = {}
    net_ex_rewards = _number(
        results, "net_trading_pnl_ex_rewards_usd", errors
    )
    pnl_lower = _number(
        results, "net_trading_pnl_ex_rewards_95ci_lower_usd", errors
    )
    markout_lower = _number(
        results, "markout_30s_per_share_95ci_lower", errors
    )
    drawdown_fraction = _number(results, "maximum_drawdown_fraction", errors)
    if net_ex_rewards is not None and net_ex_rewards <= 0:
        errors.append("net trading PnL excluding rewards must be positive")
    if pnl_lower is not None and pnl_lower <= 0:
        errors.append("95% lower confidence bound for trading PnL must be positive")
    if markout_lower is not None and markout_lower < 0:
        errors.append("30s markout lower confidence bound cannot be negative")
    if drawdown_fraction is not None and not (
        0 <= drawdown_fraction <= policy.maximum_drawdown_fraction
    ):
        errors.append("maximum_drawdown_fraction exceeds policy")

    provenance = report.get("provenance")
    if not isinstance(provenance, Mapping):
        errors.append("provenance must be an object")
        provenance = {}
    config_hash = str(provenance.get("config_sha256") or "").lower()
    if not SHA256_RE.fullmatch(config_hash):
        errors.append("provenance.config_sha256 must be a lowercase SHA-256")
    commit = str(provenance.get("code_commit") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        errors.append("provenance.code_commit must be a full commit hash")
    source_bundle_hash = str(
        provenance.get("runtime_source_sha256") or ""
    ).lower()
    if not SHA256_RE.fullmatch(source_bundle_hash):
        errors.append("provenance.runtime_source_sha256 must be a lowercase SHA-256")
    if expected_code_commit is not None and commit != str(expected_code_commit).lower():
        errors.append("provenance.code_commit does not match this build")
    if (
        expected_config_sha256 is not None
        and config_hash != str(expected_config_sha256).lower()
    ):
        errors.append("provenance.config_sha256 does not match runtime parameters")
    if (
        expected_runtime_source_sha256 is not None
        and source_bundle_hash != str(expected_runtime_source_sha256).lower()
    ):
        errors.append("provenance.runtime_source_sha256 does not match runtime source")

    return AlphaEvidenceResult(
        valid=not errors,
        errors=tuple(errors),
        report_sha256=actual_hash or None,
        strategy_id=strategy_id.strip() if isinstance(strategy_id, str) else None,
    )


def load_and_validate_alpha_evidence() -> AlphaEvidenceResult:
    if not bool(getattr(settings, "OFFLINE_VALIDATED_ALPHA_ENABLED", False)):
        return AlphaEvidenceResult(
            False,
            ("OFFLINE_VALIDATED_ALPHA_ENABLED is false",),
        )

    raw_path = str(getattr(settings, "ALPHA_VALIDATION_REPORT_PATH", "") or "").strip()
    if not raw_path:
        return AlphaEvidenceResult(False, ("ALPHA_VALIDATION_REPORT_PATH is empty",))
    path = Path(raw_path).expanduser()
    try:
        raw = path.read_bytes()
    except OSError:
        return AlphaEvidenceResult(False, ("alpha evidence file is not readable",))
    if len(raw) > MAX_REPORT_BYTES:
        return AlphaEvidenceResult(False, ("alpha evidence file exceeds size limit",))
    actual_hash = hashlib.sha256(raw).hexdigest()
    try:
        report = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return AlphaEvidenceResult(
            False,
            ("alpha evidence file is not valid UTF-8 JSON",),
            actual_hash,
        )
    if not isinstance(report, Mapping):
        return AlphaEvidenceResult(
            False,
            ("alpha evidence root must be an object",),
            actual_hash,
        )
    policy = AlphaEvidencePolicy(
        # Runtime configuration may tighten these floors, never weaken them.
        minimum_fills=max(
            1000, int(getattr(settings, "ALPHA_EVIDENCE_MIN_FILLS", 1000))
        ),
        minimum_markets=max(
            20, int(getattr(settings, "ALPHA_EVIDENCE_MIN_MARKETS", 20))
        ),
        minimum_dataset_days=max(
            30.0,
            float(getattr(settings, "ALPHA_EVIDENCE_MIN_DATASET_DAYS", 30.0)),
        ),
        maximum_report_age_days=min(
            30.0,
            float(getattr(settings, "ALPHA_EVIDENCE_MAX_AGE_DAYS", 30.0)),
        ),
        maximum_drawdown_fraction=min(
            0.25,
            float(
                getattr(settings, "ALPHA_EVIDENCE_MAX_DRAWDOWN_FRACTION", 0.25)
            ),
        ),
    )
    return validate_alpha_evidence(
        report,
        actual_sha256=actual_hash,
        expected_sha256=str(
            getattr(settings, "ALPHA_VALIDATION_REPORT_SHA256", "") or ""
        ),
        policy=policy,
        expected_strategy_id=str(getattr(settings, "ALPHA_STRATEGY_ID", "") or ""),
        expected_code_commit=str(getattr(settings, "APP_CODE_COMMIT", "") or ""),
        expected_config_sha256=runtime_strategy_config_sha256(settings),
        expected_runtime_source_sha256=critical_source_sha256(),
    )


def refresh_alpha_evidence_readiness() -> AlphaEvidenceResult:
    result = load_and_validate_alpha_evidence()
    detail = (
        f"offline evidence verified for strategy {result.strategy_id}"
        if result.valid
        else "; ".join(result.errors[:3])
    )
    trading_safety.set_readiness("alpha_evidence", result.valid, detail)
    if bool(getattr(settings, "OFFLINE_VALIDATED_ALPHA_ENABLED", False)) and not result.valid:
        trading_safety.halt(f"alpha evidence invalid: {detail}")
    return result
