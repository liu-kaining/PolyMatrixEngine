"""Database-serialized BUY capital and SELL inventory reservations."""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func
from sqlalchemy.future import select

from app.core.config import settings
from app.core.trading_safety import trading_safety
from app.db.session import AsyncSessionLocal
from app.models.db_models import (
    InventoryLedger,
    MarketMeta,
    OrderJournal,
    OrderSide,
    OrderStatus,
    PortfolioRiskState,
    RiskReservation,
)


logger = logging.getLogger(__name__)
ACTIVE_RESERVATION_STATUSES = (
    "RESERVED",
    "OPEN",
    "PARTIAL",
    "UNKNOWN",
    "CANCEL_PENDING_RECONCILE",
)


class ReservationRejected(RuntimeError):
    """Raised when atomic portfolio admission rejects a BUY order."""


class ReservationInvariantError(RuntimeError):
    """Raised when durable order risk and fill accounting no longer agree."""


@dataclass(frozen=True)
class ReservationDecision:
    allowed: bool
    reason: str
    requested_notional: float
    global_after: float
    market_after: float


@dataclass(frozen=True)
class ReservationGrant:
    reservation_id: str
    client_order_id: str
    reserved_notional: float


@dataclass(frozen=True)
class SellReservationDecision:
    allowed: bool
    reason: str
    requested_size: float
    available_after: float


def evaluate_reservation(
    *,
    requested_notional: float,
    global_capital_used: float,
    global_reserved: float,
    market_capital_used: float,
    market_reserved: float,
    global_cap: float,
    market_cap: float,
) -> ReservationDecision:
    requested = float(requested_notional)
    numeric_inputs = (
        requested,
        float(global_capital_used),
        float(global_reserved),
        float(market_capital_used),
        float(market_reserved),
        float(global_cap),
        float(market_cap),
    )
    if not all(math.isfinite(value) for value in numeric_inputs):
        return ReservationDecision(
            False, "risk inputs must be finite", requested, math.inf, math.inf
        )
    global_after = numeric_inputs[1] + numeric_inputs[2] + requested
    market_after = numeric_inputs[3] + numeric_inputs[4] + requested
    if requested <= 0:
        return ReservationDecision(
            False, "requested notional must be positive", requested, global_after, market_after
        )
    if any(value < 0 for value in numeric_inputs[1:5]):
        return ReservationDecision(
            False, "risk state cannot be negative", requested, global_after, market_after
        )
    if global_cap <= 0 or market_cap <= 0:
        return ReservationDecision(
            False, "risk caps must be positive", requested, global_after, market_after
        )
    if global_after > float(global_cap) + 1e-9:
        return ReservationDecision(
            False, "global budget would be exceeded", requested, global_after, market_after
        )
    if market_after > float(market_cap) + 1e-9:
        return ReservationDecision(
            False, "market budget would be exceeded", requested, global_after, market_after
        )
    return ReservationDecision(True, "admitted", requested, global_after, market_after)


def evaluate_sell_reservation(
    *,
    requested_size: float,
    inventory_exposure: float,
    already_reserved_size: float,
) -> SellReservationDecision:
    requested = float(requested_size)
    exposure = float(inventory_exposure)
    reserved = float(already_reserved_size)
    if not all(math.isfinite(value) for value in (requested, exposure, reserved)):
        return SellReservationDecision(
            False, "SELL risk inputs must be finite", requested, 0.0
        )
    if requested <= 0 or exposure < 0 or reserved < 0:
        return SellReservationDecision(False, "SELL risk inputs are invalid", requested, 0.0)
    available_after = exposure - reserved - requested
    if available_after < -1e-9:
        return SellReservationDecision(
            False,
            "SELL size exceeds unreserved inventory",
            requested,
            available_after,
        )
    return SellReservationDecision(True, "admitted", requested, available_after)


