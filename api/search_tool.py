"""
Unified content search across both source types: a .zim archive and a
git-repo wiki (backed by the RAG/FAISS retriever already prepared for a chat
connection). Both the "page + related pages" initial-context builder and the
agent's SEARCH_WIKI tool call this same function, so a .zim and a normal
repo behave identically from the chat's point of view.
"""
import asyncio
import json
import logging
import os
import re
from typing import Any, Awaitable, Callable, Optional, TypedDict
from urllib.parse import quote

from api import mcp_client, zim_library, zim_reader
from api.data_pipeline import get_file_content

logger = logging.getLogger(__name__)

# A tool handler is an async callable taking either a single string (the
# built-in SEARCH_WIKI/READ_FILE, whose textual convention is one line) or
# a dict of named arguments (external MCP tools, which can be multi-arg).
# Each handler normalizes its input itself (see _coerce_str_arg /
# _coerce_dict_arg) so the agent loop can pass whatever shape it has -- a
# string from the textual sniff path, or the full args dict from the native
# tool-calling path -- without knowing which kind of tool it's dispatching.
ToolHandler = Callable[[Any], Awaitable[str]]


# Tool name -> the textual prefix the model emits to invoke it (see
# api/agent_loop.py's multi-prefix sniff_and_relay) and a short label used
# for the backend-owned "(Buscando: ...)"-style status marker shown while
# it runs.
SEARCH_WIKI = "SEARCH_WIKI:"
READ_FILE = "READ_FILE:"
# Engraphis memory (api/engraphis_integration.py). Scoped to THIS wiki
# release's workspace -- memory is never shared across wikis or versions.
MEMORY_REMEMBER = "MEMORY_REMEMBER:"
MEMORY_RECALL = "MEMORY_RECALL:"

TOOL_LABELS = {
    SEARCH_WIKI: "Buscando",
    READ_FILE: "Leyendo archivo",
    MEMORY_REMEMBER: "Guardando en memoria",
    MEMORY_RECALL: "Recordando",
}

# One-line usage description per tool, shown to the model in
# TOOL_CALLING_INSTRUCTIONS -- {subject} is filled in by build_tools_block
# with "ZIM archive" or "repository" to match how the rest of the prompt
# refers to the source.
TOOL_DESCRIPTIONS = {
    SEARCH_WIKI: "{SEARCH_WIKI} <a short search query>  -- full-text search over the {subject}",
    READ_FILE: "{READ_FILE} <path to a file, e.g. api/main.py>  -- read a file's FULL content (a search result only gives a short snippet; use this when you need the whole file)",
    MEMORY_REMEMBER: "{MEMORY_REMEMBER} <one self-contained fact worth keeping>  -- store a durable memory for THIS wiki release (decisions, user preferences, conclusions). Use it whenever the user states a decision/preference or asks you to remember something",
    MEMORY_RECALL: "{MEMORY_RECALL} <a short question>  -- recall previously stored memories for THIS wiki release (earlier decisions, preferences, findings). Use it before answering questions about past work",
}


def _coerce_str_arg(arg: Any) -> str:
    """Built-in tool handlers conceptually take one string (the search query
    or file path). The native tool-calling path passes the full args dict, so
    coerce a dict back to its first value -- mirroring the old
    `next(iter(args.values()), "")` collapse -- and anything else to str."""
    if isinstance(arg, dict):
        return next(iter(arg.values()), "") or ""
    if arg is None:
        return ""
    return str(arg)


def _coerce_dict_arg(arg: Any, input_schema: dict) -> dict:
    """External MCP tool handlers take a dict of named arguments. The textual
    sniff path only ever has a single line, so: if the model emitted valid
    JSON, parse it; otherwise wrap the bare string into the schema's first
    property (the most common single-arg case) so a textual call still works."""
    if isinstance(arg, dict):
        return arg
    if arg is None:
        return {}
    if isinstance(arg, str):
        s = arg.strip()
        if s[:1] in "{[":
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:  # noqa: BLE001 - fall through to single-string wrap
                pass
        props = (input_schema or {}).get("properties", {}) if isinstance(input_schema, dict) else {}
        first_prop = next(iter(props), None) if isinstance(props, dict) else None
        if first_prop:
            return {first_prop: arg}
        return {"input": arg}
    return {}


