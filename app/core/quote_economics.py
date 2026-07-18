"""Conservative per-quote net-edge gate; rewards are intentionally excluded."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class QuoteEconomics:
    allowed: bool
    gross_edge: float
    net_edge: float
    reason: str


def evaluate_quote_economics(
    *,
    side: str,
    limit_price: float,
    fair_value: float,
    execution_cost_buffer: float,
    adverse_selection_buffer: float,
    minimum_net_edge: float,
) -> QuoteEconomics:
    normalized_side = str(side or "").upper()
    values = tuple(
        float(value)
        for value in (
            limit_price,
            fair_value,
            execution_cost_buffer,
            adverse_selection_buffer,
            minimum_net_edge,
        )
    )
    if normalized_side not in {"BUY", "SELL"} or not all(
        math.isfinite(value) for value in values
    ):
        return QuoteEconomics(False, 0.0, -math.inf, "invalid quote economics inputs")
    price, fair, costs, adverse, minimum = values
    if (
        not 0.0 < price < 1.0
        or not 0.0 < fair < 1.0
        or costs < 0
        or adverse < 0
        or minimum < 0
    ):
        return QuoteEconomics(False, 0.0, -math.inf, "invalid quote economics bounds")
    gross_edge = fair - price if normalized_side == "BUY" else price - fair
    net_edge = gross_edge - costs - adverse
    if net_edge + 1e-12 < minimum:
        return QuoteEconomics(
            False,
            gross_edge,
            net_edge,
            "expected net edge is below the configured minimum",
        )
    return QuoteEconomics(True, gross_edge, net_edge, "admitted")
