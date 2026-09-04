"""Authoritative, fail-closed reconciliation of local and exchange order facts."""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from sqlalchemy import or_
from sqlalchemy.future import select

from app.core.trading_safety import trading_safety
from app.db.session import AsyncSessionLocal
from app.models.db_models import (
    ExchangeOrderSnapshot,
    OrderJournal,
    OrderReconciliationRun,
    OrderStatus,
    PortfolioRiskState,
    RiskReservation,
)
from app.risk.reservations import ACTIVE_RESERVATION_STATUSES


logger = logging.getLogger(__name__)
OPEN_EXCHANGE_STATUSES = {"LIVE", "OPEN", "ACTIVE", "PENDING"}
CANCELED_EXCHANGE_STATUSES = {"CANCELED", "CANCELLED", "CANCELLATION", "EXPIRED"}
FILLED_EXCHANGE_STATUSES = {"MATCHED", "FILLED"}
UNSETTLED_TRADE_STATUSES = {
    "MATCHED",
    "MATCHED_NOT_BROADCASTED",
    "MINED",
    "RETRYING",
}
TOLERANCE = 1e-6


class ExchangeOrderParseError(ValueError):
    """Exchange order payload lacks a trustworthy identity or quantity."""


@dataclass(frozen=True)
class LocalOrderFact:
    local_order_id: str
    exchange_order_id: Optional[str]
    market_id: str
    token_id: str
    side: str
    price: float
    original_size: float
    locally_filled_size: float
    status: str
    reservation_id: Optional[str]
    reservation_market_id: Optional[str]
    reservation_token_id: Optional[str]
    reservation_side: Optional[str]
    reservation_limit_price: Optional[float]
    reservation_original_size: Optional[float]
    reservation_status: Optional[str]
    reservation_remaining_size: Optional[float]
    reservation_notional: Optional[float]


@dataclass(frozen=True)
class ExchangeOrderFact:
    exchange_order_id: str
    market_id: str
    token_id: str
    side: str
    price: float
    original_size: float
    matched_size: float
    status: str
    raw: Dict[str, Any]

    @property
    def remaining_size(self) -> float:
        return max(0.0, self.original_size - self.matched_size)


@dataclass(frozen=True)
class ReconciliationAction:
    local_order_id: Optional[str]
    exchange_order_id: Optional[str]
    kind: str
    blocker: bool
    reason: str
    exchange_remaining_size: Optional[float] = None
    sticky: bool = True


@dataclass(frozen=True)
class ReconciliationReport:
    actions: tuple[ReconciliationAction, ...]

    @property
    def blockers(self) -> tuple[ReconciliationAction, ...]:
        return tuple(action for action in self.actions if action.blocker)

    @property
    def safe(self) -> bool:
        return not self.blockers

    @property
    def sticky_blockers(self) -> tuple[ReconciliationAction, ...]:
        return tuple(action for action in self.blockers if action.sticky)


@dataclass(frozen=True)
class RecoveredFill:
    exchange_order_id: str
    token_id: str
    price: float
    size: float
    liquidity_role: str
    fee_rate_bps: Optional[float]
    raw: Dict[str, Any]


