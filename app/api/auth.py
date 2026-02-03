"""
API key authentication helpers.
"""

import logging
import secrets
from typing import List

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from config import settings

logger = logging.getLogger(__name__)

api_key_header = APIKeyHeader(
    name=settings.api_auth_header_name,
    auto_error=False,
)


def _configured_api_keys() -> List[str]:
    raw = settings.api_keys or settings.api_key or ""
    return [key.strip() for key in raw.split(",") if key.strip()]


def _public_paths() -> List[str]:
    raw = settings.api_auth_public_paths or ""
    return [path.strip() for path in raw.split(",") if path.strip()]


def require_private_api_key(
    request: Request,
    provided_api_key: str | None = Security(api_key_header),
) -> bool:
    """
    Validates API key for private endpoints.
    Public paths configured in API_AUTH_PUBLIC_PATHS bypass authentication.
    """
    if request.url.path in _public_paths():
        return True

    valid_keys = _configured_api_keys()
    if not valid_keys:
        logger.error("API auth is enabled but no API key is configured.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API auth misconfigured: no API key configured on server.",
        )

    if not provided_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key.",
        )

    if not any(secrets.compare_digest(provided_api_key, expected) for expected in valid_keys):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
        )

    return True
