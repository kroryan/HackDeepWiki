"""
Unified content search across both source types: a .zim archive and a
git-repo wiki (backed by the RAG/FAISS retriever already prepared for a chat
connection). Both the "page + related pages" initial-context builder and the
agent's SEARCH_WIKI tool call this same function, so a .zim and a normal
repo behave identically from the chat's point of view.
"""
import logging
import os
from typing import Callable, Optional, TypedDict
from urllib.parse import quote

from api import zim_library, zim_reader
from api.data_pipeline import get_file_content

logger = logging.getLogger(__name__)

# Tool name -> the textual prefix the model emits to invoke it (see
# api/agent_loop.py's multi-prefix sniff_and_relay) and a short label used
# for the backend-owned "(Buscando: ...)"-style status marker shown while
# it runs.
SEARCH_WIKI = "SEARCH_WIKI:"
READ_FILE = "READ_FILE:"

TOOL_LABELS = {
    SEARCH_WIKI: "Buscando",
    READ_FILE: "Leyendo archivo",
}

# One-line usage description per tool, shown to the model in
# TOOL_CALLING_INSTRUCTIONS -- {subject} is filled in by build_tools_block
# with "ZIM archive" or "repository" to match how the rest of the prompt
# refers to the source.
TOOL_DESCRIPTIONS = {
    SEARCH_WIKI: "{SEARCH_WIKI} <a short search query>  -- full-text search over the {subject}",
    READ_FILE: "{READ_FILE} <path to a file, e.g. api/main.py>  -- read a file's FULL content (a search result only gives a short snippet; use this when you need the whole file)",
}


def build_tools_block(tools: dict[str, Callable[[str], str]], subject: str) -> str:
    """Render the per-tool usage lines TOOL_CALLING_INSTRUCTIONS lists,
    limited to whatever's actually available for this chat (e.g. READ_FILE
    only exists for repo chats, never .zim -- see resolve_tool_calling)."""
    lines = []
    for prefix in tools:
        template = TOOL_DESCRIPTIONS.get(prefix)
        if not template:
            continue
        lines.append(template.format(SEARCH_WIKI=SEARCH_WIKI, READ_FILE=READ_FILE, subject=subject))
    return "\n".join(lines)


# Providers whose real API supports structured/native tool-calling (Anthropic
# Messages API `tool_use` blocks, or an OpenAI-compatible `tools`/`tool_calls`
# chat-completion field) via a client that exposes `acall_with_tools` --
# see api/anthropic_client.py and api/openai_client.py. Routed through
# api.agent_loop.run_native_tool_chat instead of the textual
# sniff_and_relay/SEARCH_WIKI: convention used for every other provider,
# since the API itself enforces the call shape instead of relying on the
# model choosing to comply with prompted-in text -- confirmed live that some
# reasoning models (seen with the gpt-oss family via Ollama, which isn't in
# this set since Ollama has no native tool-calling client here) reliably
# narrate ("Let me search for...") instead of emitting the exact textual
# line even after strengthening the anti-narration wording repeatedly.
# `litellm` is included because api.litellm_client.LiteLLMClient subclasses
# OpenAIClient and only overrides client construction, so it inherits
# acall_with_tools unchanged. `openrouter`, `azure`, `bedrock`, `dashscope`
# each use a differently-shaped client (their own aiohttp/SDK calls) that
# doesn't have this method -- they keep using the textual convention.
NATIVE_TOOL_PROVIDERS = {"claude", "openai", "openai_custom", "litellm"}

# Native tool-calling schemas: both formats below describe the exact same
# tools, just in each API's own shape. Every tool here takes exactly one
# string argument (a search query or a file path), matching the textual
# convention's "<prefix>: <single line of text>" shape one-for-one, so the
# same `tools: dict[prefix -> handler]` built by resolve_tool_calling works
# for both the textual and native paths without any other change.
_NATIVE_TOOL_NAMES = {
    SEARCH_WIKI: "search_wiki",
    READ_FILE: "read_file",
}
_NATIVE_TOOL_PARAMS = {
    SEARCH_WIKI: "query",
    READ_FILE: "path",
}
_NATIVE_TOOL_DESCRIPTIONS = {
    SEARCH_WIKI: "Full-text search over the {subject}. Returns matching pages/files with a short snippet each.",
    READ_FILE: "Read one file's FULL content from the repository, given its path (e.g. api/main.py). Use this when a search result's snippet isn't enough to answer.",
}


