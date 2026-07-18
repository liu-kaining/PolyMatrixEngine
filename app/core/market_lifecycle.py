"""
Internal market start implementation shared by POST /markets/{id}/start and Auto-Router.
Raises ValueError on failure (caller may convert to HTTPException for API).
"""
import asyncio
import logging
import math
from collections import defaultdict
from typing import Any, Dict, Set, Tuple

from sqlalchemy.future import select

from app.db.session import AsyncSessionLocal
from app.core.redis import redis_client
from app.core.trading_safety import TradingMode, trading_safety
from app.market_data.gateway import md_gateway
from app.market_data.user_stream import user_stream
from app.market_data.gamma_client import gamma_client
from app.quoting.engine import start_quoting_engine
from app.models.db_models import MarketMeta, InventoryLedger

logger = logging.getLogger(__name__)

MIN_REQUIRED_USDC = 20.0

# --- Engine Supervisor State ---
# Maps (condition_id, token_id) -> asyncio.Task
engine_tasks: Dict[Tuple[str, str], asyncio.Task] = {}
# Per-market startup lock to prevent race conditions (e.g., API & Router starting same market)
market_start_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

def get_active_router_markets() -> Set[str]:
    """Returns condition_ids that have at least one active engine task."""
    return {cid for (cid, tid), task in engine_tasks.items() if not task.done()}

async def stop_all_markets():
    """Confirm wallet-side cancels for every running market, then stop engine tasks."""
    logger.info(f"EngineSupervisor shutting down {len(engine_tasks)} active tasks...")
    active_cids = sorted(get_active_router_markets())
    if active_cids:
        from app.oms.core import oms

        for condition_id in active_cids:
            cancel_safe = await oms.cancel_market_orders(condition_id)
            if cancel_safe is not True:
                trading_safety.halt(
                    f"shutdown could not confirm cancels for {condition_id[:12]}"
                )
                logger.critical(
                    "Shutdown cancellation incomplete for market %s", condition_id[:12]
                )
    for task in engine_tasks.values():
        task.cancel()
    if engine_tasks:
        await asyncio.gather(*engine_tasks.values(), return_exceptions=True)
    engine_tasks.clear()


async def stop_market_tasks(condition_id: str) -> int:
    """Terminate every engine task for one condition and remove it from the supervisor."""
    selected = [
        task
        for (cid, _token_id), task in list(engine_tasks.items())
        if cid == condition_id
    ]
    for task in selected:
        task.cancel()
    if selected:
        await asyncio.gather(*selected, return_exceptions=True)
    for key in [key for key in list(engine_tasks) if key[0] == condition_id]:
        engine_tasks.pop(key, None)
    return len(selected)

async def _mark_market_exited(condition_id: str):
    """Update DB status when all engines for a market have successfully terminated."""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(MarketMeta).filter(MarketMeta.condition_id == condition_id))
            market = result.scalar_one_or_none()
            if market and market.status == "active":
                market.status = "exited"
                await session.commit()
                logger.info(f"[Supervisor] Market {condition_id[:10]} all engines terminated. Status -> exited.")
    except Exception as e:
        logger.exception(
            "[Supervisor] Failed to update DB status for %s (market may show stale 'active'): %s",
            condition_id[:10], e,
        )

