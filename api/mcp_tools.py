"""MCP tool implementations for HackDeepWiki (Fase 1).

Each tool here is a plain Python callable with a JSON-Schema input shape, so
``api.mcp_server`` can expose them over MCP's JSON-RPC (initialize /
tools/list / tools/call) WITHOUT depending on the ``mcp`` pip package --
that package pulls in pydantic, httpx, and an SSE stack whose packaging
(PyInstaller) risk is exactly what the portability principle forbids. The
MCP wire protocol is JSON-RPC 2.0; we speak the subset we need from stdlib.

Tools mirror OpenDeepWiki's McpGlobalTools/McpRepositoryTools surface but
backed by HackDeepWiki's existing wiki-cache + file-tree + RAG:

  search_wiki        -- full-text search over a repo's generated wiki pages
  read_doc           -- read one generated wiki page by path
  list_wiki_structure -- the wiki's page tree (so an agent can navigate)
  read_file          -- read a source file from the repo (READ_FILE parity)
  ask_repo           -- ask a question against the repo's RAG index

All tools are read-only (the MCP server exposes a wiki for consumption, not
mutation), so a leaked runtime token can't corrupt generated content.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# A tool is: name -> (description, input_schema, handler(args)->str)
TOOL_REGISTRY: dict[str, dict[str, Any]] = {}


def _tool(name: str, description: str, schema: dict[str, Any]):
    """Decorator registering a handler under ``name`` with its input schema."""

    def deco(fn: Callable[..., str]):
        TOOL_REGISTRY[name] = {
            "description": description,
            "inputSchema": schema,
            "handler": fn,
        }
        return fn

    return deco


# Cache path resolution lives in api.wiki_cache_paths (single source of truth,
# shared with api.api + wiki_search). This module used to re-declare the
# prefix strings and the dir, which is how it once looked in the wrong
# (nested) layout and found no wikis -- now it imports the real resolver.
from api.wiki_cache_paths import list_cache_files, load_latest_cache_json


def _list_cache_files(owner: str, repo: str, repo_type: str, language: str) -> list[str]:
    """All cache files for one repo/language/type, newest-first. Thin wrapper
    over the shared resolver (kept under this name since the tools below call
    it and Fase 2's wiki_search test surface used it too)."""
    return list_cache_files(repo_type, owner, repo, language)


def _extract_pages(cache: dict) -> list[dict]:
    """Pull the page list out of a WikiCacheData-shaped dict. The cache stores
    pages two ways: ``wiki_structure.pages`` (list of {id,title,content,...})
    and ``generated_pages`` (dict id->page). We union both so a search/read
    hits every page regardless of which the generator populated. Each entry is
    normalized to a plain dict with id/title/content."""
    out: dict[str, dict] = {}
    ws = cache.get("wiki_structure") or {}
    if isinstance(ws, dict):
        for p in ws.get("pages", []) or []:
            if isinstance(p, dict):
                pid = str(p.get("id") or p.get("title") or "")
                if pid:
                    out[pid] = p
    gp = cache.get("generated_pages") or {}
    if isinstance(gp, dict):
        for pid, p in gp.items():
            if isinstance(p, dict):
                out[str(pid)] = p
    return list(out.values())


def _load_latest_cache(owner: str, repo: str, repo_type: str, language: str) -> Optional[dict]:
    # Delegate to the shared resolver (api.wiki_cache_paths.load_latest_cache_json)
    # so the cache-read path isn't duplicated here -- it already does newest-first
    # selection across current + legacy prefixes.
    return load_latest_cache_json(owner, repo, repo_type, language)


@_tool(
    "search_wiki",
    "Full-text search over a repository's generated wiki pages. Returns matching page titles with a short snippet each.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string", "description": "Repository owner"},
            "repo": {"type": "string", "description": "Repository name"},
            "query": {"type": "string", "description": "Search query"},
            "language": {"type": "string", "description": "Wiki language code (default: en)", "default": "en"},
            "repo_type": {"type": "string", "description": "github/gitlab/bitbucket/local/website (default: github)", "default": "github"},
            "max_results": {"type": "integer", "description": "Max pages to return (default 5)", "default": 5},
        },
        "required": ["owner", "repo", "query"],
    },
)
def search_wiki(owner: str, repo: str, query: str, language: str = "en",
                repo_type: str = "github", max_results: int = 5) -> str:
    cache = _load_latest_cache(owner, repo, repo_type, language)
    if not cache:
        return f"No generated wiki found for {owner}/{repo} ({language}). Generate one first."
    pages = _extract_pages(cache)
    q = (query or "").lower()
    terms = [t for t in q.split() if t]
    scored = []
    for page in pages:
        title = str(page.get("title") or page.get("id") or "")
        content = str(page.get("content") or "")
        text = (title + "\n" + content).lower()
        score = sum(1 for t in terms if t in text)
        if score > 0:
            snippet = content[:200].replace("\n", " ").strip()
            scored.append((score, title, snippet, page.get("id") or title))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return f"No wiki pages matched '{query}' in {owner}/{repo}."
    lines = [f"Found {len(scored)} matching page(s) in {owner}/{repo}:"]
    for _score, title, snippet, path in scored[: max(1, max_results)]:
        lines.append(f"\n## {title} ({path})\n{snippet}...")
    return "\n".join(lines)


