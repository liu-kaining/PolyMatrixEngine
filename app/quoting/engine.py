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

class AlphaModel:
    """Calculates baseline probability and spread adjustments based on orderbook imbalance."""
    def __init__(self):
        self.base_spread = 0.02

    async def calculate_alpha(self, bids: list, asks: list) -> Tuple[float, float]:
        """
        Returns (fair_value, dynamic_spread_margin)
        Uses orderbook imbalance to skew the mid-price and widen/tighten the spread.
        """
        best_bid_price = float(bids[0]["price"])
        best_ask_price = float(asks[0]["price"])
        
        best_bid_size = float(bids[0]["size"])
        best_ask_size = float(asks[0]["size"])
        
        # 1. Base Mid-Price
        mid_price = (best_bid_price + best_ask_price) / 2.0
        
        # 2. Orderbook Imbalance (OBI)
        # Ranges from -1 (all asks) to +1 (all bids)
        total_size = best_bid_size + best_ask_size
        obi = (best_bid_size - best_ask_size) / total_size if total_size > 0 else 0.0
        
        # 3. Dynamic Skew
        # If OBI is highly positive (+0.8), buyers are aggressive, we skew fair value up.
        max_skew = 0.01
        skewed_fair_value = mid_price + (obi * max_skew)
        
        # 4. Dynamic Spread
        # Widen spread when highly imbalanced (volatile/directional flow)
        dynamic_spread = self.base_spread * (1.0 + abs(obi))
        
        return skewed_fair_value, dynamic_spread


class QuotingEngine:
    def __init__(self, condition_id: str, token_id: str):
        self.condition_id = condition_id
        self.token_id = token_id
        
        self.alpha_model = AlphaModel()
        
        # Grid settings
        self.grid_levels = 2  # Bid1, Bid2 and Ask1, Ask2
        self.tick_size = 0.01 # $0.01 per share offset
        self.base_size = 10.0 # $10 per order
        
        # Debounce/Throttle Settings
        self.price_offset_threshold = 0.005 # Mid-Price deviation threshold
        self.last_anchor_mid_price = None   # Base anchor price
        
        self._trade_lock = asyncio.Lock()   # Lock for atomic order updates
        self.active_orders: Dict[str, str] = {}
        
        self.suspended = False # Internal flag for Kill Switch

    async def run(self):
        """Main loop for the quoting engine"""
        pubsub = redis_client.client.pubsub()
        # Subscribe to market ticks and control signals for this specific market
        await pubsub.subscribe(f"tick:{self.token_id}", f"control:{self.condition_id}")
        logger.info(f"QuotingEngine started for Condition {self.condition_id[:6]} | Token {self.token_id[:6]}. Listening to tick & control.")
        
        try:
            async for message in pubsub.listen():
                if message['type'] == 'message':
                    channel = message['channel']
                    data = json.loads(message['data'])
                    
                    if channel == f"control:{self.condition_id}":
                        await self.on_control_message(data)
                    elif channel == f"tick:{self.token_id}":
                        if not self.suspended:
                            await self.on_tick(data)
        except asyncio.CancelledError:
            logger.info(f"QuotingEngine shutting down for Token {self.token_id}")
        finally:
            # Ensure Redis resources are released
            await pubsub.unsubscribe(f"tick:{self.token_id}", f"control:{self.condition_id}")
            await pubsub.close()
            logger.info(f"Redis PubSub closed for Token {self.token_id}")

    async def on_control_message(self, data: dict):
        """Handle incoming signals from the Watchdog or API"""
        action = data.get("action")
        if action == "suspend":
            async with self._trade_lock:
                self.suspended = True
                logger.critical(f"[{self.token_id[:6]}] QuotingEngine SUSPENDED by Control Signal. Halting all new orders.")
        elif action == "resume":
            async with self._trade_lock:
                self.suspended = False
                logger.info(f"[{self.token_id[:6]}] QuotingEngine RESUMED by Control Signal.")
                
    async def on_tick(self, tick_data: dict):
        """Evaluate orderbook, calculate Mid-Price, and print Mock Grid Payload"""
        bids = tick_data.get("bids", [])
        asks = tick_data.get("asks", [])
        
        if not bids or not asks:
            logger.debug(f"[{self.token_id[:6]}] Orderbook missing bids or asks. Skipping calculation.")
            return
            
        async with self._trade_lock:
            # 1. Calculate Alpha, Fair Value and Dynamic Spread
            fair_value, dynamic_spread = await self.alpha_model.calculate_alpha(bids, asks)
            
            # 2. Debounce / Throttle Mechanism Check
            if self.last_anchor_mid_price is not None:
                price_diff = abs(fair_value - self.last_anchor_mid_price)
                if price_diff <= self.price_offset_threshold:
                    logger.debug(
                        f"[{self.token_id[:6]}] Tick ignored: Fair Value diff ({price_diff:.4f}) "
                        f"<= threshold ({self.price_offset_threshold}). Skip Grid Reset."
                    )
                    return
                    
            # Update the baseline anchor mid-price for future comparisons
            self.last_anchor_mid_price = fair_value
            
            # 3. Calculate optimal grid bounds based on Fair Value and Dynamic Spread
            anchor_distance = dynamic_spread / 2.0
            
            bid_1 = round(fair_value - anchor_distance, 2)
            ask_1 = round(fair_value + anchor_distance, 2)
            
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
            logger.info(f"==== [GRID EXEC] Condition: {self.condition_id[:6]}... | Token: {self.token_id[:6]}... ====")
            logger.info(f"Top Book -> Bid: {bids[0]['price']} ({bids[0]['size']}) | Ask: {asks[0]['price']} ({asks[0]['size']})")
            logger.info(f"Alpha -> Fair Value: {fair_value:.3f} | Dynamic Spread: {dynamic_spread:.3f}")
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
