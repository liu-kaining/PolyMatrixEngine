import os
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Optional

from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import delete

from app.core.config import settings
from app.db.session import init_db, get_db, AsyncSessionLocal
from app.core.redis import redis_client
from app.core.inventory_state import inventory_state
from app.core.admin_auth import require_admin
from app.core.accounting_integrity import accounting_integrity_service
from app.core.alpha_evidence import refresh_alpha_evidence_readiness
from app.core.execution_lease import live_execution_lease
from app.core.geographic_eligibility import (
    fetch_geographic_eligibility,
    monitor_geographic_eligibility,
)
from app.core.safety_state import (
    acknowledge_persistent_halt,
    persist_halt,
    restore_persistent_halt,
)
from app.core.trading_safety import (
    SafetyInterlockError,
    TradingMode,
    trading_safety,
)
from app.market_data.gateway import md_gateway
from app.market_data.user_stream import user_stream
from app.risk.watchdog import watchdog
from app.risk.reservations import risk_reservations
from app.oms.order_reconciliation import order_reconciliation_service
from app.core.market_lifecycle import (
    get_active_router_markets,
    start_market_making_impl,
    stop_all_markets,
    stop_market_tasks,
)
from app.models.db_models import (
    AccountingAuditRun,
    ExchangeOrderSnapshot,
    ExecutionLease,
    FillCashLedger,
    FillEvent,
    InventoryLedger,
    MarketMeta,
    OrderJournal,
    OrderReconciliationRun,
    OrderStatus,
    PortfolioRiskState,
    RiskReservation,
)
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
    logger.info(
        "Starting %s with TRADING_MODE=%s",
        settings.PROJECT_NAME,
        trading_safety.mode.value,
    )
    
    # 1. DB Initialization
    await init_db()
    trading_safety.set_readiness("database", True, "database initialization completed")
    await restore_persistent_halt()
    
    # 2. Redis Connection
    await redis_client.connect()
    trading_safety.set_readiness("redis", True, "Redis ping succeeded")

    # 2.5 In-memory inventory state manager
    await inventory_state.start()
    await inventory_state.load_all()

    # 2.6 Inventory/accounting preflight. Exchange order facts are reconciled below only
    # in a valid requested live process; disabled/paper startup performs no exchange I/O.
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(OrderJournal).filter(
                OrderJournal.status.in_([OrderStatus.PENDING, OrderStatus.UNKNOWN])
            )
        )
        unresolved_orders = result.scalars().all()
        if unresolved_orders:
            logger.warning(
                "[SAFETY] Found %d PENDING/UNKNOWN orders; authoritative reconciliation "
                "must classify them before new live risk is allowed.",
                len(unresolved_orders),
            )
        legacy_ledgers = (
            await session.execute(
                select(InventoryLedger.market_id).filter(
                    InventoryLedger.accounting_version != "v2"
                )
            )
        ).scalars().all()
        if legacy_ledgers:
            trading_safety.halt(
                f"{len(legacy_ledgers)} inventory ledgers require a v2 offline accounting rebuild"
            )
            trading_safety.set_readiness(
                "positions_reconciled",
                False,
                "legacy v1 PnL ledgers require offline rebuild",
            )
            logger.critical(
                "[SAFETY] Found %d legacy v1 accounting ledgers. New live orders remain blocked.",
                len(legacy_ledgers),
            )

    # 2.7 Local-only deterministic accounting replay. This performs no exchange
    # request and blocks live risk on legacy ledgers, missing fills/cash facts,
    # unknown fees or any non-fill inventory mutation.
    try:
        await accounting_integrity_service.audit()
    except Exception:
        logger.critical(
            "[SAFETY] Accounting integrity audit failed; process remains halted."
        )

    # 2.8 Local-only strategy evidence verification. A boolean is not proof:
    # report hash, sample coverage, fee completeness, out-of-sample checks and
    # positive lower confidence bounds must all pass.
    refresh_alpha_evidence_readiness()

    if trading_safety.mode is TradingMode.LIVE:
        from app.oms.core import oms

        preflight_ok = trading_safety.is_static_live_armed() and not trading_safety.status()[
            "halted"
        ]
        if preflight_ok:
            try:
                lease_ok = await live_execution_lease.acquire(settings.FUNDER_ADDRESS)
            except Exception as exc:
                lease_ok = False
                trading_safety.set_readiness(
                    "executor_lease", False, "wallet lease acquisition failed"
                )
                trading_safety.halt(f"wallet lease acquisition failed: {exc}")
                logger.exception("Live execution lease acquisition failed")
            if lease_ok:
                live_execution_lease.start_renewal()
            else:
                trading_safety.halt("another process may own the live execution wallet")
            preflight_ok = lease_ok and not trading_safety.status()["halted"]

        if preflight_ok:
            try:
                eligibility = await fetch_geographic_eligibility()
                if eligibility.blocked:
                    trading_safety.set_readiness(
                        "geographic_eligibility",
                        False,
                        f"exchange reports blocked jurisdiction country={eligibility.country}",
                    )
                    trading_safety.halt("exchange geographic eligibility check is blocked")
                else:
                    trading_safety.set_readiness(
                        "geographic_eligibility",
                        True,
                        f"exchange eligibility passed for country={eligibility.country}",
                    )
            except Exception as exc:
                trading_safety.set_readiness(
                    "geographic_eligibility", False, "geographic eligibility check failed"
                )
                trading_safety.halt(f"geographic eligibility check failed: {exc}")
                logger.exception("Geographic eligibility preflight failed")
            preflight_ok = not trading_safety.status()["halted"]

        if preflight_ok:
            await oms.initialize_live_client()
        if oms.client is not None:
            try:
                await order_reconciliation_service.reconcile(oms.client)
            except Exception:
                # The service already records a sticky halt and failed readiness. Keep the
                # control plane alive so operators can inspect state and attempt cancellation.
                logger.critical(
                    "[SAFETY] Startup order reconciliation failed; process remains halted."
                )
        else:
            trading_safety.set_readiness(
                "open_orders_reconciled",
                False,
                "live arm/client unavailable for authoritative reconciliation",
            )
    else:
        trading_safety.set_readiness(
            "executor_lease", True, "live wallet lease is not required outside live mode"
        )
        trading_safety.set_readiness(
            "geographic_eligibility",
            True,
            "geographic execution check is not required outside live mode",
        )
        trading_safety.set_readiness(
            "open_orders_reconciled",
            True,
            "exchange order reconciliation is not required outside live mode",
        )

    # Rebuild cached reservation totals only after order reconciliation has had the
    # opportunity to close confirmed cancel-pending reservations.
    await risk_reservations.rebuild_and_validate()

    # 3. Background Services
    trading_services_allowed = trading_safety.mode is not TradingMode.DISABLED
    if trading_safety.mode is TradingMode.LIVE and not trading_safety.is_static_live_armed():
        trading_services_allowed = False
        trading_safety.halt("invalid or expired static live arm")
        logger.critical(
            "[SAFETY] Live mode requested but static arm is invalid. Network trading services were not started: %s",
            "; ".join(trading_safety.static_live_errors()),
        )
    if trading_safety.mode is TradingMode.LIVE and trading_safety.status()["halted"]:
        trading_services_allowed = False
        logger.critical(
            "[SAFETY] Live trading services were not started because a sticky halt is active."
        )

    if trading_services_allowed:
        task_md = asyncio.create_task(md_gateway.connect())
        task_watchdog = asyncio.create_task(watchdog.run())
        background_tasks.add(task_md)
        background_tasks.add(task_watchdog)

        # The private user stream is needed only for deliberately armed live execution.
        if trading_safety.mode is TradingMode.LIVE:
            task_user = asyncio.create_task(user_stream.connect())
            background_tasks.add(task_user)
            task_geo = asyncio.create_task(monitor_geographic_eligibility())
            background_tasks.add(task_geo)

        if getattr(settings, "AUTO_ROUTER_ENABLED", False):
            try:
                trading_safety.assert_router_start_allowed()
            except SafetyInterlockError as e:
                logger.critical("[SAFETY] Auto-Router blocked: %s", e)
            else:
                from app.core.auto_router import run as auto_router_run

                task_router = asyncio.create_task(auto_router_run())
                background_tasks.add(task_router)
                logger.info("Auto-Router (Portfolio Manager) started.")
    else:
        logger.warning(
            "[SAFETY] Trading background services are disabled; control plane remains available."
        )
    
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
    
    # 3. Stop the DB-authoritative inventory cache.
    await inventory_state.stop()

    # 3.5 Close OMS httpx connection pool (avoids FD / connector leak on reload/shutdown)
    from app.oms.core import oms
    await oms.aclose()
    await live_execution_lease.release()
    await trading_safety.flush_halt_persistence()

    # 4. Disconnect Redis safely
    await redis_client.disconnect()
    trading_safety.set_readiness("redis", False, "application shutdown")
    trading_safety.set_readiness("database", False, "application shutdown")
    
