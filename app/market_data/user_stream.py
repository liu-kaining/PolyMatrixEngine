import asyncio
import json
import logging
import time
import websockets
from typing import Dict, Set

from app.core.redis import redis_client
from app.core.inventory_state import inventory_state
from app.oms.core import oms
from app.db.session import AsyncSessionLocal
from app.models.db_models import OrderJournal, OrderStatus
from sqlalchemy.future import select

logger = logging.getLogger(__name__)

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
                    
                    if self.subscribed_markets:
                        await self._resubscribe()
                    
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

    async def subscribe(self, condition_id: str):
        self.subscribed_markets.add(condition_id)
        await self._resubscribe()
        
    async def _resubscribe(self):
        if self.ws is not None and not getattr(self.ws, "closed", False) and self.subscribed_markets:
            creds = oms.client.creds
            sub_msg = {
                "auth": {
                    "apiKey": creds.api_key,
                    "secret": creds.api_secret,
                    "passphrase": creds.api_passphrase
                },
                "markets": list(self.subscribed_markets),
                "type": "user"
            }
            try:
                await self.ws.send(json.dumps(sub_msg))
                logger.debug(f"User WS Subscribed to markets: {self.subscribed_markets}")
            except Exception as e:
                logger.exception(f"User WS subscribe send failed: {e}")
                raise

    async def _listen(self):
        while True:
            if getattr(self.ws, "closed", True):
                raise RuntimeError("User WS socket marked closed before recv()")
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
                        asyncio.create_task(self.handle_fill(order_id, matched_amount, price))
                        
                # Check taker order (if we were the taker)
                if taker_order_id:
                    size = float(data.get("size", 0))
                    price = float(data.get("price", 0))
                    asyncio.create_task(self.handle_fill(taker_order_id, size, price))
                    
        elif event_type == "order":
            # For CANCELLATION or CLOSED events, we check if it was partially filled before
            status = data.get("status")
            if status in ["CANCELLATION", "CLOSED", "CANCELED"]:
                order_id = data.get("id") or data.get("order_id")
                
                if order_id:
                    asyncio.create_task(self.handle_cancellation(order_id))

    async def _resolve_market_tokens(self, session, market_id: str) -> Dict[str, str] | None:
        cached = self.market_tokens.get(market_id)
        if cached and cached.get("yes_token_id") and cached.get("no_token_id"):
            return cached

        from app.models.db_models import MarketMeta

        meta_stmt = select(MarketMeta).filter(MarketMeta.condition_id == market_id)
        meta_res = await session.execute(meta_stmt)
        meta = meta_res.scalar_one_or_none()
        if not meta or not meta.yes_token_id or not meta.no_token_id:
            return None
        tokens = {"yes_token_id": meta.yes_token_id, "no_token_id": meta.no_token_id}
        self.market_tokens[market_id] = tokens
        return tokens

    async def _publish_order_status_event(self, market_id: str, token_id: str | None, order_id: str, status: str):
        if not token_id:
            return
        await redis_client.publish(
            f"order_status:{market_id}:{token_id}",
            {
                "order_id": order_id,
                "status": status,
            },
        )

    async def handle_fill(self, order_id: str, filled_size: float, fill_price: float):
        """
        Process fill:
        1) update order journal row (locked)
        2) update in-memory inventory immediately
        3) persist inventory via async background queue (fire-and-forget)
        """
        market_id = None
        token_id = None
        side = None
        status_for_event = None

        async with AsyncSessionLocal() as session:
            stmt = select(OrderJournal).filter(OrderJournal.order_id == order_id).with_for_update()
            result = await session.execute(stmt)
            order = result.scalar_one_or_none()

            if not order:
                return

            payload = dict(order.payload) if order.payload else {}
            current_filled = float(payload.get("filled_size", 0.0))
            new_total_filled = current_filled + filled_size
            payload["filled_size"] = new_total_filled
            order.payload = payload

            original_size = float(order.size)
            if new_total_filled >= original_size - 1e-6:
                order.status = OrderStatus.FILLED
                status_for_event = "FILLED"
            else:
                order.status = OrderStatus.OPEN

            market_id = order.market_id
            side = order.side.value
            token_id = payload.get("token_id")

            await session.commit()

            # Update memory-first inventory state (DB persistence is queued inside manager).
            tokens = await self._resolve_market_tokens(session, market_id)
            if tokens and token_id:
                is_yes = token_id == tokens["yes_token_id"]
                updated = await inventory_state.apply_fill(
                    market_id=market_id,
                    is_yes=is_yes,
                    side=side,
                    filled_size=filled_size,
                    fill_price=fill_price,
                )
                logger.info(
                    f"Inventory Updated for {market_id}: "
                    f"YES={updated['yes_exposure']:.4f}, NO={updated['no_exposure']:.4f}"
                )

        logger.info(f"Order {order_id} fill processed. Size: {filled_size} @ {fill_price}")
        if status_for_event and market_id and token_id:
            await self._publish_order_status_event(market_id, token_id, order_id, status_for_event)

    async def handle_cancellation(self, order_id: str):
        """Handle order cancellation, including dust/partial fill cleanup."""
        market_id = None
        token_id = None
        async with AsyncSessionLocal() as session:
            stmt = select(OrderJournal).filter(OrderJournal.order_id == order_id).with_for_update()
            result = await session.execute(stmt)
            order = result.scalar_one_or_none()
            
            if order and order.status not in [OrderStatus.CANCELED, OrderStatus.FILLED]:
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
        if market_id and token_id:
            await self._publish_order_status_event(market_id, token_id, order_id, "CANCELED")

user_stream = UserStreamGateway()
