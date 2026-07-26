"""FastAPI application factory.

Keeping construction here makes middleware, error boundaries and router
registration testable without importing the 3k-line legacy route module.  The
remaining domains are migrated incrementally into ``api.routers``; ``api.api``
is a compatibility composition root during that migration.
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.domain import DomainError
from api.observability import record_http_request
from api.security import authorization_middleware

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]
logger = logging.getLogger(__name__)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,96}$")


def create_app(*, lifespan: Lifespan | None = None) -> FastAPI:
    app = FastAPI(
        title="HackDeepWiki API",
        description="Local-first code analysis, wiki generation and memory API",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _authorization_boundary(request: Request, call_next):
        return await authorization_middleware(request, call_next)

    @app.middleware("http")
    async def _request_identity(request: Request, call_next):
        supplied = request.headers.get("X-Request-ID", "")
        request_id = (
            supplied if _REQUEST_ID.fullmatch(supplied) else secrets.token_hex(12)
        )
        request.state.request_id = request_id
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            record_http_request(
                request.method,
                request.url.path,
                500,
                time.monotonic() - started,
            )
            raise
        route = getattr(request.scope.get("route"), "path", request.url.path)
        record_http_request(
            request.method,
            str(route),
            response.status_code,
            time.monotonic() - started,
        )
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError):
        logger.info(
            "domain error request_id=%s code=%s",
            getattr(request.state, "request_id", ""),
            exc.code,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc), "code": exc.code},
        )

    return app
