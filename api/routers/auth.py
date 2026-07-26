"""Authentication endpoints and reusable FastAPI dependency."""

from __future__ import annotations

import os
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from api import config as runtime_config
from api.models import AuthorizationConfig
from api.security import (
    authorization_is_valid,
    create_authorization_session_token,
    request_authorization_value,
)

router = APIRouter(tags=["authentication"])

_AUTH_LOCKOUT_WINDOW = int(
    os.environ.get("HACKDEEPWIKI_AUTH_LOCKOUT_WINDOW", "300")
)
_AUTH_MAX_FAILED_ATTEMPTS = int(
    os.environ.get("HACKDEEPWIKI_AUTH_MAX_FAILED", "10")
)
_AUTH_FAILED_ATTEMPTS: list[float] = []


@router.get("/auth/status")
async def get_auth_status() -> dict[str, bool]:
    return {"auth_required": runtime_config.WIKI_AUTH_MODE}


@router.post("/auth/validate")
async def validate_auth_code(request: AuthorizationConfig) -> dict[str, object]:
    now = time.time()
    _AUTH_FAILED_ATTEMPTS[:] = [
        attempted
        for attempted in _AUTH_FAILED_ATTEMPTS
        if now - attempted < _AUTH_LOCKOUT_WINDOW
    ]
    if len(_AUTH_FAILED_ATTEMPTS) >= _AUTH_MAX_FAILED_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail="Too many failed attempts. Try again later.",
        )

    ok = bool(
        runtime_config.WIKI_AUTH_CODE
        and request.code
        and authorization_is_valid(request.code)
    )
    if not ok:
        _AUTH_FAILED_ATTEMPTS.append(now)
    return {
        "success": ok,
        "session_token": create_authorization_session_token() if ok else None,
    }


def verify_authorization(
    request: Request,
    authorization_code: Optional[str] = Query(None),
) -> bool:
    """Explicit dependency for especially sensitive route declarations.

    The global middleware already protects every non-public endpoint. Keeping
    this dependency on filesystem routes makes their policy obvious in OpenAPI
    source and protects them even if mounted into another FastAPI app.
    """
    value = authorization_code or request_authorization_value(request)
    if not authorization_is_valid(value):
        raise HTTPException(
            status_code=401,
            detail="Authorization code is invalid",
        )
    return True


def reset_auth_rate_limit_for_tests() -> None:
    _AUTH_FAILED_ATTEMPTS.clear()
