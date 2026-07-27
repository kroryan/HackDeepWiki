"""Provider profiles (Fase 0 storage / Fase 4 provider profiles).

A named, durable profile bundling a provider + its (eventually encrypted) API
key + endpoint, so a user configures a provider once and reuses it across
repos and restarts instead of pasting the key into the UI every time. Lives
in ``profile.db`` (cross-repo) because a provider profile is not repo-scoped.

The api_key column is ``api_key_enc`` and is intended to hold AES-encrypted
ciphertext once Fase 4.1 lands ``api.security.encrypt_secret``. Until then it
stores the key as given -- the local-first, zero-key path has no master key
to encrypt with, matching the existing .env-based key handling.
"""

from __future__ import annotations

import logging
from typing import Optional

from api.security import decrypt_secret, encrypt_secret
from api.storage import connect, profile_db_path

logger = logging.getLogger(__name__)


def _db():
    return connect(profile_db_path())


def upsert(name: str, provider: str, api_key: Optional[str] = None,
           api_endpoint: Optional[str] = None) -> int:
    """Create or update a profile by name. The api_key is encrypted at rest
    via api.security.encrypt_secret (AES-256-GCM when HACKDEEPWIKI_ENC_KEY is
    set; plaintext passthrough in the zero-config local-first default).
    A None/empty api_key clears the stored value."""
    stored_key = encrypt_secret(api_key) if api_key else None
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO provider_profiles (name, provider, api_key_enc, api_endpoint) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "provider=excluded.provider, api_key_enc=excluded.api_key_enc, "
            "api_endpoint=excluded.api_endpoint, updated_at=datetime('now')",
            (name, provider, stored_key, api_endpoint),
        )
        conn.commit()
        return int(cur.lastrowid)


def get(name: str) -> Optional[dict]:
    """Read a profile, decrypting the api_key on the way out. The returned
    dict carries the PLAINTEXT key under ``api_key`` (and the raw stored value
    under ``api_key_enc`` for diagnostics)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT id, name, provider, api_key_enc, api_endpoint, "
            "created_at, updated_at FROM provider_profiles WHERE name = ?",
            (name,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["api_key"] = decrypt_secret(d["api_key_enc"]) if d["api_key_enc"] else None
    return d


def get_decrypted_key(name: str) -> Optional[str]:
    """Convenience for the streaming path: just the plaintext key for a
    profile, or None. Keeps decryption at the storage boundary so callers
    never handle the enc blob."""
    prof = get(name)
    return prof["api_key"] if prof else None


def list_all() -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT name, provider, api_endpoint, updated_at FROM provider_profiles "
            "ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]


def delete(name: str) -> bool:
    with _db() as conn:
        cur = conn.execute("DELETE FROM provider_profiles WHERE name = ?", (name,))
        conn.commit()
        return cur.rowcount > 0
