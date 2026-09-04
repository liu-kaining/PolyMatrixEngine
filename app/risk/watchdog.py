import asyncio
import logging
import math
import time
import httpx
from sqlalchemy.future import select

from app.db.session import AsyncSessionLocal
from app.models.db_models import InventoryLedger, MarketMeta
from app.core.config import settings
from app.core.exposure_limits import exposure_cap_usd_for_condition_redis_only
from app.oms.core import oms
from app.core.redis import redis_client
from app.core.inventory_state import inventory_state
from app.core.trading_safety import TradingMode, trading_safety
from app.core.position_reconciliation import (
    build_actual_inventory_from_positions,
    normalize_condition_id,
    reconcile_capital_used,
)
from app.risk.reservations import risk_reservations

logger = logging.getLogger(__name__)


def authenticated_balance_required(
    *,
    local_yes: float,
    local_no: float,
    discovered_yes: float,
    discovered_no: float,
    active: bool,
    tolerance: float,
) -> bool:
    """Select tokens whose live quantity must come from authenticated CLOB facts."""
    values = tuple(float(value) for value in (local_yes, local_no, discovered_yes, discovered_no))
    return bool(
        active
        or any(not math.isfinite(value) for value in values)
        or any(abs(value) > float(tolerance) for value in values)
    )


def risk_limit_breached(used: float, limit: float) -> bool:
    """Treat non-finite/negative risk state or a non-positive cap as a breach."""
    used_value = float(used)
    limit_value = float(limit)
    return bool(
        not math.isfinite(used_value)
        or not math.isfinite(limit_value)
        or used_value < 0
        or limit_value <= 0
        or used_value > limit_value
    )


