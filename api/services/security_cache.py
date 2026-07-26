"""Versioned persistence for dependency and website security reports."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

from api.wiki_cache_paths import WIKI_CACHE_DIR

logger = logging.getLogger(__name__)
VULN_CACHE_PREFIX = "hackdeepwiki_vulns"
LEGACY_VULN_CACHE_PREFIX = "freedeepwiki_vulns"
WEB_VULN_CACHE_PREFIX = "hackdeepwiki_webvulns"
LEGACY_WEB_VULN_CACHE_PREFIX = "freedeepwiki_webvulns"


def parse_cache_version(filename: str) -> int:
    match = re.search(r"_v(\d+)\.json$", filename)
    return int(match.group(1)) if match else 0


def list_cache_files_for_prefix(prefix: str) -> list[str]:
    try:
        return [
            os.path.join(WIKI_CACHE_DIR, filename)
            for filename in os.listdir(WIKI_CACHE_DIR)
            if filename.startswith(prefix) and filename.endswith(".json")
        ]
    except OSError:
        return []


def _next_version(prefix: str) -> int:
    return (
        max(
            (
                parse_cache_version(os.path.basename(path))
                for path in list_cache_files_for_prefix(prefix)
            ),
            default=0,
        )
        + 1
    )


def _latest_path(prefix: str) -> Optional[str]:
    files = list_cache_files_for_prefix(prefix)
    if not files:
        return None
    return max(files, key=lambda path: parse_cache_version(os.path.basename(path)))


def vuln_cache_prefix(
    repo_type: str,
    owner: str,
    repo: str,
    language: str,
    prefix: str = VULN_CACHE_PREFIX,
) -> str:
    return f"{prefix}_{repo_type}_{owner}_{repo}_{language}"


def vuln_cache_path(
    repo_type: str,
    owner: str,
    repo: str,
    language: str,
    version: Optional[int] = None,
    prefix: str = VULN_CACHE_PREFIX,
) -> str:
    suffix = f"_v{version}" if version is not None else ""
    return os.path.join(
        WIKI_CACHE_DIR,
        f"{vuln_cache_prefix(repo_type, owner, repo, language, prefix)}{suffix}.json",
    )


def save_vuln_cache(report: dict) -> tuple[str, int]:
    repo_type = report.get("repo_type", "")
    owner = report.get("owner", "")
    repo = report.get("repo", "")
    language = report.get("language", "en")
    prefix = vuln_cache_prefix(repo_type, owner, repo, language)
    version = _next_version(prefix)
    path = vuln_cache_path(repo_type, owner, repo, language, version)
    with open(path, "w", encoding="utf-8") as report_file:
        json.dump(report, report_file, ensure_ascii=False, indent=2)
    return path, version


def read_vuln_cache(
    repo_type: str,
    owner: str,
    repo: str,
    language: str,
    version: Optional[int] = None,
) -> Optional[dict]:
    if version is not None:
        path = vuln_cache_path(repo_type, owner, repo, language, version)
        if not os.path.isfile(path):
            return None
    else:
        path = _latest_path(vuln_cache_prefix(repo_type, owner, repo, language))
        if path is None:
            path = _latest_path(
                vuln_cache_prefix(
                    repo_type, owner, repo, language, LEGACY_VULN_CACHE_PREFIX
                )
            )
        if path is None:
            return None
    try:
        with open(path, encoding="utf-8") as report_file:
            data = json.load(report_file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read vulnerability cache %s: %s", path, exc)
        return None
    from api.vuln_scanner.models import VulnReport

    return VulnReport.from_dict(data).to_dict()


def _release_metadata(paths: list[str]) -> list[dict]:
    releases: list[dict] = []
    for path in paths:
        filename = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as report_file:
                data = json.load(report_file)
            releases.append(
                {
                    "version": parse_cache_version(filename),
                    "created_at": int(os.path.getmtime(path) * 1000),
                    "total_findings": data.get("total_findings"),
                    "generated_at": data.get("generated_at"),
                    "id": filename,
                }
            )
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read security report metadata %s: %s", filename, exc)
    releases.sort(key=lambda release: (release["version"], release["created_at"]), reverse=True)
    return releases


def list_vuln_cache_releases(
    repo_type: str, owner: str, repo: str, language: str
) -> list[dict]:
    prefixes = [
        vuln_cache_prefix(repo_type, owner, repo, language),
        vuln_cache_prefix(
            repo_type, owner, repo, language, LEGACY_VULN_CACHE_PREFIX
        ),
    ]
    return _release_metadata(
        [path for prefix in prefixes for path in list_cache_files_for_prefix(prefix)]
    )


def web_vuln_cache_prefix(
    owner: str,
    repo: str,
    language: str,
    prefix: str = WEB_VULN_CACHE_PREFIX,
) -> str:
    return f"{prefix}_{owner}_{repo}_{language}"


def web_vuln_cache_path(
    owner: str,
    repo: str,
    language: str,
    version: Optional[int] = None,
    prefix: str = WEB_VULN_CACHE_PREFIX,
) -> str:
    suffix = f"_v{version}" if version is not None else ""
    return os.path.join(
        WIKI_CACHE_DIR,
        f"{web_vuln_cache_prefix(owner, repo, language, prefix)}{suffix}.json",
    )


def save_web_vuln_cache(report: dict) -> tuple[str, int]:
    owner = report.get("owner", "")
    repo = report.get("repo", "")
    language = report.get("language", "en")
    prefix = web_vuln_cache_prefix(owner, repo, language)
    version = _next_version(prefix)
    path = web_vuln_cache_path(owner, repo, language, version)
    with open(path, "w", encoding="utf-8") as report_file:
        json.dump(report, report_file, ensure_ascii=False, indent=2)
    return path, version


def read_web_vuln_cache(
    owner: str,
    repo: str,
    language: str,
    version: Optional[int] = None,
) -> Optional[dict]:
    if version is not None:
        path = web_vuln_cache_path(owner, repo, language, version)
        if not os.path.isfile(path):
            return None
    else:
        path = _latest_path(web_vuln_cache_prefix(owner, repo, language))
        if path is None:
            path = _latest_path(
                web_vuln_cache_prefix(
                    owner, repo, language, LEGACY_WEB_VULN_CACHE_PREFIX
                )
            )
        if path is None:
            return None
    try:
        with open(path, encoding="utf-8") as report_file:
            data = json.load(report_file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read website security cache %s: %s", path, exc)
        return None
    from api.web_vuln_scanner.models import WebVulnReport

    return WebVulnReport.from_dict(data).to_dict()


def list_web_vuln_cache_releases(
    owner: str, repo: str, language: str
) -> list[dict]:
    prefixes = [
        web_vuln_cache_prefix(owner, repo, language),
        web_vuln_cache_prefix(owner, repo, language, LEGACY_WEB_VULN_CACHE_PREFIX),
    ]
    return _release_metadata(
        [path for prefix in prefixes for path in list_cache_files_for_prefix(prefix)]
    )


def split_newline_filters(value: object) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [line.strip() for line in str(value).splitlines() if line.strip()]
