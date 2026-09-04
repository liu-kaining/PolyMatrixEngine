import asyncio
import hashlib
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

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
from app.oms.polymarket_v2 import (
    PolymarketV2PublicAdapter,
    normalize_sdk_stream_event,
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
            "tick_size": payload.get("tick_size"),
            "min_order_size": payload.get("min_order_size"),
            "neg_risk": payload.get("neg_risk"),
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
            "snapshot_id": metadata.get("snapshot_id"),
            "tick_size": metadata.get("tick_size"),
            "min_order_size": metadata.get("min_order_size"),
            "neg_risk": metadata.get("neg_risk"),
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
            # An auxiliary trade print is not an order-book mutation. A malformed
            # or stale print may be ignored by the conservative paper simulator,
            # but must never poison an otherwise fresh executable book.
            if event_type == "last_trade_price":
                return OrderbookEventResult([], {})
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
                previous_metadata = self.metadata.get(asset_id, {})
                previous = previous_metadata.get("sequence")
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
                        "tick_size": data.get("tick_size")
                        or previous_metadata.get("tick_size"),
                        "min_order_size": data.get("min_order_size")
                        or previous_metadata.get("min_order_size"),
                        "neg_risk": (
                            data.get("neg_risk")
                            if isinstance(data.get("neg_risk"), bool)
                            else previous_metadata.get("neg_risk")
                        ),
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
                    "snapshot_id": (
                        data.get("hash")
                        or data.get("snapshot_id")
                        or next(
                            (
                                change.get("hash")
                                for change in changes
                                if isinstance(change, dict) and change.get("hash")
                            ),
                            None,
                        )
                        or metadata.get("snapshot_id")
                    ),
                    "source": "ws_delta",
                    "resync_required": False,
                    "tick_size": metadata.get("tick_size"),
                    "min_order_size": metadata.get("min_order_size"),
                    "neg_risk": metadata.get("neg_risk"),
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

        elif event_type == "tick_size_change":
            asset_id = str(data.get("asset_id") or "")
            metadata = self.metadata.get(asset_id) or {}
            try:
                tick_size = float(data.get("new_tick_size"))
            except (TypeError, ValueError):
                tick_size = 0.0
            if (
                not asset_id
                or asset_id not in self.books
                or not math.isfinite(tick_size)
                or tick_size <= 0
                or tick_size >= 1
            ):
                if asset_id:
                    reason = "invalid tick_size_change event"
                    self.mark_invalid(asset_id, reason, resync_required=True)
                    invalid[asset_id] = reason
            else:
                metadata.update(
                    tick_size=tick_size,
                    constraint_received_at=received_at,
                    constraint_exchange_timestamp=exchange_timestamp,
                    constraint_source="ws_tick_size_change",
                    valid=True,
                    integrity_reason="healthy",
                )
                self.metadata[asset_id] = metadata
                updated.add(asset_id)

        elif event_type == "last_trade_price":
            asset_id = str(data.get("asset_id") or "")
            metadata = self.metadata.get(asset_id) or {}
            try:
                trade_price = float(data.get("price"))
                trade_size = float(data.get("size"))
            except (TypeError, ValueError):
                trade_price = trade_size = 0.0
            exchange_trade_id = str(
                data.get("id")
                or data.get("trade_id")
                or data.get("transaction_hash")
                or ""
            )
            if (
                not asset_id
                or asset_id not in self.books
                or not 0 < trade_price < 1
                or trade_size <= 0
            ):
                # Size and transaction hash are optional in the public contract.
                # Without a positive size the print cannot drive a simulated fill.
                return OrderbookEventResult([], {})
            else:
                if exchange_trade_id:
                    trade_id = exchange_trade_id
                else:
                    stable = "|".join(
                        str(data.get(key) or "")
                        for key in (
                            "market",
                            "asset_id",
                            "price",
                            "size",
                            "side",
                            "timestamp",
                        )
                    )
                    trade_id = hashlib.sha256(stable.encode("utf-8")).hexdigest()
                metadata.update(
                    last_trade_price=trade_price,
                    last_trade_size=trade_size,
                    last_trade_id=trade_id,
                    last_trade_received_at=received_at,
                    last_trade_exchange_timestamp=exchange_timestamp,
                )
                self.metadata[asset_id] = metadata
                updated.add(asset_id)

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
            "tick_size": metadata.get("tick_size"),
            "min_order_size": metadata.get("min_order_size"),
            "neg_risk": metadata.get("neg_risk"),
            "last_trade_price": metadata.get("last_trade_price"),
            "last_trade_size": metadata.get("last_trade_size"),
            "last_trade_id": metadata.get("last_trade_id"),
        }