app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan)

# --- API Endpoints ---

@app.get("/health")
async def health_check():
    health_data = {
        "status": "ok",
        "version": "0.2.0-safety-interlock",
        "trading_safety": trading_safety.status(),
    }
    if getattr(settings, "AUTO_ROUTER_ENABLED", False):
        try:
            from app.core.auto_router import router_state
            health_data["auto_router"] = router_state
        except ImportError:
            pass
    return health_data


@app.get("/ready")
async def readiness_check():
    """Readiness is fail-closed for live order submission and never exposes secrets."""
    safety = trading_safety.status()
    ready = (
        trading_safety.mode is not TradingMode.LIVE
        or safety["live_order_submission_allowed"]
    )
    payload = {
        "status": "ready" if ready else "blocked",
        "control_plane_ready": bool(
            safety["readiness"]["database"]["ready"]
            and safety["readiness"]["redis"]["ready"]
        ),
        "live_order_ready": safety["live_order_submission_allowed"],
        "trading_safety": safety,
    }
    return JSONResponse(payload, status_code=200 if ready else 503)

@app.post("/markets/{condition_id}/start", dependencies=[Depends(require_admin)])
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
    except SafetyInterlockError as e:
        raise HTTPException(status_code=423, detail=f"Trading safety interlock: {e}")

