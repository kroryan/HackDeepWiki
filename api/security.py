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

import hashlib
import hmac
import os
import re
import secrets as _secrets
import time
from base64 import urlsafe_b64encode
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlparse

if TYPE_CHECKING:
    from fastapi import Request, WebSocket

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


# --- Credential encryption at rest (Fase 4.1) ------------------------------
# Provider API keys stored in profile.db (api.storage.provider_profiles) are
# encrypted with AES-256-GCM when HACKDEEPWIKI_ENC_KEY is set. The key is
# derived (PBKDF2-HMAC-SHA256, 200k iterations) from the env value, so the
# plaintext key never sits in memory as the raw string the user typed.
#
# Local-first default: if no HACKDEEPWIKI_ENC_KEY is configured, encryption is
# DISABLED and secrets are stored as given (the same trust model as the
# existing .env-based key handling -- the local machine is the trust
# boundary). This mirrors how WIKI_AUTH_MODE / HACKDEEPWIKI_MCP_TOKEN default
# off: encryption is opt-in, never a hard requirement that breaks the
# zero-config first run.
#
# `cryptography` is already bundled in the AppImage (pulled by azure-identity
# / google-auth / msal), so this adds no new dependency -- verified against
# AppDir/usr/bin/_internal/cryptography.

_ENC_KEY_ENV = "HACKDEEPWIKI_ENC_KEY"
_PBKDF2_ITERATIONS = 200_000
_SALT_LEN = 16
# AES-GCM: 12-byte nonce is the standard, 16-byte tag appended by the library.
_NONCE_LEN = 12
# Marker so decrypt can tell an encrypted blob from a plaintext legacy value
# (a profile saved before encryption was enabled, or with encryption off).
_ENC_PREFIX = "enc:v1:"
AUTH_HEADER = "X-HackDeepWiki-Authorization"
AUTH_COOKIE = "hackdeepwiki_session"
INTERNAL_PROXY_HEADER = "X-HackDeepWiki-Internal-Proxy"
_SESSION_PREFIX = "hdw1"

# All other HTTP endpoints are protected when HACKDEEPWIKI_AUTH_MODE is on.
# Liveness must remain callable by container supervisors, while validation is
# the only endpoint that accepts the shared code itself.
PUBLIC_HTTP_PATHS = frozenset(
    {
        "/health",
        "/health/live",
        "/auth/status",
        "/auth/validate",
    }
)


def authorization_is_valid(value: str | None) -> bool:
    """Validate the shared UI authorization code in constant time.

    Importing the configuration lazily avoids a cycle: ``api.config`` imports
    model clients which themselves use helpers from this module.  Keeping the
    primitive here lets HTTP routes and WebSockets enforce exactly the same
    policy instead of gradually drifting into separate security boundaries.
    """
    from api.config import WIKI_AUTH_CODE, WIKI_AUTH_MODE

    if not WIKI_AUTH_MODE:
        return True
    if not value or not WIKI_AUTH_CODE:
        return False
    if hmac.compare_digest(WIKI_AUTH_CODE, value):
        return True
    return _authorization_session_is_valid(value, WIKI_AUTH_CODE)


