"""Database-backed single-writer lease for live execution."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.future import select

from app.core.config import settings
from app.core.trading_safety import trading_safety
from app.db.session import AsyncSessionLocal
from app.models.db_models import ExecutionLease


logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LiveExecutionLease:
    def __init__(self) -> None:
        configured = str(os.getenv("APP_INSTANCE_ID", "") or "").strip()
        self.owner_id = configured or f"{socket.gethostname()}-{uuid.uuid4().hex}"
        self.wallet_id = ""
        self.fencing_token: int | None = None
        self._task: asyncio.Task | None = None

    @property
    def ttl_seconds(self) -> float:
        return max(6.0, float(settings.EXECUTION_LEASE_TTL_SEC))

    async def acquire(self, wallet_id: str) -> bool:
        self.wallet_id = str(wallet_id or "").strip().lower()
        if not self.wallet_id:
            trading_safety.set_readiness(
                "executor_lease", False, "wallet id is missing"
            )
            return False

        for attempt in range(2):
            try:
                async with AsyncSessionLocal() as session:
                    row = (
                        await session.execute(
                            select(ExecutionLease)
                            .filter(ExecutionLease.wallet_id == self.wallet_id)
                            .with_for_update()
                        )
                    ).scalar_one_or_none()
                    now = _now()
                    expires_at = now + timedelta(seconds=self.ttl_seconds)
                    if row is None:
                        row = ExecutionLease(
                            wallet_id=self.wallet_id,
                            owner_id=self.owner_id,
                            fencing_token=1,
                            expires_at=expires_at,
                        )
                        session.add(row)
                    elif row.owner_id == self.owner_id or row.expires_at <= now:
                        if row.owner_id != self.owner_id:
                            row.fencing_token = int(row.fencing_token or 0) + 1
                        row.owner_id = self.owner_id
                        row.expires_at = expires_at
                    else:
                        trading_safety.set_readiness(
                            "executor_lease",
                            False,
                            "another process owns the live wallet lease",
                        )
                        return False
                    await session.commit()
                    self.fencing_token = int(row.fencing_token)
                trading_safety.set_readiness(
                    "executor_lease",
                    True,
                    f"wallet lease acquired with fence={self.fencing_token}",
                )
                return True
            except IntegrityError:
                if attempt == 0:
                    await asyncio.sleep(0)
                    continue
                raise
        return False

    async def renew(self) -> bool:
        if not self.wallet_id or self.fencing_token is None:
            return False
        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(
                    select(ExecutionLease)
                    .filter(ExecutionLease.wallet_id == self.wallet_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                row is None
                or row.owner_id != self.owner_id
                or int(row.fencing_token) != self.fencing_token
                or row.expires_at <= _now()
            ):
                return False
            row.expires_at = _now() + timedelta(seconds=self.ttl_seconds)
            await session.commit()
        return True

    async def _renew_loop(self) -> None:
        interval = max(2.0, self.ttl_seconds / 3.0)
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    renewed = await self.renew()
                except Exception:
                    renewed = False
                    logger.exception("Live execution lease renewal failed")
                if not renewed:
                    trading_safety.set_readiness(
                        "executor_lease", False, "wallet lease was lost"
                    )
                    trading_safety.halt("live execution wallet lease was lost")
                    return
        except asyncio.CancelledError:
            return

    def start_renewal(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._renew_loop())

    async def release(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if not self.wallet_id or self.fencing_token is None:
            return
        try:
            async with AsyncSessionLocal() as session:
                row = (
                    await session.execute(
                        select(ExecutionLease)
                        .filter(ExecutionLease.wallet_id == self.wallet_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if (
                    row is not None
                    and row.owner_id == self.owner_id
                    and int(row.fencing_token) == self.fencing_token
                ):
                    row.expires_at = _now()
                    await session.commit()
        finally:
            self.fencing_token = None
            trading_safety.set_readiness(
                "executor_lease", False, "wallet lease released"
            )


live_execution_lease = LiveExecutionLease()
