"""
Auto-Router (V7.2): Portfolio Manager — rewards-market radar via official CLOB rewards API,
bonus floor, competition-aware scoring, capital-aware rank (binary vs categorical exposure caps),
event horizon and sector limits.
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
from app.core.exposure_limits import exposure_cap_usd_for_outcome_count
from app.core.redis import redis_client
from app.core.market_lifecycle import start_market_making_impl, get_active_router_markets
from app.core.inventory_state import inventory_state
from app.market_data.gamma_client import gamma_client

logger = logging.getLogger(__name__)

# Per-market start time for min-hold enforcement (rewards threshold)
market_start_times: Dict[str, float] = {}
# Per-market metadata (endDate, tags) for event horizon and sector limits
active_market_meta: Dict[str, dict] = {}


def _router_start_redis_key(cid: str) -> str:
    return f"router:start_time:{cid}"


# Stale-key safety: crash before explicit delete still expires (30d).
ROUTER_REDIS_STATE_TTL_SEC = 2592000


async def _persist_router_start_time_to_redis(cid: str, ts: float) -> None:
    try:
        await redis_client.set_state(
            _router_start_redis_key(cid), {"started_at": ts}, ex=ROUTER_REDIS_STATE_TTL_SEC
        )
    except Exception as e:
        logger.warning("[AutoRouter] Redis persist start_time failed %s: %s", cid[:10], e)


async def _load_router_start_time_from_redis(cid: str) -> Optional[float]:
    try:
        data = await redis_client.get_state(_router_start_redis_key(cid))
        if data and "started_at" in data:
            return float(data["started_at"])
    except Exception as e:
        logger.debug("[AutoRouter] Redis load start_time %s: %s", cid[:10], e)
    return None


async def _delete_router_start_time_redis(cid: str) -> None:
    try:
        client = getattr(redis_client, "client", None)
        if client is not None:
            await client.delete(_router_start_redis_key(cid))
    except Exception as e:
        logger.debug("[AutoRouter] Redis delete start_time %s: %s", cid[:10], e)


# Global health metrics for the dashboard/API
router_state = {
    "last_scan_ts": 0.0,
    "last_scan_error": None,
    "top_targets": [],
    "active_count": 0,
}

class RadarScanIncomplete(Exception):
    """Raised when rewards radar scan fails, forcing the router to fail-closed."""
    pass

# Official rewards API (CLOB)
REWARDS_MARKETS_API_URL = "https://clob.polymarket.com/rewards/markets/multi"
REWARDS_PAGE_SIZE = 500
REWARDS_REQUEST_TIMEOUT = 25.0

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
    category_raw = (m.get("category") or m.get("subCategory") or m.get("tag_slug") or "")
    slug = (m.get("slug") or m.get("market_slug") or m.get("event_slug") or "").lower()
    question_lower = (m.get("question") or "").lower()
    haystack = " ".join([category_raw, " ".join(str(x) for x in tags_list), slug]).lower()
    if any(kw in haystack for kw in SPORTS_BLACKLIST):
        return True
    if any(kw in question_lower for kw in QUESTION_BLACKLIST):
        return True
    return False


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


def _within_event_horizon(end_date: Optional[datetime], hours: float) -> bool:
    """True if end_date is within `hours` of now OR HAS ALREADY PASSED."""
    if end_date is None:
        return False
    try:
        now = datetime.now(timezone.utc)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        delta_hours = (end_date - now).total_seconds() / 3600.0
        return delta_hours <= hours
    except Exception:
        return False


def _parse_rewards_rate_from_rewards_api(m: dict) -> float:
    """
    Parse daily reward rate from official rewards endpoint.
    Priority: rewards_config[0].rate_per_day, then common fallbacks.
    """
    raw: Any = None
    rc = m.get("rewards_config") or m.get("rewardsConfig") or []
    if isinstance(rc, list) and rc and isinstance(rc[0], dict):
        raw = rc[0].get("rate_per_day")
    if raw is None:
        raw = m.get("rate_per_day")
    if raw is None:
        raw = m.get("rewardsDailyRate")
    try:
        return max(0.0, float(raw or 0.0))
    except (ValueError, TypeError):
        return 0.0


def _parse_rewards_min_size_from_rewards_api(m: dict) -> float:
    raw = m.get("rewards_min_size")
    if raw is None:
        raw = m.get("rewardsMinSize")
    try:
        return max(0.0, float(raw or 0.0))
    except (ValueError, TypeError):
        return 0.0


def _parse_rewards_spread_from_rewards_api(m: dict) -> Optional[float]:
    """
    Returns spread in price units [0,1] when possible.
    Endpoint often returns cents-like values (e.g. 3.5), convert to 0.035.
    """
    raw = m.get("rewards_max_spread")
    if raw is None:
        raw = m.get("rewardsMaxSpread")
    try:
        v = float(raw)
    except (ValueError, TypeError):
        return None
    if v <= 0:
        return None
    return v / 100.0 if v > 1.0 else v


def _outcome_count_from_gamma_market_dict(raw: Optional[dict]) -> int:
    """Number of CLOB outcome tokens (Gamma clobTokenIds). Defaults to 2 if missing."""
    if not raw:
        return 2
    clob = raw.get("clobTokenIds") or raw.get("clob_token_ids")
    tokens: Any = None
    if isinstance(clob, str):
        try:
            tokens = json.loads(clob)
        except Exception:
            return 2
    elif isinstance(clob, list):
        tokens = clob
    else:
        return 2
    if not isinstance(tokens, list) or len(tokens) < 2:
        return 2
    return len(tokens)


def _router_rank_score(
    rate: float,
    r_min: float,
    competition_penalty: float,
    exposure_cap_usd: float,
    cap_ref_binary: float,
) -> float:
    """
    Capital-aware score for fair ranking: same pool/competition as before, scaled by
    deployable per-market USD cap (categorical uses MAX_EXPOSURE_CATEGORICAL vs binary MAX_EXPOSURE_PER_MARKET).
    """
    daily_roi = rate / r_min if r_min > 0 else 0.0
    base = (rate * daily_roi) / max(competition_penalty, 1e-9)
    ref = max(float(cap_ref_binary), 1e-6)
    capital_scale = max(0.0, float(exposure_cap_usd)) / ref
    return base * capital_scale


def _parse_competitiveness(m: dict) -> float:
    raw = m.get("market_competitiveness")
    if raw is None:
        raw = m.get("competitiveness")
    try:
        return max(0.0, float(raw or 0.0))
    except (ValueError, TypeError):
        return 0.0


async def _fetch_gamma_meta_for_conditions(condition_ids: List[str]) -> Dict[str, dict]:
    """Batch fetch Gamma metadata with chunking (gamma_client batch call is capped to 50 ids)."""
    result: Dict[str, dict] = {}
    if not condition_ids:
        return result
    for i in range(0, len(condition_ids), 50):
        chunk = condition_ids[i:i + 50]
        batch = await gamma_client.get_markets_batch(chunk)
        if batch:
            result.update(batch)
    return result


async def _radar_scan() -> List[dict]:
    """
    Fetch rewards markets from official CLOB rewards API (cursor pagination),
    apply reward/base-size/blacklist filters, batch Gamma for outcome_count + tags/endDate,
    score with capital-aware ranking (categorical uses MAX_EXPOSURE_CATEGORICAL vs binary MAX_EXPOSURE_PER_MARKET),
    then shortlist and apply event-horizon.
    """
    base_order_size = max(5.0, float(getattr(settings, "BASE_ORDER_SIZE", 10.0)))
    max_markets = int(getattr(settings, "AUTO_ROUTER_MAX_MARKETS", 4)) * 5
    top_n = int(getattr(settings, "AUTO_ROUTER_MAX_MARKETS", 4))
    event_horizon_hours = float(getattr(settings, "EVENT_HORIZON_HOURS", 24.0))
    min_pool = float(getattr(settings, "AUTO_ROUTER_MIN_REWARD_POOL", 50.0))
    all_markets: List[dict] = []
    seen: Set[str] = set()

    logger.info(
        "[AutoRouter] Radar scan: paginating full CLOB rewards catalog (500/page). "
        "Quoting engines start only after this finishes — expect tens of seconds to a few minutes."
    )

    try:
        async with httpx.AsyncClient(timeout=REWARDS_REQUEST_TIMEOUT) as client:
            cursor: Optional[str] = None
            page_idx = 0
            while True:
                page_idx += 1
                params: Dict[str, Any] = {
                    "page_size": REWARDS_PAGE_SIZE,
                    "position": "DESC",
                    "order_by": "rate_per_day",
                }
                if cursor:
                    params["next_cursor"] = cursor
                resp = await client.get(REWARDS_MARKETS_API_URL, params=params, timeout=REWARDS_REQUEST_TIMEOUT)
                resp.raise_for_status()
                payload = resp.json() or {}
                page = payload.get("data") or []
                for m in page:
                    cid = m.get("condition_id") or m.get("conditionId")
                    if not cid or cid in seen:
                        continue
                    if _blacklisted(m):
                        continue

                    rate = _parse_rewards_rate_from_rewards_api(m)
                    r_min = _parse_rewards_min_size_from_rewards_api(m)
                    r_spread = _parse_rewards_spread_from_rewards_api(m)
                    if rate < min_pool:
                        continue
                    if r_min <= 0:
                        continue
                    if r_min > base_order_size:
                        continue

                    comp = _parse_competitiveness(m)
                    daily_roi = rate / r_min if r_min > 0 else 0.0
                    seen.add(cid)
                    all_markets.append({
                        "condition_id": cid,
                        "daily_roi": daily_roi,
                        "rewards_min_size": r_min,
                        "rewards_max_spread": r_spread,
                        "reward_rate_per_day": rate,
                        "market_competitiveness": comp,
                        "outcome_count": 2,
                        "end_date": _parse_end_date(m),
                        "tags": _parse_tags(m),
                    })

                cursor = payload.get("next_cursor")
                if page_idx == 1 or page_idx % 5 == 0:
                    logger.info(
                        "[AutoRouter] Radar progress: page=%d pass-filter candidates=%d (still paging…)",
                        page_idx,
                        len(all_markets),
                    )
                if not cursor or cursor == "LTE=":
                    break
            logger.info(
                "[AutoRouter] Radar pagination done: %d pages, %d pass-filter candidates.",
                page_idx,
                len(all_markets),
            )
    except Exception as e:
        raise RadarScanIncomplete(f"Rewards API scan failed: {e}") from e

    if not all_markets:
        return []

    cap_ref_binary = max(float(getattr(settings, "MAX_EXPOSURE_PER_MARKET", 50.0)), 1e-6)
    all_cids = [m["condition_id"] for m in all_markets if m.get("condition_id")]
    logger.info(
        "[AutoRouter] Gamma batch for %d pass-filter candidates (outcome_count, tags, endDate, rank score)...",
        len(all_cids),
    )
    gamma_meta_all = await _fetch_gamma_meta_for_conditions(all_cids)

    for m in all_markets:
        raw = gamma_meta_all.get(m["condition_id"])
        oc = _outcome_count_from_gamma_market_dict(raw) if raw else 2
        m["outcome_count"] = oc
        exposure_cap = exposure_cap_usd_for_outcome_count(oc)
        m["router_exposure_cap_est"] = exposure_cap
        if raw:
            end_date = _parse_end_date(raw)
            if end_date is not None:
                m["end_date"] = end_date
            tags = _parse_tags(raw)
            if tags:
                m["tags"] = tags
        comp = float(m.get("market_competitiveness", 0.0) or 0.0)
        competition_penalty = max(1.0, math.log1p(max(comp, 0.0)))
        rate = float(m.get("reward_rate_per_day", 0.0) or 0.0)
        r_min = float(m.get("rewards_min_size", 0.0) or 0.0)
        m["score"] = _router_rank_score(
            rate, r_min, competition_penalty, exposure_cap, cap_ref_binary
        )
        if r_min > 0:
            m["daily_roi"] = rate / r_min

    all_markets.sort(key=lambda x: x["score"], reverse=True)
    shortlist_count = max(max_markets, top_n * 10)
    shortlisted = all_markets[:shortlist_count]

    filtered = [
        m for m in shortlisted if not _within_event_horizon(m.get("end_date"), event_horizon_hours)
    ]
    filtered.sort(key=lambda x: x["score"], reverse=True)
    return filtered[:top_n]


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
    target_ids = {m["condition_id"] for m in target_markets}
    now = time.time()

    # Build target lookup for metadata
    target_by_cid: Dict[str, dict] = {m["condition_id"]: m for m in target_markets if m.get("condition_id")}

    # 🛠️ 存量盘口：内存缺失时从 Redis 恢复 min_hold 起点，否则写入 Redis
    for cid in active_set:
        if cid not in market_start_times:
            ts_r = await _load_router_start_time_from_redis(cid)
            if ts_r is not None:
                market_start_times[cid] = ts_r
                logger.info("[AutoRouter] Restored market start time from Redis: %s", cid[:10])
            else:
                market_start_times[cid] = now
                await _persist_router_start_time_to_redis(cid, now)
                logger.info(
                    "[AutoRouter] ⚡ 存量盘口无 Redis 记录，已用当前时间初始化并写入 Redis: %s",
                    cid[:10],
                )

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
            await _delete_router_start_time_redis(cid)

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
                await _delete_router_start_time_redis(cid)

    # Clean up market_start_times and active_market_meta for cids no longer active
    for cid in list(market_start_times.keys()):
        if cid not in active_set:
            del market_start_times[cid]
            active_market_meta.pop(cid, None)
            await _delete_router_start_time_redis(cid)

    # 2. Compute sector exposure (USD) and slots for actually_retained
    sector_exposure: Dict[str, float] = {}
    sector_slots: Dict[str, int] = {}
    for cid in actually_retained:
        meta = active_market_meta.get(cid) or {}
        tags = meta.get("tags") or []
        if not tags:
            tags = [f"_unknown_{cid[:8]}"]
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
            tags = [f"_unknown_{cid[:8]}"]
        # Sector limits: slots and exposure
        would_exceed_slots = any(sector_slots.get(t, 0) >= max_slots_per_sector for t in tags)
        oc = int(m.get("outcome_count") or 2)
        est_new_exposure = float(exposure_cap_usd_for_outcome_count(oc))
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
            t_start = time.time()
            market_start_times[cid] = t_start
            await _persist_router_start_time_to_redis(cid, t_start)
            active_market_meta[cid] = {
                "end_date": m.get("end_date"),
                "tags": tags,
                "outcome_count": oc,
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
            router_state["top_targets"] = [
                {
                    "cid": m["condition_id"],
                    "roi": m["daily_roi"],
                    "outcome_count": m.get("outcome_count", 2),
                    "router_exposure_cap_est": m.get("router_exposure_cap_est"),
                    "score": m.get("score"),
                }
                for m in target_list
            ]
            
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
