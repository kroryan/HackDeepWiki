"""Single-process application lifecycle coordination."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def application_lifespan(_app):
    """Start embedded workers/adapters and shut every child down cleanly."""
    worker_started = False
    try:
        from api.jobs.queue import ensure_worker
        from api.memory.jobs import register_memory_jobs

        register_memory_jobs()
        await ensure_worker()
        worker_started = True
        logger.info("Job worker started")
    except Exception as exc:  # optional boundary
        logger.warning("Job worker failed to start: %s", exc)

    try:
        from api.cache_eviction import prune_wiki_cache

        prune_wiki_cache()
    except Exception as exc:  # optional maintenance boundary
        logger.warning("Wiki cache startup prune skipped: %s", exc)

    try:
        from api.memory.service import get_memory_service

        threading.Thread(
            target=lambda: get_memory_service().status(),
            name="engraphis-warmup",
            daemon=True,
        ).start()
    except Exception as exc:  # optional adapter boundary
        logger.warning("Engraphis warmup skipped: %s", exc)

    try:
        yield
    finally:
        if worker_started:
            try:
                from api.jobs.queue import stop_worker

                stop_worker()
            except Exception as exc:
                logger.warning("Job worker stop failed: %s", exc)
        try:
            from api.code_agent.service import code_agent

            code_agent.shutdown_all_sync()
        except Exception as exc:
            logger.warning("Code Agent shutdown failed: %s", exc)
        try:
            from api.memory.service import get_memory_service

            get_memory_service().close()
        except Exception as exc:
            logger.warning("Engraphis shutdown failed: %s", exc)
