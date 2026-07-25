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

import hashlib
import hmac
import os
import secrets as _secrets

_ENC_KEY_ENV = "HACKDEEPWIKI_ENC_KEY"
_PBKDF2_ITERATIONS = 200_000
_SALT_LEN = 16
# AES-GCM: 12-byte nonce is the standard, 16-byte tag appended by the library.
_NONCE_LEN = 12
# Marker so decrypt can tell an encrypted blob from a plaintext legacy value
# (a profile saved before encryption was enabled, or with encryption off).
_ENC_PREFIX = "enc:v1:"


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
    return bool(
        value
        and WIKI_AUTH_CODE
        and hmac.compare_digest(WIKI_AUTH_CODE, value)
    )


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
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import base64
    salt = _secrets.token_bytes(_SALT_LEN)
    nonce = _secrets.token_bytes(_NONCE_LEN)
    key = _derive_key(os.environ[_ENC_KEY_ENV], salt)
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
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import base64
    try:
        body = stored[len(_ENC_PREFIX):]
        salt_b64, nonce_b64, ct_b64 = body.split(":")
        salt = base64.b64decode(salt_b64)
        nonce = base64.b64decode(nonce_b64)
        ct = base64.b64decode(ct_b64)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Malformed encrypted secret: {e}")
    key = _derive_key(os.environ[_ENC_KEY_ENV], salt)
    try:
        pt = AESGCM(key).decrypt(nonce, ct, associated_data=None)
    except Exception as e:  # noqa: BLE001 - InvalidTag (wrong key) or any crypto error
        raise ValueError(f"Could not decrypt secret (wrong HACKDEEPWIKI_ENC_KEY?): {e}")
    return pt.decode("utf-8")
