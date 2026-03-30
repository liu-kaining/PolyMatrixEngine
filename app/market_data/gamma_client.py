import httpx
import logging
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class GammaMarketInfo:
    yes_token_id: str
    no_token_id: str
    outcome_count: int = 2  # len(clobTokenIds); >2 => categorical multi-choice
    rewards_min_size: Optional[float] = None
    rewards_max_spread: Optional[float] = None
    reward_rate_per_day: Optional[float] = None
    end_date: Optional[datetime] = None      # Resolution time (for event horizon)
    tags: List[str] = field(default_factory=list)  # Sector/category tags
    category: Optional[str] = None
    liquidity: float = 0.0                   # For volatility penalty scoring


class GammaAPIClient:
    def __init__(self):
        self.base_url = "https://gamma-api.polymarket.com"

    async def get_market_info(self, condition_id: str) -> Optional[GammaMarketInfo]:
        """
        Fetches market details from Gamma API: token IDs + rewards params.
        rewardsMinSize is in shares; rewardsMaxSpread is in cents (e.g. 4.5 → 0.045 price).
        """
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.base_url}/markets",
                    params={"condition_ids": condition_id},
                )
                response.raise_for_status()
                data = response.json()

                if not data or len(data) == 0:
                    logger.warning(f"Market not found for condition_id {condition_id}")
                    return None

                market_data = data[0]
                clob_tokens_str = market_data.get("clobTokenIds", "[]")
                tokens = json.loads(clob_tokens_str)

                if len(tokens) < 2:
                    logger.warning(f"Unexpected tokens format for market {condition_id}: {tokens}")
                    return None

                outcome_count = len(tokens)

                rewards_min_size: Optional[float] = None
                rewards_max_spread: Optional[float] = None
                reward_rate_per_day: Optional[float] = None
                try:
                    raw_min = market_data.get("rewardsMinSize")
                    if raw_min is not None:
                        rewards_min_size = float(raw_min)
                except (ValueError, TypeError):
                    pass
                try:
                    raw_spread = market_data.get("rewardsMaxSpread")
                    if raw_spread is not None:
                        # Gamma returns spread in cents (e.g. 4.5 = 4.5¢ = 0.045 price)
                        rewards_max_spread = float(raw_spread) / 100.0
                except (ValueError, TypeError):
                    pass
                try:
                    raw_rate = market_data.get("rewardsDailyRate")
                    if raw_rate is None:
                        cr = market_data.get("clobRewards") or []
                        if isinstance(cr, list) and len(cr) > 0 and isinstance(cr[0], dict):
                            raw_rate = cr[0].get("rewardsDailyRate")
                    if raw_rate is not None:
                        reward_rate_per_day = float(raw_rate)
                except (ValueError, TypeError):
                    pass

                # Parse endDate (for event horizon)
                end_date: Optional[datetime] = None
                for key in ("endDate", "end_date", "endDateIso"):
                    raw = market_data.get(key)
                    if raw:
                        try:
                            if isinstance(raw, str):
                                s = raw.replace("Z", "+00:00").replace("z", "+00:00")
                                end_date = datetime.fromisoformat(s)
                            elif hasattr(raw, "timestamp"):
                                end_date = raw
                            break
                        except (ValueError, TypeError):
                            pass

                # Parse tags/category
                tags_list: List[str] = []
                tags_raw = market_data.get("tags")
                if tags_raw:
                    if isinstance(tags_raw, str):
                        try:
                            parsed = json.loads(tags_raw)
                            tags_list = parsed if isinstance(parsed, list) else [parsed] if isinstance(parsed, str) else []
                        except Exception:
                            tags_list = [t.strip() for t in tags_raw.replace(";", ",").split(",") if t.strip()]
                    elif isinstance(tags_raw, list):
                        tags_list = [str(t) for t in tags_raw]
                category = market_data.get("category") or market_data.get("subCategory")
                if category and category not in tags_list:
                    tags_list.append(str(category))

                # Parse liquidity
                liquidity = 0.0
                for key in ("liquidity", "liquidityNum", "volume", "volumeNum"):
                    raw = market_data.get(key)
                    if raw is not None:
                        try:
                            liquidity = float(raw)
                            break
                        except (ValueError, TypeError):
                            pass

                return GammaMarketInfo(
                    yes_token_id=tokens[0],
                    no_token_id=tokens[1],
                    outcome_count=outcome_count,
                    rewards_min_size=rewards_min_size,
                    rewards_max_spread=rewards_max_spread,
                    reward_rate_per_day=reward_rate_per_day,
                    end_date=end_date,
                    tags=tags_list,
                    category=category,
                    liquidity=liquidity,
                )

            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP error fetching market {condition_id}: {e}")
            except Exception as e:
                logger.error(f"Error fetching market {condition_id}: {e}")
        return None

    async def get_market_tokens_by_condition_id(self, condition_id: str) -> Optional[Tuple[str, str]]:
        """Legacy helper: returns (yes_token_id, no_token_id) or None."""
        info = await self.get_market_info(condition_id)
        if info:
            return info.yes_token_id, info.no_token_id
        return None

    async def get_markets_batch(self, condition_ids: List[str]) -> Dict[str, dict]:
        """Fetch raw market dicts for multiple condition_ids. Returns cid -> raw market dict."""
        if not condition_ids:
            return {}
        result: Dict[str, dict] = {}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                # Gamma API accepts condition_ids as comma-separated
                ids_param = ",".join(condition_ids[:50])  # Limit batch size
                resp = await client.get(
                    f"{self.base_url}/markets",
                    params={"condition_ids": ids_param},
                )
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    for m in data:
                        cid = m.get("conditionId")
                        if cid:
                            result[cid] = m
        except Exception as e:
            logger.warning("get_markets_batch failed: %s", e)
        return result


gamma_client = GammaAPIClient()
