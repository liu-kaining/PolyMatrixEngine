import asyncio
import json
import logging
import time
from collections import defaultdict
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Dict, List, Optional, Tuple
from app.core.config import settings
from app.core.exposure_limits import resolve_outcome_count
from app.core.redis import redis_client
from app.core.inventory_state import inventory_state
from app.core.exit_policy import plan_bounded_sell
from app.core.pricing_model import (
    PricingModelError,
    calculate_binary_fair_value,
    pair_time_skew_seconds,
)
from app.core.quote_economics import evaluate_quote_economics
from app.core.trading_safety import TradingMode, trading_safety
from app.market_data.integrity import assess_snapshot
from app.oms.core import oms
from app.models.db_models import OrderSide, OrderStatus, OrderJournal

logger = logging.getLogger(__name__)

class AlphaModel:
    """Cross-book, depth-aware binary probability model."""
    def __init__(self):
        self.base_spread = float(getattr(settings, "QUOTE_BASE_SPREAD", 0.02))

    def calculate_yes_anchor(
        self,
        yes_snapshot: dict,
        no_snapshot: dict,
        *,
        yes_inventory_value: float,
        no_inventory_value: float,
        inventory_cap: float,
    ):
        return calculate_binary_fair_value(
            yes_bids=yes_snapshot.get("bids"),
            yes_asks=yes_snapshot.get("asks"),
            no_bids=no_snapshot.get("bids"),
            no_asks=no_snapshot.get("asks"),
            base_spread=self.base_spread,
            depth_levels=int(settings.ALPHA_BOOK_DEPTH_LEVELS),
            depth_decay=float(settings.ALPHA_BOOK_DEPTH_DECAY),
            max_parity_error=float(settings.ALPHA_MAX_BINARY_PARITY_ERROR),
            yes_inventory_value=yes_inventory_value,
            no_inventory_value=no_inventory_value,
            inventory_cap=inventory_cap,
            max_inventory_skew=float(settings.ALPHA_MAX_INVENTORY_SKEW),
        )


