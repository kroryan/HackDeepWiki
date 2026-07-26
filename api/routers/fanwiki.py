"""MediaWiki XML import, direct reader, and export routes."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import tempfile
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from api import fanwiki_library
from api.security import (
    authorization_is_valid,
    authorize_websocket,
    sanitize_error_message,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["fanwiki"])


class FanwikiInspectRequest(BaseModel):
    path: str = Field(..., description="Local path to a MediaWiki XML export file")


@router.post("/api/fanwiki/inspect")
async def fanwiki_inspect(request: FanwikiInspectRequest):
    path = request.path.strip()
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=400, detail=f"File not found: {path}")
    from api.fanwiki_import import inspect_dump

    try:
        info = await asyncio.to_thread(inspect_dump, path)
    except Exception as exc:
        logger.error("Error inspecting fanwiki dump %s: %s", path, exc)
        raise HTTPException(status_code=400, detail="Could not read MediaWiki XML") from exc
    return {
        "sitename": info.sitename,
        "base_url": info.base_url,
        "dbname": info.dbname,
        "file_size": info.file_size,
        "namespaces": [{"key": ns.key, "name": ns.name} for ns in info.namespaces],
    }


@router.websocket("/ws/fanwiki/import")
async def ws_fanwiki_import(websocket: WebSocket):
    if not await authorize_websocket(websocket):
        return
    await websocket.accept()
    try:
        payload = json.loads(await websocket.receive_text())
        path = (payload.get("path") or "").strip()
        if not path:
            await websocket.send_json({"type": "error", "message": "path is required"})
            return
        if not os.path.isfile(path):
            await websocket.send_json({"type": "error", "message": "File not found"})
            return

        namespaces_payload = payload.get("namespaces")
        allowed_namespaces = (
            set(namespaces_payload) if namespaces_payload is not None else None
        )
        images_dir = (payload.get("images_dir") or "").strip() or None
        from api.fanwiki_import import ImportProgress, import_dump, inspect_dump

        dump_info = await asyncio.to_thread(inspect_dump, path)
        loop = asyncio.get_running_loop()

        def on_progress(progress: ImportProgress) -> None:
            async def _send() -> None:
                try:
                    await websocket.send_json(
                        {
                            "type": "progress",
                            "message": progress.message,
                            "pages_done": progress.pages_done,
                            "percent": progress.percent,
                        }
                    )
                except Exception:
                    logger.debug("Fanwiki progress socket closed")

            asyncio.run_coroutine_threadsafe(_send(), loop)

        result = await asyncio.to_thread(
            import_dump,
            path,
            dump_info,
            allowed_namespaces,
            on_progress,
            bool(payload.get("fresh", False)),
            25,
            payload.get("max_pages"),
            images_dir,
        )
        from api.data_pipeline import _walk_repo_tree

        tree, _ = await asyncio.to_thread(_walk_repo_tree, result["local_dir"])
        await websocket.send_json(
            {
                "type": "done",
                "local_dir": result["local_dir"],
                "page_count": result["page_count"],
                "image_count": result["image_count"],
                "links_resolved": result["links_resolved"],
                "links_unresolved": result["links_unresolved"],
                "start_url": result["start_url"],
                "tree": tree,
            }
        )
    except Exception as exc:
        logger.error("Error in /ws/fanwiki/import: %s", exc)
        try:
            await websocket.send_json(
                {"type": "error", "message": sanitize_error_message(exc)}
            )
        except Exception:
            logger.debug("Fanwiki error socket already closed")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@router.get("/api/fanwiki/structure")
async def fanwiki_structure(
    start_url: str = Query(..., description="The fanwiki's synthetic start URL"),
):
    from api.data_pipeline import _walk_repo_tree
    from api.web_crawler.site_store import website_local_dir

    local_dir = website_local_dir(start_url)
    if not os.path.isdir(local_dir):
        raise HTTPException(status_code=404, detail="No imported fanwiki found")
    tree, _ = await asyncio.to_thread(_walk_repo_tree, local_dir)
    return {"tree": tree, "local_dir": local_dir}


@router.delete("/api/fanwiki/imported")
async def delete_imported_fanwiki(
    start_url: str = Query(..., description="Exact start URL of the imported fanwiki"),
    authorization_code: Optional[str] = Query(None, description="Authorization code"),
):
    if not authorization_is_valid(authorization_code):
        raise HTTPException(status_code=401, detail="Authorization code is invalid")
    try:
        deleted = await asyncio.to_thread(fanwiki_library.delete, start_url)
    except OSError as exc:
        raise HTTPException(
            status_code=409,
            detail="Could not delete imported source files; check ownership and permissions",
        ) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Imported fanwiki source not found")
    return {"message": "Imported fanwiki source deleted successfully"}


class FanwikiRepairLinksRequest(BaseModel):
    start_url: str = Field(..., description="Synthetic fanwiki start URL")


@router.post("/api/fanwiki/repair_links")
async def fanwiki_repair_links(request: FanwikiRepairLinksRequest):
    from api.fanwiki_import import repair_internal_links
    from api.web_crawler.site_store import website_local_dir

    if fanwiki_library.get_by_start_url(request.start_url) is None:
        raise HTTPException(status_code=404, detail="Imported fanwiki source not found")
    local_dir = website_local_dir(request.start_url)
    if not os.path.isdir(local_dir):
        raise HTTPException(status_code=404, detail="No imported fanwiki found")
    try:
        result = await asyncio.to_thread(repair_internal_links, local_dir)
    except Exception as exc:
        logger.error("Error repairing fanwiki links: %s", exc)
        raise HTTPException(status_code=500, detail="Could not repair fanwiki links") from exc
    return {
        "files_scanned": result.files_scanned,
        "links_resolved": result.links_resolved,
        "links_unresolved": result.links_unresolved,
    }


class FanwikiAttachImagesRequest(BaseModel):
    start_url: str = Field(..., description="Synthetic fanwiki start URL")
    images_dir: str = Field(..., description="Local image folder")


@router.post("/api/fanwiki/attach_images")
async def fanwiki_attach_images(request: FanwikiAttachImagesRequest):
    from api.fanwiki_import import attach_images
    from api.web_crawler.site_store import website_local_dir

    if fanwiki_library.get_by_start_url(request.start_url) is None:
        raise HTTPException(status_code=404, detail="Imported fanwiki source not found")
    local_dir = website_local_dir(request.start_url)
    if not os.path.isdir(local_dir):
        raise HTTPException(status_code=404, detail="No imported fanwiki found")
    if not os.path.isdir(request.images_dir):
        raise HTTPException(status_code=400, detail="Images folder not found")
    try:
        result = await asyncio.to_thread(attach_images, local_dir, request.images_dir)
    except Exception as exc:
        logger.error("Error attaching fanwiki images: %s", exc)
        raise HTTPException(status_code=500, detail="Could not attach images") from exc
    return {
        "files_scanned": result.files_scanned,
        "images_attached": result.images_attached,
        "images_still_missing": result.images_still_missing,
    }


def _get_fanwiki_entry_or_404(fanwiki_id: str) -> dict:
    entry = fanwiki_library.get(fanwiki_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Imported fanwiki not found")
    return entry


@router.get("/api/fanwiki/{fanwiki_id}")
async def get_fanwiki_metadata(fanwiki_id: str):
    return _get_fanwiki_entry_or_404(fanwiki_id)


@router.get("/api/fanwiki/{fanwiki_id}/index")
async def get_fanwiki_index(
    fanwiki_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
):
    _get_fanwiki_entry_or_404(fanwiki_id)
    return await asyncio.to_thread(fanwiki_library.page_index, fanwiki_id, offset, limit)


@router.get("/api/fanwiki/{fanwiki_id}/search")
async def search_fanwiki(
    fanwiki_id: str,
    q: str = Query(..., min_length=1),
    limit: int = Query(30, ge=1, le=100),
):
    _get_fanwiki_entry_or_404(fanwiki_id)
    return await asyncio.to_thread(fanwiki_library.search, fanwiki_id, q, limit)


@router.get("/api/fanwiki/{fanwiki_id}/page")
async def get_fanwiki_page(fanwiki_id: str, path: str = Query(..., min_length=1)):
    _get_fanwiki_entry_or_404(fanwiki_id)
    try:
        return await asyncio.to_thread(fanwiki_library.read_page, fanwiki_id, path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Fanwiki page not found") from exc


@router.get("/api/fanwiki/{fanwiki_id}/asset")
async def get_fanwiki_asset(fanwiki_id: str, path: str = Query(..., min_length=1)):
    _get_fanwiki_entry_or_404(fanwiki_id)
    try:
        asset_path = await asyncio.to_thread(fanwiki_library.resolve_asset, fanwiki_id, path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Fanwiki asset not found") from exc
    return FileResponse(
        asset_path,
        media_type=mimetypes.guess_type(asset_path)[0] or "application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/api/fanwiki/{fanwiki_id}/export/{export_format}")
async def export_imported_fanwiki(fanwiki_id: str, export_format: str):
    entry = _get_fanwiki_entry_or_404(fanwiki_id)
    if export_format not in {"obsidian", "hdwreader", "zim"}:
        raise HTTPException(
            status_code=400,
            detail="Unsupported export format. Use obsidian, hdwreader, or zim.",
        )
    suffix = {"obsidian": ".zip", "hdwreader": ".hdwreader", "zim": ".zim"}[
        export_format
    ]
    file_descriptor, archive_path = tempfile.mkstemp(
        prefix="hackdeepwiki-export-", suffix=suffix
    )
    os.close(file_descriptor)
    try:
        exporter = {
            "obsidian": fanwiki_library.export_obsidian,
            "hdwreader": fanwiki_library.export_hdwreader,
            "zim": fanwiki_library.export_zim,
        }[export_format]
        result = await asyncio.to_thread(exporter, fanwiki_id, archive_path)
    except (KeyError, FileNotFoundError) as exc:
        try:
            os.unlink(archive_path)
        except FileNotFoundError:
            pass
        raise HTTPException(
            status_code=404, detail="Imported fanwiki source is incomplete"
        ) from exc
    except Exception as exc:
        try:
            os.unlink(archive_path)
        except FileNotFoundError:
            pass
        logger.exception("Failed to export imported fanwiki")
        raise HTTPException(status_code=500, detail="Failed to export fanwiki") from exc

    safe_name = re.sub(
        r"[^A-Za-z0-9._-]+", "_", str(entry.get("repo") or "fanwiki")
    ).strip("._")
    filename = {
        "obsidian": f"{safe_name or 'fanwiki'}_obsidian.zip",
        "hdwreader": f"{safe_name or 'fanwiki'}.hdwreader",
        "zim": f"{safe_name or 'fanwiki'}.zim",
    }[export_format]
    return FileResponse(
        archive_path,
        media_type=(
            "application/zip"
            if export_format != "zim"
            else "application/octet-stream"
        ),
        filename=filename,
        headers={
            "X-HackDeepWiki-Page-Count": str(result["page_count"]),
            "X-HackDeepWiki-Asset-Count": str(result["asset_count"]),
        },
        background=BackgroundTask(os.unlink, archive_path),
    )
