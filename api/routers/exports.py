"""Wiki export endpoints."""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from api.exports import (
    generate_hdwreader_export,
    generate_json_export,
    generate_markdown_export,
    generate_obsidian_vault_export,
    generate_zim_export,
)
from api.models import WikiExportRequest

logger = logging.getLogger(__name__)
router = APIRouter(tags=["exports"])


@router.post("/export/wiki")
async def export_wiki(request: WikiExportRequest):
    try:
        logger.info("Exporting wiki for %s in %s format", request.repo_url, request.format)
        repo_name = request.repo_url.rstrip("/").split("/")[-1] or "wiki"
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if request.format == "markdown":
            content = generate_markdown_export(request.repo_url, request.pages)
            filename = f"{repo_name}_wiki_{timestamp}.md"
            media_type = "text/markdown"
        elif request.format == "obsidian":
            content = generate_obsidian_vault_export(
                request.repo_url,
                request.pages,
                title=request.title or f"{repo_name} Wiki",
                version=request.version,
                vuln_report=request.vuln_report,
                include_vulns=request.include_vulns,
                include_vuln_graph=request.include_vuln_graph,
            )
            suffix = f"_v{request.version}" if request.version else ""
            filename = f"{repo_name}_wiki{suffix}_{timestamp}_obsidian.zip"
            media_type = "application/zip"
        elif request.format == "hdwreader":
            content = generate_hdwreader_export(
                repo_url=request.repo_url,
                repo_type=request.repo_type or "github",
                owner=request.owner or "",
                repo=request.repo or repo_name,
                pages=request.pages,
                sections=request.sections or [],
                root_sections=request.root_sections or [],
                title=request.title or f"{repo_name} Wiki",
                description=request.description or "",
                language=request.language or "en",
                provider=request.provider or "",
                model=request.model or "",
                version=request.version,
                vuln_report=request.vuln_report,
                include_vulns=request.include_vulns,
                web_vuln_report=request.web_vuln_report,
                include_web_vulns=request.include_web_vulns,
            )
            suffix = f"_v{request.version}" if request.version else ""
            filename = f"{repo_name}_wiki{suffix}_{timestamp}.hdwreader"
            media_type = "application/zip"
        elif request.format == "mediawiki_xml":
            from api.fanwiki_import import export_mediawiki_xml

            content = export_mediawiki_xml(
                pages=request.pages,
                sitename=request.title or f"{repo_name} Wiki",
                base_url=request.repo_url,
                language=request.language or "en",
            )
            suffix = f"_v{request.version}" if request.version else ""
            filename = f"{repo_name}_wiki{suffix}_{timestamp}.xml"
            media_type = "application/xml"
        elif request.format == "zim":
            content = generate_zim_export(
                repo_url=request.repo_url,
                pages=request.pages,
                title=request.title or f"{repo_name} Wiki",
                description=request.description or "",
                language=request.language or "en",
            )
            suffix = f"_v{request.version}" if request.version else ""
            filename = f"{repo_name}_wiki{suffix}_{timestamp}.zim"
            media_type = "application/octet-stream"
        else:
            content = generate_json_export(request.repo_url, request.pages)
            filename = f"{repo_name}_wiki_{timestamp}.json"
            media_type = "application/json"

        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error exporting wiki")
        raise HTTPException(status_code=500, detail="Failed to export wiki") from exc
