"""Conservative event-driven paper execution.

Paper orders are never sent to an exchange. Maker fills require an observed trade
print through the resting limit; taker fills consume only visible top-of-book
depth. Every simulated fill is explicitly tagged so it cannot masquerade as
production execution evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.core.config import settings


@dataclass
class PaperOrder:
    order_id: str
    condition_id: str
    token_id: str
    side: str
    price: float
    remaining_size: float
    post_only: bool
    created_at: float
    last_event_key: Optional[str] = None


@dataclass(frozen=True)
class PaperFillDecision:
    price: float
    size: float
    liquidity_role: str
    event_key: str
    fee_amount: float


def decide_paper_fill(order: PaperOrder, snapshot: Dict[str, Any]) -> Optional[PaperFillDecision]:
    if snapshot.get("valid") is not True or order.remaining_size <= 0:
        return None
    if order.post_only:
        raw_trade_price = snapshot.get("last_trade_price")
        raw_trade_size = snapshot.get("last_trade_size")
        event_key = str(snapshot.get("last_trade_id") or "").strip()
        if raw_trade_price in (None, "") or raw_trade_size in (None, "") or not event_key:
            return None
        try:
            trade_price = float(raw_trade_price)
            trade_size = float(raw_trade_size)
        except (TypeError, ValueError):
            return None
        eligible = (
            trade_price <= order.price
            if order.side == "BUY"
            else trade_price >= order.price
        )
        if not eligible or event_key == order.last_event_key:
            return None
        participation = min(
            1.0, max(0.0, float(settings.PAPER_MAKER_PARTICIPATION_RATE))
        )
        size = min(order.remaining_size, trade_size * participation)
        fill_price = order.price  # conservative limit-price execution
        role = "MAKER"
        fee = 0.0
    else:
        levels = snapshot.get("asks" if order.side == "BUY" else "bids") or []
        if not levels:
            return None
        try:
            top_price = float(levels[0]["price"])
            top_size = float(levels[0]["size"])
        except (KeyError, TypeError, ValueError):
            return None
        eligible = top_price <= order.price if order.side == "BUY" else top_price >= order.price
        if not eligible:
            return None
        event_key = f"book:{snapshot.get('snapshot_id') or snapshot.get('received_at')}"
        if event_key == order.last_event_key:
            return None
        size = min(order.remaining_size, top_size)
        fill_price = top_price
        role = "TAKER"
        fee_rate = max(0.0, float(settings.PAPER_TAKER_FEE_RATE))
        fee = size * fee_rate * fill_price * (1.0 - fill_price)

    if not all(math.isfinite(value) for value in (size, fill_price, fee)) or size <= 0:
        return None
    return PaperFillDecision(fill_price, size, role, event_key, fee)


class PaperExecutionSimulator:
    def __init__(self) -> None:
        self._orders: Dict[str, PaperOrder] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        *,
        order_id: str,
        condition_id: str,
        token_id: str,
        side: str,
        price: float,
        size: float,
        post_only: bool,
        created_at: Optional[float] = None,
    ) -> None:
        async with self._lock:
            self._orders[order_id] = PaperOrder(
                order_id=str(order_id),
                condition_id=str(condition_id),
                token_id=str(token_id),
                side=str(side).upper(),
                price=float(price),
                remaining_size=float(size),
                post_only=bool(post_only),
                created_at=float(created_at) if created_at is not None else time.time(),
            )

    async def unregister(self, order_id: str) -> None:
        async with self._lock:
            self._orders.pop(str(order_id), None)

    async def on_book(self, token_id: str, snapshot: Dict[str, Any]) -> None:
        from app.oms.fill_processor import fill_processor

        async with self._lock:
            candidates = [
                order
                for order in self._orders.values()
                if order.token_id == str(token_id)
            ]
            for order in candidates:
                decision = decide_paper_fill(order, snapshot)
                if decision is None:
                    continue
                material = (
                    f"paper-fill-v2|{order.order_id}|{decision.event_key}|"
                    f"{decision.liquidity_role}"
                )
                event_id = hashlib.sha256(material.encode("utf-8")).hexdigest()
                raw_event = {
                    "id": event_id,
                    "event_type": "paper_fill",
                    "_paper_simulation": True,
                    "_paper_model": "conservative-event-driven-v1",
                    "fee_amount": decision.fee_amount,
                    "source_event_key": decision.event_key,
                }
                result = await fill_processor.record_and_process(
                    event_id=event_id,
                    exchange_order_id=order.order_id,
                    filled_size=decision.size,
                    fill_price=decision.price,
                    raw_event=raw_event,
                    token_id=order.token_id,
                    liquidity_role=decision.liquidity_role,
                    fee_rate_bps=None,
                )
                if result.status != "PROCESSED":
                    continue
                if result.duplicate:
                    # This source print was already committed before a restart.
                    # The DB remaining amount already reflects it; only restore the
                    # in-memory dedupe cursor.
                    order.last_event_key = decision.event_key
                    continue
                order.remaining_size = max(0.0, order.remaining_size - decision.size)
                order.last_event_key = decision.event_key
                if order.remaining_size <= 1e-9:
                    self._orders.pop(order.order_id, None)


paper_execution = PaperExecutionSimulator()
