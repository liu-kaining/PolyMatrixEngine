import asyncio
import logging
from typing import Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, RequestArgs
from py_clob_client.headers.headers import create_level_2_headers

from app.models.db_models import OrderJournal, OrderStatus, OrderSide
from app.db.session import AsyncSessionLocal
from app.core.config import settings

logger = logging.getLogger(__name__)

def _is_non_transient_error(e: Exception) -> bool:
    """403 geoblock / 400 balance: retrying won't help; don't count toward circuit breaker."""
    sc = getattr(e, "status_code", None)
    if sc in (403, 400):
        return True
    s = str(e).lower()
    if "status_code=403" in s or "status_code=400" in s:
        return True
    return False


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
            if not _is_non_transient_error(e):
                self.record_failure()
            else:
                logger.debug(f"CircuitBreaker: skipping failure count for non-transient error: {e}")
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

    def create_auth_headers(self, method: str, request_path: str) -> Dict[str, str]:
        """
        Build L2 HMAC-signed headers for WebSocket auth (no plaintext secret).
        Used by User Stream: GET /ws.
        """
        if not self.client or not self.client.signer or not self.client.creds:
            raise RuntimeError("ClobClient not initialized or missing signer/creds")
        request_args = RequestArgs(method=method, request_path=request_path, body="")
        return create_level_2_headers(self.client.signer, self.client.creds, request_args)

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
                order_args = OrderArgs(
                    price=price,
                    size=size,
                    side="BUY" if side == OrderSide.BUY else "SELL",
                    token_id=token_id,
                )

                async def _place_order():
                    # py-clob-client methods are synchronous HTTP; offload to thread
                    # so we don't block the async event loop during signing + POST.
                    return await asyncio.to_thread(
                        self.client.create_and_post_order, order_args
                    )
                
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
            # Re-fetch with row lock to avoid race with user_stream fills/cancels.
            result = await session.execute(
                select(OrderJournal).filter_by(order_id=order_id).with_for_update()
            )
            order = result.scalar_one_or_none()
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
                return await asyncio.to_thread(self.client.cancel, order_id)
            
            res = await self.circuit_breaker.execute(_cancel)

            # Normalize different cancel response formats from the CLOB API.
            cancel_success = False
            already_closed = False

            if res == "Canceled":
                cancel_success = True
            elif isinstance(res, dict):
                # Newer API: {'not_canceled': {...}, 'canceled': [...]} or similar.
                canceled_list = res.get("canceled") or []
                not_canceled = res.get("not_canceled") or {}

                # If our order_id is in the canceled list (or any were canceled at all),
                # we consider this a successful cancel.
                if order_id in canceled_list or (canceled_list and not not_canceled):
                    cancel_success = True
                # If the API says "already canceled", "already matched", or "matched orders
                # can't be canceled", the order is no longer active — treat as success.
                elif isinstance(not_canceled, dict) and order_id in not_canceled:
                    reason = str(not_canceled.get(order_id, "")).lower()
                    if any(kw in reason for kw in (
                        "already canceled",
                        "already matched",
                        "matched orders can't be canceled",
                        "matched orders",
                    )):
                        cancel_success = True
                        already_closed = True
                # Legacy success flag
                elif res.get("success", False) is True:
                    cancel_success = True

            if cancel_success:
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
                        if already_closed:
                            payload["status_detail"] = payload.get("status_detail") or ""
                            payload["status_detail"] += "|ALREADY_CLOSED_ON_CLOB"
                        order.payload = payload
                        if already_closed:
                            logger.info(f"[LIVE] Order {order_id} already closed on CLOB; marking as CANCELED locally.")
                        else:
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