def build_tool_schemas_anthropic(tools: dict[str, Callable[[str], str]], subject: str) -> list[dict]:
    """Anthropic Messages API `tools` shape for whichever prefixes are
    actually on offer for this chat (see resolve_tool_calling)."""
    schemas = []
    for prefix in tools:
        name = _NATIVE_TOOL_NAMES.get(prefix)
        param = _NATIVE_TOOL_PARAMS.get(prefix)
        if not name or not param:
            continue
        schemas.append({
            "name": name,
            "description": _NATIVE_TOOL_DESCRIPTIONS[prefix].format(subject=subject),
            "input_schema": {
                "type": "object",
                "properties": {param: {"type": "string"}},
                "required": [param],
            },
        })
    return schemas


def build_tool_schemas_openai(tools: dict[str, Callable[[str], str]], subject: str) -> list[dict]:
    """OpenAI-compatible chat-completions `tools` shape (function-calling)
    for the same prefixes -- used for openai/openai_custom/litellm."""
    schemas = []
    for prefix in tools:
        name = _NATIVE_TOOL_NAMES.get(prefix)
        param = _NATIVE_TOOL_PARAMS.get(prefix)
        if not name or not param:
            continue
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": _NATIVE_TOOL_DESCRIPTIONS[prefix].format(subject=subject),
                "parameters": {
                    "type": "object",
                    "properties": {param: {"type": "string"}},
                    "required": [param],
                },
            },
        })
    return schemas


def native_tool_name_to_prefix(name: str) -> Optional[str]:
    """Reverse of _NATIVE_TOOL_NAMES -- maps a tool_use/tool_call's `name`
    field (e.g. "search_wiki") back to the textual prefix (e.g.
    "SEARCH_WIKI:") that `tools` dicts are keyed by, so run_native_tool_chat
    can dispatch to the same handlers resolve_tool_calling already built."""
    for prefix, tool_name in _NATIVE_TOOL_NAMES.items():
        if tool_name == name:
            return prefix
    return None


# Cap on how much of a file's content goes into the tool result -- a huge
# file would blow past what's reasonable to feed back into the prompt (and
# most of the time the model only needs the relevant portion, which it
# already saw a snippet of via search_repo/RAG before asking to read the
# whole thing).
MAX_FILE_CHARS = 8000


class SearchResult(TypedDict):
    title: str
    snippet: str
    ref: str  # zim entry path, or file_path for a repo


def search_zim(zim_path: str, query: str, limit: int = 5) -> list[SearchResult]:
    archive = zim_reader.open_archive(zim_path)
    hits = zim_reader.search_entries(archive, query, limit=limit)
    results: list[SearchResult] = []
    for hit in hits:
        try:
            content, mimetype = zim_reader.get_entry_content(archive, hit["path"])
            snippet = (
                zim_reader.extract_plain_text(content, max_chars=1000)
                if mimetype.startswith("text/html")
                else ""
            )
        except Exception as e:
            logger.warning(f"Could not read ZIM entry {hit['path']!r} for snippet: {e}")
            snippet = ""
        results.append({"title": hit["title"], "snippet": snippet, "ref": hit["path"]})
    return results


def search_repo(request_rag, query: str, language: str = "en", limit: int = 5) -> list[SearchResult]:
    """`request_rag` is the RAG instance already prepared (embedded/retriever
    built) for the current chat connection -- reused here rather than
    creating a second one, since preparing a retriever re-embeds the whole
    repo and is expensive."""
    try:
        retrieved = request_rag(query, language=language)
    except Exception as e:
        logger.warning(f"Repo search failed for query {query!r}: {e}")
        return []
    if not retrieved or not retrieved[0].documents:
        return []
    results: list[SearchResult] = []
    for doc in retrieved[0].documents[:limit]:
        file_path = doc.meta_data.get("file_path", "unknown")
        results.append({
            "title": file_path,
            "snippet": doc.text[:1000],
            "ref": file_path,
        })
    return results


