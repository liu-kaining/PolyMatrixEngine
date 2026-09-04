"""Narrow, typed boundary around Polymarket's unified Python SDK.

Business code must not depend on SDK response aliases, paginator details, private
credential names, or wire units.  This module is the only place allowed to know
those details.  Keeping the import lazy also lets the offline unit test suite run
on developer interpreters older than the production Python 3.11 image.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from importlib.metadata import PackageNotFoundError, version
from typing import Any, AsyncIterator, Dict, Mapping, Optional


SUPPORTED_SDK_VERSION = "0.6.0"
OPEN_ORDER_STATUSES = frozenset({"LIVE", "DELAYED"})
ACCEPTED_ORDER_STATUSES = frozenset({"LIVE", "MATCHED", "DELAYED"})


def is_definite_submit_rejection_status(status: Any) -> bool:
    """Whether an HTTP response proves that an order was not accepted.

    Gateway/server timeouts and all 5xx responses remain ambiguous because a
    downstream matching engine may have accepted the request before the error.
    """
    try:
        code = int(status)
    except (TypeError, ValueError):
        return False
    return 400 <= code < 500 and code not in {408, 499}


class ExchangeContractError(RuntimeError):
    """The exchange/SDK returned a shape or value we cannot safely interpret."""


class ExchangeRequestRejected(RuntimeError):
    """The venue explicitly rejected a request, so no order was accepted."""

    def __init__(self, message: str, *, status: int, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = str(code) if code is not None else None


class ExchangePreSubmissionError(RuntimeError):
    """The pinned SDK rejected/signing failed before an order could be sent."""


@dataclass(frozen=True)
class UserStreamCredentials:
    api_key: str
    secret: str
    passphrase: str


@dataclass(frozen=True)
class MarketConstraints:
    condition_id: str
    token_id: str
    tick_size: Decimal
    min_order_size: Decimal
    neg_risk: bool


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ExchangeContractError(f"{field} must be numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ExchangeContractError(f"{field} must be numeric") from exc
    if not parsed.is_finite() or (positive and parsed <= 0):
        raise ExchangeContractError(f"{field} is outside its accepted range")
    return parsed


def _model_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="json", by_alias=True))
    raise ExchangeContractError(f"unsupported SDK response type: {type(value).__name__}")


def _assert_supported_sdk() -> None:
    try:
        installed_version = version("polymarket-client")
    except PackageNotFoundError as exc:  # pragma: no cover - production dependency gate
        raise RuntimeError(
            f"polymarket-client=={SUPPORTED_SDK_VERSION} is required"
        ) from exc
    if installed_version != SUPPORTED_SDK_VERSION:
        raise RuntimeError(
            f"unsupported polymarket-client version {installed_version}; "
            f"expected exactly {SUPPORTED_SDK_VERSION}"
        )


def normalize_sdk_stream_event(event: Any) -> Dict[str, Any]:
    """Flatten a typed SDK stream event to the application's stable wire shape."""
    raw = _model_dict(event)
    event_type = str(raw.get("type") or raw.get("event_type") or "").strip().lower()
    payload = raw.get("payload")
    if not event_type or not isinstance(payload, Mapping):
        raise ExchangeContractError("SDK stream event has no typed payload")
    normalized = dict(payload)
    normalized["event_type"] = event_type
    if normalized.get("token_id") not in (None, ""):
        normalized["asset_id"] = normalized["token_id"]
    if event_type == "order" and normalized.get("order_event_type") not in (None, ""):
        normalized["type"] = normalized["order_event_type"]
    changes = normalized.get("price_changes")
    if isinstance(changes, list):
        for change in changes:
            if isinstance(change, dict) and change.get("token_id") not in (None, ""):
                change["asset_id"] = change["token_id"]
    makers = normalized.get("maker_orders")
    if isinstance(makers, list):
        for maker in makers:
            if isinstance(maker, dict) and maker.get("token_id") not in (None, ""):
                maker["asset_id"] = maker["token_id"]
    return normalized


