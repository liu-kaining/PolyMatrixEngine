import asyncio
import logging
import time
from typing import Dict, Optional, Tuple

from sqlalchemy.future import select

from app.db.session import AsyncSessionLocal
from app.models.db_models import InventoryLedger

logger = logging.getLogger(__name__)


class InventoryStateManager:
    """
    In-memory inventory state with async DB persistence.

    - read path (engine on_tick): memory only
    - write path (user fill events): memory first, DB async queue
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(InventoryStateManager, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._state: Dict[str, Dict[str, float]] = {}
        self._lock = asyncio.Lock()
        # Bounded queue to avoid unbounded memory growth under persistent DB failures.
        self._persist_queue: asyncio.Queue[Tuple[str, float, float, float, float, float, float]] = asyncio.Queue(
            maxsize=1000
        )
        self._persist_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._persist_task and not self._persist_task.done():
            return
        self._persist_task = asyncio.create_task(self._persist_worker())
        logger.info("InventoryStateManager started.")

    async def stop(self) -> None:
        if not self._persist_task:
            return
        logger.info("Waiting for inventory persist queue to drain...")
        # Ensure all queued inventory updates are flushed to DB before shutdown.
        await self._persist_queue.join()

        self._persist_task.cancel()
        try:
            await self._persist_task
        except asyncio.CancelledError:
            pass
        self._persist_task = None
        logger.info("InventoryStateManager stopped.")

    async def clear(self) -> None:
        async with self._lock:
            self._state.clear()
        while not self._persist_queue.empty():
            try:
                self._persist_queue.get_nowait()
                self._persist_queue.task_done()
            except asyncio.QueueEmpty:
                break

    async def ensure_loaded(self, market_id: str) -> Dict[str, float]:
        async with self._lock:
            existing = self._state.get(market_id)
            if existing is not None:
                return dict(existing)

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(InventoryLedger).filter(InventoryLedger.market_id == market_id)
            )
            inv = result.scalar_one_or_none()
            yes_exposure = float(inv.yes_exposure or 0.0) if inv else 0.0
            no_exposure = float(inv.no_exposure or 0.0) if inv else 0.0
            yes_capital_used = float(getattr(inv, "yes_capital_used", 0.0) or 0.0) if inv else 0.0
            no_capital_used = float(getattr(inv, "no_capital_used", 0.0) or 0.0) if inv else 0.0
            realized_pnl = float(inv.realized_pnl or 0.0) if inv else 0.0

        snapshot = {
            "yes_exposure": yes_exposure,
            "no_exposure": no_exposure,
            "yes_capital_used": yes_capital_used,
            "no_capital_used": no_capital_used,
            "pending_yes_buy_notional": 0.0,
            "pending_no_buy_notional": 0.0,
            "realized_pnl": realized_pnl,
            "last_local_fill_timestamp": 0.0,
            "updated_at": time.time(),
            # V8.0: cumulative unhedged BUY fills (size + weighted avg price)
            "yes_unhedged_size": 0.0,
            "yes_unhedged_cost": 0.0,
            "no_unhedged_size": 0.0,
            "no_unhedged_cost": 0.0,
        }
        async with self._lock:
            current = self._state.setdefault(market_id, snapshot)
            return dict(current)

    async def get_snapshot(self, market_id: str) -> Dict[str, float]:
        return await self.ensure_loaded(market_id)

    async def get_global_used_dollars(self) -> float:
        """Total USDC used across all markets (capital_used + pending buy notional). Units: Dollars."""
        total = 0.0
        async with self._lock:
            for snap in self._state.values():
                total += (
                    float(snap.get("yes_capital_used", 0.0))
                    + float(snap.get("no_capital_used", 0.0))
                    + float(snap.get("pending_yes_buy_notional", 0.0))
                    + float(snap.get("pending_no_buy_notional", 0.0))
                )
        return total

    async def get_used_dollars_for_market(self, market_id: str) -> float:
        """USDC used for a single market. Includes capital already spent + pending open orders."""
        snap = await self.get_snapshot(market_id)
        return (
            float(snap.get("yes_capital_used", 0.0))
            + float(snap.get("no_capital_used", 0.0))
            + float(snap.get("pending_yes_buy_notional", 0.0))
            + float(snap.get("pending_no_buy_notional", 0.0))
        )

    async def get_global_used_dollars_excluding(self, market_id: str) -> float:
        """Total USDC used across all markets EXCEPT the specified one. Units: Dollars."""
        total = 0.0
        async with self._lock:
            for m_id, snap in self._state.items():
                if m_id == market_id:
                    continue
                total += (
                    float(snap.get("yes_capital_used", 0.0))
                    + float(snap.get("no_capital_used", 0.0))
                    + float(snap.get("pending_yes_buy_notional", 0.0))
                    + float(snap.get("pending_no_buy_notional", 0.0))
                )
        return total

    async def update_pending_buy_notional(
        self, market_id: str, is_yes: bool, notional: float
    ) -> None:
        """Update the total notional value of all active BUY orders for a token."""
        await self.ensure_loaded(market_id)
        async with self._lock:
            snap = self._state[market_id]
            if is_yes:
                snap["pending_yes_buy_notional"] = float(max(0.0, notional))
            else:
                snap["pending_no_buy_notional"] = float(max(0.0, notional))
            snap["updated_at"] = time.time()

    async def get_last_local_fill_timestamp(self, market_id: str) -> float:
        await self.ensure_loaded(market_id)
        async with self._lock:
            snap = self._state.get(market_id) or {}
            return float(snap.get("last_local_fill_timestamp", 0.0))

    async def get_avg_cost_basis(self, market_id: str, is_yes: bool) -> float:
        """Return average cost basis (capital_used / exposure) for requested side. 0 if no exposure.
        Used by watchdog PnL reporting and dashboard observability."""
        await self.ensure_loaded(market_id)
        async with self._lock:
            snap = self._state.get(market_id) or {}
            if is_yes:
                exp = float(snap.get("yes_exposure", 0.0))
                cap = float(snap.get("yes_capital_used", 0.0))
            else:
                exp = float(snap.get("no_exposure", 0.0))
                cap = float(snap.get("no_capital_used", 0.0))
            return cap / exp if exp > 1e-9 else 0.0

    async def get_unrealized_pnl(self, market_id: str, fv_yes: float) -> Dict[str, float]:
        """
        Compute per-side and total unrealized PnL based on current fair value.
        fv_yes: current YES token fair value [0, 1]. NO is derived as 1 - fv_yes.
        Returns dict with yes_unrealized_pnl, no_unrealized_pnl, total_unrealized_pnl.
        """
        await self.ensure_loaded(market_id)
        async with self._lock:
            snap = self._state.get(market_id) or {}
            yes_exp = float(snap.get("yes_exposure", 0.0))
            no_exp = float(snap.get("no_exposure", 0.0))
            yes_cap = float(snap.get("yes_capital_used", 0.0))
            no_cap = float(snap.get("no_capital_used", 0.0))

        fv_no = max(0.01, min(0.99, 1.0 - fv_yes))
        fv_yes = max(0.01, min(0.99, fv_yes))

        # Unrealized PnL = current_value - cost_basis
        yes_unrealized = (yes_exp * fv_yes - yes_cap) if yes_exp > 1e-9 else 0.0
        no_unrealized = (no_exp * fv_no - no_cap) if no_exp > 1e-9 else 0.0

        return {
            "yes_unrealized_pnl": yes_unrealized,
            "no_unrealized_pnl": no_unrealized,
            "total_unrealized_pnl": yes_unrealized + no_unrealized,
        }

    async def accumulate_unhedged_fill(
        self, market_id: str, is_yes: bool, filled_size: float, fill_price: float
    ) -> Tuple[float, float]:
        """
        V8.0: Accumulate a BUY fill into unhedged tracking.
        Returns (total_unhedged_size, weighted_avg_price) after accumulation.
        """
        await self.ensure_loaded(market_id)
        async with self._lock:
            snap = self._state[market_id]
            size_key = "yes_unhedged_size" if is_yes else "no_unhedged_size"
            cost_key = "yes_unhedged_cost" if is_yes else "no_unhedged_cost"
            old_size = float(snap.get(size_key, 0.0))
            old_cost = float(snap.get(cost_key, 0.0))
            new_size = old_size + filled_size
            new_cost = old_cost + filled_size * fill_price
            snap[size_key] = new_size
            snap[cost_key] = new_cost
            avg_price = new_cost / new_size if new_size > 1e-9 else fill_price
            return new_size, avg_price

    async def clear_unhedged(self, market_id: str, is_yes: bool) -> None:
        """V8.0: Reset unhedged tracking after hedge order is placed."""
        await self.ensure_loaded(market_id)
        async with self._lock:
            snap = self._state[market_id]
            if is_yes:
                snap["yes_unhedged_size"] = 0.0
                snap["yes_unhedged_cost"] = 0.0
            else:
                snap["no_unhedged_size"] = 0.0
                snap["no_unhedged_cost"] = 0.0

    async def apply_fill(
        self,
        market_id: str,
        is_yes: bool,
        side: str,
        filled_size: float,
        fill_price: float,
    ) -> Dict[str, float]:
        await self.ensure_loaded(market_id)

        side_u = (side or "").upper()
        now_ts = time.time()

        async with self._lock:
            snap = self._state[market_id]
            yes_exposure = float(snap["yes_exposure"])
            no_exposure = float(snap["no_exposure"])
            yes_capital_used = float(snap.get("yes_capital_used", 0.0))
            no_capital_used = float(snap.get("no_capital_used", 0.0))
            realized_pnl = float(snap["realized_pnl"])

            if side_u == "BUY":
                cost = fill_price * filled_size
                if is_yes:
                    yes_exposure += filled_size
                    yes_capital_used += cost
                else:
                    no_exposure += filled_size
                    no_capital_used += cost
                realized_pnl -= cost
            elif side_u == "SELL":
                if is_yes:
                    # Average-cost reduction
                    if yes_exposure > 1e-9:
                        cost_basis = yes_capital_used / yes_exposure
                        yes_capital_used -= cost_basis * filled_size
                    yes_capital_used = max(0.0, yes_capital_used)
                    yes_exposure -= filled_size
                else:
                    if no_exposure > 1e-9:
                        cost_basis = no_capital_used / no_exposure
                        no_capital_used -= cost_basis * filled_size
                    no_capital_used = max(0.0, no_capital_used)
                    no_exposure -= filled_size
                realized_pnl += fill_price * filled_size

            snap["yes_exposure"] = yes_exposure
            snap["no_exposure"] = no_exposure
            snap["yes_capital_used"] = yes_capital_used
            snap["no_capital_used"] = no_capital_used
            snap["realized_pnl"] = realized_pnl
            snap["last_local_fill_timestamp"] = now_ts
            snap["updated_at"] = now_ts

            updated = dict(snap)

        # Fire-and-forget style persistence via async queue.
        # If queue is full, do not raise (would crash handle_fill task); log and skip this persist.
        try:
            self._persist_queue.put_nowait(
                (
                    market_id,
                    updated["yes_exposure"],
                    updated["no_exposure"],
                    updated["yes_capital_used"],
                    updated["no_capital_used"],
                    updated["realized_pnl"],
                    updated["updated_at"],
                )
            )
        except asyncio.QueueFull:
            logger.error(
                f"InventoryStateManager persist queue FULL (maxsize={self._persist_queue.maxsize}). "
                f"Dropping persist for {market_id[:12]}...; in-memory state is updated but DB may drift."
            )
        return updated

    async def apply_reconciliation_snapshot(
        self,
        market_id: str,
        yes_exposure: float,
        no_exposure: float,
        yes_capital_used: Optional[float] = None,
        no_capital_used: Optional[float] = None,
    ) -> Dict[str, float]:
        await self.ensure_loaded(market_id)
        async with self._lock:
            snap = self._state[market_id]
            old_yes = float(snap["yes_exposure"])
            old_no = float(snap["no_exposure"])
            snap["yes_exposure"] = float(yes_exposure)
            snap["no_exposure"] = float(no_exposure)
            # Capital sync: prefer explicit values from reconciliation (DB-aligned).
            # If not provided, fall back to proportional adjustment based on exposure ratios.
            if yes_capital_used is not None:
                snap["yes_capital_used"] = float(yes_capital_used)
            else:
                if old_yes > 1e-9:
                    snap["yes_capital_used"] = float(snap.get("yes_capital_used", 0.0)) * (yes_exposure / old_yes)
                else:
                    snap["yes_capital_used"] = 0.0

            if no_capital_used is not None:
                snap["no_capital_used"] = float(no_capital_used)
            else:
                if old_no > 1e-9:
                    snap["no_capital_used"] = float(snap.get("no_capital_used", 0.0)) * (no_exposure / old_no)
                else:
                    snap["no_capital_used"] = 0.0
            snap["updated_at"] = time.time()
            return dict(snap)

    async def _persist_worker(self) -> None:
        while True:
            market_id, yes_exposure, no_exposure, yes_capital_used, no_capital_used, realized_pnl, snapshot_ts = await self._persist_queue.get()
            try:
                async with self._lock:
                    current = self._state.get(market_id)
                    if current and float(current.get("updated_at", 0.0)) > float(snapshot_ts):
                        continue

                async with AsyncSessionLocal() as session:
                    result = await session.execute(
                        select(InventoryLedger)
                        .filter(InventoryLedger.market_id == market_id)
                        .with_for_update()
                    )
                    inv = result.scalar_one_or_none()
                    if inv is None:
                        inv = InventoryLedger(
                            market_id=market_id,
                            yes_exposure=yes_exposure,
                            no_exposure=no_exposure,
                            yes_capital_used=yes_capital_used,
                            no_capital_used=no_capital_used,
                            realized_pnl=realized_pnl,
                        )
                        session.add(inv)
                    else:
                        inv.yes_exposure = yes_exposure
                        inv.no_exposure = no_exposure
                        inv.yes_capital_used = yes_capital_used
                        inv.no_capital_used = no_capital_used
                        inv.realized_pnl = realized_pnl
                    await session.commit()
            except Exception as e:
                logger.error(
                    f"InventoryStateManager persist failed for {market_id[:8]}...: {e}"
                )
            finally:
                self._persist_queue.task_done()


inventory_state = InventoryStateManager()