@app.post("/markets/{condition_id}/stop", dependencies=[Depends(require_admin)])
async def stop_market_making(condition_id: str, db: AsyncSession = Depends(get_db)):
    """Soft stop: Cancel all orders and suspend quoting engine for this market"""
    from app.oms.core import oms
    
    # Send pub/sub message to tell engine to halt immediately
    await redis_client.publish(f"control:{condition_id}", {"action": "suspend"})
    logger.info(f"Published suspend signal for {condition_id}")
    
    # Soft Cancel via Relayer (Cancel all active orders for this market)
    cancel_ok = await oms.cancel_market_orders(condition_id)
    stopped_tasks = await stop_market_tasks(condition_id)
    
    # Update DB status
    result = await db.execute(select(MarketMeta).filter(MarketMeta.condition_id == condition_id))
    market = result.scalar_one_or_none()
    if market:
        market.status = "suspended"
        await db.commit()
        
    if not cancel_ok:
        trading_safety.halt(
            f"stop for {condition_id[:12]} could not confirm all order cancellations"
        )
        raise HTTPException(
            status_code=502,
            detail="Engine stopped, but one or more exchange order cancellations were not confirmed",
        )

    return {
        "status": "stopped",
        "condition_id": condition_id,
        "engine_tasks_stopped": stopped_tasks,
    }

@app.post("/markets/{condition_id}/liquidate", dependencies=[Depends(require_admin)])
async def liquidate_market(condition_id: str):
    """Permanently reject the removed unbounded-slippage liquidation endpoint."""
    raise HTTPException(
        status_code=410,
        detail=(
            f"Unsafe market-dump liquidation was removed for {condition_id}. "
            "Use /stop or /admin/halt; automated exits use the bounded depth-aware policy."
        ),
    )

