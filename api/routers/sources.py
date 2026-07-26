"""Source-file, repository clone, website crawl, and filesystem routes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket

from api import fanwiki_library
from api.models import FileContentRequest, RepoStructureRequest
from api.security import authorize_websocket, sanitize_error_message
from api.services.security_cache import split_newline_filters

logger = logging.getLogger(__name__)
router = APIRouter(tags=["sources"])


@router.post("/api/wiki/file_content")
async def get_wiki_file_content(request_data: FileContentRequest):
    from api.data_pipeline import get_file_content

    try:
        content = get_file_content(
            request_data.repo_url,
            request_data.file_path,
            request_data.repo_type,
            request_data.token,
        )
    except Exception as exc:
        logger.error("Could not read repository file: %s", exc)
        raise HTTPException(status_code=404, detail="Could not read file") from exc
    return {"file_path": request_data.file_path, "content": content}


@router.post("/api/repo/structure")
async def get_repo_structure_endpoint(request_data: RepoStructureRequest):
    from api.data_pipeline import get_repo_structure

    try:
        return await asyncio.to_thread(
            get_repo_structure,
            request_data.repo_url,
            request_data.repo_type,
            request_data.token,
            request_data.force,
        )
    except Exception as exc:
        logger.error("Could not build repository structure: %s", exc)
        raise HTTPException(
            status_code=502, detail="Could not read repository structure"
        ) from exc


async def _socket_error(websocket: WebSocket, exc: Exception) -> None:
    logger.error("Source operation failed: %s", exc)
    try:
        await websocket.send_json(
            {"type": "error", "message": sanitize_error_message(exc)}
        )
    except Exception:
        logger.debug("Source operation socket already closed")


async def _close_socket(websocket: WebSocket) -> None:
    try:
        await websocket.close()
    except Exception:
        pass


@router.websocket("/ws/repo/clone")
async def ws_repo_clone(websocket: WebSocket):
    if not await authorize_websocket(websocket):
        return
    await websocket.accept()
    try:
        payload = json.loads(await websocket.receive_text())
        repo_url = (payload.get("repo_url") or "").strip()
        if not repo_url:
            await websocket.send_json(
                {"type": "error", "message": "repo_url is required"}
            )
            return
        from api.data_pipeline import (
            _local_clone_dir,
            _repo_default_branch,
            _walk_repo_tree,
            clone_repo_with_progress,
        )

        local_dir = _local_clone_dir(
            repo_url, payload.get("repo_type") or "github"
        )

        async def on_progress(event):
            await websocket.send_json({"type": "progress", **event})

        await clone_repo_with_progress(
            repo_url,
            local_dir,
            payload.get("repo_type") or "github",
            payload.get("token") or None,
            on_progress,
            force=bool(payload.get("force", False)),
        )
        tree, readme = await asyncio.to_thread(_walk_repo_tree, local_dir)
        branch = await asyncio.to_thread(_repo_default_branch, local_dir)
        await websocket.send_json(
            {
                "type": "done",
                "default_branch": branch,
                "tree": tree,
                "readme": readme,
            }
        )
    except Exception as exc:
        await _socket_error(websocket, exc)
    finally:
        await _close_socket(websocket)


def _empty_crawl_reason(diagnostics: dict) -> str:
    if diagnostics.get("bot_challenge"):
        return "El sitio está protegido por un desafío anti-bots no automatizable."
    if diagnostics.get("robots_blocked"):
        return "El robots.txt no permite el rastreo."
    if diagnostics.get("http_error"):
        return "El sitio devolvió un error HTTP."
    if diagnostics.get("fetch_failed"):
        return "No se pudo conectar con el sitio."
    return "No se encontró contenido de página válido."


@router.websocket("/ws/website/crawl")
async def ws_website_crawl(websocket: WebSocket):
    if not await authorize_websocket(websocket):
        return
    await websocket.accept()
    try:
        payload = json.loads(await websocket.receive_text())
        start_url = (payload.get("start_url") or "").strip()
        if not start_url:
            await websocket.send_json(
                {"type": "error", "message": "start_url is required"}
            )
            return
        if not start_url.startswith(("http://", "https://")):
            start_url = f"https://{start_url}"
        scope_payload = payload.get("scope") or {}
        from api.web_crawler.models import CrawlScope
        from api.web_crawler.orchestrator import run_site_crawl

        scope = CrawlScope(
            mode=scope_payload.get("mode") or "count",
            max_pages=int(scope_payload.get("max_pages") or 60),
            subdomains=split_newline_filters(scope_payload.get("subdomains")),
            respect_robots=bool(scope_payload.get("respect_robots", True)),
        )

        async def on_progress(event):
            await websocket.send_json(
                {
                    "type": "progress",
                    "message": event.message,
                    "pages_done": event.pages_done,
                    "percent": event.percent,
                }
            )

        result = await run_site_crawl(
            start_url, scope, on_progress, fresh=bool(payload.get("fresh", False))
        )
        if result["page_count"] == 0:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "El rastreo no encontró ninguna página. "
                    + _empty_crawl_reason(result.get("diagnostics") or {}),
                }
            )
            return
        from api.data_pipeline import _walk_repo_tree

        tree, _ = await asyncio.to_thread(_walk_repo_tree, result["local_dir"])
        library_entry = await asyncio.to_thread(
            fanwiki_library.get_by_start_url, result["start_url"]
        )
        await websocket.send_json(
            {
                "type": "done",
                "id": library_entry["id"] if library_entry else None,
                "local_dir": result["local_dir"],
                "page_count": result["page_count"],
                "tree": tree,
            }
        )
    except Exception as exc:
        await _socket_error(websocket, exc)
    finally:
        await _close_socket(websocket)


@router.get("/api/fs/list")
async def fs_list(
    path: Optional[str] = Query(None),
    extensions: Optional[str] = Query(None),
):
    target = os.path.abspath(path or os.path.expanduser("~"))
    if not os.path.isdir(target):
        raise HTTPException(status_code=400, detail="Not a directory")
    extension_filter = (
        {item.strip().lower() for item in extensions.split(",") if item.strip()}
        if extensions
        else None
    )
    entries: list[dict] = []
    try:
        with os.scandir(target) as directory:
            for entry in directory:
                if entry.name.startswith("."):
                    continue
                try:
                    is_directory = entry.is_dir(follow_symlinks=True)
                except OSError:
                    continue
                if not is_directory and extension_filter:
                    if os.path.splitext(entry.name)[1].lower() not in extension_filter:
                        continue
                size: Optional[int] = None
                if not is_directory:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        pass
                entries.append(
                    {"name": entry.name, "is_dir": is_directory, "size": size}
                )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="Permission denied") from exc
    entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
    root = os.path.abspath(os.sep)
    return {
        "path": target,
        "parent": None if target == root else (os.path.dirname(target) or root),
        "entries": entries,
    }