@_tool(
    "read_doc",
    "Read one generated wiki page by its path/title. Returns the full page markdown.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "path": {"type": "string", "description": "Page path or title (as returned by search_wiki/list_wiki_structure)"},
            "language": {"type": "string", "default": "en"},
            "repo_type": {"type": "string", "default": "github"},
        },
        "required": ["owner", "repo", "path"],
    },
)
def read_doc(owner: str, repo: str, path: str, language: str = "en",
             repo_type: str = "github") -> str:
    cache = _load_latest_cache(owner, repo, repo_type, language)
    if not cache:
        return f"No generated wiki found for {owner}/{repo} ({language})."
    pages = _extract_pages(cache)
    target = (path or "").strip().lower()
    for page in pages:
        cand = str(page.get("id") or page.get("title") or "")
        title = str(page.get("title") or "")
        if target and (target == cand.lower() or target == title.lower() or target in cand.lower()):
            content = page.get("content") or ""
            return f"# {title}\n\n{content}"
    return f"Page '{path}' not found in {owner}/{repo} wiki. Use list_wiki_structure to see available pages."


@_tool(
    "list_wiki_structure",
    "List the page tree of a repository's generated wiki, so an agent can navigate before reading.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "language": {"type": "string", "default": "en"},
            "repo_type": {"type": "string", "default": "github"},
        },
        "required": ["owner", "repo"],
    },
)
def list_wiki_structure(owner: str, repo: str, language: str = "en",
                        repo_type: str = "github") -> str:
    cache = _load_latest_cache(owner, repo, repo_type, language)
    if not cache:
        return f"No generated wiki found for {owner}/{repo} ({language})."
    pages = _extract_pages(cache)
    ws = cache.get("wiki_structure") or {}
    sections = ws.get("sections") if isinstance(ws, dict) else None
    lines = [f"Wiki structure for {owner}/{repo} ({language}):"]
    for page in pages:
        title = str(page.get("title") or page.get("id") or "(untitled)")
        pid = str(page.get("id") or title)
        lines.append(f"  - {title}  [{pid}]")
    if sections:
        lines.append(f"\n({len(sections)} section(s) in wiki_structure)")
    return "\n".join(lines) if len(lines) > 1 else f"Wiki for {owner}/{repo} has no pages."


@_tool(
    "read_file",
    "Read a source file from the repository (relative path from repo root). Returns file content. Use when a wiki snippet isn't enough.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "path": {"type": "string", "description": "File path relative to repo root, e.g. api/main.py"},
            "repo_type": {"type": "string", "default": "github"},
        },
        "required": ["owner", "repo", "path"],
    },
)
def read_file(owner: str, repo: str, path: str, repo_type: str = "github") -> str:
    # Resolve the local clone path the same way the app does (repos/<owner>_<repo>).
    try:
        from api.data_pipeline import get_local_file_content
        from api.data_root import get_data_root
        clone_dir = os.path.join(get_data_root(), "repos", f"{owner}_{repo}")
        if not os.path.isdir(clone_dir):
            return f"Repository {owner}/{repo} is not cloned locally. Generate its wiki first."
        return get_local_file_content(clone_dir, path)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"read_file failed for {owner}/{repo}/{path}: {e}")
        return f"Could not read {path} from {owner}/{repo}: {e}"


@_tool(
    "ask_repo",
    "Ask a natural-language question about a repository, answered against its RAG index (semantic search over the embedded code/docs). Use this when search_wiki (keyword) isn't precise enough.",
    {
        "type": "object",
        "properties": {
            "owner": {"type": "string"},
            "repo": {"type": "string"},
            "question": {"type": "string"},
            "repo_type": {"type": "string", "default": "github"},
        },
        "required": ["owner", "repo", "question"],
    },
)
def ask_repo(owner: str, repo: str, question: str, repo_type: str = "github") -> str:
    """Returns the top retrieved context chunks for the question (the MCP
    server answers with evidence, not an LLM-generated answer, so the
    calling agent stays in control of synthesis and there's no recursive
    model call inside a tool)."""
    try:
        from api.data_pipeline import DatabaseManager
        from api.rag import RAG
        # DatabaseManager.prepare_retriever loads/creates the embedding index
        # for owner/repo exactly as the chat path does.
        mgr = DatabaseManager()
        mgr.prepare_retriever(f"{owner}/{repo}", repo_type=repo_type)
        rag = RAG(
            db=mgr.db,
            transformed_docs=mgr.db.state.get("transformed_docs", [])
            if hasattr(mgr.db, "state") else [],
            embedder=mgr.db,
            retriever=None,
        )
        retrieved = rag.call(question)
        if not retrieved or not retrieved[0].documents:
            return f"No relevant context found in {owner}/{repo} for: {question}"
        chunks = retrieved[0].documents[:5]
        lines = [f"Top context chunks for '{question}' in {owner}/{repo}:"]
        for i, d in enumerate(chunks, 1):
            md = getattr(d, "meta_data", {}) or {}
            fp = md.get("file_path", "?")
            text = (getattr(d, "text", "") or "")[:400].replace("\n", " ")
            lines.append(f"\n[{i}] {fp}: {text}")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ask_repo failed for {owner}/{repo}: {e}")
        return f"Could not query {owner}/{repo} RAG index: {e}. Generate the wiki first."


