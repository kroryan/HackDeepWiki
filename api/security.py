"""Security helpers shared across the HackDeepWiki backend.

This module is the home for cross-cutting security primitives so they aren't
duplicated per-transport or per-route. Today it provides error-message
sanitization (redacting API keys, bearer tokens, and absolute filesystem
paths before an exception string is sent to the browser). Future credential
encryption (AES-at-rest for provider profiles, Fase 4.1) will live here too.

Guiding principle: exception text is for the server log (full detail) and the
*client* gets a redacted, length-bounded version. Raw `str(e)` can carry
absolute paths, and occasionally fragments of credentials (e.g. an auth
error echoing a key prefix), which have no business in a websocket frame or
HTTPException detail.
"""

from __future__ import annotations

import re

# Patterns that must never reach a client. Keys first (OpenAI sk-..., Anthropic
# sk-ant-..., generic long bearer tokens, hex runs >= 32 chars), then absolute
# filesystem paths (POSIX and Windows).
_KEY_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.=]{16,}"),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),  # long hex (raw keys/hashes)
]
_PATH_PATTERNS = [
    re.compile(r"(?<![\w.-])/(?:home|app|root|usr|var|tmp|opt|mnt|etc|Users)/[^\s'\"<>)]+"),
    re.compile(r"(?<![\w.-])[A-Za-z]:\\[^\s'\"<>)]+"),
]

_MAX_CLIENT_ERROR_LEN = 300


def sanitize_error_message(message: str) -> str:
    """Redact secrets and absolute paths from an exception/message string
    before it is sent to a client, and bound its length.

    The full, unredacted message should still be written to the server log
    (callers already do `logger.error(...)` with the original). This function
    only controls what crosses the wire to the browser.
    """
    if not message:
        return ""
    redacted = message
    for pat in _KEY_PATTERNS:
        redacted = pat.sub("[REDACTED]", redacted)
    for pat in _PATH_PATTERNS:
        redacted = pat.sub("<path>", redacted)
    if len(redacted) > _MAX_CLIENT_ERROR_LEN:
        redacted = redacted[: _MAX_CLIENT_ERROR_LEN - 3] + "..."
    return redacted