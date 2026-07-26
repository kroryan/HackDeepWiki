"""Backup, integrity and redacted diagnostic operations."""

from __future__ import annotations

import io
import json
import logging
import os
import re
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Response

from api.build_info import build_info
from api.data_root import get_data_root
from api.memory.service import get_memory_service
from api.routers.health import readiness_snapshot
from api.security import sanitize_error_message
from api.settings import get_settings
from api.storage import (
    backup_all_databases,
    database_integrity,
    profile_db_path,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/storage", tags=["storage"])
_SAFE_BACKUP_NAME = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


@router.get("/integrity")
async def integrity() -> dict[str, Any]:
    root = Path(profile_db_path()).parent
    checks = {}
    for path in sorted(root.glob("*.db")):
        try:
            checks[path.name] = database_integrity(str(path))
        except Exception as exc:
            checks[path.name] = {
                "ok": False,
                "messages": [type(exc).__name__],
            }
    return {
        "ok": all(value["ok"] for value in checks.values()),
        "databases": checks,
    }


@router.post("/backup")
async def backup(name: str = "manual") -> dict[str, Any]:
    if not _SAFE_BACKUP_NAME.fullmatch(name):
        raise HTTPException(400, "Invalid backup name")
    destination = Path(get_data_root()) / "backups" / name
    try:
        paths = backup_all_databases(str(destination))
    except Exception as exc:
        logger.error("Backup failed: %s", exc, exc_info=True)
        raise HTTPException(500, sanitize_error_message(str(exc))) from exc
    return {
        "created": len(paths),
        "directory": destination.name,
        "files": [Path(path).name for path in paths],
    }


def _redacted_log_tail(limit: int = 200) -> list[str]:
    path = Path(os.environ.get("LOG_FILE_PATH", "api/logs/application.log"))
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [sanitize_error_message(line) for line in lines[-limit:]]


@router.get("/diagnostics")
async def diagnostics() -> dict[str, Any]:
    readiness, _ready = readiness_snapshot()
    return {
        "build": build_info(),
        "settings": get_settings().public_diagnostic(),
        "readiness": readiness,
        "memory": asdict(get_memory_service().status()),
        "logs": _redacted_log_tail(),
        "notice": "Secrets and database contents are intentionally excluded.",
    }


@router.get("/diagnostics/export")
async def export_diagnostics() -> Response:
    payload = await diagnostics()
    logs = payload.pop("logs", [])
    archive = io.BytesIO()
    with zipfile.ZipFile(
        archive,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as bundle:
        bundle.writestr(
            "diagnostics.json",
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        )
        bundle.writestr("recent.log", "\n".join(logs) + "\n")
        bundle.writestr(
            "README.txt",
            "Redacted HackDeepWiki diagnostics. Databases and secrets are "
            "intentionally excluded.\n",
        )
    return Response(
        archive.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                "attachment; filename=hackdeepwiki-diagnostics.zip"
            )
        },
    )
