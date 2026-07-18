"""Pure, depth-aware and loss-bounded exit planning."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class BoundedSellIntent:
    limit_price: float
    size: float
    best_bid: float
    impact_floor: float
    loss_floor: float
    available_depth: float


def plan_bounded_sell(
    *,
    bids: Iterable[dict[str, Any]],
    requested_size: float,
    exposure: float,
    capital_used: float,
    max_book_impact: float,
    max_realized_loss_fraction: float,
    min_order_size: float = 5.0,
) -> Optional[BoundedSellIntent]:
    """Return an executable SELL limit backed by visible depth, or None to wait."""
    values = (
        float(requested_size),
        float(exposure),
        float(capital_used),
        float(max_book_impact),
        float(max_realized_loss_fraction),
        float(min_order_size),
    )
    if not all(math.isfinite(value) for value in values):
        return None
    requested, held, cost, max_impact, max_loss, minimum = values
    if (
        requested <= 0
        or held <= 0
        or cost < 0
        or max_impact < 0
        or not 0 <= max_loss < 1
        or minimum <= 0
    ):
        return None

    normalized = []
    for level in bids:
        if not isinstance(level, dict):
            return None
        try:
            price = float(level.get("price"))
            size = float(level.get("size"))
        except (TypeError, ValueError):
            return None
        if (
            not math.isfinite(price)
            or not math.isfinite(size)
            or not 0.0 < price < 1.0
            or size <= 0
        ):
            return None
        normalized.append((price, size))
    if not normalized:
        return None
    normalized.sort(reverse=True)

    best_bid = normalized[0][0]
    impact_floor = max(0.01, best_bid - max_impact)
    average_cost = cost / held if held > 1e-9 else 0.0
    loss_floor = max(0.01, average_cost * (1.0 - max_loss)) if cost > 0 else 0.01
    absolute_floor = max(impact_floor, loss_floor)

    target = min(requested, held)
    cumulative = 0.0
    selected_limit = None
    for price, size in normalized:
        if price + 1e-9 < absolute_floor:
            break
        cumulative += size
        selected_limit = price
        if cumulative >= target - 1e-9:
            break

    executable_size = min(target, cumulative)
    if selected_limit is None or executable_size + 1e-9 < minimum:
        return None
    return BoundedSellIntent(
        limit_price=selected_limit,
        size=executable_size,
        best_bid=best_bid,
        impact_floor=impact_floor,
        loss_floor=loss_floor,
        available_depth=cumulative,
    )
