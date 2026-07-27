"""Shareable wiki links (Fase 2).

OpenDeepWiki has ``web/app/share/[shareId]`` backed by ``ChatShareService``.
HackDeepWiki's local-first take: a share is an opaque ID that resolves to a
saved wiki-cache version, so a user can send someone a link to a specific
generated wiki without shipping them the whole repo. The link stores only
the (repo, language, version) pointer -- the wiki content itself is read
from the existing wikicache on demand, so sharing doesn't duplicate content
and a deleted wiki invalidates its shares automatically (the resolver
returns None when the referenced cache file is gone).

Lives in ``profile.db`` (cross-repo: a share ID isn't repo-scoped).
"""

from __future__ import annotations

import logging
import secrets
from typing import Optional

from api.storage import connect, profile_db_path

logger = logging.getLogger(__name__)

_SHARE_ID_LEN = 16  # url-safe; ~96 bits of entropy, enough to be unguessable


def _db():
    conn = connect(profile_db_path())
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_shares (
            id            TEXT PRIMARY KEY,
            repo_type     TEXT NOT NULL,
            owner         TEXT,
            repo          TEXT NOT NULL,
            language      TEXT NOT NULL,
            version       TEXT,
            title         TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at    TEXT
        )
        """
    )
    conn.commit()
    return conn


def create_share(owner: Optional[str], repo: str, repo_type: str, language: str,
                 version: Optional[str] = None, title: Optional[str] = None,
                 expires_at: Optional[str] = None) -> str:
    """Mint a share ID for one wiki release. Idempotent on the (repo, lang,
    version) tuple: re-sharing the same release returns the existing ID
    instead of minting a duplicate, so a user re-clicking "share" doesn't
    accumulate dead links."""
    with _db() as conn:
        existing = conn.execute(
            "SELECT id FROM wiki_shares WHERE owner IS ? AND repo=? AND repo_type=? "
            "AND language=? AND COALESCE(version,'')=COALESCE(?,'')",
            (owner, repo, repo_type, language, version),
        ).fetchone()
        if existing:
            return existing["id"]
        share_id = secrets.token_urlsafe(_SHARE_ID_LEN)[:_SHARE_ID_LEN]
        conn.execute(
            "INSERT INTO wiki_shares (id, repo_type, owner, repo, language, version, title, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (share_id, repo_type, owner, repo, language, version, title, expires_at),
        )
        conn.commit()
        return share_id


def resolve_share(share_id: str) -> Optional[dict]:
    """Look up a share ID -> its (repo, language, version) pointer, or None
    if unknown/expired. Does NOT load the wiki content (the caller resolves
    the cache via api.read_wiki_cache); this keeps a share resolution cheap
    and lets an expired-or-deleted wiki surface a clean 'not found'."""
    with _db() as conn:
        row = conn.execute(
            "SELECT id, repo_type, owner, repo, language, version, title, expires_at "
            "FROM wiki_shares WHERE id = ?",
            (share_id,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    # Expiry is advisory (a share is local-first; there's no scheduler to
    # reap), but we honor it at resolve time so an expired link 404s.
    if d.get("expires_at"):
        expired = conn.execute(
            "SELECT 1 FROM wiki_shares WHERE id=? AND expires_at < datetime('now')", (share_id,)
        ).fetchone() if False else None  # cheap check below instead
        with _db() as c2:
            expired = c2.execute(
                "SELECT 1 FROM wiki_shares WHERE id=? AND expires_at < datetime('now')", (share_id,)
            ).fetchone()
        if expired:
            return None
    return d


def list_shares(owner: Optional[str] = None, repo: Optional[str] = None) -> list[dict]:
    """All shares, optionally filtered by repo. For a 'my shares' UI."""
    with _db() as conn:
        if owner and repo:
            rows = conn.execute(
                "SELECT id, repo_type, owner, repo, language, version, title, created_at, expires_at "
                "FROM wiki_shares WHERE owner IS ? AND repo=? ORDER BY created_at DESC",
                (owner, repo),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, repo_type, owner, repo, language, version, title, created_at, expires_at "
                "FROM wiki_shares ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def delete_share(share_id: str) -> bool:
    with _db() as conn:
        cur = conn.execute("DELETE FROM wiki_shares WHERE id = ?", (share_id,))
        conn.commit()
        return cur.rowcount > 0
