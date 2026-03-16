"""
Auto-Router (V6.2): Portfolio Manager — Event horizon, sector limits, volatility penalty.
Runs as a background asyncio task when AUTO_ROUTER_ENABLED=True.
"""
import asyncio
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

import time
from app.core.config import settings
from app.core.redis import redis_client
from app.core.market_lifecycle import start_market_making_impl, get_active_router_markets
from app.core.inventory_state import inventory_state
from app.market_data.gamma_client import gamma_client

logger = logging.getLogger(__name__)

# Per-market start time for min-hold enforcement (rewards threshold)
market_start_times: Dict[str, float] = {}
# Per-market metadata (endDate, tags) for event horizon and sector limits
active_market_meta: Dict[str, dict] = {}

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


def _parse_end_date(m: dict) -> Optional[datetime]:
    """Parse endDate from Gamma market dict. Returns None if missing/invalid."""
    for key in ("endDate", "end_date", "endDateIso"):
        raw = m.get(key)
        if raw:
            try:
                if isinstance(raw, str):
                    s = raw.replace("Z", "+00:00").replace("z", "+00:00")
                    return datetime.fromisoformat(s)
                if hasattr(raw, "timestamp"):
                    return raw
            except (ValueError, TypeError):
                pass
    return None


def _parse_tags(m: dict) -> List[str]:
    """Parse tags/category from Gamma market dict."""
    tags_list: List[str] = []
    tags_raw = m.get("tags")
    if tags_raw:
        if isinstance(tags_raw, str):
            try:
                parsed = json.loads(tags_raw)
                tags_list = parsed if isinstance(parsed, list) else [parsed] if isinstance(parsed, str) else []
            except Exception:
                tags_list = [t.strip() for t in tags_raw.replace(";", ",").split(",") if t.strip()]
        elif isinstance(tags_raw, list):
            tags_list = [str(t) for t in tags_raw]
    cat = m.get("category") or m.get("subCategory")
    if cat and str(cat) not in tags_list:
        tags_list.append(str(cat))
    return tags_list


def _parse_liquidity(m: dict) -> float:
    """Parse liquidity from Gamma market dict."""
    for key in ("liquidity", "liquidityNum", "volume", "volumeNum"):
        raw = m.get(key)
        if raw is not None:
            try:
                return float(raw)
            except (ValueError, TypeError):
                pass
    return 0.0


