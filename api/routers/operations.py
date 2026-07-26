"""Accounting, pricing and provider-profile operations."""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from api.security import sanitize_error_message
from api.storage import accounting, provider_profiles

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["operations"])


def _internal_error(operation: str, exc: Exception) -> HTTPException:
    logger.error("%s failed: %s", operation, exc, exc_info=True)
    return HTTPException(
        status_code=500,
        detail=sanitize_error_message(str(exc)),
    )


@router.get("/accounting")
async def accounting_summary(since_days: Optional[int] = Query(None, ge=1, le=365)):
    try:
        return accounting.summary(since_days=since_days)
    except Exception as exc:
        raise _internal_error("accounting summary", exc) from exc


@router.get("/pricing")
async def list_pricing():
    try:
        return {"pricing": accounting.list_pricing()}
    except Exception as exc:
        raise _internal_error("list pricing", exc) from exc


@router.put("/pricing")
async def set_pricing(
    model_pattern: str = Query(...),
    input_per_m: Optional[float] = Query(None),
    output_per_m: Optional[float] = Query(None),
):
    try:
        accounting.set_price(model_pattern, input_per_m, output_per_m)
        return {
            "set": model_pattern,
            "input_per_m": input_per_m,
            "output_per_m": output_per_m,
        }
    except Exception as exc:
        raise _internal_error("set pricing", exc) from exc


@router.delete("/pricing")
async def delete_pricing(model_pattern: str = Query(...)):
    try:
        deleted = accounting.delete_price(model_pattern)
    except Exception as exc:
        raise _internal_error("delete pricing", exc) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Pricing row not found")
    return {"deleted": model_pattern}


@router.get("/profiles")
async def list_profiles():
    try:
        return {"profiles": provider_profiles.list_all()}
    except Exception as exc:
        raise _internal_error("list profiles", exc) from exc


@router.post("/profiles")
async def upsert_profile(
    name: str = Query(...),
    provider: str = Query(...),
    api_key: Optional[str] = Query(None),
    api_endpoint: Optional[str] = Query(None),
):
    try:
        provider_profiles.upsert(
            name,
            provider,
            api_key=api_key,
            api_endpoint=api_endpoint,
        )
    except Exception as exc:
        raise _internal_error("upsert profile", exc) from exc
    return {
        "saved": name,
        "provider": provider,
        "encrypted_at_rest": bool(os.environ.get("HACKDEEPWIKI_ENC_KEY")),
    }


@router.delete("/profiles/{name}")
async def delete_profile(name: str):
    try:
        deleted = provider_profiles.delete(name)
    except Exception as exc:
        raise _internal_error("delete profile", exc) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"deleted": name}
