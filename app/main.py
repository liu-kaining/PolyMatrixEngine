import os
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import delete

from app.core.config import settings
from app.db.session import init_db, get_db, AsyncSessionLocal
from app.core.redis import redis_client
from app.core.inventory_state import inventory_state
from app.market_data.gateway import md_gateway
from app.market_data.user_stream import user_stream
from app.market_data.gamma_client import gamma_client
from app.quoting.engine import start_quoting_engine
from app.risk.watchdog import watchdog
from app.core.market_lifecycle import start_market_making_impl, stop_all_markets
from app.models.db_models import MarketMeta, InventoryLedger, OrderJournal, OrderSide, OrderStatus
from logging.handlers import RotatingFileHandler

# Force application timezone to Beijing (UTC+8) for consistent logging timestamps.
os.environ.setdefault("TZ", "Asia/Shanghai")
try:
    time.tzset()
except Exception:
    # tzset may not be available on some platforms; ignore if so.
    pass

# --- Logging configuration: console + rotating file ---
log_format = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

# Configure root logger to at least INFO and keep console handler
logging.basicConfig(level=logging.INFO, format=log_format)
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Determine log file path (can be overridden via TRADING_LOG_PATH)
env_log_path = os.getenv("TRADING_LOG_PATH")
if env_log_path:
    log_path = env_log_path
    log_dir = os.path.dirname(log_path) or "."
else:
    base_dir = os.path.dirname(os.path.dirname(__file__))
    log_dir = os.path.join(base_dir, "data", "logs")
    log_path = os.path.join(log_dir, "trading.log")

os.makedirs(log_dir, exist_ok=True)

file_handler = RotatingFileHandler(
    log_path,
    maxBytes=5 * 1024 * 1024,  # 5 MB
    backupCount=3,
    encoding="utf-8",
)
file_handler.setFormatter(logging.Formatter(log_format))
logger.addHandler(file_handler)

# Reduce noise from SQLAlchemy internals; focus logs on business events.
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy.pool").setLevel(logging.WARNING)

# Application state for background tasks
background_tasks = set()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup Events
    logger.info(f"Starting {settings.PROJECT_NAME}")
    
    # 1. DB Initialization
    await init_db()
    
    # 2. Redis Connection
    await redis_client.connect()

    # 2.5 In-memory inventory state manager
    await inventory_state.start()

    # 2.6 Sweep historical PENDING ghost orders before starting background services.
    from app.oms.core import oms
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(OrderJournal).filter(OrderJournal.status == OrderStatus.PENDING)
        )
        pending_orders = result.scalars().all()
        if pending_orders:
            logger.warning(f"扫地僧启动，准备清理 {len(pending_orders)} 条历史遗留的 PENDING 幽灵订单...")
        for order in pending_orders:
            try:
                await oms.cancel_order(order.order_id)
            except Exception as e:
                logger.warning(f"扫地僧在尝试撤销历史 PENDING 订单 {order.order_id} 时出错: {e}")
            order.status = OrderStatus.FAILED
            logger.warning(f"扫地僧已清理历史遗留的 PENDING 幽灵订单: {order.order_id}")
        if pending_orders:
            await session.commit()

    # 3. Background Services
    task_md = asyncio.create_task(md_gateway.connect())
    task_user = asyncio.create_task(user_stream.connect())
    task_watchdog = asyncio.create_task(watchdog.run())
    
    background_tasks.add(task_md)
    background_tasks.add(task_user)
    background_tasks.add(task_watchdog)

    if getattr(settings, "AUTO_ROUTER_ENABLED", False):
        from app.core.auto_router import run as auto_router_run
        task_router = asyncio.create_task(auto_router_run())
        background_tasks.add(task_router)
        logger.info("Auto-Router (Portfolio Manager) started.")
    
    yield
    
    # Shutdown Events
    logger.info("Shutting down...")
    
    # 1. Cancel background network tasks (Stops Router, Market Gateway, User Stream, Watchdog)
    # This prevents new markets from starting and new stream data from arriving.
    for task in background_tasks:
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)
    
    # 2. Stop all running market engines safely (Wait for pubsub close & cancellation)
    await stop_all_markets()
    
    # 3. Drain inventory queue safely to DB before connection closes
    await inventory_state.stop()
    
    # 4. Disconnect Redis safely
    await redis_client.disconnect()
    
app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)

# --- API Endpoints ---

@app.get("/health")
async def health_check():
    health_data = {"status": "ok", "version": "0.1.0"}
    if getattr(settings, "AUTO_ROUTER_ENABLED", False):
        try:
            from app.core.auto_router import router_state
            health_data["auto_router"] = router_state
        except ImportError:
            pass
    return health_data

