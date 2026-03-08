import asyncio
import json
import logging
from typing import Callable, Any, Dict, Optional

import redis.asyncio as redis
import websockets
from app.core.config import settings

logger = logging.getLogger(__name__)

class RedisManager:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(RedisManager, cls).__new__(cls)
            cls._instance.client = None
            cls._instance.pubsub = None
        return cls._instance

    async def connect(self):
        self.client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        self.pubsub = self.client.pubsub()
        await self.client.ping()
        logger.info("Connected to Redis.")

    async def disconnect(self):
        if self.pubsub:
            await self.pubsub.close()
        if self.client:
            await self.client.aclose()
        logger.info("Disconnected from Redis.")

    async def set_state(self, key: str, value: Any, ex: int = None):
        """High-frequency cache for Orderbook snapshots"""
        await self.client.set(key, json.dumps(value), ex=ex)
        
    async def get_state(self, key: str) -> Optional[Dict]:
        val = await self.client.get(key)
        return json.loads(val) if val else None

    async def delete_key(self, key: str) -> None:
        await self.client.delete(key)

    async def publish(self, channel: str, message: Any):
        """Publish market updates to strategies"""
        await self.client.publish(channel, json.dumps(message))

redis_client = RedisManager()