def _first(payload: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if payload.get(key) not in (None, ""):
            return payload[key]
    return None


def normalize_exchange_order(payload: Dict[str, Any]) -> ExchangeOrderFact:
    if not isinstance(payload, dict):
        raise ExchangeOrderParseError("exchange order must be an object")
    exchange_order_id = str(_first(payload, "id", "orderID", "order_id") or "").strip()
    market_id = str(_first(payload, "market", "condition_id") or "").strip()
    token_id = str(_first(payload, "asset_id", "token_id") or "").strip()
    side = str(payload.get("side") or "").strip().upper()
    status = str(payload.get("status") or "").strip().upper()
    if status.startswith("ORDER_STATUS_"):
        status = status[len("ORDER_STATUS_") :]
    size_encoding = str(payload.get("_size_encoding") or "").strip().lower()
    if size_encoding not in {"human", "e6"}:
        raise ExchangeOrderParseError(
            "exchange order must declare _size_encoding as human or e6"
        )
    try:
        price = float(payload.get("price"))
        original_size = float(_first(payload, "original_size", "size"))
        matched_size = float(
            _first(payload, "size_matched", "matched_size", "filled_size") or 0.0
        )
    except (TypeError, ValueError) as exc:
        raise ExchangeOrderParseError("exchange price/size fields are invalid") from exc
    if size_encoding == "e6":
        original_size /= 1_000_000.0
        matched_size /= 1_000_000.0
    if not exchange_order_id or not market_id or not token_id:
        raise ExchangeOrderParseError("exchange order identity is incomplete")
    if side not in {"BUY", "SELL"} or not status:
        raise ExchangeOrderParseError("exchange side/status is invalid")
    if not all(math.isfinite(value) for value in (price, original_size, matched_size)):
        raise ExchangeOrderParseError("exchange price/size must be finite")
    if (
        not 0.0 < price < 1.0
        or original_size <= 0
        or matched_size < 0
        or matched_size > original_size + TOLERANCE
    ):
        raise ExchangeOrderParseError("exchange price/size is outside safe bounds")
    return ExchangeOrderFact(
        exchange_order_id=exchange_order_id,
        market_id=market_id,
        token_id=token_id,
        side=side,
        price=price,
        original_size=original_size,
        matched_size=matched_size,
        status=status,
        raw=dict(payload),
    )


def extract_fills_for_order(
    trades: Iterable[Dict[str, Any]], exchange_order_id: str
) -> list[RecoveredFill]:
    """Extract role-specific human-unit fills from authenticated account trades."""
    target = str(exchange_order_id)
    output: list[RecoveredFill] = []
    for trade in trades:
        if not isinstance(trade, dict):
            raise ExchangeOrderParseError("account trade must be an object")
        status = str(trade.get("status") or "").upper()
        if status.startswith("TRADE_STATUS_"):
            status = status[len("TRADE_STATUS_") :]
        # Settlement can permanently fail before CONFIRMED. Only the terminal
        # success state is an authoritative cash/inventory mutation.
        if status != "CONFIRMED":
            continue
        encoding = str(trade.get("_size_encoding") or "").lower()
        if encoding not in {"human", "e6"}:
            raise ExchangeOrderParseError("account trade has no trusted size encoding")
        divisor = 1_000_000.0 if encoding == "e6" else 1.0

        if str(trade.get("taker_order_id") or "") == target:
            try:
                size = float(trade.get("size")) / divisor
                price = float(trade.get("price"))
                fee_rate = float(trade.get("fee_rate_bps"))
            except (TypeError, ValueError) as exc:
                raise ExchangeOrderParseError("taker trade values are invalid") from exc
            output.append(
                RecoveredFill(
                    exchange_order_id=target,
                    token_id=str(trade.get("asset_id") or trade.get("token_id") or ""),
                    price=price,
                    size=size,
                    liquidity_role="TAKER",
                    fee_rate_bps=fee_rate,
                    raw=dict(trade),
                )
            )

        maker_orders = trade.get("maker_orders") or []
        if not isinstance(maker_orders, list):
            raise ExchangeOrderParseError("maker_orders must be a list")
        for maker in maker_orders:
            if not isinstance(maker, dict) or str(maker.get("order_id") or "") != target:
                continue
            try:
                size = float(maker.get("matched_amount")) / divisor
                price = float(maker.get("price"))
                fee_rate_raw = maker.get("fee_rate_bps")
                fee_rate = (
                    float(fee_rate_raw) if fee_rate_raw not in (None, "") else None
                )
            except (TypeError, ValueError) as exc:
                raise ExchangeOrderParseError("maker trade values are invalid") from exc
            output.append(
                RecoveredFill(
                    exchange_order_id=target,
                    token_id=str(maker.get("asset_id") or maker.get("token_id") or ""),
                    price=price,
                    size=size,
                    liquidity_role="MAKER",
                    fee_rate_bps=fee_rate,
                    raw=dict(trade),
                )
            )

    for fill in output:
        if (
            not fill.token_id
            or not math.isfinite(fill.price)
            or not math.isfinite(fill.size)
            or not 0 < fill.price < 1
            or fill.size <= 0
        ):
            raise ExchangeOrderParseError("recovered trade fill is outside safe bounds")
    return output


def extract_unsettled_size_for_order(
    trades: Iterable[Dict[str, Any]], exchange_order_id: str
) -> float:
    """Return this order's matched shares that have not reached a terminal state.

    The CLOB can expose matched size before settlement is CONFIRMED.  Those shares
    must block new risk temporarily, but must not mutate cash/inventory or create a
    sticky incident because settlement can still become CONFIRMED or FAILED.
    """
    target = str(exchange_order_id)
    total = 0.0
    for trade in trades:
        if not isinstance(trade, dict):
            raise ExchangeOrderParseError("account trade must be an object")
        status = str(trade.get("status") or "").upper()
        if status.startswith("TRADE_STATUS_"):
            status = status[len("TRADE_STATUS_") :]
        if status not in UNSETTLED_TRADE_STATUSES:
            continue
        encoding = str(trade.get("_size_encoding") or "").lower()
        if encoding not in {"human", "e6"}:
            raise ExchangeOrderParseError("account trade has no trusted size encoding")
        divisor = 1_000_000.0 if encoding == "e6" else 1.0
        matched = 0.0
        if str(trade.get("taker_order_id") or "") == target:
            try:
                matched += float(trade.get("size")) / divisor
            except (TypeError, ValueError) as exc:
                raise ExchangeOrderParseError("unsettled taker size is invalid") from exc
        maker_orders = trade.get("maker_orders") or []
        if not isinstance(maker_orders, list):
            raise ExchangeOrderParseError("maker_orders must be a list")
        for maker in maker_orders:
            if not isinstance(maker, dict):
                raise ExchangeOrderParseError("maker order must be an object")
            if str(maker.get("order_id") or "") != target:
                continue
            try:
                matched += float(maker.get("matched_amount")) / divisor
            except (TypeError, ValueError) as exc:
                raise ExchangeOrderParseError("unsettled maker size is invalid") from exc
        if not math.isfinite(matched) or matched < 0:
            raise ExchangeOrderParseError("unsettled matched size is outside safe bounds")
        total += matched
    if not math.isfinite(total):
        raise ExchangeOrderParseError("unsettled matched size is outside safe bounds")
    return total


def _identity_conflict(local: LocalOrderFact, exchange: ExchangeOrderFact) -> Optional[str]:
    if local.market_id != exchange.market_id:
        return "market id differs"
    if local.token_id != exchange.token_id:
        return "token id differs"
    if local.side != exchange.side:
        return "side differs"
    if abs(local.price - exchange.price) > TOLERANCE:
        return "limit price differs"
    if abs(local.original_size - exchange.original_size) > TOLERANCE:
        return "original size differs"
    if local.reservation_side is not None and local.reservation_side != local.side:
        return "reservation side differs from local order"
    if (
        local.reservation_market_id is not None
        and local.reservation_market_id != local.market_id
    ):
        return "reservation market differs from local order"
    if (
        local.reservation_token_id is not None
        and local.reservation_token_id != local.token_id
    ):
        return "reservation token differs from local order"
    if (
        local.reservation_limit_price is not None
        and abs(local.reservation_limit_price - local.price) > TOLERANCE
    ):
        return "reservation price differs from local order"
    if (
        local.reservation_original_size is not None
        and abs(local.reservation_original_size - local.original_size) > TOLERANCE
    ):
        return "reservation original size differs from local order"
    return None


def reconcile_order_facts(
    local_orders: Iterable[LocalOrderFact],
    exchange_open_orders: Iterable[ExchangeOrderFact],
    exchange_details: Dict[str, ExchangeOrderFact],
    unsettled_sizes: Optional[Dict[str, float]] = None,
) -> ReconciliationReport:
    locals_list = list(local_orders)
    open_by_id = {order.exchange_order_id: order for order in exchange_open_orders}
    local_exchange_ids = {
        order.exchange_order_id for order in locals_list if order.exchange_order_id
    }
    actions = []
    pending_by_id = unsettled_sizes or {}

    for local in locals_list:
        if not local.exchange_order_id:
            actions.append(
                ReconciliationAction(
                    local.local_order_id,
                    None,
                    "UNKNOWN",
                    True,
                    "local order has no exchange id and cannot be authoritatively queried",
                )
            )
            continue
        exchange = open_by_id.get(local.exchange_order_id) or exchange_details.get(
            local.exchange_order_id
        )
        if exchange is None:
            actions.append(
                ReconciliationAction(
                    local.local_order_id,
                    local.exchange_order_id,
                    "UNKNOWN",
                    True,
                    "exchange order detail is unavailable",
                )
            )
            continue
        conflict = _identity_conflict(local, exchange)
        if conflict:
            actions.append(
                ReconciliationAction(
                    local.local_order_id,
                    exchange.exchange_order_id,
                    "CONFLICT",
                    True,
                    conflict,
                )
            )
            continue
        fill_delta = exchange.matched_size - local.locally_filled_size
        if fill_delta > TOLERANCE:
            unsettled_size = float(pending_by_id.get(exchange.exchange_order_id, 0.0))
            if unsettled_size + TOLERANCE >= fill_delta:
                actions.append(
                    ReconciliationAction(
                        local.local_order_id,
                        exchange.exchange_order_id,
                        "SETTLEMENT_PENDING",
                        True,
                        (
                            f"{fill_delta:.8f} matched shares await terminal "
                            "CONFIRMED/FAILED settlement"
                        ),
                        exchange.remaining_size,
                        False,
                    )
                )
                continue
            actions.append(
                ReconciliationAction(
                    local.local_order_id,
                    exchange.exchange_order_id,
                    "MISSING_FILLS",
                    True,
                    f"exchange has {fill_delta:.8f} more matched shares than local accounting",
                )
            )
            continue
        if fill_delta < -TOLERANCE:
            actions.append(
                ReconciliationAction(
                    local.local_order_id,
                    exchange.exchange_order_id,
                    "CONFLICT",
                    True,
                    "local accounted fills exceed exchange matched size",
                )
            )
            continue

        if exchange.status in OPEN_EXCHANGE_STATUSES:
            if not local.reservation_id or local.reservation_remaining_size is None:
                actions.append(
                    ReconciliationAction(
                        local.local_order_id,
                        exchange.exchange_order_id,
                        "MISSING_RESERVATION",
                        True,
                        "open exchange order has no durable reservation",
                    )
                )
                continue
            actions.append(
                ReconciliationAction(
                    local.local_order_id,
                    exchange.exchange_order_id,
                    "OPEN_CONFIRMED",
                    False,
                    "exchange order is open and fills agree",
                    exchange.remaining_size,
                )
            )
            continue

        if exchange.status in CANCELED_EXCHANGE_STATUSES:
            actions.append(
                ReconciliationAction(
                    local.local_order_id,
                    exchange.exchange_order_id,
                    "CANCELED_CONFIRMED",
                    False,
                    "exchange terminal cancel and matched size agree",
                    0.0,
                )
            )
            continue

        if exchange.status in FILLED_EXCHANGE_STATUSES:
            if exchange.remaining_size > TOLERANCE:
                actions.append(
                    ReconciliationAction(
                        local.local_order_id,
                        exchange.exchange_order_id,
                        "CONFLICT",
                        True,
                        "filled exchange status has nonzero remaining size",
                    )
                )
            elif not local.reservation_id or (
                local.reservation_remaining_size is None
                or local.reservation_remaining_size > TOLERANCE
            ):
                actions.append(
                    ReconciliationAction(
                        local.local_order_id,
                        exchange.exchange_order_id,
                        "RESERVATION_MISMATCH",
                        True,
                        "fully filled order still has missing/nonzero reservation",
                    )
                )
            else:
                actions.append(
                    ReconciliationAction(
                        local.local_order_id,
                        exchange.exchange_order_id,
                        "FILLED_CONFIRMED",
                        False,
                        "exchange and local accounting both show a full fill",
                        0.0,
                    )
                )
            continue

        actions.append(
            ReconciliationAction(
                local.local_order_id,
                exchange.exchange_order_id,
                "UNKNOWN",
                True,
                f"unsupported exchange status={exchange.status}",
            )
        )

    for exchange_order_id in sorted(set(open_by_id) - local_exchange_ids):
        actions.append(
            ReconciliationAction(
                None,
                exchange_order_id,
                "EXTERNAL_ORPHAN",
                True,
                "exchange has an open order with no local journal mapping",
                open_by_id[exchange_order_id].remaining_size,
            )
        )
    return ReconciliationReport(tuple(actions))


async def _call_exchange_method(method, *args, **kwargs):
    """Await async adapters while keeping synchronous offline test doubles usable."""
    if inspect.iscoroutinefunction(method):
        return await method(*args, **kwargs)
    result = await asyncio.to_thread(method, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


class OrderReconciliationService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def _record_failed_run(self, run_id: str, error: Exception) -> None:
        try:
            async with AsyncSessionLocal() as session:
                session.add(
                    OrderReconciliationRun(
                        run_id=run_id,
                        status="FAILED",
                        local_order_count=0,
                        exchange_open_count=0,
                        blocker_count=1,
                        summary={"error": str(error)[:1000]},
                        completed_at=datetime.now(timezone.utc),
                    )
                )
                await session.commit()
        except Exception:
            logger.exception("Could not persist failed order reconciliation audit run")

    async def _load_local_facts(self) -> list[LocalOrderFact]:
        async with AsyncSessionLocal() as session:
            stmt = (
                select(OrderJournal, RiskReservation)
                .outerjoin(
                    RiskReservation,
                    OrderJournal.reservation_id == RiskReservation.reservation_id,
                )
                .filter(
                    or_(
                        OrderJournal.status.in_(
                            [OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.UNKNOWN]
                        ),
                        RiskReservation.status == "CANCEL_PENDING_RECONCILE",
                    )
                )
            )
            rows = (await session.execute(stmt)).all()
        facts = []
        for order, reservation in rows:
            payload = dict(order.payload or {})
            facts.append(
                LocalOrderFact(
                    local_order_id=order.order_id,
                    exchange_order_id=order.exchange_order_id,
                    market_id=order.market_id,
                    token_id=str(payload.get("token_id") or ""),
                    side=order.side.value,
                    price=float(order.price),
                    original_size=float(order.size),
                    locally_filled_size=float(payload.get("filled_size", 0.0) or 0.0),
                    status=order.status.value,
                    reservation_id=order.reservation_id,
                    reservation_market_id=(
                        reservation.market_id if reservation else None
                    ),
                    reservation_token_id=(
                        reservation.token_id if reservation else None
                    ),
                    reservation_side=reservation.side if reservation else None,
                    reservation_limit_price=(
                        float(reservation.limit_price) if reservation else None
                    ),
                    reservation_original_size=(
                        float(reservation.original_size) if reservation else None
                    ),
                    reservation_status=reservation.status if reservation else None,
                    reservation_remaining_size=(
                        float(reservation.remaining_size) if reservation else None
                    ),
                    reservation_notional=(
                        float(reservation.reserved_notional) if reservation else None
                    ),
                )
            )
        return facts

    async def _fetch_exchange_facts(self, client, local_facts):
        raw_open = await _call_exchange_method(client.get_orders)
        if isinstance(raw_open, dict):
            page = raw_open
            raw_open = page.get("data")
            cursor = page.get("next_cursor")
            if cursor not in (None, "", "END", "LTE=", "-1"):
                raise ExchangeOrderParseError(
                    "raw paginated get_orders response was not fully drained"
                )
        if not isinstance(raw_open, list):
            raise ExchangeOrderParseError("get_orders did not return a list/page")
        open_facts = [normalize_exchange_order(item) for item in raw_open]
        open_ids = {item.exchange_order_id for item in open_facts}
        details: Dict[str, ExchangeOrderFact] = {}
        raw_details: Dict[str, Dict[str, Any]] = {}
        for local in local_facts:
            exchange_id = local.exchange_order_id
            if not exchange_id or exchange_id in open_ids or exchange_id in details:
                continue
            raw = await _call_exchange_method(client.get_order, exchange_id)
            fact = normalize_exchange_order(raw)
            if fact.exchange_order_id != exchange_id:
                raise ExchangeOrderParseError("get_order returned a different order id")
            details[exchange_id] = fact
            raw_details[exchange_id] = dict(raw)
        return open_facts, details, raw_open, raw_details

    async def _recover_exchange_fills(
        self, client, local_facts
    ) -> Dict[str, float]:
        if not local_facts:
            return {}
        get_trades = getattr(client, "get_trades", None)
        if not callable(get_trades):
            raise ExchangeOrderParseError(
                "authenticated adapter cannot enumerate paginated account trades"
            )
        from app.oms.fill_processor import derive_fill_event_id, fill_processor

        unsettled_sizes: Dict[str, float] = {}
        by_market: Dict[str, list[LocalOrderFact]] = {}
        for local in local_facts:
            if local.exchange_order_id:
                by_market.setdefault(local.market_id, []).append(local)

        for market_id, market_orders in by_market.items():
            # The authenticated CLOB API filters by trade id, token or market;
            # it has no order-id filter. Fully drain once per condition, then
            # select role-specific order references locally.
            raw_trades = await _call_exchange_method(get_trades, market=market_id)
            if isinstance(raw_trades, dict):
                page = raw_trades
                raw_trades = page.get("data")
                if page.get("next_cursor") not in (None, "", "END", "LTE=", "-1"):
                    raise ExchangeOrderParseError(
                        "raw paginated account trades were not fully drained"
                    )
            if not isinstance(raw_trades, list):
                raise ExchangeOrderParseError("get_trades did not return a list/page")
            for local in market_orders:
                exchange_id = str(local.exchange_order_id)
                unsettled_sizes[exchange_id] = extract_unsettled_size_for_order(
                    raw_trades, exchange_id
                )
                for fill in extract_fills_for_order(raw_trades, exchange_id):
                    event_id = derive_fill_event_id(
                        fill.raw, fill.exchange_order_id, fill.liquidity_role.lower()
                    )
                    result = await fill_processor.record_and_process(
                        event_id=event_id,
                        exchange_order_id=fill.exchange_order_id,
                        filled_size=fill.size,
                        fill_price=fill.price,
                        raw_event=fill.raw,
                        token_id=fill.token_id,
                        liquidity_role=fill.liquidity_role,
                        fee_rate_bps=fill.fee_rate_bps,
                    )
                    if result.status not in {"PROCESSED"}:
                        raise ExchangeOrderParseError(
                            "authenticated trade recovery failed with "
                            f"status={result.status}"
                        )
        return unsettled_sizes

    async def _persist_and_apply(
        self,
        run_id: str,
        local_facts: list[LocalOrderFact],
        open_facts: list[ExchangeOrderFact],
        details: Dict[str, ExchangeOrderFact],
        raw_open: list[Dict[str, Any]],
        raw_details: Dict[str, Dict[str, Any]],
        report: ReconciliationReport,
    ) -> None:
        raw_by_id = {
            normalize_exchange_order(raw).exchange_order_id: raw for raw in raw_open
        }
        async with AsyncSessionLocal() as session:
            run = OrderReconciliationRun(
                run_id=run_id,
                status="SAFE" if report.safe else "BLOCKED",
                local_order_count=len(local_facts),
                exchange_open_count=len(open_facts),
                blocker_count=len(report.blockers),
                summary={
                    "actions": [asdict(action) for action in report.actions],
                },
                completed_at=datetime.now(timezone.utc),
            )
            session.add(run)
            for exchange_id, raw in {**raw_by_id, **raw_details}.items():
                source = "OPEN_LIST" if exchange_id in raw_by_id else "ORDER_DETAIL"
                session.add(
                    ExchangeOrderSnapshot(
                        run_id=run_id,
                        exchange_order_id=exchange_id,
                        source=source,
                        status=str(raw.get("status") or ""),
                        payload=dict(raw),
                    )
                )

            state = (
                await session.execute(
                    select(PortfolioRiskState)
                    .filter(PortfolioRiskState.wallet_id == "default")
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if state is None:
                state = PortfolioRiskState(wallet_id="default")
                session.add(state)
                await session.flush()

            exchange_by_id = {
                fact.exchange_order_id: fact for fact in [*open_facts, *details.values()]
            }
            for action in report.actions:
                if not action.local_order_id:
                    continue
                order = await session.get(
                    OrderJournal, action.local_order_id, with_for_update=True
                )
                if order is None:
                    raise RuntimeError("local order disappeared during reconciliation")
                payload = dict(order.payload or {})
                payload["last_order_reconciliation_run"] = run_id
                payload["reconciliation_action"] = action.kind
                payload["reconciliation_reason"] = action.reason
                order.payload = payload

                reservation = None
                if order.reservation_id:
                    reservation = await session.get(
                        RiskReservation, order.reservation_id, with_for_update=True
                    )

                if action.blocker and action.sticky:
                    order.status = OrderStatus.UNKNOWN
                    if reservation and reservation.status in ACTIVE_RESERVATION_STATUSES:
                        reservation.status = "UNKNOWN"
                    continue
                if action.blocker:
                    # Expected in-flight settlement freezes readiness but preserves the
                    # order/reservation facts until a terminal trade state is observed.
                    continue
                exchange = exchange_by_id[action.exchange_order_id]
                if action.kind == "OPEN_CONFIRMED":
                    if reservation is None:
                        raise RuntimeError("confirmed open order lost its reservation")
                    remaining = float(action.exchange_remaining_size or 0.0)
                    reservation.exchange_order_id = exchange.exchange_order_id
                    reservation.remaining_size = remaining
                    reservation.reserved_notional = (
                        remaining * float(reservation.limit_price)
                        if reservation.side == "BUY"
                        else 0.0
                    )
                    reservation.status = "OPEN"
                    order.status = OrderStatus.OPEN
                elif action.kind == "CANCELED_CONFIRMED":
                    if reservation:
                        reservation.remaining_size = 0.0
                        reservation.reserved_notional = 0.0
                        reservation.status = "RECONCILED_CANCELED"
                    order.status = OrderStatus.CANCELED
                elif action.kind == "FILLED_CONFIRMED":
                    if reservation:
                        reservation.remaining_size = 0.0
                        reservation.reserved_notional = 0.0
                        reservation.status = "FILLED"
                    order.status = OrderStatus.FILLED

            reserved_total = sum(
                float(value or 0.0)
                for value in (
                    await session.execute(
                        select(RiskReservation.reserved_notional).filter(
                            RiskReservation.status.in_(ACTIVE_RESERVATION_STATUSES)
                        )
                    )
                ).scalars()
            )
            state.reserved_buy_notional = reserved_total
            state.state_version = int(state.state_version or 0) + 1
            await session.commit()

    async def _reconcile_locked(self, client) -> ReconciliationReport:
        run_id = uuid.uuid4().hex
        try:
            local_facts = await self._load_local_facts()
            unsettled_sizes = await self._recover_exchange_fills(client, local_facts)
            local_facts = await self._load_local_facts()
            (
                open_facts,
                details,
                raw_open,
                raw_details,
            ) = await self._fetch_exchange_facts(client, local_facts)
            report = reconcile_order_facts(
                local_facts, open_facts, details, unsettled_sizes
            )
            await self._persist_and_apply(
                run_id,
                local_facts,
                open_facts,
                details,
                raw_open,
                raw_details,
                report,
            )
        except Exception as exc:
            await self._record_failed_run(run_id, exc)
            trading_safety.set_readiness(
                "open_orders_reconciled", False, "authoritative order reconciliation failed"
            )
            trading_safety.halt(f"order reconciliation failed: {exc}")
            logger.exception("Authoritative order reconciliation failed")
            raise

        if report.safe:
            trading_safety.set_readiness(
                "open_orders_reconciled",
                True,
                f"authoritative pass {run_id[:12]} found no conflicts",
            )
            # The same authenticated REST credentials back the user WebSocket.
            # A successful REST pass plus the live SDK subscription is the strongest
            # acknowledgement available in the documented user-stream contract.
            from app.market_data.user_stream import user_stream

            user_stream.confirm_authenticated_rest()
        else:
            detail = "; ".join(
                f"{item.kind}:{item.reason}" for item in report.blockers[:3]
            )
            trading_safety.set_readiness("open_orders_reconciled", False, detail)
            if report.sticky_blockers:
                trading_safety.halt(
                    "order reconciliation found "
                    f"{len(report.sticky_blockers)} persistent blocker(s)"
                )
        return report

    async def reconcile(self, client) -> ReconciliationReport:
        async with self._lock:
            return await self._reconcile_locked(client)


order_reconciliation_service = OrderReconciliationService()
