"""Liveness, readiness and capability diagnostics."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response

from api.build_info import build_info
from api.data_root import get_data_root
from api.observability import metrics_snapshot
from api.settings import get_settings

router = APIRouter(tags=["health"])


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/health")
@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {
        "status": "healthy",
        "timestamp": _utc_now(),
        "service": "hackdeepwiki-api",
    }


def readiness_snapshot() -> tuple[dict[str, Any], bool]:
    checks: dict[str, dict[str, Any]] = {}
    ready = True

    try:
        data_root = Path(get_data_root())
        data_root.mkdir(parents=True, exist_ok=True)
        probe = data_root / ".health-write-probe"
        probe.touch(exist_ok=True)
        probe.unlink(missing_ok=True)
        usage = shutil.disk_usage(data_root)
        checks["storage"] = {
            "status": "ready" if usage.free >= 64 * 1024 * 1024 else "failed",
            "free_bytes": usage.free,
        }
        if usage.free < 64 * 1024 * 1024:
            ready = False
    except Exception as exc:  # boundary: readiness must report, not crash
        ready = False
        checks["storage"] = {
            "status": "failed",
            "detail": type(exc).__name__,
        }

    try:
        from api.jobs.queue import worker_status

        checks["jobs"] = worker_status()
        if checks["jobs"].get("status") == "failed":
            ready = False
    except Exception as exc:
        checks["jobs"] = {
            "status": "degraded",
            "detail": type(exc).__name__,
        }

    try:
        from api.storage import connect, database_integrity, profile_db_path

        path = profile_db_path()
        connection = connect(path)
        connection.execute("SELECT 1").fetchone()
        connection.close()
        db_check = database_integrity(path)
        checks["database"] = {
            "status": "ready" if db_check["ok"] else "failed",
            "integrity": db_check["messages"],
        }
        if not db_check["ok"]:
            ready = False
    except Exception as exc:
        ready = False
        checks["database"] = {
            "status": "failed",
            "detail": type(exc).__name__,
        }

    try:
        from api.code_agent.binary import resolve_opencode_binary

        binary = resolve_opencode_binary()
        checks["opencode"] = {
            "status": "ready" if binary else "degraded",
            "installed": bool(binary),
        }
    except Exception as exc:
        checks["opencode"] = {
            "status": "degraded",
            "detail": type(exc).__name__,
        }

    try:
        from api.memory.service import get_memory_service

        memory = get_memory_service().status()
        checks["engraphis"] = {
            "status": memory.state,
            "available": memory.available,
            "semantic": memory.semantic,
        }
    except Exception as exc:
        checks["engraphis"] = {
            "status": "degraded",
            "detail": type(exc).__name__,
        }

    return (
        {
            "status": "ready" if ready else "not_ready",
            "timestamp": _utc_now(),
            "checks": checks,
        },
        ready,
    )


@router.get("/health/ready")
async def readiness(response: Response) -> dict[str, Any]:
    snapshot, ready = readiness_snapshot()
    if not ready:
        response.status_code = 503
    return snapshot


@router.get("/health/capabilities")
async def capabilities() -> dict[str, Any]:
    from api import engraphis_integration
    from api.code_agent.binary import (
        OPENCODE_VERSION,
        installed_opencode_version,
        resolve_opencode_binary,
    )

    opencode_path = resolve_opencode_binary()
    return {
        "timestamp": _utc_now(),
        "deployment": get_settings().public_diagnostic(),
        "components": {
            "opencode": {
                "pinned": OPENCODE_VERSION.lstrip("v"),
                "installed": (
                    installed_opencode_version(opencode_path)
                    if opencode_path
                    else None
                ),
            },
            "engraphis": engraphis_integration.status(),
        },
    }


@router.get("/health/build")
async def build_identity() -> dict[str, Any]:
    return build_info()


@router.get("/health/metrics")
async def runtime_metrics() -> dict[str, Any]:
    return metrics_snapshot()


@router.get("/")
async def api_index(request: Request) -> dict[str, Any]:
    endpoints: dict[str, list[str]] = {}
    ignored = {"/openapi.json", "/docs", "/redoc", "/favicon.ico"}
    for route in request.app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or not path or path in ignored:
            continue
        group = path.strip("/").split("/")[0].capitalize() or "Root"
        for method in methods - {"HEAD", "OPTIONS"}:
            endpoints.setdefault(group, []).append(f"{method} {path}")
    for values in endpoints.values():
        values.sort()
    return {
        "message": "HackDeepWiki API",
        "build": build_info(),
        "endpoints": endpoints,
    }