def _session_signature(payload: str, auth_code: str) -> str:
    digest = hmac.new(
        auth_code.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def create_authorization_session_token(
    *,
    now: int | None = None,
    lifetime_seconds: int | None = None,
) -> str:
    """Create a short-lived bearer token without exposing the shared code.

    Browser code stores this token in ``sessionStorage`` for direct WebSocket
    upgrades, while the Next.js auth proxy also places it in an HttpOnly
    cookie.  Restarting/changing the configured auth code invalidates every
    token because the code is the HMAC key.
    """
    from api.config import WIKI_AUTH_CODE, WIKI_AUTH_MODE
    from api.settings import get_settings

    if not WIKI_AUTH_MODE or not WIKI_AUTH_CODE:
        return ""
    issued = int(time.time() if now is None else now)
    lifetime = (
        get_settings().auth_session_seconds
        if lifetime_seconds is None
        else int(lifetime_seconds)
    )
    expires = issued + lifetime
    nonce = _secrets.token_urlsafe(12)
    payload = f"{_SESSION_PREFIX}.{expires}.{nonce}"
    return f"{payload}.{_session_signature(payload, WIKI_AUTH_CODE)}"


def _authorization_session_is_valid(
    value: str,
    auth_code: str,
    *,
    now: int | None = None,
) -> bool:
    parts = value.split(".")
    if len(parts) != 4 or parts[0] != _SESSION_PREFIX:
        return False
    try:
        expires = int(parts[1])
    except ValueError:
        return False
    current = int(time.time() if now is None else now)
    if expires <= current:
        return False
    payload = ".".join(parts[:3])
    expected = _session_signature(payload, auth_code)
    return hmac.compare_digest(expected, parts[3])


def request_authorization_value(request: "Request") -> str | None:
    """Extract the supported browser/API credentials from one HTTP request."""
    return (
        request.headers.get(AUTH_HEADER)
        or request.query_params.get("authorization_code")
        or request.cookies.get(AUTH_COOKIE)
    )


def internal_proxy_is_valid(value: str | None) -> bool:
    """Authenticate the bundled Next.js server to the loopback FastAPI API."""
    expected = os.environ.get("HACKDEEPWIKI_INTERNAL_PROXY_TOKEN", "")
    return bool(value and expected and hmac.compare_digest(value, expected))


def origin_is_allowed(origin: str | None, request_host: str | None) -> bool:
    """Shared HTTP/WebSocket browser-origin policy."""
    if not origin:
        return True
    configured = {
        item.strip().rstrip("/")
        for item in os.environ.get("HACKDEEPWIKI_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    }
    if origin.rstrip("/") in configured:
        return True
    origin_host = (urlparse(origin).hostname or "").lower()
    host = (urlparse(f"//{request_host or ''}").hostname or "").lower()
    loopback = {"localhost", "127.0.0.1", "::1"}
    return bool(
        origin_host
        and host
        and (origin_host == host or {origin_host, host} <= loopback)
    )


async def authorization_middleware(request: "Request", call_next):
    """Fail-closed authentication/origin boundary for the entire HTTP API."""
    from fastapi.responses import JSONResponse

    from api.config import WIKI_AUTH_MODE

    if request.method == "OPTIONS" or request.url.path in PUBLIC_HTTP_PATHS:
        return await call_next(request)

    origin = request.headers.get("origin")
    if not origin_is_allowed(origin, request.headers.get("host")):
        return JSONResponse(
            status_code=403,
            content={"detail": "Request origin is not allowed"},
        )

    if not WIKI_AUTH_MODE:
        return await call_next(request)

    proxy_value = request.headers.get(INTERNAL_PROXY_HEADER)
    if internal_proxy_is_valid(proxy_value):
        return await call_next(request)

    if not authorization_is_valid(request_authorization_value(request)):
        return JSONResponse(
            status_code=401,
            content={"detail": "Authorization code is invalid"},
        )
    return await call_next(request)


async def authorize_websocket(
    websocket: "WebSocket",
    *,
    validator: Callable[[str | None], bool] = authorization_is_valid,
) -> bool:
    """Apply the same auth/origin policy before any WebSocket ``accept``."""
    code = (
        websocket.query_params.get("authorization_code")
        or websocket.headers.get(AUTH_HEADER)
        or getattr(websocket, "cookies", {}).get(AUTH_COOKIE)
    )
    proxy_value = websocket.headers.get(INTERNAL_PROXY_HEADER)
    allowed = origin_is_allowed(
        websocket.headers.get("origin"),
        websocket.headers.get("host"),
    )
    if allowed and (
        internal_proxy_is_valid(proxy_value) or validator(code)
    ):
        return True
    await websocket.close(code=1008)
    return False


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, _PBKDF2_ITERATIONS, dklen=32)


def encryption_enabled() -> bool:
    """True when a master passphrase is configured (secrets will be encrypted
    at rest). False in the zero-config local-first default."""
    return bool(os.environ.get(_ENC_KEY_ENV))


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret for storage. Returns ``enc:v1:<b64 salt>:<b64 nonce>:
    <b64 ciphertext+tag>``. When encryption is disabled (no
    HACKDEEPWIKI_ENC_KEY), returns the plaintext unchanged so the storage
    layer works identically in the local-first default."""
    if not plaintext:
        return plaintext
    if not encryption_enabled():
        return plaintext
    return _encrypt_secret_with_passphrase(
        plaintext,
        os.environ[_ENC_KEY_ENV],
    )


def _encrypt_secret_with_passphrase(
    plaintext: str,
    passphrase: str,
) -> str:
    import base64

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = _secrets.token_bytes(_SALT_LEN)
    nonce = _secrets.token_bytes(_NONCE_LEN)
    key = _derive_key(passphrase, salt)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    return (
        f"{_ENC_PREFIX}"
        f"{base64.b64encode(salt).decode()}:"
        f"{base64.b64encode(nonce).decode()}:"
        f"{base64.b64encode(ct).decode()}"
    )


def decrypt_secret(stored: str) -> str:
    """Inverse of encrypt_secret. If ``stored`` doesn't carry the enc:v1:
    marker (a plaintext legacy value, or encryption was off when it was
    saved), it's returned as-is -- so decrypt never breaks a profile saved
    before encryption was enabled, and toggling encryption on later Just
    Works for new writes while old values stay readable.

    Raises ValueError if the marker is present but the blob is malformed or
    the passphrase is wrong (GCM tag check fails) -- a wrong-passphrase
    decryption is a hard error, not a silent wrong-key return."""
    if not stored or not stored.startswith(_ENC_PREFIX):
        return stored
    if not encryption_enabled():
        # An encrypted blob with no key configured: we can't decrypt. Surface
        # a clear error rather than returning garbage.
        raise ValueError("Encrypted secret present but HACKDEEPWIKI_ENC_KEY is not set")
    return _decrypt_secret_with_passphrase(
        stored,
        os.environ[_ENC_KEY_ENV],
    )


def _decrypt_secret_with_passphrase(
    stored: str,
    passphrase: str,
) -> str:
    import base64

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        body = stored[len(_ENC_PREFIX):]
        salt_b64, nonce_b64, ct_b64 = body.split(":")
        salt = base64.b64decode(salt_b64)
        nonce = base64.b64decode(nonce_b64)
        ct = base64.b64decode(ct_b64)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Malformed encrypted secret: {e}")
    key = _derive_key(passphrase, salt)
    try:
        pt = AESGCM(key).decrypt(nonce, ct, associated_data=None)
    except Exception as e:  # noqa: BLE001 - InvalidTag (wrong key) or any crypto error
        raise ValueError(f"Could not decrypt secret (wrong HACKDEEPWIKI_ENC_KEY?): {e}")
    return pt.decode("utf-8")


def rotate_secret(
    stored: str,
    *,
    old_passphrase: str,
    new_passphrase: str,
) -> str:
    """Re-encrypt one stored secret without changing process environment."""
    plaintext = (
        _decrypt_secret_with_passphrase(stored, old_passphrase)
        if stored.startswith(_ENC_PREFIX)
        else stored
    )
    if not new_passphrase:
        return plaintext
    return _encrypt_secret_with_passphrase(plaintext, new_passphrase)
