"""
Runtime config overrides stored in Redis. Allows changing trading parameters from the
dashboard without restart. Engine and API read effective value = Redis override or env default.
"""
import logging
from typing import Any, Optional

from app.core.config import settings
from app.core.redis import redis_client

logger = logging.getLogger(__name__)

CONFIG_PREFIX = "config:"

# Keys that can be overridden at runtime (type for parsing)
RUNTIME_CONFIG_SPEC = {
    "BASE_ORDER_SIZE": float,
    "GRID_LEVELS": int,
    "QUOTE_BASE_SPREAD": float,
    "QUOTE_PRICE_OFFSET_THRESHOLD": float,
    "QUOTE_BID_ONE_TICK_BELOW_TOUCH": bool,
    "AUTO_TUNE_FOR_REWARDS": bool,
    "MAX_EXPOSURE_PER_MARKET": float,
    "GLOBAL_MAX_BUDGET": float,
}


def _parse_value(key: str, raw: str) -> Any:
    """Parse string from API/dashboard form to typed value."""
    spec = RUNTIME_CONFIG_SPEC.get(key)
    if spec is None:
        return raw
    if spec is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    try:
        return spec(raw) if raw is not None else None
    except (ValueError, TypeError):
        return None


def _value_matches_spec(key: str, val: Any) -> bool:
    """Ensure Redis value type matches spec; avoid using wrong type (e.g. bool for float)."""
    spec = RUNTIME_CONFIG_SPEC.get(key)
    if spec is None:
        return True
    if spec is bool:
        return isinstance(val, bool)
    if spec is int:
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if spec is float:
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    return True


async def get_effective(key: str) -> Optional[Any]:
    """Return effective value for key: Redis override if present and valid, else settings/env."""
    if key not in RUNTIME_CONFIG_SPEC:
        return getattr(settings, key, None)
    try:
        val = await redis_client.get_state(f"{CONFIG_PREFIX}{key}")
        if val is not None:
            # Redis stores JSON; may be int/float/bool already
            if isinstance(val, (int, float, bool)):
                if _value_matches_spec(key, val):
                    # Coerce int spec to int (e.g. 2.0 -> 2 for GRID_LEVELS)
                    spec = RUNTIME_CONFIG_SPEC[key]
                    if spec is int and isinstance(val, float):
                        return int(val)
                    return val
                # Wrong type in Redis (e.g. bool for numeric key); fall back to env
            else:
                parsed = _parse_value(key, str(val))
                if parsed is not None and _value_matches_spec(key, parsed):
                    return parsed
    except Exception as e:
        logger.debug("runtime_config get_effective %s: %s", key, e)
    return getattr(settings, key, None)


async def set_override(key: str, value: Any) -> bool:
    """Set Redis override for key. Value can be str (parsed) or typed. Returns True if success."""
    if key not in RUNTIME_CONFIG_SPEC:
        return False
    try:
        if isinstance(value, str):
            value = _parse_value(key, value)
            if value is None:
                return False
        # Clamp / coerce for safety
        spec = RUNTIME_CONFIG_SPEC[key]
        if spec is int:
            value = int(value) if isinstance(value, (int, float)) else value
            if not isinstance(value, int) or value < 1:
                return False
        if spec is float:
            val_f = float(value)
            if key == "BASE_ORDER_SIZE" and val_f < 5.0:
                return False  # CLOB minimum
            if val_f < 0:
                return False  # no negative values for any float key
        # Store typed value (Redis layer uses json.dumps)
        await redis_client.set_state(f"{CONFIG_PREFIX}{key}", value)
        return True
    except (ValueError, TypeError):
        return False
    except Exception as e:
        logger.warning("runtime_config set_override %s: %s", key, e)
        return False


async def delete_override(key: str) -> bool:
    """Remove Redis override so effective value falls back to env."""
    if key not in RUNTIME_CONFIG_SPEC:
        return False
    try:
        await redis_client.delete_key(f"{CONFIG_PREFIX}{key}")
        return True
    except Exception as e:
        logger.warning("runtime_config delete_override %s: %s", key, e)
        return False


async def get_all_effective() -> dict:
    """Return dict of all allowlisted keys to their effective values (for dashboard)."""
    out = {}
    for key in RUNTIME_CONFIG_SPEC:
        val = await get_effective(key)
        if val is not None:
            out[key] = val
    return out


def allowlist() -> list:
    """Return list of keys that can be overridden at runtime."""
    return list(RUNTIME_CONFIG_SPEC.keys())
