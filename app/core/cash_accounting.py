"""Pure helpers for immutable fill cash facts.

The project deliberately distinguishes gross trade cash from net cash. A fee is
never estimated from an undocumented rate or silently treated as zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping, Optional

from app.core.accounting import AccountingInvariantError


EXPLICIT_FEE_KEYS = ("fee_amount", "feeAmount", "fee_paid", "feePaid")
USDC_FEE_QUANTUM = Decimal("0.00001")


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


def resolve_fee_amount(
    payload: Mapping[str, Any],
    liquidity_role: Optional[str],
    *,
    price: Optional[Any] = None,
    size: Optional[Any] = None,
    fee_rate_bps: Optional[Any] = None,
) -> Optional[float]:
    """Resolve a fee using the current documented CLOB contract.

    An explicit absolute fee always wins. Polymarket's current CLOB fee contract
    charges only takers.  ``fee_rate_bps`` uses basis points, and the USDC fee is
    ``shares * (bps / 10_000) * price * (1-price)``, rounded to five decimals.
    Missing taker inputs remain unknown rather than becoming a zero fee.
    """
    explicit = extract_explicit_fee_amount(payload)
    if explicit is not None:
        return explicit
    role = str(liquidity_role or "").strip().upper()
    if role == "MAKER":
        return 0.0
    if role == "TAKER":
        if price in (None, "") or size in (None, "") or fee_rate_bps in (None, ""):
            return None
        return calculate_taker_fee_usdc(
            price=price,
            size=size,
            fee_rate_bps=fee_rate_bps,
        )
    if role == "":
        return None
    raise AccountingInvariantError(f"unsupported liquidity role: {liquidity_role}")


def calculate_taker_fee_usdc(*, price: Any, size: Any, fee_rate_bps: Any) -> float:
    """Calculate the documented taker fee with exact decimal/USDC precision."""
    try:
        price_value = Decimal(str(price))
        size_value = Decimal(str(size))
        bps_value = Decimal(str(fee_rate_bps))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AccountingInvariantError("taker fee inputs must be decimal numbers") from exc
    if not all(value.is_finite() for value in (price_value, size_value, bps_value)):
        raise AccountingInvariantError("taker fee inputs must be finite")
    if not Decimal("0") < price_value < Decimal("1"):
        raise AccountingInvariantError("taker fee price must be within (0, 1)")
    if size_value <= 0:
        raise AccountingInvariantError("taker fee size must be positive")
    if not Decimal("0") <= bps_value <= Decimal("10000"):
        raise AccountingInvariantError("fee_rate_bps must be within [0, 10000]")

    fee = size_value * (bps_value / Decimal("10000")) * price_value * (
        Decimal("1") - price_value
    )
    rounded = fee.quantize(USDC_FEE_QUANTUM, rounding=ROUND_HALF_UP)
    return float(rounded)


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
