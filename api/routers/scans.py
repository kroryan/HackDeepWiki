"""Dependency and website security scan routes."""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket

from api.security import authorize_websocket, sanitize_error_message
from api.services.security_cache import (
    LEGACY_VULN_CACHE_PREFIX,
    LEGACY_WEB_VULN_CACHE_PREFIX,
    list_cache_files_for_prefix,
    list_vuln_cache_releases,
    list_web_vuln_cache_releases,
    parse_cache_version,
    read_vuln_cache,
    read_web_vuln_cache,
    save_vuln_cache,
    save_web_vuln_cache,
    split_newline_filters,
    vuln_cache_prefix,
    web_vuln_cache_prefix,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["security-scans"])


async def _socket_error(websocket: WebSocket, exc: Exception) -> None:
    logger.error("Security scan failed: %s", exc)
    try:
        await websocket.send_json(
            {"type": "error", "message": sanitize_error_message(exc)}
        )
    except Exception:
        logger.debug("Security scan socket already closed")


async def _close_socket(websocket: WebSocket) -> None:
    try:
        await websocket.close()
    except Exception:
        pass


@router.websocket("/ws/vuln_scan")
async def ws_vuln_scan(websocket: WebSocket):
    if not await authorize_websocket(websocket):
        return
    await websocket.accept()
    try:
        payload = json.loads(await websocket.receive_text())
        repo_url = (payload.get("repo_url") or "").strip()
        repo_type = payload.get("repo_type") or "github"
        local_path = (payload.get("local_path") or "").strip()
        if not repo_url and not local_path:
            await websocket.send_json(
                {"type": "error", "message": "repo_url or local_path is required"}
            )
            return

        if repo_type == "local":
            repo_dir = local_path or repo_url
        else:
            from api.data_pipeline import (
                _local_clone_dir,
                clone_repo_with_progress,
            )

            repo_dir = _local_clone_dir(repo_url, repo_type)
            if bool(payload.get("force", False)) and repo_url:
                await websocket.send_json(
                    {
                        "type": "progress",
                        "message": "Refreshing repository clone…",
                        "percent": 0,
                    }
                )
                try:
                    await clone_repo_with_progress(
                        repo_url,
                        repo_dir,
                        repo_type,
                        payload.get("token") or None,
                        None,
                        force=True,
                    )
                except Exception as exc:
                    logger.warning(
                        "Force re-clone failed; scanning existing clone: %s", exc
                    )
        if not repo_dir or not os.path.isdir(repo_dir):
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "Repository clone not found; generate the wiki first",
                }
            )
            return

        from api.vuln_scanner.orchestrator import run_vuln_scan

        async def on_progress(message: str, percent: Optional[int] = None):
            await websocket.send_json(
                {"type": "progress", "message": message, "percent": percent}
            )

        report = await run_vuln_scan(
            repo_dir=repo_dir,
            repo_url=repo_url,
            repo_type=repo_type,
            owner=payload.get("owner") or "",
            repo=payload.get("repo") or "",
            language=payload.get("language") or "en",
            provider=payload.get("provider") or "google",
            model=payload.get("model") or None,
            api_key=payload.get("api_key") or None,
            api_endpoint=payload.get("api_endpoint") or None,
            excluded_dirs=split_newline_filters(payload.get("excluded_dirs")),
            excluded_files=split_newline_filters(payload.get("excluded_files")),
            nvd_key=payload.get("nvd_key") or None,
            enable_client=bool(payload.get("enable_client", True)),
            enable_server=bool(payload.get("enable_server", True)),
            enable_deps=bool(payload.get("enable_deps", True)),
            run_llm=bool(payload.get("run_llm", True)),
            on_progress=on_progress,
        )
        report_dict = report.to_dict()
        saved_version: Optional[int] = None
        try:
            _, saved_version = save_vuln_cache(report_dict)
        except Exception as exc:
            logger.warning("Failed to persist vulnerability cache: %s", exc)
        await websocket.send_json(
            {"type": "done", "report": report_dict, "version": saved_version}
        )
    except Exception as exc:
        await _socket_error(websocket, exc)
    finally:
        await _close_socket(websocket)


@router.get("/api/vuln_cache")
async def get_vuln_cache(
    owner: str = Query(..., description="Repository owner"),
    repo: str = Query(..., description="Repository name"),
    repo_type: str = Query(..., description="Repository type"),
    language: str = Query("en", description="Wiki language"),
    version: Optional[int] = Query(None, description="Specific scan release"),
):
    data = read_vuln_cache(repo_type, owner, repo, language, version)
    if data is None:
        raise HTTPException(status_code=404, detail="No vulnerability scan found")
    return data


