import asyncio
import logging
import time
import httpx
from sqlalchemy.future import select

from app.db.session import AsyncSessionLocal
from app.models.db_models import InventoryLedger, MarketMeta
from app.core.config import settings
from app.oms.core import oms
from app.core.redis import redis_client
from app.core.inventory_state import inventory_state

logger = logging.getLogger(__name__)

class RiskMonitor:
    def __init__(self):
        self.max_exposure = settings.MAX_EXPOSURE_PER_MARKET
        self.reconciliation_interval = 300 # 5 minutes
        self.exposure_tolerance = settings.EXPOSURE_TOLERANCE
        self.reconciliation_buffer_seconds = float(
            getattr(settings, "RECONCILIATION_BUFFER_SECONDS", 8.0)
        )

    async def run(self):
        """Background daemon polling risk metrics and reconciling"""
        logger.info("Watchdog started: Monitoring Delta Exposure & Reconciliation")
        
        # Start the reconciliation loop as a background task
        asyncio.create_task(self.reconciliation_loop())
        
        while True:
            try:
                await self.check_exposure()
                await asyncio.sleep(1) # Poll every second
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Watchdog error: {e}")
                await asyncio.sleep(5)

    async def check_exposure(self):
        """
        Real-time risk check based on in-memory state.
        This ensures immediate kill-switch activation on fill, without waiting for DB persistence.
        """
        # 1. Get all active condition_ids from the EngineSupervisor
        from app.core.market_lifecycle import get_active_router_markets
        active_cids = get_active_router_markets()

        for cid in active_cids:
            snap = await inventory_state.get_snapshot(cid)
            local_used_dollars = (
                float(snap.get("yes_capital_used", 0.0))
                + float(snap.get("no_capital_used", 0.0))
                + float(snap.get("pending_yes_buy_notional", 0.0))
                + float(snap.get("pending_no_buy_notional", 0.0))
            )

            if local_used_dollars <= self.max_exposure:
                continue

            # 3. Breach detected: Verify status in DB before triggering
            async with AsyncSessionLocal() as session:
                stmt = select(MarketMeta).filter(MarketMeta.condition_id == cid)
                result = await session.execute(stmt)
                market = result.scalar_one_or_none()

                if market and (market.status or "").lower() == "suspended":
                    continue

                logger.critical(
                    f"RISK BREACH (Memory): Market {cid[:12]} exceeded limit (${self.max_exposure:.2f})! "
                    f"local_used_dollars: ${local_used_dollars:.2f}"
                )
                await self.trigger_kill_switch(cid, session)

        # 4. Global Budget Check (all in Dollars)
        global_used_dollars = await inventory_state.get_global_used_dollars()
        global_max = float(getattr(settings, "GLOBAL_MAX_BUDGET", 1000.0))
        if global_used_dollars > global_max * 1.05:
            logger.critical(
                f"GLOBAL RISK BREACH: Total used ${global_used_dollars:.2f} exceeds budget ${global_max:.2f}!"
            )
             # Note: We don't trigger a global kill switch here yet to avoid market-wide panic,
             # but we log it as critical. The per-engine balance_precheck is the primary preventer.
                    
    async def trigger_kill_switch(self, condition_id: str, session):
        """Emergency procedure: cancel all orders, suspend quoting"""
        logger.error(f"!!! KILL SWITCH ACTIVATED for {condition_id} !!!")
        
        # 1. Suspend Quoting (Communicate to QuotingEngine via DB and Redis)
        stmt = select(MarketMeta).filter(MarketMeta.condition_id == condition_id)
        result = await session.execute(stmt)
        market = result.scalar_one_or_none()
        
        if market and market.status != "suspended":
            market.status = "suspended"
            await session.commit()
            
            # Send pub/sub message to tell engine to halt immediately
            await redis_client.publish(f"control:{condition_id}", {"action": "suspend"})
            logger.info(f"Published suspend signal for {condition_id}")
        
        # 2. Soft Cancel via Relayer (Cancel all active orders for this market)
        await oms.cancel_market_orders(condition_id)

    async def reconciliation_loop(self):
        """Periodically sync actual on-chain positions from Polymarket Data API"""
        if not settings.FUNDER_ADDRESS:
            logger.warning("FUNDER_ADDRESS not set. Skipping reconciliation loop.")
            return
            
        while True:
            await asyncio.sleep(self.reconciliation_interval)
            try:
                logger.info("Starting REST API Reconciliation Fallback...")
                await self.reconcile_positions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Reconciliation loop error: {e}")

    async def reconcile_positions(self):
        # 1. Fetch real on-chain positions
        url = f"https://data-api.polymarket.com/positions?user={settings.FUNDER_ADDRESS}"
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10.0)
            if resp.status_code != 200:
                logger.error(f"Failed to fetch positions. Status: {resp.status_code}")
                return
                
            positions = resp.json()
            
        if not isinstance(positions, list):
            logger.error(f"Unexpected positions format: {type(positions)}")
            return
            
        # Normalize conditionId for lookup (API may return different casing than DB)
        def _norm_cid(cid: str | None) -> str | None:
            if not cid or not isinstance(cid, str):
                return None
            s = cid.strip()
            if s.startswith("0x"):
                return s.lower()
            return s

        # Group actual positions by conditionId
        actual_inventory = {}
        for p in positions:
            cid = p.get("conditionId")
            if not cid:
                continue
            key = _norm_cid(cid)
            if key is None:
                continue
            if key not in actual_inventory:
                actual_inventory[key] = {"yes": 0.0, "no": 0.0}
            # Usually outcomeIndex 0 is YES, 1 is NO for binary
            outcome_idx = p.get("outcomeIndex")
            size = float(p.get("size", 0.0))
            if outcome_idx == 0 or str(p.get("outcome")).upper() == "YES":
                actual_inventory[key]["yes"] += size
            else:
                actual_inventory[key]["no"] += size

        # 2. Compare with DB Ledger (row-level lock to prevent dirty writes from concurrent handle_fill)
        async with AsyncSessionLocal() as session:
            stmt = select(InventoryLedger).with_for_update()
            result = await session.execute(stmt)
            db_inventories = result.scalars().all()
            
            for db_inv in db_inventories:
                cid = db_inv.market_id
                key = _norm_cid(cid)
                actual = actual_inventory.get(key, {"yes": 0.0, "no": 0.0}) if key else {"yes": 0.0, "no": 0.0}
                
                db_yes = float(db_inv.yes_exposure)
                db_no = float(db_inv.no_exposure)
                
                diff_yes = abs(db_yes - actual["yes"])
                diff_no = abs(db_no - actual["no"])
                
                if diff_yes > self.exposure_tolerance or diff_no > self.exposure_tolerance:
                    last_local_fill_ts = await inventory_state.get_last_local_fill_timestamp(cid)
                    if (
                        last_local_fill_ts > 0
                        and (time.time() - last_local_fill_ts) < self.reconciliation_buffer_seconds
                    ):
                        logger.info(
                            "本地刚刚发生真实成交，暂不信任远端 REST API 延迟数据，跳过本次对账"
                        )
                        logger.info(
                            f"Skipped reconcile overwrite for {cid[:8]} "
                            f"(age={time.time() - last_local_fill_ts:.2f}s < "
                            f"buffer={self.reconciliation_buffer_seconds:.2f}s)"
                        )
                        continue

                    logger.error(f"RECONCILIATION MISMATCH for {cid[:8]}!")
                    logger.error(f"DB -> YES: {db_yes:.2f}, NO: {db_no:.2f}")
                    logger.error(f"API -> YES: {actual['yes']:.2f}, NO: {actual['no']:.2f}")
                    
                    db_inv.yes_exposure = actual["yes"]
                    db_inv.no_exposure = actual["no"]

                    # Zero-out or proportionally adjust phantom capital
                    if actual["yes"] <= 0.001:
                        db_inv.yes_capital_used = 0.0
                    elif db_yes > 1e-9:
                        db_inv.yes_capital_used = float(db_inv.yes_capital_used) * (actual["yes"] / db_yes)

                    if actual["no"] <= 0.001:
                        db_inv.no_capital_used = 0.0
                    elif db_no > 1e-9:
                        db_inv.no_capital_used = float(db_inv.no_capital_used) * (actual["no"] / db_no)

                    logger.info(f"Local ledger overwritten with on-chain data for {cid[:8]}")

                    # Keep in-memory state aligned with DB overwrite.
                    await inventory_state.apply_reconciliation_snapshot(
                        market_id=cid,
                        yes_exposure=actual["yes"],
                        no_exposure=actual["no"],
                    )
            
            await session.commit()

watchdog = RiskMonitor()
