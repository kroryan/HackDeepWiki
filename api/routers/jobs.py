"""Durable background-job HTTP contract."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query

from api.jobs import queue
from api.storage import repo_key

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.post("")
async def enqueue_job(
    kind: str = Query(...),
    owner: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    repo_type: Optional[str] = Query(None),
    payload: dict = Body(default={}),
):
    if kind not in queue._HANDLERS:
        raise HTTPException(
            status_code=400,
            detail=f"No handler registered for job kind '{kind}'",
        )
    job_id = queue.enqueue(kind, repo_key(owner, repo, repo_type), payload)
    return {"job_id": job_id, "status": "queued", "kind": kind}


@router.get("")
async def list_jobs(
    owner: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    repo_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    key = repo_key(owner, repo, repo_type) if (owner or repo or repo_type) else None
    return {
        "jobs": queue.list_jobs(
            repo_key_value=key,
            status=status,
            limit=limit,
        )
    }


@router.get("/{job_id}")
async def get_job(job_id: int):
    job = next(
        (item for item in queue.list_jobs(limit=500) if item["id"] == job_id),
        None,
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.delete("/{job_id}")
async def cancel_job(job_id: int):
    if not queue.cancel(job_id):
        raise HTTPException(
            status_code=409,
            detail="Job is not queued (running/done/dead) and cannot be cancelled",
        )
    return {"cancelled": job_id}
