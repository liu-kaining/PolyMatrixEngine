"""Deterministic accounting replay and durable integrity audit service."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy.future import select

from app.core.accounting import AccountingInvariantError, apply_fill_accounting
from app.core.trading_safety import trading_safety
from app.db.session import AsyncSessionLocal
from app.models.db_models import (
    AccountingAuditRun,
    FillCashLedger,
    FillEvent,
    InventoryLedger,
    MarketMeta,
)


TOLERANCE = 1e-6


@dataclass(frozen=True)
class InventoryAccountingFact:
    market_id: str
    accounting_version: str
    state_version: int
    yes_exposure: float
    no_exposure: float
    yes_capital_used: float
    no_capital_used: float
    net_realized_pnl: float


@dataclass(frozen=True)
class ProcessedFillFact:
    event_id: str
    status: str
    market_id: Optional[str]
    outcome: Optional[str]
    side: Optional[str]
    price: float
    size: float
    accounting_state_version: Optional[int]


@dataclass(frozen=True)
class CashLedgerFact:
    event_id: str
    market_id: str
    side: str
    gross_cash_delta: float
    fee_amount: Optional[float]
    net_cash_delta: Optional[float]
    fee_status: str


@dataclass(frozen=True)
class AccountingBlocker:
    code: str
    detail: str
    market_id: Optional[str] = None
    event_id: Optional[str] = None


@dataclass(frozen=True)
class AccountingIntegrityReport:
    safe: bool
    inventory_count: int
    fill_count: int
    blockers: tuple[AccountingBlocker, ...]


def _close(actual: float, expected: float) -> bool:
    return math.isfinite(actual) and abs(actual - expected) <= TOLERANCE


def audit_accounting_facts(
    inventories: Iterable[InventoryAccountingFact],
    fills: Iterable[ProcessedFillFact],
    cash_entries: Iterable[CashLedgerFact],
    *,
    require_known_fees: bool = True,
) -> AccountingIntegrityReport:
    """Replay v2 inventory and verify one immutable cash fact per processed fill."""
    inventory_rows = list(inventories)
    fill_rows = list(fills)
    cash_rows = list(cash_entries)
    blockers: list[AccountingBlocker] = []

    inventory_by_market: dict[str, InventoryAccountingFact] = {}
    for row in inventory_rows:
        if row.market_id in inventory_by_market:
            blockers.append(
                AccountingBlocker("DUPLICATE_INVENTORY", "duplicate inventory ledger", row.market_id)
            )
        inventory_by_market[row.market_id] = row
        if row.accounting_version != "v2":
            blockers.append(
                AccountingBlocker(
                    "UNVERIFIED_ACCOUNTING_VERSION",
                    f"ledger version is {row.accounting_version or 'missing'}",
                    row.market_id,
                )
            )

    cash_by_event: dict[str, CashLedgerFact] = {}
    for row in cash_rows:
        if row.event_id in cash_by_event:
            blockers.append(
                AccountingBlocker(
                    "DUPLICATE_CASH_FACT", "duplicate fill cash fact", row.market_id, row.event_id
                )
            )
        cash_by_event[row.event_id] = row

    processed_by_market: dict[str, list[ProcessedFillFact]] = {}
    fill_ids: set[str] = set()
    for fill in fill_rows:
        fill_ids.add(fill.event_id)
        if fill.status != "PROCESSED":
            blockers.append(
                AccountingBlocker(
                    "UNPROCESSED_FILL",
                    f"fill inbox status is {fill.status or 'missing'}",
                    fill.market_id,
                    fill.event_id,
                )
            )
            continue
        if not fill.market_id or fill.market_id not in inventory_by_market:
            blockers.append(
                AccountingBlocker(
                    "FILL_WITHOUT_INVENTORY",
                    "processed fill is not mapped to an inventory ledger",
                    fill.market_id,
                    fill.event_id,
                )
            )
            continue
        if fill.outcome not in {"YES", "NO"} or fill.side not in {"BUY", "SELL"}:
            blockers.append(
                AccountingBlocker(
                    "INVALID_FILL_MAPPING",
                    "processed fill has no authoritative outcome/side mapping",
                    fill.market_id,
                    fill.event_id,
                )
            )
            continue
        if fill.accounting_state_version is None or fill.accounting_state_version <= 0:
            blockers.append(
                AccountingBlocker(
                    "MISSING_FILL_STATE_VERSION",
                    "processed fill cannot be deterministically ordered",
                    fill.market_id,
                    fill.event_id,
                )
            )
            continue
        processed_by_market.setdefault(fill.market_id, []).append(fill)

        cash = cash_by_event.get(fill.event_id)
        if cash is None:
            blockers.append(
                AccountingBlocker(
                    "MISSING_CASH_FACT",
                    "processed fill has no immutable cash fact",
                    fill.market_id,
                    fill.event_id,
                )
            )
            continue
        if cash.market_id != fill.market_id or cash.side != fill.side:
            blockers.append(
                AccountingBlocker(
                    "CASH_IDENTITY_MISMATCH",
                    "cash fact identity differs from processed fill",
                    fill.market_id,
                    fill.event_id,
                )
            )
            continue
        expected_gross = fill.price * fill.size * (-1 if fill.side == "BUY" else 1)
        if not _close(cash.gross_cash_delta, expected_gross):
            blockers.append(
                AccountingBlocker(
                    "CASH_NOTIONAL_MISMATCH",
                    "cash fact does not equal signed fill notional",
                    fill.market_id,
                    fill.event_id,
                )
            )
        if cash.fee_status == "KNOWN":
            if cash.fee_amount is None or cash.net_cash_delta is None:
                blockers.append(
                    AccountingBlocker(
                        "INCOMPLETE_KNOWN_FEE",
                        "known fee cash fact lacks fee or net cash",
                        fill.market_id,
                        fill.event_id,
                    )
                )
            elif cash.fee_amount < 0 or not _close(
                cash.net_cash_delta, cash.gross_cash_delta - cash.fee_amount
            ):
                blockers.append(
                    AccountingBlocker(
                        "INVALID_NET_CASH",
                        "net cash is inconsistent with gross cash and fee",
                        fill.market_id,
                        fill.event_id,
                    )
                )
        elif cash.fee_status == "UNKNOWN":
            if cash.fee_amount is not None or cash.net_cash_delta is not None:
                blockers.append(
                    AccountingBlocker(
                        "AMBIGUOUS_UNKNOWN_FEE",
                        "unknown fee cash fact must not invent net cash",
                        fill.market_id,
                        fill.event_id,
                    )
                )
            if require_known_fees:
                blockers.append(
                    AccountingBlocker(
                        "UNKNOWN_EXECUTION_FEE",
                        "net PnL is unavailable until the execution fee is authoritative",
                        fill.market_id,
                        fill.event_id,
                    )
                )
        else:
            blockers.append(
                AccountingBlocker(
                    "INVALID_FEE_STATUS",
                    "cash fact has an unsupported fee status",
                    fill.market_id,
                    fill.event_id,
                )
            )

    for cash in cash_rows:
        if cash.event_id not in fill_ids:
            blockers.append(
                AccountingBlocker(
                    "ORPHAN_CASH_FACT",
                    "cash fact has no fill inbox event",
                    cash.market_id,
                    cash.event_id,
                )
            )

    for market_id, inventory in inventory_by_market.items():
        if inventory.accounting_version != "v2":
            continue
        market_fills = sorted(
            processed_by_market.get(market_id, []),
            key=lambda item: (int(item.accounting_state_version or 0), item.event_id),
        )
        versions = [int(item.accounting_state_version or 0) for item in market_fills]
        expected_versions = list(range(1, int(inventory.state_version) + 1))
        if versions != expected_versions:
            blockers.append(
                AccountingBlocker(
                    "LEDGER_VERSION_GAP",
                    "inventory mutations are not a contiguous sequence of durable fills",
                    market_id,
                )
            )
            continue

        yes_exposure = yes_capital = no_exposure = no_capital = realized = 0.0
        try:
            for fill in market_fills:
                cash = cash_by_event.get(fill.event_id)
                replay_fee = (
                    float(cash.fee_amount)
                    if cash is not None
                    and cash.fee_status == "KNOWN"
                    and cash.fee_amount is not None
                    else 0.0
                )
                if fill.outcome == "YES":
                    result = apply_fill_accounting(
                        exposure=yes_exposure,
                        capital_used=yes_capital,
                        realized_pnl=realized,
                        side=str(fill.side),
                        fill_size=fill.size,
                        fill_price=fill.price,
                        fee_amount=replay_fee,
                    )
                    yes_exposure, yes_capital = result.exposure, result.capital_used
                else:
                    result = apply_fill_accounting(
                        exposure=no_exposure,
                        capital_used=no_capital,
                        realized_pnl=realized,
                        side=str(fill.side),
                        fill_size=fill.size,
                        fill_price=fill.price,
                        fee_amount=replay_fee,
                    )
                    no_exposure, no_capital = result.exposure, result.capital_used
                realized = result.realized_pnl
        except AccountingInvariantError as exc:
            blockers.append(
                AccountingBlocker("ACCOUNTING_REPLAY_FAILED", str(exc), market_id)
            )
            continue

        expected_values = (
            ("yes_exposure", inventory.yes_exposure, yes_exposure),
            ("no_exposure", inventory.no_exposure, no_exposure),
            ("yes_capital_used", inventory.yes_capital_used, yes_capital),
            ("no_capital_used", inventory.no_capital_used, no_capital),
            ("net_realized_pnl", inventory.net_realized_pnl, realized),
        )
        for field, actual, expected in expected_values:
            if not _close(actual, expected):
                blockers.append(
                    AccountingBlocker(
                        "LEDGER_REPLAY_MISMATCH",
                        f"{field} differs from deterministic fill replay",
                        market_id,
                    )
                )

    return AccountingIntegrityReport(
        safe=not blockers,
        inventory_count=len(inventory_rows),
        fill_count=len(fill_rows),
        blockers=tuple(blockers),
    )


class AccountingIntegrityService:
    async def audit(self, *, halt_on_failure: bool = True) -> AccountingIntegrityReport:
        run_id = f"accounting_{uuid.uuid4().hex}"
        async with AsyncSessionLocal() as session:
            session.add(AccountingAuditRun(run_id=run_id, summary={}))
            await session.commit()

        try:
            async with AsyncSessionLocal() as session:
                inventories = (await session.execute(select(InventoryLedger))).scalars().all()
                fills = (await session.execute(select(FillEvent))).scalars().all()
                cash_entries = (await session.execute(select(FillCashLedger))).scalars().all()
                markets = (await session.execute(select(MarketMeta))).scalars().all()

            market_tokens = {
                market.condition_id: (market.yes_token_id, market.no_token_id)
                for market in markets
            }
            inventory_facts = [
                InventoryAccountingFact(
                    market_id=row.market_id,
                    accounting_version=str(row.accounting_version or "v1"),
                    state_version=int(row.state_version or 0),
                    yes_exposure=float(row.yes_exposure or 0),
                    no_exposure=float(row.no_exposure or 0),
                    yes_capital_used=float(row.yes_capital_used or 0),
                    no_capital_used=float(row.no_capital_used or 0),
                    net_realized_pnl=float(row.realized_pnl or 0),
                )
                for row in inventories
            ]
            fill_facts = []
            for row in fills:
                tokens = market_tokens.get(row.market_id)
                outcome = None
                if tokens and row.token_id == tokens[0]:
                    outcome = "YES"
                elif tokens and row.token_id == tokens[1]:
                    outcome = "NO"
                fill_facts.append(
                    ProcessedFillFact(
                        event_id=row.event_id,
                        status=str(row.status or ""),
                        market_id=row.market_id,
                        outcome=outcome,
                        side=str(row.side or "").upper() or None,
                        price=float(row.price),
                        size=float(row.size),
                        accounting_state_version=row.accounting_state_version,
                    )
                )
            cash_facts = [
                CashLedgerFact(
                    event_id=row.event_id,
                    market_id=row.market_id,
                    side=str(row.side or "").upper(),
                    gross_cash_delta=float(row.gross_cash_delta),
                    fee_amount=float(row.fee_amount) if row.fee_amount is not None else None,
                    net_cash_delta=(
                        float(row.net_cash_delta) if row.net_cash_delta is not None else None
                    ),
                    fee_status=str(row.fee_status or ""),
                )
                for row in cash_entries
            ]
            report = audit_accounting_facts(inventory_facts, fill_facts, cash_facts)
            summary = {
                "safe": report.safe,
                "blockers": [
                    {
                        "code": blocker.code,
                        "detail": blocker.detail,
                        "market_id": blocker.market_id,
                        "event_id": blocker.event_id,
                    }
                    for blocker in report.blockers[:100]
                ],
                "truncated": len(report.blockers) > 100,
            }
            async with AsyncSessionLocal() as session:
                run = await session.get(AccountingAuditRun, run_id, with_for_update=True)
                run.status = "SAFE" if report.safe else "BLOCKED"
                run.inventory_count = report.inventory_count
                run.fill_count = report.fill_count
                run.blocker_count = len(report.blockers)
                run.summary = summary
                run.completed_at = datetime.now(timezone.utc)
                await session.commit()

            detail = (
                f"accounting replay verified for {report.inventory_count} ledgers"
                if report.safe
                else f"accounting audit found {len(report.blockers)} blocker(s)"
            )
            trading_safety.set_readiness("accounting_integrity", report.safe, detail)
            if not report.safe and halt_on_failure:
                trading_safety.halt(detail)
            return report
        except Exception as exc:
            detail = f"accounting audit failed: {str(exc)[:500]}"
            async with AsyncSessionLocal() as session:
                run = await session.get(AccountingAuditRun, run_id, with_for_update=True)
                if run is not None:
                    run.status = "FAILED"
                    run.blocker_count = 1
                    run.summary = {"safe": False, "error": detail}
                    run.completed_at = datetime.now(timezone.utc)
                    await session.commit()
            trading_safety.set_readiness("accounting_integrity", False, detail)
            if halt_on_failure:
                trading_safety.halt(detail)
            raise


accounting_integrity_service = AccountingIntegrityService()