class MarketDataGateway:
    def __init__(self):
        self.subscribed_markets: Set[str] = set()
        self.orderbook = LocalOrderbook()
        self.freshness_task = None
        self._last_invalid_notice: Dict[str, str] = {}
        self._last_rest_resync_at = 0.0
        self._sdk_adapter: Optional[PolymarketV2PublicAdapter] = None
        self._adapter_lock = asyncio.Lock()
        self._subscription_event = asyncio.Event()
        self._sdk_subscribed: Set[str] = set()
        self._stream_handles: list[Any] = []
        self._reader_tasks: Set[asyncio.Task] = set()
        self._last_stream_open = False

    async def _ensure_adapter(self) -> PolymarketV2PublicAdapter:
        async with self._adapter_lock:
            if self._sdk_adapter is None:
                self._sdk_adapter = PolymarketV2PublicAdapter.create()
            return self._sdk_adapter

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
            require_snapshot_id=(
                trading_safety.mode is TradingMode.LIVE
                and bool(settings.MARKET_DATA_REQUIRE_SNAPSHOT_ID_LIVE)
            ),
        )

    async def _publish_valid_snapshot(self, asset_id: str, snapshot: dict) -> None:
        self._last_invalid_notice.pop(asset_id, None)
        await redis_client.set_state(
            f"market_constraints:{asset_id}",
            {
                "tick_size": snapshot.get("tick_size"),
                "min_order_size": snapshot.get("min_order_size"),
                "neg_risk": snapshot.get("neg_risk"),
                "snapshot_id": snapshot.get("snapshot_id"),
                "updated_at": snapshot.get("received_at"),
            },
        )
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
                now = time.time()
                stream_open = bool(
                    self._sdk_adapter is not None
                    and self._sdk_adapter.market_stream_is_open()
                )
                if stream_open != self._last_stream_open:
                    self._last_stream_open = stream_open
                    trading_safety.set_readiness(
                        "market_stream",
                        stream_open,
                        "SDK market stream connected"
                        if stream_open
                        else "SDK market stream disconnected",
                    )
                    if not stream_open:
                        for asset_id in sorted(self.subscribed_markets):
                            reason = "SDK market stream disconnected; full snapshot required"
                            self.orderbook.mark_invalid(
                                asset_id, reason, resync_required=True
                            )
                            await self._publish_invalid_snapshot(asset_id, reason)
                    elif self.subscribed_markets:
                        # SDK reconnect is transparent to its iterator. Re-seed from
                        # authoritative REST before trusting further deltas.
                        await asyncio.gather(
                            *(
                                self.fetch_initial_snapshot(asset)
                                for asset in sorted(self.subscribed_markets)
                            ),
                            return_exceptions=True,
                        )
                resync_interval = max(
                    5.0, float(settings.MARKET_DATA_REST_RESYNC_SEC)
                )
                if (
                    self.subscribed_markets
                    and now - self._last_rest_resync_at >= resync_interval
                ):
                    self._last_rest_resync_at = now
                    await asyncio.gather(
                        *(self.fetch_initial_snapshot(asset) for asset in sorted(self.subscribed_markets)),
                        return_exceptions=True,
                    )
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
        try:
            adapter = await self._ensure_adapter()
            data = await adapter.get_order_book(token_id)

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
        except BookIntegrityError as e:
            self.orderbook.mark_invalid(token_id, str(e), resync_required=True)
            await self._publish_invalid_snapshot(token_id, str(e))
            await self._refresh_integrity_readiness()
            logger.error(f"Invalid initial snapshot for {token_id[:8]}: {e}")
        except Exception as e:
            logger.error(f"Failed to fetch initial snapshot for {token_id[:8]}: {e}")

    async def connect(self):
        """Own SDK stream handles; the SDK manages PING/PONG and reconnects."""
        await self._ensure_adapter()
        self.freshness_task = asyncio.create_task(self._freshness_monitor())
        try:
            while True:
                await self._subscription_event.wait()
                self._subscription_event.clear()
                new_assets = sorted(self.subscribed_markets - self._sdk_subscribed)
                if not new_assets:
                    continue
                handle = await self._sdk_adapter.subscribe_market(new_assets)
                self._stream_handles.append(handle)
                self._sdk_subscribed.update(new_assets)
                self._last_stream_open = self._sdk_adapter.market_stream_is_open()
                trading_safety.set_readiness(
                    "market_stream",
                    self._last_stream_open,
                    "SDK market stream connected"
                    if self._last_stream_open
                    else "SDK market subscription did not open",
                )
                task = asyncio.create_task(self._consume_sdk_stream(handle, new_assets))
                self._reader_tasks.add(task)
                task.add_done_callback(self._reader_tasks.discard)
        finally:
            if self.freshness_task is not None:
                self.freshness_task.cancel()
                await asyncio.gather(self.freshness_task, return_exceptions=True)
                self.freshness_task = None
            for task in list(self._reader_tasks):
                task.cancel()
            await asyncio.gather(*self._reader_tasks, return_exceptions=True)
            self._reader_tasks.clear()
            await asyncio.gather(
                *(handle.close() for handle in self._stream_handles),
                return_exceptions=True,
            )
            self._stream_handles.clear()
            adapter, self._sdk_adapter = self._sdk_adapter, None
            if adapter is not None:
                try:
                    await adapter.close()
                except Exception as exc:
                    logger.debug("SDK market adapter close failed: %s", exc)
            self._sdk_subscribed.clear()
            self._last_stream_open = False
            trading_safety.set_readiness(
                "market_stream", False, "SDK market stream stopped"
            )
            trading_safety.set_readiness(
                "market_data_integrity", False, "SDK market stream stopped"
            )

    async def subscribe(self, asset_ids: List[str]):
        self.subscribed_markets.update(asset_ids)
        trading_safety.set_readiness(
            "market_data_integrity", False, "new subscriptions await valid snapshots"
        )
        self._subscription_event.set()
        logger.info("Requested SDK market subscriptions (count=%d)", len(asset_ids))

    async def _consume_sdk_stream(self, handle: Any, asset_ids: list[str]) -> None:
        last_dropped = int(getattr(handle, "dropped", 0) or 0)
        try:
            async for event in handle:
                dropped = int(getattr(handle, "dropped", 0) or 0)
                if dropped > last_dropped:
                    reason = f"SDK market stream dropped {dropped - last_dropped} event(s)"
                    for asset_id in asset_ids:
                        self.orderbook.mark_invalid(
                            asset_id, reason, resync_required=True
                        )
                        await self._publish_invalid_snapshot(asset_id, reason)
                    trading_safety.halt(reason)
                    return
                last_dropped = dropped
                item = normalize_sdk_stream_event(event)
                result = self.orderbook.apply_event(item)
                for aid, reason in result.invalid_assets.items():
                    await self._publish_invalid_snapshot(aid, reason)
                for aid in result.updated_asset_ids:
                    snap = self.orderbook.snapshot(aid)
                    if snap:
                        await self._publish_valid_snapshot(aid, snap)
                if result.invalid_assets or result.updated_asset_ids:
                    await self._refresh_integrity_readiness()
            raise RuntimeError("SDK market subscription ended unexpectedly")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            trading_safety.set_readiness(
                "market_stream", False, "SDK market stream consumer failed"
            )
            trading_safety.halt(f"SDK market stream consumer failed: {exc}")
            logger.exception("SDK market stream consumer failed")


md_gateway = MarketDataGateway()
