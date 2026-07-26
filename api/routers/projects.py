"""Unified list of generated wikis and imported offline projects."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from fastapi import APIRouter, HTTPException

from api import fanwiki_library, zim_library
from api.models import ProcessedProjectEntry
from api.wiki_cache_paths import (
    LEGACY_WIKI_CACHE_FILE_PREFIX,
    WIKI_CACHE_DIR,
    WIKI_CACHE_FILE_PREFIX,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["projects"])


def _fallback_identity(filename: str, prefix: str) -> tuple[str, str, str, str] | None:
    cache_name = filename.removeprefix(prefix).removesuffix(".json")
    cache_name = re.sub(r"_v\d+$", "", cache_name)
    cache_name = re.sub(r"_(?:comprehensive|concise)_\d+$", "", cache_name)
    parts = cache_name.split("_")
    if len(parts) < 4:
        return None
    return parts[0], parts[1], "_".join(parts[2:-1]), parts[-1]


def _generated_projects() -> list[ProcessedProjectEntry]:
    newest: dict[tuple[str, str, str, str], ProcessedProjectEntry] = {}
    if not os.path.isdir(WIKI_CACHE_DIR):
        return []
    for filename in os.listdir(WIKI_CACHE_DIR):
        prefix = next(
            (
                candidate
                for candidate in (
                    WIKI_CACHE_FILE_PREFIX,
                    LEGACY_WIKI_CACHE_FILE_PREFIX,
                )
                if filename.startswith(candidate)
            ),
            None,
        )
        if prefix is None or not filename.endswith(".json"):
            continue
        fallback = _fallback_identity(filename, prefix)
        if fallback is None:
            continue
        path = os.path.join(WIKI_CACHE_DIR, filename)
        try:
            with open(path, encoding="utf-8") as cache_file:
                payload = json.load(cache_file)
            repo_payload = payload.get("repo") or {}
            repo_type = str(repo_payload.get("type") or fallback[0])
            owner = str(repo_payload.get("owner") or fallback[1])
            repo = str(repo_payload.get("repo") or fallback[2])
            language = fallback[3]
            entry = ProcessedProjectEntry(
                id=filename,
                owner=owner,
                repo=repo,
                name=(payload.get("wiki_structure") or {}).get("title")
                or f"{owner}/{repo}",
                repo_type=repo_type,
                submittedAt=int(os.path.getmtime(path) * 1000),
                language=language,
                status="generated",
                start_url=str(repo_payload.get("repoUrl") or "") or None,
            )
            key = (repo_type, owner, repo, language)
            previous = newest.get(key)
            if previous is None or entry.submittedAt > previous.submittedAt:
                newest[key] = entry
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("Could not read project cache %s: %s", filename, exc)
    return list(newest.values())


@router.get("/api/processed_projects", response_model=list[ProcessedProjectEntry])
async def get_processed_projects():
    try:
        projects = await asyncio.to_thread(_generated_projects)
        imported_fanwikis = await asyncio.to_thread(fanwiki_library.list_all)
        imports_by_route = {
            (entry["owner"], entry["repo"]): entry for entry in imported_fanwikis
        }
        for project in projects:
            if project.repo_type == "fanwiki" and not project.start_url:
                imported = imports_by_route.get((project.owner, project.repo))
                if imported:
                    project.start_url = imported["start_url"]

        generated_urls = {
            project.start_url
            for project in projects
            if project.repo_type == "fanwiki" and project.start_url
        }
        generated_routes = {
            (project.owner, project.repo)
            for project in projects
            if project.repo_type == "fanwiki"
        }
        projects.extend(
            ProcessedProjectEntry(**entry)
            for entry in imported_fanwikis
            if entry["start_url"] not in generated_urls
            and (entry["owner"], entry["repo"]) not in generated_routes
        )
        projects.extend(
            ProcessedProjectEntry(
                id=entry["id"],
                owner="zim",
                repo=entry["id"],
                name=entry["title"],
                repo_type="zim",
                submittedAt=entry["importedAt"],
                language="",
            )
            for entry in zim_library.list_all()
        )
        projects.sort(key=lambda project: project.submittedAt, reverse=True)
        return projects
    except Exception as exc:
        logger.exception("Failed to list processed projects")
        raise HTTPException(
            status_code=500, detail="Failed to list processed projects"
        ) from exc