def _sanitize_tool_name(name: str) -> str:
    """OpenAI/Anthropic function names must match ^[a-zA-Z0-9_-]{1,64}$.
    Replace anything else with '_' and cap length; never return empty."""
    clean = re.sub(r"[^a-zA-Z0-9_-]", "_", str(name or "")).strip("_")
    return (clean[:64] or "tool")


def build_tools_block(
    tools: dict[str, ToolHandler],
    subject: str,
    external_tools: Optional[list[dict]] = None,
) -> str:
    """Render the per-tool usage lines TOOL_CALLING_INSTRUCTIONS lists,
    limited to whatever's actually available for this chat (e.g. READ_FILE
    only exists for repo chats, never .zim -- see resolve_tool_calling).

    External MCP tools (when the user has configured servers) are appended
    after the built-ins with their own one-line usage: the textual convention
    for a multi-arg external tool is `PREFIX: <json arguments object>`."""
    lines = []
    for prefix in tools:
        template = TOOL_DESCRIPTIONS.get(prefix)
        if template:
            lines.append(template.format(
                SEARCH_WIKI=SEARCH_WIKI, READ_FILE=READ_FILE,
                MEMORY_REMEMBER=MEMORY_REMEMBER, MEMORY_RECALL=MEMORY_RECALL,
                subject=subject,
            ))
    for ext in external_tools or []:
        desc = (ext.get("description") or "").strip().splitlines()[0:1]
        desc_str = desc[0] if desc else f"External tool {ext.get('tool_name')}"
        # Cap so the tools block stays reasonable for small context windows.
        desc_str = desc_str[:160]
        lines.append(f'{ext["prefix"]} <json arguments object>  -- {desc_str}')
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
    MEMORY_REMEMBER: "memory_remember",
    MEMORY_RECALL: "memory_recall",
}
_NATIVE_TOOL_PARAMS = {
    SEARCH_WIKI: "query",
    READ_FILE: "path",
    MEMORY_REMEMBER: "content",
    MEMORY_RECALL: "query",
}
_NATIVE_TOOL_DESCRIPTIONS = {
    SEARCH_WIKI: "Full-text search over the {subject}. Returns matching pages/files with a short snippet each.",
    READ_FILE: "Read one file's FULL content from the repository, given its path (e.g. api/main.py). Use this when a search result's snippet isn't enough to answer.",
    MEMORY_REMEMBER: "Store one durable, self-contained memory for this exact wiki release (a decision, user preference, or conclusion worth keeping across sessions). Memory is scoped to this wiki version only.",
    MEMORY_RECALL: "Recall previously stored memories for this exact wiki release (earlier decisions, preferences, findings from past chat or code-editing sessions).",
}


def build_tool_schemas_anthropic(
    tools: dict[str, ToolHandler],
    subject: str,
    external_tools: Optional[list[dict]] = None,
) -> list[dict]:
    """Anthropic Messages API `tools` shape for whichever prefixes are
    actually on offer for this chat (see resolve_tool_calling). External
    MCP tools are appended with their own JSON schema (from the server's
    inputSchema) so the native path can call them with full multi-arg
    input instead of the single-string built-in shape."""
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
    for ext in external_tools or []:
        input_schema = ext.get("input_schema")
        if not isinstance(input_schema, dict) or not input_schema:
            input_schema = {"type": "object", "properties": {}}
        schemas.append({
            "name": ext["native_name"],
            "description": (ext.get("description") or f"External MCP tool {ext.get('tool_name')}")[:1024],
            "input_schema": input_schema,
        })
    return schemas


