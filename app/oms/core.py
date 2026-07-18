import asyncio
import inspect
import logging
import uuid
from types import SimpleNamespace
from typing import Any, Dict, Optional

import httpx
from sqlalchemy import or_
from sqlalchemy.future import select

from app.models.db_models import OrderJournal, OrderStatus, OrderSide
from app.db.session import AsyncSessionLocal
from app.core.config import settings
from app.core.trading_safety import TradingMode, trading_safety
from app.risk.reservations import ReservationRejected, risk_reservations
from app.oms.validation import OrderValidationError, validate_order_intent

logger = logging.getLogger(__name__)

CANCEL_CONFIRMED = "CONFIRMED_CANCELED"
CANCEL_ALREADY_CLOSED = "ALREADY_CANCELED"
CANCEL_MATCHED_UNKNOWN = "MATCHED_UNKNOWN"
CANCEL_UNKNOWN = "UNKNOWN"


def classify_cancel_response(exchange_order_id: str, response: Any) -> str:
    """Accept a cancel only when the response proves the requested order's outcome."""
    target = str(exchange_order_id)
    if response == "Canceled":
        return CANCEL_CONFIRMED
    if not isinstance(response, dict):
        return CANCEL_UNKNOWN
    canceled = response.get("canceled")
    if isinstance(canceled, list) and target in {str(value) for value in canceled}:
        return CANCEL_CONFIRMED
    not_canceled = response.get("not_canceled")
    if not isinstance(not_canceled, dict) or target not in not_canceled:
        return CANCEL_UNKNOWN
    reason = str(not_canceled.get(target, "")).lower()
    if "already canceled" in reason or "already cancelled" in reason:
        return CANCEL_ALREADY_CLOSED
    if any(
        keyword in reason
        for keyword in ("already matched", "matched orders can't be canceled", "matched orders")
    ):
        return CANCEL_MATCHED_UNKNOWN
    return CANCEL_UNKNOWN