def _within_event_horizon(end_date: Optional[datetime], hours: float) -> bool:
    """True if end_date is within `hours` of now (resolution imminent)."""
    if end_date is None:
        return False
    try:
        now = datetime.now(timezone.utc)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        delta_hours = (end_date - now).total_seconds() / 3600.0
        return 0 <= delta_hours <= hours
    except Exception:
        return False


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
    Fetch active markets from Gamma, filter by rewards + blacklist + event horizon,
    score with volatility penalty (liquidity), return list with condition_id, daily_roi, end_date, tags.
    """
    base_order_size = max(5.0, float(getattr(settings, "BASE_ORDER_SIZE", 10.0)))
    max_markets = int(getattr(settings, "AUTO_ROUTER_MAX_MARKETS", 4)) * 5
    event_horizon_hours = float(getattr(settings, "EVENT_HORIZON_HOURS", 24.0))
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
                    end_date = _parse_end_date(m)
                    if _within_event_horizon(end_date, event_horizon_hours):
                        continue
                    tags = _parse_tags(m)
                    liquidity = _parse_liquidity(m)
                    rate = (r_rate or 0.0)
                    daily_roi = rate / r_min if r_min > 0 else 0.0
                    score = daily_roi * math.log10(liquidity + 1.0)
                    seen.add(cid)
                    all_markets.append({
                        "condition_id": cid,
                        "daily_roi": daily_roi,
                        "score": score,
                        "rewards_min_size": r_min,
                        "reward_rate_per_day": rate,
                        "end_date": end_date,
                        "tags": tags,
                        "liquidity": liquidity,
                    })
            if done or len(all_markets) >= max_markets:
                break
            offset += GAMMA_SEMAPHORE * GAMMA_PAGE_LIMIT

    all_markets.sort(key=lambda x: x["score"], reverse=True)
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
    Evict markets: (1) Event horizon → immediate graceful_exit (bypass min_hold).
    (2) Dropped from Top N → graceful_exit only after min_hold_sec.
    Add new markets respecting sector limits (slots + exposure).
    """
    max_markets = int(getattr(settings, "AUTO_ROUTER_MAX_MARKETS", 4))
    min_hold_sec = max(0.0, float(getattr(settings, "AUTO_ROUTER_MIN_HOLD_HOURS", 12)) * 3600.0)
    event_horizon_hours = float(getattr(settings, "EVENT_HORIZON_HOURS", 24.0))
    max_exposure_per_sector = float(getattr(settings, "MAX_EXPOSURE_PER_SECTOR", 300.0))
    max_slots_per_sector = int(getattr(settings, "MAX_SLOTS_PER_SECTOR", 2))
    max_exposure_per_market = float(getattr(settings, "MAX_EXPOSURE_PER_MARKET", 50.0))
    target_ids = {m["condition_id"] for m in target_markets}
    now = time.time()

    # Build target lookup for metadata
    target_by_cid: Dict[str, dict] = {m["condition_id"]: m for m in target_markets if m.get("condition_id")}

    # 🛠️ 存量盘口强制注册（防重启状态丢失）
    for cid in active_set:
        if cid not in market_start_times:
            market_start_times[cid] = now
            logger.info("[AutoRouter] ⚡ 发现未记录时间的存量盘口，已初始化时间戳: %s", cid[:10])

    # 0. Event Horizon Eviction (bypass min_hold — do NOT hold into binary resolution)
    need_meta = [cid for cid in active_set if cid not in active_market_meta]
    if need_meta:
        batch = await gamma_client.get_markets_batch(need_meta)
        for cid, raw in batch.items():
            active_market_meta[cid] = {
                "end_date": _parse_end_date(raw),
                "tags": _parse_tags(raw),
            }
    for cid in list(active_set):
        meta = active_market_meta.get(cid) or {}
        end_date = meta.get("end_date")
        if _within_event_horizon(end_date, event_horizon_hours):
            logger.info(
                "[AutoRouter] Event horizon: %s resolving within %.1fh. Immediate graceful_exit (bypass min_hold).",
                cid[:10], event_horizon_hours,
            )
            try:
                await redis_client.publish(f"control:{cid}", {"action": "graceful_exit"})
            except Exception as e:
                logger.warning("[AutoRouter] Failed to publish graceful_exit for %s: %s", cid[:10], e)
            active_set.discard(cid)
            market_start_times.pop(cid, None)
            active_market_meta.pop(cid, None)

    # 1. Evict: Dropped from Top N — graceful_exit only if runtime >= min_hold_sec (定力锁)
    retained_active = active_set.intersection(target_ids)
    actually_retained: Set[str] = set(retained_active)

    for cid in list(active_set):
        if cid not in target_ids:
            start_ts = market_start_times.get(cid, now)
            runtime = now - start_ts
            if runtime < min_hold_sec:
                actually_retained.add(cid)
                logger.info(
                    "[AutoRouter] 定力锁: %s dropped from Top N but runtime %.1fh < %.1fh min hold. Retaining.",
                    cid[:10], runtime / 3600.0, min_hold_sec / 3600.0,
                )
            else:
                logger.info(
                    "[AutoRouter] Evicting %s (dropped from Top N, runtime %.1fh >= min hold). Sending graceful_exit.",
                    cid[:10], runtime / 3600.0,
                )
                try:
                    await redis_client.publish(f"control:{cid}", {"action": "graceful_exit"})
                except Exception as e:
                    logger.warning("[AutoRouter] Failed to publish graceful_exit for %s: %s", cid[:10], e)
                active_set.discard(cid)
                market_start_times.pop(cid, None)
                active_market_meta.pop(cid, None)

    # Clean up market_start_times and active_market_meta for cids no longer active
    for cid in list(market_start_times.keys()):
        if cid not in active_set:
            del market_start_times[cid]
            active_market_meta.pop(cid, None)

    # 2. Compute sector exposure (USD) and slots for actually_retained
    sector_exposure: Dict[str, float] = {}
    sector_slots: Dict[str, int] = {}
    for cid in actually_retained:
        meta = active_market_meta.get(cid) or {}
        tags = meta.get("tags") or []
        if not tags:
            tags = ["_unknown"]
        used = await inventory_state.get_used_dollars_for_market(cid)
        for tag in tags:
            sector_exposure[tag] = sector_exposure.get(tag, 0.0) + used
            sector_slots[tag] = sector_slots.get(tag, 0) + 1

    # 3. Add: Start new markets respecting sector limits
    slots_available = max(0, max_markets - len(actually_retained))

    for m in target_markets:
        cid = m.get("condition_id")
        if not cid or cid in active_set:
            continue
        if slots_available <= 0:
            break
        tags = m.get("tags") or []
        if not tags:
            tags = ["_unknown"]
        # Sector limits: slots and exposure
        would_exceed_slots = any(sector_slots.get(t, 0) >= max_slots_per_sector for t in tags)
        est_new_exposure = max_exposure_per_market
        would_exceed_exposure = any(
            (sector_exposure.get(t, 0.0) + est_new_exposure) > max_exposure_per_sector for t in tags
        )
        if would_exceed_slots or would_exceed_exposure:
            logger.debug(
                "[AutoRouter] Skipping %s: sector limit (slots=%s exposure=%s).",
                cid[:10], would_exceed_slots, would_exceed_exposure,
            )
            continue
        try:
            await start_market_making_impl(cid)
            active_set.add(cid)
            market_start_times[cid] = time.time()
            active_market_meta[cid] = {
                "end_date": m.get("end_date"),
                "tags": tags,
            }
            for t in tags:
                sector_exposure[t] = sector_exposure.get(t, 0.0) + est_new_exposure
                sector_slots[t] = sector_slots.get(t, 0) + 1
            slots_available -= 1
            logger.info(
                "[AutoRouter] Started market %s (ROI: %.4f, tags: %s).",
                cid[:10], m.get("daily_roi", 0), tags[:3],
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