# ---------------------------------------------------------------------------
# Engraphis memory tools (api/engraphis_integration.py). These are the FIRST
# write-capable tools on this MCP server (everything above is read-only wiki
# consumption): they write MEMORIES into the per-wiki-release Engraphis
# workspace, never generated content, and every write is audited/receipted by
# Engraphis itself. The code agent (opencode) reaches them through the
# `hackdeepwiki` MCP entry its generated config already carries; the chat has
# its own in-process handlers (api/search_tool.py) against the SAME shared
# MemoryService, which is what makes memory shared between chat and editor.
# Scope contract: workspace = {owner}_{repo}_v{wiki_version} -- memory is
# hard-isolated per wiki release, never global.
# ---------------------------------------------------------------------------

_MEMORY_SCOPE_PROPS = {
    "owner": {"type": "string", "description": "Repository owner"},
    "repo": {"type": "string", "description": "Repository name"},
    "wiki_version": {
        "type": "integer",
        "description": "Wiki release version the session is anchored to "
                       "(given in your system prompt). Memory is isolated per release.",
    },
}


def _memory_workspace(owner: str, repo: str, wiki_version: Any) -> str:
    from api import engraphis_integration
    try:
        version = int(wiki_version) if wiki_version is not None else None
    except (TypeError, ValueError):
        version = None
    return engraphis_integration.workspace_for_version(owner, repo, version)


@_tool(
    "memory_remember",
    "Store one durable, self-contained memory for this wiki release (a decision, "
    "user preference, or conclusion worth keeping across sessions). Shared with the "
    "repository chat for the same release.",
    {
        "type": "object",
        "properties": {
            **_MEMORY_SCOPE_PROPS,
            "content": {"type": "string", "description": "The fact to remember (self-contained, one fact)"},
        },
        "required": ["owner", "repo", "content"],
    },
)
def memory_remember(owner: str, repo: str, content: str,
                    wiki_version: Any = None) -> str:
    from api import engraphis_integration
    return engraphis_integration.remember(
        _memory_workspace(owner, repo, wiki_version), content, source="code_agent"
    )


@_tool(
    "memory_recall",
    "Recall previously stored memories for this wiki release (earlier decisions, "
    "preferences, findings from past chat or code-editing sessions).",
    {
        "type": "object",
        "properties": {
            **_MEMORY_SCOPE_PROPS,
            "query": {"type": "string", "description": "What to recall"},
        },
        "required": ["owner", "repo", "query"],
    },
)
def memory_recall(owner: str, repo: str, query: str,
                  wiki_version: Any = None) -> str:
    from api import engraphis_integration
    return engraphis_integration.recall(
        _memory_workspace(owner, repo, wiki_version), query
    )


@_tool(
    "memory_why",
    "Explain WHY the memories matching a query were recalled (Engraphis's recall "
    "trace: scope gate, ranking signals, receipts).",
    {
        "type": "object",
        "properties": {
            **_MEMORY_SCOPE_PROPS,
            "query": {"type": "string"},
        },
        "required": ["owner", "repo", "query"],
    },
)
def memory_why(owner: str, repo: str, query: str, wiki_version: Any = None) -> str:
    from api import engraphis_integration
    return engraphis_integration.why(
        _memory_workspace(owner, repo, wiki_version), query
    )


@_tool(
    "memory_timeline",
    "Chronological history of the memories matching a query for this wiki release "
    "(including superseded versions of changed facts).",
    {
        "type": "object",
        "properties": {
            **_MEMORY_SCOPE_PROPS,
            "query": {"type": "string"},
        },
        "required": ["owner", "repo", "query"],
    },
)
def memory_timeline(owner: str, repo: str, query: str,
                    wiki_version: Any = None) -> str:
    from api import engraphis_integration
    return engraphis_integration.timeline(
        _memory_workspace(owner, repo, wiki_version), query
    )


def list_tools() -> list[dict[str, Any]]:
    """MCP tools/list response shape."""
    return [
        {
            "name": name,
            "description": t["description"],
            "inputSchema": t["inputSchema"],
        }
        for name, t in TOOL_REGISTRY.items()
    ]


def call_tool(name: str, arguments: dict[str, Any]) -> str:
    """Invoke a registered tool. Returns its text result. Raises ValueError
    for an unknown tool (the server maps that to a JSON-RPC error)."""
    entry = TOOL_REGISTRY.get(name)
    if not entry:
        raise ValueError(f"Unknown tool: {name}")
    handler = entry["handler"]
    return handler(**(arguments or {}))
