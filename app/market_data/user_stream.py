import asyncio
import logging
import time
from typing import Dict, Optional, Set

from app.core.config import settings
from app.core.redis import redis_client
from app.oms.core import oms
from app.oms.fill_processor import derive_fill_event_id, fill_processor
from app.risk.reservations import risk_reservations
from app.risk.watchdog import watchdog
from app.core.trading_safety import trading_safety
from app.db.session import AsyncSessionLocal
from app.models.db_models import OrderJournal, OrderStatus
from app.oms.polymarket_v2 import (
    ExchangeContractError,
    normalize_exchange_status,
    normalize_sdk_stream_event,
)
from sqlalchemy import or_
from sqlalchemy.future import select

logger = logging.getLogger(__name__)


class UserStreamGateway:
    def __init__(self):
        self.subscribed_markets: Set[str] = set() # Condition IDs
        self.market_tokens: Dict[str, Dict[str, str]] = {}
        self.stream = None
        self.reconnect_delay = 1.0
        self.max_reconnect_delay = 60.0
        self._sdk_subscription_active = False
        self._authenticated_rest_confirmed = False

    def _refresh_readiness(self) -> None:
        ready = (
            self._sdk_subscription_active
            and self._authenticated_rest_confirmed
        )
        missing = []
        if not self._sdk_subscription_active:
            missing.append("SDK-authenticated subscription")
        if not self._authenticated_rest_confirmed:
            missing.append("authenticated REST reconciliation")
        trading_safety.set_readiness(
            "user_stream",
            ready,
            "SDK user subscription and authoritative REST reconciliation confirmed"
            if ready
            else f"awaiting {', '.join(missing) or 'connection'}",
        )

    def confirm_authenticated_rest(self) -> None:
        """Bind WS readiness to a successful authenticated REST reconciliation."""
        self._authenticated_rest_confirmed = True
        self._refresh_readiness()

    async def connect(self):
        """Consume the pinned SDK's authenticated stream and reconcile every gap."""
        while True:
            if oms.client is not None:
                break
            logger.debug("UserStreamGateway waiting for V2 adapter...")
            await asyncio.sleep(2.0)

        while True:
            connected_at = None
            periodic_task = None
            try:
                self.stream = await oms.client.subscribe_user()
                connected_at = time.monotonic()
                self._sdk_subscription_active = oms.client.user_stream_is_open()
                if not self._sdk_subscription_active:
                    raise ExchangeContractError("SDK user subscription is not open")
                self._refresh_readiness()
                logger.info("Pinned SDK user stream subscribed.")

                # Authoritative reads close the connect/subscribe race. The same pass is
                # repeated periodically because the SDK transparently reconnects sockets.
                from app.oms.order_reconciliation import order_reconciliation_service

                await order_reconciliation_service.reconcile(oms.client)
                if await watchdog.reconcile_positions() is not True:
                    raise ExchangeContractError(
                        "initial position reconciliation did not pass"
                    )
                periodic_task = asyncio.create_task(self._periodic_reconciliation())

                last_dropped = int(getattr(self.stream, "dropped", 0) or 0)
                while True:
                    if periodic_task.done():
                        exc = periodic_task.exception()
                        raise ExchangeContractError(
                            f"periodic authenticated reconciliation stopped: {exc}"
                        )
                    try:
                        event = await asyncio.wait_for(
                            self.stream.__anext__(), timeout=1.0
                        )
                    except asyncio.TimeoutError:
                        is_open = oms.client.user_stream_is_open()
                        if not is_open and self._sdk_subscription_active:
                            self._sdk_subscription_active = False
                            self._authenticated_rest_confirmed = False
                            self._refresh_readiness()
                        elif is_open and not self._sdk_subscription_active:
                            # The SDK reconnects internally. Close the missed-event gap
                            # with authenticated REST before restoring readiness.
                            self._sdk_subscription_active = True
                            self._refresh_readiness()
                            await order_reconciliation_service.reconcile(oms.client)
                            if await watchdog.reconcile_positions() is not True:
                                raise ExchangeContractError(
                                    "post-reconnect position reconciliation did not pass"
                                )
                        continue

                    dropped = int(getattr(self.stream, "dropped", 0) or 0)
                    if dropped > last_dropped:
                        self._sdk_subscription_active = False
                        self._authenticated_rest_confirmed = False
                        self._refresh_readiness()
                        trading_safety.halt(
                            f"authenticated user stream dropped {dropped - last_dropped} event(s)"
                        )
                        raise ExchangeContractError("SDK user stream queue overflowed")
                    last_dropped = dropped
                    self._sdk_subscription_active = oms.client.user_stream_is_open()
                    self._refresh_readiness()
                    await self.process_message(normalize_sdk_stream_event(event))

            except Exception as e:
                logger.exception("SDK user stream loop failed: %s", e)
            finally:
                if periodic_task is not None:
                    periodic_task.cancel()
                    await asyncio.gather(periodic_task, return_exceptions=True)
                if self.stream is not None:
                    await self.stream.close()
                    self.stream = None
                self._sdk_subscription_active = False
                self._authenticated_rest_confirmed = False
                trading_safety.set_readiness(
                    "user_stream", False, "SDK user stream is disconnected"
                )
                connected_for = 0.0
                if connected_at is not None:
                    connected_for = max(0.0, time.monotonic() - connected_at)
                if connected_for >= 60.0:
                    self.reconnect_delay = 1.0
                logger.warning(
                    f"User WS reconnecting in {self.reconnect_delay:.1f}s "
                    f"(last_session={connected_for:.1f}s)."
                )
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)

    async def _periodic_reconciliation(self) -> None:
        from app.oms.order_reconciliation import order_reconciliation_service

        interval = max(
            15.0,
            float(getattr(settings, "ORDER_RECONCILIATION_INTERVAL_SEC", 60.0)),
        )
        try:
            while True:
                await asyncio.sleep(interval)
                await order_reconciliation_service.reconcile(oms.client)
                if await watchdog.reconcile_positions() is not True:
                    raise ExchangeContractError(
                        "periodic position reconciliation did not pass"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._authenticated_rest_confirmed = False
            self._refresh_readiness()
            trading_safety.halt(f"periodic authenticated reconciliation failed: {exc}")
            raise

    async def subscribe(self, condition_id: str):
        """Track condition_id locally only. Do NOT send subscribe to User WS."""
        self.subscribed_markets.add(condition_id)
        logger.info("User WS: added condition_id to local tracking: %s", condition_id[:20] + "..." if len(condition_id) > 20 else condition_id)

    async def process_message(self, data: dict):
        # We need to handle trades and cancellations carefully
        
        # 1. Order Canceled/Closed (Could be full cancel, or the remainder of a partial fill)
        if isinstance(data, list) and len(data) > 0 and "event_type" in data[0]:
            # Sometimes polymarket sends arrays of events
            for event in data:
                await self._process_single_event(event)
        elif isinstance(data, dict):
            await self._process_single_event(data)

    async def _process_single_event(self, data: dict):
        event_type = str(data.get("event_type") or "").lower()
        
        if event_type == "trade":
            # Match status is usually "MATCHED" for a fill
            status = normalize_exchange_status(data.get("status"), "TRADE_STATUS_")
            # Only terminal confirmation mutates cash/inventory. Earlier match/mined
            # states can permanently fail and are recovered by the periodic REST pass.
            if status == "CONFIRMED":
                maker_orders = data.get("maker_orders", [])
                taker_order_id = data.get("taker_order_id")
                
                # We need to process each maker order we might own
                for maker in maker_orders:
                    order_id = maker.get("order_id")
                    matched_amount = float(maker.get("matched_amount", 0))
                    price = float(maker.get("price", 0))
                    if order_id:
                        event_id = derive_fill_event_id(data, order_id, "maker")
                        token_id = maker.get("asset_id") or data.get("asset_id")
                        await self.handle_fill_if_local(
                            event_id=event_id,
                            order_id=order_id,
                            filled_size=matched_amount,
                            fill_price=price,
                            raw_event=data,
                            token_id=token_id,
                            liquidity_role="MAKER",
                            fee_rate_bps=maker.get("fee_rate_bps"),
                        )
                        
                # Check taker order (if we were the taker)
                if taker_order_id:
                    size = float(data.get("size", 0))
                    price = float(data.get("price", 0))
                    event_id = derive_fill_event_id(data, taker_order_id, "taker")
                    await self.handle_fill_if_local(
                        event_id=event_id,
                        order_id=taker_order_id,
                        filled_size=size,
                        fill_price=price,
                        raw_event=data,
                        token_id=data.get("asset_id"),
                        liquidity_role="TAKER",
                        fee_rate_bps=data.get("fee_rate_bps"),
                    )
                    
        elif event_type == "order":
            # For CANCELLATION or CLOSED events, we check if it was partially filled before
            status = normalize_exchange_status(data.get("status"), "ORDER_STATUS_")
            action = str(data.get("type") or "").strip().upper()
            if action == "CANCELLATION" or status in {
                "CANCELLATION",
                "CLOSED",
                "CANCELED",
                "CANCELLED",
            }:
                order_id = data.get("id") or data.get("order_id")
                
                if order_id:
                    await self.handle_cancellation(order_id)

    async def _publish_order_status_event(self, market_id: str, token_id: Optional[str], order_id: str, status: str):
        if not token_id:
            return
        await redis_client.publish(
            f"order_status:{market_id}:{token_id}",
            {
                "order_id": order_id,
                "status": status,
            },
        )

    async def handle_fill(
        self,
        *,
        event_id: str,
        order_id: str,
        filled_size: float,
        fill_price: float,
        raw_event: dict,
        token_id: Optional[str] = None,
        liquidity_role: Optional[str] = None,
        fee_rate_bps: Optional[object] = None,
    ):
        result = await fill_processor.record_and_process(
            event_id=event_id,
            exchange_order_id=order_id,
            filled_size=filled_size,
            fill_price=fill_price,
            raw_event=raw_event,
            token_id=token_id,
            liquidity_role=liquidity_role,
            fee_rate_bps=fee_rate_bps,
        )
        logger.info(
            "Fill event %s for order %s result=%s duplicate=%s",
            event_id[:12],
            order_id[:12],
            result.status,
            result.duplicate,
        )

    async def _is_local_order(self, order_id: str) -> bool:
        async with AsyncSessionLocal() as session:
            stmt = select(OrderJournal.order_id).filter(
                or_(
                    OrderJournal.order_id == order_id,
                    OrderJournal.exchange_order_id == order_id,
                )
            ).limit(1)
            return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def handle_fill_if_local(
        self,
        *,
        event_id: str,
        order_id: str,
        filled_size: float,
        fill_price: float,
        raw_event: dict,
        token_id: Optional[str] = None,
        liquidity_role: Optional[str] = None,
        fee_rate_bps: Optional[object] = None,
    ) -> None:
        """Reject counterparty maker/taker rows that are not in our order journal.

        The short retry window covers the valid race where User WS delivery beats
        persistence of the exchange order id from the submit response.
        """
        for delay in (0.0, 0.05, 0.2, 0.75, 2.0):
            if delay:
                await asyncio.sleep(delay)
            if await self._is_local_order(order_id):
                await self.handle_fill(
                    event_id=event_id,
                    order_id=order_id,
                    filled_size=filled_size,
                    fill_price=fill_price,
                    raw_event=raw_event,
                    token_id=token_id,
                    liquidity_role=liquidity_role,
                    fee_rate_bps=fee_rate_bps,
                )
                return
        logger.info(
            "Ignored trade candidate for non-local order %s after ownership retry",
            str(order_id)[:12],
        )

    async def handle_cancellation(self, order_id: str):
        """Handle order cancellation, including dust/partial fill cleanup."""
        market_id = None
        token_id = None
        local_order_id = None
        has_reservation = False
        async with AsyncSessionLocal() as session:
            stmt = (
                select(OrderJournal)
                .filter(
                    or_(
                        OrderJournal.order_id == order_id,
                        OrderJournal.exchange_order_id == order_id,
                    )
                )
                .with_for_update()
            )
            result = await session.execute(stmt)
            order = result.scalar_one_or_none()
            
            if order and order.status not in [OrderStatus.CANCELED, OrderStatus.FILLED]:
                local_order_id = order.order_id
                has_reservation = bool(order.reservation_id)
                payload = dict(order.payload) if order.payload else {}
                filled_size = float(payload.get("filled_size", 0.0))
                original_size = float(order.size)
                market_id = order.market_id
                token_id = payload.get("token_id")
                
                # Check for partial fill vs complete cancellation
                if filled_size > 0:
                    logger.info(f"Order {order_id} canceled after partial fill. (Filled: {filled_size}/{original_size})")
                    payload["status_detail"] = "PARTIALLY_FILLED_AND_CANCELED"
                else:
                    logger.info(f"Order {order_id} fully canceled.")
                    
                order.payload = payload
                order.status = OrderStatus.CANCELED
                await session.commit()
        if local_order_id and has_reservation:
            # The cancel event can overtake an earlier trade event. Retain remaining
            # capital/shares until authoritative reconciliation proves the final fill.
            await risk_reservations.mark_cancel_pending_for_order(local_order_id)
            trading_safety.set_readiness(
                "open_orders_reconciled",
                False,
                "canceled BUY reservations await authoritative reconciliation",
            )
        if market_id and token_id:
            await self._publish_order_status_event(market_id, token_id, order_id, "CANCELED")

user_stream = UserStreamGateway()
