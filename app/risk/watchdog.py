import asyncio
import logging
import time
import httpx
from typing import Dict, Optional
from sqlalchemy.future import select

from app.db.session import AsyncSessionLocal
from app.models.db_models import InventoryLedger, MarketMeta
from app.core.config import settings
from app.core.exposure_limits import exposure_cap_usd_for_condition_redis_only
from app.oms.core import oms
from app.core.redis import redis_client
from app.core.inventory_state import inventory_state

logger = logging.getLogger(__name__)

def _norm_cid(cid: Optional[str]) -> Optional[str]:
    if not cid or not isinstance(cid, str):
        return None
    s = cid.strip()
    if s.startswith("0x"):
        return s.lower()
    return s


def _build_actual_inventory_from_positions(positions: list) -> Dict[str, Dict[str, float]]:
    """Group Polymarket Data API positions by normalized conditionId -> {yes, no} sizes."""
    actual_inventory: Dict[str, Dict[str, float]] = {}
    for p in positions:
        cid = p.get("conditionId")
        if not cid:
            continue
        key = _norm_cid(cid)
        if key is None:
            continue
        if key not in actual_inventory:
            actual_inventory[key] = {"yes": 0.0, "no": 0.0}
        outcome_idx = p.get("outcomeIndex")
        size = float(p.get("size", 0.0))
        if outcome_idx == 0 or str(p.get("outcome")).upper() == "YES":
            actual_inventory[key]["yes"] += size
        else:
            actual_inventory[key]["no"] += size
    return actual_inventory


