"""Canonical runtime strategy/config and critical-source fingerprints."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, List, Mapping


STRATEGY_CONFIG_SCHEMA_VERSION = "alpha-runtime-config-v1"
CRITICAL_SOURCE_FILES = (
    "app/main.py",
    "app/quoting/engine.py",
    "app/core/accounting.py",
    "app/core/accounting_integrity.py",
    "app/core/cash_accounting.py",
    "app/core/config.py",
    "app/core/inventory_state.py",
    "app/core/market_lifecycle.py",
    "app/core/position_reconciliation.py",
    "app/core/quote_economics.py",
    "app/core/exit_policy.py",
    "app/core/strategy_fingerprint.py",
    "app/core/trading_safety.py",
    "app/core/alpha_evidence.py",
    "app/core/alpha_evaluator.py",
    "app/oms/core.py",
    "app/oms/fill_processor.py",
    "app/oms/order_reconciliation.py",
    "app/oms/validation.py",
    "app/risk/reservations.py",
    "app/risk/watchdog.py",
    "app/market_data/gateway.py",
    "app/market_data/integrity.py",
    "app/market_data/user_stream.py",
)


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_mapping(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def build_runtime_strategy_config(settings_obj: Any) -> dict[str, Any]:
    """Select every runtime setting that changes quote economics or risk behavior."""
    config = {
        "schema_version": STRATEGY_CONFIG_SCHEMA_VERSION,
        "strategy_id": str(getattr(settings_obj, "ALPHA_STRATEGY_ID", "") or ""),
        "model": "binary_obi_mid_v1",
        "parameters": {
            "base_order_size": float(settings_obj.BASE_ORDER_SIZE),
            "grid_levels": int(settings_obj.GRID_LEVELS),
            "quote_base_spread": float(settings_obj.QUOTE_BASE_SPREAD),
            "quote_price_offset_threshold": float(
                settings_obj.QUOTE_PRICE_OFFSET_THRESHOLD
            ),
            "quote_bid_one_tick_below_touch": bool(
                settings_obj.QUOTE_BID_ONE_TICK_BELOW_TOUCH
            ),
            "minimum_expected_net_edge": float(settings_obj.MIN_EXPECTED_NET_EDGE),
            "execution_cost_buffer": float(settings_obj.EXECUTION_COST_BUFFER),
            "adverse_selection_buffer": float(settings_obj.ADVERSE_SELECTION_BUFFER),
            "exit_max_book_impact": float(settings_obj.EXIT_MAX_BOOK_IMPACT),
            "exit_max_realized_loss_fraction": float(
                settings_obj.EXIT_MAX_REALIZED_LOSS_FRACTION
            ),
            "max_exposure_per_market": float(settings_obj.MAX_EXPOSURE_PER_MARKET),
            "global_max_budget": float(settings_obj.GLOBAL_MAX_BUDGET),
            "market_data_max_age_sec": float(settings_obj.MARKET_DATA_MAX_AGE_SEC),
            "market_data_max_future_skew_sec": float(
                settings_obj.MARKET_DATA_MAX_FUTURE_SKEW_SEC
            ),
            "market_data_require_sequence_live": bool(
                settings_obj.MARKET_DATA_REQUIRE_SEQUENCE_LIVE
            ),
            "market_data_require_exchange_timestamp_live": bool(
                settings_obj.MARKET_DATA_REQUIRE_EXCHANGE_TIMESTAMP_LIVE
            ),
        },
    }
    numeric_values = (
        value
        for value in config["parameters"].values()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    )
    if not all(math.isfinite(float(value)) for value in numeric_values):
        raise ValueError("runtime strategy parameters must be finite")
    return config


def runtime_strategy_config_sha256(settings_obj: Any) -> str:
    return sha256_mapping(build_runtime_strategy_config(settings_obj))


def runtime_strategy_config_errors(settings_obj: Any) -> List[str]:
    """Validate safety-critical strategy ranges before live startup."""
    try:
        parameters = build_runtime_strategy_config(settings_obj)["parameters"]
    except (AttributeError, TypeError, ValueError) as exc:
        return [f"runtime strategy configuration is invalid: {exc}"]

    errors: List[str] = []

    def require_range(
        name: str,
        *,
        minimum: float,
        maximum: float,
        include_minimum: bool = True,
        include_maximum: bool = True,
    ) -> None:
        value = float(parameters[name])
        below = value < minimum if include_minimum else value <= minimum
        above = value > maximum if include_maximum else value >= maximum
        if below or above:
            left = "[" if include_minimum else "("
            right = "]" if include_maximum else ")"
            errors.append(f"{name} must be in {left}{minimum}, {maximum}{right}")

    require_range(
        "base_order_size", minimum=5.0, maximum=1_000_000.0
    )
    grid_levels = int(parameters["grid_levels"])
    if grid_levels < 1 or grid_levels > 20:
        errors.append("grid_levels must be in [1, 20]")
    require_range(
        "quote_base_spread",
        minimum=0.0,
        maximum=1.0,
        include_minimum=False,
        include_maximum=False,
    )
    require_range(
        "quote_price_offset_threshold",
        minimum=0.0,
        maximum=1.0,
        include_minimum=False,
    )
    require_range(
        "minimum_expected_net_edge",
        minimum=0.0,
        maximum=1.0,
        include_maximum=False,
    )
    require_range(
        "execution_cost_buffer",
        minimum=0.0,
        maximum=1.0,
        include_maximum=False,
    )
    require_range(
        "adverse_selection_buffer",
        minimum=0.0,
        maximum=1.0,
        include_maximum=False,
    )
    require_range(
        "exit_max_book_impact", minimum=0.0, maximum=1.0
    )
    require_range(
        "exit_max_realized_loss_fraction",
        minimum=0.0,
        maximum=1.0,
        include_maximum=False,
    )
    require_range(
        "max_exposure_per_market",
        minimum=0.0,
        maximum=1_000_000_000.0,
        include_minimum=False,
    )
    require_range(
        "global_max_budget",
        minimum=0.0,
        maximum=1_000_000_000.0,
        include_minimum=False,
    )
    if parameters["max_exposure_per_market"] > parameters["global_max_budget"]:
        errors.append("max_exposure_per_market cannot exceed global_max_budget")
    require_range(
        "market_data_max_age_sec",
        minimum=0.0,
        maximum=300.0,
        include_minimum=False,
    )
    require_range(
        "market_data_max_future_skew_sec",
        minimum=0.0,
        maximum=60.0,
    )
    if not parameters["market_data_require_sequence_live"]:
        errors.append("market_data_require_sequence_live must be true")
    if not parameters["market_data_require_exchange_timestamp_live"]:
        errors.append("market_data_require_exchange_timestamp_live must be true")
    return errors


def critical_source_sha256(project_root: Path | None = None) -> str:
    root = project_root or Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for relative in CRITICAL_SOURCE_FILES:
        path = root / relative
        raw = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
    return digest.hexdigest()


def _main() -> None:
    from app.core.config import settings

    parser = argparse.ArgumentParser(
        description="Export the canonical PolyMatrix runtime strategy config"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config = build_runtime_strategy_config(settings)
    serialized = json.dumps(config, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(serialized, encoding="utf-8")
        print(f"strategy_config={args.output}")
    else:
        print(serialized, end="")
    print(f"config_sha256={sha256_mapping(config)}")
    print(f"critical_source_sha256={critical_source_sha256()}")


if __name__ == "__main__":
    _main()
