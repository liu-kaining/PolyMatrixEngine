"""Deterministic, offline-only alpha evidence generator.

The evaluator accepts a strict JSON dataset, excludes rewards by construction, and computes
fee-aware mark-to-market PnL, 30-second markout, market-cluster bootstrap confidence bounds,
and strategy-capital drawdown. It never imports an exchange adapter or performs network I/O.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.core.strategy_fingerprint import critical_source_sha256, sha256_mapping


DATASET_SCHEMA_VERSION = "alpha-dataset-v1"
EVIDENCE_SCHEMA_VERSION = "alpha-evidence-v2"
EVALUATOR_NAME = "polymatrix-alpha-evaluator"
EVALUATOR_VERSION = "1.1.0"
MIN_BOOTSTRAP_ITERATIONS = 5000
DEFAULT_BOOTSTRAP_SEED = 20260718
MAX_MARKOUT_DELAY_SECONDS = 35.0
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")


class AlphaEvaluationError(ValueError):
    """Raised when an offline dataset cannot support trustworthy evaluation."""


@dataclass(frozen=True)
class _Fill:
    event_id: str
    market_id: str
    token_id: str
    executed_at: datetime
    side: str
    price: float
    size: float
    fee_amount: float
    mark_30s_at: datetime
    mark_30s_mid: float


@dataclass(frozen=True)
class _MarketResult:
    pnl: float
    markout_value: float
    volume: float


def _strict_keys(mapping: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    missing = sorted(allowed - set(mapping))
    if unknown:
        raise AlphaEvaluationError(f"{label} has unknown fields: {', '.join(unknown)}")
    if missing:
        raise AlphaEvaluationError(f"{label} is missing fields: {', '.join(missing)}")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AlphaEvaluationError(f"{label} must be an object")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise AlphaEvaluationError(f"{label} must be an array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AlphaEvaluationError(f"{label} must be a non-empty string")
    return value.strip()


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise AlphaEvaluationError(f"{label} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise AlphaEvaluationError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise AlphaEvaluationError(f"{label} must be a finite number")
    return result


def _timestamp(value: Any, label: str) -> datetime:
    raw = _text(value, label)
    try:
        result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AlphaEvaluationError(f"{label} must be an ISO-8601 timestamp") from exc
    if result.tzinfo is None:
        raise AlphaEvaluationError(f"{label} must include a timezone")
    return result.astimezone(timezone.utc)


def _parse_fill(raw_value: Any, index: int) -> _Fill:
    label = f"fills[{index}]"
    raw = _mapping(raw_value, label)
    _strict_keys(
        raw,
        {
            "event_id",
            "market_id",
            "token_id",
            "executed_at",
            "side",
            "price",
            "size",
            "fee_amount",
            "mark_30s_at",
            "mark_30s_mid",
        },
        label,
    )
    side = _text(raw["side"], f"{label}.side").upper()
    if side not in {"BUY", "SELL"}:
        raise AlphaEvaluationError(f"{label}.side must be BUY or SELL")
    price = _number(raw["price"], f"{label}.price")
    size = _number(raw["size"], f"{label}.size")
    fee = _number(raw["fee_amount"], f"{label}.fee_amount")
    mark = _number(raw["mark_30s_mid"], f"{label}.mark_30s_mid")
    if not 0.0 < price < 1.0 or not 0.0 <= mark <= 1.0:
        raise AlphaEvaluationError(f"{label} price and mark must be valid probabilities")
    if size <= 0 or fee < 0:
        raise AlphaEvaluationError(f"{label} size must be positive and fee non-negative")
    executed_at = _timestamp(raw["executed_at"], f"{label}.executed_at")
    marked_at = _timestamp(raw["mark_30s_at"], f"{label}.mark_30s_at")
    delay = (marked_at - executed_at).total_seconds()
    if not 30.0 <= delay <= MAX_MARKOUT_DELAY_SECONDS:
        raise AlphaEvaluationError(
            f"{label} mark must be captured 30-{MAX_MARKOUT_DELAY_SECONDS:g}s after execution"
        )
    return _Fill(
        event_id=_text(raw["event_id"], f"{label}.event_id"),
        market_id=_text(raw["market_id"], f"{label}.market_id"),
        token_id=_text(raw["token_id"], f"{label}.token_id"),
        executed_at=executed_at,
        side=side,
        price=price,
        size=size,
        fee_amount=fee,
        mark_30s_at=marked_at,
        mark_30s_mid=mark,
    )


def _percentile_lower(values: list[float], probability: float) -> float:
    if not values:
        raise AlphaEvaluationError("bootstrap produced no samples")
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.floor(probability * len(ordered))))
    return ordered[index]


def _bootstrap_lower_bounds(
    market_results: Mapping[str, _MarketResult], *, iterations: int, seed: int
) -> tuple[float, float]:
    markets = sorted(market_results)
    if not markets:
        raise AlphaEvaluationError("dataset has no markets")
    rng = random.Random(seed)
    pnl_samples: list[float] = []
    markout_samples: list[float] = []
    for _ in range(iterations):
        pnl = 0.0
        markout = 0.0
        volume = 0.0
        for _ in markets:
            selected = market_results[rng.choice(markets)]
            pnl += selected.pnl
            markout += selected.markout_value
            volume += selected.volume
        pnl_samples.append(pnl)
        markout_samples.append(markout / volume if volume > 0 else -math.inf)
    return (
        _percentile_lower(pnl_samples, 0.025),
        _percentile_lower(markout_samples, 0.025),
    )


def evaluate_alpha_dataset(
    dataset: Mapping[str, Any],
    *,
    source_data_sha256: str,
    config_sha256: str,
    runtime_source_sha256: str,
    code_commit: str,
    generated_at: datetime | None = None,
    bootstrap_iterations: int = MIN_BOOTSTRAP_ITERATIONS,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Validate raw offline observations and return a deterministic evidence report."""
    _strict_keys(
        dataset,
        {
            "schema_version",
            "strategy_id",
            "training_end_at",
            "strategy_capital_usd",
            "fills",
            "terminal_marks",
            "equity_curve",
        },
        "dataset",
    )
    if dataset["schema_version"] != DATASET_SCHEMA_VERSION:
        raise AlphaEvaluationError(f"dataset.schema_version must equal {DATASET_SCHEMA_VERSION}")
    source_hash = str(source_data_sha256 or "").lower()
    strategy_config_hash = str(config_sha256 or "").lower()
    source_bundle_hash = str(runtime_source_sha256 or "").lower()
    commit = str(code_commit or "").lower()
    if not SHA256_RE.fullmatch(source_hash):
        raise AlphaEvaluationError("source_data_sha256 must be a lowercase SHA-256")
    if not SHA256_RE.fullmatch(strategy_config_hash):
        raise AlphaEvaluationError("config_sha256 must be a lowercase SHA-256")
    if not SHA256_RE.fullmatch(source_bundle_hash):
        raise AlphaEvaluationError("runtime_source_sha256 must be a lowercase SHA-256")
    if not COMMIT_RE.fullmatch(commit):
        raise AlphaEvaluationError("code_commit must be a full commit hash")
    if isinstance(bootstrap_iterations, bool) or bootstrap_iterations < MIN_BOOTSTRAP_ITERATIONS:
        raise AlphaEvaluationError(
            f"bootstrap_iterations must be at least {MIN_BOOTSTRAP_ITERATIONS}"
        )
    if isinstance(bootstrap_seed, bool) or not isinstance(bootstrap_seed, int):
        raise AlphaEvaluationError("bootstrap_seed must be an integer")

    strategy_id = _text(dataset["strategy_id"], "dataset.strategy_id")
    training_end = _timestamp(dataset["training_end_at"], "dataset.training_end_at")
    strategy_capital = _number(
        dataset["strategy_capital_usd"], "dataset.strategy_capital_usd"
    )
    if strategy_capital <= 0:
        raise AlphaEvaluationError("dataset.strategy_capital_usd must be positive")

    fills = [_parse_fill(value, index) for index, value in enumerate(
        _sequence(dataset["fills"], "dataset.fills")
    )]
    if not fills:
        raise AlphaEvaluationError("dataset.fills cannot be empty")
    fills.sort(key=lambda fill: (fill.executed_at, fill.event_id))
    event_ids = [fill.event_id for fill in fills]
    if len(set(event_ids)) != len(event_ids):
        raise AlphaEvaluationError("fill event_id values must be unique")
    start_at = fills[0].executed_at
    end_at = fills[-1].executed_at
    if training_end >= start_at:
        raise AlphaEvaluationError("training_end_at must be earlier than every evaluated fill")

    terminal_marks: dict[tuple[str, str], tuple[datetime, float]] = {}
    for index, raw_value in enumerate(
        _sequence(dataset["terminal_marks"], "dataset.terminal_marks")
    ):
        label = f"terminal_marks[{index}]"
        raw = _mapping(raw_value, label)
        _strict_keys(raw, {"market_id", "token_id", "marked_at", "mid"}, label)
        key = (
            _text(raw["market_id"], f"{label}.market_id"),
            _text(raw["token_id"], f"{label}.token_id"),
        )
        if key in terminal_marks:
            raise AlphaEvaluationError(f"duplicate terminal mark for {key[0]}/{key[1]}")
        marked_at = _timestamp(raw["marked_at"], f"{label}.marked_at")
        mid = _number(raw["mid"], f"{label}.mid")
        if not 0.0 <= mid <= 1.0:
            raise AlphaEvaluationError(f"{label}.mid must be between 0 and 1")
        terminal_marks[key] = (marked_at, mid)

    inventory: dict[tuple[str, str], float] = {}
    last_fill_at: dict[tuple[str, str], datetime] = {}
    cash_by_market: dict[str, float] = {}
    markout_by_market: dict[str, float] = {}
    volume_by_market: dict[str, float] = {}
    total_fees = 0.0
    for fill in fills:
        key = (fill.market_id, fill.token_id)
        held = inventory.get(key, 0.0)
        if fill.side == "BUY":
            held += fill.size
            cash_delta = -(fill.price * fill.size) - fill.fee_amount
            markout = (fill.mark_30s_mid - fill.price) * fill.size - fill.fee_amount
        else:
            if fill.size > held + 1e-9:
                raise AlphaEvaluationError(
                    f"SELL {fill.event_id} exceeds evaluated inventory for {fill.market_id}/{fill.token_id}"
                )
            held = max(0.0, held - fill.size)
            cash_delta = (fill.price * fill.size) - fill.fee_amount
            markout = (fill.price - fill.mark_30s_mid) * fill.size - fill.fee_amount
        inventory[key] = held
        last_fill_at[key] = fill.executed_at
        cash_by_market[fill.market_id] = cash_by_market.get(fill.market_id, 0.0) + cash_delta
        markout_by_market[fill.market_id] = (
            markout_by_market.get(fill.market_id, 0.0) + markout
        )
        volume_by_market[fill.market_id] = volume_by_market.get(fill.market_id, 0.0) + fill.size
        total_fees += fill.fee_amount

    terminal_value_by_market: dict[str, float] = {}
    open_inventory_markets: set[str] = set()
    for key, held in inventory.items():
        if held <= 1e-9:
            continue
        if key not in terminal_marks:
            raise AlphaEvaluationError(f"open inventory lacks terminal mark for {key[0]}/{key[1]}")
        marked_at, mid = terminal_marks[key]
        if marked_at < last_fill_at[key]:
            raise AlphaEvaluationError(f"terminal mark predates last fill for {key[0]}/{key[1]}")
        terminal_value_by_market[key[0]] = (
            terminal_value_by_market.get(key[0], 0.0) + held * mid
        )
        open_inventory_markets.add(key[0])

    markets = sorted(cash_by_market)
    market_results = {
        market_id: _MarketResult(
            pnl=cash_by_market[market_id] + terminal_value_by_market.get(market_id, 0.0),
            markout_value=markout_by_market[market_id],
            volume=volume_by_market[market_id],
        )
        for market_id in markets
    }
    net_pnl = sum(item.pnl for item in market_results.values())
    total_volume = sum(item.volume for item in market_results.values())
    markout_per_share = sum(item.markout_value for item in market_results.values()) / total_volume
    pnl_lower, markout_lower = _bootstrap_lower_bounds(
        market_results, iterations=bootstrap_iterations, seed=bootstrap_seed
    )

    equity_points: list[tuple[datetime, float]] = []
    for index, raw_value in enumerate(
        _sequence(dataset["equity_curve"], "dataset.equity_curve")
    ):
        label = f"equity_curve[{index}]"
        raw = _mapping(raw_value, label)
        _strict_keys(raw, {"timestamp", "strategy_equity_usd"}, label)
        timestamp = _timestamp(raw["timestamp"], f"{label}.timestamp")
        equity = _number(raw["strategy_equity_usd"], f"{label}.strategy_equity_usd")
        if equity < 0:
            raise AlphaEvaluationError(f"{label}.strategy_equity_usd cannot be negative")
        if equity_points and timestamp <= equity_points[-1][0]:
            raise AlphaEvaluationError("equity_curve timestamps must be strictly increasing")
        equity_points.append((timestamp, equity))
    if len(equity_points) < 2:
        raise AlphaEvaluationError("equity_curve must contain at least two points")
    if equity_points[0][0] > start_at or equity_points[-1][0] < end_at:
        raise AlphaEvaluationError("equity_curve must cover the complete evaluated fill interval")
    if abs(equity_points[0][1] - strategy_capital) > 0.01:
        raise AlphaEvaluationError(
            "first strategy equity must equal strategy_capital_usd within one cent"
        )
    peak = equity_points[0][1]
    max_drawdown = 0.0
    for _, equity in equity_points:
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    max_drawdown_fraction = max_drawdown / strategy_capital

    created = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "strategy_id": strategy_id,
        "generated_at": created.isoformat(),
        "generator": {
            "name": EVALUATOR_NAME,
            "version": EVALUATOR_VERSION,
            "input_schema": DATASET_SCHEMA_VERSION,
            "bootstrap_iterations": bootstrap_iterations,
            "bootstrap_seed": bootstrap_seed,
            "confidence_level": 0.95,
            "cluster_unit": "market_id",
            "pnl_method": "fee_aware_cash_plus_terminal_mark",
            "markout_horizon_seconds": 30,
            "rewards_included": False,
            "drawdown_method": "peak_to_trough_over_strategy_capital",
        },
        "dataset": {
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
            "fill_count": len(fills),
            "market_count": len(markets),
            "fee_completeness": 1.0,
            "out_of_sample": True,
            "lookahead_checks_passed": True,
            "fill_event_uniqueness_passed": True,
            "market_data_integrity_passed": True,
            "source_data_sha256": source_hash,
        },
        "results": {
            "net_trading_pnl_ex_rewards_usd": net_pnl,
            "net_trading_pnl_ex_rewards_95ci_lower_usd": pnl_lower,
            "markout_30s_per_share": markout_per_share,
            "markout_30s_per_share_95ci_lower": markout_lower,
            "maximum_drawdown_fraction": max_drawdown_fraction,
            "total_fees_usd": total_fees,
            "open_inventory_market_count": len(open_inventory_markets),
        },
        "provenance": {
            "code_commit": commit,
            "config_sha256": strategy_config_hash,
            "runtime_source_sha256": source_bundle_hash,
        },
    }


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _main() -> None:
    parser = argparse.ArgumentParser(description="Generate offline PolyMatrix alpha evidence")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--strategy-config", required=True, type=Path)
    parser.add_argument("--code-commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--bootstrap-iterations", type=int, default=MIN_BOOTSTRAP_ITERATIONS)
    parser.add_argument("--bootstrap-seed", type=int, default=DEFAULT_BOOTSTRAP_SEED)
    args = parser.parse_args()

    dataset_raw = args.dataset.read_bytes()
    config_raw = args.strategy_config.read_bytes()
    dataset = json.loads(dataset_raw)
    strategy_config = json.loads(config_raw)
    if not isinstance(dataset, Mapping):
        raise AlphaEvaluationError("dataset root must be an object")
    if not isinstance(strategy_config, Mapping):
        raise AlphaEvaluationError("strategy config root must be an object")
    report = evaluate_alpha_dataset(
        dataset,
        source_data_sha256=_sha256(dataset_raw),
        config_sha256=sha256_mapping(strategy_config),
        runtime_source_sha256=critical_source_sha256(),
        code_commit=args.code_commit,
        bootstrap_iterations=args.bootstrap_iterations,
        bootstrap_seed=args.bootstrap_seed,
    )
    serialized = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    args.output.write_text(serialized, encoding="utf-8")
    print(f"evidence_sha256={_sha256(serialized.encode('utf-8'))}")


if __name__ == "__main__":
    _main()
