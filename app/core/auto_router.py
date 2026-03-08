"""
Auto-Router (V4.0): Portfolio Manager — Radar scan, ROI ranking, graceful rebalancing.
Runs as a background asyncio task when AUTO_ROUTER_ENABLED=True.
"""
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

import time
from app.core.config import settings
from app.core.redis import redis_client
from app.core.market_lifecycle import start_market_making_impl, get_active_router_markets

logger = logging.getLogger(__name__)

# Global health metrics for the dashboard/API
router_state = {
    "last_scan_ts": 0.0,
    "last_scan_error": None,
    "top_targets": [],
    "active_count": 0,
}

class RadarScanIncomplete(Exception):
    """Raised when Gamma API pagination fails, forcing the router to fail-closed."""
    pass

# Gamma API
GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_PAGE_LIMIT = 1000
GAMMA_REQUEST_TIMEOUT = 25.0
GAMMA_SEMAPHORE = 3

# Blacklist (aligned with dashboard screener)
SPORTS_BLACKLIST = {
    "sports", "sport", "nfl", "nba", "mlb", "nhl", "soccer", "football", "tennis",
    "hockey", "baseball", "basketball", "premier-league", "premier league",
    "champions-league", "champions league", "division", "win the cup", "stanley cup",
    "super bowl", "world series", "playoffs", "play-offs", "ucl", "uefa",
}
QUESTION_BLACKLIST = {
    "win the match", "wins the match", "to win the match", "halftime", "half-time",
    "in-play", "in play", "live betting", "live market", "live odds",
    "up or down", "strikes by", "one day after launch", "one week after",
    "points", "score", "goals", "touchdown", "points by", "home team", "away team",
}


