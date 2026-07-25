"""Shared primitives for the two chat transports (WebSocket via
``websocket_wiki.handle_websocket_chat`` and HTTP via ``simple_chat``).

Fase 8.2 -- the context-limit fallback path was hand-mirrored between the two
transports: ``MAX_FALLBACK_QUERY_CHARS``, ``CONTEXT_LIMIT_ERROR_PHRASES``,
``_is_context_limit_error`` and the head/tail query-truncation block were
copy-pasted with cross-referencing comments ("mirrors ... so the two
transports can't drift"). That parity is real today, but it's maintained by
discipline -- any future tweak has to be made in two places that are easy to
forget. This module converts the hand-mirror into actual shared code so the
two transports import the same constants and call the same truncation helper,
and can't drift by construction.

Stdlib + logging only. No new dependency (portable).
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Character budget for `query` in the token-limit fallback path. Roughly a
# few thousand tokens -- generous enough for real questions, small enough to
# actually fit after a "prompt too long" error, whatever the original size.
MAX_FALLBACK_QUERY_CHARS = 8000

# Substrings that indicate a provider rejected the request for being too
# long, across every provider/wording variant seen in practice -- checked
# case-insensitively against the exception message to decide whether to
# retry with a truncated prompt. This used to only match "maximum context
# length" (OpenAI's exact wording), which missed Ollama's own phrasing
# ("...exceeded max context length by N tokens") -- missing "maximum" vs
# "max" meant Ollama's oversized-prompt errors were never caught and always
# surfaced as a hard failure instead of falling back to a truncated prompt,
# regardless of repo/model.
CONTEXT_LIMIT_ERROR_PHRASES = (
    "maximum context length",
    "max context length",
    "context length",
    "context_length_exceeded",
    "token limit",
    "too many tokens",
    "prompt is too long",
    "prompt too long",
    "input is too long",
)


def is_context_limit_error(error_message: str) -> bool:
    """True if an exception message indicates the provider rejected the
    request for being too long (any of CONTEXT_LIMIT_ERROR_PHRASES,
    case-insensitive). Used by both transports' fallback handler."""
    lowered = (error_message or "").lower()
    return any(phrase in lowered for phrase in CONTEXT_LIMIT_ERROR_PHRASES)


# Back-compat alias for the original private name both transports used.
_is_context_limit_error = is_context_limit_error


def truncate_query_for_fallback(query: str, max_chars: int = MAX_FALLBACK_QUERY_CHARS) -> str:
    """Cap a fallback query defensively: keep the head (task instructions)
    and tail (the actual question, usually at the end) and drop the middle,
    which is where a runaway file/content list tends to live. If the query
    already fits, return it unchanged. Logs a warning when it truncates, so
    operators can see the oversized-prompt case actually fired (both
    transports used to log this independently)."""
    if not query or len(query) <= max_chars:
        return query
    half = max_chars // 2
    head = query[:half]
    tail = query[-half:]
    truncated = (
        f"{head}\n\n[... truncated: original query was "
        f"{len(query)} characters, too large to process ...]\n\n{tail}"
    )
    logger.warning(
        f"Query itself was oversized ({len(query)} chars); truncated for fallback"
    )
    return truncated


def apply_skills_to_system_prompt(system_prompt: str, selected_skills) -> str:
    """Fase 6 -- append the opt-in skills block to a system prompt.

    ``selected_skills`` is the request's ``skills`` field (a list of skill
    names or None). When empty/None, or when no discovered skill matches, this
    is a no-op and returns ``system_prompt`` unchanged -- so chats that don't
    opt in pay zero context cost. The actual discovery + rendering lives in
    ``api.skills.render_skills_block``; this wrapper exists in chat_common so
    BOTH chat transports (simple_chat HTTP + websocket_wiki WS) call the same
    helper and can't drift on where/whether skills are injected. Importing
    ``api.skills`` lazily here keeps chat_common dependency-light (skills is
    only imported on the path that actually uses it)."""
    if not selected_skills:
        return system_prompt
    try:
        from api.skills import render_skills_block
    except Exception:  # noqa: BLE001 -- skills is optional; never break chat
        logger.warning("skills module unavailable; skipping skills injection")
        return system_prompt
    block = render_skills_block(list(selected_skills))
    if not block:
        return system_prompt
    return f"{system_prompt}\n\n{block}"


