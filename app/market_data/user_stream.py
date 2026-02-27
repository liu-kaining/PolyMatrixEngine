import asyncio
import json
import logging
import websockets
from typing import Set

from app.core.config import settings
from app.oms.core import oms
from app.db.session import AsyncSessionLocal
from app.models.db_models import OrderJournal, OrderStatus, InventoryLedger
from sqlalchemy.future import select

logger = logging.getLogger(__name__)

class UserStreamGateway:
    def __init__(self):
        self.ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
        self.subscribed_markets: Set[str] = set() # Condition IDs
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
            try:
                logger.debug(f"Connecting to Polymarket User WS: {self.ws_url}")
                async with websockets.connect(self.ws_url, ping_interval=None) as ws:
                    self.ws = ws
                    self.reconnect_delay = 1.0
                    logger.info("User WS connected.")
                    
                    self.ping_task = asyncio.create_task(self._heartbeat())
                    
                    if self.subscribed_markets:
                        await self._resubscribe()
                    
                    await self._listen()
                    
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"User WS connection closed: {e}. Reconnecting...")
            except Exception as e:
                logger.error(f"User WS error: {e}. Reconnecting...")
            finally:
                if self.ping_task:
                    self.ping_task.cancel()
                    self.ping_task = None
                self.ws = None
                
                logger.debug(f"User WS reconnecting in {self.reconnect_delay} seconds...")
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
            await self.ws.send(json.dumps(sub_msg))
            logger.info(f"User WS Subscribed to markets: {self.subscribed_markets}")

    async def _listen(self):
        async for message in self.ws:
            try:
                if message == "PONG":
                    continue
                data = json.loads(message)
                await self.process_message(data)
            except json.JSONDecodeError:
                pass
            except Exception as e:
                logger.error(f"Error processing User WS message: {e}")

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

    async def handle_fill(self, order_id: str, filled_size: float, fill_price: float):
        """Process an order fill and update inventory atomically"""
        async with AsyncSessionLocal() as session:
            # 1. Get the order with FOR UPDATE lock to prevent race conditions on inventory
            # We must link back to inventory ledger to update it atomically
            stmt = select(OrderJournal).filter(OrderJournal.order_id == order_id).with_for_update()
            result = await session.execute(stmt)
            order = result.scalar_one_or_none()
            
            if not order:
                # Could be an order from another session or before tracking
                return
                
            # Track cumulative fills to support partial fills and dust handling
            # We initialize a "filled_size" counter in payload if not exists
            payload = dict(order.payload) if order.payload else {}
            current_filled = float(payload.get("filled_size", 0.0))
            new_total_filled = current_filled + filled_size
            
            payload["filled_size"] = new_total_filled
            order.payload = payload
            
            original_size = float(order.size)
            
            # Determine if fully filled or partially filled
            if new_total_filled >= original_size - 1e-6: # Dust tolerance
                order.status = OrderStatus.FILLED
            else:
                # Mark as partially filled (using a string or creating a new enum, 
                # but if enum is strictly FILLED, we might just leave it OPEN but with accumulated fill payload)
                # To stick to existing enum, we keep it OPEN, but the payload tracks filled size.
                order.status = OrderStatus.OPEN
            
            # 2. Update Inventory Ledger atomically
            market_id = order.market_id
            side = order.side.value # "BUY" or "SELL"
            
            # We need to know if this was a YES or NO token.
            # We stored the token_id in the payload during create_order
            payload = order.payload or {}
            token_id = payload.get("token_id")
            
            # Fetch MarketMeta to determine token orientation
            from app.models.db_models import MarketMeta
            meta_stmt = select(MarketMeta).filter(MarketMeta.condition_id == market_id)
            meta_res = await session.execute(meta_stmt)
            meta = meta_res.scalar_one_or_none()
            
            if meta:
                is_yes = (token_id == meta.yes_token_id)
                
                # Lock inventory ledger
                inv_stmt = select(InventoryLedger).filter(InventoryLedger.market_id == market_id).with_for_update()
                inv_res = await session.execute(inv_stmt)
                inv = inv_res.scalar_one_or_none()
                
                if inv:
                    # Update exposure
                    # Buying increases exposure, selling decreases it
                    if side == "BUY":
                        if is_yes:
                            inv.yes_exposure = float(inv.yes_exposure) + filled_size
                        else:
                            inv.no_exposure = float(inv.no_exposure) + filled_size
                    elif side == "SELL":
                        if is_yes:
                            inv.yes_exposure = float(inv.yes_exposure) - filled_size
                        else:
                            inv.no_exposure = float(inv.no_exposure) - filled_size
                            
                        # Basic PnL calculation (Mock logic: Sell Price * Size)
                        inv.realized_pnl = float(inv.realized_pnl) + (fill_price * filled_size)
                        
                    logger.info(f"Inventory Updated for {market_id}: YES={inv.yes_exposure}, NO={inv.no_exposure}")
            
            await session.commit()
            logger.info(f"Order {order_id} marked as FILLED. Size: {filled_size} @ {fill_price}")

    async def handle_cancellation(self, order_id: str):
        """Handle order cancellation, including dust/partial fill cleanup."""
        async with AsyncSessionLocal() as session:
            stmt = select(OrderJournal).filter(OrderJournal.order_id == order_id).with_for_update()
            result = await session.execute(stmt)
            order = result.scalar_one_or_none()
            
            if order and order.status not in [OrderStatus.CANCELED, OrderStatus.FILLED]:
                payload = dict(order.payload) if order.payload else {}
                filled_size = float(payload.get("filled_size", 0.0))
                original_size = float(order.size)
                
                # Check for partial fill vs complete cancellation
                if filled_size > 0:
                    logger.info(f"Order {order_id} canceled after partial fill. (Filled: {filled_size}/{original_size})")
                    payload["status_detail"] = "PARTIALLY_FILLED_AND_CANCELED"
                else:
                    logger.info(f"Order {order_id} fully canceled.")
                    
                order.payload = payload
                order.status = OrderStatus.CANCELED
                await session.commit()

user_stream = UserStreamGateway()
