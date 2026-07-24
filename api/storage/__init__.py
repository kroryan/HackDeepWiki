"""SQLite-backed persistence layer for HackDeepWiki (Fase 0).

Goals (from the improvement plan):
- Portable: ``sqlite3`` is stdlib and already bundled by PyInstaller -- no
  new runtime dependency, no always-on service (no Postgres/Redis/Turso).
- Per-repo isolation: one ``<repo_key>.db`` per repo + a small ``profile.db``
  for cross-repo state (provider profiles, accounting, jobs). A single
  monolithic DB would let one repo's multi-GB embedding index dominate
  VACUUM/migration time and couple every repo's schema version.
- Self-initializing: every ``connect()`` runs ``CREATE TABLE IF NOT EXISTS``
  so a first run and an upgrade-in-place are both no-ops (no manual migrate
  step). This is the contract [[project_accounting_bootstrap]] relies on.

Layout under ``get_data_root()/hackdeepwiki_db/``:
  profile.db                 -- cross-repo: provider_profiles, accounts,
                                token_accounting, jobs, bookmarks
  <repo_key>.db              -- per-repo: chat_history, file_hashes,
                                embeddings (blob + metadata; FAISS index is
                                materialized at runtime from these rows)

The existing adalflow ``<owner>_<repo>.pkl`` embedding cache is NOT touched
here -- Fase 0 adds a *parallel* durable record so chat history / file
hashes / accounting survive independently, and embeddings persistence (Fase
6/7) will backfill from the pkl into these tables.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from typing import Any, Optional

from api.data_root import get_data_root

logger = logging.getLogger(__name__)

_DB_SUBDIR = "hackdeepwiki_db"

# SQLite is thread-safe per-connection only by default; we open with
# check_same_thread=False and guard writes with a process-wide lock so the
# FastAPI threadpool + background worker can share one connection per DB
# without "SQLite objects created in a thread can only be used in that same
# thread" errors. SQLite serializes writes internally; the lock just keeps
# our own multi-statement transactions atomic against each other.
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _db_dir() -> str:
    path = os.path.join(get_data_root(), _DB_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def _lock_for(path: str) -> threading.RLock:
    """One reentrant lock per DB file path, created lazily."""
    with _LOCKS_GUARD:
        lk = _LOCKS.get(path)
        if lk is None:
            lk = threading.RLock()
            _LOCKS[path] = lk
        return lk


# repo_key must be filesystem-safe across OSes (owner/repo can contain dots,
# but the existing pkl naming already flattens owner_repo, so mirror that).
_REPO_KEY_RE = re.compile(r"[^A-Za-z0-9._-]")


def repo_key(owner: Optional[str], repo: Optional[str], repo_type: Optional[str]) -> str:
    """Filesystem-safe unique key for one repo's DB file. Mirrors the
    ``{owner}_{repo}`` convention adalflow already uses for the .pkl cache
    (see data_pipeline._extract_repo_name_from_url) so a repo's DB and its
    pkl sit next to each other under the same name."""
    parts = [p for p in (owner or "", repo or "") if p]
    base = "_".join(parts) if parts else (repo_type or "local")
    base = base.rstrip("/").replace(".git", "")
    base = _REPO_KEY_RE.sub("_", base)
    return base or "local"


def repo_db_path(owner: Optional[str], repo: Optional[str], repo_type: Optional[str]) -> str:
    return os.path.join(_db_dir(), f"{repo_key(owner, repo, repo_type)}.db")


def profile_db_path() -> str:
    return os.path.join(_db_dir(), "profile.db")


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """WAL + sane busy timeout + foreign keys. WAL lets the worker read while
    a request writes (important for the jobs queue) and survives an unclean
    shutdown better than the default rollback journal."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")