@app.get("/markets/{condition_id}/risk")
async def get_market_risk(condition_id: str, db: AsyncSession = Depends(get_db)):
    """View current inventory and delta"""
    result = await db.execute(select(InventoryLedger).filter(InventoryLedger.market_id == condition_id))
    inventory = result.scalar_one_or_none()
    
    if not inventory:
        raise HTTPException(status_code=404, detail="Market inventory not found")
        
    accounting_ready = trading_safety.status()["readiness"]["accounting_integrity"][
        "ready"
    ]
    verified = accounting_ready and str(inventory.accounting_version) == "v2"
    verified_pnl = float(inventory.realized_pnl or 0) if verified else None
    return {
        "market_id": condition_id,
        "yes_exposure": float(inventory.yes_exposure or 0),
        "no_exposure": float(inventory.no_exposure or 0),
        # Backward-compatible key, but never return an unverified number.
        "realized_pnl": verified_pnl,
        "net_realized_pnl": verified_pnl,
        "pnl_status": "VERIFIED_NET" if verified else "UNVERIFIED",
        "accounting_version": inventory.accounting_version,
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
    """List all pending/open/unknown orders that may still carry exchange risk."""
    result = await db.execute(
        select(OrderJournal).filter(
            OrderJournal.status.in_(
                [OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.UNKNOWN]
            )
        )
    )
    orders = result.scalars().all()
    
    return [
        {
            "id": o.exchange_order_id or o.order_id,
            "client_order_id": o.order_id,
            "market_id": o.market_id,
            "side": o.side,
            "price": float(o.price),
            "size": float(o.size),
            "status": o.status
        } for o in orders
    ]


@app.get(
    "/admin/reconciliation/orders/latest",
    dependencies=[Depends(require_admin)],
)
async def latest_order_reconciliations(
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """Return redacted reconciliation audit summaries, never raw exchange payloads."""
    safe_limit = max(1, min(int(limit), 100))
    rows = (
        await db.execute(
            select(OrderReconciliationRun)
            .order_by(OrderReconciliationRun.started_at.desc())
            .limit(safe_limit)
        )
    ).scalars().all()
    return [
        {
            "run_id": row.run_id,
            "status": row.status,
            "local_order_count": row.local_order_count,
            "exchange_open_count": row.exchange_open_count,
            "blocker_count": row.blocker_count,
            "summary": row.summary,
            "started_at": row.started_at,
            "completed_at": row.completed_at,
        }
        for row in rows
    ]


@app.post("/admin/reconciliation/orders", dependencies=[Depends(require_admin)])
async def reconcile_orders_now():
    """Run read-only exchange fact collection and local reservation reconciliation."""
    if trading_safety.mode is not TradingMode.LIVE:
        raise HTTPException(status_code=409, detail="order reconciliation requires live mode")
    if get_active_router_markets():
        raise HTTPException(
            status_code=409,
            detail="stop all market engines before manual order reconciliation",
        )
    from app.oms.core import oms

    if oms.client is None:
        raise HTTPException(status_code=503, detail="CLOB client is unavailable")
    trading_safety.set_readiness(
        "open_orders_reconciled", False, "manual reconciliation in progress"
    )
    report = await order_reconciliation_service.reconcile(oms.client)
    return {
        "safe": report.safe,
        "blocker_count": len(report.blockers),
        "actions": [asdict(action) for action in report.actions],
        "restart_required_to_clear_prior_halt": trading_safety.status()["halted"],
    }


@app.get(
    "/admin/accounting/audits/latest",
    dependencies=[Depends(require_admin)],
)
async def latest_accounting_audits(
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """Return local accounting replay results; no exchange facts are requested."""
    safe_limit = max(1, min(int(limit), 100))
    rows = (
        await db.execute(
            select(AccountingAuditRun)
            .order_by(AccountingAuditRun.started_at.desc())
            .limit(safe_limit)
        )
    ).scalars().all()
    return [
        {
            "run_id": row.run_id,
            "status": row.status,
            "inventory_count": row.inventory_count,
            "fill_count": row.fill_count,
            "blocker_count": row.blocker_count,
            "summary": row.summary,
            "started_at": row.started_at,
            "completed_at": row.completed_at,
        }
        for row in rows
    ]


@app.post("/admin/accounting/audits", dependencies=[Depends(require_admin)])
async def run_accounting_audit_now():
    """Run a deterministic local replay only while all engines are stopped."""
    if trading_safety.mode is not TradingMode.DISABLED:
        raise HTTPException(
            status_code=409,
            detail="manual accounting audit requires TRADING_MODE=disabled",
        )
    if get_active_router_markets():
        raise HTTPException(
            status_code=409,
            detail="stop all market engines before accounting audit",
        )
    report = await accounting_integrity_service.audit()
    return {
        "safe": report.safe,
        "inventory_count": report.inventory_count,
        "fill_count": report.fill_count,
        "blocker_count": len(report.blockers),
        "blockers": [asdict(blocker) for blocker in report.blockers],
        "restart_required_to_clear_prior_halt": trading_safety.status()["halted"],
    }


@app.post("/admin/halt", dependencies=[Depends(require_admin)])
async def emergency_halt(db: AsyncSession = Depends(get_db)):
    """Sticky safety halt: block new risk and attempt to suspend/cancel every known market."""
    from app.oms.core import oms

    trading_safety.halt("authenticated manual emergency halt")
    await persist_halt("authenticated manual emergency halt")
    active = set(get_active_router_markets())
    journal_markets = set(
        (
            await db.execute(
                select(OrderJournal.market_id).filter(
                    OrderJournal.status.in_(
                        [OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.UNKNOWN]
                    )
                )
            )
        ).scalars().all()
    )
    targets = sorted(active.union(journal_markets))
    results = {}
    for condition_id in targets:
        if condition_id in active:
            await redis_client.publish(
                f"control:{condition_id}", {"action": "suspend"}
            )
        cancel_ok = await oms.cancel_market_orders(condition_id)
        stopped_tasks = (
            await stop_market_tasks(condition_id) if condition_id in active else 0
        )
        results[condition_id] = {
            "cancel_confirmed": cancel_ok,
            "engine_tasks_stopped": stopped_tasks,
        }
    if targets:
        markets = (
            await db.execute(
                select(MarketMeta).filter(MarketMeta.condition_id.in_(targets))
            )
        ).scalars().all()
        for market in markets:
            market.status = "suspended"
        await db.commit()
    return {
        "status": "halted",
        "new_live_orders_blocked": True,
        "markets": results,
    }


@app.post("/admin/halt/acknowledge", dependencies=[Depends(require_admin)])
async def acknowledge_halt(
    confirmation: Optional[str] = Header(
        default=None, alias="X-Confirm-Safety-Recovery"
    ),
):
    """Acknowledge a durable halt only in disabled, locally flat state."""
    if trading_safety.mode is not TradingMode.DISABLED:
        raise HTTPException(
            status_code=409,
            detail="persistent halt acknowledgement requires TRADING_MODE=disabled",
        )
    if confirmation != "ACKNOWLEDGE_PERSISTED_HALT":
        raise HTTPException(
            status_code=400,
            detail="Missing exact X-Confirm-Safety-Recovery safety phrase",
        )
    if get_active_router_markets():
        raise HTTPException(status_code=409, detail="stop all market engines first")
    async with AsyncSessionLocal() as session:
        unresolved = (
            await session.execute(
                select(OrderJournal.order_id)
                .filter(
                    OrderJournal.status.in_(
                        [OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.UNKNOWN]
                    )
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        nonzero = (
            await session.execute(
                select(InventoryLedger.market_id)
                .filter(
                    (InventoryLedger.yes_exposure != 0)
                    | (InventoryLedger.no_exposure != 0)
                )
                .limit(1)
            )
        ).scalar_one_or_none()
    if unresolved or nonzero:
        raise HTTPException(
            status_code=409,
            detail="reconcile unresolved orders and flatten local inventory first",
        )
    try:
        incident_id = await acknowledge_persistent_halt()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "status": "acknowledged",
        "incident_id": incident_id,
        "live_restart_requires_new_arm": True,
    }


@app.post("/admin/wipe", dependencies=[Depends(require_admin)])
async def wipe_all_data(
    db: AsyncSession = Depends(get_db),
    confirmation: Optional[str] = Header(default=None, alias="X-Confirm-Wipe"),
):
    """
    DANGER: Wipe all local state (Postgres + Redis) for a clean reset.
    Intended for development / manual recovery only.
    """
    if not bool(getattr(settings, "ENABLE_ADMIN_WIPE", False)):
        raise HTTPException(status_code=403, detail="ADMIN wipe is disabled by configuration")
    if trading_safety.mode is not TradingMode.DISABLED:
        raise HTTPException(
            status_code=409,
            detail="ADMIN wipe requires TRADING_MODE=disabled",
        )
    active = get_active_router_markets()
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"ADMIN wipe blocked while {len(active)} market engines are active",
        )
    if confirmation != "WIPE_LOCAL_STATE_IRREVERSIBLY":
        raise HTTPException(
            status_code=400,
            detail="Missing exact X-Confirm-Wipe safety phrase",
        )

    unresolved_orders = (
        await db.execute(
            select(OrderJournal.order_id).filter(
                OrderJournal.status.in_(
                    [OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.UNKNOWN]
                )
            ).limit(1)
        )
    ).scalar_one_or_none()
    nonzero_inventory = (
        await db.execute(
            select(InventoryLedger.market_id).filter(
                (InventoryLedger.yes_exposure > 0)
                | (InventoryLedger.no_exposure > 0)
            ).limit(1)
        )
    ).scalar_one_or_none()
    if unresolved_orders or nonzero_inventory:
        raise HTTPException(
            status_code=409,
            detail=(
                "ADMIN wipe is blocked while unresolved orders or nonzero positions exist; "
                "preserve local evidence and reconcile/recover first"
            ),
        )

    # 1. Wipe Postgres tables in safe order (children first).
    await db.execute(delete(ExchangeOrderSnapshot))
    await db.execute(delete(OrderReconciliationRun))
    await db.execute(delete(AccountingAuditRun))
    await db.execute(delete(FillCashLedger))
    await db.execute(delete(FillEvent))
    await db.execute(delete(RiskReservation))
    await db.execute(delete(ExecutionLease))
    await db.execute(delete(PortfolioRiskState))
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
