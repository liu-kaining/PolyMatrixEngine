import asyncio
import json
import logging
import time
from typing import Dict, List, Optional, Tuple
from app.core.config import settings
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
    """Unified pricing oracle anchored by YES orderbook."""
    def __init__(self):
        self.base_spread = float(getattr(settings, "QUOTE_BASE_SPREAD", 0.02))

    def calculate_yes_anchor(self, bids: list, asks: list) -> Tuple[float, float, float]:
        """
        Unified pricing anchor from YES orderbook.
        FV_yes = clamp(mid_yes + OBI_yes * 0.015, 0.01, 0.99)
        Returns (fv_yes, dynamic_spread, obi_yes)
        """
        best_bid_price = float(bids[0]["price"])
        best_ask_price = float(asks[0]["price"])
        best_bid_size = float(bids[0]["size"])
        best_ask_size = float(asks[0]["size"])

        mid_yes = (best_bid_price + best_ask_price) / 2.0
        total_size = best_bid_size + best_ask_size
        obi_yes = (best_bid_size - best_ask_size) / total_size if total_size > 0 else 0.0
        fv_yes = max(0.01, min(0.99, mid_yes + (obi_yes * 0.015)))
        dynamic_spread = self.base_spread * (1.0 + abs(obi_yes))
        return fv_yes, dynamic_spread, obi_yes