def connect(path: str) -> sqlite3.Connection:
    """Open (and self-initialize schema for) a DB at ``path``. Returns a
    connection usable from any thread. Schema init is idempotent."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    _init_schema(conn, path)
    return conn


# ---- schema ---------------------------------------------------------------

_PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_profiles (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    provider      TEXT NOT NULL,
    -- api_key is stored AES-encrypted at rest (Fase 4.1, api.security) when
    -- HACKDEEPWIKI_ENC_KEY is set; plaintext fallback only for the legacy
    -- zero-key local-first path. See api.security.encrypt_secret.
    api_key_enc   TEXT,
    api_endpoint  TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS accounts (
    -- Single default account for the local-first app (Fase 4/5 accounting).
    -- Created lazily on first connect -- see bootstrap_default_account().
    -- [[project_accounting_bootstrap]]
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT 'default',
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(name)
);

CREATE TABLE IF NOT EXISTS token_accounting (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    provider      TEXT NOT NULL,
    model         TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0.0,
    recorded_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_accounting_account ON token_accounting(account_id);
CREATE INDEX IF NOT EXISTS idx_accounting_recorded ON token_accounting(recorded_at);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'queued',
    payload_json  TEXT,
    result_json   TEXT,
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    started_at    TEXT,
    finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_repo ON jobs(repo_key);

CREATE TABLE IF NOT EXISTS bookmarks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_key      TEXT NOT NULL,
    title         TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bookmarks_repo ON bookmarks(repo_key);
"""

_REPO_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    role          TEXT NOT NULL,
    content       TEXT NOT NULL,
    -- optional: the provider/model that produced an assistant turn, so a
    -- restored session can show "answered by claude-3.5" provenance.
    provider      TEXT,
    model         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_history(session_id, id);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id            TEXT PRIMARY KEY,
    title         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS file_hashes (
    -- incremental-update bookkeeping (Fase 3): the SHA-256 of each file as
    -- last indexed, so a re-run only re-embeds changed files instead of the
    -- whole repo.
    file_path     TEXT PRIMARY KEY,
    sha256        TEXT NOT NULL,
    size_bytes    INTEGER,
    indexed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS embeddings (
    -- durable copy of the chunk index. The FAISS index itself is rebuilt at
    -- runtime from these rows (Fase 6/7 wires the backfill from the legacy
    -- .pkl into here; for now this table exists so writes have a home).
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path     TEXT NOT NULL,
    chunk_order   INTEGER NOT NULL,
    text          TEXT NOT NULL,
    vector        BLOB,
    meta_json     TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_embeddings_file ON embeddings(file_path);

CREATE TABLE IF NOT EXISTS wiki_releases (
    -- thin index of saved wiki-cache versions per language, so the UI can
    -- list releases without scanning filenames (mirrors
    -- api/api._list_repo_cache_files but queryable).
    version       TEXT NOT NULL,
    language      TEXT NOT NULL,
    repo_type     TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (version, language)
);
"""


def _init_schema(conn: sqlite3.Connection, path: str) -> None:
    """Run CREATE TABLE IF NOT EXISTS for whichever schema family this DB
    belongs to (profile vs per-repo). Idempotent."""
    base = os.path.basename(path)
    if base == "profile.db":
        conn.executescript(_PROFILE_SCHEMA)
        bootstrap_default_account(conn)
    else:
        conn.executescript(_REPO_SCHEMA)
    conn.commit()


def bootstrap_default_account(conn: sqlite3.Connection) -> None:
    """Idempotent: ensure the default account row exists. Safe to call on
    every connect of profile.db -- INSERT ... ON CONFLICT DO NOTHING makes a
    fresh install a one-shot create and a restart a no-op. This is the
    contract the accounting layer ([[project_accounting_bootstrap]]) depends
    on: an upgrade from a pre-accounting profile.db auto-creates the account
    the first time the new server starts against it."""
    conn.execute(
        "INSERT INTO accounts (id, name, is_active) VALUES (1, 'default', 1) "
        "ON CONFLICT(id) DO NOTHING"
    )
    conn.commit()
