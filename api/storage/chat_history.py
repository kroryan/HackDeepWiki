"""Per-repo chat history + sessions, backed by the Fase 0 SQLite layer.

This is the durable backing store the frontend's localStorage sessions (see
Ask.tsx's createSession/loadSessions) currently have NO server-side mirror
for -- close the browser, clear storage, switch machines, and the whole
conversation is gone. Fase 0/6 give it a server home in ``<repo_key>.db``.

Wiring it into Ask.tsx (fetch/save round-trips) is Fase 6's job; this module
is the storage API itself, unit-tested in isolation here.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from api.storage import connect, repo_db_path

logger = logging.getLogger(__name__)


def _db(owner: Optional[str], repo: Optional[str], repo_type: Optional[str]):
    return connect(repo_db_path(owner, repo, repo_type))


def create_session(owner: Optional[str], repo: Optional[str], repo_type: Optional[str],
                   session_id: str, title: Optional[str] = None) -> None:
    with _db(owner, repo, repo_type) as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, title) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, "
            "updated_at=datetime('now')",
            (session_id, title),
        )
        conn.commit()


def list_sessions(owner: Optional[str], repo: Optional[str], repo_type: Optional[str]) -> list[dict]:
    with _db(owner, repo, repo_type) as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM chat_sessions "
            "ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def delete_session(owner: Optional[str], repo: Optional[str], repo_type: Optional[str],
                   session_id: str) -> None:
    with _db(owner, repo, repo_type) as conn:
        conn.execute("DELETE FROM chat_history WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        conn.commit()


def append_message(owner: Optional[str], repo: Optional[str], repo_type: Optional[str],
                   session_id: str, role: str, content: str,
                   provider: Optional[str] = None, model: Optional[str] = None) -> int:
    """Append one message to a session's history. Returns the new row id.
    Touches the session's updated_at so list_sessions ordering reflects
    activity. The session row is created on demand if it doesn't exist yet
    (the frontend sometimes appends before explicitly creating)."""
    with _db(owner, repo, repo_type) as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, title) VALUES (?, NULL) "
            "ON CONFLICT(id) DO UPDATE SET updated_at=datetime('now')",
            (session_id,),
        )
        cur = conn.execute(
            "INSERT INTO chat_history (session_id, role, content, provider, model) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, provider, model),
        )
        conn.execute(
            "UPDATE chat_sessions SET updated_at=datetime('now') WHERE id = ?",
            (session_id,),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_history(owner: Optional[str], repo: Optional[str], repo_type: Optional[str],
                session_id: str, limit: int = 200) -> list[dict]:
    with _db(owner, repo, repo_type) as conn:
        rows = conn.execute(
            "SELECT role, content, provider, model, created_at FROM chat_history "
            "WHERE session_id = ? ORDER BY id ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def persist_session_json(owner: Optional[str], repo: Optional[str], repo_type: Optional[str],
                         session_id: str, title: str, messages: list[dict[str, Any]]) -> None:
    """Bulk-save a whole session as imported from the frontend's localStorage
    shape ({role, content}[]). Used by Fase 6's "sync local sessions to
    server" step. Replaces the session's history atomically (delete + insert
    in one transaction) so a partial write can't leave a duplicated/gapped
    transcript."""
    with _db(owner, repo, repo_type) as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, title) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, "
            "updated_at=datetime('now')",
            (session_id, title),
        )
        conn.execute("DELETE FROM chat_history WHERE session_id = ?", (session_id,))
        conn.executemany(
            "INSERT INTO chat_history (session_id, role, content) VALUES (?, ?, ?)",
            [(session_id, m.get("role", "user"), m.get("content", "")) for m in messages],
        )
        conn.commit()
