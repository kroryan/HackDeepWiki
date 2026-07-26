"""Durable chat-session and embedding-maintenance routes."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request

from api.security import sanitize_error_message
from api.storage import chat_history, embeddings, embeddings_backfill

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["conversations"])


def _error(operation: str, exc: Exception) -> HTTPException:
    logger.error("%s failed: %s", operation, exc, exc_info=True)
    return HTTPException(500, sanitize_error_message(str(exc)))


@router.get("/chat_history/sessions")
async def list_sessions(
    owner: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    type: Optional[str] = Query("github"),
):
    try:
        return {"sessions": chat_history.list_sessions(owner, repo, type)}
    except Exception as exc:
        raise _error("list chat sessions", exc) from exc


@router.post("/chat_history/sessions")
async def persist_session(request: Request):
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(
            400,
            sanitize_error_message(f"invalid JSON body: {exc}"),
        ) from exc
    session_id = body.get("session_id")
    if not session_id:
        raise HTTPException(400, "session_id is required")
    messages = body.get("messages") or []
    try:
        chat_history.persist_session_json(
            body.get("owner"),
            body.get("repo"),
            body.get("type") or "github",
            session_id,
            body.get("title") or "",
            messages,
        )
    except Exception as exc:
        raise _error("persist chat session", exc) from exc
    return {"saved": session_id, "count": len(messages)}


@router.get("/chat_history")
async def history(
    session_id: str = Query(...),
    owner: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    type: Optional[str] = Query("github"),
    limit: int = Query(200, ge=1, le=2000),
):
    try:
        messages = chat_history.get_history(owner, repo, type, session_id, limit)
    except Exception as exc:
        raise _error("get chat history", exc) from exc
    return {"messages": messages}


@router.delete("/chat_history/sessions/{session_id}")
async def delete_session(
    session_id: str,
    owner: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    type: Optional[str] = Query("github"),
):
    try:
        chat_history.delete_session(owner, repo, type, session_id)
    except Exception as exc:
        raise _error("delete chat session", exc) from exc
    return {"deleted": session_id}


@router.post("/embeddings/backfill")
async def backfill_embeddings(
    owner: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    type: Optional[str] = Query("github"),
):
    try:
        report = embeddings_backfill.backfill_from_pkl(owner, repo, type)
        report["rows_in_db"] = embeddings.count(owner, repo, type)
        return report
    except Exception as exc:
        raise _error("embeddings backfill", exc) from exc