@app.post("/markets/{condition_id}/start")
async def start_market_making(condition_id: str):
    """Add market to engine and start quoting (shared impl with Auto-Router)."""
    logger.info(f"POST /markets/{condition_id[:12]}.../start received")
    try:
        result = await start_market_making_impl(condition_id)
        return result
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)

@app.post("/markets/{condition_id}/stop")
async def stop_market_making(condition_id: str, db: AsyncSession = Depends(get_db)):
    """Soft stop: Cancel all orders and suspend quoting engine for this market"""
    from app.oms.core import oms
    
    # Send pub/sub message to tell engine to halt immediately
    await redis_client.publish(f"control:{condition_id}", {"action": "suspend"})
    logger.info(f"Published suspend signal for {condition_id}")
    
    # Soft Cancel via Relayer (Cancel all active orders for this market)
    await oms.cancel_market_orders(condition_id)
    
    # Update DB status
    result = await db.execute(select(MarketMeta).filter(MarketMeta.condition_id == condition_id))
    market = result.scalar_one_or_none()
    if market:
        market.status = "suspended"
        await db.commit()
        
    return {"status": "stopped", "condition_id": condition_id}

@app.post("/markets/{condition_id}/liquidate")
async def liquidate_market(condition_id: str, db: AsyncSession = Depends(get_db)):
    """Liquidate all positions: Cancel orders and market dump (Cross the spread)"""
    from app.oms.core import oms
    
    # 1. Immediate Suspension and Soft Cancel
    await redis_client.publish(f"control:{condition_id}", {"action": "suspend"})
    await oms.cancel_market_orders(condition_id)
    
    # Update DB status
    result = await db.execute(select(MarketMeta).filter(MarketMeta.condition_id == condition_id))
    market = result.scalar_one_or_none()
    if market:
        market.status = "suspended"
        
    # 2. Get current inventory
    result_inv = await db.execute(select(InventoryLedger).filter(InventoryLedger.market_id == condition_id))
    inv = result_inv.scalar_one_or_none()
    
    if not inv or not market:
        await db.commit()
        return {"status": "liquidated (no inventory)", "condition_id": condition_id}
        
    # 3. Liquidate Yes/No Exposure by crossing the spread (Taker)
    yes_exp = float(inv.yes_exposure)
    no_exp = float(inv.no_exposure)
    
    # We construct market orders (Taker). For CLOB, we place limit orders deep into the book to guarantee execution.
    tasks = []
    
    # Selling Yes exposure (If we hold YES, we SELL YES at $0.01 to guarantee fill)
    if yes_exp > 0:
        logger.warning(f"Liquidating {yes_exp} YES exposure for {condition_id}")
        tasks.append(oms.create_order(
            condition_id=condition_id, 
            token_id=market.yes_token_id, 
            side=OrderSide.SELL, 
            price=0.01, # Floor price to match any bid
            size=yes_exp
        ))
    
    # Selling No exposure (If we hold NO, we SELL NO at $0.01 to guarantee fill)
    if no_exp > 0:
        logger.warning(f"Liquidating {no_exp} NO exposure for {condition_id}")
        tasks.append(oms.create_order(
            condition_id=condition_id, 
            token_id=market.no_token_id, 
            side=OrderSide.SELL, 
            price=0.01, # Floor price to match any bid
            size=no_exp
        ))
        
    if tasks:
        # We don't wait for fills here; they will be handled by the user stream and update the DB automatically.
        await asyncio.gather(*tasks, return_exceptions=True)
    
    await db.commit()
    return {"status": "liquidating", "condition_id": condition_id, "yes_liquidated": yes_exp, "no_liquidated": no_exp}

@app.get("/markets/{condition_id}/risk")
async def get_market_risk(condition_id: str, db: AsyncSession = Depends(get_db)):
    """View current inventory and delta"""
    result = await db.execute(select(InventoryLedger).filter(InventoryLedger.market_id == condition_id))
    inventory = result.scalar_one_or_none()
    
    if not inventory:
        raise HTTPException(status_code=404, detail="Market inventory not found")
        
    return {
        "market_id": condition_id,
        "yes_exposure": float(inventory.yes_exposure),
        "no_exposure": float(inventory.no_exposure),
        "realized_pnl": float(inventory.realized_pnl)
    }

