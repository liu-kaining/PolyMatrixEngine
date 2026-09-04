"""Fail-closed Polymarket geographic eligibility preflight."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
import asyncio
import logging

import httpx

from app.core.config import settings
from app.core.trading_safety import trading_safety


logger = logging.getLogger(__name__)


class GeographicEligibilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeographicEligibility:
    blocked: bool
    country: str
    region: str


def parse_geoblock_response(payload: Any) -> GeographicEligibility:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("blocked"), bool):
        raise GeographicEligibilityError("geoblock response lacks a boolean blocked field")
    country = str(payload.get("country") or "").strip().upper()
    region = str(payload.get("region") or "").strip().upper()
    if not country:
        raise GeographicEligibilityError("geoblock response lacks a country code")
    return GeographicEligibility(bool(payload["blocked"]), country, region)


async def fetch_geographic_eligibility() -> GeographicEligibility:
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        response = await client.get(settings.GEOBLOCK_URL)
        response.raise_for_status()
        return parse_geoblock_response(response.json())


async def monitor_geographic_eligibility() -> None:
    """Recheck eligibility while live; any uncertainty halts new risk."""
    interval = max(30.0, float(settings.GEOBLOCK_RECHECK_SEC))
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                eligibility = await fetch_geographic_eligibility()
                if eligibility.blocked:
                    trading_safety.set_readiness(
                        "geographic_eligibility",
                        False,
                        f"exchange reports blocked jurisdiction country={eligibility.country}",
                    )
                    trading_safety.halt("geographic eligibility changed to blocked")
                    return
                trading_safety.set_readiness(
                    "geographic_eligibility",
                    True,
                    f"exchange eligibility passed for country={eligibility.country}",
                )
            except Exception as exc:
                trading_safety.set_readiness(
                    "geographic_eligibility", False, "geographic eligibility recheck failed"
                )
                trading_safety.halt(f"geographic eligibility recheck failed: {exc}")
                logger.exception("Geographic eligibility recheck failed")
                return
    except asyncio.CancelledError:
        return
