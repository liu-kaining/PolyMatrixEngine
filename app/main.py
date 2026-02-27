import os
import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.config import settings
from app.db.session import init_db, get_db
from app.core.redis import redis_client
from app.market_data.gateway import md_gateway
from app.market_data.user_stream import user_stream
from app.market_data.gamma_client import gamma_client
from app.quoting.engine import start_quoting_engine
from app.risk.watchdog import watchdog
from app.models.db_models import MarketMeta, InventoryLedger, OrderJournal, OrderSide

# Force application timezone to Beijing (UTC+8) for consistent logging timestamps.
os.environ.setdefault("TZ", "Asia/Shanghai")
try:
    time.tzset()
except Exception:
    # tzset may not be available on some platforms; ignore if so.
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

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
    
    # 3. Background Services
    task_md = asyncio.create_task(md_gateway.connect())
    task_user = asyncio.create_task(user_stream.connect())
    task_watchdog = asyncio.create_task(watchdog.run())
    
    background_tasks.add(task_md)
    background_tasks.add(task_user)
    background_tasks.add(task_watchdog)
    
    yield
    
    # Shutdown Events
    logger.info("Shutting down...")
    await redis_client.disconnect()
    
    for task in background_tasks:
        task.cancel()
    
app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)

# --- API Endpoints ---

@app.get("/health")
async def health_check():
    return {"status": "ok", "version": "0.1.0"}

@app.post("/markets/{condition_id}/start")
async def start_market_making(condition_id: str, db: AsyncSession = Depends(get_db)):
    """Add market to engine and start quoting"""
    
    # Check if market exists in DB
    result = await db.execute(select(MarketMeta).filter(MarketMeta.condition_id == condition_id))
    market = result.scalar_one_or_none()
    
    # Always refresh token IDs from Gamma to ensure we use current CLOB tokens.
    tokens = await gamma_client.get_market_tokens_by_condition_id(condition_id)
    if not tokens and (not market or not market.yes_token_id or not market.no_token_id):
        raise HTTPException(status_code=404, detail="Market tokens not found in Polymarket Gamma API")
    
    if tokens:
        yes_token_id, no_token_id = tokens
        if not market:
            market = MarketMeta(
                condition_id=condition_id,
                status="active",
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
            )
            new_inventory = InventoryLedger(market_id=condition_id)
            db.add(market)
            db.add(new_inventory)
        else:
            market.yes_token_id = yes_token_id
            market.no_token_id = no_token_id
        await db.commit()
        
    # Pre-flight Check: USDC Balance (best-effort, only when LIVE_TRADING_ENABLED=True and SDK exposes it)
    MIN_REQUIRED_USDC = 50.0
    from app.oms.core import oms
    if oms.client and settings.LIVE_TRADING_ENABLED:
        try:
            if hasattr(oms.client, "get_balance"):
                balance = float(oms.client.get_balance())
                if balance < MIN_REQUIRED_USDC:
                    logger.error(f"Insufficient USDC balance: {balance} < {MIN_REQUIRED_USDC}")
                    raise HTTPException(
                        status_code=400,
                        detail=f"Insufficient funds. Required: {MIN_REQUIRED_USDC} USDC, Available: {balance} USDC",
                    )
                logger.info(f"Pre-flight check passed. USDC Balance: {balance}")
            else:
                logger.warning("ClobClient does not expose balance API; skipping capital pre-flight check.")
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"Could not verify USDC balance during pre-flight (might be offline/dry-run): {e}")
    
    # Subscribe to WS with token IDs (asset_ids)
    await md_gateway.subscribe([market.yes_token_id, market.no_token_id])
    
    # Subscribe to private User Stream (uses condition_id)
    await user_stream.subscribe(condition_id)
    
    # Start Quoting Engine Daemon FIRST so they subscribe to Redis PubSub
    # before we publish the initial snapshot tick.
    task_quoting_yes = asyncio.create_task(start_quoting_engine(condition_id, market.yes_token_id))
    task_quoting_no = asyncio.create_task(start_quoting_engine(condition_id, market.no_token_id))
    
    background_tasks.add(task_quoting_yes)
    background_tasks.add(task_quoting_no)
    
    # Give engines a moment to complete their Redis PubSub subscribe.
    # asyncio.create_task schedules them but they need one event-loop turn
    # to actually reach `await pubsub.subscribe(...)` inside engine.run().
    await asyncio.sleep(0.5)
    
    # NOW seed the local orderbook from REST and publish the initial tick.
    # The engines are already listening so they will receive this immediately.
    await md_gateway.fetch_initial_snapshot(market.yes_token_id)
    await md_gateway.fetch_initial_snapshot(market.no_token_id)

    logger.info(
        f"Market making started for {condition_id[:10]}... "
        f"YES={market.yes_token_id[:10]}... NO={market.no_token_id[:10]}..."
    )
    
    return {
        "status": "started", 
        "condition_id": condition_id,
        "tokens": {
            "YES": market.yes_token_id,
            "NO": market.no_token_id
        }
    }

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