@app.get("/markets/status")
async def get_markets_status(
    condition_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Lightweight observability endpoint for Dashboard:
    - Unified fair values (FV_yes / FV_no) from Redis anchor
    - Per-side engine mode from Redis runtime keys
    - Fallback derived mode from DB exposures if runtime key is absent
    """
    stmt = (
        select(InventoryLedger, MarketMeta)
        .outerjoin(MarketMeta, InventoryLedger.market_id == MarketMeta.condition_id)
    )
    if condition_id:
        stmt = stmt.filter(InventoryLedger.market_id == condition_id)

    rows = (await db.execute(stmt)).all()

    base_size = max(5.0, float(getattr(settings, "BASE_ORDER_SIZE", 10.0)))
    liquidate_threshold = base_size * 2.0

    def _dust_filter(e: float) -> float:
        return 0.0 if abs(e) < 1.0 else e

    def derive_mode(own_exp: float, opp_exp: float, market_status: str) -> str:
        if market_status == "suspended":
            return "SUSPENDED"
        own_exp = _dust_filter(own_exp)
        opp_exp = _dust_filter(opp_exp)
        if own_exp >= liquidate_threshold:
            return "LIQUIDATING"
        if opp_exp >= liquidate_threshold:
            return "LOCKED_BY_OPPOSITE"
        return "QUOTING"

    markets = []
    for inv, market in rows:
        cid = inv.market_id
        market_status = ((market.status if market else None) or "unknown").lower()

        yes_exposure = float(inv.yes_exposure or 0.0)
        no_exposure = float(inv.no_exposure or 0.0)

        anchor = await redis_client.get_state(f"fv_anchor:{cid}") or {}
        fv_yes = None
        fv_no = None
        if "fv_yes" in anchor:
            try:
                fv_yes = max(0.01, min(0.99, float(anchor["fv_yes"])))
                fv_no = max(0.01, min(0.99, 1.0 - fv_yes))
            except Exception:
                fv_yes = None
                fv_no = None

        yes_runtime = await redis_client.get_state(f"engine_state:{cid}:YES") or {}
        no_runtime = await redis_client.get_state(f"engine_state:{cid}:NO") or {}

        yes_mode = yes_runtime.get("mode") or derive_mode(yes_exposure, no_exposure, market_status)
        no_mode = no_runtime.get("mode") or derive_mode(no_exposure, yes_exposure, market_status)

        rewards_data = await redis_client.get_state(f"rewards:{cid}") or {}
        r_min_size = rewards_data.get("rewards_min_size")
        r_max_spread = rewards_data.get("rewards_max_spread")
        r_rate = rewards_data.get("reward_rate_per_day")

        markets.append(
            {
                "condition_id": cid,
                "market_status": market_status,
                "fv_yes": fv_yes,
                "fv_no": fv_no,
                "fv_sum": (fv_yes + fv_no) if (fv_yes is not None and fv_no is not None) else None,
                "yes_exposure": yes_exposure,
                "no_exposure": no_exposure,
                "yes_mode": yes_mode,
                "no_mode": no_mode,
                "yes_runtime": yes_runtime,
                "no_runtime": no_runtime,
                "rewards_min_size": r_min_size,
                "rewards_max_spread": r_max_spread,
                "reward_rate_per_day": r_rate,
            }
        )

    return {
        "markets": markets,
        "base_order_size": base_size,
        "liquidate_threshold": liquidate_threshold,
    }

@app.get("/orders/active")
async def get_active_orders(db: AsyncSession = Depends(get_db)):
    """List all pending/open orders"""
    result = await db.execute(select(OrderJournal).filter(OrderJournal.status.in_(["PENDING", "OPEN"])))
    orders = result.scalars().all()
    
    return [
        {
            "id": o.order_id,
            "market_id": o.market_id,
            "side": o.side,
            "price": float(o.price),
            "size": float(o.size),
            "status": o.status
        } for o in orders
    ]


@app.post("/admin/wipe")
async def wipe_all_data(db: AsyncSession = Depends(get_db)):
    """
    DANGER: Wipe all local state (Postgres + Redis) for a clean reset.
    Intended for development / manual recovery only.
    """
    # 1. Wipe Postgres tables in safe order (children first).
    await db.execute(delete(OrderJournal))
    await db.execute(delete(InventoryLedger))
    await db.execute(delete(MarketMeta))
    await db.commit()
    await inventory_state.clear()

    # 2. Wipe Redis database (orderbooks, ticks, pubsub state cache).
    try:
        if redis_client.client is not None:
            await redis_client.client.flushdb()
            logger.warning("Redis DB flushed as part of admin wipe.")
    except Exception as e:
        logger.warning(f"Failed to flush Redis during admin wipe: {e}")

    logger.critical("ADMIN WIPE executed: all local DB and Redis state cleared.")
    return {"status": "wiped"}
