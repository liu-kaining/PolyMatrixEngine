"""Authentication dependency for mutating control-plane endpoints."""

import hmac
from typing import Optional

from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.trading_safety import MIN_ADMIN_TOKEN_LENGTH


_bearer = HTTPBearer(auto_error=False)


def admin_token_matches(configured: str, supplied: Optional[str]) -> bool:
    configured_value = str(configured or "")
    supplied_value = str(supplied or "")
    if len(configured_value) < MIN_ADMIN_TOKEN_LENGTH or not supplied_value:
        return False
    return hmac.compare_digest(configured_value, supplied_value)


async def require_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> None:
    """Validate a bearer token without ever logging or returning the configured secret."""
    supplied = credentials.credentials if credentials else None
    configured = str(getattr(settings, "ADMIN_API_TOKEN", "") or "")
    if len(configured) < MIN_ADMIN_TOKEN_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Mutating control API is disabled: ADMIN_API_TOKEN is not configured",
        )
    if not admin_token_matches(configured, supplied):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing administrator bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
