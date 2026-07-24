"""System prompt for code-editing sessions + wiki<->clone version anchoring.

Two jobs:

1. Give the agent the SAME knowledge the wiki chat has: repo identity, the
   page tree of the wiki release the user actually has OPEN (not just the
   latest one), the security findings when that toggle is also on, and an
   instruction to consult the wiki through the ``hackdeepwiki`` MCP tools.

2. Guard against editing stale code. The wiki describes the repo *as of the
   commit it was generated from*; the on-disk clone is what the agent edits.
   Each wiki release records the clone's HEAD at save time (``repo_commit``,
   added to WikiCacheData); at session start we compare it against the
   clone's current HEAD and, on mismatch, both warn the user (surfaced by
   routes.py) and tell the agent explicitly so it re-verifies against the
   working tree instead of trusting wiki excerpts blindly.
"""

import logging
from typing import Optional, Tuple

from api.code_agent.manager import repo_head_commit

logger = logging.getLogger(__name__)

_MAX_PAGES_LISTED = 60


async def _load_wiki_cache(owner: str, repo: str, repo_type: str, language: str,
                           wiki_version: Optional[int]):
    """Load the wiki release the user has open (lazy import to avoid a module
    cycle with api.api, same pattern websocket_wiki.py uses)."""
    try:
        from api.api import read_wiki_cache
        return await read_wiki_cache(owner, repo, repo_type, language, version=wiki_version)
    except Exception as e:  # noqa: BLE001 - the wiki being unreadable must not block coding
        logger.warning("Could not load wiki cache for code session: %s", e)
        return None


def _security_context_text(owner: str, repo: str, repo_type: str, language: str) -> str:
    try:
        from api.api import read_vuln_cache
        from api.vuln_common.chat_context import build_security_context_text
        vuln_report = read_vuln_cache(repo_type, owner, repo, language)
        return build_security_context_text(vuln_report, None) or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to load security context for code session: %s", exc)
        return ""


async def build_code_session_context(
    owner: str,
    repo: str,
    repo_type: str,
    language: str,
    repo_dir: str,
    wiki_version: Optional[int] = None,
    include_security_context: bool = False,
) -> Tuple[str, Optional[str]]:
    """Return ``(system_prompt, version_warning)``.

    ``version_warning`` is a human-readable string when the open wiki release
    was generated from a different commit than the clone on disk (or when the
    correspondence can't be verified); None when everything matches.
    """
    cached = await _load_wiki_cache(owner, repo, repo_type, language, wiki_version)
    head = repo_head_commit(repo_dir)

    version_warning: Optional[str] = None
    wiki_commit = getattr(cached, "repo_commit", None) if cached else None
    if cached is None:
        version_warning = "No wiki release found for this repository/language; the agent works from the code alone."
    elif wiki_commit and head and wiki_commit != head:
        version_warning = (
            f"The open wiki (v{cached.version or 0}) was generated from commit "
            f"{wiki_commit[:12]}, but the local clone is at {head[:12]}. "
            "The wiki may describe a different version of the code."
        )
    elif not wiki_commit:
        version_warning = (
            "This wiki release predates commit tracking, so wiki/code version "
            "consistency could not be verified."
        )

    parts: list[str] = []
    parts.append(
        "You are the code-editing agent embedded in HackDeepWiki, working on the "
        f"repository {owner}/{repo} (type: {repo_type}). Your working directory IS the "
        "repository checkout -- edit files, run commands, and build/test directly in it. "
        "You run in full-auto mode: never ask for permission or confirmation; act, then "
        "report clearly what you changed and why. Reply to the user in the language they "
        "write in."
    )
    if repo_type == "local":
        parts.append(
            "IMPORTANT: this is the user's own local directory, edited IN PLACE (not a "
            "disposable clone). Be deliberate with destructive operations."
        )
    if head:
        parts.append(f"Current checkout HEAD: {head}.")

    if cached is not None:
        titles = []
        try:
            for page in cached.wiki_structure.pages[:_MAX_PAGES_LISTED]:
                titles.append(f"- {page.id}: {page.title}")
        except Exception:  # noqa: BLE001 - malformed cache must not block coding
            titles = []
        wiki_block = "\n".join(titles)
        parts.append(
            f"A generated wiki (release v{cached.version or 0}, language '{language}') "
            "documents this repository. Its pages:\n"
            f"{wiki_block}\n"
            "Before exploring the code blindly, consult the wiki through your "
            "`hackdeepwiki` MCP tools: `search_wiki` (semantic search over the wiki), "
            "`read_doc` (full page by id), `list_wiki_structure`, `read_file`, and "
            "`ask_repo` (RAG question over the codebase)."
        )

    if version_warning:
        parts.append(
            f"VERSION WARNING: {version_warning} Treat wiki content as potentially "
            "outdated: verify any wiki claim against the actual working tree (the files "
            "on disk are the source of truth) before editing based on it."
        )

    if include_security_context:
        security_text = _security_context_text(owner, repo, repo_type, language)
        if security_text:
            parts.append(
                "<security_analysis>\n" + security_text + "\n</security_analysis>\n"
                "The findings above come from HackDeepWiki's saved security scan of this "
                "repository; use them when the user asks you to fix vulnerabilities."
            )

    return "\n\n".join(parts), version_warning
