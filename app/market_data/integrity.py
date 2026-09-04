"""Pure market-data validation used by the gateway and quote engine."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional


class BookIntegrityError(ValueError):
    """A book update cannot safely be used for trading decisions."""


@dataclass(frozen=True)
class CursorDecision:
    accepted: bool
    requires_resync: bool
    reason: str


@dataclass(frozen=True)
class SnapshotHealth:
    healthy: bool
    reason: str
    age_seconds: float


def extract_sequence(payload: dict[str, Any]) -> Optional[int]:
    for key in ("sequence", "seq", "sequence_number"):
        value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise BookIntegrityError(f"invalid {key}") from exc
        if parsed < 0:
            raise BookIntegrityError(f"negative {key}")
        return parsed
    return None


def extract_exchange_timestamp(payload: dict[str, Any]) -> Optional[float]:
    value = None
    for key in ("timestamp", "exchange_timestamp", "ts"):
        if payload.get(key) not in (None, ""):
            value = payload[key]
            break
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise BookIntegrityError("invalid exchange timestamp") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise BookIntegrityError("invalid exchange timestamp")
    while parsed > 10_000_000_000:
        parsed /= 1000.0
    return parsed


def validate_event_time(
    exchange_timestamp: Optional[float],
    *,
    received_at: Optional[float] = None,
    max_age_seconds: float,
    max_future_skew_seconds: float,
) -> None:
    if exchange_timestamp is None:
        return
    now = time.time() if received_at is None else float(received_at)
    age = now - float(exchange_timestamp)
    if age > float(max_age_seconds):
        raise BookIntegrityError(f"exchange event is stale by {age:.3f}s")
    if age < -float(max_future_skew_seconds):
        raise BookIntegrityError(f"exchange event is {abs(age):.3f}s in the future")


def evaluate_cursor(previous: Optional[int], current: Optional[int]) -> CursorDecision:
    if current is None:
        return CursorDecision(True, False, "sequence unavailable")
    if previous is None:
        return CursorDecision(True, False, "sequence initialized")
    if current <= previous:
        return CursorDecision(False, False, "duplicate or regressing sequence")
    if current != previous + 1:
        return CursorDecision(False, True, "sequence gap requires a full snapshot")
    return CursorDecision(True, False, "contiguous sequence")


def _normalize_levels(levels: Iterable[dict[str, Any]], *, reverse: bool) -> list[dict]:
    normalized: dict[float, float] = {}
    for level in levels:
        if not isinstance(level, dict):
            raise BookIntegrityError("book level must be an object")
        try:
            price = float(level.get("price"))
            size = float(level.get("size"))
        except (TypeError, ValueError) as exc:
            raise BookIntegrityError("book price and size must be numeric") from exc
        if not math.isfinite(price) or not math.isfinite(size):
            raise BookIntegrityError("book price and size must be finite")
        if not 0.0 < price < 1.0:
            raise BookIntegrityError("book price must be between 0 and 1")
        if size <= 0:
            raise BookIntegrityError("snapshot book size must be positive")
        normalized[price] = size
    return [
        {"price": f"{price:.10g}", "size": size}
        for price, size in sorted(normalized.items(), reverse=reverse)
    ]


def validate_book_levels(
    bids: Iterable[dict[str, Any]], asks: Iterable[dict[str, Any]]
) -> tuple[list[dict], list[dict]]:
    normalized_bids = _normalize_levels(bids, reverse=True)
    normalized_asks = _normalize_levels(asks, reverse=False)
    if not normalized_bids or not normalized_asks:
        raise BookIntegrityError("both bid and ask liquidity are required")
    if float(normalized_bids[0]["price"]) >= float(normalized_asks[0]["price"]):
        raise BookIntegrityError("crossed or locked orderbook")
    return normalized_bids, normalized_asks


def assess_snapshot(
    snapshot: Any,
    *,
    now: Optional[float] = None,
    max_age_seconds: float,
    require_sequence: bool,
    require_exchange_timestamp: bool,
    require_snapshot_id: bool = False,
) -> SnapshotHealth:
    if not isinstance(snapshot, dict):
        return SnapshotHealth(False, "snapshot is missing or malformed", math.inf)
    if snapshot.get("valid") is not True:
        return SnapshotHealth(
            False,
            str(snapshot.get("integrity_reason") or "snapshot is marked invalid"),
            math.inf,
        )
    try:
        received_at = float(snapshot.get("received_at"))
    except (TypeError, ValueError):
        return SnapshotHealth(False, "snapshot has no valid receive timestamp", math.inf)
    current = time.time() if now is None else float(now)
    age = current - received_at
    if not math.isfinite(age) or age < -1.0:
        return SnapshotHealth(False, "snapshot receive timestamp is invalid", age)
    if age > float(max_age_seconds):
        return SnapshotHealth(False, f"snapshot is stale by {age:.3f}s", age)
    if require_sequence and snapshot.get("sequence") is None:
        return SnapshotHealth(False, "exchange sequence is unavailable", age)
    if require_exchange_timestamp and snapshot.get("exchange_timestamp") is None:
        return SnapshotHealth(False, "exchange timestamp is unavailable", age)
    if require_snapshot_id and not str(snapshot.get("snapshot_id") or "").strip():
        return SnapshotHealth(False, "exchange snapshot hash is unavailable", age)
    try:
        validate_book_levels(snapshot.get("bids", []), snapshot.get("asks", []))
    except BookIntegrityError as exc:
        return SnapshotHealth(False, str(exc), age)
    return SnapshotHealth(True, "healthy", age)
