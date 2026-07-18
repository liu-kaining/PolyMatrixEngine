import asyncio
import json
import logging
import math
import time
import httpx
import websockets
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Set

from app.core.config import settings
from app.core.redis import redis_client
from app.core.trading_safety import TradingMode, trading_safety
from app.market_data.integrity import (
    BookIntegrityError,
    assess_snapshot,
    evaluate_cursor,
    extract_exchange_timestamp,
    extract_sequence,
    validate_book_levels,
    validate_event_time,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrderbookEventResult:
    updated_asset_ids: List[str]
    invalid_assets: Dict[str, str]


class LocalOrderbook:
    """
    Maintains a full local copy of the orderbook per asset_id.
    All WS deltas are merged into this state; every publish to Redis
    is a complete top-N snapshot so the QuotingEngine never sees partial data.
    """
    def __init__(self):
        self.books: Dict[str, Dict[str, Dict[str, float]]] = {}
        self.metadata: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _to_maps(bids: list, asks: list) -> Dict[str, Dict[str, float]]:
        return {
            "bids": {str(level["price"]): float(level["size"]) for level in bids},
            "asks": {str(level["price"]): float(level["size"]) for level in asks},
        }

    def seed(
        self,
        asset_id: str,
        bids: list,
        asks: list,
        *,
        raw_metadata: Optional[dict] = None,
        source: str = "rest",
    ) -> None:
        """Seed the local book from a REST API full snapshot."""
        payload = raw_metadata or {}
        received_at = time.time()
        normalized_bids, normalized_asks = validate_book_levels(bids, asks)
        exchange_timestamp = extract_exchange_timestamp(payload)
        validate_event_time(
            exchange_timestamp,
            received_at=received_at,
            max_age_seconds=float(settings.MARKET_DATA_MAX_AGE_SEC),
            max_future_skew_seconds=float(settings.MARKET_DATA_MAX_FUTURE_SKEW_SEC),
        )
        self.books[asset_id] = self._to_maps(normalized_bids, normalized_asks)
        self.metadata[asset_id] = {
            "valid": True,
            "integrity_reason": "healthy",
            "received_at": received_at,
            "exchange_timestamp": exchange_timestamp,
            "sequence": extract_sequence(payload),
            "snapshot_id": payload.get("hash") or payload.get("snapshot_id"),
            "source": source,
            "resync_required": False,
        }

    def mark_invalid(
        self,
        asset_id: str,
        reason: str,
        *,
        resync_required: bool,
    ) -> None:
        current = dict(self.metadata.get(asset_id) or {})
        current.update(
            {
                "valid": False,
                "integrity_reason": str(reason),
                "resync_required": bool(
                    resync_required or current.get("resync_required")
                ),
            }
        )
        self.metadata[asset_id] = current

    def invalid_snapshot(self, asset_id: str, reason: Optional[str] = None) -> dict:
        metadata = dict(self.metadata.get(asset_id) or {})
        return {
            "asset_id": asset_id,
            "bids": [],
            "asks": [],
            "valid": False,
            "integrity_reason": str(
                reason or metadata.get("integrity_reason") or "orderbook is invalid"
            ),
            "received_at": metadata.get("received_at"),
            "exchange_timestamp": metadata.get("exchange_timestamp"),
            "sequence": metadata.get("sequence"),
            "source": metadata.get("source"),
        }

    def apply_event(self, data: dict) -> OrderbookEventResult:
        """Apply a WS event atomically and identify assets that must stop quoting."""
        event_type = data.get("event_type")
        updated: Set[str] = set()
        invalid: Dict[str, str] = {}
        received_at = time.time()

        try:
            sequence = extract_sequence(data)
            exchange_timestamp = extract_exchange_timestamp(data)
            validate_event_time(
                exchange_timestamp,
                received_at=received_at,
                max_age_seconds=float(settings.MARKET_DATA_MAX_AGE_SEC),
                max_future_skew_seconds=float(settings.MARKET_DATA_MAX_FUTURE_SKEW_SEC),
            )
        except BookIntegrityError as exc:
            asset_ids = {
                str(item.get("asset_id"))
                for item in data.get("price_changes", [])
                if isinstance(item, dict) and item.get("asset_id")
            }
            if data.get("asset_id"):
                asset_ids.add(str(data["asset_id"]))
            for asset_id in asset_ids:
                self.mark_invalid(asset_id, str(exc), resync_required=True)
                invalid[asset_id] = str(exc)
            return OrderbookEventResult([], invalid)

        if event_type == "book":
            asset_id = data.get("asset_id")
            if asset_id:
                asset_id = str(asset_id)
                previous = self.metadata.get(asset_id, {}).get("sequence")
                if sequence is not None and previous is not None and sequence <= previous:
                    return OrderbookEventResult([], {})
                try:
                    bids, asks = validate_book_levels(
                        data.get("bids", []), data.get("asks", [])
                    )
                except BookIntegrityError as exc:
                    self.mark_invalid(asset_id, str(exc), resync_required=True)
                    invalid[asset_id] = str(exc)
                else:
                    self.books[asset_id] = self._to_maps(bids, asks)
                    self.metadata[asset_id] = {
                        "valid": True,
                        "integrity_reason": "healthy",
                        "received_at": received_at,
                        "exchange_timestamp": exchange_timestamp,
                        "sequence": sequence,
                        "snapshot_id": data.get("hash") or data.get("snapshot_id"),
                        "source": "ws_book",
                        "resync_required": False,
                    }
                    updated.add(asset_id)

        elif event_type == "price_change":
            changes_by_asset: Dict[str, list] = {}
            for change in data.get("price_changes", []):
                if isinstance(change, dict) and change.get("asset_id"):
                    changes_by_asset.setdefault(str(change["asset_id"]), []).append(change)

            for asset_id, changes in changes_by_asset.items():
                metadata = self.metadata.get(asset_id) or {}
                if asset_id not in self.books:
                    reason = "delta received before a full book snapshot"
                    self.mark_invalid(asset_id, reason, resync_required=True)
                    invalid[asset_id] = reason
                    continue
                if metadata.get("resync_required"):
                    reason = "full snapshot is required before applying more deltas"
                    invalid[asset_id] = reason
                    continue

                cursor = evaluate_cursor(metadata.get("sequence"), sequence)
                if not cursor.accepted:
                    if cursor.requires_resync:
                        self.mark_invalid(asset_id, cursor.reason, resync_required=True)
                        invalid[asset_id] = cursor.reason
                    continue

                candidate = {
                    "bids": dict(self.books[asset_id]["bids"]),
                    "asks": dict(self.books[asset_id]["asks"]),
                }
                malformed_reason = None
                for change in changes:
                    side = str(change.get("side", "")).upper()
                    try:
                        price_value = float(change.get("price"))
                        size = float(change.get("size", "0"))
                    except (TypeError, ValueError):
                        malformed_reason = "delta price and size must be numeric"
                        break
                    if (
                        not math.isfinite(price_value)
                        or not math.isfinite(size)
                        or not 0.0 < price_value < 1.0
                        or size < 0
                    ):
                        malformed_reason = "delta price/size is outside safe bounds"
                        break
                    if side == "BUY":
                        book = candidate["bids"]
                    elif side == "SELL":
                        book = candidate["asks"]
                    else:
                        malformed_reason = "delta side must be BUY or SELL"
                        break
                    price = f"{price_value:.10g}"
                    if size <= 1e-9:
                        book.pop(price, None)
                    else:
                        book[price] = size

                if malformed_reason:
                    self.mark_invalid(asset_id, malformed_reason, resync_required=True)
                    invalid[asset_id] = malformed_reason
                    continue

                self.books[asset_id] = candidate
                next_metadata = {
                    "received_at": received_at,
                    "exchange_timestamp": exchange_timestamp,
                    "sequence": sequence,
                    "snapshot_id": data.get("hash") or data.get("snapshot_id"),
                    "source": "ws_delta",
                    "resync_required": False,
                }
                try:
                    validate_book_levels(
                        [
                            {"price": price, "size": size}
                            for price, size in candidate["bids"].items()
                        ],
                        [
                            {"price": price, "size": size}
                            for price, size in candidate["asks"].items()
                        ],
                    )
                except BookIntegrityError as exc:
                    next_metadata.update(
                        valid=False,
                        integrity_reason=str(exc),
                        resync_required=True,
                    )
                    invalid[asset_id] = str(exc)
                else:
                    next_metadata.update(valid=True, integrity_reason="healthy")
                    updated.add(asset_id)
                self.metadata[asset_id] = next_metadata

        return OrderbookEventResult(sorted(updated), invalid)

    def snapshot(self, asset_id: str, depth: int = 5) -> Optional[dict]:
        """Return a complete top-N snapshot for the given asset."""
        if asset_id not in self.books:
            return None
        metadata = self.metadata.get(asset_id) or {}
        if metadata.get("valid") is not True:
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
            "valid": True,
            "integrity_reason": "healthy",
            "received_at": metadata.get("received_at"),
            "exchange_timestamp": metadata.get("exchange_timestamp"),
            "sequence": metadata.get("sequence"),
            "snapshot_id": metadata.get("snapshot_id"),
            "source": metadata.get("source"),
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
        self.freshness_task = None
        self._last_invalid_notice: Dict[str, str] = {}

    def _strict_sequence_required(self) -> bool:
        return (
            trading_safety.mode is TradingMode.LIVE
            and bool(settings.MARKET_DATA_REQUIRE_SEQUENCE_LIVE)
        )

    def _strict_exchange_timestamp_required(self) -> bool:
        return (
            trading_safety.mode is TradingMode.LIVE
            and bool(settings.MARKET_DATA_REQUIRE_EXCHANGE_TIMESTAMP_LIVE)
        )

    def _snapshot_health(self, snapshot: Optional[dict]):
        return assess_snapshot(
            snapshot,
            max_age_seconds=float(settings.MARKET_DATA_MAX_AGE_SEC),
            require_sequence=self._strict_sequence_required(),
            require_exchange_timestamp=self._strict_exchange_timestamp_required(),
        )

    async def _publish_valid_snapshot(self, asset_id: str, snapshot: dict) -> None:
        self._last_invalid_notice.pop(asset_id, None)
        await redis_client.set_state(f"ob:{asset_id}", snapshot)
        await redis_client.publish(f"tick:{asset_id}", snapshot)

    async def _publish_invalid_snapshot(self, asset_id: str, reason: str) -> None:
        invalid = self.orderbook.invalid_snapshot(asset_id, reason)
        await redis_client.set_state(f"ob:{asset_id}", invalid)
        if self._last_invalid_notice.get(asset_id) != reason:
            await redis_client.publish(f"tick:{asset_id}", invalid)
            self._last_invalid_notice[asset_id] = reason

    async def _refresh_integrity_readiness(self) -> bool:
        if not self.subscribed_markets:
            trading_safety.set_readiness(
                "market_data_integrity", False, "no market assets are subscribed"
            )
            return False
        failures = []
        for asset_id in sorted(self.subscribed_markets):
            health = self._snapshot_health(self.orderbook.snapshot(asset_id))
            if not health.healthy:
                failures.append(f"{asset_id[:8]}:{health.reason}")
        ready = not failures
        trading_safety.set_readiness(
            "market_data_integrity",
            ready,
            "all subscribed books are fresh and valid"
            if ready
            else "; ".join(failures[:4]),
        )
        return ready

    async def _freshness_monitor(self) -> None:
        try:
            while True:
                await asyncio.sleep(1.0)
                for asset_id in sorted(self.subscribed_markets):
                    health = self._snapshot_health(self.orderbook.snapshot(asset_id))
                    if not health.healthy:
                        self.orderbook.mark_invalid(
                            asset_id,
                            health.reason,
                            resync_required=False,
                        )
                        await self._publish_invalid_snapshot(asset_id, health.reason)
                await self._refresh_integrity_readiness()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            trading_safety.set_readiness(
                "market_data_integrity", False, "freshness monitor failed"
            )
            trading_safety.halt(f"market-data freshness monitor failed: {exc}")
            logger.exception("Market-data freshness monitor crashed")
            raise

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

            self.orderbook.seed(
                token_id,
                bids,
                asks,
                raw_metadata=data,
                source="rest",
            )
            snap = self.orderbook.snapshot(token_id)
            if snap:
                await self._publish_valid_snapshot(token_id, snap)
                await self._refresh_integrity_readiness()
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
        except BookIntegrityError as e:
            self.orderbook.mark_invalid(token_id, str(e), resync_required=True)
            await self._publish_invalid_snapshot(token_id, str(e))
            await self._refresh_integrity_readiness()
            logger.error(f"Invalid initial snapshot for {token_id[:8]}: {e}")
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
                    trading_safety.set_readiness(
                        "market_stream", True, "market WebSocket connected"
                    )
                    logger.info("Market WS connected.")

                    self.ping_task = asyncio.create_task(self._heartbeat())
                    self.freshness_task = asyncio.create_task(self._freshness_monitor())

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
                if self.freshness_task:
                    self.freshness_task.cancel()
                    await asyncio.gather(self.freshness_task, return_exceptions=True)
                    self.freshness_task = None
                self.ws = None
                trading_safety.set_readiness(
                    "market_stream", False, "market WebSocket disconnected"
                )
                trading_safety.set_readiness(
                    "market_data_integrity", False, "market WebSocket disconnected"
                )
                for asset_id in sorted(self.subscribed_markets):
                    reason = "market WebSocket disconnected; full snapshot required"
                    self.orderbook.mark_invalid(
                        asset_id, reason, resync_required=True
                    )
                    try:
                        await self._publish_invalid_snapshot(asset_id, reason)
                    except Exception:
                        logger.exception(
                            "Failed to publish disconnect invalidation for %s",
                            asset_id[:8],
                        )
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
        trading_safety.set_readiness(
            "market_data_integrity", False, "new subscriptions await valid snapshots"
        )
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
                    result = self.orderbook.apply_event(item)
                    for aid, reason in result.invalid_assets.items():
                        await self._publish_invalid_snapshot(aid, reason)
                    for aid in result.updated_asset_ids:
                        snap = self.orderbook.snapshot(aid)
                        if snap:
                            await self._publish_valid_snapshot(aid, snap)
                    if result.invalid_assets or result.updated_asset_ids:
                        await self._refresh_integrity_readiness()

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