class RiskReservationService:
    async def _lock_portfolio_state(self, session) -> PortfolioRiskState:
        stmt = (
            select(PortfolioRiskState)
            .filter(PortfolioRiskState.wallet_id == "default")
            .with_for_update()
        )
        state = (await session.execute(stmt)).scalar_one_or_none()
        if state is None:
            state = PortfolioRiskState(wallet_id="default")
            session.add(state)
            await session.flush()
        return state

    async def _active_reserved_sum(self, session, market_id: Optional[str] = None) -> float:
        stmt = select(func.coalesce(func.sum(RiskReservation.reserved_notional), 0)).filter(
            RiskReservation.status.in_(ACTIVE_RESERVATION_STATUSES)
        )
        if market_id is not None:
            stmt = stmt.filter(RiskReservation.market_id == market_id)
        return float((await session.execute(stmt)).scalar_one() or 0.0)

    async def _capital_used_sum(self, session, market_id: Optional[str] = None) -> float:
        expression = func.coalesce(
            func.sum(InventoryLedger.yes_capital_used + InventoryLedger.no_capital_used),
            0,
        )
        stmt = select(expression)
        if market_id is not None:
            stmt = stmt.filter(InventoryLedger.market_id == market_id)
        return float((await session.execute(stmt)).scalar_one() or 0.0)

    async def reserve_buy(
        self,
        *,
        client_order_id: str,
        market_id: str,
        token_id: str,
        limit_price: float,
        size: float,
        market_cap: Optional[float] = None,
    ) -> ReservationGrant:
        normalized_price = float(limit_price)
        normalized_size = float(size)
        if (
            not math.isfinite(normalized_price)
            or not math.isfinite(normalized_size)
            or not 0.0 < normalized_price < 1.0
            or normalized_size <= 0
        ):
            raise ReservationRejected("invalid BUY price/size for reservation")
        requested = normalized_price * normalized_size
        async with AsyncSessionLocal() as session:
            state = await self._lock_portfolio_state(session)

            market = await session.get(MarketMeta, market_id)
            if market is None or token_id not in {market.yes_token_id, market.no_token_id}:
                raise ReservationRejected("BUY token is not mapped to the market")

            existing_stmt = select(RiskReservation).filter(
                RiskReservation.client_order_id == client_order_id
            )
            existing = (await session.execute(existing_stmt)).scalar_one_or_none()
            if existing is not None:
                if existing.status in ACTIVE_RESERVATION_STATUSES:
                    return ReservationGrant(
                        reservation_id=existing.reservation_id,
                        client_order_id=existing.client_order_id,
                        reserved_notional=float(existing.reserved_notional),
                    )
                raise ReservationRejected("client_order_id cannot reuse a terminal reservation")

            global_reserved = await self._active_reserved_sum(session)
            market_reserved = await self._active_reserved_sum(session, market_id)
            global_capital = await self._capital_used_sum(session)
            market_capital = await self._capital_used_sum(session, market_id)
            resolved_market_cap = float(
                market_cap
                if market_cap is not None
                else getattr(settings, "MAX_EXPOSURE_PER_MARKET", 40.0)
            )
            decision = evaluate_reservation(
                requested_notional=requested,
                global_capital_used=global_capital,
                global_reserved=global_reserved,
                market_capital_used=market_capital,
                market_reserved=market_reserved,
                global_cap=float(getattr(settings, "GLOBAL_MAX_BUDGET", 0.0)),
                market_cap=resolved_market_cap,
            )
            if not decision.allowed:
                raise ReservationRejected(
                    f"{decision.reason}; global_after={decision.global_after:.4f}; "
                    f"market_after={decision.market_after:.4f}"
                )

            reservation_id = uuid.uuid4().hex
            reservation = RiskReservation(
                reservation_id=reservation_id,
                client_order_id=client_order_id,
                market_id=market_id,
                token_id=token_id,
                side=OrderSide.BUY.value,
                limit_price=normalized_price,
                original_size=normalized_size,
                remaining_size=normalized_size,
                reserved_notional=requested,
                status="RESERVED",
            )
            session.add(reservation)
            state.reserved_buy_notional = global_reserved + requested
            state.state_version = int(state.state_version or 0) + 1
            await session.commit()
            return ReservationGrant(reservation_id, client_order_id, requested)

    async def reserve_sell(
        self,
        *,
        client_order_id: str,
        market_id: str,
        token_id: str,
        limit_price: float,
        size: float,
    ) -> ReservationGrant:
        normalized_price = float(limit_price)
        normalized_size = float(size)
        if (
            not math.isfinite(normalized_price)
            or not math.isfinite(normalized_size)
            or not 0.0 < normalized_price < 1.0
            or normalized_size <= 0
        ):
            raise ReservationRejected("invalid SELL price/size for reservation")

        async with AsyncSessionLocal() as session:
            await self._lock_portfolio_state(session)
            existing = (
                await session.execute(
                    select(RiskReservation).filter(
                        RiskReservation.client_order_id == client_order_id
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.status in ACTIVE_RESERVATION_STATUSES:
                    return ReservationGrant(
                        existing.reservation_id,
                        existing.client_order_id,
                        float(existing.reserved_notional or 0.0),
                    )
                raise ReservationRejected("client_order_id cannot reuse a terminal reservation")

            market = await session.get(MarketMeta, market_id)
            if market is None or token_id not in {market.yes_token_id, market.no_token_id}:
                raise ReservationRejected("SELL token is not mapped to the market")
            inventory = (
                await session.execute(
                    select(InventoryLedger)
                    .filter(InventoryLedger.market_id == market_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if inventory is None:
                raise ReservationRejected("SELL inventory ledger is missing")
            exposure = float(
                inventory.yes_exposure
                if token_id == market.yes_token_id
                else inventory.no_exposure
            )
            already_reserved = float(
                (
                    await session.execute(
                        select(
                            func.coalesce(func.sum(RiskReservation.remaining_size), 0)
                        ).filter(
                            RiskReservation.market_id == market_id,
                            RiskReservation.token_id == token_id,
                            RiskReservation.side == OrderSide.SELL.value,
                            RiskReservation.status.in_(ACTIVE_RESERVATION_STATUSES),
                        )
                    )
                ).scalar_one()
                or 0.0
            )
            decision = evaluate_sell_reservation(
                requested_size=normalized_size,
                inventory_exposure=exposure,
                already_reserved_size=already_reserved,
            )
            if not decision.allowed:
                raise ReservationRejected(
                    f"{decision.reason}; available_after={decision.available_after:.8f}"
                )

            reservation_id = uuid.uuid4().hex
            session.add(
                RiskReservation(
                    reservation_id=reservation_id,
                    client_order_id=client_order_id,
                    market_id=market_id,
                    token_id=token_id,
                    side=OrderSide.SELL.value,
                    limit_price=normalized_price,
                    original_size=normalized_size,
                    remaining_size=normalized_size,
                    reserved_notional=0.0,
                    status="RESERVED",
                )
            )
            await session.commit()
            return ReservationGrant(reservation_id, client_order_id, 0.0)

    async def mark_open(self, reservation_id: str, exchange_order_id: str) -> None:
        async with AsyncSessionLocal() as session:
            stmt = (
                select(RiskReservation)
                .filter(RiskReservation.reservation_id == reservation_id)
                .with_for_update()
            )
            reservation = (await session.execute(stmt)).scalar_one_or_none()
            if reservation and reservation.status in ("RESERVED", "OPEN"):
                reservation.status = "OPEN"
                reservation.exchange_order_id = exchange_order_id
                await session.commit()

    async def release(self, reservation_id: Optional[str], terminal_status: str) -> float:
        if not reservation_id:
            return 0.0
        async with AsyncSessionLocal() as session:
            state = await self._lock_portfolio_state(session)
            stmt = (
                select(RiskReservation)
                .filter(RiskReservation.reservation_id == reservation_id)
                .with_for_update()
            )
            reservation = (await session.execute(stmt)).scalar_one_or_none()
            if reservation is None or reservation.status not in ACTIVE_RESERVATION_STATUSES:
                return 0.0
            released = float(reservation.reserved_notional or 0.0)
            reservation.reserved_notional = 0.0
            reservation.remaining_size = 0.0
            reservation.status = str(terminal_status)
            state.reserved_buy_notional = max(
                0.0, float(state.reserved_buy_notional or 0.0) - released
            )
            state.state_version = int(state.state_version or 0) + 1
            await session.commit()
            return released

    async def release_for_order(self, local_order_id: str, terminal_status: str) -> float:
        async with AsyncSessionLocal() as session:
            order = await session.get(OrderJournal, local_order_id)
            reservation_id = order.reservation_id if order else None
        return await self.release(reservation_id, terminal_status)

    async def _mark_active_for_order(self, local_order_id: str, status: str) -> None:
        async with AsyncSessionLocal() as session:
            order = await session.get(OrderJournal, local_order_id)
            if not order or not order.reservation_id:
                return
            stmt = (
                select(RiskReservation)
                .filter(RiskReservation.reservation_id == order.reservation_id)
                .with_for_update()
            )
            reservation = (await session.execute(stmt)).scalar_one_or_none()
            if reservation and reservation.status in ACTIVE_RESERVATION_STATUSES:
                reservation.status = status
                await session.commit()

    async def mark_unknown_for_order(self, local_order_id: str) -> None:
        await self._mark_active_for_order(local_order_id, "UNKNOWN")

    async def mark_cancel_pending_for_order(self, local_order_id: str) -> None:
        """Retain remaining capital/shares until fills and orders are reconciled."""
        await self._mark_active_for_order(local_order_id, "CANCEL_PENDING_RECONCILE")

    async def apply_fill_in_session(
        self,
        session,
        reservation_id: Optional[str],
        fill_size: float,
        fill_price: float,
        order_side: str,
        *,
        required: bool,
    ) -> None:
        """Release the filled portion inside the same DB transaction as inventory accounting."""
        if not reservation_id:
            if required:
                raise ReservationInvariantError("fill has no linked risk reservation")
            return
        state = await self._lock_portfolio_state(session)
        stmt = (
            select(RiskReservation)
            .filter(RiskReservation.reservation_id == reservation_id)
            .with_for_update()
        )
        reservation = (await session.execute(stmt)).scalar_one_or_none()
        if reservation is None:
            raise ReservationInvariantError("linked risk reservation does not exist")
        if reservation.status not in ACTIVE_RESERVATION_STATUSES:
            if required:
                raise ReservationInvariantError(
                    f"fill reached terminal reservation status={reservation.status}"
                )
            return
        normalized_side = str(order_side or "").upper()
        if reservation.side != normalized_side:
            raise ReservationInvariantError(
                f"order side {normalized_side} does not match reservation side "
                f"{reservation.side}"
            )
        applied_size = float(fill_size)
        applied_price = float(fill_price)
        remaining_size = float(reservation.remaining_size or 0.0)
        if not math.isfinite(applied_size) or not math.isfinite(applied_price):
            raise ReservationInvariantError("fill price and size must be finite")
        if applied_size <= 0 or applied_price <= 0:
            raise ReservationInvariantError("fill price and size must be positive")
        if normalized_side == OrderSide.BUY.value and applied_price > float(
            reservation.limit_price
        ) + 1e-6:
            raise ReservationInvariantError(
                f"BUY fill price {applied_price:.8f} exceeds limit "
                f"{float(reservation.limit_price):.8f}"
            )
        if applied_size > remaining_size + 1e-6:
            raise ReservationInvariantError(
                f"fill size {applied_size:.8f} exceeds reserved remaining size "
                f"{remaining_size:.8f}"
            )
        released = 0.0
        if normalized_side == OrderSide.BUY.value:
            released = min(
                float(reservation.reserved_notional or 0.0),
                applied_size * float(reservation.limit_price),
            )
        reservation.remaining_size = max(
            0.0, float(reservation.remaining_size or 0.0) - applied_size
        )
        reservation.reserved_notional = max(
            0.0, float(reservation.reserved_notional or 0.0) - released
        )
        reservation.status = (
            "FILLED" if float(reservation.remaining_size) <= 1e-9 else "PARTIAL"
        )
        state.reserved_buy_notional = max(
            0.0, float(state.reserved_buy_notional or 0.0) - released
        )
        state.state_version = int(state.state_version or 0) + 1

    async def rebuild_and_validate(self) -> bool:
        """Recompute cached reservation total and fail closed on legacy/unlinked orders."""
        async with AsyncSessionLocal() as session:
            state = await self._lock_portfolio_state(session)
            active_total = await self._active_reserved_sum(session)
            state.reserved_buy_notional = active_total
            state.state_version = int(state.state_version or 0) + 1

            orphan_reservations = (
                await session.execute(
                    select(RiskReservation.client_order_id).filter(
                        RiskReservation.status.in_(ACTIVE_RESERVATION_STATUSES),
                        ~RiskReservation.client_order_id.in_(select(OrderJournal.order_id)),
                    )
                )
            ).scalars().all()
            unreserved_orders = (
                await session.execute(
                    select(OrderJournal.order_id).filter(
                        OrderJournal.status.in_(
                            [OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.UNKNOWN]
                        ),
                        OrderJournal.reservation_id.is_(None),
                    )
                )
            ).scalars().all()
            cancel_pending = (
                await session.execute(
                    select(RiskReservation.reservation_id).filter(
                        RiskReservation.status == "CANCEL_PENDING_RECONCILE"
                    )
                )
            ).scalars().all()
            await session.commit()

        safe = not orphan_reservations and not unreserved_orders and not cancel_pending
        detail = (
            "risk reservations rebuilt and linked"
            if safe
            else (
                f"orphan_reservations={len(orphan_reservations)}, "
                f"unreserved_active_orders={len(unreserved_orders)}, "
                f"cancel_pending_reconcile={len(cancel_pending)}"
            )
        )
        trading_safety.set_readiness("risk_reservations", safe, detail)
        if not safe:
            trading_safety.halt(f"risk reservation rebuild failed: {detail}")
        return safe

    async def totals(self) -> tuple[float, dict[str, float]]:
        async with AsyncSessionLocal() as session:
            global_total = await self._active_reserved_sum(session)
            rows = (
                await session.execute(
                    select(
                        RiskReservation.market_id,
                        func.coalesce(func.sum(RiskReservation.reserved_notional), 0),
                    )
                    .filter(RiskReservation.status.in_(ACTIVE_RESERVATION_STATUSES))
                    .group_by(RiskReservation.market_id)
                )
            ).all()
        return global_total, {market_id: float(value or 0.0) for market_id, value in rows}


risk_reservations = RiskReservationService()