def normalize_order_book(book: Any) -> Dict[str, Any]:
    """Normalize a typed public book without changing human share units."""
    raw = _model_dict(book)
    asset_id = str(raw.get("asset_id") or raw.get("token_id") or "").strip()
    if not asset_id:
        raise ExchangeContractError("order book token id is missing")
    bids = raw.get("bids")
    asks = raw.get("asks")
    if not isinstance(bids, list) or not isinstance(asks, list):
        raise ExchangeContractError("order book sides must be lists")
    return {
        **raw,
        "asset_id": asset_id,
        "bids": bids,
        "asks": asks,
        "hash": raw.get("hash") or raw.get("snapshot_id"),
    }


def normalize_exchange_status(value: Any, prefix: str = "") -> str:
    """Normalize V2 enum strings such as ORDER_STATUS_LIVE to LIVE."""
    status = str(value or "").strip().upper()
    normalized_prefix = str(prefix or "").strip().upper()
    if normalized_prefix and status.startswith(normalized_prefix):
        status = status[len(normalized_prefix) :]
    return status


def normalize_order_post_response(response: Any) -> Dict[str, Any]:
    """Convert the unified SDK order union to the OMS's stable dictionary contract."""
    raw = _model_dict(response)
    ok = raw.get("ok")
    if ok is False:
        code = str(raw.get("code") or "unknown")
        message = str(raw.get("message") or "exchange rejected order")
        return {"success": False, "errorCode": code, "errorMsg": message}
    if ok is not True:
        raise ExchangeContractError("order response is missing an explicit ok discriminator")

    order_id = str(raw.get("order_id") or raw.get("orderID") or "").strip()
    status = normalize_exchange_status(raw.get("status"), "ORDER_STATUS_")
    if not order_id or status not in ACCEPTED_ORDER_STATUSES:
        raise ExchangeContractError("accepted order response has invalid id/status")
    return {
        "success": True,
        "orderID": order_id,
        "status": status,
        "makingAmount": str(
            _decimal(raw.get("making_amount", raw.get("makingAmount", "0")), "making_amount")
        ),
        "takingAmount": str(
            _decimal(raw.get("taking_amount", raw.get("takingAmount", "0")), "taking_amount")
        ),
        "tradeIDs": list(raw.get("trade_ids", raw.get("tradeIDs", ())) or ()),
        "transactionsHashes": list(
            raw.get("transactions_hashes", raw.get("transactionsHashes", ())) or ()
        ),
    }


def normalize_open_order(order: Any) -> Dict[str, Any]:
    """Return human-unit order fields and tag the unit contract explicitly."""
    raw = _model_dict(order)
    order_id = str(raw.get("id") or raw.get("order_id") or "").strip()
    token_id = str(raw.get("asset_id") or raw.get("token_id") or "").strip()
    market = str(raw.get("market") or raw.get("condition_id") or "").strip()
    side = str(raw.get("side") or "").upper()
    status = normalize_exchange_status(raw.get("status"), "ORDER_STATUS_")
    if not order_id or not token_id or side not in {"BUY", "SELL"} or not status:
        raise ExchangeContractError("open order is missing required identity fields")
    original_size = _decimal(
        raw.get("original_size", raw.get("originalSize", raw.get("size"))),
        "original_size",
        positive=True,
    )
    matched_size = _decimal(
        raw.get("size_matched", raw.get("sizeMatched", raw.get("matched_size", "0"))),
        "size_matched",
    )
    if matched_size < 0 or matched_size > original_size:
        raise ExchangeContractError("open order matched size is invalid")
    return {
        **raw,
        "id": order_id,
        "asset_id": token_id,
        "market": market,
        "side": side,
        "status": status,
        "price": str(_decimal(raw.get("price"), "price", positive=True)),
        "original_size": str(original_size),
        "size_matched": str(matched_size),
        "_size_encoding": "human",
        "_adapter_contract": f"polymarket-client/{SUPPORTED_SDK_VERSION}",
    }


async def _iter_paginator_items(paginator: Any) -> AsyncIterator[Any]:
    iterator = getattr(paginator, "iter_items", None)
    if callable(iterator):
        async for item in iterator():
            yield item
        return
    if hasattr(paginator, "__aiter__"):
        async for page in paginator:
            if isinstance(page, Mapping):
                items = page.get("data", ())
            else:
                items = getattr(page, "data", ())
            for item in items or ():
                yield item
        return
    raise ExchangeContractError("SDK paginator is not asynchronously iterable")