class RiskMonitor:
    def __init__(self):
        self.reconciliation_interval = int(getattr(settings, "RECONCILIATION_INTERVAL_SEC", 60))
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
            # ONLY use actual spent capital for the hard kill switch
            actual_used_dollars = (
                float(snap.get("yes_capital_used", 0.0))
                + float(snap.get("no_capital_used", 0.0))
            )

            per_market_cap = await exposure_cap_usd_for_condition_redis_only(cid)
            if actual_used_dollars <= per_market_cap:
                continue

            # 3. Breach detected: Verify status in DB before triggering
            async with AsyncSessionLocal() as session:
                stmt = select(MarketMeta).filter(MarketMeta.condition_id == cid)
                result = await session.execute(stmt)
                market = result.scalar_one_or_none()

                if market and (market.status or "").lower() == "suspended":
                    continue

                logger.critical(
                    f"RISK BREACH (Actual Capital): Market {cid[:12]} exceeded limit (${per_market_cap:.2f})! "
                    f"actual_used_dollars: ${actual_used_dollars:.2f}"
                )
                await self.trigger_kill_switch(cid, session)

        # 4. V8.0: Per-market unrealized PnL stop-loss
        stop_loss_usd = float(getattr(settings, "PER_MARKET_STOP_LOSS_USD", 5.0))
        if stop_loss_usd > 0:
            for cid in active_cids:
                try:
                    fv_anchor = await redis_client.get_state(f"fv_anchor:{cid}")
                    if not fv_anchor or "fv_yes" not in fv_anchor:
                        continue
                    fv_yes = float(fv_anchor["fv_yes"])
                    pnl_data = await inventory_state.get_unrealized_pnl(cid, fv_yes)
                    total_unrealized = float(pnl_data.get("total_unrealized_pnl", 0.0))
                    if total_unrealized < -stop_loss_usd:
                        logger.critical(
                            f"PER-MARKET STOP-LOSS: {cid[:12]} unrealized PnL ${total_unrealized:.2f} "
                            f"< -${stop_loss_usd:.2f}. Triggering graceful_exit."
                        )
                        await redis_client.publish(f"control:{cid}", {"action": "graceful_exit"})
                except Exception as e:
                    logger.debug(f"PnL stop-loss check error for {cid[:12]}: {e}")

        # 5. Global Budget Check (all in Dollars)
        global_used_dollars = await inventory_state.get_global_used_dollars()
        global_max = float(getattr(settings, "GLOBAL_MAX_BUDGET", 280.0))
        if global_used_dollars > global_max * 1.05:
            logger.critical(
                f"GLOBAL RISK BREACH: Total used ${global_used_dollars:.2f} exceeds budget ${global_max:.2f}!"
            )
            # V8.0: Actually take action — find worst-performing market and exit it
            worst_cid = None
            worst_pnl = 0.0
            for cid in active_cids:
                try:
                    fv_anchor = await redis_client.get_state(f"fv_anchor:{cid}")
                    if not fv_anchor or "fv_yes" not in fv_anchor:
                        continue
                    pnl_data = await inventory_state.get_unrealized_pnl(cid, float(fv_anchor["fv_yes"]))
                    total_pnl = float(pnl_data.get("total_unrealized_pnl", 0.0))
                    if total_pnl < worst_pnl:
                        worst_pnl = total_pnl
                        worst_cid = cid
                except Exception:
                    pass
            if worst_cid:
                logger.critical(
                    f"GLOBAL BREACH ACTION: Exiting worst market {worst_cid[:12]} "
                    f"(unrealized PnL: ${worst_pnl:.2f})"
                )
                await redis_client.publish(f"control:{worst_cid}", {"action": "graceful_exit"})
                    
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
        """
        Periodically sync actual on-chain positions from Polymarket Data API.
        Default interval is 3600s (see RECONCILIATION_INTERVAL_SEC) to avoid hammering data-api;
        intraday risk uses in-memory inventory + User WS fills.
        """
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

    async def reconcile_single_market(self, condition_id: str, *, force: bool = False) -> bool:
        """
        REST sync for one condition_id (e.g. after Periodic Hard Reset).
        When force=True, skip RECONCILIATION_BUFFER_SECONDS guard so WS drops cannot block truth.
        """
        if not settings.FUNDER_ADDRESS:
            logger.warning("reconcile_single_market: FUNDER_ADDRESS not set; skip.")
            return False
        url = f"https://data-api.polymarket.com/positions?user={settings.FUNDER_ADDRESS}"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, timeout=15.0)
                if resp.status_code != 200:
                    logger.error(f"reconcile_single_market: positions HTTP {resp.status_code}")
                    return False
                positions = resp.json()
        except Exception as e:
            logger.error(f"reconcile_single_market: fetch failed: {e}")
            return False
        if not isinstance(positions, list):
            logger.error(f"reconcile_single_market: bad positions type {type(positions)}")
            return False

        actual_inventory = _build_actual_inventory_from_positions(positions)
        key = _norm_cid(condition_id)
        actual = actual_inventory.get(key, {"yes": 0.0, "no": 0.0}) if key else {"yes": 0.0, "no": 0.0}

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(InventoryLedger)
                .filter(InventoryLedger.market_id == condition_id)
                .with_for_update()
            )
            db_inv = result.scalar_one_or_none()
            if not db_inv:
                logger.warning(
                    "reconcile_single_market: no InventoryLedger row for %s; cannot sync",
                    condition_id[:12],
                )
                return False

            db_yes = float(db_inv.yes_exposure)
            db_no = float(db_inv.no_exposure)
            diff_yes = abs(db_yes - actual["yes"])
            diff_no = abs(db_no - actual["no"])

            if diff_yes <= self.exposure_tolerance and diff_no <= self.exposure_tolerance:
                # Memory can still be wrong (WS drops) while DB/API agree — re-push truth.
                if force:
                    await inventory_state.apply_reconciliation_snapshot(
                        market_id=condition_id,
                        yes_exposure=actual["yes"],
                        no_exposure=actual["no"],
                        yes_capital_used=float(db_inv.yes_capital_used),
                        no_capital_used=float(db_inv.no_capital_used),
                    )
                return True

            if not force:
                last_local_fill_ts = await inventory_state.get_last_local_fill_timestamp(condition_id)
                if (
                    last_local_fill_ts > 0
                    and (time.time() - last_local_fill_ts) < self.reconciliation_buffer_seconds
                ):
                    logger.info(
                        "reconcile_single_market: skipped (recent local fill, not force) %s",
                        condition_id[:12],
                    )
                    return False

            logger.warning(
                "reconcile_single_market: overwriting ledger for %s API YES=%.4f NO=%.4f (was YES=%.4f NO=%.4f) force=%s",
                condition_id[:12],
                actual["yes"],
                actual["no"],
                db_yes,
                db_no,
                force,
            )
            db_inv.yes_exposure = actual["yes"]
            db_inv.no_exposure = actual["no"]
            if actual["yes"] <= 0.001:
                db_inv.yes_capital_used = 0.0
            elif db_yes > 1e-9:
                db_inv.yes_capital_used = float(db_inv.yes_capital_used) * (actual["yes"] / db_yes)
            if actual["no"] <= 0.001:
                db_inv.no_capital_used = 0.0
            elif db_no > 1e-9:
                db_inv.no_capital_used = float(db_inv.no_capital_used) * (actual["no"] / db_no)

            await inventory_state.apply_reconciliation_snapshot(
                market_id=condition_id,
                yes_exposure=actual["yes"],
                no_exposure=actual["no"],
                yes_capital_used=float(db_inv.yes_capital_used),
                no_capital_used=float(db_inv.no_capital_used),
            )
            await session.commit()
        return True

    async def reconcile_positions(self, *, force: bool = False):
        """
        Full REST reconciliation vs Data API for all ledger rows.
        When force=True (e.g. User WS reconnect), skip RECONCILIATION_BUFFER_SECONDS so missed fills can be repaired.
        """
        if not settings.FUNDER_ADDRESS:
            logger.debug("reconcile_positions: FUNDER_ADDRESS not set; skip.")
            return

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

        actual_inventory = _build_actual_inventory_from_positions(positions)

        # 2. Compare with DB Ledger (row-level lock to prevent dirty writes from concurrent handle_fill)
        async with AsyncSessionLocal() as session:
            stmt = select(InventoryLedger).with_for_update()
            result = await session.execute(stmt)
            db_inventories = result.scalars().all()
            
            for db_inv in db_inventories:
                cid = db_inv.market_id
                key = _norm_cid(cid)
                actual = (
                    actual_inventory.get(key, {"yes": 0.0, "no": 0.0})
                    if key
                    else {"yes": 0.0, "no": 0.0}
                )
                
                db_yes = float(db_inv.yes_exposure)
                db_no = float(db_inv.no_exposure)
                
                diff_yes = abs(db_yes - actual["yes"])
                diff_no = abs(db_no - actual["no"])
                
                if diff_yes > self.exposure_tolerance or diff_no > self.exposure_tolerance:
                    last_local_fill_ts = await inventory_state.get_last_local_fill_timestamp(cid)
                    if (
                        not force
                        and last_local_fill_ts > 0
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
                        yes_capital_used=float(db_inv.yes_capital_used),
                        no_capital_used=float(db_inv.no_capital_used),
                    )
            
            await session.commit()

watchdog = RiskMonitor()
