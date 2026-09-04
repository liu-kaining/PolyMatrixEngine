"""Pure validation for order intents before any reservation, journal, or network call."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any


MIN_ORDER_SIZE_SHARES = 5.0


@dataclass(frozen=True)
class ValidatedOrderIntent:
    condition_id: str
    token_id: str
    side: str
    price: float
    size: float


class OrderValidationError(ValueError):
    """An order intent is structurally invalid or outside prediction-token bounds."""


def normalize_order_size(size: Any) -> float:
    """Mirror the pinned SDK's two-decimal share-size rounding before journaling."""
    try:
        value = Decimal(str(size))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OrderValidationError("size must be numeric") from exc
    if not value.is_finite():
        raise OrderValidationError("size must be finite")
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_FLOOR))


def validate_order_intent(
    *,
    condition_id: Any,
    token_id: Any,
    side: Any,
    price: Any,
    size: Any,
) -> ValidatedOrderIntent:
    normalized_condition_id = str(condition_id or "").strip()
    normalized_token_id = str(token_id or "").strip()
    normalized_side = str(getattr(side, "value", side) or "").strip().upper()
    try:
        normalized_price = float(price)
        normalized_size = float(size)
    except (TypeError, ValueError) as exc:
        raise OrderValidationError("price and size must be numeric") from exc

    if not normalized_condition_id or not normalized_token_id:
        raise OrderValidationError("condition_id and token_id are required")
    if normalized_side not in {"BUY", "SELL"}:
        raise OrderValidationError("side must be BUY or SELL")
    if not math.isfinite(normalized_price) or not math.isfinite(normalized_size):
        raise OrderValidationError("price and size must be finite")
    if not 0.0 < normalized_price < 1.0:
        raise OrderValidationError("prediction-token limit price must be between 0 and 1")
    normalized_size = normalize_order_size(normalized_size)
    if normalized_size < MIN_ORDER_SIZE_SHARES:
        raise OrderValidationError(
            f"order size must be at least {MIN_ORDER_SIZE_SHARES:g} shares"
        )

    return ValidatedOrderIntent(
        condition_id=normalized_condition_id,
        token_id=normalized_token_id,
        side=normalized_side,
        price=normalized_price,
        size=normalized_size,
    )
