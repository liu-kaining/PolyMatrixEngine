"""Pure helpers for conservative external-position reconciliation."""

from __future__ import annotations

from typing import Dict, Optional


def normalize_condition_id(condition_id: Optional[str]) -> Optional[str]:
    if not condition_id or not isinstance(condition_id, str):
        return None
    value = condition_id.strip()
    return value.lower() if value.startswith("0x") else value


def _position_cost(position: dict, size: float) -> float:
    """Best-effort reported cost; never trusted as the only risk measure."""
    for key in ("initialValue", "initial_value", "cost", "costBasis", "cost_basis"):
        raw = position.get(key)
        if raw is not None:
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                pass
    for key in ("avgPrice", "averagePrice", "avg_price"):
        raw = position.get(key)
        if raw is not None:
            try:
                return max(0.0, float(raw) * size)
            except (TypeError, ValueError):
                pass
    return 0.0


def build_actual_inventory_from_positions(positions: list) -> Dict[str, Dict[str, float]]:
    """Group external positions by condition with size and reported cost per binary side."""
    inventory: Dict[str, Dict[str, float]] = {}
    for position in positions:
        condition_id = normalize_condition_id(position.get("conditionId"))
        if condition_id is None:
            continue
        bucket = inventory.setdefault(
            condition_id,
            {"yes": 0.0, "no": 0.0, "yes_cost": 0.0, "no_cost": 0.0},
        )
        try:
            size = max(0.0, float(position.get("size", 0.0)))
        except (TypeError, ValueError):
            continue
        cost = _position_cost(position, size)
        outcome_idx = position.get("outcomeIndex")
        if outcome_idx == 0 or str(position.get("outcome", "")).upper() == "YES":
            bucket["yes"] += size
            bucket["yes_cost"] += cost
        else:
            bucket["no"] += size
            bucket["no_cost"] += cost
    return inventory


def reconcile_capital_used(
    *,
    actual_size: float,
    reported_cost: float,
    previous_size: float,
    previous_capital_used: float,
) -> tuple[float, bool]:
    """Return conservative capital and whether an untracked position was discovered.

    When local size was zero, exact historical cost is unknowable. The function uses the
    maximum binary payoff ($1/share) as risk capital and reports discovery so callers can
    halt new risk until an offline accounting rebuild is performed.
    """
    actual = max(0.0, float(actual_size))
    previous = max(0.0, float(previous_size))
    previous_capital = max(0.0, float(previous_capital_used))
    reported = max(0.0, float(reported_cost))
    if actual <= 0.001:
        return 0.0, False
    if previous > 1e-9 and previous_capital > 1e-9:
        return previous_capital * (actual / previous), False
    return max(actual, reported), True
