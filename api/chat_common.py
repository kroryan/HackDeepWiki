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