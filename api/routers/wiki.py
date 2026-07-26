"""Wiki release CRUD, page editing, pruning, and mind-map routes."""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from api.config import (
    get_model_config,
    is_supported_language,
    language_display_name,
    normalize_language,
)
from api.models import PageEditAIRequest, PageEditRequest, WikiCacheData, WikiCacheRequest
from api.prompts import PAGE_EDIT_AI_SYSTEM_PROMPT
from api.provider_streaming import stream_provider_response
from api.security import authorization_is_valid
from api.services.wiki_cache import (
    delete_local_repo_clone,
    list_repo_cache_files,
    parse_cache_version,
    read_wiki_cache,
    repo_has_any_cache,
    save_wiki_cache,
)
from api.wiki_cache_paths import WIKI_CACHE_DIR, repo_cache_prefixes

logger = logging.getLogger(__name__)
router = APIRouter(tags=["wiki"])


@router.get("/api/wiki_cache", response_model=Optional[WikiCacheData])
async def get_cached_wiki(
    owner: str = Query(..., description="Repository owner"),
    repo: str = Query(..., description="Repository name"),
    repo_type: str = Query(..., description="Repository type"),
    language: str = Query(..., description="Language"),
    comprehensive: Optional[bool] = Query(None),
    page_count: Optional[int] = Query(None, ge=1, le=50),
    version: Optional[int] = Query(None, ge=0),
):
    return await read_wiki_cache(
        owner,
        repo,
        repo_type,
        normalize_language(language),
        comprehensive,
        page_count,
        version,
    )


@router.get("/api/wiki_cache/releases")
async def list_wiki_releases(
    owner: str = Query(...),
    repo: str = Query(...),
    repo_type: str = Query(...),
    language: str = Query(...),
):
    releases: list[dict] = []
    for path in list_repo_cache_files(
        repo_type, owner, repo, normalize_language(language)
    ):
        filename = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as cache_file:
                cached = WikiCacheData(**json.load(cache_file))
            releases.append(
                {
                    "version": parse_cache_version(filename),
                    "created_at": int(os.path.getmtime(path) * 1000),
                    "comprehensive": cached.comprehensive,
                    "page_count": len(cached.wiki_structure.pages),
                    "provider": cached.provider,
                    "model": cached.model,
                    "title": cached.wiki_structure.title,
                    "id": filename,
                }
            )
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Could not read release metadata %s: %s", filename, exc)
    releases.sort(key=lambda item: (item["version"], item["created_at"]), reverse=True)
    return {"releases": releases, "latest": releases[0]["version"] if releases else None}


@router.post("/api/wiki_cache")
async def store_wiki_cache(request_data: WikiCacheRequest):
    request_data.language = normalize_language(request_data.language)
    version = await save_wiki_cache(request_data)
    if version is None:
        raise HTTPException(status_code=500, detail="Failed to save wiki cache")
    return {"message": "Wiki cache saved successfully", "version": version}


@router.patch("/api/wiki_cache/page")
async def edit_wiki_page(request_data: PageEditRequest):
    language = normalize_language(request_data.language)
    cached = await read_wiki_cache(
        request_data.repo.owner,
        request_data.repo.repo,
        request_data.repo.type,
        language,
        version=request_data.version,
    )
    if cached is None:
        raise HTTPException(status_code=404, detail="Wiki cache not found")
    if request_data.page_id not in cached.generated_pages:
        raise HTTPException(status_code=404, detail="Page not found")
    pages = dict(cached.generated_pages)
    pages[request_data.page_id] = pages[request_data.page_id].model_copy(
        update={"content": request_data.content}
    )
    version = await save_wiki_cache(
        WikiCacheRequest(
            repo=request_data.repo,
            language=language,
            wiki_structure=cached.wiki_structure,
            generated_pages=pages,
            provider=cached.provider or "",
            model=cached.model or "",
            comprehensive=cached.comprehensive
            if cached.comprehensive is not None
            else True,
            page_count=cached.page_count or len(cached.wiki_structure.pages),
        )
    )
    if version is None:
        raise HTTPException(status_code=500, detail="Failed to save edited page")
    return {"message": "Page updated successfully", "version": version, "page_id": request_data.page_id}