async def start_market_making_impl(condition_id: str) -> Dict[str, Any]:
    """
    Start quoting for a market safely. Managed by EngineSupervisor.
    Raises ValueError on failure.
    """
    trading_safety.assert_engine_start_allowed()

    async with market_start_locks[condition_id]:
        # 1. Prevent duplicate starts
        active = get_active_router_markets()
        if condition_id in active:
            logger.info(f"[Supervisor] Market {condition_id[:10]} already running. Skipping start.")
            return {"status": "already_running", "condition_id": condition_id}

        # Perform network IO BEFORE acquiring DB session lock to prevent connection pool exhaustion
        gamma_info = await gamma_client.get_market_info(condition_id)
        if trading_safety.mode is TradingMode.LIVE and gamma_info is None:
            raise ValueError(
                "Live start rejected: authoritative market metadata is unavailable"
            )
        if gamma_info and int(getattr(gamma_info, "outcome_count", 2) or 2) != 2:
            raise ValueError(
                "Safety gate rejects categorical markets: only outcome_count == 2 is supported"
            )

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(MarketMeta).filter(MarketMeta.condition_id == condition_id))
            market = result.scalar_one_or_none()

            if not gamma_info and (not market or not market.yes_token_id or not market.no_token_id):
                raise ValueError("Market tokens not found in Polymarket Gamma API")

            if gamma_info:
                if market and (
                    (market.yes_token_id and market.yes_token_id != gamma_info.yes_token_id)
                    or (market.no_token_id and market.no_token_id != gamma_info.no_token_id)
                ):
                    raise ValueError(
                        "Market token mapping changed; offline inventory/order review is required"
                    )
                if not market:
                    market = MarketMeta(
                        condition_id=condition_id,
                        status="active",
                        yes_token_id=gamma_info.yes_token_id,
                        no_token_id=gamma_info.no_token_id,
                        rewards_min_size=gamma_info.rewards_min_size,
                        rewards_max_spread=gamma_info.rewards_max_spread,
                        reward_rate_per_day=gamma_info.reward_rate_per_day,
                    )
                    new_inventory = InventoryLedger(market_id=condition_id)
                    session.add(market)
                    session.add(new_inventory)
                else:
                    market.status = "active"  # Explicitly reset status in case it was suspended
                    market.yes_token_id = gamma_info.yes_token_id
                    market.no_token_id = gamma_info.no_token_id
                    market.rewards_min_size = gamma_info.rewards_min_size
                    market.rewards_max_spread = gamma_info.rewards_max_spread
                    market.reward_rate_per_day = gamma_info.reward_rate_per_day
                await session.commit()

                rewards_payload = {
                    "rewards_min_size": gamma_info.rewards_min_size,
                    "rewards_max_spread": gamma_info.rewards_max_spread,
                    "reward_rate_per_day": gamma_info.reward_rate_per_day,
                    "outcome_count": int(getattr(gamma_info, "outcome_count", 2) or 2),
                }
                await redis_client.set_state(f"rewards:{condition_id}", rewards_payload)
            else:
                market.status = "active"
                await session.commit()

            yes_token_id = str(market.yes_token_id)
            no_token_id = str(market.no_token_id)
            # No database connection is held during balance, stream, or snapshot I/O below.
            await session.close()

            from app.oms.core import oms
            if trading_safety.mode is TradingMode.LIVE:
                if oms.client is None:
                    raise ValueError("Live pre-flight failed: ClobClient is not initialized")
                try:
                    if hasattr(oms.client, "get_balance"):
                        balance = float(await asyncio.to_thread(oms.client.get_balance))
                        if not math.isfinite(balance) or balance < MIN_REQUIRED_USDC:
                            raise ValueError(
                                f"Insufficient funds. Required: {MIN_REQUIRED_USDC} USDC, Available: {balance} USDC"
                            )
                        logger.info(f"Pre-flight check passed. USDC Balance: {balance}")
                    else:
                        raise ValueError("Live pre-flight failed: balance check is unavailable")
                except ValueError:
                    raise
                except Exception as e:
                    raise ValueError(f"Live pre-flight balance verification failed: {e}") from e

            await md_gateway.subscribe([yes_token_id, no_token_id])
            await user_stream.subscribe(condition_id)

            # Seed books before engines exist. Invalid/missing snapshots remain blocked by
            # market-data readiness and the engines' per-tick integrity checks.
            await md_gateway.fetch_initial_snapshot(yes_token_id)
            await md_gateway.fetch_initial_snapshot(no_token_id)

            # 2. Start Tasks and attach lifecycle cleanup
            def _cleanup(task: asyncio.Task, cid: str, tid: str):
                engine_tasks.pop((cid, tid), None)
                try:
                    exc = task.exception()
                    if exc:
                        logger.exception(f"[Supervisor] Engine crashed for {cid[:8]}/{tid[:8]}", exc_info=exc)
                except asyncio.CancelledError:
                    pass
                
                # If no more engines are running for this condition_id, mark as exited in DB
                still_running = any(t_cid == cid for (t_cid, t_tid) in engine_tasks.keys())
                if not still_running:
                    asyncio.create_task(_mark_market_exited(cid))

            for token_id in (yes_token_id, no_token_id):
                task = asyncio.create_task(start_quoting_engine(condition_id, token_id))
                task.add_done_callback(lambda t, c=condition_id, tk=token_id: _cleanup(t, c, tk))
                engine_tasks[(condition_id, token_id)] = task

            logger.info(
                f"[Supervisor] Market making started for {condition_id[:10]}... "
                f"YES={yes_token_id[:10]}... NO={no_token_id[:10]}..."
            )

            return {
                "status": "started",
                "condition_id": condition_id,
                "tokens": {
                    "YES": yes_token_id,
                    "NO": no_token_id,
                },
            }
