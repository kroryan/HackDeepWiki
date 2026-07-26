"""Cross-wiki search and share-link routes."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from api.security import sanitize_error_message
from api.storage.wiki_search import search
from api.storage.wiki_shares import (
    create_share,
    delete_share,
    list_shares,
    resolve_share,
)
from api.wiki_cache_paths import list_cache_files

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["wiki-discovery"])


@router.get("/wiki/search")
async def wiki_search(
    q: str = Query(..., min_length=1),
    owner: str | None = Query(None),
    repo: str | None = Query(None),
    language: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    try:
        results = search(
            q,
            owner=owner,
            repo=repo,
            language=language,
            limit=limit,
        )
        return {"query": q, "count": len(results), "results": results}
    except Exception as exc:
        logger.exception("Wiki search failed")
        raise HTTPException(
            500,
            sanitize_error_message(str(exc)),
        ) from exc


@router.post("/share")
async def new_share(
    owner: str | None = Query(None),
    repo: str = Query(...),
    repo_type: str = Query("github"),
    language: str = Query(...),
    version: str | None = Query(None),
    title: str | None = Query(None),
) -> dict[str, str]:
    try:
        share_id = create_share(
            owner,
            repo,
            repo_type,
            language,
            version=version,
            title=title,
        )
    except Exception as exc:
        logger.exception("Could not create share")
        raise HTTPException(
            500,
            sanitize_error_message(str(exc)),
        ) from exc
    return {"share_id": share_id, "url": f"/share/{share_id}"}


@router.get("/share/{share_id}")
async def share(share_id: str) -> dict[str, Any]:
    try:
        resolved = resolve_share(share_id)
    except Exception as exc:
        logger.exception("Could not resolve share")
        raise HTTPException(
            500,
            sanitize_error_message(str(exc)),
        ) from exc
    if not resolved:
        raise HTTPException(404, "Share not found or expired")
    files = list_cache_files(
        resolved["repo_type"],
        resolved.get("owner", ""),
        resolved["repo"],
        resolved["language"],
    )
    if not files:
        raise HTTPException(
            404,
            "The wiki this share pointed to has been deleted",
        )
    return resolved


@router.get("/shares")
async def shares(
    owner: str | None = Query(None),
    repo: str | None = Query(None),
) -> dict[str, Any]:
    try:
        return {"shares": list_shares(owner=owner, repo=repo)}
    except Exception as exc:
        logger.exception("Could not list shares")
        raise HTTPException(
            500,
            sanitize_error_message(str(exc)),
        ) from exc


@router.delete("/share/{share_id}")
async def revoke_share(share_id: str) -> dict[str, str]:
    try:
        deleted = delete_share(share_id)
    except Exception as exc:
        logger.exception("Could not revoke share")
        raise HTTPException(
            500,
            sanitize_error_message(str(exc)),
        ) from exc
    if not deleted:
        raise HTTPException(404, "Share not found")
    return {"deleted": share_id}
