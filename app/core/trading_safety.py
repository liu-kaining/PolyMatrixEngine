"""Central, fail-closed trading safety interlock.

This module deliberately separates three concepts:

* requested mode (disabled / paper / live)
* short-lived static live arming
* runtime readiness of the components required for safe order submission

Cancellation remains available for a deliberately requested live process even when new
orders are blocked, because a safety halt must never prevent risk reduction.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import re
import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from app.core.config import settings
from app.core.strategy_fingerprint import runtime_strategy_config_errors


MAX_ARM_WINDOW_SECONDS = 24 * 60 * 60
MIN_ADMIN_TOKEN_LENGTH = 32


class TradingMode(str, Enum):
    DISABLED = "disabled"
    PAPER = "paper"
    LIVE = "live"


class SafetyInterlockError(RuntimeError):
    """Raised when a control-plane action is blocked by the safety interlock."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc_timestamp(raw: str) -> Optional[datetime]:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def build_live_arm_token(funder_address: str, expires_at: str, budget_usd: float) -> str:
    """Build the explicit arm token tied to wallet, expiry and configured global budget."""
    material = (
        f"polymatrix-live-v1|{str(funder_address or '').strip().lower()}|"
        f"{str(expires_at or '').strip()}|{float(budget_usd):.2f}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class TradingSafetyGate:
    """Thread-safe process-wide safety state with redacted status output."""

    REQUIRED_LIVE_READINESS = (
        "database",
        "redis",
        "oms_credentials",
        "market_stream",
        "market_data_integrity",
        "user_stream",
        "positions_reconciled",
        "open_orders_reconciled",
        "risk_reservations",
        "risk_monitor",
        "accounting_integrity",
        "alpha_evidence",
    )

    def __init__(
        self,
        settings_obj: Any,
        *,
        now_fn: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._settings = settings_obj
        self._now_fn = now_fn
        self._lock = threading.RLock()
        self._readiness: Dict[str, Dict[str, Any]] = {
            name: {"ready": False, "detail": "not reported"}
            for name in self.REQUIRED_LIVE_READINESS
        }
        self._halted = False
        self._halt_reason: Optional[str] = None

    @property
    def mode(self) -> TradingMode:
        raw = str(getattr(self._settings, "TRADING_MODE", "disabled") or "disabled").lower()
        try:
            return TradingMode(raw)
        except ValueError:
            return TradingMode.DISABLED

    def static_live_errors(self) -> List[str]:
        """Return every static configuration error; never include secret values."""
        if self.mode is not TradingMode.LIVE:
            return ["TRADING_MODE is not live"]

        errors: List[str] = []
        if not bool(getattr(self._settings, "LIVE_TRADING_ENABLED", False)):
            errors.append("LIVE_TRADING_ENABLED secondary confirmation is false")
        # Defense-in-depth for stale deployments that still inject the removed legacy flag.
        if bool(getattr(self._settings, "AUTO_TUNE_FOR_REWARDS", False)):
            errors.append("removed AUTO_TUNE_FOR_REWARDS flag cannot be true")
        if bool(getattr(self._settings, "SINGLE_SIDE_CHEAP_ONLY", False)):
            errors.append("removed SINGLE_SIDE_CHEAP_ONLY flag cannot be true")
        if bool(getattr(self._settings, "HEDGE_ON_FILL", False)):
            errors.append("removed HEDGE_ON_FILL flag cannot be true")
        if not bool(
            getattr(self._settings, "OFFLINE_VALIDATED_ALPHA_ENABLED", False)
        ):
            errors.append("OFFLINE_VALIDATED_ALPHA_ENABLED is false")
        if not bool(getattr(self._settings, "LIVE_FEE_ACCOUNTING_VALIDATED", False)):
            errors.append("LIVE_FEE_ACCOUNTING_VALIDATED confirmation is false")
        code_commit = str(getattr(self._settings, "APP_CODE_COMMIT", "") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{40,64}", code_commit):
            errors.append("APP_CODE_COMMIT must be a full build commit hash")
        if not str(getattr(self._settings, "ALPHA_STRATEGY_ID", "") or "").strip():
            errors.append("ALPHA_STRATEGY_ID is empty")
        errors.extend(runtime_strategy_config_errors(self._settings))

        funder = str(getattr(self._settings, "FUNDER_ADDRESS", "") or "").strip().lower()
        if not funder:
            errors.append("FUNDER_ADDRESS is missing")

        allowed_raw = str(
            getattr(self._settings, "LIVE_ALLOWED_FUNDER_ADDRESSES", "") or ""
        )
        allowed = {item.strip().lower() for item in allowed_raw.split(",") if item.strip()}
        if not allowed:
            errors.append("LIVE_ALLOWED_FUNDER_ADDRESSES is empty")
        elif funder and funder not in allowed:
            errors.append("FUNDER_ADDRESS is not in LIVE_ALLOWED_FUNDER_ADDRESSES")

        expires_raw = str(getattr(self._settings, "LIVE_ARM_EXPIRES_AT", "") or "").strip()
        expires_at = _parse_utc_timestamp(expires_raw)
        if expires_at is None:
            errors.append("LIVE_ARM_EXPIRES_AT must be a timezone-aware ISO-8601 timestamp")
        else:
            now = self._now_fn().astimezone(timezone.utc)
            seconds = (expires_at - now).total_seconds()
            if seconds <= 0:
                errors.append("live arm has expired")
            elif seconds > MAX_ARM_WINDOW_SECONDS:
                errors.append("live arm expiry is more than 24 hours away")

        global_budget = float(getattr(self._settings, "GLOBAL_MAX_BUDGET", 0.0) or 0.0)
        live_cap = float(getattr(self._settings, "LIVE_BUDGET_CAP_USD", 0.0) or 0.0)
        if global_budget <= 0:
            errors.append("GLOBAL_MAX_BUDGET must be positive")
        if live_cap <= 0:
            errors.append("LIVE_BUDGET_CAP_USD must be positive")
        elif global_budget > live_cap:
            errors.append("GLOBAL_MAX_BUDGET exceeds LIVE_BUDGET_CAP_USD")

        supplied_token = str(getattr(self._settings, "LIVE_ARM_TOKEN", "") or "").strip()
        expected_token = build_live_arm_token(funder, expires_raw, global_budget)
        if not supplied_token or not hmac.compare_digest(supplied_token, expected_token):
            errors.append("LIVE_ARM_TOKEN does not match wallet, expiry and budget")

        return errors

    def is_static_live_armed(self) -> bool:
        return not self.static_live_errors()

    def set_readiness(self, component: str, ready: bool, detail: str = "") -> None:
        if component not in self.REQUIRED_LIVE_READINESS:
            raise ValueError(f"Unknown readiness component: {component}")
        with self._lock:
            self._readiness[component] = {
                "ready": bool(ready),
                "detail": str(detail or ("ready" if ready else "not ready")),
            }

    def halt(self, reason: str) -> None:
        with self._lock:
            self._halted = True
            self._halt_reason = str(reason or "manual safety halt")

    def clear_halt_for_tests(self) -> None:
        """Test-only reset; production recovery should require a process restart and fresh arm."""
        with self._lock:
            self._halted = False
            self._halt_reason = None

    def runtime_order_blockers(self) -> List[str]:
        blockers = self.static_live_errors()
        with self._lock:
            if self._halted:
                blockers.append(f"safety halt active: {self._halt_reason or 'unspecified'}")
            for name in self.REQUIRED_LIVE_READINESS:
                item = self._readiness[name]
                if not item["ready"]:
                    blockers.append(f"{name} is not ready: {item['detail']}")
        return blockers

    def runtime_reduce_only_blockers(self) -> List[str]:
        """Block SELL unless live is armed and all facts are ready; ignore only sticky halt."""
        blockers = self.static_live_errors()
        with self._lock:
            for name in self.REQUIRED_LIVE_READINESS:
                item = self._readiness[name]
                if not item["ready"]:
                    blockers.append(f"{name} is not ready: {item['detail']}")
        return blockers

    def can_submit_live_reduce_only(self) -> bool:
        return self.mode is TradingMode.LIVE and not self.runtime_reduce_only_blockers()

    def can_submit_live_order(self) -> bool:
        return self.mode is TradingMode.LIVE and not self.runtime_order_blockers()

    def can_send_exchange_cancel(self) -> bool:
        """Cancellation is allowed despite expiry/halt, but requires two explicit live flags."""
        return (
            self.mode is TradingMode.LIVE
            and bool(getattr(self._settings, "LIVE_TRADING_ENABLED", False))
        )

    def assert_engine_start_allowed(self) -> None:
        if self.mode is TradingMode.DISABLED:
            raise SafetyInterlockError("TRADING_MODE=disabled blocks engine startup")
        if self.mode is TradingMode.LIVE:
            errors = self.static_live_errors()
            with self._lock:
                if self._halted:
                    errors.append(f"safety halt active: {self._halt_reason or 'unspecified'}")
            if errors:
                raise SafetyInterlockError("; ".join(errors))

    def assert_router_start_allowed(self) -> None:
        self.assert_engine_start_allowed()
        if self.mode is TradingMode.LIVE:
            raise SafetyInterlockError(
                "reward-ranked Auto-Router is paper-only and cannot run in live mode"
            )

    def admin_token_configured(self) -> bool:
        token = str(getattr(self._settings, "ADMIN_API_TOKEN", "") or "")
        return len(token) >= MIN_ADMIN_TOKEN_LENGTH

    def status(self) -> Dict[str, Any]:
        with self._lock:
            readiness = {name: dict(value) for name, value in self._readiness.items()}
            halted = self._halted
            halt_reason = self._halt_reason
        static_errors = self.static_live_errors() if self.mode is TradingMode.LIVE else []
        runtime_blockers = self.runtime_order_blockers() if self.mode is TradingMode.LIVE else []
        return {
            "mode": self.mode.value,
            "legacy_live_confirmation": bool(
                getattr(self._settings, "LIVE_TRADING_ENABLED", False)
            ),
            "static_live_armed": self.mode is TradingMode.LIVE and not static_errors,
            "live_order_submission_allowed": self.can_submit_live_order(),
            "live_reduce_only_submission_allowed": self.can_submit_live_reduce_only(),
            "exchange_cancel_allowed": self.can_send_exchange_cancel(),
            "auto_router_live_supported": False,
            "admin_token_configured": self.admin_token_configured(),
            "halted": halted,
            "halt_reason": halt_reason,
            "static_errors": static_errors,
            "runtime_blockers": runtime_blockers,
            "readiness": readiness,
        }


trading_safety = TradingSafetyGate(settings)


def _main() -> None:
    parser = argparse.ArgumentParser(description="Generate a short-lived PolyMatrix live arm token")
    parser.add_argument("--funder", required=True)
    parser.add_argument("--expires-at", required=True, help="Timezone-aware ISO-8601 timestamp")
    parser.add_argument("--budget", required=True, type=float)
    args = parser.parse_args()
    print(build_live_arm_token(args.funder, args.expires_at, args.budget))


if __name__ == "__main__":
    _main()
