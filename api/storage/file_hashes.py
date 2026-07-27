"""Per-file content hashes for incremental re-indexing (Fase 0 storage /
Fase 3 incremental updates).

The current pipeline re-embeds the whole repo on every "generate wiki"
because the only cache signal is the .pkl's existence -- there's no per-file
freshness check (data_pipeline even calls this out: "there's no mtime/hash
on the .pkl so a cache hit is otherwise trusted blindly"). Storing a SHA-256
per indexed file lets a re-run skip unchanged files and only re-embed the
diff, which is the difference between a 3-minute and a 3-second refresh on a
large repo.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Iterable, Optional

from api.storage import connect, repo_db_path

logger = logging.getLogger(__name__)


def _db(owner: Optional[str], repo: Optional[str], repo_type: Optional[str]):
    return connect(repo_db_path(owner, repo, repo_type))


def sha256_of_file(path: str) -> Optional[str]:
    """Streaming SHA-256 of a file's bytes, or None if unreadable."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:  # noqa: BLE001 - a single unreadable file must not abort indexing
        logger.warning(f"could not hash {path}: {e}")
        return None


def upsert_hash(owner: Optional[str], repo: Optional[str], repo_type: Optional[str],
                file_path: str, sha256: str, size_bytes: Optional[int] = None) -> None:
    with _db(owner, repo, repo_type) as conn:
        conn.execute(
            "INSERT INTO file_hashes (file_path, sha256, size_bytes) VALUES (?, ?, ?) "
            "ON CONFLICT(file_path) DO UPDATE SET "
            "sha256=excluded.sha256, size_bytes=excluded.size_bytes, "
            "indexed_at=datetime('now')",
            (file_path, sha256, size_bytes),
        )
        conn.commit()


def changed_files(owner: Optional[str], repo: Optional[str], repo_type: Optional[str],
                  candidates: Iterable[tuple[str, str, Optional[int]]]) -> list[str]:
    """Given (file_path, current_sha256, size_bytes) tuples, return the subset
    whose stored hash differs (or that aren't stored yet). These are the files
    a re-index needs to actually re-embed; everything else can be reused."""
    cand = list(candidates)
    if not cand:
        return []
    with _db(owner, repo, repo_type) as conn:
        placeholders = ",".join("?" * len(cand))
        rows = conn.execute(
            f"SELECT file_path, sha256 FROM file_hashes WHERE file_path IN ({placeholders})",
            [c[0] for c in cand],
        ).fetchall()
    stored = {r["file_path"]: r["sha256"] for r in rows}
    return [path for path, sha, _size in cand if stored.get(path) != sha]


def reset(owner: Optional[str], repo: Optional[str], repo_type: Optional[str]) -> None:
    """Drop all stored hashes for a repo (used by a forced full refresh)."""
    with _db(owner, repo, repo_type) as conn:
        conn.execute("DELETE FROM file_hashes")
        conn.commit()
