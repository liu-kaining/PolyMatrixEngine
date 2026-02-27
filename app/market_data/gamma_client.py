import httpx
import logging
import json
from typing import Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)

class GammaAPIClient:
    def __init__(self):
        self.base_url = "https://gamma-api.polymarket.com"
        
    async def get_market_tokens_by_condition_id(self, condition_id: str) -> Optional[Tuple[str, str]]:
        """
        Fetches market details from Gamma API and extracts YES and NO token IDs.
        Returns: (yes_token_id, no_token_id) or None if not found/error.
        """
        async with httpx.AsyncClient() as client:
            try:
                # Prefer the documented `condition_ids` filter which returns CLOB token ids.
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
                # 'clobTokenIds' is a stringified JSON array like '["token1", "token2"]'
                clob_tokens_str = market_data.get("clobTokenIds", "[]")
                tokens = json.loads(clob_tokens_str)
                
                if len(tokens) >= 2:
                    # By convention in Polymarket binary markets:
                    # Index 0 is YES, Index 1 is NO
                    return tokens[0], tokens[1]
                else:
                    logger.warning(f"Unexpected tokens format for market {condition_id}: {tokens}")
                    return None
                    
            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP error fetching market {condition_id}: {e}")
            except Exception as e:
                logger.error(f"Error fetching market {condition_id}: {e}")
        return None

gamma_client = GammaAPIClient()
