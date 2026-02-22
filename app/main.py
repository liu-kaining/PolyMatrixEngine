import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.config import settings
from app.db.session import init_db, get_db
from app.core.redis import redis_client
from app.market_data.gateway import md_gateway
from app.market_data.gamma_client import gamma_client
from app.quoting.engine import start_quoting_engine
from app.risk.watchdog import watchdog
from app.models.db_models import MarketMeta, InventoryLedger, OrderJournal

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

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
    task_watchdog = asyncio.create_task(watchdog.run())
    
    background_tasks.add(task_md)
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
    
    if not market or not market.yes_token_id or not market.no_token_id:
        # Fetch tokens from Gamma API
        tokens = await gamma_client.get_market_tokens_by_condition_id(condition_id)
        if not tokens:
            raise HTTPException(status_code=404, detail="Market tokens not found in Polymarket Gamma API")
            
        yes_token_id, no_token_id = tokens
        
        if not market:
            market = MarketMeta(
                condition_id=condition_id, 
                status="active",
                yes_token_id=yes_token_id,
                no_token_id=no_token_id
            )
            new_inventory = InventoryLedger(market_id=condition_id)
            db.add(market)
            db.add(new_inventory)
        else:
            market.yes_token_id = yes_token_id
            market.no_token_id = no_token_id
            
        await db.commit()
    
    # Subscribe to WS with token IDs (asset_ids)
    await md_gateway.subscribe([market.yes_token_id, market.no_token_id])
    
    # Start Quoting Engine Daemon for both YES and NO tokens
    task_quoting_yes = asyncio.create_task(start_quoting_engine(condition_id, market.yes_token_id))
    task_quoting_no = asyncio.create_task(start_quoting_engine(condition_id, market.no_token_id))
    
    background_tasks.add(task_quoting_yes)
    background_tasks.add(task_quoting_no)
    
    return {
        "status": "started", 
        "condition_id": condition_id,
        "tokens": {
            "YES": market.yes_token_id,
            "NO": market.no_token_id
        }
    }

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