@router.post("/api/wiki/page/edit/stream")
async def edit_wiki_page_ai_stream(request_data: PageEditAIRequest):
    prompt = PAGE_EDIT_AI_SYSTEM_PROMPT.format(
        page_title=request_data.page_title,
        current_content=request_data.current_content,
        instruction=request_data.instruction,
        language_name=language_display_name(request_data.language),
    )
    model_config = get_model_config(request_data.provider, request_data.model)[
        "model_kwargs"
    ]

    async def response_stream():
        try:
            async for text in stream_provider_response(
                provider=request_data.provider,
                requested_model=request_data.model,
                prompt=prompt,
                model_config_kwargs=model_config,
                api_key=request_data.api_key,
                api_endpoint=request_data.api_endpoint,
            ):
                yield text
        except Exception as exc:
            logger.error("Page edit AI stream failed: %s", exc)
            yield "\nError: page edit generation failed"

    return StreamingResponse(response_stream(), media_type="text/event-stream")


@router.delete("/api/wiki_cache")
async def delete_wiki_cache(
    owner: str = Query(...),
    repo: str = Query(...),
    repo_type: str = Query(...),
    language: str = Query(...),
    authorization_code: Optional[str] = Query(None),
    comprehensive: Optional[bool] = Query(None),
    page_count: Optional[int] = Query(None, ge=1, le=50),
    version: Optional[int] = Query(None, ge=0),
):
    del comprehensive, page_count
    if not is_supported_language(language):
        raise HTTPException(status_code=400, detail="Language is not supported")
    if not authorization_is_valid(authorization_code):
        raise HTTPException(status_code=401, detail="Authorization code is invalid")
    prefixes = repo_cache_prefixes(repo_type, owner, repo, language)
    try:
        paths = [
            os.path.join(WIKI_CACHE_DIR, filename)
            for filename in os.listdir(WIKI_CACHE_DIR)
            if filename.endswith(".json")
            and any(
                filename == f"{prefix}.json" or filename.startswith(f"{prefix}_")
                for prefix in prefixes
            )
            and (
                version is None
                or parse_cache_version(filename) == version
            )
        ]
        deleted = 0
        for path in dict.fromkeys(paths):
            if os.path.exists(path):
                os.remove(path)
                deleted += 1
    except OSError as exc:
        logger.error("Could not delete wiki cache: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to delete wiki cache") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Wiki cache not found")
    if repo_type in {"github", "gitlab", "bitbucket"} and not repo_has_any_cache(
        repo_type, owner, repo
    ):
        try:
            delete_local_repo_clone(repo_type, owner, repo)
        except OSError as exc:
            logger.warning("Could not remove unused repository clone: %s", exc)
    return {"message": f"Wiki cache for {owner}/{repo} ({language}) deleted successfully"}


@router.post("/api/wiki_cache/prune")
async def prune_wiki_cache_endpoint(
    max_age_days: Optional[int] = Query(None, ge=0),
    max_bytes: Optional[int] = Query(None, ge=0),
    max_files: Optional[int] = Query(None, ge=0),
):
    from api.cache_eviction import prune_wiki_cache

    try:
        return prune_wiki_cache(
            max_age_days=max_age_days, max_bytes=max_bytes, max_files=max_files
        )
    except Exception as exc:
        logger.exception("Wiki cache prune failed")
        raise HTTPException(status_code=500, detail="Wiki cache prune failed") from exc


@router.get("/api/mindmap/{owner}/{repo}")
async def mindmap_endpoint(
    owner: str,
    repo: str,
    repo_type: str = Query("github"),
    language: str = Query("en"),
):
    cached = await read_wiki_cache(owner, repo, repo_type, language)
    if not cached:
        raise HTTPException(status_code=404, detail="No wiki generated")
    structure = cached.wiki_structure
    sections = {section.id: section for section in structure.sections or []}
    pages = {page.id: page for page in structure.pages or []}

    def page_node(page_id: str) -> dict:
        page = pages.get(page_id)
        return (
            {"id": page.id, "title": page.title, "related": page.relatedPages or []}
            if page
            else {"id": page_id, "title": page_id}
        )

    def section_node(section_id: str) -> dict:
        section = sections.get(section_id)
        if not section:
            return {"id": section_id, "title": section_id, "children": []}
        children = [section_node(child) for child in section.subsections or []]
        children.extend(page_node(page_id) for page_id in section.pages or [])
        return {"id": section.id, "title": section.title, "children": children}

    tree = (
        [section_node(section_id) for section_id in structure.rootSections]
        if structure.rootSections
        else [page_node(page.id) for page in structure.pages or []]
    )
    return {
        "title": structure.title or f"{owner}/{repo}",
        "description": structure.description or "",
        "tree": tree,
    }
