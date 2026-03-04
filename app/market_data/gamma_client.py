import httpx
import logging
import json
from dataclasses import dataclass
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class GammaMarketInfo:
    yes_token_id: str
    no_token_id: str
    rewards_min_size: Optional[float] = None
    rewards_max_spread: Optional[float] = None


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

                rewards_min_size: Optional[float] = None
                rewards_max_spread: Optional[float] = None
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

                return GammaMarketInfo(
                    yes_token_id=tokens[0],
                    no_token_id=tokens[1],
                    rewards_min_size=rewards_min_size,
                    rewards_max_spread=rewards_max_spread,
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


gamma_client = GammaAPIClient()
