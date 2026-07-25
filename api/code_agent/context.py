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


def _cache_file_version(path: str) -> int:
    """``_vN`` from a cache filename (same rule as api.api._parse_cache_version;
    re-implemented locally because that helper is private to the huge api
    module). Legacy files without a suffix count as v0."""
    import re
    m = re.search(r"_v(\d+)\.json$", __import__("os").path.basename(path))
    return int(m.group(1)) if m else 0


def _maybe_backfill_wiki_commit(cached, owner: str, repo: str, repo_type: str,
                                language: str, repo_dir: str) -> Optional[str]:
    """Anchor a pre-commit-tracking wiki release to a commit when that is
    SOUND, and persist the anchor into the cache file so the warning never
    reappears for it.

    Why this is sound for git-host repos: the local clone is made at wiki
    generation and never pulled afterwards (api/data_pipeline.py -- reuse is
    the default); the only thing that replaces it is "Refresh Wiki", which
    re-clones AND mints a NEW wiki release. So for the LATEST release, the
    clone's HEAD is the very commit the wiki was generated from. Older
    releases genuinely can't be recovered (the clone has moved on), and
    ``type='local'`` dirs are live working copies the user may have edited
    since -- both keep their warning.
    """
    existing = getattr(cached, "repo_commit", None)
    if existing:
        return existing
    if repo_type not in ("github", "gitlab", "bitbucket"):
        return None
    try:
        import json
        import os
        from api.wiki_cache_paths import list_cache_files

        files = list_cache_files(repo_type, owner, repo, language)
        if not files:
            return None
        max_version = max(_cache_file_version(p) for p in files)
        this_version = cached.version or 0
        if this_version != max_version:
            return None  # older release: unknowable, keep the warning

        head = repo_head_commit(repo_dir)
        if not head:
            return None
        for path in files:
            if _cache_file_version(path) != this_version:
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not data.get("repo_commit"):
                    data["repo_commit"] = head
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)
            except OSError as e:
                logger.warning("Could not backfill repo_commit into %s: %s", path, e)
        cached.repo_commit = head
        logger.info("Backfilled wiki release v%s of %s/%s with commit %s "
                    "(clone HEAD; clones are immutable post-generation)",
                    this_version, owner, repo, head[:12])
        return head
    except Exception as e:  # noqa: BLE001 - anchoring must never block coding
        logger.warning("Wiki commit backfill failed: %s", e)
        return None


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
    wiki_commit = (
        _maybe_backfill_wiki_commit(cached, owner, repo, repo_type, language, repo_dir)
        if cached is not None else None
    )
    if cached is None:
        version_warning = "No wiki release found for this repository/language; the agent works from the code alone."
    elif wiki_commit and head and wiki_commit != head:
        version_warning = (
            f"The open wiki (v{cached.version or 0}) was generated from commit "
            f"{wiki_commit[:12]}, but the local clone is at {head[:12]}. "
            "The wiki may describe a different version of the code."
        )
    elif not wiki_commit:
        # Backfill couldn't anchor it: either an OLDER release (the clone has
        # moved on to a newer one) or a live 'local' directory. Tell the user
        # how to get a verified anchor instead of a vague "predates tracking".
        if repo_type == "local":
            version_warning = (
                "This wiki has no commit anchor and the local directory is a live "
                "working copy -- regenerate/update the wiki to anchor it to the "
                "current code."
            )
        else:
            version_warning = (
                f"Wiki release v{cached.version or 0} is not the latest, so it can't "
                "be anchored to a commit. Open the latest release (or update the "
                "wiki) for verified wiki/code consistency."
            )

    parts: list[str] = []
    parts.append(
        "You are the code-editing agent embedded in HackDeepWiki, working on the "
        f"repository {owner}/{repo} (type: {repo_type}). Your working directory IS the "
        "repository checkout: you can read files, run commands, build/test, and edit. "
        "Tool permissions are auto-approved -- no confirmation prompt will ever appear, "
        "so never ask for permission. But full-auto is NOT a license to change things "
        "unprompted. Infer the mode from what the user asked:\n"
        "- PLAN/ANSWER mode (default): questions, explanations, analysis, reviews, "
        "recommendations, or requests for a plan ('how could I improve X', 'what would "
        "you change', 'make a detailed plan'). Investigate READ-ONLY (read files, run "
        "harmless inspection commands) and deliver the answer or plan. Do NOT modify "
        "files, do NOT run state-changing commands, do NOT commit.\n"
        "- BUILD mode: only when the user explicitly asks you to implement, fix, "
        "refactor, apply, or change something ('do it', 'fix it', 'implement option 2'). "
        "Then act end-to-end without asking for confirmation, verify (build/tests where "
        "sensible), and report clearly what you changed and why.\n"
        "When in doubt, stay in PLAN/ANSWER mode and end by offering to implement. "
        "Reply to the user in the language they write in."
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

    # Engraphis memory: per-wiki-release workspace shared with the repository
    # chat. Instruct the agent on the exact scope arguments AND seed the top
    # proactive memories so past decisions are visible before the first tool
    # call. Best-effort -- memory being unavailable must never block coding.
    try:
        from api import engraphis_integration
        if engraphis_integration.is_available():
            effective_version = wiki_version
            if effective_version is None and cached is not None:
                effective_version = cached.version or 0
            workspace = engraphis_integration.workspace_for_version(
                owner, repo, effective_version
            )
            memory_part = (
                "PERSISTENT MEMORY: this wiki release has a durable memory store "
                "shared with the repository chat, reachable through your "
                "`hackdeepwiki` MCP tools `memory_remember`, `memory_recall`, "
                "`memory_why` and `memory_timeline`. Always call them with "
                f"owner='{owner}', repo='{repo}', wiki_version="
                f"{int(effective_version) if effective_version is not None else 0}. "
                "Use `memory_recall` before making architectural decisions (earlier "
                "sessions may have already decided), and `memory_remember` when the "
                "user states a decision/preference or when you complete a change "
                "worth knowing about later (one self-contained fact per call)."
            )
            seeded = engraphis_integration.proactive_block(
                workspace, f"{owner}/{repo} decisions preferences recent changes"
            )
            if seeded:
                memory_part += "\n\n" + seeded
            parts.append(memory_part)
    except Exception as e:  # noqa: BLE001
        logger.warning("Engraphis memory context skipped: %s", e)

    return "\n\n".join(parts), version_warning
