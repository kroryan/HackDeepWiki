"""Versioned wiki persistence and its indexing/memory side effects."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from typing import Optional

from api.data_root import get_data_root
from api.models import WikiCacheData, WikiCacheRequest
from api.storage import repo_key
from api.storage.wiki_search import index_wiki_cache
from api.wiki_cache_paths import (
    LEGACY_WIKI_CACHE_FILE_PREFIX,
    WIKI_CACHE_DIR,
    WIKI_CACHE_FILE_PREFIX,
    list_cache_files,
)

logger = logging.getLogger(__name__)


def parse_cache_version(filename: str) -> int:
    match = re.search(r"_v(\d+)\.json$", filename)
    return int(match.group(1)) if match else 0


def repo_has_any_cache(repo_type: str, owner: str, repo: str) -> bool:
    prefixes = (
        f"{WIKI_CACHE_FILE_PREFIX}{repo_type}_{owner}_{repo}_",
        f"{LEGACY_WIKI_CACHE_FILE_PREFIX}{repo_type}_{owner}_{repo}_",
    )
    try:
        return any(
            filename.startswith(prefixes) and filename.endswith(".json")
            for filename in os.listdir(WIKI_CACHE_DIR)
        )
    except OSError:
        return True


def delete_local_repo_clone(repo_type: str, owner: str, repo: str) -> None:
    root_path = get_data_root()
    legacy_name = f"{owner}_{repo}"
    names = {legacy_name, repo_key(owner, repo, repo_type)}
    for name in names:
        clone_dir = os.path.join(root_path, "repos", name)
        database_file = os.path.join(root_path, "databases", f"{name}.pkl")
        if os.path.isdir(clone_dir):
            shutil.rmtree(clone_dir, ignore_errors=True)
        if os.path.isfile(database_file):
            os.remove(database_file)


def get_wiki_cache_path(
    owner: str,
    repo: str,
    repo_type: str,
    language: str,
    comprehensive: Optional[bool] = None,
    page_count: Optional[int] = None,
    version: Optional[int] = None,
) -> str:
    variant = ""
    if comprehensive is not None and page_count is not None:
        variant = f"_{'comprehensive' if comprehensive else 'concise'}_{page_count}"
    version_suffix = f"_v{version}" if version is not None else ""
    return os.path.join(
        WIKI_CACHE_DIR,
        f"{WIKI_CACHE_FILE_PREFIX}{repo_type}_{owner}_{repo}_{language}"
        f"{variant}{version_suffix}.json",
    )


def list_repo_cache_files(
    repo_type: str, owner: str, repo: str, language: str
) -> list[str]:
    try:
        return list_cache_files(repo_type, owner, repo, language)
    except OSError as exc:
        logger.error("Error listing wiki cache files: %s", exc)
        return []


def next_cache_version(repo_type: str, owner: str, repo: str, language: str) -> int:
    return (
        max(
            (
                parse_cache_version(os.path.basename(path))
                for path in list_repo_cache_files(repo_type, owner, repo, language)
            ),
            default=0,
        )
        + 1
    )


def _load(path: str) -> Optional[WikiCacheData]:
    try:
        with open(path, encoding="utf-8") as cache_file:
            return WikiCacheData(**json.load(cache_file))
    except (OSError, ValueError, TypeError) as exc:
        logger.error("Error reading wiki cache %s: %s", path, exc)
        return None


async def read_wiki_cache(
    owner: str,
    repo: str,
    repo_type: str,
    language: str,
    comprehensive: Optional[bool] = None,
    page_count: Optional[int] = None,
    version: Optional[int] = None,
) -> Optional[WikiCacheData]:
    files = list_repo_cache_files(repo_type, owner, repo, language)
    if version is not None:
        files = [
            path
            for path in files
            if parse_cache_version(os.path.basename(path)) == version
        ]
    files.sort(
        key=lambda path: (
            parse_cache_version(os.path.basename(path)),
            os.path.getmtime(path),
        ),
        reverse=True,
    )
    fallback: Optional[WikiCacheData] = None
    for path in files:
        cached = _load(path)
        if cached is None:
            continue
        if fallback is None:
            fallback = cached
        page_count_matches = (
            page_count is None or len(cached.wiki_structure.pages) == page_count
        )
        mode_matches = (
            comprehensive is None
            or cached.comprehensive is None
            or cached.comprehensive == comprehensive
        )
        if page_count_matches and mode_matches:
            return cached
    return fallback


def _atomic_write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    file_descriptor, temp_path = tempfile.mkstemp(
        prefix=".wiki-cache-", suffix=".tmp", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as cache_file:
            json.dump(payload, cache_file, indent=2)
            cache_file.flush()
            os.fsync(cache_file.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def _repo_commit(data: WikiCacheRequest) -> tuple[Optional[str], Optional[str]]:
    try:
        from api.code_agent.manager import repo_head_commit, repo_key_for

        if data.repo.type == "local" and data.repo.localPath:
            return repo_head_commit(data.repo.localPath), data.repo.localPath
        if (
            data.repo.type in {"github", "gitlab", "bitbucket"}
            and data.repo.repoUrl
        ):
            _, clone_dir = repo_key_for(data.repo.repoUrl, data.repo.type)
            return repo_head_commit(clone_dir), clone_dir
    except Exception as exc:
        logger.debug("Could not resolve repository commit: %s", exc)
    return None, None


def _enqueue_memory(
    data: WikiCacheRequest,
    payload: WikiCacheData,
    clone_dir: Optional[str],
    previous_version: Optional[int],
    previous_commit: Optional[str],
) -> None:
    try:
        from api.jobs.queue import enqueue

        key = repo_key(data.repo.owner, data.repo.repo, data.repo.type)
        enqueue(
            "engraphis.release",
            key,
            {
                "owner": data.repo.owner,
                "repo": data.repo.repo,
                "repo_type": data.repo.type,
                "language": data.language,
                "version": payload.version,
                "repo_commit": payload.repo_commit,
                "previous_version": previous_version,
                "previous_commit": previous_commit,
                "clone_dir": clone_dir,
            },
        )
        enqueue(
            "engraphis.content",
            key,
            {
                "owner": data.repo.owner,
                "repo": data.repo.repo,
                "version": payload.version,
                "wiki_structure": payload.wiki_structure.model_dump(),
                "generated_pages": {
                    page_id: page.model_dump()
                    for page_id, page in payload.generated_pages.items()
                },
            },
        )
    except Exception as exc:
        logger.warning("Could not enqueue Engraphis ingestion: %s", exc)


async def save_wiki_cache(data: WikiCacheRequest) -> Optional[int]:
    files = list_repo_cache_files(
        data.repo.type, data.repo.owner, data.repo.repo, data.language
    )
    files.sort(
        key=lambda path: (
            parse_cache_version(os.path.basename(path)),
            os.path.getmtime(path),
        ),
        reverse=True,
    )
    if files:
        newest = _load(files[0])
        if newest and (
            newest.wiki_structure == data.wiki_structure
            and newest.generated_pages == data.generated_pages
            and newest.provider == data.provider
            and newest.model == data.model
        ):
            return parse_cache_version(os.path.basename(files[0]))

    next_version = next_cache_version(
        data.repo.type, data.repo.owner, data.repo.repo, data.language
    )
    repo_commit, clone_dir = _repo_commit(data)
    previous_version: Optional[int] = None
    previous_commit: Optional[str] = None
    if files:
        previous_version = parse_cache_version(os.path.basename(files[0]))
        previous = _load(files[0])
        previous_commit = previous.repo_commit if previous else None
    payload = WikiCacheData(
        wiki_structure=data.wiki_structure,
        generated_pages=data.generated_pages,
        repo=data.repo,
        provider=data.provider,
        model=data.model,
        comprehensive=data.comprehensive,
        page_count=data.page_count,
        version=next_version,
        repo_commit=repo_commit,
    )
    cache_path = get_wiki_cache_path(
        data.repo.owner,
        data.repo.repo,
        data.repo.type,
        data.language,
        data.comprehensive,
        data.page_count,
        next_version,
    )
    try:
        _atomic_write_json(cache_path, payload.model_dump())
    except OSError as exc:
        logger.error("Could not save wiki cache %s: %s", cache_path, exc)
        return None

    try:
        pages = [page.model_dump() for page in payload.wiki_structure.pages]
        pages.extend(page.model_dump() for page in payload.generated_pages.values())
        if pages:
            index_wiki_cache(
                data.repo.owner,
                data.repo.repo,
                data.repo.type,
                data.language,
                pages,
                version=f"v{next_version}",
            )
    except Exception as exc:
        logger.warning("Wiki FTS index update failed: %s", exc)
    try:
        from api.cache_eviction import prune_wiki_cache

        prune_wiki_cache()
    except Exception as exc:
        logger.warning("Wiki cache prune skipped: %s", exc)
    _enqueue_memory(
        data, payload, clone_dir, previous_version, previous_commit
    )
    return next_version
