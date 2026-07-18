"""Pure helpers for immutable fill cash facts.

The project deliberately distinguishes gross trade cash from net cash. A fee is
never estimated from an undocumented rate or silently treated as zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from app.core.accounting import AccountingInvariantError


EXPLICIT_FEE_KEYS = ("fee_amount", "feeAmount", "fee_paid", "feePaid")


@dataclass(frozen=True)
class FillCashFact:
    gross_cash_delta: float
    fee_amount: Optional[float]
    net_cash_delta: Optional[float]
    fee_status: str


def extract_explicit_fee_amount(payload: Mapping[str, Any]) -> Optional[float]:
    """Read only an explicit absolute fee from a payload.

    Fee-rate fields are intentionally ignored because payer, units and rounding
    semantics must be contract-tested before they can become accounting facts.
    Conflicting aliases fail closed instead of selecting one arbitrarily.
    """
    values: list[float] = []
    for key in EXPLICIT_FEE_KEYS:
        raw = payload.get(key)
        if raw in (None, ""):
            continue
        if isinstance(raw, bool):
            raise AccountingInvariantError(f"invalid explicit fee field: {key}")
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise AccountingInvariantError(f"invalid explicit fee field: {key}") from exc
        if not math.isfinite(value) or value < 0:
            raise AccountingInvariantError(f"invalid explicit fee field: {key}")
        values.append(value)

    if not values:
        return None
    first = values[0]
    if any(abs(value - first) > 1e-9 for value in values[1:]):
        raise AccountingInvariantError("conflicting explicit fee fields")
    return first


def build_fill_cash_fact(
    *,
    side: str,
    price: float,
    size: float,
    fee_amount: Optional[float],
) -> FillCashFact:
    side_value = str(side or "").upper()
    try:
        price_value = float(price)
        size_value = float(size)
    except (TypeError, ValueError) as exc:
        raise AccountingInvariantError("cash fact price/size must be numeric") from exc

    if side_value not in {"BUY", "SELL"}:
        raise AccountingInvariantError(f"unsupported cash fact side: {side}")
    if not math.isfinite(price_value) or price_value < 0 or price_value > 1:
        raise AccountingInvariantError("cash fact price must be within [0, 1]")
    if not math.isfinite(size_value) or size_value <= 0:
        raise AccountingInvariantError("cash fact size must be positive")

    notional = price_value * size_value
    gross = -notional if side_value == "BUY" else notional
    if fee_amount is None:
        return FillCashFact(
            gross_cash_delta=gross,
            fee_amount=None,
            net_cash_delta=None,
            fee_status="UNKNOWN",
        )

    fee_value = float(fee_amount)
    if not math.isfinite(fee_value) or fee_value < 0:
        raise AccountingInvariantError("fee_amount must be finite and non-negative")
    return FillCashFact(
        gross_cash_delta=gross,
        fee_amount=fee_value,
        net_cash_delta=gross - fee_value,
        fee_status="KNOWN",
    )
