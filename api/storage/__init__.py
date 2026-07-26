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

import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
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


def _legacy_repo_key(
    owner: Optional[str],
    repo: Optional[str],
    repo_type: Optional[str],
) -> str:
    parts = [p for p in (owner or "", repo or "") if p]
    base = "_".join(parts) if parts else (repo_type or "local")
    base = base.rstrip("/").replace(".git", "")
    base = _REPO_KEY_RE.sub("_", base)
    return base or "local"


def repo_key(owner: Optional[str], repo: Optional[str], repo_type: Optional[str]) -> str:
    """Filesystem-safe, collision-resistant key for one repository.

    The old ``owner_repo`` flattening collided for values such as
    ``("a_b", "c")`` and ``("a", "b_c")``. Keep a readable prefix while
    hashing the unambiguous tuple used by storage and search.
    """
    readable = _legacy_repo_key(owner, repo, repo_type)[:80]
    identity = json.dumps(
        [repo_type or "", owner or "", repo or ""],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{readable}-{digest}"


def repo_db_path(owner: Optional[str], repo: Optional[str], repo_type: Optional[str]) -> str:
    root = _db_dir()
    current = os.path.join(root, f"{repo_key(owner, repo, repo_type)}.db")
    legacy = os.path.join(root, f"{_legacy_repo_key(owner, repo, repo_type)}.db")
    # Upgrades continue using an existing legacy DB. New repositories always
    # get the collision-resistant name; an explicit migration tool can rename
    # legacy files later without risking open WAL sidecars.
    if os.path.exists(legacy) and not os.path.exists(current):
        return legacy
    return current


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
    available_at  TEXT,
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

CREATE TABLE IF NOT EXISTS mcp_servers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    transport     TEXT NOT NULL DEFAULT 'stdio',
    config_json   TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
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


_PROFILE_SCHEMA_VERSION = 4
_REPO_SCHEMA_VERSION = 1


def _has_user_tables(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone()
    return row is not None


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(f"PRAGMA user_version={int(version)}")


def _execute_schema(conn: sqlite3.Connection, script: str) -> None:
    """Execute this repository's simple DDL script inside the caller's txn."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                conn.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("Incomplete SQL schema statement")


def _apply_profile_migration(conn: sqlite3.Connection, version: int) -> None:
    if version == 1:
        _execute_schema(conn, _PROFILE_SCHEMA)
    elif version == 2:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        for name, sql_type in (
            ("result_json", "TEXT"),
            ("error", "TEXT"),
            ("created_at", "TEXT"),
            ("started_at", "TEXT"),
            ("finished_at", "TEXT"),
        ):
            if name not in columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {sql_type}")
        if "attempts" not in columns:
            conn.execute(
                "ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
            )
        conn.execute(
            "UPDATE jobs SET created_at=COALESCE(created_at, datetime('now'))"
        )
    elif version == 3:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mcp_servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                transport TEXT NOT NULL DEFAULT 'stdio',
                config_json TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
    elif version == 4:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "available_at" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN available_at TEXT")
        conn.execute(
            "UPDATE jobs SET available_at=COALESCE(available_at, created_at, datetime('now'))"
        )
    else:  # pragma: no cover - programming error guarded by target constant
        raise RuntimeError(f"Unknown profile DB migration {version}")


def _apply_repo_migration(conn: sqlite3.Connection, version: int) -> None:
    if version == 1:
        _execute_schema(conn, _REPO_SCHEMA)
    else:  # pragma: no cover
        raise RuntimeError(f"Unknown repository DB migration {version}")


def _migration_backup(path: str, current: int, target: int) -> str | None:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = os.path.join(os.path.dirname(path), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    destination = os.path.join(
        backup_dir,
        f"{os.path.basename(path)}.schema-{current}-to-{target}.{timestamp}.bak",
    )
    backup_database(path, destination)
    return destination


def _init_schema(conn: sqlite3.Connection, path: str) -> None:
    """Apply ordered, versioned migrations for the selected DB family."""
    is_profile = os.path.basename(path) == "profile.db"
    target = _PROFILE_SCHEMA_VERSION if is_profile else _REPO_SCHEMA_VERSION
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current > target:
        raise RuntimeError(
            f"Database schema {current} is newer than supported schema {target}"
        )
    if current < target and _has_user_tables(conn):
        # Close no connections and mutate no state through this helper; SQLite's
        # online backup API produces a consistent snapshot even with WAL.
        _migration_backup(path, current, target)

    for version in range(current + 1, target + 1):
        try:
            conn.execute("BEGIN IMMEDIATE")
            if is_profile:
                _apply_profile_migration(conn, version)
            else:
                _apply_repo_migration(conn, version)
            _set_user_version(conn, version)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # CREATE IF NOT EXISTS remains a cheap integrity net for installations
    # created by an intermediate development build with an incorrect version.
    conn.executescript(_PROFILE_SCHEMA if is_profile else _REPO_SCHEMA)
    if is_profile:
        bootstrap_default_account(conn)
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


def database_integrity(path: str) -> dict[str, Any]:
    """Run SQLite integrity_check without initializing or mutating the DB."""
    if not os.path.isfile(path):
        return {"ok": False, "messages": ["database file does not exist"]}
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    try:
        messages = [
            str(row[0])
            for row in conn.execute("PRAGMA integrity_check").fetchall()
        ]
    finally:
        conn.close()
    return {"ok": messages == ["ok"], "messages": messages}


def backup_database(path: str, destination: str | None = None) -> str:
    """Create a consistent SQLite backup and verify it before returning."""
    source_path = os.path.abspath(path)
    if not os.path.isfile(source_path):
        raise FileNotFoundError(source_path)
    if destination is None:
        backup_dir = os.path.join(os.path.dirname(source_path), "backups")
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = os.path.join(
            backup_dir,
            f"{os.path.basename(source_path)}.{timestamp}.bak",
        )
    destination = os.path.abspath(destination)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    if source_path == destination:
        raise ValueError("Backup destination must differ from source")

    source = sqlite3.connect(f"file:{Path(source_path).resolve()}?mode=ro", uri=True)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    integrity = database_integrity(destination)
    if not integrity["ok"]:
        raise RuntimeError(
            f"Backup integrity check failed: {integrity['messages']}"
        )
    return destination


def backup_all_databases(destination_dir: str) -> list[str]:
    """Back up every HackDeepWiki DB into one operator-selected directory."""
    os.makedirs(destination_dir, exist_ok=True)
    backed_up: list[str] = []
    for name in sorted(os.listdir(_db_dir())):
        if not name.endswith(".db"):
            continue
        source = os.path.join(_db_dir(), name)
        backed_up.append(
            backup_database(source, os.path.join(destination_dir, f"{name}.bak"))
        )
    return backed_up


def restore_database(backup_path: str, destination: str) -> str:
    """Verify a backup then atomically replace the destination DB.

    Callers must ensure no live connection is using ``destination``. The
    function itself stages a second SQLite backup in the destination
    directory, fsyncs it through SQLite commit and swaps it atomically.
    """
    integrity = database_integrity(backup_path)
    if not integrity["ok"]:
        raise ValueError(f"Refusing corrupt backup: {integrity['messages']}")
    destination = os.path.abspath(destination)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    fd, staged = tempfile.mkstemp(
        prefix=f".{os.path.basename(destination)}.",
        suffix=".restore",
        dir=os.path.dirname(destination),
    )
    os.close(fd)
    try:
        os.unlink(staged)
        backup_database(backup_path, staged)
        os.replace(staged, destination)
        for suffix in ("-wal", "-shm"):
            sidecar = destination + suffix
            if os.path.exists(sidecar):
                os.unlink(sidecar)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)
    return destination
