import asyncio
import json
import logging
import time
import websockets
from typing import Dict, Optional, Set

from app.core.redis import redis_client
from app.oms.core import oms
from app.oms.fill_processor import derive_fill_event_id, fill_processor
from app.risk.reservations import risk_reservations
from app.risk.watchdog import watchdog
from app.core.trading_safety import trading_safety
from app.db.session import AsyncSessionLocal
from app.models.db_models import OrderJournal, OrderStatus
from sqlalchemy import or_
from sqlalchemy.future import select

logger = logging.getLogger(__name__)


def _safe_create_task(coro):
    """Fire-and-forget with exception logging to prevent silent failures."""
    task = asyncio.create_task(coro)
    def _done(t):
        try:
            t.result()
        except Exception as e:
            logger.exception("User WS fire-and-forget task failed: %s", e)
    task.add_done_callback(_done)
    return task


class UserStreamGateway:
    def __init__(self):
        self.ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
        self.subscribed_markets: Set[str] = set() # Condition IDs
        self.market_tokens: Dict[str, Dict[str, str]] = {}
        self.ws = None
        self.reconnect_delay = 1.0
        self.max_reconnect_delay = 60.0
        self.ping_task = None

    async def connect(self):
        # We need the client credentials to connect
        while oms.client is None or not oms.client.creds:
            logger.debug("UserStreamGateway waiting for ClobClient initialization...")
            await asyncio.sleep(2.0)
            
        while True:
            connected_at = None
            try:
                logger.debug(f"Connecting to Polymarket User WS: {self.ws_url}")
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                ) as ws:
                    self.ws = ws
                    connected_at = time.monotonic()
                    logger.info("User WS connected.")

                    self.ping_task = asyncio.create_task(self._heartbeat())
                    await self._authenticate()
                    trading_safety.set_readiness(
                        "user_stream",
                        False,
                        "subscription sent but authentication acknowledgement contract is unverified",
                    )
                    # Compare positions after any WS gap, but retain the recent-fill
                    # delay guard because the Data API can lag a committed local fill.
                    _safe_create_task(watchdog.reconcile_positions())
                    await self._listen()
                    raise RuntimeError("User WS listen loop exited unexpectedly without exception.")
                    
            except websockets.exceptions.ConnectionClosed as e:
                logger.exception(
                    "User WS connection closed. code=%s reason=%s clean=%s",
                    getattr(e, "code", None),
                    getattr(e, "reason", ""),
                    isinstance(e, websockets.exceptions.ConnectionClosedOK),
                )
            except Exception as e:
                logger.exception(f"User WS connect loop crashed: {e}")
            finally:
                if self.ping_task:
                    self.ping_task.cancel()
                    self.ping_task = None
                self.ws = None
                trading_safety.set_readiness(
                    "user_stream", False, "user WebSocket disconnected"
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

    async def _heartbeat(self):
        """Send PING every 10 seconds"""
        try:
            while True:
                await asyncio.sleep(10)
                if self.ws is not None and not getattr(self.ws, "closed", False):
                    await self.ws.send("PING")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f"User WS heartbeat error: {e}")

    async def _authenticate(self):
        """
        Send subscription per Polymarket docs: type=user, auth={apiKey, secret, passphrase}.
        WSS is TLS-encrypted; omit markets to receive all user order/trade events.
        """
        try:
            creds = oms.client.creds
            sub_msg = {
                "type": "user",
                "auth": {
                    "apiKey": creds.api_key,
                    "secret": creds.api_secret,
                    "passphrase": creds.api_passphrase,
                },
            }
            await self.ws.send(json.dumps(sub_msg))
            logger.info("User WS authenticated (subscription sent).")
        except Exception as e:
            logger.exception("User WS auth failed: %s", e)
            raise

    async def subscribe(self, condition_id: str):
        """Track condition_id locally only. Do NOT send subscribe to User WS."""
        self.subscribed_markets.add(condition_id)
        logger.info("User WS: added condition_id to local tracking: %s", condition_id[:20] + "..." if len(condition_id) > 20 else condition_id)

    async def _listen(self):
        while True:
            try:
                # Add strict receive timeout. If no message (trade/order or PONG) arrives for 45s,
                # the connection is a zombie. Force an exception to trigger reconnection.
                # User stream is less chatty, so 45s is safer.
                message = await asyncio.wait_for(self.ws.recv(), timeout=45.0)
                if isinstance(message, bytes):
                    message = message.decode("utf-8", errors="replace")
                
                if message == "PONG":
                    continue
                if message == "PING":
                    await self.ws.send("PONG")
                    continue
                try:
                    data = json.loads(message)
                except json.JSONDecodeError as e:
                    logger.exception(
                        f"User WS JSON decode failed: {e}. Raw message (first 200 chars): {str(message)[:200]}"
                    )
                    continue
                await self.process_message(data)
            except asyncio.TimeoutError:
                logger.exception("User WS silent drop detected (45s without message). Forcing reconnect...")
                raise
            except websockets.exceptions.ConnectionClosed as e:
                logger.exception(
                    "User WS recv closed. code=%s reason=%s clean=%s",
                    getattr(e, "code", None),
                    getattr(e, "reason", ""),
                    isinstance(e, websockets.exceptions.ConnectionClosedOK),
                )
                raise
            except Exception as e:
                logger.exception(f"Error processing User WS message: {e}")
                raise

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
        event_type = data.get("event_type")
        
        if event_type == "trade":
            # Match status is usually "MATCHED" for a fill
            status = data.get("status")
            if status == "MATCHED":
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
                        _safe_create_task(
                            self.handle_fill_if_local(
                                event_id=event_id,
                                order_id=order_id,
                                filled_size=matched_amount,
                                fill_price=price,
                                raw_event=data,
                                token_id=token_id,
                            )
                        )
                        
                # Check taker order (if we were the taker)
                if taker_order_id:
                    size = float(data.get("size", 0))
                    price = float(data.get("price", 0))
                    event_id = derive_fill_event_id(data, taker_order_id, "taker")
                    _safe_create_task(
                        self.handle_fill_if_local(
                            event_id=event_id,
                            order_id=taker_order_id,
                            filled_size=size,
                            fill_price=price,
                            raw_event=data,
                            token_id=data.get("asset_id"),
                        )
                    )
                    
        elif event_type == "order":
            # For CANCELLATION or CLOSED events, we check if it was partially filled before
            status = data.get("status")
            if status in ["CANCELLATION", "CLOSED", "CANCELED"]:
                order_id = data.get("id") or data.get("order_id")
                
                if order_id:
                    _safe_create_task(self.handle_cancellation(order_id))

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
    ):
        result = await fill_processor.record_and_process(
            event_id=event_id,
            exchange_order_id=order_id,
            filled_size=filled_size,
            fill_price=fill_price,
            raw_event=raw_event,
            token_id=token_id,
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
