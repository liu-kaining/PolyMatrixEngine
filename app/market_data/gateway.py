import asyncio
import json
import logging
import websockets
from typing import List, Set, Dict, Optional
from app.core.config import settings
from app.core.redis import redis_client

logger = logging.getLogger(__name__)

class OrderbookParser:
    def __init__(self):
        # Dictionary of asset_id -> {"bids": {price: size}, "asks": {price: size}}
        self.books: Dict[str, Dict[str, Dict[str, float]]] = {}
        
    def process_message(self, data: dict) -> List[dict]:
        """Parses WS message, updates local state, returns top 5 levels for updated assets"""
        event_type = data.get("event_type")
        
        if event_type == "book":
            asset_id = data.get("asset_id")
            if not asset_id:
                return []
                
            self.books[asset_id] = {"bids": {}, "asks": {}}
            for bid in data.get("bids", []):
                self.books[asset_id]["bids"][bid["price"]] = float(bid["size"])
            for ask in data.get("asks", []):
                self.books[asset_id]["asks"][ask["price"]] = float(ask["size"])
                
            top_5 = self.get_top_5(asset_id)
            return [top_5] if top_5 else []
            
        elif event_type == "price_change":
            changes = data.get("price_changes", [])
            asset_ids_updated = set()
            
            for change in changes:
                asset_id = change.get("asset_id")
                if not asset_id:
                    continue
                
                if asset_id not in self.books:
                    self.books[asset_id] = {"bids": {}, "asks": {}}
                    
                side = change.get("side", "").upper()
                price = change.get("price")
                size = float(change.get("size", "0"))
                
                if side == "BUY":
                    target_book = self.books[asset_id]["bids"]
                elif side == "SELL":
                    target_book = self.books[asset_id]["asks"]
                else:
                    continue
                
                if size == 0:
                    target_book.pop(price, None)
                else:
                    target_book[price] = size
                    
                asset_ids_updated.add(asset_id)
                
            updates = []
            for aid in asset_ids_updated:
                top_5 = self.get_top_5(aid)
                if top_5:
                    updates.append(top_5)
            return updates
            
        return []

    def get_top_5(self, asset_id: str) -> Optional[dict]:
        if asset_id not in self.books:
            return None
            
        bids = self.books[asset_id]["bids"]
        asks = self.books[asset_id]["asks"]
        
        # Bids sorted descending
        top_bids = sorted(bids.items(), key=lambda x: float(x[0]), reverse=True)[:5]
        # Asks sorted ascending
        top_asks = sorted(asks.items(), key=lambda x: float(x[0]))[:5]
        
        return {
            "asset_id": asset_id,
            "bids": [{"price": p, "size": s} for p, s in top_bids],
            "asks": [{"price": p, "size": s} for p, s in top_asks]
        }


class MarketDataGateway:
    def __init__(self):
        self.ws_url = settings.PM_WS_URL
        self.subscribed_markets: Set[str] = set()
        self.ws = None
        self.reconnect_delay = 1.0
        self.max_reconnect_delay = 60.0
        self.parser = OrderbookParser()
        self.ping_task = None

    async def connect(self):
        while True:
            try:
                logger.info(f"Connecting to Polymarket WS: {self.ws_url}")
                async with websockets.connect(self.ws_url, ping_interval=None) as ws:
                    self.ws = ws
                    self.reconnect_delay = 1.0 # Reset delay on success
                    logger.info("Connected successfully.")
                    
                    self.ping_task = asyncio.create_task(self._heartbeat())
                    
                    if self.subscribed_markets:
                        await self._resubscribe()
                    
                    await self._listen()
                    
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"Connection closed: {e}. Reconnecting...")
            except Exception as e:
                logger.error(f"WebSocket error: {e}. Reconnecting...")
            finally:
                if self.ping_task:
                    self.ping_task.cancel()
                    self.ping_task = None
                self.ws = None
                
                logger.info(f"Reconnecting in {self.reconnect_delay} seconds...")
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)

    async def _heartbeat(self):
        """Send PING every 10 seconds per Polymarket documentation"""
        try:
            while True:
                await asyncio.sleep(10)
                if self.ws and self.ws.open:
                    await self.ws.send("PING")
                    logger.debug("Sent PING")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")

    async def subscribe(self, asset_ids: List[str]):
        """Subscribe to a list of market asset/token IDs"""
        self.subscribed_markets.update(asset_ids)
        if self.ws and self.ws.open:
            sub_msg = {
                "assets_ids": list(self.subscribed_markets),
                "type": "market",
                "custom_feature_enabled": True
            }
            await self.ws.send(json.dumps(sub_msg))
            logger.info(f"Subscribed to assets: {asset_ids}")

    async def _resubscribe(self):
        """Internal resub on reconnect"""
        if self.subscribed_markets:
            sub_msg = {
                "assets_ids": list(self.subscribed_markets),
                "type": "market",
                "custom_feature_enabled": True
            }
            await self.ws.send(json.dumps(sub_msg))
            logger.info("Resubscribed to active markets.")

    async def _listen(self):
        """Process incoming WS messages"""
        async for message in self.ws:
            try:
                if message == "PONG":
                    logger.debug("Received PONG")
                    continue
                    
                data = json.loads(message)
                
                # Use JSON parser to get top-5 book updates
                updates = self.parser.process_message(data)
                
                for update in updates:
                    asset_id = update["asset_id"]
                    # 1. Update Snapshot in Redis Cache (for fast lookup)
                    await redis_client.set_state(f"ob:{asset_id}", update)
                    
                    # 2. Publish tick to Quoting Engine
                    await redis_client.publish(f"tick:{asset_id}", update)
                    
            except json.JSONDecodeError:
                logger.error(f"Failed to parse WS message: {message}")
            except Exception as e:
                logger.error(f"Error processing message: {e}")

# Global instance
md_gateway = MarketDataGateway()