def read_file(repo_url: str, repo_type: str, token: Optional[str], file_path: str) -> str:
    """Full content of one file in the repo -- the thing a RAG/SEARCH_WIKI
    hit can't give the model: a chunked snippet is often not enough to
    understand a whole function/class, so this lets the agent ask for the
    complete file once it knows the path (from an earlier search result).
    Only offered for repo chats, never .zim (see resolve_tool_calling) --
    a .zim entry's "page" already comes back in full via SEARCH_WIKI, there
    is no separate "file" concept for it.
    """
    content = get_file_content(repo_url, file_path.strip(), repo_type, token)
    if len(content) > MAX_FILE_CHARS:
        content = content[:MAX_FILE_CHARS] + f"\n\n... (truncated, {len(content)} chars total)"
    return f"## {file_path}\n\n```\n{content}\n```"


def format_search_results(results: list[SearchResult]) -> str:
    """Render results as the `<tool_result>` block injected back into the
    conversation for both the initial-context builder and the agent loop."""
    if not results:
        return "No results found."
    parts = []
    for r in results:
        parts.append(f"## {r['title']} ({r['ref']})\n\n{r['snippet']}")
    return "\n\n---\n\n".join(parts)


class PageRef(TypedDict):
    title: str
    ref: str


def _zim_id_for_path(zim_path: str) -> Optional[str]:
    """The chat backend only ever knows a .zim by its filesystem path
    (repo_url doubles as the path for type='zim', see websocket_wiki.py),
    but a clickable link needs the library id the frontend routes by
    (/zim/{id}) -- reverse-lookup it from the registry."""
    for entry in zim_library.list_all():
        if entry["path"] == zim_path:
            return entry["id"]
    return None


def format_sources_footer(
    refs: list[PageRef],
    is_zim: bool,
    zim_path: Optional[str] = None,
    label: str = "Pages consulted",
) -> str:
    """Render the distinct "pages consulted" footer appended after an
    answer -- deduped by ref, in first-seen order. ZIM entries get a real
    clickable link (opens that entry directly); repo source files get a
    `codefile:<path>` pseudo-link -- not a real URL (repo access needs the
    caller's own repo_url/type/token, which don't belong in a link a bot or
    browser might otherwise try to follow), just a marker the frontend's
    Markdown renderer (src/components/Markdown.tsx) intercepts to open the
    in-app code viewer instead of navigating. `label` lets callers localize
    it to the same language as the response."""
    seen: dict[str, str] = {}
    for r in refs:
        seen.setdefault(r["ref"], r["title"])
    if not seen:
        return ""

    if is_zim and zim_path:
        zim_id = _zim_id_for_path(zim_path)
        if zim_id:
            items = [
                f"[{title}](/api/zim/{zim_id}/entry?path={quote(ref, safe='')})"
                for ref, title in seen.items()
            ]
        else:
            items = list(seen.values())
    else:
        items = [f"[`{title}`](codefile:{quote(ref, safe='')})" for ref, title in seen.items()]

    return f"\n\n---\n*📚 {label}: " + " · ".join(items) + "*"


def _record(refs_sink: Optional[list], results: list[SearchResult]) -> None:
    if refs_sink is None:
        return
    for r in results:
        refs_sink.append({"title": r["title"], "ref": r["ref"]})


def build_zim_context(
    zim_path: str,
    query: str,
    current_entry_path: Optional[str],
    limit: int = 5,
    refs_sink: Optional[list] = None,
) -> str:
    """Context for a .zim chat: when the chat was opened from a specific
    entry, that entry (full plain text) plus up to `limit` related entries
    (found by searching the archive's own title, i.e. "what is this page
    about") -- never the whole archive, which can hold millions of entries.
    Without a current entry, falls back to searching the user's own query.

    If `refs_sink` is given, every page actually included in the context
    (the current page plus each related/searched page) is appended to it
    as `{title, ref}` -- used to show the user which pages the answer
    actually drew on.
    """
    archive = zim_reader.open_archive(zim_path)

    if not current_entry_path:
        results = search_zim(zim_path, query, limit=limit)
        _record(refs_sink, results)
        return format_search_results(results)

    try:
        content, mimetype = zim_reader.get_entry_content(archive, current_entry_path)
        current_title = zim_reader.get_entry_title(archive, current_entry_path)
    except Exception as e:
        logger.warning(f"Could not load current ZIM entry {current_entry_path!r}: {e}")
        results = search_zim(zim_path, query, limit=limit)
        _record(refs_sink, results)
        return format_search_results(results)

    _record(refs_sink, [{"title": current_title, "snippet": "", "ref": current_entry_path}])

    page_text = (
        zim_reader.extract_plain_text(content, max_chars=3000)
        if mimetype.startswith("text/html")
        else ""
    )
    related = [
        r for r in search_zim(zim_path, current_title, limit=limit + 1)
        if r["ref"] != current_entry_path
    ][:limit]
    _record(refs_sink, related)

    parts = [f"## Current page: {current_title} ({current_entry_path})\n\n{page_text}"]
    if related:
        parts.append("# Related pages\n\n" + format_search_results(related))
    return "\n\n---\n\n".join(parts)


