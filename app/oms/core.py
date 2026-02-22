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
        # LIVE_TRADING_ENABLED checks if we want to actually push to the Polymarket network
        self.live_trading_enabled = settings.LIVE_TRADING_ENABLED
        
        if key and funder:
            try:
                # 2 is typically the signature_type for POLY_PROXY / POLYMORPHIC (proxy wallets)
                # Ensure the funder address is correct.
                self.client = ClobClient(
                    host, 
                    key=key, 
                    chain_id=chain_id, 
                    signature_type=2, # POLY_PROXY signature type for gasless transactions
                    funder=funder
                )
                
                # Derive or set API creds (standard for proxy wallets in py-clob-client)
                creds = self.client.create_or_derive_api_creds()
                self.client.set_api_creds(creds)
                
                logger.info(f"ClobClient initialized. Live Trading Enabled: {self.live_trading_enabled}")
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
        
        # Test Mode (Dry-Run) or missing client
        if not self.client or not self.live_trading_enabled:
            logger.info(f"[DRY-RUN] Simulating execution for local order: {order_id} (PENDING)")
            await asyncio.sleep(0.5) # Simulate network delay non-blockingly
            
            # Simulated outcome
            api_status = OrderStatus.OPEN
            api_payload = {"mock_response": "Success (Dry-Run)"}
            logger.info(f"[DRY-RUN] Simulated success for order {order_id} -> OPEN")
                
        # Real Execution Mode
        else:
            try:
                async def _place_order():
                    # Format args for py-clob-client
                    order_args = OrderArgs(
                        price=price,
                        size=size,
                        side="BUY" if side == OrderSide.BUY else "SELL",
                        token_id=token_id, # Must be the accurate Token ID
                    )
                    # We use create_and_post_order for automatic signing and API dispatch
                    return self.client.create_and_post_order(order_args)
                
                # Wrapped in circuit breaker to handle rate limits (429) or gateway errors (502)
                res = await self.circuit_breaker.execute(_place_order)
                
                if res and res.get("success") and res.get("orderID"):
                    api_status = OrderStatus.OPEN
                    api_payload = res
                    final_order_id = res["orderID"]
                    logger.info(f"[LIVE] Order successfully posted to CLOB: {final_order_id}")
                else:
                    error_msg = res.get("errorMsg", "Unknown API Error") if res else "No response"
                    raise Exception(f"Failed to get orderID. Response: {error_msg}")
                    
            except Exception as e:
                # 4. State Machine: FAILED
                logger.error(f"[LIVE] Order failed: {e}")
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
        # Test Mode (Dry-Run) or missing client
        if not self.client or not self.live_trading_enabled:
            logger.info(f"[DRY-RUN] Simulating cancel for {order_id}...")
            await asyncio.sleep(0.3)
            
            async with AsyncSessionLocal() as session:
                order = await session.get(OrderJournal, order_id)
                if order:
                    order.status = OrderStatus.CANCELED
                    logger.info(f"[DRY-RUN] Simulated cancel success for order {order_id} -> CANCELED")
                    await session.commit()
            return True
            
        # Real Execution Mode
        try:
            async def _cancel():
                # Real API cancel call using Polymarket Client
                return self.client.cancel(order_id)
            
            res = await self.circuit_breaker.execute(_cancel)
            
            if res == "Canceled" or (isinstance(res, dict) and res.get("success", False) is True):
                async with AsyncSessionLocal() as session:
                    order = await session.get(OrderJournal, order_id)
                    if order:
                        order.status = OrderStatus.CANCELED
                        payload = dict(order.payload) if order.payload else {}
                        
                        # Handle Dusting: Verify if there was any partial fill immediately prior to cancel
                        try:
                            # We check the API directly for size_matched
                            # Note: self.client methods are typically synchronous HTTP calls. 
                            # Wrapping in to_thread to prevent event loop blocking during HFT load.
                            order_info = await asyncio.to_thread(self.client.get_order, order_id)
                            
                            if isinstance(order_info, dict):
                                size_matched = float(order_info.get("size_matched", 0.0))
                                payload["filled_size_api_check"] = size_matched
                                
                                # If API indicates it matched before being canceled, note it.
                                # The user_stream WebSocket is the primary source of truth, but this is a fail-safe.
                                if size_matched > 0 and size_matched < float(order.size):
                                    logger.warning(f"[{order_id}] Cancelled, but API shows Partial Fill ({size_matched}/{order.size})")
                                    payload["status_detail"] = "PARTIALLY_FILLED_AND_CANCELED"
                                    # We don't forcefully overwrite filled_size here because user_stream 
                                    # maintains the atomic ledger. We just keep the audit trail.
                        except Exception as fetch_e:
                            logger.debug(f"Could not fetch final order status for {order_id} to check dust: {fetch_e}")

                        payload["cancel_response"] = res
                        order.payload = payload
                        logger.info(f"[LIVE] Order successfully canceled on CLOB: {order_id}")
                        await session.commit()
                return True
            else:
                raise Exception(f"Cancel failed or unrecognized response format: {res}")
                
        except Exception as e:
            logger.error(f"[LIVE] Failed to cancel order {order_id}: {e}")
            return False

    async def cancel_market_orders(self, condition_id: str):
        """Emergency cancel all OPEN/PENDING orders for a specific market"""
        logger.warning(f"Initiating TRUE KILL SWITCH (Cancel All) for {condition_id}")
        async with AsyncSessionLocal() as session:
            stmt = select(OrderJournal).filter(
                OrderJournal.market_id == condition_id,
                OrderJournal.status.in_([OrderStatus.PENDING, OrderStatus.OPEN])
            )
            result = await session.execute(stmt)
            active_orders = result.scalars().all()
            
        if not active_orders:
            logger.info(f"No active orders found for {condition_id} to cancel.")
            return True
            
        tasks = []
        for order in active_orders:
            tasks.append(self.cancel_order(order.order_id))
            
        # Execute concurrently, wait for all
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        success_count = sum(1 for r in results if r is True)
        failed_count = len(active_orders) - success_count
        
        if failed_count > 0:
            logger.critical(f"🚨 KILL SWITCH INCOMPLETE: {failed_count} orders failed to cancel for {condition_id}!")
            return False
            
        logger.info(f"KILL SWITCH SUCCESS: {success_count}/{len(active_orders)} orders canceled for {condition_id}")
        return True

oms = OrderManagementSystem()