def _try_build_polymarket_builder_config(
    api_key: str,
    secret: str,
    passphrase: str,
) -> Optional[Any]:
    """
    Lazy-load Polymarket builder signing SDK so missing/empty package __init__ or old layouts
    never break app import. Uses a SimpleNamespace for creds (same duck-typing as BuilderApiKeyCreds).
    """
    BuilderConfig = None
    try:
        mod = __import__("py_builder_signing_sdk.config", fromlist=["BuilderConfig"])
        BuilderConfig = getattr(mod, "BuilderConfig", None)
    except Exception:
        pass
    if BuilderConfig is None:
        try:
            root = __import__("py_builder_signing_sdk", fromlist=["BuilderConfig"])
            BuilderConfig = getattr(root, "BuilderConfig", None)
        except Exception:
            BuilderConfig = None
    if BuilderConfig is None:
        logger.warning(
            "py_builder_signing_sdk has no BuilderConfig; Polymarket builder attribution disabled."
        )
        return None
    creds = SimpleNamespace(key=api_key, secret=secret, passphrase=passphrase)
    try:
        return BuilderConfig(local_builder_creds=creds)
    except Exception as e:
        logger.warning("BuilderConfig init failed; builder attribution disabled: %s", e)
        return None


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
        self.client = None
        self._client_init_lock = asyncio.Lock()
        # Importing this module must never derive credentials or touch the exchange.
        trading_safety.set_readiness(
            "oms_credentials", False, "CLOB client has not passed guarded initialization"
        )
                
        self.circuit_breaker = CircuitBreaker()

        # Long-lived HTTP client for OMS-owned REST (shared pool; avoid per-call AsyncClient).
        self.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )

    def _build_live_client(self):
        """Synchronous SDK setup, invoked only in a worker after local safety preflight."""
        from py_clob_client.client import ClobClient

        builder_config = None
        builder_api_key = str(
            getattr(settings, "POLY_BUILDER_API_KEY", "") or ""
        ).strip()
        builder_secret = str(getattr(settings, "POLY_BUILDER_SECRET", "") or "").strip()
        builder_passphrase = str(
            getattr(settings, "POLY_BUILDER_PASSPHRASE", "") or ""
        ).strip()
        if builder_api_key and builder_secret and builder_passphrase:
            builder_config = _try_build_polymarket_builder_config(
                builder_api_key, builder_secret, builder_passphrase
            )
        elif builder_api_key or builder_secret or builder_passphrase:
            logger.warning(
                "Incomplete POLY_BUILDER_* credentials; builder attribution disabled."
            )

        clob_kw: Dict[str, Any] = {
            "key": settings.PK,
            "chain_id": settings.PM_CHAIN_ID,
            "signature_type": 2,
            "funder": settings.FUNDER_ADDRESS,
        }
        if "builder_config" in inspect.signature(ClobClient.__init__).parameters:
            clob_kw["builder_config"] = builder_config
        elif builder_config is not None:
            logger.warning(
                "Installed py-clob-client has no builder_config; builder attribution disabled."
            )
        client = ClobClient(settings.PM_API_URL, **clob_kw)
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        return client

    async def initialize_live_client(self) -> bool:
        """Initialize exchange credentials only after local gates have passed."""
        async with self._client_init_lock:
            if self.client is not None:
                return True
            if trading_safety.mode is not TradingMode.LIVE:
                trading_safety.set_readiness(
                    "oms_credentials", False, "CLOB client is disabled outside live mode"
                )
                return False
            if not trading_safety.is_static_live_armed():
                trading_safety.set_readiness(
                    "oms_credentials", False, "static live arm is invalid"
                )
                return False
            if trading_safety.status()["halted"]:
                trading_safety.set_readiness(
                    "oms_credentials", False, "local preflight raised a sticky halt"
                )
                return False
            if not settings.PK or not settings.FUNDER_ADDRESS:
                trading_safety.set_readiness(
                    "oms_credentials", False, "live credentials are missing"
                )
                return False
            try:
                self.client = await asyncio.to_thread(self._build_live_client)
            except Exception:
                self.client = None
                trading_safety.set_readiness(
                    "oms_credentials", False, "CLOB client initialization failed"
                )
                trading_safety.halt("CLOB credential initialization failed")
                logger.exception("Failed to initialize guarded CLOB client")
                return False
            trading_safety.set_readiness(
                "oms_credentials", True, "guarded CLOB credential initialization completed"
            )
            logger.info("Guarded CLOB client initialization completed.")
            return True

    async def aclose(self) -> None:
        """Close pooled HTTP resources (call from app shutdown if desired)."""
        try:
            await self.http_client.aclose()
        except Exception as e:
            logger.debug("OMS http_client aclose: %s", e)

    def create_auth_headers(self, method: str, request_path: str) -> Dict[str, str]:
        """
        Build L2 HMAC-signed headers for WebSocket auth (no plaintext secret).
        Used by User Stream: GET /ws.
        """
        if not self.client or not self.client.signer or not self.client.creds:
            raise RuntimeError("ClobClient not initialized or missing signer/creds")
        from py_clob_client.clob_types import RequestArgs
        from py_clob_client.headers.headers import create_level_2_headers

        request_args = RequestArgs(method=method, request_path=request_path, body="")
        return create_level_2_headers(self.client.signer, self.client.creds, request_args)

    async def create_order(self, condition_id: str, token_id: str, side: OrderSide, price: float, size: float) -> Optional[str]:
        """Creates an order: DB Pending -> API Call -> DB Open/Failed"""

        try:
            intent = validate_order_intent(
                condition_id=condition_id,
                token_id=token_id,
                side=side,
                price=price,
                size=size,
            )
        except OrderValidationError as exc:
            logger.error("[SAFETY] Invalid order intent blocked before journaling: %s", exc)
            return None
        condition_id = intent.condition_id
        token_id = intent.token_id
        side = OrderSide(intent.side)
        price = intent.price
        size = intent.size

        mode = trading_safety.mode
        if mode is TradingMode.DISABLED:
            logger.error(
                "[SAFETY] Order blocked before journaling: TRADING_MODE=disabled "
                "condition=%s token=%s side=%s",
                condition_id[:12],
                token_id[:12],
                side.value,
            )
            return None
        if mode is TradingMode.LIVE:
            blockers = (
                trading_safety.runtime_order_blockers()
                if side == OrderSide.BUY
                else trading_safety.runtime_reduce_only_blockers()
            )
            if blockers or self.client is None:
                if self.client is None:
                    blockers = [*blockers, "ClobClient is not initialized"]
                logger.critical(
                    "[SAFETY] LIVE %s order blocked before journaling: %s",
                    side.value,
                    "; ".join(blockers),
                )
                return None

        # 1. Atomically reserve BUY capital or SELL inventory before journaling/submitting.
        order_id = f"local_{uuid.uuid4().hex}"
        reservation_id = None
        try:
            if side == OrderSide.BUY:
                grant = await risk_reservations.reserve_buy(
                    client_order_id=order_id,
                    market_id=condition_id,
                    token_id=token_id,
                    limit_price=price,
                    size=size,
                )
            else:
                grant = await risk_reservations.reserve_sell(
                    client_order_id=order_id,
                    market_id=condition_id,
                    token_id=token_id,
                    limit_price=price,
                    size=size,
                )
            reservation_id = grant.reservation_id
        except ReservationRejected as exc:
            logger.warning("[RISK] %s reservation rejected: %s", side.value, exc)
            return None
        except Exception as exc:
            trading_safety.halt(f"risk reservation service failure: {exc}")
            logger.exception("[RISK] %s reservation failed closed", side.value)
            return None

        # 2. State Machine: PENDING (Session 1)
        try:
            async with AsyncSessionLocal() as session:
                journal_entry = OrderJournal(
                    order_id=order_id,
                    exchange_order_id=None,
                    reservation_id=reservation_id,
                    market_id=condition_id,
                    side=side,
                    price=price,
                    size=size,
                    status=OrderStatus.PENDING,
                    payload={"token_id": token_id, "reservation_id": reservation_id},
                )
                session.add(journal_entry)
                await session.commit()
        except Exception:
            await risk_reservations.release(reservation_id, "JOURNAL_FAILED")
            raise
            
        # 3. API Execution via Circuit Breaker (NO DB SESSION)
        api_status = None
        api_payload = {}
        final_order_id = order_id
        
        # Paper mode never sends an exchange command.
        if mode is TradingMode.PAPER:
            logger.info(f"[DRY-RUN] Simulating execution for local order: {order_id} (PENDING)")
            await asyncio.sleep(0.5) # Simulate network delay non-blockingly
            
            # Simulated outcome
            api_status = OrderStatus.OPEN
            api_payload = {"mock_response": "Success (Dry-Run)"}
            logger.info(f"[DRY-RUN] Simulated success for order {order_id} -> OPEN")
                
        # Real Execution Mode
        else:
            try:
                from py_clob_client.clob_types import OrderArgs

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
                
                if isinstance(res, dict) and res.get("success") and res.get("orderID"):
                    api_status = OrderStatus.OPEN
                    api_payload = res
                    final_order_id = res["orderID"]
                    logger.info(f"[LIVE] Order successfully posted to CLOB: {final_order_id}")
                elif isinstance(res, dict) and res.get("success") is False:
                    # An explicit exchange rejection is the only remote outcome for which
                    # releasing the reservation is safe without an order reconciliation.
                    api_status = OrderStatus.FAILED
                    api_payload = {
                        "error": str(res.get("errorMsg", "exchange rejected order")),
                        "exchange_response": res,
                    }
                else:
                    api_status = OrderStatus.UNKNOWN
                    api_payload = {
                        "error": "ambiguous exchange submit response",
                        "exchange_response": res,
                    }
                    trading_safety.halt(
                        f"ambiguous submit outcome for local order {order_id[:24]}"
                    )
                    logger.critical(
                        "[SAFETY] Submit outcome is ambiguous for %s; reservation retained.",
                        order_id,
                    )
                    
            except Exception as e:
                # A timeout/transport exception can happen after the exchange accepted an
                # order. Never call it FAILED or release its budget without reconciliation.
                logger.exception("[LIVE] Order submit outcome is unknown: %s", e)
                api_status = OrderStatus.UNKNOWN
                api_payload = {
                    "error": str(e),
                    "status_detail": "SUBMIT_OUTCOME_UNKNOWN",
                }
                trading_safety.halt(
                    f"unknown submit outcome for local order {order_id[:24]}"
                )
                
        # 4. State Machine: OPEN / FAILED (Session 2). A remote acceptance followed by
        # local persistence failure is an UNKNOWN exposure, never an ordinary exception.
        try:
            async with AsyncSessionLocal() as session:
                # Re-fetch with row lock to avoid race with user_stream fills/cancels.
                result = await session.execute(
                    select(OrderJournal).filter_by(order_id=order_id).with_for_update()
                )
                order = result.scalar_one_or_none()
                if not order:
                    raise RuntimeError("order journal disappeared after exchange submit")

                if final_order_id != order_id:
                    # Keep the local primary key stable. The separate exchange id removes the
                    # race where a fill arrived while the primary key was being replaced.
                    order.exchange_order_id = final_order_id

                order.status = api_status
                payload = dict(order.payload) if order.payload else {}
                payload.update(api_payload)
                order.payload = payload

                await session.commit()
        except Exception as exc:
            trading_safety.halt(
                f"submit outcome could not be persisted for {order_id[:24]}"
            )
            logger.critical(
                "[SAFETY] Could not persist submit outcome for local=%s exchange=%s; "
                "reservation retained and all new risk halted: %s",
                order_id,
                final_order_id,
                exc,
            )
            try:
                await risk_reservations.mark_unknown_for_order(order_id)
            except Exception:
                logger.exception(
                    "[SAFETY] Could not mark reservation UNKNOWN after submit persistence failure"
                )
            return None

        if final_order_id != order_id and api_status == OrderStatus.OPEN:
            try:
                await risk_reservations.mark_open(reservation_id, final_order_id)
                from app.oms.fill_processor import fill_processor

                asyncio.create_task(
                    fill_processor.retry_unmapped_for_exchange_order(final_order_id)
                )
            except Exception as exc:
                trading_safety.halt(
                    f"failed to map risk reservation for order {final_order_id[:12]}: {exc}"
                )
                logger.exception("[RISK] Failed to map open reservation")
        elif api_status == OrderStatus.FAILED:
            await risk_reservations.release(reservation_id, "ORDER_FAILED")
        elif api_status == OrderStatus.UNKNOWN:
            await risk_reservations.mark_unknown_for_order(order_id)
        return final_order_id if api_status == OrderStatus.OPEN else None

    async def cancel_order(self, order_id: str):
        """Cancels an open order."""
        mode = trading_safety.mode
        if mode is TradingMode.DISABLED:
            logger.error(
                "[SAFETY] Cancel blocked: TRADING_MODE=disabled. Local order state is unchanged: %s",
                order_id,
            )
            return False

        # Paper mode changes only the local simulated journal.
        if mode is TradingMode.PAPER:
            logger.info(f"[DRY-RUN] Simulating cancel for {order_id}...")
            await asyncio.sleep(0.3)
            reservation_id = None
            async with AsyncSessionLocal() as session:
                stmt = select(OrderJournal).filter(
                    or_(
                        OrderJournal.order_id == order_id,
                        OrderJournal.exchange_order_id == order_id,
                    )
                )
                order = (await session.execute(stmt)).scalar_one_or_none()
                if order:
                    reservation_id = order.reservation_id
                    order.status = OrderStatus.CANCELED
                    logger.info(f"[DRY-RUN] Simulated cancel success for order {order_id} -> CANCELED")
                    await session.commit()
            await risk_reservations.release(reservation_id, "CANCELED")
            return True

        if not self.client or not trading_safety.can_send_exchange_cancel():
            logger.critical(
                "[SAFETY] Exchange cancel unavailable for %s (client=%s, mode=%s). "
                "Local order state is unchanged.",
                order_id,
                self.client is not None,
                mode.value,
            )
            return False

        async with AsyncSessionLocal() as session:
            stmt = select(OrderJournal).filter(
                or_(
                    OrderJournal.order_id == order_id,
                    OrderJournal.exchange_order_id == order_id,
                )
            )
            local_order = (await session.execute(stmt)).scalar_one_or_none()
            exchange_order_id = None
            if local_order:
                exchange_order_id = local_order.exchange_order_id
                if exchange_order_id is None and not local_order.order_id.startswith("local_"):
                    exchange_order_id = local_order.order_id
            elif not str(order_id).startswith("local_"):
                exchange_order_id = str(order_id)
        if not exchange_order_id:
            logger.critical(
                "[SAFETY] Cannot cancel local order %s: exchange order id is unknown.",
                order_id,
            )
            return False
            
        # Real Execution Mode
        try:
            async def _cancel():
                return await asyncio.to_thread(self.client.cancel, exchange_order_id)
            
            res = await self.circuit_breaker.execute(_cancel)

            outcome = classify_cancel_response(exchange_order_id, res)
            cancel_success = outcome in {CANCEL_CONFIRMED, CANCEL_ALREADY_CLOSED}
            already_closed = outcome == CANCEL_ALREADY_CLOSED
            matched_unknown = outcome == CANCEL_MATCHED_UNKNOWN

            if matched_unknown:
                local_order_id = None
                async with AsyncSessionLocal() as session:
                    stmt = select(OrderJournal).filter(
                        or_(
                            OrderJournal.order_id == order_id,
                            OrderJournal.exchange_order_id == exchange_order_id,
                        )
                    )
                    order = (await session.execute(stmt)).scalar_one_or_none()
                    if order:
                        local_order_id = order.order_id
                        order.status = OrderStatus.UNKNOWN
                        payload = dict(order.payload) if order.payload else {}
                        payload["cancel_response"] = res
                        payload["status_detail"] = "MATCHED_BUT_FILL_NOT_ACCOUNTED"
                        order.payload = payload
                        await session.commit()
                if local_order_id:
                    await risk_reservations.mark_unknown_for_order(local_order_id)
                trading_safety.halt(
                    f"order {str(exchange_order_id)[:12]} matched before fill accounting"
                )
                logger.critical(
                    "[SAFETY] Order %s is matched but fill accounting is not confirmed; "
                    "reservation retained and new risk halted.",
                    exchange_order_id,
                )
                return False

            if cancel_success:
                local_order_id = None
                async with AsyncSessionLocal() as session:
                    stmt = (
                        select(OrderJournal)
                        .filter(
                            or_(
                                OrderJournal.order_id == order_id,
                                OrderJournal.exchange_order_id == exchange_order_id,
                            )
                        )
                        .with_for_update()
                    )
                    order = (await session.execute(stmt)).scalar_one_or_none()
                    if order:
                        local_order_id = order.order_id
                        order.status = OrderStatus.CANCELED
                        payload = dict(order.payload) if order.payload else {}
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
                if local_order_id:
                    # A cancellation response proves the remaining order is closed, but an
                    # earlier fill event may still be delayed. Keep capital/shares reserved
                    # until authoritative order/fill/position reconciliation releases it.
                    await risk_reservations.mark_cancel_pending_for_order(local_order_id)
                    trading_safety.set_readiness(
                        "open_orders_reconciled",
                        False,
                        "canceled BUY reservations await authoritative reconciliation",
                    )
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
                OrderJournal.status.in_(
                    [OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.UNKNOWN]
                )
            )
            result = await session.execute(stmt)
            active_orders = result.scalars().all()
            
        if not active_orders:
            logger.info(f"No active orders found for {condition_id} to cancel.")
            return True
            
        tasks = []
        for order in active_orders:
            tasks.append(self.cancel_order(order.exchange_order_id or order.order_id))
            
        # Execute concurrently, wait for all
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for order, result in zip(active_orders, results):
            if isinstance(result, Exception):
                logger.error(
                    "Cancel task crashed for %s: %s",
                    order.exchange_order_id or order.order_id,
                    result,
                )
        
        success_count = sum(1 for r in results if r is True)
        failed_count = len(active_orders) - success_count
        
        if failed_count > 0:
            logger.critical(f"🚨 KILL SWITCH INCOMPLETE: {failed_count} orders failed to cancel for {condition_id}!")
            return False
            
        logger.info(f"KILL SWITCH SUCCESS: {success_count}/{len(active_orders)} orders canceled for {condition_id}")
        return True

oms = OrderManagementSystem()