class PolymarketV2Adapter:
    """Application-facing adapter for ``polymarket-client==0.6.0``."""

    def __init__(self, client: Any, *, builder_code: str = "") -> None:
        self._client = client
        self._builder_code = str(builder_code or "").strip()

    @classmethod
    async def create(
        cls,
        *,
        private_key: str,
        wallet: str,
        builder_code: str = "",
    ) -> "PolymarketV2Adapter":
        _assert_supported_sdk()
        try:
            from polymarket import AsyncSecureClient
        except ImportError as exc:  # pragma: no cover - production dependency gate
            raise RuntimeError(
                f"polymarket-client=={SUPPORTED_SDK_VERSION} is required for live mode"
            ) from exc

        client = await AsyncSecureClient.create(private_key=private_key, wallet=wallet)
        expected_wallet = str(wallet or "").strip().lower()
        actual_wallet = str(client.wallet or "").strip().lower()
        if not expected_wallet or actual_wallet != expected_wallet:
            await client.close()
            raise ExchangeContractError("authenticated wallet differs from FUNDER_ADDRESS")
        return cls(client, builder_code=builder_code)

    @property
    def wallet(self) -> str:
        return str(self._client.wallet)

    @property
    def ws_credentials(self) -> UserStreamCredentials:
        creds = self._client.credentials
        key = str(getattr(creds, "key", "") or "")
        secret = str(getattr(creds, "secret", "") or "")
        passphrase = str(getattr(creds, "passphrase", "") or "")
        if not key or not secret or not passphrase:
            raise ExchangeContractError("SDK credentials are incomplete")
        return UserStreamCredentials(key, secret, passphrase)

    async def place_limit_order(
        self,
        *,
        token_id: str,
        price: float,
        size: float,
        side: str,
        post_only: bool,
    ) -> Dict[str, Any]:
        try:
            response = await self._client.place_limit_order(
                token_id=str(token_id),
                price=str(price),
                size=str(size),
                side=str(side).upper(),
                post_only=bool(post_only),
                builder_code=self._builder_code or None,
            )
        except Exception as exc:
            # Import only after the exact package version has passed the gate.
            from polymarket import RequestRejectedError, SigningError, UserInputError

            if isinstance(exc, RequestRejectedError) and is_definite_submit_rejection_status(
                exc.status
            ):
                raise ExchangeRequestRejected(
                    str(exc), status=exc.status, code=exc.code
                ) from exc
            if isinstance(exc, (SigningError, UserInputError)):
                raise ExchangePreSubmissionError(str(exc)) from exc
            raise
        return normalize_order_post_response(response)

    async def cancel_order(self, *, order_id: str) -> Dict[str, Any]:
        return _model_dict(await self._client.cancel_order(order_id=str(order_id)))

    async def cancel_market_orders(
        self, *, market: Optional[str] = None, token_id: Optional[str] = None
    ) -> Dict[str, Any]:
        result = await self._client.cancel_market_orders(market=market, token_id=token_id)
        return _model_dict(result)

    async def get_orders(
        self,
        *,
        token_id: Optional[str] = None,
        market: Optional[str] = None,
    ) -> list[Dict[str, Any]]:
        paginator = self._client.list_open_orders(token_id=token_id, market=market)
        return [normalize_open_order(item) async for item in _iter_paginator_items(paginator)]

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        return normalize_open_order(await self._client.get_order(order_id=str(order_id)))

    async def get_trades(
        self,
        *,
        token_id: Optional[str] = None,
        market: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> list[Dict[str, Any]]:
        # The SDK/API ``id`` filter is a trade id, not an order id.  Never pass
        # ``order_id`` to it: enumerate the requested market/token page set and
        # apply the role-specific order identity filter locally.
        paginator = self._client.list_account_trades(
            token_id=token_id,
            market=market,
        )
        output: list[Dict[str, Any]] = []
        async for item in _iter_paginator_items(paginator):
            raw = _model_dict(item)
            raw["status"] = normalize_exchange_status(raw.get("status"), "TRADE_STATUS_")
            raw["_size_encoding"] = "human"
            raw["_adapter_contract"] = f"polymarket-client/{SUPPORTED_SDK_VERSION}"
            if order_id and not _trade_references_order(raw, str(order_id)):
                continue
            output.append(raw)
        return output

    async def get_market_constraints(self, token_id: str) -> MarketConstraints:
        book = await self._client.get_order_book(token_id=str(token_id))
        actual_token = str(getattr(book, "asset_id", "") or getattr(book, "token_id", ""))
        if actual_token and actual_token != str(token_id):
            raise ExchangeContractError("order book token differs from requested token")
        tick_size = _decimal(getattr(book, "tick_size", None), "tick_size", positive=True)
        min_order_size = _decimal(
            getattr(book, "min_order_size", None), "min_order_size", positive=True
        )
        neg_risk = getattr(book, "neg_risk", None)
        if not isinstance(neg_risk, bool):
            raise ExchangeContractError("order book neg_risk must be boolean")
        condition_id = str(
            getattr(book, "condition_id", "") or getattr(book, "market", "")
        ).strip()
        if not condition_id:
            raise ExchangeContractError("order book condition id is missing")
        return MarketConstraints(
            condition_id, str(token_id), tick_size, min_order_size, neg_risk
        )

    async def get_balance(self) -> float:
        balance = await self._client.get_balance_allowance(asset_type="COLLATERAL")
        raw = getattr(balance, "balance", None)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ExchangeContractError("collateral balance is not a non-negative integer")
        return float(Decimal(raw) / Decimal(1_000_000))

    async def subscribe_user(self, markets: Optional[list[str]] = None) -> Any:
        from polymarket.streams import UserSpec

        return await self._client.subscribe(UserSpec(markets=markets or None))

    def user_stream_is_open(self) -> bool:
        manager = getattr(self._client, "_user_manager", None)
        return bool(manager is not None and getattr(manager, "is_open", False))

    async def close(self) -> None:
        await self._client.close()


def _trade_references_order(trade: Mapping[str, Any], order_id: str) -> bool:
    if str(trade.get("taker_order_id") or "") == order_id:
        return True
    makers = trade.get("maker_orders") or ()
    if not isinstance(makers, (list, tuple)):
        raise ExchangeContractError("trade maker_orders must be a list/tuple")
    return any(
        isinstance(maker, Mapping)
        and str(maker.get("order_id") or "") == order_id
        for maker in makers
    )


class PolymarketV2PublicAdapter:
    """Pinned, SDK-native public book and market-stream boundary."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def create(cls) -> "PolymarketV2PublicAdapter":
        _assert_supported_sdk()
        try:
            from polymarket import AsyncPublicClient
        except ImportError as exc:  # pragma: no cover - production dependency gate
            raise RuntimeError(
                f"polymarket-client=={SUPPORTED_SDK_VERSION} is required"
            ) from exc
        return cls(AsyncPublicClient())

    async def get_order_book(self, token_id: str) -> Dict[str, Any]:
        book = normalize_order_book(
            await self._client.get_order_book(token_id=str(token_id))
        )
        if book["asset_id"] != str(token_id):
            raise ExchangeContractError("order book token differs from requested token")
        return book

    async def subscribe_market(self, token_ids: list[str]) -> Any:
        from polymarket.streams import MarketSpec

        return await self._client.subscribe(
            MarketSpec(token_ids=token_ids, custom_feature_enabled=True)
        )

    def market_stream_is_open(self) -> bool:
        manager = getattr(self._client, "_market_manager", None)
        return bool(manager is not None and getattr(manager, "is_open", False))

    async def close(self) -> None:
        await self._client.close()


__all__ = [
    "ExchangeContractError",
    "ExchangePreSubmissionError",
    "ExchangeRequestRejected",
    "MarketConstraints",
    "PolymarketV2Adapter",
    "PolymarketV2PublicAdapter",
    "UserStreamCredentials",
    "normalize_exchange_status",
    "normalize_order_book",
    "normalize_open_order",
    "normalize_order_post_response",
    "normalize_sdk_stream_event",
    "is_definite_submit_rejection_status",
]
