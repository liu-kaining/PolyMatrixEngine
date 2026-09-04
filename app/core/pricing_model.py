"""Pure, evidence-bindable pricing primitives for binary CLOB markets."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence


class PricingModelError(ValueError):
    """The supplied books cannot support a safe binary fair value."""


@dataclass(frozen=True)
class BookSignal:
    microprice: float
    midpoint: float
    spread: float
    imbalance: float
    weighted_depth: float


@dataclass(frozen=True)
class BinaryFairValue:
    yes_fair_value: float
    dynamic_spread: float
    parity_error: float
    combined_imbalance: float


def _levels(raw: Any, *, reverse: bool, depth_levels: int) -> list[tuple[float, float]]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise PricingModelError("book side must be a sequence")
    output: list[tuple[float, float]] = []
    for level in raw:
        if not isinstance(level, dict):
            raise PricingModelError("book level must be an object")
        try:
            price = float(level["price"])
            size = float(level["size"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PricingModelError("book level price/size is invalid") from exc
        if not math.isfinite(price) or not math.isfinite(size):
            raise PricingModelError("book level price/size must be finite")
        if not 0.0 < price < 1.0 or size <= 0.0:
            raise PricingModelError("book level price/size is outside safe bounds")
        output.append((price, size))
    output.sort(key=lambda item: item[0], reverse=reverse)
    if not output:
        raise PricingModelError("both sides of the book are required")
    return output[:depth_levels]


def calculate_book_signal(
    bids: Any,
    asks: Any,
    *,
    depth_levels: int,
    depth_decay: float,
) -> BookSignal:
    """Return a bounded multi-level microprice/depth signal."""
    if depth_levels < 1 or depth_levels > 20:
        raise PricingModelError("depth_levels must be in [1, 20]")
    if not math.isfinite(depth_decay) or not 0.0 < depth_decay <= 1.0:
        raise PricingModelError("depth_decay must be in (0, 1]")
    bid_levels = _levels(bids, reverse=True, depth_levels=depth_levels)
    ask_levels = _levels(asks, reverse=False, depth_levels=depth_levels)
    best_bid = bid_levels[0][0]
    best_ask = ask_levels[0][0]
    if best_bid >= best_ask:
        raise PricingModelError("book is crossed or locked")

    bid_depth = sum(size * (depth_decay**index) for index, (_, size) in enumerate(bid_levels))
    ask_depth = sum(size * (depth_decay**index) for index, (_, size) in enumerate(ask_levels))
    total_depth = bid_depth + ask_depth
    if not math.isfinite(total_depth) or total_depth <= 0:
        raise PricingModelError("weighted book depth is invalid")
    imbalance = (bid_depth - ask_depth) / total_depth
    microprice = (best_ask * bid_depth + best_bid * ask_depth) / total_depth
    midpoint = (best_bid + best_ask) / 2.0
    spread = best_ask - best_bid
    return BookSignal(microprice, midpoint, spread, imbalance, total_depth)


def calculate_binary_fair_value(
    *,
    yes_bids: Any,
    yes_asks: Any,
    no_bids: Any,
    no_asks: Any,
    base_spread: float,
    depth_levels: int,
    depth_decay: float,
    max_parity_error: float,
    yes_inventory_value: float = 0.0,
    no_inventory_value: float = 0.0,
    inventory_cap: float = 1.0,
    max_inventory_skew: float = 0.0,
) -> BinaryFairValue:
    """Fuse independent YES and NO books, then apply a bounded inventory skew."""
    yes = calculate_book_signal(
        yes_bids, yes_asks, depth_levels=depth_levels, depth_decay=depth_decay
    )
    no = calculate_book_signal(
        no_bids, no_asks, depth_levels=depth_levels, depth_decay=depth_decay
    )
    if not math.isfinite(base_spread) or not 0.0 < base_spread < 1.0:
        raise PricingModelError("base_spread must be in (0, 1)")
    if not math.isfinite(max_parity_error) or not 0.0 <= max_parity_error < 1.0:
        raise PricingModelError("max_parity_error must be in [0, 1)")
    if not math.isfinite(max_inventory_skew) or not 0.0 <= max_inventory_skew < 0.5:
        raise PricingModelError("max_inventory_skew must be in [0, 0.5)")
    if not all(
        math.isfinite(value)
        for value in (yes_inventory_value, no_inventory_value, inventory_cap)
    ) or inventory_cap <= 0:
        raise PricingModelError("inventory values/cap are invalid")

    no_as_yes = 1.0 - no.microprice
    parity_error = abs(yes.microprice - no_as_yes)
    if parity_error > max_parity_error:
        raise PricingModelError(
            f"binary book parity error {parity_error:.6f} exceeds {max_parity_error:.6f}"
        )
    total_weight = yes.weighted_depth + no.weighted_depth
    raw_yes = (
        yes.microprice * yes.weighted_depth + no_as_yes * no.weighted_depth
    ) / total_weight

    inventory_ratio = max(
        -1.0,
        min(1.0, (yes_inventory_value - no_inventory_value) / inventory_cap),
    )
    adjusted_yes = raw_yes - inventory_ratio * max_inventory_skew
    bounded_yes = max(0.01, min(0.99, adjusted_yes))
    combined_imbalance = (
        yes.imbalance * yes.weighted_depth
        - no.imbalance * no.weighted_depth
    ) / total_weight
    dynamic_spread = max(
        base_spread * (1.0 + abs(combined_imbalance)),
        min(0.25, max(yes.spread, no.spread)),
    )
    return BinaryFairValue(
        yes_fair_value=bounded_yes,
        dynamic_spread=dynamic_spread,
        parity_error=parity_error,
        combined_imbalance=combined_imbalance,
    )


def pair_time_skew_seconds(first: dict[str, Any], second: dict[str, Any]) -> float:
    """Measure the comparable exchange/receive time skew of two snapshots."""
    first_time = first.get("exchange_timestamp") or first.get("received_at")
    second_time = second.get("exchange_timestamp") or second.get("received_at")
    try:
        left = float(first_time)
        right = float(second_time)
    except (TypeError, ValueError) as exc:
        raise PricingModelError("paired books have no comparable timestamps") from exc
    if not math.isfinite(left) or not math.isfinite(right):
        raise PricingModelError("paired book timestamps must be finite")
    return abs(left - right)
