import asyncio
import logging
import time
from typing import Any, Dict, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, RequestArgs
from py_clob_client.headers.headers import create_level_2_headers
from py_builder_signing_sdk import BuilderApiKeyCreds, BuilderConfig

from app.models.db_models import OrderJournal, OrderStatus, OrderSide
from app.db.session import AsyncSessionLocal
from app.core.config import settings

logger = logging.getLogger(__name__)

# Process-wide: YES/NO engines share one wallet — avoid double cancel_all + double sleep same second.
_last_wallet_cancel_all_monotonic: float = 0.0


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
                builder_config = None
                builder_api_key = str(getattr(settings, "POLY_BUILDER_API_KEY", "") or "").strip()
                builder_secret = str(getattr(settings, "POLY_BUILDER_SECRET", "") or "").strip()
                builder_passphrase = str(getattr(settings, "POLY_BUILDER_PASSPHRASE", "") or "").strip()
                if builder_api_key and builder_secret and builder_passphrase:
                    builder_config = BuilderConfig(
                        local_builder_creds=BuilderApiKeyCreds(
                            key=builder_api_key,
                            secret=builder_secret,
                            passphrase=builder_passphrase,
                        )
                    )
                    logger.info("BuilderConfig enabled for official Polymarket volume attribution.")
                elif builder_api_key or builder_secret or builder_passphrase:
                    logger.warning(
                        "Incomplete POLY_BUILDER_* credentials; BuilderConfig disabled. "
                        "Set POLY_BUILDER_API_KEY, POLY_BUILDER_SECRET, POLY_BUILDER_PASSPHRASE together."
                    )

                # 2 is typically the signature_type for POLY_PROXY / POLYMORPHIC (proxy wallets)
                # Ensure the funder address is correct.
                self.client = ClobClient(
                    host, 
                    key=key, 
                    chain_id=chain_id, 
                    signature_type=2, # POLY_PROXY signature type for gasless transactions
                    funder=funder,
                    builder_config=builder_config,
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

    @staticmethod
    def _format_collateral_balance(bal: Any) -> str:
        """Best-effort stringify of CLOB get_balance_allowance response."""
        if bal is None:
            return "unknown"
        if isinstance(bal, dict):
            for k in ("balance", "available", "availableBalance", "allowance", "collateral"):
                v = bal.get(k)
                if v is not None and v != "":
                    return str(v)
            return str(bal)
        return str(bal)

    def _sync_clob_cancel_all_wallet(self) -> Any:
        """
        Synchronous: cancel every open order for this API key on the CLOB (ghost-order purge).
        Prefer client.cancel_all(); fall back to get_orders + cancel_orders if unavailable.
        """
        self.client.assert_level_2_auth()
        if hasattr(self.client, "cancel_all") and callable(getattr(self.client, "cancel_all")):
            return self.client.cancel_all()
        # Older py-clob-client fallback
        orders: List[dict] = self.client.get_orders() or []
        ids: List[str] = []
        for o in orders:
            if not isinstance(o, dict):
                continue
            oid = o.get("id") or o.get("orderID") or o.get("order_id")
            if oid:
                ids.append(str(oid))
        if not ids:
            return {"canceled": [], "not_canceled": {}, "note": "no open orders from get_orders"}
        if hasattr(self.client, "cancel_orders") and callable(getattr(self.client, "cancel_orders")):
            return self.client.cancel_orders(ids)
        last = None
        for oid in ids:
            last = self.client.cancel(oid)
        return last

    async def physical_clob_cancel_all_for_hard_reset(self) -> Dict[str, Any]:
        """
        V6.4 — Wallet-wide physical cancel on Polymarket CLOB, then sleep for balance release.
        Safe for the event loop: blocking HTTP runs in a thread. Never raises; logs errors.

        Returns:
            dict with keys: cancel_all_ok (Optional[bool]), usdc_balance_label (str), skipped (bool)
        """
        global _last_wallet_cancel_all_monotonic

        result: Dict[str, Any] = {
            "cancel_all_ok": None,
            "usdc_balance_label": "unknown",
            "skipped": False,
        }

        sleep_sec = float(getattr(settings, "HARD_RESET_CLOB_CANCEL_ALL_SLEEP_SEC", 3.0))
        cancel_timeout = float(getattr(settings, "HARD_RESET_CLOB_CANCEL_ALL_TIMEOUT_SEC", 45.0))
        bal_timeout = float(getattr(settings, "HARD_RESET_CLOB_BALANCE_FETCH_TIMEOUT_SEC", 20.0))
        enabled = bool(getattr(settings, "HARD_RESET_CLOB_CANCEL_ALL_ENABLED", True))

        dedup_sec = float(getattr(settings, "HARD_RESET_CLOB_WALLET_DEDUP_SEC", 15.0))
        now_m = time.monotonic()
        if (
            dedup_sec > 0
            and _last_wallet_cancel_all_monotonic > 0
            and (now_m - _last_wallet_cancel_all_monotonic) < dedup_sec
        ):
            logger.info(
                "[HARD RESET] Wallet Cancel-All dedup: another engine ran within "
                f"{dedup_sec:.0f}s — skipping repeat (ghost purge already triggered)."
            )
            result["skipped"] = True
            result["dedup"] = True
            return result

        logger.info("[HARD RESET] Initiating physical CLOB Cancel-All to clear Ghost Orders...")

        if not enabled:
            logger.info("[HARD RESET] CLOB Cancel-All disabled via HARD_RESET_CLOB_CANCEL_ALL_ENABLED=false.")
            result["skipped"] = True
            logger.info(
                f"[HARD RESET] Cancel-All skipped (disabled). Sleeping {sleep_sec:.1f}s before local cleanup..."
            )
            await asyncio.sleep(sleep_sec)
            _last_wallet_cancel_all_monotonic = time.monotonic()
            return result

        if not self.client or not self.live_trading_enabled:
            logger.info(
                "[HARD RESET] Skipping CLOB Cancel-All (dry-run or ClobClient not initialized). "
                "Sleeping before local cleanup anyway."
            )
            result["skipped"] = True
            await asyncio.sleep(sleep_sec)
            _last_wallet_cancel_all_monotonic = time.monotonic()
            return result

        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(self._sync_clob_cancel_all_wallet),
                timeout=cancel_timeout,
            )
            result["cancel_all_ok"] = True
            logger.info(f"[HARD RESET] CLOB Cancel-All completed. API response (truncated): {str(raw)[:500]}")
        except asyncio.TimeoutError:
            result["cancel_all_ok"] = False
            logger.error(
                f"[HARD RESET] CLOB Cancel-All timed out after {cancel_timeout:.1f}s — continuing with sleep."
            )
        except Exception as e:
            result["cancel_all_ok"] = False
            logger.error(f"[HARD RESET] CLOB Cancel-All failed — continuing: {e}", exc_info=True)

        logger.info(
            f"[HARD RESET] Cancel-All sent. Sleeping for {sleep_sec:.1f}s to await balance release..."
        )
        await asyncio.sleep(sleep_sec)

        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            bal = await asyncio.wait_for(
                asyncio.to_thread(self.client.get_balance_allowance, params),
                timeout=bal_timeout,
            )
            label = self._format_collateral_balance(bal)
            result["usdc_balance_label"] = label
            logger.info(f"[HARD RESET] CLOB collateral balance read: {label}")
        except asyncio.TimeoutError:
            logger.warning(
                f"[HARD RESET] get_balance_allowance timed out after {bal_timeout:.1f}s — balance unknown."
            )
        except Exception as e:
            logger.warning(f"[HARD RESET] Could not fetch USDC collateral balance: {e}")

        _last_wallet_cancel_all_monotonic = time.monotonic()
        return result

oms = OrderManagementSystem()