def build_tool_schemas_openai(
    tools: dict[str, ToolHandler],
    subject: str,
    external_tools: Optional[list[dict]] = None,
) -> list[dict]:
    """OpenAI-compatible chat-completions `tools` shape (function-calling)
    for the same prefixes -- used for openai/openai_custom/litellm. External
    MCP tools are appended with their own JSON schema (from the server's
    inputSchema) so the native path can call them with full multi-arg input."""
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
    for ext in external_tools or []:
        input_schema = ext.get("input_schema")
        if not isinstance(input_schema, dict) or not input_schema:
            input_schema = {"type": "object", "properties": {}}
        schemas.append({
            "type": "function",
            "function": {
                "name": ext["native_name"],
                "description": (ext.get("description") or f"External MCP tool {ext.get('tool_name')}")[:1024],
                "parameters": input_schema,
            },
        })
    return schemas


def native_tool_name_to_prefix(name: str) -> Optional[str]:
    """Reverse of _NATIVE_TOOL_NAMES -- maps a built-in tool_use/tool_call's
    `name` field (e.g. "search_wiki") back to the textual prefix (e.g.
    "SEARCH_WIKI:") that `tools` dicts are keyed by, so run_native_tool_chat
    can dispatch to the same handlers resolve_tool_calling already built.

    External MCP tools have per-chat native names (built from server+tool),
    so the native loop also passes an `external_name_to_prefix` map; this
    function only handles the two built-ins."""
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