def apply_memory_to_system_prompt(system_prompt: str, *, owner, repo,
                                  wiki_version, query: str) -> str:
    """Engraphis proactive recall -- prepend previously stored memories for
    THIS wiki release to the system prompt, so the model starts the answer
    already knowing earlier decisions/preferences without having to call
    MEMORY_RECALL first.

    Lives in chat_common for the same reason as the skills helper: BOTH chat
    transports (simple_chat HTTP + websocket_wiki WS) must call the exact same
    injection or they drift. No-op (returns the prompt unchanged) when
    Engraphis is not installed, when the request doesn't identify a wiki
    (owner/repo missing, e.g. .zim chats), or when nothing relevant is stored.
    Never raises: memory must never break a chat."""
    if not owner or not repo:
        return system_prompt
    try:
        from api import engraphis_integration
        if not engraphis_integration.is_available():
            return system_prompt
        workspace = engraphis_integration.workspace_for_version(
            owner, repo, wiki_version
        )
        block = engraphis_integration.proactive_block(workspace, query or "")
    except Exception as e:  # noqa: BLE001
        logger.warning("Engraphis proactive recall skipped: %s", e)
        return system_prompt
    if not block:
        return system_prompt
    return f"{system_prompt}\n\n{block}"

# Auto-capture bounds: one memory per exchange, big enough to hold a real
# answer's substance, small enough that a long chat can't bloat the store.
MAX_AUTOMEMORY_QUESTION_CHARS = 500
MAX_AUTOMEMORY_ANSWER_CHARS = 1500
MIN_AUTOMEMORY_ANSWER_CHARS = 120
MIN_AUTOMEMORY_QUESTION_CHARS = 8

# A line the model emitted as a *tool call* rather than as prose:
# "SEARCH_WIKI: foo", "READ_FILE: api/main.py", "MCP_server__tool: {...}".
# The exact prefixes api/search_tool.py defines, matched literally so a
# perfectly good "TODO:" or "NOTE:" line in an answer is not mistaken for
# protocol. When these reach the answer text at all, something upstream
# failed to intercept them (see api/agent_loop.sniff_and_relay) -- they are
# protocol noise, never knowledge, and must never be stored as a "fact".
_TOOL_CALL_LINE = re.compile(
    r"^\s*(SEARCH_WIKI|READ_FILE|MEMORY_REMEMBER|MEMORY_RECALL"
    r"|MCP_[A-Za-z0-9_]+):\s*\S"
)

# Openers of pure narration ("let me look at...", "voy a buscar..."). The
# sentence that follows them is about the model's own process, not about the
# project, so they carry no memory value.
_NARRATION_LINE = re.compile(
    r"^\s*(let me\b|i'?ll\b|i am going to\b|i'?m going to\b|now i\b|first,? i\b"
    r"|next,? i\b|let'?s\b|voy a\b|vamos a\b|primero\b|ahora\b|d[ée]jame\b)",
    re.IGNORECASE,
)

# Answers that report a failure instead of stating knowledge. Storing these
# poisons later recall: the next question gets "the tool is not working"
# injected as project context.
_JUNK_ANSWER_MARKERS = (
    "i don't have access", "i do not have access", "no tengo acceso",
    "i cannot access", "i can't access", "no puedo acceder",
    "the tool is not working", "the tool is not functioning",
    "tool is not available", "no such tool", "herramienta no funciona",
    "i was unable to", "no he podido", "no pude encontrar",
    "an error occurred", "ha ocurrido un error",
    "i'm sorry, but i", "lo siento, pero",
)


