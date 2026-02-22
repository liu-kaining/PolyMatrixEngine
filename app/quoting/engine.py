import asyncio
import json
import logging
from typing import Dict, List, Tuple
from app.core.redis import redis_client
from app.oms.core import oms
from app.models.db_models import OrderSide

logger = logging.getLogger(__name__)

class AlphaPricingModel:
    """Calculates baseline probability based on inputs"""
    def __init__(self):
        self.external_sources = []
        
    async def get_baseline_probability(self, market_id: str) -> float:
        # Default 50/50 for MVP. In reality, query AI or sportsbook data.
        return 0.50

class QuotingEngine:
    def __init__(self, condition_id: str, token_id: str):
        self.condition_id = condition_id
        self.token_id = token_id
        
        # Grid settings
        self.profit_margin = 0.02 # Fixed spread anchor target (Mid +/- 0.01)
        self.grid_levels = 2  # Bid1, Bid2 and Ask1, Ask2
        self.tick_size = 0.01 # $0.01 per share offset
        self.base_size = 10.0 # $10 per order
        
        # Debounce/Throttle Settings
        self.price_offset_threshold = 0.005 # Mid-Price deviation threshold
        self.last_anchor_mid_price = None   # Base anchor price
        
        self._trade_lock = asyncio.Lock()   # Lock for atomic order updates
        self.active_orders: Dict[str, str] = {}

    async def run(self):
        """Main loop for the quoting engine"""
        pubsub = redis_client.client.pubsub()
        await pubsub.subscribe(f"tick:{self.token_id}")
        logger.info(f"QuotingEngine started for Condition {self.condition_id[:6]} | Token {self.token_id[:6]}. Listening to tick:{self.token_id}")
        
        try:
            async for message in pubsub.listen():
                if message['type'] == 'message':
                    tick_data = json.loads(message['data'])
                    await self.on_tick(tick_data)
        except asyncio.CancelledError:
            logger.info(f"QuotingEngine shutting down for Token {self.token_id}")
        finally:
            # Ensure Redis resources are released
            await pubsub.unsubscribe(f"tick:{self.token_id}")
            await pubsub.close()
            logger.info(f"Redis PubSub closed for Token {self.token_id}")
                
    async def on_tick(self, tick_data: dict):
        """Evaluate orderbook, calculate Mid-Price, and print Mock Grid Payload"""
        bids = tick_data.get("bids", [])
        asks = tick_data.get("asks", [])
        
        if not bids or not asks:
            logger.debug(f"[{self.token_id[:6]}] Orderbook missing bids or asks. Skipping calculation.")
            return
            
        async with self._trade_lock:
            # 1. Calculate Mid-Price and Spread based on Top 1
            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            
            mid_price = (best_bid + best_ask) / 2.0
            spread = best_ask - best_bid
            
            # 2. Debounce / Throttle Mechanism Check
            if self.last_anchor_mid_price is not None:
                price_diff = abs(mid_price - self.last_anchor_mid_price)
                if price_diff <= self.price_offset_threshold:
                    logger.debug(
                        f"[{self.token_id[:6]}] Tick ignored: Mid-Price diff ({price_diff:.4f}) "
                        f"<= threshold ({self.price_offset_threshold}). Skip Grid Reset."
                    )
                    return
                    
            # Update the baseline anchor mid-price for future comparisons
            self.last_anchor_mid_price = mid_price
            
            # 3. Calculate optimal grid bounds based on Mid-Price
            # Margin is 0.02, meaning distance from Mid is 0.01
            anchor_distance = self.profit_margin / 2.0
            
            bid_1 = round(mid_price - anchor_distance, 2)
            ask_1 = round(mid_price + anchor_distance, 2)
            
            # Construct grid orders JSON
            orders_payload = []
            
            for i in range(self.grid_levels):
                # Bid tier: Mid - 0.01, Mid - 0.02...
                bid_price = round(bid_1 - (i * self.tick_size), 2)
                # Ask tier: Mid + 0.01, Mid + 0.02...
                ask_price = round(ask_1 + (i * self.tick_size), 2)
                
                # Polymarket boundaries bounds check (0.01 to 0.99)
                if 0.01 <= bid_price <= 0.99:
                    orders_payload.append({
                        "condition_id": self.condition_id,
                        "token_id": self.token_id,
                        "side": OrderSide.BUY,
                        "price": bid_price,
                        "size": self.base_size
                    })
                    
                if 0.01 <= ask_price <= 0.99:
                    orders_payload.append({
                        "condition_id": self.condition_id,
                        "token_id": self.token_id,
                        "side": OrderSide.SELL,
                        "price": ask_price,
                        "size": self.base_size
                    })
                    
            # 3. Log Mock output instead of firing to actual OMS right now
            logger.info(f"==== [GRID MOCK] Condition: {self.condition_id[:6]}... | Token: {self.token_id[:6]}... ====")
            logger.info(f"Top Book -> Bid: {best_bid:.3f} | Ask: {best_ask:.3f}")
            logger.info(f"Calculated -> Mid-Price: {mid_price:.3f} | Spread: {spread:.3f}")
            logger.info("Order Instructions Payload:")
            # Enum serialization requires .value if doing standard json.dumps, but we'll format it simply:
            log_payload = [
                {
                    "condition_id": o["condition_id"],
                    "token_id": o["token_id"],
                    "side": o["side"].value,
                    "price": o["price"],
                    "size": o["size"]
                }
                for o in orders_payload
            ]
            logger.info(json.dumps(log_payload, indent=2))
            logger.info("=========================================================================")
            
            # Actual OMS execution code
            await self.cancel_all_orders()
            await self.place_orders(orders_payload)

    async def place_orders(self, orders_payload: List[dict]):
        """Executes the placement of multiple orders concurrently through OMS"""
        tasks = []
        for o in orders_payload:
            tasks.append(oms.create_order(
                condition_id=o["condition_id"],
                token_id=o["token_id"],
                side=o["side"],
                price=o["price"],
                size=o["size"]
            ))
            
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Store active order IDs
        for i, res in enumerate(results):
            if isinstance(res, str):
                self.active_orders[f"order_{i}_{self.token_id}"] = res

    async def cancel_all_orders(self):
        """Cancel current active grid"""
        if not self.active_orders:
            return
            
        logger.info(f"Canceling {len(self.active_orders)} active orders for Token {self.token_id[:6]}...")
        tasks = [oms.cancel_order(order_id) for order_id in self.active_orders.values()]
        await asyncio.gather(*tasks, return_exceptions=True)
        self.active_orders.clear()

async def start_quoting_engine(condition_id: str, token_id: str):
    engine = QuotingEngine(condition_id, token_id)
    await engine.run()
