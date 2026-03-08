import asyncio
import json
import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple
from app.core.config import settings
from app.core.redis import redis_client
from app.core.inventory_state import inventory_state
from app.oms.core import oms
from app.models.db_models import OrderSide, OrderStatus, OrderJournal

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
        self.active_orders: Dict[str, Dict[str, Any]] = {}
        self.local_yes_exposure: float = 0.0
        self.local_no_exposure: float = 0.0
        
        self.suspended = False # Internal flag for Kill Switch

        # Rewards Farming: loaded once from Redis on first tick
        self._rewards_loaded = False
        self.rewards_min_size: float = 0.0
        self.rewards_max_spread: float = 0.0
        self.reward_rate_per_day: float = 0.0

    async def run(self):
        """Main loop for the quoting engine"""
        if not await self._bootstrap_context_and_inventory():
            logger.error(
                f"[{self.token_id[:6]}] Failed to bootstrap market context/inventory; engine exiting."
            )
            return

        pubsub = redis_client.client.pubsub()
        order_status_channel = f"order_status:{self.condition_id}:{self.token_id}"
        await pubsub.subscribe(
            f"tick:{self.token_id}",
            f"control:{self.condition_id}",
            order_status_channel,
        )
        logger.info(
            f"QuotingEngine started for Condition {self.condition_id[:6]} | Token {self.token_id[:6]}. "
            "Listening to tick/control/order status."
        )
        
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    channel = message.get("channel", "")
                    raw_data = message.get("data")
                    if raw_data is None:
                        continue
                    data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
                except (TypeError, ValueError, KeyError) as e:
                    logger.warning(f"[{self.token_id[:6]}] PubSub message parse error: {e}. Skip.")
                    continue
                try:
                    if channel == f"control:{self.condition_id}":
                        await self.on_control_message(data)
                    elif channel == f"tick:{self.token_id}":
                        if not self.suspended:
                            await self.on_tick(data)
                    elif channel == order_status_channel:
                        await self.on_order_status_message(data)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.exception(
                        f"[{self.token_id[:6]}] Error processing channel {channel}: {e}. "
                        "Engine continues to avoid permanent exit."
                    )
        except asyncio.CancelledError:
            logger.info(f"QuotingEngine shutting down for Token {self.token_id}")
        finally:
            # Ensure Redis resources are released
            await pubsub.unsubscribe(
                f"tick:{self.token_id}",
                f"control:{self.condition_id}",
                order_status_channel,
            )
            await pubsub.close()
            logger.info(f"Redis PubSub closed for Token {self.token_id}")

    async def _bootstrap_context_and_inventory(self) -> bool:
        from app.db.session import AsyncSessionLocal
        from sqlalchemy.future import select

        async with AsyncSessionLocal() as session:
            ok = await self._resolve_market_context(session)
            if ok:
                # Rehydrate active order cache from local journal (helps diff quoting
                # keep/replace decisions after process restarts).
                res = await session.execute(
                    select(OrderJournal).filter(
                        OrderJournal.market_id == self.condition_id,
                        OrderJournal.status.in_([OrderStatus.OPEN, OrderStatus.PENDING]),
                    )
                )
                rows = res.scalars().all()
                for o in rows:
                    payload = o.payload or {}
                    if payload.get("token_id") != self.token_id:
                        continue
                    self.active_orders[o.order_id] = {
                        "side": o.side.value,
                        "price": float(o.price),
                        "size": float(o.size),
                    }
        if not ok:
            return False

        snap = await inventory_state.ensure_loaded(self.condition_id)
        self.local_yes_exposure = float(snap.get("yes_exposure", 0.0))
        self.local_no_exposure = float(snap.get("no_exposure", 0.0))
        logger.info(
            f"[{self.token_id[:6]}] Bootstrap complete: inventory YES={self.local_yes_exposure:.4f}, "
            f"NO={self.local_no_exposure:.4f}, active_orders={len(self.active_orders)}"
        )
        return True

    async def on_order_status_message(self, data: dict):
        order_id = data.get("order_id")
        status = str(data.get("status", "")).upper()
        if not order_id:
            return
        if status in {"FILLED", "CANCELED", "CLOSED", "FAILED"} and order_id in self.active_orders:
            del self.active_orders[order_id]
            logger.info(
                f"[{self.token_id[:6]}] Active order removed by status event: {order_id[:10]}... ({status})"
            )

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
            try:
                self.reward_rate_per_day = float(rewards.get("reward_rate_per_day") or 0)
            except (ValueError, TypeError):
                self.reward_rate_per_day = 0.0
            if self.rewards_min_size > 0 or self.rewards_max_spread > 0:
                logger.info(
                    f"[{self.token_id[:6]}] Rewards config loaded: "
                    f"min_size={self.rewards_min_size}, max_spread={self.rewards_max_spread:.4f}, "
                    f"daily_rate={self.reward_rate_per_day}"
                )
        self._rewards_loaded = True

    def _compute_effective_size(self, price: float) -> float:
        """
        Grid-budget-aware size calculation.

        If AUTO_TUNE_FOR_REWARDS=True and rewards exist, auto-adjust size to rewards_min_size * 1.05.
        Fall back to BASE_ORDER_SIZE if it breaches risk limits.
        """
        auto_tune = getattr(settings, "AUTO_TUNE_FOR_REWARDS", False)
        max_exposure = float(getattr(settings, "MAX_EXPOSURE_PER_MARKET", 50.0))
        total_slots = max(1, self.grid_levels * 2)
        budget_per_order = max_exposure / total_slots

        # [AUTOTUNE] Logic
        if auto_tune and self.rewards_min_size > 0:
            target_size = max(5.0, round(self.rewards_min_size * 1.05, 1))
            exposure_cost = target_size * self.grid_levels
            
            # Risk check 1: Target * GRID_LEVELS > MAX_EXPOSURE_PER_MARKET
            if exposure_cost > max_exposure:
                logger.warning(
                    f"[{self.token_id[:6]}] [AUTOTUNE] Auto-size {target_size:.1f} rejected by MAX_EXPOSURE_PER_MARKET ({max_exposure:.1f}). Using Fallback Size: {self.base_size}."
                )
                return self.base_size
            
            # Risk check 2: Single order notional > budget_per_order
            # (In Auto-tune mode, if you pass exposure_cost check, you usually pass this, but we keep it safe)
            if (target_size * price) > budget_per_order:
                logger.warning(
                    f"[{self.token_id[:6]}] [AUTOTUNE] Auto-size {target_size:.1f} (notional {target_size*price:.2f}) rejected by per-order budget ({budget_per_order:.2f}). Using Fallback Size: {self.base_size}."
                )
                return self.base_size
            
            return target_size

        # Fallback to standard base logic
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

        # Legacy rewards logic (when auto_tune=False)
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
        bids = tick_data.get("bids", [])
        asks = tick_data.get("asks", [])
        
        if not bids or not asks:
            logger.debug(f"[{self.token_id[:6]}] Orderbook missing bids or asks. Skipping calculation.")
            return
            
        await self._load_rewards_config()

        async with self._trade_lock:
            # 1. Memory-only inventory read path (no DB I/O in on_tick).
            if self.is_yes_token is None:
                logger.warning(f"[{self.token_id[:6]}] Market context unavailable; skip tick.")
                return

            snap = await inventory_state.get_snapshot(self.condition_id)
            yes_exposure = float(snap.get("yes_exposure", 0.0))
            no_exposure = float(snap.get("no_exposure", 0.0))
            self.local_yes_exposure = yes_exposure
            self.local_no_exposure = no_exposure

            current_exposure = 0.0
            opposite_exposure = 0.0
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
            
            # [AUTOTUNE] Auto-Spread 动态点差决策
            auto_tune = getattr(settings, "AUTO_TUNE_FOR_REWARDS", False)
            if auto_tune and self.rewards_min_size > 0 and self.rewards_max_spread > 0:
                logger.info(
                    f"[{self.token_id[:6]}] [AUTOTUNE] Market has rewards! "
                    f"MinSize: {self.rewards_min_size}, MaxSpread: {self.rewards_max_spread:.4f}."
                )
                target_spread = self.rewards_max_spread * 0.90
                base_spread = float(getattr(settings, "QUOTE_BASE_SPREAD", 0.02))
                
                # If target spread is wider than base, take target_spread (safe fishing)
                applied_spread = dynamic_spread
                if target_spread > base_spread:
                    applied_spread = max(dynamic_spread, target_spread)
                    dynamic_spread = applied_spread
                
                target_size_log = max(5.0, round(self.rewards_min_size * 1.05, 1))
                logger.info(
                    f"[{self.token_id[:6]}] [AUTOTUNE] Auto-adjusting -> "
                    f"Size: {target_size_log:.1f} | Spread: {applied_spread:.4f}."
                )

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

            best_bid_price = float(bids[0]["price"])
            best_ask_price = float(asks[0]["price"])

            if is_long:
                # State B: Long inventory → aggressive sell to unwind
                logger.warning(
                    f"[{self.token_id[:6]}] INVENTORY HIGH ({current_exposure:.2f} >= {self.liquidate_threshold:.2f}). "
                    "Entering AGGRESSIVE SELL MODE."
                )

                aggressive_ask = min(fair_value + 0.01, best_ask_price - 0.01)
                ask_price = max(0.01, min(0.99, round(aggressive_ask, 2)))

                # Crosses-the-book guard: SELL price must be >= best_bid + tick,
                # and never exceed the protocol's 0.99 ceiling.
                min_sell = min(0.99, round(best_bid_price + self.tick_size, 2))
                if ask_price < min_sell:
                    logger.warning(
                        f"[{self.token_id[:6]}] 触发价格极值保护: SELL {ask_price} < best_bid+tick {min_sell}, "
                        f"已强制修正价格以避免 crosses the book"
                    )
                    ask_price = min_sell

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
                    one_tick_below = getattr(settings, "QUOTE_BID_ONE_TICK_BELOW_TOUCH", True)
                    seen_bid_prices: set = set()
                    for i in range(self.grid_levels):
                        raw = round(bid_1 - (i * self.tick_size), 2)
                        bid_price = max(0.01, min(0.99, raw))
                        if one_tick_below and i == 0 and bid_price < best_bid_price - 0.01:
                            bid_price = round(max(bid_price, best_bid_price - 0.01), 2)
                            bid_price = max(0.01, min(0.99, bid_price))

                        # Crosses-the-book guard: BUY price must be <= best_ask - tick,
                        # and never go below the protocol's 0.01 floor.
                        max_buy = max(0.01, round(best_ask_price - self.tick_size, 2))
                        if bid_price > max_buy:
                            logger.warning(
                                f"[{self.token_id[:6]}] 触发价格极值保护: BUY {bid_price} > best_ask-tick {max_buy}, "
                                f"已强制修正价格以避免 crosses the book"
                            )
                            bid_price = max_buy

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
            # 5b. Local balance pre-check: trim orders if total notional exceeds budget
            orders_payload = self._apply_balance_precheck(
                orders_payload,
                current_exposure=current_exposure,
                opposite_exposure=opposite_exposure,
            )

            logger.info("Order Instructions Payload:")
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
            
            # 6. Diff Quoting: keep unchanged orders, cancel stale, create missing
            await self.sync_orders_diff(orders_payload)

    def _apply_balance_precheck(
        self,
        orders_payload: List[dict],
        current_exposure: float,
        opposite_exposure: float,
    ) -> List[dict]:
        """
        Estimate total USDC commitment for this batch and trim if it would exceed
        available budget.  Budget = MAX_EXPOSURE_PER_MARKET minus notional already
        locked by existing positions on BOTH sides of this market.

        Also strictly capped by GLOBAL_MAX_BUDGET if specified in .env.

        BUY orders lock price*size USDC.  SELL orders only need shares already held
        (no incremental USDC).
        """
        if not orders_payload:
            return orders_payload

        max_exposure = float(getattr(settings, "MAX_EXPOSURE_PER_MARKET", 50.0))
        global_max_budget = float(getattr(settings, "GLOBAL_MAX_BUDGET", max_exposure))
        
        # We take the stricter of MAX_EXPOSURE_PER_MARKET and GLOBAL_MAX_BUDGET
        budget_limit = min(max_exposure, global_max_budget)
        
        # Cautious: existing exposure on both sides counts against the budget.
        used_notional = current_exposure + opposite_exposure
        available = max(0.0, budget_limit - used_notional)

        buy_orders = [o for o in orders_payload if o["side"] == OrderSide.BUY]
        sell_orders = [o for o in orders_payload if o["side"] == OrderSide.SELL]

        total_buy_notional = sum(o["price"] * o["size"] for o in buy_orders)

        if total_buy_notional <= available:
            return orders_payload

        logger.warning(
            f"[{self.token_id[:6]}] 本地资金预检: BUY 总名义=${total_buy_notional:.2f} > "
            f"可用预算=${available:.2f} (limit={budget_limit}, used={used_notional:.2f}). "
            f"正在自动缩减挂单."
        )

        if available <= 0:
            logger.warning(
                f"[{self.token_id[:6]}] 可用预算已耗尽, 跳过全部 BUY 挂单."
            )
            return sell_orders

        # Strategy: keep orders from most aggressive (highest price) first,
        # shrink size or drop tail levels to stay within budget.
        buy_orders.sort(key=lambda o: o["price"], reverse=True)
        remaining = available
        kept: List[dict] = []
        for o in buy_orders:
            notional = o["price"] * o["size"]
            if notional <= remaining:
                kept.append(o)
                remaining -= notional
            else:
                # Try to shrink size to fit remaining budget
                if o["price"] > 0:
                    max_size = remaining / o["price"]
                    # Polymarket min order size is 5
                    if max_size >= 5.0:
                        shrunk = dict(o)
                        shrunk["size"] = round(max_size, 1)
                        kept.append(shrunk)
                        logger.warning(
                            f"[{self.token_id[:6]}] 缩减 BUY@{o['price']} size: "
                            f"{o['size']:.1f} -> {shrunk['size']:.1f}"
                        )
                    else:
                        logger.warning(
                            f"[{self.token_id[:6]}] 跳过 BUY@{o['price']}: "
                            f"余额不足最小单量 5"
                        )
                break  # no budget left for further levels

        return sell_orders + kept

    @staticmethod
    def _order_signature(side: str, price: float, size: float) -> Tuple[str, float, float]:
        return (
            side,
            round(float(price), 4),
            round(float(size), 4),
        )

    async def sync_orders_diff(self, desired_orders: List[dict]):
        desired_by_sig: Dict[Tuple[str, float, float], List[dict]] = defaultdict(list)
        for o in desired_orders:
            sig = self._order_signature(o["side"].value, o["price"], o["size"])
            desired_by_sig[sig].append(o)

        # 1) Keep exact matches to preserve time-priority.
        to_cancel: List[str] = []
        for order_id, meta in list(self.active_orders.items()):
            sig = self._order_signature(
                str(meta.get("side", "")),
                float(meta.get("price", 0.0)),
                float(meta.get("size", 0.0)),
            )
            bucket = desired_by_sig.get(sig)
            if bucket:
                bucket.pop()
                if not bucket:
                    desired_by_sig.pop(sig, None)
            else:
                to_cancel.append(order_id)

        # 2) Cancel only stale orders.
        if to_cancel:
            logger.info(f"[{self.token_id[:6]}] Diff quoting: cancel stale={len(to_cancel)}")
            tasks = [oms.cancel_order(oid) for oid in to_cancel]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for order_id, success in zip(to_cancel, results):
                if success is True:
                    self.active_orders.pop(order_id, None)
                else:
                    logger.warning(
                        f"[{self.token_id[:6]}] Diff cancel failed for {order_id}: {success}"
                    )

        # 3) Create only missing desired orders.
        to_create = [o for bucket in desired_by_sig.values() for o in bucket]
        if to_create:
            logger.info(f"[{self.token_id[:6]}] Diff quoting: create missing={len(to_create)}")
            await self.place_orders(to_create)

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
        
        for order_req, res in zip(orders_payload, results):
            if isinstance(res, str):
                self.active_orders[res] = {
                    "side": order_req["side"].value,
                    "price": float(order_req["price"]),
                    "size": float(order_req["size"]),
                }

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
                # Downgraded from CRITICAL: OMS already handles matched-order scenarios
                # at INFO level. Remaining failures are transient network / circuit-breaker.
                logger.warning(f"[{self.token_id[:6]}] Cancel failed for order {order_id} (reason: {success}). Will retry next tick.")

async def start_quoting_engine(condition_id: str, token_id: str):
    engine = QuotingEngine(condition_id, token_id)
    await engine.run()
