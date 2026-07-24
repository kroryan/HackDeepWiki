"""Incremental re-indexing (Fase 3).

Today a wiki "refresh" re-clones and re-embeds the WHOLE repo every time --
data_pipeline even calls out that there's no per-file freshness check ("no
mtime/hash on the .pkl so a cache hit is otherwise trusted blindly"). For a
large repo that's minutes of work + API spend on every refresh, when usually
only a handful of files changed.

This module computes the diff: walk the repo's tracked files, hash each,
compare against the stored hashes (Fase 0 ``file_hashes``), and return the
added/changed/removed sets. A future ``wiki_incremental`` job handler feeds
only the changed files back through the splitter+embedder instead of the
whole repo, then updates the stored hashes.

Portable: reuses api.storage.file_hashes + stdlib only.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from api.data_root import get_data_root
from api.storage.file_hashes import sha256_of_file, changed_files, upsert_hash, reset

logger = logging.getLogger(__name__)

# Walk only the file types the indexer would embed -- mirroring
# data_pipeline.read_all_documents's extensions so the diff matches what a
# refresh would actually touch.
_CODE_EXTS = (".py", ".js", ".ts", ".java", ".cpp", ".c", ".h", ".hpp", ".go", ".rs",
              ".rb", ".php", ".swift", ".kt", ".scala", ".cs", ".lua", ".sh", ".bash",
              ".jsx", ".tsx", ".mjs", ".vue", ".svelte")
_DOC_EXTS = (".md", ".txt", ".rst", ".json", ".yaml", ".yml")
_TRACKED_EXTS = set(_CODE_EXTS + _DOC_EXTS)

# Cheap exclude set for the walk (full exclusion rules live in
# data_pipeline.should_process_file; here we only skip the heavy obvious
# dirs so the diff doesn't descend into node_modules/.git).
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env",
              "dist", "build", ".next", "target", ".adalflow", "DATABASE"}


def _clone_dir(owner: Optional[str], repo: str, repo_type: Optional[str]) -> Optional[str]:
    """Resolve the local clone dir for a repo. Mirrors
    data_pipeline._local_clone_dir's naming (repos/{owner}_{repo}) but takes
    owner/repo/repo_type directly (the job payload shape) instead of a URL."""
    root = get_data_root()
    repo_name = f"{owner}_{repo}" if owner else repo
    d = os.path.join(root, "repos", repo_name)
    return d if os.path.isdir(d) else None


def compute_diff(owner: Optional[str], repo: str, repo_type: Optional[str]) -> dict:
    """Walk the repo's tracked files and diff them against the stored hashes.

    Returns ``{added_or_changed: [...], unchanged: N, removed: [...], total: N}``.

    - ``added_or_changed``: files new on disk or whose hash differs from
      what's stored -- these are what a selective re-embed needs to process.
    - ``removed``: files that were indexed before but are gone from disk --
      a caller should drop their chunks from the index.
    - ``unchanged``/``total``: bookkeeping for a "X of Y files unchanged"
      status line.

    Returns ``{total: 0}`` if the repo isn't cloned locally (the caller
    should fall back to a full re-clone + index)."""
    clone = _clone_dir(owner, repo, repo_type)
    if not clone:
        return {"added_or_changed": [], "unchanged": 0, "removed": [], "total": 0,
                "note": "repo not cloned locally; full re-clone required"}

    # Current on-disk state: (rel_path, sha256, size) for each tracked file.
    current: list[tuple[str, str, Optional[int]]] = []
    for dirpath, dirnames, filenames in os.walk(clone):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in _TRACKED_EXTS:
                continue
            abspath = os.path.join(dirpath, fn)
            relpath = os.path.relpath(abspath, clone)
            sha = sha256_of_file(abspath)
            if sha is None:
                continue
            try:
                size = os.path.getsize(abspath)
            except OSError:
                size = None
            current.append((relpath, sha, size))

    current_paths = {p for p, _, _ in current}
    changed = changed_files(owner, repo, repo_type, current)

    # Removed = previously-hashed files not present on disk anymore.
    from api.storage import connect, repo_db_path
    with connect(repo_db_path(owner, repo, repo_type)) as conn:
        try:
            rows = conn.execute("SELECT file_path FROM file_hashes").fetchall()
        except Exception:
            rows = []
    stored_paths = {r["file_path"] for r in rows}
    removed = sorted(stored_paths - current_paths)

    return {
        "added_or_changed": sorted(changed),
        "unchanged": len(current_paths) - len(changed),
        "removed": removed,
        "total": len(current_paths),
    }


def apply_diff(owner: Optional[str], repo: str, repo_type: Optional[str],
               processed: list[str]) -> int:
    """After a selective re-embed of ``processed`` files, record their new
    hashes so the NEXT diff sees them as unchanged. Returns the count stored."""
    clone = _clone_dir(owner, repo, repo_type)
    if not clone:
        return 0
    n = 0
    for relpath in processed:
        abspath = os.path.join(clone, relpath)
        sha = sha256_of_file(abspath)
        if sha is None:
            continue
        try:
            size = os.path.getsize(abspath)
        except OSError:
            size = None
        upsert_hash(owner, repo, repo_type, relpath, sha, size)
        n += 1
    return n


def reset_hashes(owner: Optional[str], repo: str, repo_type: Optional[str]) -> None:
    """Drop all stored hashes for a repo -- a forced full refresh starts
    from a clean slate (everything looks 'changed')."""
    reset(owner, repo, repo_type)
