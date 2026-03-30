"""
Per-condition exposure caps: binary (2 outcomes) vs categorical (>2 outcomes).
Outcome count is cached under Redis key rewards:{condition_id} as outcome_count.
"""
import logging
from typing import Any, Dict, Optional

from app.core.config import settings
from app.core.redis import redis_client

logger = logging.getLogger(__name__)


def exposure_cap_usd_for_outcome_count(outcome_count: int) -> float:
    """Binary markets use MAX_EXPOSURE_PER_MARKET; categorical (>2 tokens) use MAX_EXPOSURE_CATEGORICAL."""
    n = int(outcome_count) if outcome_count else 2
    if n > 2:
        return float(getattr(settings, "MAX_EXPOSURE_CATEGORICAL", 30.0))
    return float(getattr(settings, "MAX_EXPOSURE_PER_MARKET", 50.0))


def _parse_outcome_count_from_redis(payload: Optional[Dict[str, Any]]) -> Optional[int]:
    if not payload or payload.get("outcome_count") is None:
        return None
    try:
        n = int(payload["outcome_count"])
        return n if n >= 2 else None
    except (ValueError, TypeError):
        return None


async def merge_rewards_redis_fields(condition_id: str, fields: Dict[str, Any]) -> None:
    """Merge fields into rewards:{condition_id} without dropping existing keys."""
    cur = await redis_client.get_state(f"rewards:{condition_id}") or {}
    if not isinstance(cur, dict):
        cur = {}
    updated = {**cur, **{k: v for k, v in fields.items() if v is not None}}
    await redis_client.set_state(f"rewards:{condition_id}", updated)


async def exposure_cap_usd_for_condition_redis_only(condition_id: str) -> float:
    """Watchdog-friendly: use Redis outcome_count only (no Gamma I/O). Defaults to binary cap if unset."""
    r = await redis_client.get_state(f"rewards:{condition_id}")
    oc = _parse_outcome_count_from_redis(r if isinstance(r, dict) else None) or 2
    return exposure_cap_usd_for_outcome_count(oc)


async def resolve_outcome_count(condition_id: str) -> int:
    """
    Resolve number of CLOB outcome tokens for this condition.
    Prefer Redis cache; on miss, fetch Gamma once and write outcome_count to Redis.
    """
    existing = await redis_client.get_state(f"rewards:{condition_id}")
    cached = _parse_outcome_count_from_redis(existing if isinstance(existing, dict) else None)
    if cached is not None:
        return cached

    from app.market_data.gamma_client import gamma_client

    info = await gamma_client.get_market_info(condition_id)
    if info is None:
        return 2
    n = max(2, int(getattr(info, "outcome_count", 2)))
    await merge_rewards_redis_fields(condition_id, {"outcome_count": n})
    if n > 2:
        logger.info(
            "[exposure] condition %s: categorical market outcome_count=%d (cached to Redis)",
            condition_id[:12],
            n,
        )
    return n