def _parse_rewards(m: dict) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Returns (rewards_min_size, rewards_max_spread, reward_rate_per_day)."""
    r_min = None
    try:
        raw = m.get("rewardsMinSize")
        if raw is not None:
            r_min = float(raw)
    except (ValueError, TypeError):
        pass
    r_spread = None
    try:
        raw = m.get("rewardsMaxSpread")
        if raw is not None:
            r_spread = float(raw) / 100.0
    except (ValueError, TypeError):
        pass
    r_rate = None
    raw = m.get("rewardsDailyRate")
    if raw is None:
        cr = m.get("clobRewards") or []
        if isinstance(cr, list) and len(cr) > 0 and isinstance(cr[0], dict):
            raw = cr[0].get("rewardsDailyRate")
    try:
        if raw is not None:
            r_rate = float(raw)
    except (ValueError, TypeError):
        pass
    return r_min, r_spread, r_rate


def _blacklisted(m: dict) -> bool:
    """True if market should be excluded (sports / question keywords)."""
    tags_raw = m.get("tags")
    tags_list: List[str] = []
    if tags_raw:
        if isinstance(tags_raw, str):
            try:
                parsed = json.loads(tags_raw)
                tags_list = parsed if isinstance(parsed, list) else [parsed] if isinstance(parsed, str) else []
            except Exception:
                tags_list = [t.strip() for t in tags_raw.replace(";", ",").split(",") if t.strip()]
        elif isinstance(tags_raw, list):
            tags_list = tags_raw
    category_raw = (m.get("category") or m.get("subCategory") or "")
    slug = (m.get("slug") or "").lower()
    question_lower = (m.get("question") or "").lower()
    haystack = " ".join([category_raw, " ".join(str(x) for x in tags_list), slug]).lower()
    if any(kw in haystack for kw in SPORTS_BLACKLIST):
        return True
    if any(kw in question_lower for kw in QUESTION_BLACKLIST):
        return True
    return False


def _is_binary_yes_no(m: dict) -> bool:
    outcomes_raw = m.get("outcomes")
    outcomes: List[str] = []
    if outcomes_raw:
        if isinstance(outcomes_raw, str):
            try:
                outcomes = json.loads(outcomes_raw)
            except Exception:
                outcomes = []
        elif isinstance(outcomes_raw, list):
            outcomes = outcomes_raw
    return {str(o).strip().lower() for o in outcomes} == {"yes", "no"}


async def _fetch_gamma_page(
    client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    offset: int,
) -> Tuple[int, List[dict]]:
    """Fetch one page; raises exception on failure to ensure fail-closed scan."""
    async with sem:
        r = await client.get(
            GAMMA_API_URL,
            params={"active": "true", "closed": "false", "limit": GAMMA_PAGE_LIMIT, "offset": offset},
            timeout=GAMMA_REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return (offset, r.json() or [])


async def _radar_scan() -> List[dict]:
    """
    Fetch active markets from Gamma (with timeout), filter by rewards + blacklist + BASE_ORDER_SIZE,
    compute daily_roi = reward_rate_per_day / rewards_min_size, return list of dicts with condition_id and daily_roi.
    """
    base_order_size = max(5.0, float(getattr(settings, "BASE_ORDER_SIZE", 10.0)))
    max_markets = int(getattr(settings, "AUTO_ROUTER_MAX_MARKETS", 4)) * 5  # fetch more to rank
    all_markets: List[dict] = []
    seen: Set[str] = set()
    sem = asyncio.Semaphore(GAMMA_SEMAPHORE)

    async with httpx.AsyncClient(timeout=GAMMA_REQUEST_TIMEOUT) as client:
        offset = 0
        while len(all_markets) < max_markets:
            offsets = [offset + i * GAMMA_PAGE_LIMIT for i in range(GAMMA_SEMAPHORE)]
            tasks = [_fetch_gamma_page(client, sem, o) for o in offsets]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            done = False
            for res in results:
                if isinstance(res, Exception):
                    raise RadarScanIncomplete(f"Gamma page task failed: {res}")
                _off, page = res
                if len(page) < GAMMA_PAGE_LIMIT:
                    done = True
                for m in page or []:
                    cid = m.get("conditionId")
                    if not cid or cid in seen:
                        continue
                    if _blacklisted(m):
                        continue
                    if not _is_binary_yes_no(m):
                        continue
                    r_min, _r_spread, r_rate = _parse_rewards(m)
                    if r_min is None or r_min <= 0:
                        continue
                    if r_min > base_order_size:
                        continue
                    seen.add(cid)
                    rate = (r_rate or 0.0)
                    daily_roi = rate / r_min if r_min > 0 else 0.0
                    all_markets.append({
                        "condition_id": cid,
                        "daily_roi": daily_roi,
                        "rewards_min_size": r_min,
                        "reward_rate_per_day": rate,
                    })
            if done or len(all_markets) >= max_markets:
                break
            offset += GAMMA_SEMAPHORE * GAMMA_PAGE_LIMIT

    all_markets.sort(key=lambda x: x["daily_roi"], reverse=True)
    top_n = int(getattr(settings, "AUTO_ROUTER_MAX_MARKETS", 4))
    return all_markets[:top_n]


async def _get_active_markets() -> Set[str]:
    """
    Return set of condition_ids strictly from the EngineSupervisor's active task list.
    This guarantees we only interact with genuinely running markets.
    """
    return get_active_router_markets()

async def _rebalance(
    target_markets: List[dict],
    active_set: Set[str],
) -> None:
    """
    Evict markets not in target by sending graceful_exit, then calculate available 
    slots and start new markets to keep capital dynamically deployed.
    """
    max_markets = int(getattr(settings, "AUTO_ROUTER_MAX_MARKETS", 4))
    target_ids = {m["condition_id"] for m in target_markets}

    # 1. Evict: Send graceful_exit to markets that dropped out of Top N
    for cid in list(active_set):
        if cid not in target_ids:
            logger.info(
                "[AutoRouter] Evicting %s (dropped from Top N). Sending graceful_exit signal.",
                cid[:10],
            )
            try:
                await redis_client.publish(f"control:{cid}", {"action": "graceful_exit"})
            except Exception as e:
                logger.warning("[AutoRouter] Failed to publish graceful_exit for %s: %s", cid[:10], e)

    # 2. Add: Start new markets while staying under the maximum capital concurrent slots
    # Note: Markets in GRACEFUL_EXIT only SELL, they do not consume new USDC buying power.
    # Therefore, we only count retained active targets against our slot limit.
    retained_active = active_set.intersection(target_ids)
    slots_available = max_markets - len(retained_active)

    for m in target_markets:
        cid = m["condition_id"]
        if cid in active_set:
            continue
        if slots_available <= 0:
            break
        try:
            await start_market_making_impl(cid)
            active_set.add(cid)
            slots_available -= 1
            logger.info(
                "[AutoRouter] Started market %s (ROI: %.4f).",
                cid[:10], m.get("daily_roi", 0),
            )
        except Exception as e:
            logger.warning("[AutoRouter] Failed to start market %s: %s", cid[:10], e)


async def run() -> None:
    """
    Main loop: scan Gamma at interval, rank by daily_roi, rebalance (add / graceful_exit).
    Timeout-safe; never blocks indefinitely on external API.
    """
    interval = max(60, int(getattr(settings, "AUTO_ROUTER_SCAN_INTERVAL_SEC", 3600)))
    logger.info(
        "[AutoRouter] Started. Scan interval=%ds, max_markets=%d.",
        interval,
        int(getattr(settings, "AUTO_ROUTER_MAX_MARKETS", 4)),
    )
    
    # Do an immediate scan on startup, then sleep at the end of the loop
    while True:
        try:
            target_list = await _radar_scan()
            
            # Update health metrics
            router_state["last_scan_ts"] = time.time()
            router_state["last_scan_error"] = None
            router_state["top_targets"] = [{"cid": m["condition_id"], "roi": m["daily_roi"]} for m in target_list]
            
            if not target_list:
                logger.info("[AutoRouter] Scan complete. No eligible targets (rewards + base size).")
            else:
                logger.info(
                    "[AutoRouter] Scan complete. Top targets: %s",
                    ", ".join(f"{m['condition_id'][:10]}(ROI: {m['daily_roi']:.4f})" for m in target_list),
                )
                active_set = await _get_active_markets()
                router_state["active_count"] = len(active_set)
                await _rebalance(target_list, active_set)
                
            await asyncio.sleep(interval)

        except RadarScanIncomplete as e:
            router_state["last_scan_error"] = str(e)
            logger.warning("[AutoRouter] %s. Preserving current portfolio.", e)
            await asyncio.sleep(min(60, interval))
        except asyncio.CancelledError:
            logger.info("[AutoRouter] Shutting down.")
            break
        except Exception as e:
            router_state["last_scan_error"] = str(e)
            logger.exception("[AutoRouter] Scan/rebalance error: %s. Retrying after interval.", e)
            await asyncio.sleep(min(60, interval))
