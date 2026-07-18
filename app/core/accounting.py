"""Pure inventory accounting functions used by the durable fill processor."""

from __future__ import annotations

import math
from dataclasses import dataclass


class AccountingInvariantError(ValueError):
    """Raised when a fill would violate basic position-accounting invariants."""


@dataclass(frozen=True)
class SideBalances:
    exposure: float
    capital_used: float
    realized_pnl: float


def apply_fill_accounting(
    *,
    exposure: float,
    capital_used: float,
    realized_pnl: float,
    side: str,
    fill_size: float,
    fill_price: float,
    fee_amount: float = 0.0,
    tolerance: float = 1e-9,
) -> SideBalances:
    """Apply one incremental fill using average-cost realized PnL accounting.

    BUY increases position cost and does not change realized PnL. SELL removes the
    proportional average cost and realizes proceeds minus removed cost basis.
    """
    current_exposure = float(exposure)
    current_capital = float(capital_used)
    current_realized = float(realized_pnl)
    size = float(fill_size)
    price = float(fill_price)
    fee = float(fee_amount)
    side_value = str(side or "").upper()

    if current_exposure < -tolerance or current_capital < -tolerance:
        raise AccountingInvariantError("existing exposure/capital cannot be negative")
    if not all(math.isfinite(value) for value in (current_exposure, current_capital, current_realized, size, price, fee)):
        raise AccountingInvariantError("accounting values must be finite")
    if size <= 0:
        raise AccountingInvariantError("fill_size must be positive")
    if price < 0.0 or price > 1.0:
        raise AccountingInvariantError("fill_price must be within [0, 1]")
    if fee < 0:
        raise AccountingInvariantError("fee_amount cannot be negative")

    if side_value == "BUY":
        return SideBalances(
            exposure=current_exposure + size,
            capital_used=current_capital + (price * size) + fee,
            realized_pnl=current_realized,
        )

    if side_value != "SELL":
        raise AccountingInvariantError(f"unsupported fill side: {side}")
    if size > current_exposure + tolerance:
        raise AccountingInvariantError(
            f"SELL fill {size:.8f} exceeds known exposure {current_exposure:.8f}"
        )
    if current_exposure <= tolerance:
        raise AccountingInvariantError("cannot apply SELL fill to zero exposure")

    applied_size = min(size, current_exposure)
    average_cost = current_capital / current_exposure
    removed_cost = average_cost * applied_size
    remaining_exposure = max(0.0, current_exposure - applied_size)
    remaining_capital = max(0.0, current_capital - removed_cost)
    realized_delta = (price * applied_size) - fee - removed_cost

    if remaining_exposure <= tolerance:
        remaining_exposure = 0.0
        remaining_capital = 0.0

    return SideBalances(
        exposure=remaining_exposure,
        capital_used=remaining_capital,
        realized_pnl=current_realized + realized_delta,
    )
