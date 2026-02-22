import asyncio
import logging
from typing import Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, FilterParams

from app.models.db_models import OrderJournal, OrderStatus, OrderSide
from app.db.session import AsyncSessionLocal
from app.core.config import settings

logger = logging.getLogger(__name__)

class CircuitBreaker:
    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failures = 0
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self.last_failure_time = 0.0

    async def execute(self, func, *args, **kwargs):
        if self.state == "OPEN":
            if (asyncio.get_event_loop().time() - self.last_failure_time) > self.recovery_timeout:
                self.state = "HALF_OPEN"
                logger.info("CircuitBreaker: HALF_OPEN")
            else:
                logger.warning("CircuitBreaker is OPEN. Blocking request.")
                raise Exception("Circuit breaker is OPEN")

        try:
            result = await func(*args, **kwargs)
            if self.state == "HALF_OPEN":
                self.reset()
            return result
        except Exception as e:
            self.record_failure()
            raise e

    def record_failure(self):
        self.failures += 1
        self.last_failure_time = asyncio.get_event_loop().time()
        logger.error(f"CircuitBreaker failure: {self.failures}/{self.failure_threshold}")
        if self.failures >= self.failure_threshold:
            self.state = "OPEN"
            logger.critical("CircuitBreaker: OPEN. Stop routing requests.")

    def reset(self):
        self.failures = 0
        self.state = "CLOSED"
        logger.info("CircuitBreaker: CLOSED")

class OrderManagementSystem:
    def __init__(self):
        # py-clob-client initialization (Note: requires actual PK/Funder for live usage)
        host = settings.PM_API_URL
        key = settings.PK
        chain_id = settings.PM_CHAIN_ID
        funder = settings.FUNDER_ADDRESS
        
        self.client = None
        if key and funder:
            try:
                self.client = ClobClient(host, key=key, chain_id=chain_id, funder=funder)
                logger.info("ClobClient initialized.")
            except Exception as e:
                logger.error(f"Failed to init ClobClient: {e}")
                
        self.circuit_breaker = CircuitBreaker()

    async def create_order(self, condition_id: str, token_id: str, side: OrderSide, price: float, size: float) -> Optional[str]:
        """Creates an order: DB Pending -> API Call -> DB Open/Failed"""
        
        # 1. State Machine: PENDING (Session 1)
        order_id = f"local_{token_id}_{side}_{asyncio.get_event_loop().time()}" # Temp ID until polymarket returns one
        async with AsyncSessionLocal() as session:
            journal_entry = OrderJournal(
                order_id=order_id,
                market_id=condition_id,  # Storing condition_id for foreign key relation
                side=side,
                price=price,
                size=size,
                status=OrderStatus.PENDING,
                payload={"token_id": token_id} # Stash the token_id in payload for context
            )
            session.add(journal_entry)
            await session.commit()
            
        # 2. API Execution via Circuit Breaker (NO DB SESSION)
        api_status = None
        api_payload = {}
        final_order_id = order_id
        
        if not self.client:
            # Mock execution path
            logger.info(f"No ClobClient. Simulating execution for local order: {order_id} (PENDING)")
            
            # Simulate network delay non-blockingly outside DB Session
            await asyncio.sleep(0.5)
            
            import random
            if random.random() < 0.1:  # 10% chance to fail
                logger.warning(f"Simulated network/API failure for order {order_id}")
                api_status = OrderStatus.FAILED
                api_payload = {"error": "Simulated random API failure"}
            else:
                logger.info(f"Simulated success for order {order_id} -> OPEN")
                api_status = OrderStatus.OPEN
                api_payload = {"mock_response": "Success"}
                
        else:
            try:
                async def _place_order():
                    # Format args for py-clob-client
                    order_args = OrderArgs(
                        price=price,
                        size=size,
                        side="BUY" if side == OrderSide.BUY else "SELL",
                        token_id=token_id, # Must be the accurate Token ID, not Condition ID!
                    )
                    return self.client.create_and_post_order(order_args)
                
                res = await self.circuit_breaker.execute(_place_order)
                
                if res and res.get("orderID"):
                    api_status = OrderStatus.OPEN
                    api_payload = res
                    final_order_id = res["orderID"]
                else:
                    raise Exception(f"Failed to get orderID from response: {res}")
                    
            except Exception as e:
                # 4. State Machine: FAILED
                logger.error(f"Order failed: {e}")
                api_status = OrderStatus.FAILED
                api_payload = {"error": str(e)}

        # 3. State Machine: OPEN / FAILED (Session 2)
        async with AsyncSessionLocal() as session:
            # Re-fetch the order to ensure it hasn't been modified externally
            order = await session.get(OrderJournal, order_id)
            if not order:
                return None
                
            if final_order_id != order_id:
                # If API returned a real order_id, update it
                order.order_id = final_order_id
                
            order.status = api_status
            payload = dict(order.payload) if order.payload else {}
            payload.update(api_payload)
            order.payload = payload
                
            await session.commit()
            return final_order_id if api_status == OrderStatus.OPEN else None

    async def cancel_order(self, order_id: str):
        """Cancels an open order."""
        if not self.client:
            logger.info(f"No ClobClient. Simulating cancel for {order_id}...")
            # Simulate network delay non-blockingly
            await asyncio.sleep(0.3)
            
            async with AsyncSessionLocal() as session:
                order = await session.get(OrderJournal, order_id)
                if order:
                    order.status = OrderStatus.CANCELED
                    logger.info(f"Simulated cancel success for order {order_id} -> CANCELED")
                    await session.commit()
            return True
            
        try:
            async def _cancel():
                return self.client.cancel(order_id)
            
            res = await self.circuit_breaker.execute(_cancel)
            
            async with AsyncSessionLocal() as session:
                order = await session.get(OrderJournal, order_id)
                if order:
                    order.status = OrderStatus.CANCELED
                    order.payload = {"cancel_response": res}
                    await session.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

oms = OrderManagementSystem()
