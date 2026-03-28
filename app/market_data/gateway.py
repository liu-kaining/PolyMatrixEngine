import asyncio
import json
import logging
import time
import httpx
import websockets
from typing import List, Set, Dict, Optional, Literal
from app.core.config import settings
from app.core.redis import redis_client

logger = logging.getLogger(__name__)


class LocalOrderbook:
    """
    Maintains a full local copy of the orderbook per asset_id.
    All WS deltas are merged into this state; every publish to Redis
    is a complete top-N snapshot so the QuotingEngine never sees partial data.
    """
    def __init__(self):
        self.books: Dict[str, Dict[str, Dict[str, float]]] = {}

    def seed(self, asset_id: str, bids: list, asks: list):
        """Seed the local book from a REST API full snapshot."""
        self.books[asset_id] = {"bids": {}, "asks": {}}
        for b in bids:
            self.books[asset_id]["bids"][str(b["price"])] = float(b["size"])
        for a in asks:
            self.books[asset_id]["asks"][str(a["price"])] = float(a["size"])

    def apply_event(self, data: dict) -> List[str]:
        """Apply a single WS event (book or price_change) and return updated asset_ids."""
        event_type = data.get("event_type")
        updated: Set[str] = set()

        if event_type == "book":
            asset_id = data.get("asset_id")
            if asset_id:
                self.books[asset_id] = {"bids": {}, "asks": {}}
                for b in data.get("bids", []):
                    self.books[asset_id]["bids"][str(b["price"])] = float(b["size"])
                for a in data.get("asks", []):
                    self.books[asset_id]["asks"][str(a["price"])] = float(a["size"])
                updated.add(asset_id)

        elif event_type == "price_change":
            for change in data.get("price_changes", []):
                asset_id = change.get("asset_id")
                if not asset_id:
                    continue
                if asset_id not in self.books:
                    self.books[asset_id] = {"bids": {}, "asks": {}}

                side = change.get("side", "").upper()
                price = str(change.get("price"))
                size = float(change.get("size", "0"))

                if side == "BUY":
                    book = self.books[asset_id]["bids"]
                elif side == "SELL":
                    book = self.books[asset_id]["asks"]
                else:
                    continue

                if abs(size) < 1e-9:
                    book.pop(price, None)
                else:
                    book[price] = size
                updated.add(asset_id)

        return list(updated)

    def snapshot(self, asset_id: str, depth: int = 5) -> Optional[dict]:
        """Return a complete top-N snapshot for the given asset."""
        if asset_id not in self.books:
            return None
        bids = self.books[asset_id]["bids"]
        asks = self.books[asset_id]["asks"]
        if not bids and not asks:
            return None
        top_bids = sorted(bids.items(), key=lambda x: float(x[0]), reverse=True)[:depth]
        top_asks = sorted(asks.items(), key=lambda x: float(x[0]))[:depth]
        if not top_bids or not top_asks:
            return None
        return {
            "asset_id": asset_id,
            "bids": [{"price": p, "size": s} for p, s in top_bids],
            "asks": [{"price": p, "size": s} for p, s in top_asks],
        }