class RiskMonitor:
    def __init__(self):
        self.reconciliation_interval = max(
            60, int(getattr(settings, "RECONCILIATION_INTERVAL_SEC", 60))
        )
        self.exposure_tolerance = settings.EXPOSURE_TOLERANCE
        self.reconciliation_buffer_seconds = float(
            getattr(settings, "RECONCILIATION_BUFFER_SECONDS", 8.0)
        )
        self._global_kill_triggered = False
        self._position_reconciliation_lock = asyncio.Lock()

    async def run(self):
        """Background daemon polling risk metrics and reconciling"""
        logger.info("Watchdog started: Monitoring Delta Exposure & Reconciliation")
        trading_safety.set_readiness("risk_monitor", False, "risk monitor is starting")

        # In live mode the authenticated user-stream task owns the periodic
        # order+position reconciliation cycle so both facts advance together.
        # Paper retains the standalone public comparison loop.
        reconciliation_task = (
            None
            if trading_safety.mode is TradingMode.LIVE
            else asyncio.create_task(self.reconciliation_loop())
        )
        try:
            while True:
                try:
                    await self.check_exposure()
                    trading_safety.set_readiness(
                        "risk_monitor", True, "risk monitor completed its latest cycle"
                    )
                    await asyncio.sleep(1)  # Poll every second
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    trading_safety.set_readiness(
                        "risk_monitor", False, "risk monitor cycle failed"
                    )
                    trading_safety.halt(f"risk monitor cycle failed: {e}")
                    logger.exception("Watchdog cycle failed closed: %s", e)
                    await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass
        finally:
            trading_safety.set_readiness("risk_monitor", False, "risk monitor stopped")
            if reconciliation_task is not None:
                reconciliation_task.cancel()
                await asyncio.gather(reconciliation_task, return_exceptions=True)

    async def check_exposure(self):
        """
        Real-time risk check based on in-memory state.
        This ensures immediate kill-switch activation on fill, without waiting for DB persistence.
        """
        # 1. Get all active condition_ids from the EngineSupervisor
        from app.core.market_lifecycle import get_active_router_markets
        active_cids = get_active_router_markets()
        known_cids = await inventory_state.get_all_market_ids()
        global_reserved, reserved_by_market = await risk_reservations.totals()

        all_cids = known_cids.union(active_cids).union(reserved_by_market)
        for cid in all_cids:
            capital_used = await inventory_state.get_market_capital_used(cid)
            risk_used_dollars = capital_used + float(reserved_by_market.get(cid, 0.0))

            per_market_cap = await exposure_cap_usd_for_condition_redis_only(cid)
            if not risk_limit_breached(risk_used_dollars, per_market_cap):
                continue

            # 3. Breach detected: hold a session for the suspend transaction.
            async with AsyncSessionLocal() as session:
                logger.critical(
                    f"RISK BREACH: Market {cid[:12]} exceeded limit (${per_market_cap:.2f})! "
                    f"capital_plus_reservations: ${risk_used_dollars:.2f}"
                )
                if cid in active_cids:
                    await self.trigger_kill_switch(cid, session)
                else:
                    trading_safety.halt(
                        f"inactive/cold market {cid[:12]} exceeds its risk cap"
                    )
                    logger.critical(
                        "Cold-position risk breach for %s; no engine is active, new risk halted.",
                        cid[:12],
                    )

        # 4. Global Budget Check (all in Dollars)
        global_used_dollars = (
            await inventory_state.get_global_capital_used()
        ) + global_reserved
        global_max = float(getattr(settings, "GLOBAL_MAX_BUDGET", 280.0))
        if risk_limit_breached(global_used_dollars, global_max):
            logger.critical(
                f"GLOBAL RISK BREACH: Total used ${global_used_dollars:.2f} exceeds budget ${global_max:.2f}!"
            )
            if not self._global_kill_triggered:
                self._global_kill_triggered = True
                try:
                    self._global_kill_triggered = await self.trigger_global_kill_switch(
                        all_cids, active_cids
                    )
                except Exception:
                    # Retry the cancellation sweep on the next watchdog cycle.
                    self._global_kill_triggered = False
                    raise
                    
    async def trigger_kill_switch(self, condition_id: str, session):
        """Emergency procedure: cancel all orders, suspend quoting"""
        trading_safety.halt(f"market risk cap breached for {condition_id[:12]}")
        logger.error(f"!!! KILL SWITCH ACTIVATED for {condition_id} !!!")
        
        # 1. Suspend Quoting (Communicate to QuotingEngine via DB and Redis)
        stmt = select(MarketMeta).filter(MarketMeta.condition_id == condition_id)
        result = await session.execute(stmt)
        market = result.scalar_one_or_none()
        
        if market and market.status != "suspended":
            market.status = "suspended"
            await session.commit()

        # The database may already say suspended while a stale/restarted engine is still
        # running. Always publish the immediate control signal for an active breach.
        await redis_client.publish(f"control:{condition_id}", {"action": "suspend"})
        logger.info(f"Published suspend signal for {condition_id}")
        
        # 2. Soft Cancel via Relayer (Cancel all active orders for this market)
        cancel_safe = await oms.cancel_market_orders(condition_id)
        if cancel_safe is not True:
            trading_safety.halt(
                f"market kill switch could not confirm every cancel for {condition_id[:12]}"
            )
            logger.critical(
                "Market kill switch cancellation incomplete for %s", condition_id[:12]
            )

    async def trigger_global_kill_switch(self, target_cids, active_cids) -> bool:
        """Block new risk globally, suspend every engine and cancel every known active order."""
        trading_safety.halt("GLOBAL_MAX_BUDGET breached")
        results = {}
        for condition_id in sorted(target_cids):
            try:
                if condition_id in active_cids:
                    await redis_client.publish(
                        f"control:{condition_id}", {"action": "suspend"}
                    )
                results[condition_id] = await oms.cancel_market_orders(condition_id)
            except Exception as exc:
                results[condition_id] = False
                logger.exception(
                    "Global kill failed while suspending/canceling %s: %s",
                    condition_id[:12],
                    exc,
                )
        if target_cids:
            async with AsyncSessionLocal() as session:
                rows = (
                    await session.execute(
                        select(MarketMeta).filter(MarketMeta.condition_id.in_(target_cids))
                    )
                ).scalars().all()
                for market in rows:
                    market.status = "suspended"
                await session.commit()
        failed = [cid for cid, success in results.items() if success is not True]
        if failed:
            logger.critical(
                "GLOBAL KILL INCOMPLETE for %d market(s): %s",
                len(failed),
                ", ".join(cid[:12] for cid in failed),
            )
        else:
            logger.critical("GLOBAL KILL completed for %d active market(s).", len(results))
        return not failed

    async def reconciliation_loop(self):
        """
        Periodically reconcile positions against authenticated conditional-token balances.

        The public Data API is used only to discover previously unknown conditions
        and, when its size agrees, as best-effort cost metadata.
        Default interval is 300s (see RECONCILIATION_INTERVAL_SEC);
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

    async def reconcile_positions(self):
        """Serialize startup, reconnect and periodic authoritative passes."""
        async with self._position_reconciliation_lock:
            return await self._reconcile_positions_once()

    async def _reconcile_positions_once(self):
        """
        Reconcile known tokens against authenticated CLOB balances in live mode.

        The public Data API is retained only to discover conditions absent from the
        local ledger and to provide best-effort cost metadata. It is not authoritative
        enough to overwrite known positions because it can lag recent fills.
        """
        if not settings.FUNDER_ADDRESS:
            logger.debug("reconcile_positions: FUNDER_ADDRESS not set; skip.")
            trading_safety.set_readiness(
                "positions_reconciled", False, "FUNDER_ADDRESS is not configured"
            )
            return False

        trading_safety.set_readiness(
            "positions_reconciled", False, "full position reconciliation in progress"
        )

        # 1. Fetch public position metadata for unknown-condition discovery.
        url = f"https://data-api.polymarket.com/positions?user={settings.FUNDER_ADDRESS}"
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10.0)
            if resp.status_code != 200:
                logger.error(f"Failed to fetch positions. Status: {resp.status_code}")
                trading_safety.set_readiness(
                    "positions_reconciled", False, "positions API returned a non-200 status"
                )
                return False
                
            positions = resp.json()
            
        if not isinstance(positions, list):
            logger.error(f"Unexpected positions format: {type(positions)}")
            trading_safety.set_readiness(
                "positions_reconciled", False, "positions API response format is invalid"
            )
            return False

        data_api_inventory = build_actual_inventory_from_positions(positions)

        authoritative_inventory = {}
        if trading_safety.mode is TradingMode.LIVE:
            if oms.client is None:
                trading_safety.set_readiness(
                    "positions_reconciled",
                    False,
                    "authenticated CLOB client is unavailable for token balances",
                )
                return False
            async with AsyncSessionLocal() as session:
                tracked_rows = (
                    await session.execute(
                        select(InventoryLedger, MarketMeta).join(
                            MarketMeta, InventoryLedger.market_id == MarketMeta.condition_id
                        )
                    )
                ).all()

            from app.core.market_lifecycle import get_active_router_markets

            active_keys = {
                key
                for condition_id in get_active_router_markets()
                if (key := normalize_condition_id(condition_id))
            }
            candidates = []
            for inventory, market in tracked_rows:
                key = normalize_condition_id(inventory.market_id)
                data_actual = data_api_inventory.get(key or "", {})
                # Query active markets even if both local and public snapshots say
                # zero. This catches an externally acquired token when the public
                # Data API is itself lagging.
                if key and authenticated_balance_required(
                    local_yes=float(inventory.yes_exposure or 0.0),
                    local_no=float(inventory.no_exposure or 0.0),
                    discovered_yes=float(data_actual.get("yes", 0.0)),
                    discovered_no=float(data_actual.get("no", 0.0)),
                    active=key in active_keys,
                    tolerance=self.exposure_tolerance,
                ):
                    candidates.append((key, market.yes_token_id, market.no_token_id))

            async def fetch_pair(key: str, yes_token: str, no_token: str):
                if not yes_token or not no_token:
                    raise RuntimeError(f"market {key[:12]} has incomplete token mapping")
                yes_balance, no_balance = await asyncio.gather(
                    oms.client.get_token_balance(yes_token),
                    oms.client.get_token_balance(no_token),
                )
                return key, yes_balance, no_balance

            try:
                pairs = await asyncio.gather(
                    *(fetch_pair(*candidate) for candidate in candidates)
                )
            except Exception:
                trading_safety.set_readiness(
                    "positions_reconciled",
                    False,
                    "authenticated conditional-token balance query failed",
                )
                logger.exception("Authenticated conditional-token balance query failed")
                return False
            for key, yes_balance, no_balance in pairs:
                reported = data_api_inventory.get(key, {})
                data_yes = float(reported.get("yes", 0.0))
                data_no = float(reported.get("no", 0.0))
                authoritative_inventory[key] = {
                    "yes": yes_balance,
                    "no": no_balance,
                    "yes_cost": (
                        float(reported.get("yes_cost", 0.0))
                        if abs(data_yes - yes_balance) <= self.exposure_tolerance
                        else 0.0
                    ),
                    "no_cost": (
                        float(reported.get("no_cost", 0.0))
                        if abs(data_no - no_balance) <= self.exposure_tolerance
                        else 0.0
                    ),
                }

        # 2. Compare with DB Ledger (row-level lock to prevent dirty writes from concurrent handle_fill)
        async with AsyncSessionLocal() as session:
            stmt = select(InventoryLedger).with_for_update()
            result = await session.execute(stmt)
            db_inventories = result.scalars().all()
            reconciliation_safe = True
            db_condition_keys = {
                normalize_condition_id(inv.market_id)
                for inv in db_inventories
                if normalize_condition_id(inv.market_id)
            }
            unknown_external = {
                key: value
                for key, value in data_api_inventory.items()
                if key not in db_condition_keys
                and (value["yes"] > 0.001 or value["no"] > 0.001)
            }
            if unknown_external:
                reconciliation_safe = False
                trading_safety.halt(
                    f"{len(unknown_external)} external positions have no local ledger"
                )
                logger.critical(
                    "[SAFETY] External positions without local ledgers: %s",
                    ", ".join(key[:12] for key in sorted(unknown_external)),
                )
            
            for db_inv in db_inventories:
                cid = db_inv.market_id
                key = normalize_condition_id(cid)
                empty_actual = {
                    "yes": 0.0,
                    "no": 0.0,
                    "yes_cost": 0.0,
                    "no_cost": 0.0,
                }
                actual = (
                    authoritative_inventory.get(
                        key, data_api_inventory.get(key, empty_actual)
                    )
                    if key
                    else empty_actual
                )
                
                db_yes = float(db_inv.yes_exposure)
                db_no = float(db_inv.no_exposure)
                
                diff_yes = abs(db_yes - actual["yes"])
                diff_no = abs(db_no - actual["no"])
                
                capital_missing = (
                    (actual["yes"] > 0.001 and float(db_inv.yes_capital_used or 0.0) <= 0)
                    or (actual["no"] > 0.001 and float(db_inv.no_capital_used or 0.0) <= 0)
                )
                if (
                    diff_yes > self.exposure_tolerance
                    or diff_no > self.exposure_tolerance
                    or capital_missing
                ):
                    last_local_fill_ts = await inventory_state.get_last_local_fill_timestamp(cid)
                    if last_local_fill_ts > 0 and (
                        time.time() - last_local_fill_ts
                    ) < self.reconciliation_buffer_seconds:
                        logger.info(
                            "本地刚刚发生真实成交，等待认证余额更新，跳过本次对账"
                        )
                        logger.info(
                            f"Skipped reconcile overwrite for {cid[:8]} "
                            f"(age={time.time() - last_local_fill_ts:.2f}s < "
                            f"buffer={self.reconciliation_buffer_seconds:.2f}s)"
                        )
                        reconciliation_safe = False
                        continue

                    logger.error(f"RECONCILIATION MISMATCH for {cid[:8]}!")
                    logger.error(f"DB -> YES: {db_yes:.2f}, NO: {db_no:.2f}")
                    logger.error(
                        f"Authoritative -> YES: {actual['yes']:.2f}, "
                        f"NO: {actual['no']:.2f}"
                    )
                    
                    db_inv.yes_exposure = actual["yes"]
                    db_inv.no_exposure = actual["no"]
                    db_inv.state_version = int(db_inv.state_version or 0) + 1

                    yes_capital, _ = reconcile_capital_used(
                        actual_size=actual["yes"],
                        reported_cost=actual["yes_cost"],
                        previous_size=db_yes,
                        previous_capital_used=float(db_inv.yes_capital_used or 0.0),
                    )
                    no_capital, _ = reconcile_capital_used(
                        actual_size=actual["no"],
                        reported_cost=actual["no_cost"],
                        previous_size=db_no,
                        previous_capital_used=float(db_inv.no_capital_used or 0.0),
                    )
                    db_inv.yes_capital_used = yes_capital
                    db_inv.no_capital_used = no_capital
                    reconciliation_safe = False
                    db_inv.accounting_version = "unverified_external"
                    trading_safety.halt(
                        f"external position mismatch invalidated accounting for {cid[:12]}"
                    )

                    logger.info(f"Local ledger overwritten with on-chain data for {cid[:8]}")

                    # Keep in-memory state aligned with DB overwrite.
                    await inventory_state.apply_reconciliation_snapshot(
                        market_id=cid,
                        yes_exposure=actual["yes"],
                        no_exposure=actual["no"],
                        yes_capital_used=float(db_inv.yes_capital_used),
                        no_capital_used=float(db_inv.no_capital_used),
                        state_version=int(db_inv.state_version or 0),
                    )
            
            await session.commit()
        trading_safety.set_readiness(
            "positions_reconciled",
            reconciliation_safe,
            (
                "full position reconciliation completed"
                if reconciliation_safe
                else "reconciliation found untracked positions requiring offline recovery"
            ),
        )
        return reconciliation_safe

watchdog = RiskMonitor()