def is_worth_remembering(question: str, answer: str) -> bool:
    """Would storing this exchange make later recall better, or worse?

    Auto-capture used to store any answer over 120 characters, which meant a
    model that narrated its way through a broken tool loop ("SEARCH_WIKI: ...
    the tool doesn't seem to work ... let me try again") became a permanent
    "memory" -- and got re-injected into the next question's system prompt as
    project context. This gate keeps the store to exchanges that actually say
    something about the project.
    """
    question = _collapse(question)
    if len(question) < MIN_AUTOMEMORY_QUESTION_CHARS:
        return False  # "hi", "ok", "gracias"
    lines = [ln.strip() for ln in str(answer or "").splitlines() if ln.strip()]
    if not lines:
        return False

    substantive = [
        ln for ln in lines
        if not _TOOL_CALL_LINE.match(ln) and not _NARRATION_LINE.match(ln)
    ]
    body = _collapse(" ".join(substantive))
    if len(body) < MIN_AUTOMEMORY_ANSWER_CHARS:
        return False  # nothing left once protocol noise and narration go

    lowered = body.lower()
    if any(marker in lowered for marker in _JUNK_ANSWER_MARKERS):
        return False

    # A loop (the same sentence over and over while a tool never fires) has
    # very few distinct lines for its length; a real answer doesn't.
    if len(lines) >= 6 and len(set(lines)) * 2 < len(lines):
        return False

    # Protocol noise dominating the reply means the answer IS the failure.
    tool_lines = sum(1 for ln in lines if _TOOL_CALL_LINE.match(ln))
    if tool_lines and tool_lines * 3 >= len(lines):
        return False
    return True


def distill_exchange(question: str, answer: str) -> str:
    """The storable substance of one exchange: the question, plus the answer
    with tool-protocol lines, narration and repetition removed.

    Deterministic (no extra model call, no tokens): the answer was already
    written: this only strips what is provably not knowledge. When the answer
    is longer than the budget, the head AND tail are kept -- an answer's
    conclusion usually lives at the end, and a plain truncation would drop
    exactly the part worth remembering.
    """
    question = _collapse(question)[:MAX_AUTOMEMORY_QUESTION_CHARS]
    kept: list[str] = []
    seen: set[str] = set()
    for line in str(answer or "").splitlines():
        line = line.strip()
        if not line or _TOOL_CALL_LINE.match(line) or _NARRATION_LINE.match(line):
            continue
        if line in seen:
            continue  # a repeated line adds nothing to a memory
        seen.add(line)
        kept.append(line)
    body = "\n".join(kept).strip()
    if len(body) > MAX_AUTOMEMORY_ANSWER_CHARS:
        half = MAX_AUTOMEMORY_ANSWER_CHARS // 2
        body = f"{body[:half].rstrip()}\n[...]\n{body[-half:].lstrip()}"
    return f"Q: {question}\nA: {body}"


def capture_chat_exchange(*, owner, repo, wiki_version, question: str,
                          answer: str, source: str = "chat") -> None:
    """Store one finished Q&A turn in this wiki release's memory -- after the
    answer is complete, filtered for relevance, and connected to the memories
    it relates to.

    Relying on the model to call MEMORY_REMEMBER doesn't work in practice: it
    answers and moves on, so the workspace stays empty. Writing the exchange
    ourselves makes memory accumulate from normal use -- but only when the
    exchange is worth keeping (``is_worth_remembering``) and only its
    substance (``distill_exchange``). ``remember_linked`` then wires it into
    the graph: ``follows`` the previous exchange in this workspace, and
    ``references`` the memories the question actually relates to (wiki pages,
    earlier decisions). Never raises -- memory must never break a chat.
    """
    if not owner or not repo:
        return
    if not is_worth_remembering(question, answer):
        logger.debug("Engraphis: exchange not worth remembering, skipped")
        return
    content = distill_exchange(question, answer)
    try:
        from api import engraphis_integration
        if not engraphis_integration.is_available():
            return
        workspace = engraphis_integration.workspace_for_version(
            owner, repo, wiki_version
        )
        engraphis_integration.ensure_workspace(
            workspace,
            f"Knowledge about {owner}/{repo} (wiki release v{wiki_version}).",
        )
        engraphis_integration.remember_linked(
            workspace, content,
            mtype="episodic", source=source,
            title=_collapse(question)[:120],
            metadata={"kind": "chat_exchange", "owner": owner, "repo": repo,
                      "wiki_version": wiki_version, "source": source},
            # One chain per surface: the chat's turns and the code editor's
            # turns are each a timeline, and mixing them would claim an edit
            # "follows" an unrelated chat question.
            chain_key=f"exchange_{source}",
            relate_query=_collapse(question),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Engraphis chat auto-capture skipped: %s", e)


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()