@router.get("/api/vuln_cache/releases")
async def get_vuln_cache_releases(
    owner: str = Query(...),
    repo: str = Query(...),
    repo_type: str = Query(...),
    language: str = Query("en"),
):
    releases = list_vuln_cache_releases(repo_type, owner, repo, language)
    return {"releases": releases, "latest": releases[0]["version"] if releases else None}


@router.websocket("/ws/web_vuln_scan")
async def ws_web_vuln_scan(websocket: WebSocket):
    if not await authorize_websocket(websocket):
        return
    await websocket.accept()
    try:
        payload = json.loads(await websocket.receive_text())
        site_url = (payload.get("site_url") or "").strip()
        if not site_url:
            await websocket.send_json(
                {"type": "error", "message": "site_url is required"}
            )
            return
        from api.web_vuln_scanner.orchestrator import run_web_vuln_scan

        async def on_progress(message: str, percent: Optional[int] = None):
            await websocket.send_json(
                {"type": "progress", "message": message, "percent": percent}
            )

        report = await run_web_vuln_scan(
            site_url=site_url,
            owner=payload.get("owner") or "website",
            repo=payload.get("repo") or "",
            language=payload.get("language") or "en",
            provider=payload.get("provider") or "google",
            model=payload.get("model") or None,
            api_key=payload.get("api_key") or None,
            api_endpoint=payload.get("api_endpoint") or None,
            run_llm=bool(payload.get("run_llm", True)),
            enable_deep_scan=bool(payload.get("enable_deep_scan", False)),
            on_progress=on_progress,
        )
        report_dict = report.to_dict()
        saved_version: Optional[int] = None
        try:
            _, saved_version = save_web_vuln_cache(report_dict)
        except Exception as exc:
            logger.warning("Failed to persist website scan cache: %s", exc)
        await websocket.send_json(
            {"type": "done", "report": report_dict, "version": saved_version}
        )
    except Exception as exc:
        await _socket_error(websocket, exc)
    finally:
        await _close_socket(websocket)


@router.get("/api/web_vuln_cache")
async def get_web_vuln_cache(
    owner: str = Query(..., description="Repository owner"),
    repo: str = Query(..., description="Site hostname"),
    language: str = Query("en"),
    version: Optional[int] = Query(None),
):
    data = read_web_vuln_cache(owner, repo, language, version)
    if data is None:
        raise HTTPException(status_code=404, detail="No website vulnerability scan found")
    return data


@router.get("/api/web_vuln_cache/releases")
async def get_web_vuln_cache_releases(
    owner: str = Query(...),
    repo: str = Query(...),
    language: str = Query("en"),
):
    releases = list_web_vuln_cache_releases(owner, repo, language)
    return {"releases": releases, "latest": releases[0]["version"] if releases else None}


def _delete_reports(prefixes: list[str], version: Optional[int]) -> int:
    deleted = 0
    for prefix in prefixes:
        for path in list_cache_files_for_prefix(prefix):
            if version is not None and parse_cache_version(os.path.basename(path)) != version:
                continue
            try:
                os.remove(path)
                deleted += 1
            except OSError as exc:
                logger.error("Could not delete security cache %s: %s", path, exc)
                raise HTTPException(
                    status_code=500, detail="Failed to delete security cache"
                ) from exc
    return deleted


@router.delete("/api/vuln_cache")
async def delete_vuln_cache_release(
    owner: str = Query(...),
    repo: str = Query(...),
    repo_type: str = Query(...),
    language: str = Query("en"),
    version: Optional[int] = Query(None, ge=0),
):
    prefixes = [
        vuln_cache_prefix(repo_type, owner, repo, language),
        vuln_cache_prefix(
            repo_type, owner, repo, language, LEGACY_VULN_CACHE_PREFIX
        ),
    ]
    if not _delete_reports(prefixes, version):
        raise HTTPException(status_code=404, detail="Vulnerability scan cache not found")
    return {"message": f"Vulnerability scan cache for {owner}/{repo} ({language}) deleted successfully"}


@router.delete("/api/web_vuln_cache")
async def delete_web_vuln_cache_release(
    owner: str = Query(...),
    repo: str = Query(...),
    language: str = Query("en"),
    version: Optional[int] = Query(None, ge=0),
):
    prefixes = [
        web_vuln_cache_prefix(owner, repo, language),
        web_vuln_cache_prefix(
            owner, repo, language, LEGACY_WEB_VULN_CACHE_PREFIX
        ),
    ]
    if not _delete_reports(prefixes, version):
        raise HTTPException(
            status_code=404, detail="Website vulnerability scan cache not found"
        )
    return {"message": f"Website vulnerability scan cache for {owner}/{repo} ({language}) deleted successfully"}