class QuotingEngine:
    def __init__(self, condition_id: str, token_id: str):
        self.condition_id = condition_id
        self.token_id = token_id
        
        self.alpha_model = AlphaModel()
        
        # Grid settings (number of price levels per side)
        # Configurable via .env → GRID_LEVELS
        self.grid_levels = int(getattr(settings, "GRID_LEVELS", 1))
        self.tick_size = 0.01  # $0.01 per share offset
        # Per-order notional size in USDC, configurable via .env (BASE_ORDER_SIZE)
        # Polymarket requires minimum order size 5, enforce that here.
        self.base_size = max(5.0, float(getattr(settings, "BASE_ORDER_SIZE", 10.0)))
        # Partial-fill safety: any fragment > 20% of base_size triggers de-inventory.
        # This prevents exposure stacking from partial fills (e.g. 4.7 out of 5.0).
        self.liquidate_threshold = self.base_size * 0.2
        
        # Debounce/Throttle Settings (smaller threshold = refresh grid more often, stay closer to touch)
        self.price_offset_threshold = float(getattr(settings, "QUOTE_PRICE_OFFSET_THRESHOLD", 0.005))
        self.last_anchor_mid_price = None   # Base anchor price
        
        self.is_yes_token = None # Resolved dynamically
        self.yes_token_id = None
        self.no_token_id = None
        
        self._trade_lock = asyncio.Lock()   # Lock for atomic order updates
        self.active_orders: Dict[str, str] = {}
        
        self.suspended = False # Internal flag for Kill Switch

        # Rewards Farming: loaded once from Redis on first tick
        self._rewards_loaded = False
        self.rewards_min_size: float = 0.0
        self.rewards_max_spread: float = 0.0

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
                logger.critical(f"[{self.token_id[:6]}] QuotingEngine SUSPENDED by Control Signal. Executing TRUE KILL SWITCH.")
                # True Kill Switch: Must synchronously wait for all orphans to cancel
                await self.cancel_all_orders()
                await self._publish_engine_mode("SUSPENDED")
        elif action == "resume":
            async with self._trade_lock:
                self.suspended = False
                logger.info(f"[{self.token_id[:6]}] QuotingEngine RESUMED by Control Signal.")

    async def _resolve_market_context(self, session) -> bool:
        """Resolve YES/NO token mapping once for unified pricing + cross-token lock."""
        from app.models.db_models import MarketMeta
        from sqlalchemy.future import select

        if self.is_yes_token is not None and self.yes_token_id and self.no_token_id:
            return True

        meta_res = await session.execute(select(MarketMeta).filter(MarketMeta.condition_id == self.condition_id))
        meta = meta_res.scalar_one_or_none()
        if not meta or not meta.yes_token_id or not meta.no_token_id:
            return False

        self.yes_token_id = meta.yes_token_id
        self.no_token_id = meta.no_token_id
        self.is_yes_token = (self.token_id == self.yes_token_id)
        return True

    async def _publish_engine_mode(
        self,
        mode: str,
        fair_value: Optional[float] = None,
        fv_yes: Optional[float] = None,
        current_exposure: Optional[float] = None,
        opposite_exposure: Optional[float] = None,
        rewards_eligible: Optional[bool] = None,
    ) -> None:
        """Publish runtime engine mode for Dashboard observability."""
        if self.is_yes_token is None:
            return
        side = "YES" if self.is_yes_token else "NO"
        payload = {
            "mode": mode,
            "side": side,
            "token_id": self.token_id,
            "updated_at": time.time(),
        }
        if fair_value is not None:
            payload["fair_value"] = float(fair_value)
        if fv_yes is not None:
            payload["fv_yes"] = float(fv_yes)
            payload["fv_no"] = float(max(0.01, min(0.99, 1.0 - fv_yes)))
        if current_exposure is not None:
            payload["own_exposure"] = float(current_exposure)
        if opposite_exposure is not None:
            payload["opposite_exposure"] = float(opposite_exposure)
        if rewards_eligible is not None:
            payload["rewards_eligible"] = rewards_eligible

        await redis_client.set_state(f"engine_state:{self.condition_id}:{side}", payload, ex=30)

    async def _load_rewards_config(self) -> None:
        """Load rewards params from Redis once. Safe for markets with no rewards (defaults to 0)."""
        if self._rewards_loaded:
            return
        rewards = await redis_client.get_state(f"rewards:{self.condition_id}")
        if rewards:
            try:
                self.rewards_min_size = float(rewards.get("rewards_min_size") or 0)
            except (ValueError, TypeError):
                self.rewards_min_size = 0.0
            try:
                self.rewards_max_spread = float(rewards.get("rewards_max_spread") or 0)
            except (ValueError, TypeError):
                self.rewards_max_spread = 0.0
            if self.rewards_min_size > 0 or self.rewards_max_spread > 0:
                logger.info(
                    f"[{self.token_id[:6]}] Rewards config loaded: "
                    f"min_size={self.rewards_min_size}, max_spread={self.rewards_max_spread:.4f}"
                )
        self._rewards_loaded = True

    def _compute_effective_size(self, price: float) -> float:
        """
        Grid-budget-aware size calculation.

        total_slots = grid_levels * 2  (YES engine + NO engine each post grid_levels orders)
        budget_per_order = MAX_EXPOSURE_PER_MARKET / total_slots

        If the rewards-target notional exceeds budget_per_order, we fall back to
        base_size to avoid blowing through the wallet balance across all grid slots.
        """
        max_exposure = float(getattr(settings, "MAX_EXPOSURE_PER_MARKET", 50.0))
        total_slots = max(1, self.grid_levels * 2)
        budget_per_order = max_exposure / total_slots

        if self.rewards_min_size <= 0:
            fallback_notional = self.base_size * price
            if fallback_notional > budget_per_order:
                safe_size = max(5.0, budget_per_order / price if price > 0 else self.base_size)
                logger.warning(
                    f"[{self.token_id[:6]}] 基础单量超预算 "
                    f"(base={self.base_size}×{price:.2f}=${fallback_notional:.2f} > "
                    f"budget_per_order=${budget_per_order:.2f}), 缩小至 {safe_size:.1f}"
                )
                return round(safe_size, 1)
            return self.base_size

        target = max(self.base_size, self.rewards_min_size)
        notional = target * price

        if notional > budget_per_order:
            logger.warning(
                f"[{self.token_id[:6]}] 单笔预算不足 "
                f"(target={target}×{price:.2f}=${notional:.2f} > "
                f"budget_per_order=${budget_per_order:.2f}, "
                f"slots={total_slots}), "
                f"为保证多档网格安全，放弃追求官方奖励，回退至基础单量 ({self.base_size})"
            )
            return self.base_size

        return target

    async def _get_unified_fair_value(self, bids: list, asks: list) -> Optional[Tuple[float, float, float]]:
        """
        Unified Pricing Oracle:
        - YES engine computes anchor FV_yes and publishes it.
        - NO engine consumes anchor and derives FV_no = 1 - FV_yes.
        Returns: (fv_current_token, dynamic_spread, fv_yes)
        """
        if self.is_yes_token is None:
            return None

        anchor_key = f"fv_anchor:{self.condition_id}"

        if self.is_yes_token:
            fv_yes, dynamic_spread, obi_yes = self.alpha_model.calculate_yes_anchor(bids, asks)
            await redis_client.set_state(
                anchor_key,
                {
                    "fv_yes": fv_yes,
                    "dynamic_spread": dynamic_spread,
                    "obi_yes": obi_yes,
                    "updated_at": time.time(),
                },
                ex=30,
            )
        else:
            anchor = await redis_client.get_state(anchor_key)
            if anchor and "fv_yes" in anchor:
                fv_yes = max(0.01, min(0.99, float(anchor["fv_yes"])))
                dynamic_spread = float(anchor.get("dynamic_spread", self.alpha_model.base_spread))
            else:
                # Fallback: derive from latest YES orderbook snapshot if anchor not ready yet.
                if not self.yes_token_id:
                    return None
                yes_snap = await redis_client.get_state(f"ob:{self.yes_token_id}")
                if not yes_snap:
                    logger.debug(f"[{self.token_id[:6]}] Unified anchor missing; waiting for YES snapshot.")
                    return None
                yes_bids = yes_snap.get("bids", [])
                yes_asks = yes_snap.get("asks", [])
                if not yes_bids or not yes_asks:
                    return None
                fv_yes, dynamic_spread, obi_yes = self.alpha_model.calculate_yes_anchor(yes_bids, yes_asks)
                await redis_client.set_state(
                    anchor_key,
                    {
                        "fv_yes": fv_yes,
                        "dynamic_spread": dynamic_spread,
                        "obi_yes": obi_yes,
                        "updated_at": time.time(),
                    },
                    ex=30,
                )

        fv_current = fv_yes if self.is_yes_token else max(0.01, min(0.99, 1.0 - fv_yes))
        return fv_current, dynamic_spread, fv_yes
                
    async def on_tick(self, tick_data: dict):
        """Evaluate orderbook, apply unified FV + inventory state machine, execute dynamic spread."""
        from app.db.session import AsyncSessionLocal
        from app.models.db_models import InventoryLedger
        from sqlalchemy.future import select

        bids = tick_data.get("bids", [])
        asks = tick_data.get("asks", [])
        
        if not bids or not asks:
            logger.debug(f"[{self.token_id[:6]}] Orderbook missing bids or asks. Skipping calculation.")
            return
            
        await self._load_rewards_config()

        async with self._trade_lock:
            # 1. Resolve token context and fetch BOTH-side exposure for cross-token lock
            current_exposure = 0.0
            opposite_exposure = 0.0
            yes_exposure = 0.0
            no_exposure = 0.0
            async with AsyncSessionLocal() as session:
                if not await self._resolve_market_context(session):
                    logger.warning(f"[{self.token_id[:6]}] Market context unavailable; skip tick.")
                    return
                
                # Fetch live inventory
                inv = await session.execute(select(InventoryLedger).filter(InventoryLedger.market_id == self.condition_id))
                inv = inv.scalar_one_or_none()
                if inv:
                    yes_exposure = float(inv.yes_exposure or 0.0)
                    no_exposure = float(inv.no_exposure or 0.0)
                    current_exposure = yes_exposure if self.is_yes_token else no_exposure
                    opposite_exposure = no_exposure if self.is_yes_token else yes_exposure

            # 2. Unified pricing (YES anchor + NO derived from 1-FV_yes)
            unified = await self._get_unified_fair_value(bids, asks)
            if unified is None:
                return
            fair_value, dynamic_spread, fv_yes = unified
            
            # 3. Debounce / Throttle Mechanism Check
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
            
            # 4. Calculate optimal grid bounds based on Skewed Fair Value and Dynamic Spread
            anchor_distance = dynamic_spread / 2.0
            bid_1 = round(fair_value - anchor_distance, 2)
            ask_1 = round(fair_value + anchor_distance, 2)

            # Construct grid orders JSON
            orders_payload: List[dict] = []

            # V3 State Machine: partial-fill aware.
            # Any fragment > 20% of base_size triggers liquidation / cross-lock,
            # preventing exposure stacking from partial fills (e.g. 4.7 / 5.0).
            is_long = current_exposure >= self.liquidate_threshold
            cross_token_locked = opposite_exposure >= self.liquidate_threshold
            own_side = "YES" if self.is_yes_token else "NO"
            opposite_side = "NO" if self.is_yes_token else "YES"

            if is_long:
                # State B: Long inventory → aggressive sell to unwind
                logger.warning(
                    f"[{self.token_id[:6]}] INVENTORY HIGH ({current_exposure:.2f} >= {self.liquidate_threshold:.2f}). "
                    "Entering AGGRESSIVE SELL MODE."
                )

                # No new BUYs in this mode – protect cash.
                # Aggressive ask: try to be at or inside best ask while keeping at least +0.01 edge over fair value.
                best_ask = float(asks[0]["price"])
                aggressive_ask = min(fair_value + 0.01, best_ask - 0.01)

                # Pricing Formula: Ask_Price = min(Fair Value + 0.01, Best_Ask - 0.01), clamped to [0.01, 0.99].
                ask_price = max(0.01, min(0.99, round(aggressive_ask, 2)))

                # Respect Polymarket minimum size 5 and never oversell inventory.
                target_size = max(self.base_size, 5.0)
                sell_size = min(current_exposure, target_size)

                orders_payload.append(
                    {
                        "condition_id": self.condition_id,
                        "token_id": self.token_id,
                        "side": OrderSide.SELL,
                        "price": ask_price,
                        "size": sell_size,
                    }
                )

                logger.info(
                    f"[{self.token_id[:6]}] AGGRESSIVE SELL: Ask {ask_price} | Size {sell_size:.2f} | "
                    f"Exposure {current_exposure:.2f}"
                )

            else:
                if cross_token_locked:
                    logger.warning(
                        f"[{self.token_id[:6]}] CROSS-TOKEN LOCK: opposite {opposite_side} exposure "
                        f"{opposite_exposure:.2f} >= BASE_ORDER_SIZE({self.base_size:.2f}). "
                        f"Suspend BUY on {own_side}, keep cash for {opposite_side} liquidation."
                    )
                else:
                    # State A: neutral / light exposure — 少而精，高概率赚钱。
                    # We only place BUY at fair_value - spread/2 (and below). No joining best_bid: we get filled only when
                    # someone sells to us at our price (positive edge per fill). 不轻易出手，一出手就要能高概率赚钱。
                    # Optional: first level at most 1 tick below best_bid so we get hit first when someone sells (still ~1¢ edge).
                    best_bid = float(bids[0]["price"])
                    one_tick_below = getattr(settings, "QUOTE_BID_ONE_TICK_BELOW_TOUCH", True)
                    # Clamp to Polymarket bounds so we still place at 0.01 (or 0.99) when fair value is at the floor/ceiling.
                    seen_bid_prices: set = set()
                    for i in range(self.grid_levels):
                        raw = round(bid_1 - (i * self.tick_size), 2)
                        bid_price = max(0.01, min(0.99, raw))
                        if one_tick_below and i == 0 and bid_price < best_bid - 0.01:
                            bid_price = round(max(bid_price, best_bid - 0.01), 2)
                            bid_price = max(0.01, min(0.99, bid_price))

                        if bid_price in seen_bid_prices:
                            continue
                        seen_bid_prices.add(bid_price)

                        effective_size = self._compute_effective_size(bid_price)
                        orders_payload.append(
                            {
                                "condition_id": self.condition_id,
                                "token_id": self.token_id,
                                "side": OrderSide.BUY,
                                "price": bid_price,
                                "size": effective_size,
                            }
                        )

                        # SELL side is intentionally skipped in neutral state to avoid inefficient capital lockup
                        # when we have very limited test capital and no shorting/minting capacity.
                        # Once inventory >= LIQUIDATE_THRESHOLD, the engine automatically switches
                        # to aggressive SELL mode above.

            mode = "LIQUIDATING" if is_long else ("LOCKED_BY_OPPOSITE" if cross_token_locked else "QUOTING")

            # Rewards eligibility: check size and spread vs official requirements
            rewards_size_ok = True
            rewards_spread_ok = True
            if self.rewards_min_size > 0:
                actual_sizes = [o["size"] for o in orders_payload] if orders_payload else [self.base_size]
                rewards_size_ok = all(s >= self.rewards_min_size for s in actual_sizes)
            if self.rewards_max_spread > 0 and dynamic_spread > self.rewards_max_spread:
                rewards_spread_ok = False
                logger.info(
                    f"[{self.token_id[:6]}] Spread too wide for rewards: "
                    f"dynamic_spread={dynamic_spread:.4f} > max_spread={self.rewards_max_spread:.4f}. "
                    f"Current orders will NOT earn liquidity rewards."
                )

            await self._publish_engine_mode(
                mode=mode,
                fair_value=fair_value,
                fv_yes=fv_yes,
                current_exposure=current_exposure,
                opposite_exposure=opposite_exposure,
                rewards_eligible=rewards_size_ok and rewards_spread_ok,
            )

            # 5. Log Execution output
            logger.info(
                f"==== [GRID EXEC] Condition: {self.condition_id[:6]}... | Token: {self.token_id[:6]}... ===="
            )
            logger.info(
                f"Top Book -> Bid: {bids[0]['price']} ({bids[0]['size']}) | "
                f"Ask: {asks[0]['price']} ({asks[0]['size']})"
            )
            logger.info(
                "Unified Pricing -> "
                f"FV_yes: {fv_yes:.4f} | FV_{own_side}: {fair_value:.4f} | "
                f"Dynamic Spread: {dynamic_spread:.4f} | "
                f"Own Exp: {current_exposure:.2f} | Opp Exp: {opposite_exposure:.2f} | "
                f"Mode: "
                f"{mode}"
            )
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
            
            # 6. Strict HFT Update Loop
            # A true kill switch or strict grid update requires synchronous cancellation of orphans
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
        
        for res in results:
            if isinstance(res, str):
                self.active_orders[res] = res

    async def cancel_all_orders(self):
        """Cancel current active grid and ensure no orphan orders remain."""
        if not self.active_orders:
            return
            
        order_ids = list(self.active_orders.keys())
        logger.info(f"[{self.token_id[:6]}] Canceling {len(order_ids)} active orders...")
        
        tasks = [oms.cancel_order(oid) for oid in order_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for order_id, success in zip(order_ids, results):
            if success is True:
                del self.active_orders[order_id]
            else:
                logger.error(f"[{self.token_id[:6]}] CRITICAL: Failed to cancel order {order_id}. Reason: {success}")

async def start_quoting_engine(condition_id: str, token_id: str):
    engine = QuotingEngine(condition_id, token_id)
    await engine.run()