async def resolve_tool_calling(
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
    owner: Optional[str] = None,
    repo: Optional[str] = None,
    wiki_version: Optional[int] = None,
) -> tuple[bool, dict[str, ToolHandler], list[dict]]:
    """Shared gate + tool resolution for the agent loop (api/agent_loop.py),
    used identically by the WebSocket and HTTP chat handlers so the two
    transports can't drift on what "tool calling enabled" means or what
    tools are on offer. Never enabled for Deep Research (it has its own
    multi-iteration structure/prompts) or via the
    HACKDEEPWIKI_DISABLE_AGENT_LOOP=1 env killswitch.

    Returns ``(enabled, tools, external_tools)``:

    * ``tools`` maps a textual prefix (e.g. "SEARCH_WIKI:") to an async
      ``arg -> tool_result text`` handler. Every source type gets SEARCH_WIKI;
      repo chats (never .zim -- there's no separate "file" concept for a wiki
      entry) additionally get READ_FILE, since a RAG/search hit is only ever a
      chunked snippet and the model sometimes needs the whole file.
    * ``external_tools`` is a list of metadata dicts for tools from external
      MCP servers the user has configured (see api/mcp_client.py). Each entry
      carries the textual prefix, the sanitized native schema name, the
      description, the server's input schema, and the server dict -- enough for
      build_tools_block / build_tool_schemas_* to expose them on both the
      textual and native tool-calling paths, and for the agent loop to build a
      native_name -> prefix reverse map. Empty when no servers are configured
      or every server is unreachable (an unreachable external server must not
      break the chat -- the built-in tools still work).

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
        return False, {}, []

    tools: dict[str, ToolHandler] = {}

    if is_zim:
        async def search_fn(arg: Any, _path=zim_path) -> str:
            q = _coerce_str_arg(arg)
            results = search_zim(_path, q, limit=5)
            _record(refs_sink, results)
            return format_search_results(results)
        tools[SEARCH_WIKI] = search_fn
    elif request_rag is not None:
        async def search_fn(arg: Any, _rag=request_rag, _lang=language) -> str:
            q = _coerce_str_arg(arg)
            results = search_repo(_rag, q, language=_lang, limit=5)
            _record(refs_sink, results)
            return format_search_results(results)
        tools[SEARCH_WIKI] = search_fn

        if repo_url:
            async def read_file_fn(arg: Any, _url=repo_url, _type=repo_type, _token=token) -> str:
                path = _coerce_str_arg(arg)
                try:
                    result = read_file(_url, _type, _token, path)
                except Exception as e:
                    return f"Could not read {path!r}: {e}"
                _record(refs_sink, [{"title": path.strip(), "snippet": "", "ref": path.strip()}])
                return result
            tools[READ_FILE] = read_file_fn
    else:
        return False, {}, []

    if not tools:
        return False, {}, []

    # Engraphis memory tools -- per-wiki-release scope, shared with the code
    # editor. Only offered when the package is installed AND the request
    # identifies a wiki (owner+repo); .zim archives have no owner/repo and get
    # no memory. Handlers run in a thread: recall does numpy/SQLite work that
    # must not block the event loop.
    try:
        from api import engraphis_integration
        if owner and repo and engraphis_integration.is_available():
            workspace = engraphis_integration.workspace_for_version(
                owner, repo, wiki_version
            )

            async def memory_remember_fn(arg: Any, _ws=workspace) -> str:
                content = _coerce_str_arg(arg)
                return await asyncio.to_thread(
                    engraphis_integration.remember, _ws, content, source="chat"
                )

            async def memory_recall_fn(arg: Any, _ws=workspace) -> str:
                q = _coerce_str_arg(arg)
                return await asyncio.to_thread(
                    engraphis_integration.recall, _ws, q
                )

            tools[MEMORY_REMEMBER] = memory_remember_fn
            tools[MEMORY_RECALL] = memory_recall_fn
    except Exception as e:  # noqa: BLE001 - memory must never break a chat
        logger.warning("Engraphis chat tools unavailable: %s", e)

    external_tools = await _collect_external_tools()
    for ext in external_tools:
        tools[ext["prefix"]] = ext["handler"]

    return True, tools, external_tools


async def _collect_external_tools() -> list[dict]:
    """Enumerate the user's enabled external MCP servers and build a tool
    metadata entry per tool each one exposes. Best-effort and isolated: a
    server that's down, slow, or returns a malformed tools/list is skipped
    (logged) so it can never break the chat. Returns [] when MCP is unused."""
    external_tools: list[dict] = []
    try:
        servers = mcp_client.list_servers()
    except Exception as e:  # noqa: BLE001 - profile.db not ready / corrupt -> no external tools
        logger.warning(f"MCP list_servers failed (external tools disabled): {e}")
        return external_tools

    enabled_servers = [s for s in servers if s.get("enabled")]
    if not enabled_servers:
        return external_tools

    # Query all enabled servers concurrently so one slow server only costs its
    # own LIST_TOOLS_TIMEOUT, not the sum of every server's latency.
    tool_lists = await asyncio.gather(
        *(mcp_client.list_server_tools_timed(s) for s in enabled_servers),
        return_exceptions=False,
    )

    seen_native_names: set[str] = set()
    for server, tool_list in zip(enabled_servers, tool_lists):
        server_name = _sanitize_tool_name(server.get("name", "server"))
        for tool in tool_list or []:
            if not isinstance(tool, dict):
                continue
            tool_name = tool.get("name") or ""
            if not tool_name:
                continue
            # Unique, schema-safe native name. Server-prefixed so two servers
            # exposing a same-named tool don't collide; de-duped defensively.
            native_name = f"mcp_{server_name}_{_sanitize_tool_name(tool_name)}"
            base = native_name[:55]
            candidate = base
            i = 2
            while candidate in seen_native_names:
                suffix = f"_{i}"
                candidate = (base[: 64 - len(suffix)]) + suffix
                i += 1
            seen_native_names.add(candidate)
            input_schema = tool.get("inputSchema")
            if not isinstance(input_schema, dict):
                input_schema = {"type": "object", "properties": {}}
            prefix = f"MCP_{server_name}__{_sanitize_tool_name(tool_name)}:"
            description = tool.get("description") or f"External MCP tool {tool_name} from {server.get('name')}"

            # Capture per-tool bindings in the closure defaults so the handler
            # is self-contained and safe to call concurrently across chats.
            def _make_handler(_server=server, _tool_name=tool_name, _schema=input_schema):
                async def handler(arg: Any) -> str:
                    args = _coerce_dict_arg(arg, _schema)
                    return await mcp_client.call_server_tool(_server, _tool_name, args)
                return handler

            external_tools.append({
                "prefix": prefix,
                "native_name": candidate,
                "tool_name": tool_name,
                "server": server,
                "description": description,
                "input_schema": input_schema,
                "handler": _make_handler(),
            })
    if external_tools:
        logger.info(
            "MCP external tools registered: %d from %d server(s)",
            len(external_tools), len(enabled_servers),
        )
    return external_tools
