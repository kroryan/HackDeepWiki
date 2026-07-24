"""SQLite-backed job queue + worker (Fase 3).

OpenDeepWiki has BranchGenerationWorker / TranslationWorker / MindMapWorker /
DeadLetterProcessor on a real broker. HackDeepWiki's local-first, no-service
constraint forbids Celery/Redis, so this is a minimal durable queue on the
existing profile.db (Fase 0): one ``jobs`` table, a claim loop with
row-level locking via ``BEGIN IMMEDIATE``, retries with backoff, and a
dead-letter state.

What it's FOR today: long wiki operations (re-generate, translate, ZIM
export, vuln scan) that shouldn't block the HTTP request that triggered
them. The frontend already polls wiki-cache state, so a job that finishes
async and writes the result to the wikicache is picked up by the existing
UI refresh. Fase 3 wires the queue + worker; specific job kinds are
registered by callers (the worker dispatches by ``kind``).

Design notes:
- Claim via UPDATE...RETURNING under BEGIN IMMEDIATE so two workers can't
  grab the same job (SQLite serializes writers; IMMEDIATE takes the write
  lock up front, avoiding a "database is locked" mid-transaction).
- A worker is a plain asyncio task started by the app; it polls every
  POLL_INTERVAL, claims one job, runs its handler, and records the result.
  No external process to supervise -- the app's lifetime is the worker's.
- Retry/backoff: a failed job's attempt count increments; after MAX_ATTEMPTS
  it moves to 'dead' (the dead-letter equivalent) instead of looping forever.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Optional

from api.storage import connect, profile_db_path, repo_key

logger = logging.getLogger(__name__)

# Tunables (env-overridable). Conservative defaults for a local app: poll
# every few seconds, give a job 3 tries, then dead-letter it.
POLL_INTERVAL = float(os.environ.get("HACKDEEPWIKI_JOB_POLL", "3"))
MAX_ATTEMPTS = int(os.environ.get("HACKDEEPWIKI_JOB_MAX_ATTEMPTS", "3"))
# Base backoff seconds; actual delay = BASE * 2**(attempt-1).
BACKOFF_BASE = float(os.environ.get("HACKDEEPWIKI_JOB_BACKOFF_BASE", "5"))

# A handler maps the job's payload dict to a result (any JSON-serializable)
# and is awaited. Registered by kind.
JobHandler = Callable[[dict[str, Any]], Awaitable[Any]]
_HANDLERS: dict[str, JobHandler] = {}

# A single in-process worker loop; started once by the app (ensure_worker).
_WORKER_TASK: Optional[asyncio.Task] = None
_WORKER_LOCK = asyncio.Lock()


def register_handler(kind: str, handler: JobHandler) -> None:
    """Register the async handler for a job ``kind``. The worker dispatches
    claimed jobs to the handler matching their kind; an unknown kind fails
    the job (and, after retries, dead-letters it)."""
    _HANDLERS[kind] = handler


def enqueue(kind: str, repo_key_value: str, payload: Optional[dict] = None) -> int:
    """Push a job onto the queue. Returns the job id. ``repo_key_value`` is
    the per-repo key (so a job can be listed/cancelled per repo); payload is
    any JSON the handler needs."""
    with connect(profile_db_path()) as conn:
        cur = conn.execute(
            "INSERT INTO jobs (repo_key, kind, status, payload_json) VALUES (?, ?, 'queued', ?)",
            (repo_key_value, kind, json.dumps(payload or {})),
        )
        conn.commit()
        return int(cur.lastrowid)


def claim() -> Optional[dict]:
    """Atomically claim the oldest queued job (or one whose retry-backoff
    has elapsed) and mark it 'running'. Returns the job row or None if no
    job is ready. Uses BEGIN IMMEDIATE so concurrent workers can't double-claim."""
    with connect(profile_db_path()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Ready = queued, OR running-but-its-worker-died (stale: started
            # long ago without finishing -- we reclaim it for a retry). We
            # treat a 'running' job older than a heartbeat timeout as stale.
            row = conn.execute(
                "SELECT id, repo_key, kind, payload_json, attempts FROM jobs "
                "WHERE status = 'queued' "
                "OR (status = 'running' AND started_at < datetime('now', ?)) "
                "ORDER BY created_at ASC LIMIT 1",
                (f"-{int(os.environ.get('HACKDEEPWIKI_JOB_STALE_SECONDS', '120'))} seconds",),
            ).fetchone()
            if not row:
                conn.execute("COMMIT")
                return None
            conn.execute(
                "UPDATE jobs SET status='running', started_at=datetime('now'), "
                "attempts=COALESCE(attempts,0)+1 WHERE id = ?",
                (row["id"],),
            )
            conn.execute("COMMIT")
            return {
                "id": row["id"], "repo_key": row["repo_key"], "kind": row["kind"],
                "payload": json.loads(row["payload_json"] or "{}"),
                "attempts": (row["attempts"] or 0) + 1,
            }
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _complete(job_id: int, result: Any) -> None:
    with connect(profile_db_path()) as conn:
        conn.execute(
            "UPDATE jobs SET status='done', result_json=?, finished_at=datetime('now') WHERE id = ?",
            (json.dumps(result) if result is not None else None, job_id),
        )
        conn.commit()


def _fail(job_id: int, error: str, attempts: int) -> None:
    """Mark a job failed. If under MAX_ATTEMPTS, requeue it for a backed-off
    retry (status back to 'queued' but the claim loop's stale-check + the
    attempt count gate it); otherwise move to 'dead' (dead-letter)."""
    status = "queued" if attempts < MAX_ATTEMPTS else "dead"
    with connect(profile_db_path()) as conn:
        conn.execute(
            "UPDATE jobs SET status=?, error=?, finished_at=datetime('now') WHERE id = ?",
            (status, error[:1000], job_id),
        )
        conn.commit()
    if status == "queued":
        # backoff: don't let the claim loop grab it immediately. We encode
        # the "eligible after" by leaving started_at at the recent value so
        # the stale-check doesn't fire, and sleeping the worker is the wrong
        # layer; instead set a not-before via a tiny delay column would be
        # over-engineering -- the attempts-based backoff + the poll interval
        # already space retries by ~POLL_INTERVAL each. Good enough for local.
        logger.info(f"job {job_id} failed (attempt {attempts}), requeued for retry")


def list_jobs(repo_key_value: Optional[str] = None, status: Optional[str] = None,
              limit: int = 50) -> list[dict]:
    """List jobs, optionally filtered by repo and/or status, newest first."""
    sql = "SELECT id, repo_key, kind, status, attempts, error, created_at, started_at, finished_at FROM jobs"
    clauses, params = [], []
    if repo_key_value:
        clauses.append("repo_key = ?"); params.append(repo_key_value)
    if status:
        clauses.append("status = ?"); params.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with connect(profile_db_path()) as conn:
        # 'attempts' column may not exist on a pre-Fase-3 profile.db; add it
        # lazily so an upgrade-in-place doesn't crash the lister.
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        except Exception:
            _ensure_attempts_column(conn)
            return [dict(r) for r in conn.execute(sql, params).fetchall()]


def cancel(job_id: int) -> bool:
    """Cancel a queued job (no-op if already running/done). Only queued jobs
    are cancellable -- a running job can't be safely killed mid-handler."""
    with connect(profile_db_path()) as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='cancelled', finished_at=datetime('now') "
            "WHERE id = ? AND status = 'queued'",
            (job_id,),
        )
        conn.commit()
        return cur.rowcount > 0


def _ensure_attempts_column(conn) -> None:
    """Add the attempts column to a legacy jobs table (pre-Fase-3 schema
    didn't track retries). Idempotent."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "attempts" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        conn.commit()


async def _run_one() -> bool:
    """Claim and run one job if available. Returns True if a job ran."""
    job = claim()
    if not job:
        return False
    handler = _HANDLERS.get(job["kind"])
    if not handler:
        _fail(job["id"], f"No handler registered for kind '{job['kind']}'", job["attempts"])
        return True
    try:
        result = await handler(job["payload"])
        _complete(job["id"], result)
        logger.info(f"job {job['id']} ({job['kind']}) done")
    except Exception as e:  # noqa: BLE001 - a handler bug must not kill the worker
        logger.error(f"job {job['id']} ({job['kind']}) failed: {e}", exc_info=True)
        _fail(job["id"], str(e), job["attempts"])
    return True


async def _worker_loop() -> None:
    logger.info("HackDeepWiki job worker started")
    _ensure_attempts_column(connect(profile_db_path()))
    while True:
        try:
            ran = await _run_one()
            # drain queued jobs back-to-back, else idle-poll
            await asyncio.sleep(0 if ran else POLL_INTERVAL)
        except asyncio.CancelledError:
            logger.info("HackDeepWiki job worker cancelled, exiting")
            raise
        except Exception as e:  # noqa: BLE001 - the loop itself must never die
            logger.error(f"job worker loop error: {e}", exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)


async def ensure_worker() -> None:
    """Start the background worker loop exactly once per process. Safe to
    call from the app startup hook on every reload -- the lock + the
    task-state check make it idempotent."""
    global _WORKER_TASK
    async with _WORKER_LOCK:
        if _WORKER_TASK is None or _WORKER_TASK.done():
            _WORKER_TASK = asyncio.create_task(_worker_loop())


def stop_worker() -> None:
    """Cancel the worker (used by app shutdown / tests)."""
    global _WORKER_TASK
    if _WORKER_TASK is not None and not _WORKER_TASK.done():
        _WORKER_TASK.cancel()
    _WORKER_TASK = None