class QuotingEngine:
    def __init__(self, condition_id: str, token_id: str):
        self.condition_id = condition_id
        self.token_id = token_id
        
        self.alpha_model = AlphaModel()
        
        # Grid settings (number of price levels per side)
        # Configurable via .env → GRID_LEVELS
        self.grid_levels = int(getattr(settings, "GRID_LEVELS", 1))
        self.tick_size = 0.01
        self.min_order_size = 5.0
        # Per-order size in OUTCOME SHARES (CLOB `OrderArgs.size`), from .env BASE_ORDER_SIZE.
        # Not USDC: BUY notional ≈ price × size. Polymarket min order size is 5 shares.
        self.configured_base_size = float(getattr(settings, "BASE_ORDER_SIZE", 10.0))
        self.base_size = max(self.min_order_size, self.configured_base_size)
        # Panic threshold: exposure >= this triggers unwind. base_size * 2.0 = hold 2 orders before defense.
        self.liquidate_threshold = self.base_size * 2.0
        
        # Debounce/Throttle Settings (smaller threshold = refresh grid more often, stay closer to touch)
        self.price_offset_threshold = float(getattr(settings, "QUOTE_PRICE_OFFSET_THRESHOLD", 0.005))
        self.last_anchor_mid_price = None   # Base anchor price
        self._last_raw_fv_yes: Optional[float] = None
        self._ewma_abs_fv_move = 0.0
        self._volatility_cooldown_until = 0.0
        
        self.is_yes_token = None # Resolved dynamically
        self.yes_token_id = None
        self.no_token_id = None
        
        self._trade_lock = asyncio.Lock()   # Lock for atomic order updates
        self.active_orders: Dict[str, Dict[str, Any]] = {}
        self.local_yes_exposure: float = 0.0
        self.local_no_exposure: float = 0.0
        
        self.suspended = False # Internal flag for Kill Switch
        self.exit_mode = False   # Graceful exit: stop BUY, unwind inventory, then shut down
        self._shutdown_requested = False  # Set when exposure cleared so run() can break

        # Outcomes: 2 = binary YES/NO; >2 = categorical (stricter MAX_EXPOSURE_CATEGORICAL)
        self.outcome_count: int = 2
        # Rewards Farming: loaded once from Redis on first tick
        self._rewards_loaded = False
        self.rewards_min_size: float = 0.0
        self.rewards_max_spread: float = 0.0
        self.reward_rate_per_day: float = 0.0

    @staticmethod
    def _dust_filter(exposure: float, threshold: float = 1.0) -> float:
        """
        Dust immunity: treat exposure < threshold as 0 for mode decisions.
        Prevents tiny residuals from triggering CROSS-TOKEN LOCK or liquidation.
        """
        return 0.0 if abs(exposure) < threshold else exposure

    def _market_data_health(self, snapshot: Any):
        is_live = trading_safety.mode is TradingMode.LIVE
        return assess_snapshot(
            snapshot,
            max_age_seconds=float(settings.MARKET_DATA_MAX_AGE_SEC),
            require_sequence=(
                is_live and bool(settings.MARKET_DATA_REQUIRE_SEQUENCE_LIVE)
            ),
            require_exchange_timestamp=(
                is_live
                and bool(settings.MARKET_DATA_REQUIRE_EXCHANGE_TIMESTAMP_LIVE)
            ),
            require_snapshot_id=(
                is_live and bool(settings.MARKET_DATA_REQUIRE_SNAPSHOT_ID_LIVE)
            ),
        )

    def _apply_market_constraints(self, snapshot: dict) -> bool:
        """Accept only venue constraints delivered with the authoritative book."""
        raw_tick = snapshot.get("tick_size")
        raw_min = snapshot.get("min_order_size")
        neg_risk = snapshot.get("neg_risk")
        if raw_tick in (None, "") or raw_min in (None, "") or not isinstance(neg_risk, bool):
            return trading_safety.mode is not TradingMode.LIVE
        try:
            tick = float(raw_tick)
            minimum = float(raw_min)
        except (TypeError, ValueError):
            return False
        # Keep this set aligned with the pinned SDK's exact rounding contract.
        if tick not in {0.1, 0.01, 0.005, 0.0025, 0.001, 0.0001} or minimum <= 0:
            return False
        changed = tick != self.tick_size or minimum != self.min_order_size
        self.tick_size = tick
        self.min_order_size = minimum
        self.base_size = max(self.min_order_size, self.configured_base_size)
        self.liquidate_threshold = self.base_size * 2.0
        if changed:
            self.last_anchor_mid_price = None
            logger.info(
                "[%s] Venue constraints updated: tick=%s min_size=%s neg_risk=%s",
                self.token_id[:6],
                self.tick_size,
                self.min_order_size,
                neg_risk,
            )
        return True

    def _quantize_price(self, price: float, side: OrderSide) -> float:
        tick = Decimal(str(self.tick_size))
        value = Decimal(str(price))
        rounding = ROUND_FLOOR if side == OrderSide.BUY else ROUND_CEILING
        steps = (value / tick).to_integral_value(rounding=rounding)
        lower = tick
        upper = Decimal("1") - tick
        return float(max(lower, min(upper, steps * tick)))

    @staticmethod
    def _quantize_size(size: float) -> float:
        return float(
            Decimal(str(size)).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        )

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
                    self.suspended = True
                    trading_safety.halt(
                        f"quote input parse failure for {self.token_id[:12]}"
                    )
                    logger.exception(
                        "[%s] PubSub message parse failed closed: %s",
                        self.token_id[:6],
                        e,
                    )
                    await self.cancel_all_orders()
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
                    self.suspended = True
                    trading_safety.halt(
                        f"quoting engine processing failure for {self.token_id[:12]}"
                    )
                    logger.exception(
                        f"[{self.token_id[:6]}] Error processing channel {channel}: {e}. "
                        "Engine suspended and canceling known orders."
                    )
                    await self.cancel_all_orders()
                if getattr(self, "_shutdown_requested", False):
                    break
        except asyncio.CancelledError:
            logger.info(f"QuotingEngine shutting down for Token {self.token_id}")
        finally:
            try:
                cancel_safe = await self.cancel_all_orders()
                if cancel_safe is not True:
                    trading_safety.halt(
                        f"engine shutdown could not confirm cancels for {self.token_id[:12]}"
                    )
            except Exception as exc:
                trading_safety.halt(
                    f"engine shutdown cancel crashed for {self.token_id[:12]}"
                )
                logger.exception(
                    "[%s] Engine shutdown cancellation failed: %s",
                    self.token_id[:6],
                    exc,
                )
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
                        OrderJournal.status.in_(
                            [OrderStatus.OPEN, OrderStatus.PENDING, OrderStatus.UNKNOWN]
                        ),
                    )
                )
                rows = res.scalars().all()
                paper_orders_to_restore = []
                for o in rows:
                    payload = o.payload or {}
                    if payload.get("token_id") != self.token_id:
                        continue
                    created_ts = time.time()
                    if getattr(o, "created_at", None) is not None:
                        try:
                            created_ts = float(o.created_at.timestamp())
                        except Exception:
                            created_ts = time.time()
                    active_id = o.exchange_order_id or o.order_id
                    filled_size = float(payload.get("filled_size", 0.0) or 0.0)
                    remaining_size = max(0.0, float(o.size) - filled_size)
                    if remaining_size <= 1e-9:
                        continue
                    self.active_orders[active_id] = {
                        "side": o.side.value,
                        "price": float(o.price),
                        "size": remaining_size,
                        "created_ts": created_ts,
                        "post_only": bool(payload.get("post_only", True)),
                    }
                    if (
                        trading_safety.mode is TradingMode.PAPER
                        and o.status in {OrderStatus.OPEN, OrderStatus.PENDING}
                    ):
                        if o.status == OrderStatus.PENDING:
                            o.status = OrderStatus.OPEN
                        paper_orders_to_restore.append(
                            {
                                "order_id": o.order_id,
                                "condition_id": self.condition_id,
                                "token_id": self.token_id,
                                "side": o.side.value,
                                "price": float(o.price),
                                "size": remaining_size,
                                "post_only": bool(payload.get("post_only", True)),
                                "created_at": created_ts,
                            }
                        )
                if paper_orders_to_restore:
                    await session.commit()
        if not ok:
            return False

        if trading_safety.mode is TradingMode.PAPER:
            from app.oms.paper_execution import paper_execution

            for paper_order in paper_orders_to_restore:
                await paper_execution.register(**paper_order)

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
            # Release locked margin immediately
            await self._update_pending_buy_notional()

    async def on_control_message(self, data: dict):
        """Handle incoming signals from the Watchdog, API, or Auto-Router"""
        action = data.get("action")
        if action == "suspend":
            async with self._trade_lock:
                self.suspended = True
                logger.critical(f"[{self.token_id[:6]}] QuotingEngine SUSPENDED by Control Signal. Executing TRUE KILL SWITCH.")
                # True Kill Switch: Must synchronously wait for all orphans to cancel
                if await self.cancel_all_orders() is not True:
                    trading_safety.halt(
                        f"suspend could not confirm cancels for {self.token_id[:12]}"
                    )
                await self._publish_engine_mode("SUSPENDED")
        elif action == "resume":
            async with self._trade_lock:
                self.suspended = False
                logger.info(f"[{self.token_id[:6]}] QuotingEngine RESUMED by Control Signal.")
        elif action == "graceful_exit":
            async with self._trade_lock:
                if self.exit_mode:
                    return
                self.exit_mode = True
                self.last_anchor_mid_price = None  # Force re-eval on next tick without debounce
                logger.info(
                    f"[QuotingEngine {self.condition_id[:10]}] Entered GRACEFUL_EXIT mode. "
                    "Immediately canceling all active orders..."
                )
                await self.cancel_all_orders()
                await self._publish_engine_mode("GRACEFUL_EXIT")
                
    async def _complete_graceful_exit(self, mode_label="GRACEFUL_EXIT"):
        """Cleanly release resources for this token engine upon exit."""
        logger.info(f"[QuotingEngine {self.condition_id[:10]}] {mode_label} complete. Shutting down.")
        if await self.cancel_all_orders() is not True:
            trading_safety.halt(
                f"graceful exit could not confirm cancels for {self.token_id[:12]}"
            )
        side = "YES" if self.is_yes_token else "NO"
        if redis_client.client:
            await redis_client.client.delete(f"engine_state:{self.condition_id}:{side}")
        self._shutdown_requested = True

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

    def _per_market_exposure_cap(self) -> float:
        from app.core.exposure_limits import exposure_cap_usd_for_outcome_count

        return exposure_cap_usd_for_outcome_count(self.outcome_count)

    async def _load_rewards_config(self) -> None:
        """Load rewards params from Redis once. Safe for markets with no rewards (defaults to 0)."""
        if self._rewards_loaded:
            return
        self.outcome_count = await resolve_outcome_count(self.condition_id)
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

    def _compute_effective_size(self, price: float, max_additional_notional: Optional[float] = None) -> float:
        """
        Grid-budget-aware size calculation (return value = CLOB order size in **outcome shares**).

        Rewards are never an input. Size comes from strategy configuration and is shrunk to
        risk budgets, or rejected if it falls below the venue minimum.

        max_additional_notional: if set, notional for this order is capped so cumulative new BUYs
        stay within strict per-market budget (MTM inventory + opposite-side pending already deducted).
        """
        max_exposure = self._per_market_exposure_cap()
        total_slots = max(1, self.grid_levels * 2)
        budget_per_order = max_exposure / total_slots

        # 1. Strategy size only; incentive metadata must not increase risk.
        target_size = self.base_size

        # 2. Risk Check: Total exposure cost for this engine's grid levels
        # (Approximate check: target_size * grid_levels should not exceed max_exposure)
        exposure_cost = target_size * self.grid_levels
        if exposure_cost > max_exposure:
            # Shrink target_size to fit max_exposure across all levels
            shrunk_size = max_exposure / self.grid_levels
            logger.warning(
                f"[{self.token_id[:6]}] [BUDGET] Shrinking size {target_size:.1f} -> {shrunk_size:.1f} "
                f"to fit per-market cap ({max_exposure:.1f}) across {self.grid_levels} levels."
            )
            target_size = shrunk_size

        # 3. Risk Check: Single order notional vs per-order budget
        if (target_size * price) > budget_per_order:
            # Shrink target_size to fit per-order notional budget
            shrunk_size = budget_per_order / price if price > 0 else 0.0
            logger.warning(
                f"[{self.token_id[:6]}] [BUDGET] Shrinking size {target_size:.1f} -> {shrunk_size:.1f} "
                f"to fit per-order notional budget ({budget_per_order:.2f} @ price {price:.2f})."
            )
            target_size = shrunk_size

        # 3b. Strict remaining notional (MTM + pending-aware budget from caller)
        if max_additional_notional is not None and price > 0:
            cap = max(0.0, float(max_additional_notional))
            max_shares = cap / price
            if target_size > max_shares:
                target_size = max_shares

        # 4. Final floor check (Polymarket minimum)
        if target_size < self.min_order_size:
            if target_size > 0:
                logger.warning(
                    f"[{self.token_id[:6]}] [BUDGET] Final size {target_size:.4f} "
                    f"< venue minimum {self.min_order_size:.4f}. Dropping order."
                )
            return 0.0

        return self._quantize_size(target_size)

    async def _get_unified_fair_value(
        self,
        current_snapshot: dict,
        *,
        yes_exposure: float,
        no_exposure: float,
        inventory_cap: float,
    ) -> Optional[Tuple[float, float, float]]:
        """Fuse independent YES/NO books; never infer one side from the other alone."""
        if self.is_yes_token is None or not self.yes_token_id or not self.no_token_id:
            return None
        other_token = self.no_token_id if self.is_yes_token else self.yes_token_id
        other_snapshot = await redis_client.get_state(f"ob:{other_token}")
        yes_snapshot = current_snapshot if self.is_yes_token else other_snapshot
        no_snapshot = other_snapshot if self.is_yes_token else current_snapshot
        for label, snapshot in (("YES", yes_snapshot), ("NO", no_snapshot)):
            health = self._market_data_health(snapshot)
            if not health.healthy:
                logger.warning(
                    "[%s] %s book cannot support binary pricing: %s",
                    self.token_id[:6],
                    label,
                    health.reason,
                )
                return None
        try:
            pair_skew = pair_time_skew_seconds(yes_snapshot, no_snapshot)
            if pair_skew > float(settings.ALPHA_MAX_PAIR_SKEW_SEC):
                raise PricingModelError(
                    f"paired book time skew {pair_skew:.3f}s exceeds limit"
                )
            yes_mid = (
                float(yes_snapshot["bids"][0]["price"])
                + float(yes_snapshot["asks"][0]["price"])
            ) / 2.0
            no_mid = (
                float(no_snapshot["bids"][0]["price"])
                + float(no_snapshot["asks"][0]["price"])
            ) / 2.0
            signal = self.alpha_model.calculate_yes_anchor(
                yes_snapshot,
                no_snapshot,
                yes_inventory_value=yes_exposure * yes_mid,
                no_inventory_value=no_exposure * no_mid,
                inventory_cap=inventory_cap,
            )
        except (KeyError, TypeError, ValueError, PricingModelError) as exc:
            logger.warning("[%s] Binary pricing rejected: %s", self.token_id[:6], exc)
            return None

        fv_yes = signal.yes_fair_value
        dynamic_spread = min(
            0.25,
            signal.dynamic_spread
            + self._ewma_abs_fv_move
            * float(settings.ALPHA_VOLATILITY_SPREAD_MULTIPLIER),
        )
        fv_current = fv_yes if self.is_yes_token else max(0.01, min(0.99, 1.0 - fv_yes))
        return fv_current, dynamic_spread, fv_yes

    def _volatility_guard_allows(self, fv_yes: float) -> bool:
        """Cancel/pause after an abrupt probability move or persistent turbulence."""
        current = float(fv_yes)
        if self._last_raw_fv_yes is None:
            self._last_raw_fv_yes = current
            return True
        absolute_move = abs(current - self._last_raw_fv_yes)
        self._last_raw_fv_yes = current
        alpha = float(settings.ALPHA_VOLATILITY_EWMA_ALPHA)
        self._ewma_abs_fv_move = (
            alpha * absolute_move + (1.0 - alpha) * self._ewma_abs_fv_move
        )
        if (
            absolute_move > float(settings.ALPHA_MAX_TICK_MOVE)
            or self._ewma_abs_fv_move > float(settings.ALPHA_MAX_EWMA_ABS_MOVE)
        ):
            self._volatility_cooldown_until = max(
                self._volatility_cooldown_until,
                time.monotonic() + float(settings.ALPHA_VOLATILITY_COOLDOWN_SEC),
            )
        return time.monotonic() >= self._volatility_cooldown_until
                
    async def on_tick(self, tick_data: dict):
        """Evaluate orderbook, apply unified FV + inventory state machine, execute dynamic spread."""
        bids = tick_data.get("bids", [])
        asks = tick_data.get("asks", [])
        book_health = self._market_data_health(tick_data)
        
        await self._load_rewards_config()

        async with self._trade_lock:
            if not book_health.healthy:
                logger.error(
                    "[%s] Market data rejected: %s. Canceling known orders and pausing quotes.",
                    self.token_id[:6],
                    book_health.reason,
                )
                await self.cancel_all_orders()
                await self._publish_engine_mode("MARKET_DATA_INVALID")
                return
            if not self._apply_market_constraints(tick_data):
                logger.error(
                    "[%s] Venue tick/min-size constraints are missing or invalid; pausing quotes.",
                    self.token_id[:6],
                )
                await self.cancel_all_orders()
                await self._publish_engine_mode("MARKET_CONSTRAINTS_INVALID")
                return
            if trading_safety.mode is TradingMode.PAPER:
                from app.oms.paper_execution import paper_execution

                await paper_execution.on_book(self.token_id, tick_data)

            # 1. Memory-only inventory read path (no DB I/O in on_tick).
            if self.is_yes_token is None:
                logger.warning(f"[{self.token_id[:6]}] Market context unavailable; skip tick.")
                return

            snap = await inventory_state.get_snapshot(self.condition_id)
            yes_exposure = float(snap.get("yes_exposure", 0.0))
            no_exposure = float(snap.get("no_exposure", 0.0))
            self.local_yes_exposure = yes_exposure
            self.local_no_exposure = no_exposure

            current_exposure = yes_exposure if self.is_yes_token else no_exposure
            opposite_exposure = no_exposure if self.is_yes_token else yes_exposure
            # Dust immunity: treat exposure < 1.0 as 0 for mode decisions (is_long, cross_token_locked)
            current_exposure_for_logic = self._dust_filter(current_exposure)
            my_pending_notional = (
                float(snap.get("pending_yes_buy_notional", 0.0))
                if self.is_yes_token
                else float(snap.get("pending_no_buy_notional", 0.0))
            )

            # [Graceful Exit] State Machine
            if self.exit_mode:
                if current_exposure <= 1.0:
                    await self._complete_graceful_exit("Exposure Cleared")
                    return
                
                # Check for dust below minimum exchange order size
                if current_exposure < self.min_order_size:
                    logger.warning(
                        f"[QuotingEngine {self.condition_id[:10]}] Residual exposure {current_exposure:.4f} "
                        f"is below venue min size ({self.min_order_size}). Executing DUST_EXIT."
                    )
                    await self._complete_graceful_exit("DUST_EXIT")
                    return

                logger.info(
                    f"[QuotingEngine {self.condition_id[:10]}] GRACEFUL_EXIT mode. "
                    f"Current exposure: ${current_exposure:.2f}. Waiting to unwind..."
                )

            # Check orderbook data AFTER graceful exit dust checks
            if not bids or not asks:
                if self.exit_mode:
                    # Still hold exposure but book is empty, publish mode and wait
                    await self._publish_engine_mode("GRACEFUL_EXIT", current_exposure=current_exposure)
                else:
                    logger.debug(f"[{self.token_id[:6]}] Orderbook missing bids or asks. Skipping calculation.")
                return

            # 2. Unified pricing (YES anchor + NO derived from 1-FV_yes)
            max_exposure_per_market = self._per_market_exposure_cap()
            unified = await self._get_unified_fair_value(
                tick_data,
                yes_exposure=yes_exposure,
                no_exposure=no_exposure,
                inventory_cap=max_exposure_per_market,
            )
            if unified is None:
                await self.cancel_all_orders()
                await self._publish_engine_mode("MARKET_DATA_INVALID")
                return
            fair_value, dynamic_spread, fv_yes = unified
            if not self._volatility_guard_allows(fv_yes):
                await self.cancel_all_orders()
                await self._publish_engine_mode(
                    "VOLATILITY_COOLDOWN", fair_value=fair_value, fv_yes=fv_yes
                )
                return

            # Strict per-market "used" budget: MTM inventory + all active BUY notional (bulletproof vs stale capital_used).
            fv_y_anchor = max(0.01, min(0.99, float(fv_yes)))
            held_inventory_value = yes_exposure * fv_y_anchor + no_exposure * (1.0 - fv_y_anchor)
            pending_yes_n = float(snap.get("pending_yes_buy_notional", 0.0))
            pending_no_n = float(snap.get("pending_no_buy_notional", 0.0))
            strict_local_used_dollars = held_inventory_value + pending_yes_n + pending_no_n
            strict_local_used_excluding_me = strict_local_used_dollars - my_pending_notional
            # Balance precheck: MTM inventory + opposite side's pending BUYs (this side's pending excluded — we replace our own quotes).
            local_used_dollars_excluding_me = strict_local_used_excluding_me

            # 3. Debounce / Throttle Mechanism Check (Bypassed if exiting)
            if not self.exit_mode and self.last_anchor_mid_price is not None:
                price_diff = abs(fair_value - self.last_anchor_mid_price)
                if price_diff <= self.price_offset_threshold:
                    # If local active orders are empty after confirmed cancels/restart,
                    # do not debounce the rebuild or the engine would stay idle.
                    if len(self.active_orders) > 0:
                        logger.debug(
                            f"[{self.token_id[:6]}] Tick ignored: Fair Value diff ({price_diff:.4f}) "
                            f"<= threshold ({self.price_offset_threshold}). Skip Grid Reset."
                        )
                        return
                    
            # Update the baseline anchor mid-price for future comparisons
            self.last_anchor_mid_price = fair_value
            
            # 4. Calculate optimal grid bounds based on Skewed Fair Value and Dynamic Spread
            anchor_distance = dynamic_spread / 2.0
            bid_1 = self._quantize_price(
                fair_value - anchor_distance, OrderSide.BUY
            )

            # Construct grid orders JSON
            orders_payload: List[dict] = []

            # Two-way quoting: extreme line (dollars) and exit flags
            extreme_threshold_dollars = max_exposure_per_market * 0.9
            my_capital_used = (
                float(snap.get("yes_capital_used", 0.0))
                if self.is_yes_token
                else float(snap.get("no_capital_used", 0.0))
            )
            is_extreme_long = my_capital_used >= extreme_threshold_dollars
            force_taker_exit = self.exit_mode and current_exposure > 1.0
            opposite_capital_used = (
                float(snap.get("no_capital_used", 0.0))
                if self.is_yes_token
                else float(snap.get("yes_capital_used", 0.0))
            )
            # Lock opposite side if it has consumed more than half of the extreme threshold
            cross_token_locked = opposite_capital_used >= (extreme_threshold_dollars * 0.5)
            own_side = "YES" if self.is_yes_token else "NO"
            opposite_side = "NO" if self.is_yes_token else "YES"

            best_bid_price = float(bids[0]["price"])
            best_ask_price = float(asks[0]["price"])

            # --- Line 1: SELL side (unwind / take profit / stop loss) ---
            if current_exposure_for_logic >= self.min_order_size or force_taker_exit:
                if is_extreme_long or force_taker_exit:
                    exit_intent = plan_bounded_sell(
                        bids=bids,
                        requested_size=min(
                            current_exposure, max(self.base_size, self.min_order_size)
                        ),
                        exposure=current_exposure,
                        capital_used=my_capital_used,
                        max_book_impact=float(settings.EXIT_MAX_BOOK_IMPACT),
                        max_realized_loss_fraction=float(
                            settings.EXIT_MAX_REALIZED_LOSS_FRACTION
                        ),
                    )
                    if exit_intent is None:
                        logger.warning(
                            "[%s] Bounded exit is waiting: visible bid depth does not satisfy "
                            "impact/loss floors.",
                            self.token_id[:6],
                        )
                        ask_price = None
                    else:
                        ask_price = exit_intent.limit_price
                        ask_price = self._quantize_price(ask_price, OrderSide.SELL)
                        logger.warning(
                            "[%s] Bounded depth-aware exit: limit=%s size=%s "
                            "impact_floor=%s loss_floor=%s",
                            self.token_id[:6],
                            exit_intent.limit_price,
                            exit_intent.size,
                            exit_intent.impact_floor,
                            exit_intent.loss_floor,
                        )
                else:
                    logger.warning(
                        f"[{self.token_id[:6]}] INVENTORY HIGH ({current_exposure:.2f} "
                        f">= {self.min_order_size}). "
                        "MAKER UNWINDING (earn spread)."
                    )
                    ask_price = self._quantize_price(
                        fair_value + anchor_distance, OrderSide.SELL
                    )
                    safe_maker_floor = self._quantize_price(
                        best_bid_price + self.tick_size, OrderSide.SELL
                    )
                    ask_price = max(safe_maker_floor, ask_price)
                    
                    if ask_price > 1.0 - self.tick_size:
                        logger.warning(
                            f"[{self.token_id[:6]}] MAKER SELL blocked: required price {ask_price} > 0.99 "
                            f"(best_bid={best_bid_price}). Waiting for book to shift."
                        )
                        ask_price = None  # Use None to signal skipping this order
                    else:
                        ask_price = max(self.tick_size, ask_price)

                if ask_price is not None:
                    if current_exposure < self.min_order_size:
                        logger.debug(
                            f"[{self.token_id[:6]}] Inventory too small to sell "
                            f"({current_exposure:.2f} < {self.min_order_size}). Skipping."
                        )
                    else:
                        sell_size = self._quantize_size(
                            exit_intent.size
                            if (is_extreme_long or force_taker_exit)
                            else min(
                                current_exposure,
                                max(self.base_size, self.min_order_size),
                            )
                        )
                        orders_payload.append(
                            {
                                "condition_id": self.condition_id,
                                "token_id": self.token_id,
                                "side": OrderSide.SELL,
                                "price": ask_price,
                                "size": sell_size,
                                "post_only": not (is_extreme_long or force_taker_exit),
                            }
                        )
                        mode_label = "EXTREME TAKER" if (is_extreme_long or force_taker_exit) else "MAKER UNWINDING"
                        logger.info(
                            f"[{self.token_id[:6]}] {mode_label}: Ask {ask_price} | Size {sell_size:.2f} | "
                            f"Exposure {current_exposure:.2f}"
                        )

            # --- Line 2: BUY side (build position / take liquidity when safe) ---
            new_risk_blocked = not bool(settings.OFFLINE_VALIDATED_ALPHA_ENABLED)
            strict_budget_block_buys = strict_local_used_dollars >= max_exposure_per_market - 1e-6
            if new_risk_blocked and not self.exit_mode:
                logger.warning(
                    "[%s] New BUY risk disabled: no offline-validated alpha is armed.",
                    self.token_id[:6],
                )
            if strict_budget_block_buys and not self.exit_mode:
                if self.outcome_count > 2:
                    logger.warning(
                        "[BUDGET] Categorical market strict cap hit: %s >= MAX_EXPOSURE_CATEGORICAL (%s). No new BUY orders.",
                        f"{strict_local_used_dollars:.2f}",
                        f"{max_exposure_per_market:.2f}",
                    )
                else:
                    logger.warning(
                        f"[{self.token_id[:6]}] STRICT BUDGET CAP: MTM+pending ${strict_local_used_dollars:.2f} "
                        f">= MAX_EXPOSURE_PER_MARKET ${max_exposure_per_market:.2f} — no new BUY orders."
                    )
            if (
                not is_extreme_long
                and not self.exit_mode
                and not strict_budget_block_buys
                and not new_risk_blocked
            ):
                if cross_token_locked:
                    logger.warning(
                        f"[{self.token_id[:6]}] CROSS-TOKEN LOCK: opposite {opposite_side} exposure "
                        f"{opposite_exposure:.2f} >= liquidate_threshold({self.liquidate_threshold:.2f}). "
                        f"Suspend BUY on {own_side}, keep cash for {opposite_side} liquidation."
                    )
                else:
                    one_tick_below = getattr(settings, "QUOTE_BID_ONE_TICK_BELOW_TOUCH", True)
                    seen_bid_prices: set = set()
                    buy_budget_remaining = max(
                        0.0, max_exposure_per_market - strict_local_used_excluding_me
                    )
                    for i in range(self.grid_levels):
                        raw = bid_1 - (i * self.tick_size)
                        bid_price = self._quantize_price(raw, OrderSide.BUY)
                        if (
                            one_tick_below
                            and i == 0
                            and bid_price < best_bid_price - self.tick_size
                        ):
                            bid_price = self._quantize_price(
                                max(bid_price, best_bid_price - self.tick_size),
                                OrderSide.BUY,
                            )

                        max_buy = self._quantize_price(
                            best_ask_price - self.tick_size, OrderSide.BUY
                        )
                        if bid_price > max_buy:
                            logger.warning(
                                f"[{self.token_id[:6]}] 触发价格极值保护: BUY {bid_price} > best_ask-tick {max_buy}"
                            )
                            bid_price = max_buy
                        
                        if bid_price < self.tick_size:
                            logger.warning(
                                f"[{self.token_id[:6]}] BUY price dropped below tick {self.tick_size} "
                                f"(best_ask={best_ask_price}). "
                                "Skipping order to avoid crossing book."
                            )
                            continue

                        if bid_price in seen_bid_prices:
                            continue
                        seen_bid_prices.add(bid_price)

                        economics = evaluate_quote_economics(
                            side=OrderSide.BUY.value,
                            limit_price=bid_price,
                            fair_value=fair_value,
                            execution_cost_buffer=float(
                                settings.EXECUTION_COST_BUFFER
                            ),
                            adverse_selection_buffer=float(
                                settings.ADVERSE_SELECTION_BUFFER
                            ),
                            minimum_net_edge=float(settings.MIN_EXPECTED_NET_EDGE),
                        )
                        if not economics.allowed:
                            logger.info(
                                "[%s] BUY@%s rejected by net-edge gate: net=%s min=%s",
                                self.token_id[:6],
                                bid_price,
                                f"{economics.net_edge:.4f}",
                                f"{float(settings.MIN_EXPECTED_NET_EDGE):.4f}",
                            )
                            continue

                        effective_size = self._compute_effective_size(
                            bid_price, max_additional_notional=buy_budget_remaining
                        )
                        if effective_size <= 0:
                            continue
                        orders_payload.append(
                            {
                                "condition_id": self.condition_id,
                                "token_id": self.token_id,
                                "side": OrderSide.BUY,
                                "price": bid_price,
                                "size": effective_size,
                                "post_only": True,
                            }
                        )
                        buy_budget_remaining = max(
                            0.0, buy_budget_remaining - effective_size * bid_price
                        )

            if self.exit_mode:
                mode = "GRACEFUL_EXIT"
            elif is_extreme_long:
                mode = "EXTREME_LIQUIDATING"
            elif new_risk_blocked:
                mode = "NO_VALIDATED_ALPHA"
            elif cross_token_locked:
                mode = "LOCKED_BY_OPPOSITE"
            else:
                mode = (
                    "TWO_WAY_QUOTING"
                    if current_exposure_for_logic >= self.min_order_size
                    else "QUOTING_BIDS_ONLY"
                )

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
            # 5b. Balance pre-check: trim BUY orders if budget exceeded (all in Dollars)
            global_other_markets_dollars = await inventory_state.get_global_used_dollars_excluding(self.condition_id)
            orders_payload = self._apply_balance_precheck(
                orders_payload,
                local_used_dollars_excluding_me=local_used_dollars_excluding_me,
                global_other_markets_dollars=global_other_markets_dollars,
                per_market_cap=max_exposure_per_market,
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
            await self.sync_orders_diff(
                orders_payload,
                fair_value=fair_value,
                force_cancel_undesired_buys=(
                    new_risk_blocked
                    or strict_budget_block_buys
                    or self.exit_mode
                    or is_extreme_long
                    or cross_token_locked
                ),
            )

    def _apply_balance_precheck(
        self,
        orders_payload: List[dict],
        local_used_dollars_excluding_me: float,
        global_other_markets_dollars: float,
        per_market_cap: Optional[float] = None,
    ) -> List[dict]:
        """
        Trim BUY orders if total notional exceeds available budget. All in Dollars.
        SELL orders are NEVER trimmed.
        """
        if not orders_payload:
            return orders_payload

        cap = (
            float(per_market_cap)
            if per_market_cap is not None
            else float(getattr(settings, "MAX_EXPOSURE_PER_MARKET", 40.0))
        )
        global_max_budget = float(getattr(settings, "GLOBAL_MAX_BUDGET", 280.0))

        local_available = max(0.0, cap - local_used_dollars_excluding_me)
        global_used = global_other_markets_dollars + local_used_dollars_excluding_me
        global_available = max(0.0, global_max_budget - global_used)
        available = min(local_available, global_available)

        buy_orders = [o for o in orders_payload if o["side"] == OrderSide.BUY]
        sell_orders = [o for o in orders_payload if o["side"] == OrderSide.SELL]

        total_buy_notional = sum(o["price"] * o["size"] for o in buy_orders)

        if total_buy_notional <= available:
            return orders_payload

        if not buy_orders:
            return sell_orders

        logger.warning(
            f"[{self.token_id[:6]}] 本地资金预检: BUY 总名义=${total_buy_notional:.2f} > "
            f"可用预算=${available:.2f} (local_used_dollars=${local_used_dollars_excluding_me:.2f}, global_used=${global_used:.2f}). "
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
                    if max_size >= self.min_order_size:
                        shrunk = dict(o)
                        shrunk["size"] = self._quantize_size(max_size)
                        kept.append(shrunk)
                        logger.warning(
                            f"[{self.token_id[:6]}] 缩减 BUY@{o['price']} size: "
                            f"{o['size']:.1f} -> {shrunk['size']:.1f}"
                        )
                    else:
                        logger.warning(
                            f"[{self.token_id[:6]}] 跳过 BUY@{o['price']}: "
                            f"余额不足最小单量 {self.min_order_size}"
                        )
                break  # no budget left for further levels

        return sell_orders + kept

    @staticmethod
    def _order_signature(
        side: str, price: float, size: float, post_only: bool
    ) -> Tuple[str, float, float, bool]:
        return (
            side,
            round(float(price), 4),
            round(float(size), 4),
            bool(post_only),
        )

    async def _update_pending_buy_notional(self):
        """Calculate and update the total notional value of active BUY orders in inventory_state."""
        if self.is_yes_token is None:
            return
        
        total_buy_notional = 0.0
        for meta in self.active_orders.values():
            if str(meta.get("side", "")).upper() == "BUY":
                price = float(meta.get("price", 0.0))
                size = float(meta.get("size", 0.0))
                total_buy_notional += price * size

        await inventory_state.update_pending_buy_notional(
            market_id=self.condition_id,
            is_yes=self.is_yes_token,
            notional=total_buy_notional
        )

    def _consume_compatible_desired_order(
        self,
        desired_by_sig: Dict[Tuple[str, float, float, bool], List[dict]],
        side: str,
        price: float,
        price_offset_threshold: float,
    ) -> None:
        """
        When we keep a stale order for anti-churn reasons, consume one compatible desired order
        so we don't create a near-duplicate replacement in the same tick.
        """
        for sig in list(desired_by_sig.keys()):
            sig_side, sig_price, _sig_size, _post_only = sig
            if str(sig_side).upper() != str(side).upper():
                continue
            if abs(float(sig_price) - float(price)) <= price_offset_threshold:
                bucket = desired_by_sig.get(sig) or []
                if bucket:
                    bucket.pop()
                if not bucket:
                    desired_by_sig.pop(sig, None)
                return

    async def sync_orders_diff(
        self,
        desired_orders: List[dict],
        fair_value: Optional[float] = None,
        *,
        force_cancel_undesired_buys: bool = False,
    ):
        desired_by_sig: Dict[Tuple[str, float, float, bool], List[dict]] = defaultdict(list)
        for o in desired_orders:
            sig = self._order_signature(
                o["side"].value,
                o["price"],
                o["size"],
                o.get("post_only", True),
            )
            desired_by_sig[sig].append(o)

        # 1) Keep exact matches to preserve time-priority.
        to_cancel: List[str] = []
        kept_for_lifetime = 0
        kept_for_price_offset = 0
        now_ts = time.time()
        reconciliation_buffer_seconds = float(
            getattr(settings, "RECONCILIATION_BUFFER_SECONDS", 8.0)
        )
        price_offset_threshold = float(
            getattr(settings, "QUOTE_PRICE_OFFSET_THRESHOLD", 0.01)
        )
        for order_id, meta in list(self.active_orders.items()):
            if force_cancel_undesired_buys and str(
                meta.get("side", "")
            ).upper() == OrderSide.BUY.value:
                to_cancel.append(order_id)
                continue
            sig = self._order_signature(
                str(meta.get("side", "")),
                float(meta.get("price", 0.0)),
                float(meta.get("size", 0.0)),
                bool(meta.get("post_only", True)),
            )
            bucket = desired_by_sig.get(sig)
            if bucket:
                bucket.pop()
                if not bucket:
                    desired_by_sig.pop(sig, None)
            else:
                # Never preserve an undesired BUY for queue priority/rewards. A risk or
                # economics contraction must remove old new-risk intents immediately.
                if str(meta.get("side", "")).upper() == OrderSide.BUY.value:
                    to_cancel.append(order_id)
                    continue
                # Anti-churn gates for non-exact SELL replacement: minimum lifetime and
                # fair-value proximity only. Incentives never keep stale risk-reduction orders.
                created_ts = float(meta.get("created_ts", now_ts))
                if "created_ts" not in meta:
                    # Older cache entries: initialize to "now" to avoid immediate churn.
                    meta["created_ts"] = now_ts
                    created_ts = now_ts
                age_sec = max(0.0, now_ts - created_ts)
                order_price = float(meta.get("price", 0.0))
                side = str(meta.get("side", ""))
                price_diff_from_fv = (
                    abs(order_price - float(fair_value)) if fair_value is not None else float("inf")
                )
                if age_sec < reconciliation_buffer_seconds:
                    kept_for_lifetime += 1
                    self._consume_compatible_desired_order(
                        desired_by_sig, side, order_price, price_offset_threshold
                    )
                    continue
                if fair_value is not None and price_diff_from_fv <= price_offset_threshold:
                    kept_for_price_offset += 1
                    self._consume_compatible_desired_order(
                        desired_by_sig, side, order_price, price_offset_threshold
                    )
                    continue
                to_cancel.append(order_id)

        # 2) Cancel only stale orders.
        if kept_for_lifetime or kept_for_price_offset:
            logger.info(
                f"[{self.token_id[:6]}] Diff quoting anti-churn keep: "
                f"lifetime={kept_for_lifetime}, "
                f"price_offset={kept_for_price_offset} "
                f"(buffer={reconciliation_buffer_seconds:.2f}s, "
                f"offset={price_offset_threshold:.4f})"
            )
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
            if any(result is not True for result in results):
                trading_safety.set_readiness(
                    "open_orders_reconciled",
                    False,
                    "quote replacement cancel was not confirmed",
                )
                trading_safety.halt(
                    f"quote replacement cancel failed for {self.token_id[:12]}"
                )
                await self._update_pending_buy_notional()
                return

        # 3) Create only missing desired orders.
        to_create = [o for bucket in desired_by_sig.values() for o in bucket]
        if to_create:
            logger.info(f"[{self.token_id[:6]}] Diff quoting: create missing={len(to_create)}")
            await self.place_orders(to_create)

        # 4) Update pending buy notional tracker
        await self._update_pending_buy_notional()

    async def place_orders(self, orders_payload: List[dict]):
        """Executes the placement of multiple orders concurrently through OMS"""
        tasks = []
        for o in orders_payload:
            tasks.append(oms.create_order(
                condition_id=o["condition_id"],
                token_id=o["token_id"],
                side=o["side"],
                price=o["price"],
                size=o["size"],
                post_only=bool(o.get("post_only", True)),
            ))
            
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for order_req, res in zip(orders_payload, results):
            if isinstance(res, str):
                self.active_orders[res] = {
                    "side": order_req["side"].value,
                    "price": float(order_req["price"]),
                    "size": float(order_req["size"]),
                    "created_ts": time.time(),
                    "post_only": bool(order_req.get("post_only", True)),
                }
            elif isinstance(res, Exception):
                logger.error(
                    "[%s] Order placement task crashed for %s %s@%s: %s",
                    self.token_id[:6],
                    order_req["side"].value,
                    order_req["size"],
                    order_req["price"],
                    res,
                )
            else:
                logger.warning(
                    "[%s] Order placement was rejected/blocked for %s %s@%s",
                    self.token_id[:6],
                    order_req["side"].value,
                    order_req["size"],
                    order_req["price"],
                )

    async def cancel_all_orders(self):
        """Cancel the cached grid and return True only when every cancel is confirmed."""
        if not self.active_orders:
            # Still update notional to 0 just to be sure
            await self._update_pending_buy_notional()
            return True
            
        order_ids = list(self.active_orders.keys())
        logger.info(f"[{self.token_id[:6]}] Canceling {len(order_ids)} active orders...")
        
        tasks = [oms.cancel_order(oid) for oid in order_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for order_id, success in zip(order_ids, results):
            if success is True:
                self.active_orders.pop(order_id, None)
            else:
                # Downgraded from CRITICAL: OMS already handles matched-order scenarios
                # at INFO level. Remaining failures are transient network / circuit-breaker.
                logger.warning(f"[{self.token_id[:6]}] Cancel failed for order {order_id} (reason: {success}). Will retry next tick.")
                
        await self._update_pending_buy_notional()
        return not self.active_orders

async def start_quoting_engine(condition_id: str, token_id: str):
    engine = QuotingEngine(condition_id, token_id)
    await engine.run()
