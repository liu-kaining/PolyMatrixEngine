import asyncio
import logging
import time
from typing import Dict, Optional

from sqlalchemy.future import select

from app.db.session import AsyncSessionLocal
from app.models.db_models import InventoryLedger
logger = logging.getLogger(__name__)


class InventoryStateManager:
    """
    Read-optimized cache of the DB-authoritative inventory ledger.

    - read path (engine on_tick): memory only
    - write path: durable DB transaction first, then apply_reconciliation_snapshot
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

    async def start(self) -> None:
        logger.info("InventoryStateManager DB-authoritative cache started.")

    async def stop(self) -> None:
        logger.info("InventoryStateManager DB-authoritative cache stopped.")

    async def clear(self) -> None:
        async with self._lock:
            self._state.clear()

    async def load_all(self) -> int:
        """Load every persisted ledger so global risk never ignores cold positions."""
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(select(InventoryLedger))).scalars().all()
        loaded_at = time.time()
        snapshots = {}
        for inv in rows:
            snapshots[inv.market_id] = {
                "yes_exposure": float(inv.yes_exposure or 0.0),
                "no_exposure": float(inv.no_exposure or 0.0),
                "yes_capital_used": float(inv.yes_capital_used or 0.0),
                "no_capital_used": float(inv.no_capital_used or 0.0),
                "pending_yes_buy_notional": 0.0,
                "pending_no_buy_notional": 0.0,
                "realized_pnl": float(inv.realized_pnl or 0.0),
                "last_local_fill_timestamp": 0.0,
                "state_version": int(inv.state_version or 0),
                "updated_at": loaded_at,
            }
        async with self._lock:
            self._state.update(snapshots)
        logger.info("Loaded %d inventory ledgers into global risk state.", len(snapshots))
        return len(snapshots)

    async def get_all_market_ids(self) -> set[str]:
        async with self._lock:
            return set(self._state.keys())

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
            state_version = int(getattr(inv, "state_version", 0) or 0) if inv else 0

        snapshot = {
            "yes_exposure": yes_exposure,
            "no_exposure": no_exposure,
            "yes_capital_used": yes_capital_used,
            "no_capital_used": no_capital_used,
            "pending_yes_buy_notional": 0.0,
            "pending_no_buy_notional": 0.0,
            "realized_pnl": realized_pnl,
            "last_local_fill_timestamp": 0.0,
            "state_version": state_version,
            "updated_at": time.time(),
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

    async def get_global_capital_used(self) -> float:
        """Persisted fill cost basis only; durable reservations are added by the caller."""
        total = 0.0
        async with self._lock:
            for snap in self._state.values():
                total += float(snap.get("yes_capital_used", 0.0)) + float(
                    snap.get("no_capital_used", 0.0)
                )
        return total

    async def get_market_capital_used(self, market_id: str) -> float:
        """Single-market fill cost basis only; excludes legacy in-memory pending values."""
        snap = await self.get_snapshot(market_id)
        return float(snap.get("yes_capital_used", 0.0)) + float(
            snap.get("no_capital_used", 0.0)
        )

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

    async def apply_reconciliation_snapshot(
        self,
        market_id: str,
        yes_exposure: float,
        no_exposure: float,
        yes_capital_used: Optional[float] = None,
        no_capital_used: Optional[float] = None,
        realized_pnl: Optional[float] = None,
        last_local_fill_timestamp: Optional[float] = None,
        state_version: Optional[int] = None,
    ) -> Dict[str, float]:
        await self.ensure_loaded(market_id)
        async with self._lock:
            snap = self._state[market_id]
            current_version = int(snap.get("state_version", 0) or 0)
            if state_version is not None and int(state_version) < current_version:
                logger.warning(
                    "Ignoring stale inventory snapshot for %s: version=%s < current=%s",
                    market_id[:12],
                    state_version,
                    current_version,
                )
                return dict(snap)
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
            if realized_pnl is not None:
                snap["realized_pnl"] = float(realized_pnl)
            if last_local_fill_timestamp is not None:
                snap["last_local_fill_timestamp"] = float(last_local_fill_timestamp)
            if state_version is not None:
                snap["state_version"] = int(state_version)
            snap["updated_at"] = time.time()
            return dict(snap)

inventory_state = InventoryStateManager()
