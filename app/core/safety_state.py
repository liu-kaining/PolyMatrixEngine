"""Durable sticky-halt state.

The in-process halt remains immediate. This module mirrors it to Postgres and
restores it before any live adapter is initialized on the next process start.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy.future import select

from app.db.session import AsyncSessionLocal
from app.models.db_models import TradingControlState


logger = logging.getLogger(__name__)
CONTROL_ID = "global"


async def persist_halt(reason: str) -> None:
    try:
        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(
                    select(TradingControlState)
                    .filter(TradingControlState.control_id == CONTROL_ID)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            now = datetime.now(timezone.utc)
            if row is None:
                row = TradingControlState(
                    control_id=CONTROL_ID,
                    halted=True,
                    reason=str(reason)[:1000],
                    incident_id=uuid.uuid4().hex,
                    state_version=1,
                    halted_at=now,
                )
                session.add(row)
            else:
                if not row.halted:
                    row.incident_id = uuid.uuid4().hex
                    row.halted_at = now
                row.halted = True
                row.reason = str(reason)[:1000]
                row.acknowledged_at = None
                row.state_version = int(row.state_version or 0) + 1
            await session.commit()
    except Exception:
        # Never weaken the already-active in-memory interlock because persistence
        # failed. Operators can see the DB readiness/halt logs and recover offline.
        logger.exception("Could not persist sticky safety halt")


async def restore_persistent_halt() -> bool:
    from app.core.trading_safety import trading_safety

    async with AsyncSessionLocal() as session:
        row = await session.get(TradingControlState, CONTROL_ID)
    if row is not None and row.halted:
        trading_safety.restore_persistent_halt(row.reason or "persisted safety halt")
        logger.critical(
            "[SAFETY] Restored persistent halt incident=%s",
            str(row.incident_id or "unknown")[:12],
        )
        return True
    return False


async def acknowledge_persistent_halt() -> str:
    """Durably acknowledge/clear one incident, then clear process memory."""
    from app.core.trading_safety import trading_safety

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(TradingControlState)
                .filter(TradingControlState.control_id == CONTROL_ID)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None or not row.halted:
            raise RuntimeError("no persistent halt is active")
        incident_id = str(row.incident_id or "unknown")
        row.halted = False
        row.acknowledged_at = datetime.now(timezone.utc)
        row.state_version = int(row.state_version or 0) + 1
        await session.commit()
    trading_safety.clear_persistent_halt_after_ack()
    return incident_id
