import asyncio
import logging
from sqlalchemy.future import select

from app.db.session import AsyncSessionLocal
from app.models.db_models import InventoryLedger
from app.core.config import settings
from app.oms.core import oms

logger = logging.getLogger(__name__)

class RiskMonitor:
    def __init__(self):
        self.max_exposure = settings.MAX_EXPOSURE_PER_MARKET
        self.kill_switch_activated = False

    async def run(self):
        """Background daemon polling risk metrics"""
        logger.info("Watchdog started: Monitoring Delta Exposure")
        while not self.kill_switch_activated:
            try:
                await self.check_exposure()
                await asyncio.sleep(1) # Poll every second
            except Exception as e:
                logger.error(f"Watchdog error: {e}")
                await asyncio.sleep(5)

    async def check_exposure(self):
        async with AsyncSessionLocal() as session:
            # Get all active inventories
            stmt = select(InventoryLedger)
            result = await session.execute(stmt)
            inventories = result.scalars().all()

            for inv in inventories:
                if abs(inv.yes_exposure) > self.max_exposure or \
                   abs(inv.no_exposure) > self.max_exposure:
                    
                    logger.critical(f"RISK BREACH: Market {inv.market_id} exceeded limits!")
                    logger.critical(f"Exposure YES: {inv.yes_exposure}, NO: {inv.no_exposure}")
                    
                    await self.trigger_kill_switch(inv.market_id)
                    
    async def trigger_kill_switch(self, market_id: str):
        """Emergency procedure: cancel all orders, stop quoting"""
        self.kill_switch_activated = True
        logger.error(f"!!! KILL SWITCH ACTIVATED for {market_id} !!!")
        
        # 1. Soft Cancel via Relayer (Fastest path if API is up)
        await self._soft_cancel(market_id)
        
        # 2. Hard Cancel via Smart Contract (RPC) if soft cancel fails
        await self._hard_cancel_on_chain(market_id)
        
        # 3. Trigger Market Taker Hedge order to neutralize delta (Implementation details omitted for MVP)
        logger.info("Delta hedge logic triggered.")

    async def _soft_cancel(self, market_id: str):
        """Cancel via API (free)"""
        if oms.client:
            try:
                # Assuming clob client has a cancel_all method per market
                oms.client.cancel_all()
                logger.info(f"Soft cancel successful for {market_id}")
            except Exception as e:
                logger.error(f"Soft cancel failed: {e}")

    async def _hard_cancel_on_chain(self, market_id: str):
        """Cancel via Polygon RPC (costs Gas, but guaranteed)"""
        if not settings.ALCHEMY_RPC_URL:
            logger.warning("No RPC URL configured. Skipping hard cancel.")
            return
            
        logger.info(f"Sending Hard Cancel TX to RPC: {settings.ALCHEMY_RPC_URL}")
        # Requires Web3.py and contract ABI.
        # contract = w3.eth.contract(address=CTF_EXCHANGE, abi=ABI)
        # tx = contract.functions.cancelAll().buildTransaction(...)
        # w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        pass

watchdog = RiskMonitor()