def build_repo_context(
    request_rag,
    query: str,
    current_page_title: Optional[str],
    language: str = "en",
    limit: int = 5,
    refs_sink: Optional[list] = None,
) -> str:
    """Context for a normal repo-wiki chat. When opened from a specific wiki
    page, the retrieval query is anchored to that page's title instead of
    just the user's question, so FAISS returns documents relevant to the
    page being viewed rather than the whole repo."""
    effective_query = current_page_title or query
    results = search_repo(request_rag, effective_query, language=language, limit=limit)
    _record(refs_sink, results)
    return format_search_results(results)


def resolve_tool_calling(
    *,
    enable_tool_calling: Optional[bool],
    is_deep_research: bool,
    is_zim: bool,
    zim_path: Optional[str],
    request_rag,
    language: str,
    repo_url: Optional[str] = None,
    repo_type: Optional[str] = None,
    token: Optional[str] = None,
    refs_sink: Optional[list] = None,
) -> tuple[bool, dict[str, Callable[[str], str]]]:
    """Shared gate + tool resolution for the agent loop (api/agent_loop.py),
    used identically by the WebSocket and HTTP chat handlers so the two
    transports can't drift on what "tool calling enabled" means or what
    tools are on offer. Never enabled for Deep Research (it has its own
    multi-iteration structure/prompts) or via the
    HACKDEEPWIKI_DISABLE_AGENT_LOOP=1 env killswitch.

    Returns (enabled, tools) where `tools` maps a textual prefix (e.g.
    "SEARCH_WIKI:") to a `query -> tool_result text` handler. Every source
    type gets SEARCH_WIKI; repo chats (never .zim -- there's no separate
    "file" concept for a wiki entry) additionally get READ_FILE, since a
    RAG/search hit is only ever a chunked snippet and the model sometimes
    needs the whole file to make sense of it.

    When `refs_sink` is given, every page/file a tool call actually reads
    during the conversation is appended to it too, alongside whatever the
    initial context builder already recorded -- so the caller can show a
    single, complete "pages consulted" list covering the whole answer.
    """
    enabled = (
        bool(enable_tool_calling)
        and not is_deep_research
        and os.environ.get("HACKDEEPWIKI_DISABLE_AGENT_LOOP") != "1"
    )
    if not enabled:
        return False, {}

    tools: dict[str, Callable[[str], str]] = {}

    if is_zim:
        def search_fn(q: str, _path=zim_path) -> str:
            results = search_zim(_path, q, limit=5)
            _record(refs_sink, results)
            return format_search_results(results)
        tools[SEARCH_WIKI] = search_fn
    elif request_rag is not None:
        def search_fn(q: str, _rag=request_rag, _lang=language) -> str:
            results = search_repo(_rag, q, language=_lang, limit=5)
            _record(refs_sink, results)
            return format_search_results(results)
        tools[SEARCH_WIKI] = search_fn

        if repo_url:
            def read_file_fn(path: str, _url=repo_url, _type=repo_type, _token=token) -> str:
                try:
                    result = read_file(_url, _type, _token, path)
                except Exception as e:
                    return f"Could not read {path!r}: {e}"
                _record(refs_sink, [{"title": path.strip(), "snippet": "", "ref": path.strip()}])
                return result
            tools[READ_FILE] = read_file_fn
    else:
        return False, {}

    if not tools:
        return False, {}
    return True, tools
