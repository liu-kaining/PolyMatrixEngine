"""Durable and idempotent processing for user-stream fill events."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.future import select

from app.core.accounting import AccountingInvariantError, apply_fill_accounting
from app.core.cash_accounting import build_fill_cash_fact, resolve_fee_amount
from app.core.inventory_state import inventory_state
from app.core.redis import redis_client
from app.core.trading_safety import trading_safety
from app.db.session import AsyncSessionLocal
from app.models.db_models import (
    FillEvent,
    FillCashLedger,
    InventoryLedger,
    MarketMeta,
    OrderJournal,
    OrderStatus,
)
from app.risk.reservations import ReservationInvariantError, risk_reservations


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FillProcessingResult:
    event_id: str
    status: str
    duplicate: bool = False
    detail: str = ""


def derive_fill_event_id(raw_event: Dict[str, Any], exchange_order_id: str, role: str) -> str:
    """Create a deterministic per-order event id from stable exchange fields or payload hash."""
    primary = None
    for key in (
        "trade_id",
        "tradeId",
        "id",
        "transaction_hash",
        "transactionHash",
        "match_id",
        "matchId",
    ):
        value = raw_event.get(key)
        if value not in (None, ""):
            primary = f"{key}:{value}"
            break
    if primary is None:
        canonical = json.dumps(raw_event, sort_keys=True, separators=(",", ":"), default=str)
        primary = f"payload:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    material = f"fill-v1|{primary}|{exchange_order_id}|{role}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class FillProcessor:
    async def record_and_process(
        self,
        *,
        event_id: str,
        exchange_order_id: str,
        filled_size: float,
        fill_price: float,
        raw_event: Dict[str, Any],
        token_id: Optional[str] = None,
        liquidity_role: Optional[str] = None,
        fee_rate_bps: Optional[Any] = None,
    ) -> FillProcessingResult:
        """Persist the inbox event first, then process it exactly once."""
        normalized_role = str(liquidity_role or "").strip().upper() or None
        if normalized_role not in {None, "MAKER", "TAKER"}:
            raise AccountingInvariantError("fill liquidity role must be MAKER or TAKER")
        normalized_fee_rate = None
        if fee_rate_bps not in (None, ""):
            try:
                normalized_fee_rate = float(fee_rate_bps)
            except (TypeError, ValueError) as exc:
                raise AccountingInvariantError("fee_rate_bps must be numeric") from exc
            if (
                not math.isfinite(normalized_fee_rate)
                or normalized_fee_rate < 0
                or normalized_fee_rate > 10_000
            ):
                raise AccountingInvariantError("fee_rate_bps must be within [0, 10000]")
        async with AsyncSessionLocal() as session:
            existing = await session.get(FillEvent, event_id)
            if existing is None:
                session.add(
                    FillEvent(
                        event_id=event_id,
                        exchange_order_id=str(exchange_order_id),
                        token_id=str(token_id) if token_id else None,
                        price=float(fill_price),
                        size=float(filled_size),
                        liquidity_role=normalized_role,
                        fee_rate_bps=normalized_fee_rate,
                        status="RECEIVED",
                        payload=dict(raw_event),
                    )
                )
                try:
                    await session.commit()
                except IntegrityError:
                    # A concurrent task inserted the same deterministic event id.
                    await session.rollback()

        return await self._process_event(event_id)

    async def _mark_failed(self, event_id: str, detail: str) -> FillProcessingResult:
        safe_detail = str(detail)[:1000]
        async with AsyncSessionLocal() as session:
            event = await session.get(FillEvent, event_id, with_for_update=True)
            if event and event.status != "PROCESSED":
                event.status = "FAILED"
                event.processing_error = safe_detail
                await session.commit()
        trading_safety.halt(f"fill accounting failure: {safe_detail}")
        logger.critical("[FILL] Event %s failed closed: %s", event_id[:12], safe_detail)
        return FillProcessingResult(event_id=event_id, status="FAILED", detail=safe_detail)

    async def _process_event(self, event_id: str) -> FillProcessingResult:
        snapshot: Optional[Dict[str, float]] = None
        publish_payload: Optional[Dict[str, str]] = None
        fee_fact_known = False

        try:
            async with AsyncSessionLocal() as session:
                event_stmt = (
                    select(FillEvent)
                    .filter(FillEvent.event_id == event_id)
                    .with_for_update()
                )
                event = (await session.execute(event_stmt)).scalar_one_or_none()
                if event is None:
                    return FillProcessingResult(
                        event_id=event_id,
                        status="MISSING",
                        detail="fill event was not found after inbox insert",
                    )
                if event.status == "PROCESSED":
                    return FillProcessingResult(
                        event_id=event_id,
                        status="PROCESSED",
                        duplicate=True,
                        detail="duplicate event ignored",
                    )
                if event.status == "FAILED":
                    return FillProcessingResult(
                        event_id=event_id,
                        status="FAILED",
                        duplicate=True,
                        detail=event.processing_error or "previous processing failure",
                    )

                order_stmt = (
                    select(OrderJournal)
                    .filter(
                        or_(
                            OrderJournal.exchange_order_id == event.exchange_order_id,
                            OrderJournal.order_id == event.exchange_order_id,
                        )
                    )
                    .with_for_update()
                )
                order = (await session.execute(order_stmt)).scalar_one_or_none()
                if order is None:
                    event.status = "UNMAPPED"
                    event.processing_error = "exchange order id is not mapped yet"
                    await session.commit()
                    return FillProcessingResult(
                        event_id=event_id,
                        status="UNMAPPED",
                        detail=event.processing_error,
                    )

                payload = dict(order.payload) if order.payload else {}
                token_id = event.token_id or payload.get("token_id")
                market = await session.get(MarketMeta, order.market_id)
                if not market or not token_id:
                    raise AccountingInvariantError("market/token mapping is unavailable for fill")
                if token_id not in {market.yes_token_id, market.no_token_id}:
                    raise AccountingInvariantError("fill token does not belong to order market")
                is_yes = token_id == market.yes_token_id

                current_filled = float(payload.get("filled_size", 0.0) or 0.0)
                fill_size = float(event.size)
                fee_amount = resolve_fee_amount(
                    event.payload or {},
                    event.liquidity_role,
                    price=event.price,
                    size=event.size,
                    fee_rate_bps=event.fee_rate_bps,
                )
                cash_fact = build_fill_cash_fact(
                    side=order.side.value,
                    price=float(event.price),
                    size=fill_size,
                    fee_amount=fee_amount,
                )
                new_total_filled = current_filled + fill_size
                original_size = float(order.size)
                if new_total_filled > original_size + 1e-6:
                    raise AccountingInvariantError(
                        f"cumulative fill {new_total_filled:.8f} exceeds order size {original_size:.8f}"
                    )

                # Lock/update the wallet reservation before inventory. The same transaction
                # then commits reservation release, cost basis, order state and inbox state.
                await risk_reservations.apply_fill_in_session(
                    session,
                    order.reservation_id,
                    fill_size,
                    float(event.price),
                    order_side=order.side.value,
                    required=True,
                )

                inv_stmt = (
                    select(InventoryLedger)
                    .filter(InventoryLedger.market_id == order.market_id)
                    .with_for_update()
                )
                inventory = (await session.execute(inv_stmt)).scalar_one_or_none()
                if inventory is None:
                    raise AccountingInvariantError("inventory ledger row is missing")
                if str(inventory.accounting_version or "v1") != "v2":
                    raise AccountingInvariantError(
                        "legacy v1 PnL ledger requires an offline accounting rebuild"
                    )

                realized_pnl = float(inventory.realized_pnl or 0.0)
                if is_yes:
                    accounting = apply_fill_accounting(
                        exposure=float(inventory.yes_exposure or 0.0),
                        capital_used=float(inventory.yes_capital_used or 0.0),
                        realized_pnl=realized_pnl,
                        side=order.side.value,
                        fill_size=fill_size,
                        fill_price=float(event.price),
                        fee_amount=fee_amount or 0.0,
                    )
                    inventory.yes_exposure = accounting.exposure
                    inventory.yes_capital_used = accounting.capital_used
                else:
                    accounting = apply_fill_accounting(
                        exposure=float(inventory.no_exposure or 0.0),
                        capital_used=float(inventory.no_capital_used or 0.0),
                        realized_pnl=realized_pnl,
                        side=order.side.value,
                        fill_size=fill_size,
                        fill_price=float(event.price),
                        fee_amount=fee_amount or 0.0,
                    )
                    inventory.no_exposure = accounting.exposure
                    inventory.no_capital_used = accounting.capital_used
                inventory.realized_pnl = accounting.realized_pnl
                inventory.state_version = int(inventory.state_version or 0) + 1

                # Persist signed gross cash and an explicit fee fact in this same
                # transaction. Missing fees stay unknown; zero is never invented.
                if await session.get(FillCashLedger, event_id) is not None:
                    raise AccountingInvariantError(
                        "cash fact already exists for an unprocessed fill event"
                    )
                session.add(
                    FillCashLedger(
                        event_id=event_id,
                        market_id=order.market_id,
                        side=order.side.value,
                        gross_cash_delta=cash_fact.gross_cash_delta,
                        fee_amount=cash_fact.fee_amount,
                        net_cash_delta=cash_fact.net_cash_delta,
                        fee_status=cash_fact.fee_status,
                    )
                )
                fee_fact_known = cash_fact.fee_status == "KNOWN"
                if not fee_fact_known:
                    # The provisional balances exclude an unknown fee and therefore
                    # cannot be represented as verified v2 net accounting.
                    inventory.accounting_version = "v2_fee_incomplete"

                payload["filled_size"] = new_total_filled
                payload["last_fill_event_id"] = event_id
                order.payload = payload
                fully_filled = new_total_filled >= original_size - 1e-6
                order.status = OrderStatus.FILLED if fully_filled else OrderStatus.OPEN

                event.local_order_id = order.order_id
                event.market_id = order.market_id
                event.token_id = token_id
                event.side = order.side.value
                event.accounting_state_version = int(inventory.state_version)
                event.status = "PROCESSED"
                event.processing_error = None
                event.processed_at = datetime.now(timezone.utc)

                snapshot = {
                    "market_id": order.market_id,
                    "yes_exposure": float(inventory.yes_exposure or 0.0),
                    "no_exposure": float(inventory.no_exposure or 0.0),
                    "yes_capital_used": float(inventory.yes_capital_used or 0.0),
                    "no_capital_used": float(inventory.no_capital_used or 0.0),
                    "realized_pnl": float(inventory.realized_pnl or 0.0),
                    "state_version": int(inventory.state_version or 0),
                }
                if fully_filled:
                    publish_payload = {
                        "market_id": order.market_id,
                        "token_id": token_id,
                        "order_id": event.exchange_order_id,
                    }
                await session.commit()
        except (AccountingInvariantError, ReservationInvariantError) as exc:
            return await self._mark_failed(event_id, str(exc))
        except Exception as exc:
            logger.exception("[FILL] Unexpected processing failure for %s", event_id[:12])
            return await self._mark_failed(event_id, f"unexpected fill processing error: {exc}")

        if snapshot:
            await inventory_state.apply_reconciliation_snapshot(
                market_id=str(snapshot["market_id"]),
                yes_exposure=snapshot["yes_exposure"],
                no_exposure=snapshot["no_exposure"],
                yes_capital_used=snapshot["yes_capital_used"],
                no_capital_used=snapshot["no_capital_used"],
                realized_pnl=snapshot["realized_pnl"],
                last_local_fill_timestamp=time.time(),
                state_version=int(snapshot["state_version"]),
            )
        if publish_payload:
            await redis_client.publish(
                f"order_status:{publish_payload['market_id']}:{publish_payload['token_id']}",
                {
                    "order_id": publish_payload["order_id"],
                    "status": "FILLED",
                },
            )
        if not fee_fact_known:
            detail = (
                f"execution fee is not authoritative for fill {event_id[:12]}; "
                "net PnL and new live risk are blocked"
            )
            trading_safety.set_readiness("accounting_integrity", False, detail)
            trading_safety.halt(detail)
        return FillProcessingResult(event_id=event_id, status="PROCESSED")

    async def retry_unmapped_for_exchange_order(
        self, exchange_order_id: str
    ) -> list[FillProcessingResult]:
        """Retry durable events that arrived before the OMS saved exchange id mapping."""
        async with AsyncSessionLocal() as session:
            stmt = select(FillEvent.event_id).filter(
                FillEvent.exchange_order_id == str(exchange_order_id),
                FillEvent.status == "UNMAPPED",
            )
            event_ids = list((await session.execute(stmt)).scalars().all())
        results = []
        for event_id in event_ids:
            results.append(await self._process_event(event_id))
        return results


fill_processor = FillProcessor()