class MarketDataGateway:
    def __init__(self):
        self.ws_url = settings.PM_WS_URL
        self.subscribed_markets: Set[str] = set()
        self.ws = None
        self.reconnect_delay = 1.0
        self.max_reconnect_delay = 60.0
        self.orderbook = LocalOrderbook()
        self.ping_task = None

    async def fetch_initial_snapshot(self, token_id: str):
        """
        Pull full orderbook via Polymarket CLOB REST API and seed the local book.
        Then publish the snapshot to Redis so the QuotingEngine fires immediately.
        """
        url = f"{settings.PM_API_URL}/book"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params={"token_id": token_id})
                resp.raise_for_status()
                data = resp.json()

            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if not bids and not asks:
                logger.warning(f"REST snapshot for {token_id[:8]} returned empty book.")
                return

            self.orderbook.seed(token_id, bids, asks)
            snap = self.orderbook.snapshot(token_id)
            if snap:
                await redis_client.set_state(f"ob:{token_id}", snap)
                await redis_client.publish(f"tick:{token_id}", snap)
                best_bid = snap["bids"][0]["price"] if snap["bids"] else "?"
                best_ask = snap["asks"][0]["price"] if snap["asks"] else "?"
                logger.info(f"Initial snapshot seeded for {token_id[:8]}: Bid={best_bid} Ask={best_ask} (bids={len(bids)} asks={len(asks)})")
        except httpx.HTTPStatusError as e:
            # 404 is common for illiquid / not-yet-listed books; treat as soft warning.
            if e.response is not None and e.response.status_code == 404:
                logger.warning(
                    f"Initial snapshot 404 for {token_id[:8]} – "
                    "orderbook not available via REST yet; waiting for WS ticks."
                )
            else:
                logger.error(f"Failed to fetch initial snapshot for {token_id[:8]}: {e}")
        except Exception as e:
            logger.error(f"Failed to fetch initial snapshot for {token_id[:8]}: {e}")

    async def connect(self):
        while True:
            connected_at = None
            try:
                logger.debug(f"Connecting to Polymarket WS: {self.ws_url}")
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                ) as ws:
                    self.ws = ws
                    connected_at = time.monotonic()
                    logger.info("Market WS connected.")

                    self.ping_task = asyncio.create_task(self._heartbeat())

                    # Always register on the market channel (even assets_ids=[]). If we skip this
                    # while AUTO_ROUTER finds no targets, the server often drops the socket ~10s idle.
                    await self._send_market_subscribe(mode="initial")

                    await self._listen()
                    raise RuntimeError("Market WS listen loop exited unexpectedly without exception.")

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(
                    "Market WS connection closed. code=%s reason=%r clean=%s",
                    getattr(e, "code", None),
                    getattr(e, "reason", "") or "",
                    isinstance(e, websockets.exceptions.ConnectionClosedOK),
                )
            except Exception as e:
                logger.exception(f"Market WS connect loop crashed: {e}")
            finally:
                if self.ping_task:
                    self.ping_task.cancel()
                    self.ping_task = None
                self.ws = None
                connected_for = 0.0
                if connected_at is not None:
                    connected_for = max(0.0, time.monotonic() - connected_at)
                if connected_for >= 60.0:
                    self.reconnect_delay = 1.0
                logger.warning(
                    f"Market WS reconnecting in {self.reconnect_delay:.1f}s "
                    f"(last_session={connected_for:.1f}s)."
                )
                await asyncio.sleep(self.reconnect_delay)
                self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)

    async def _heartbeat(self):
        """Polymarket expects text PING ~every 10s; send one immediately so we don't sit idle
        until the first sleep (RFC ping_interval is 20s here, too late for ~10s server idle cuts)."""
        try:
            while True:
                if self.ws is not None and not getattr(self.ws, "closed", False):
                    try:
                        await self.ws.send("PING")
                        logger.debug("Sent PING")
                    except Exception:
                        pass
                await asyncio.sleep(10)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f"Market WS heartbeat error: {e}")

    async def _send_market_subscribe(
        self, *, mode: Literal["initial", "update"] = "initial"
    ) -> None:
        if self.ws is None or getattr(self.ws, "closed", False):
            return
        sub_msg: Dict[str, object] = {
            "assets_ids": list(self.subscribed_markets),
            "type": "market",
            "custom_feature_enabled": True,
        }
        if mode == "update":
            sub_msg["operation"] = "subscribe"
        try:
            await self.ws.send(json.dumps(sub_msg))
            if mode == "initial":
                logger.debug(
                    "Market WS initial subscription sent (asset count=%s).",
                    len(self.subscribed_markets),
                )
        except Exception as e:
            logger.exception(f"Market WS subscribe send failed: {e}")
            raise

    async def subscribe(self, asset_ids: List[str]):
        self.subscribed_markets.update(asset_ids)
        if self.ws is not None and not getattr(self.ws, "closed", False):
            await self._send_market_subscribe(mode="update")
            logger.info(f"Subscribed to assets (count={len(self.subscribed_markets)})")

    async def _listen(self):
        while True:
            try:
                # Add strict receive timeout. If no message (tick or PONG) arrives for 30s,
                # the connection is a zombie. Force an exception to trigger reconnection.
                message = await asyncio.wait_for(self.ws.recv(), timeout=30.0)
                if isinstance(message, bytes):
                    message = message.decode("utf-8", errors="replace")
                
                if message == "PONG":
                    continue
                if message == "PING":
                    await self.ws.send("PONG")
                    continue

                try:
                    data = json.loads(message)
                except json.JSONDecodeError as e:
                    logger.exception(
                        f"Market WS JSON decode failed: {e}. Raw message (first 200 chars): {str(message)[:200]}"
                    )
                    continue
                items = data if isinstance(data, list) else [data]

                for item in items:
                    if not isinstance(item, dict):
                        continue
                    updated_ids = self.orderbook.apply_event(item)
                    for aid in updated_ids:
                        snap = self.orderbook.snapshot(aid)
                        if snap:
                            await redis_client.set_state(f"ob:{aid}", snap)
                            await redis_client.publish(f"tick:{aid}", snap)

            except asyncio.TimeoutError:
                logger.exception("Market WS silent drop detected (30s without message). Forcing reconnect...")
                raise
            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(
                    "Market WS recv closed. code=%s reason=%r",
                    getattr(e, "code", None),
                    getattr(e, "reason", "") or "",
                )
                raise
            except Exception as e:
                logger.exception(f"Error processing market WS message: {e}")
                raise


md_gateway = MarketDataGateway()
