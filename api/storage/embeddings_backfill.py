"""One-shot migration: backfill the durable ``embeddings`` SQLite table from
adalflow's legacy ``<owner>_<repo>.pkl`` binary cache (Fase 7.2).

Why this exists: the RAG index currently lives ONLY in the opaque ``.pkl``
written by ``adalflow.core.db.LocalDB.save_state``. Lose/delete that file and
the repo must be re-embedded from scratch (cost + API spend + a fragile
``unpickle`` that's also an RCE vector if a crafted file is dropped in). Fase
0 added a durable ``embeddings`` table; this module populates it from an
existing ``.pkl`` so the index becomes reconstructable from an inspectable
source.

Safety contract -- this is a MIGRATION helper, not part of the hot path:
  * It never touches the ``.pkl`` (read-only).
  * It never changes how ``prepare_db_index`` loads vectors -- the chat/RAG
    path still reads the ``.pkl`` exactly as before. The table is a parallel
    durable copy that future code may prefer to read; today it's a backup.
  * Best-effort: a missing/unreadable ``.pkl`` returns 0 and logs, never
    raises, so a UI "backfill" button or a startup hook can't break chat.
  * Idempotent-ish: it wipes the table for this repo first, then inserts,
    so re-running after a re-embed doesn't accumulate duplicates. (The
    caller decides when to re-run; this does NOT auto-track .pkl mtime.)

LocalDB is an adalflow class already bundled in the AppImage (it's how the
.pkl is written in the first place), so this adds no new dependency.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from api.data_root import get_data_root as get_adalflow_default_root_path
from api.storage import embeddings as embeddings_store

logger = logging.getLogger(__name__)


def _pkl_path_for(owner: Optional[str], repo: Optional[str], repo_type: Optional[str]) -> Optional[str]:
    """Derive the legacy .pkl path the same way DatabaseManager / the cache
    deletion helper do, so this stays in sync with where the .pkl is actually
    written. Returns None for repo types that have no adalflow .pkl (zim,
    website/fanwiki are crawled, not embedded) or when owner/repo are missing.

    Mirrors ``api.api._delete_local_repo_clone``'s naming for hosted repos
    (``{owner}_{repo}.pkl``) rather than re-deriving independently -- the
    single source of truth for the filename lives there.

    Checks the current (patched) data root FIRST, then the legacy adalflow
    roots (``~/.adalflow``, ``~/.freedeepwiki/adalflow``) -- a .pkl built
    before the portable-root patch (or under a different writable-root
    resolution) still lives in the legacy spot, and a backfill that can't
    find it is useless. Same spirit as ``migrate_legacy_wikicache``.
    """
    if repo_type in ("zim", "website", "fanwiki"):
        return None
    if not owner or not repo:
        return None
    repo_name = f"{owner}_{repo}"
    home = os.path.expanduser("~")
    candidate_roots = [
        get_adalflow_default_root_path(),  # current patched root (where new .pkl go)
        os.path.join(home, ".adalflow"),               # legacy adalflow default
        os.path.join(home, ".freedeepwiki", "adalflow"),  # pre-rename
    ]
    seen = set()
    for root in candidate_roots:
        if not root or root in seen:
            continue
        seen.add(root)
        candidate = os.path.join(root, "databases", f"{repo_name}.pkl")
        if os.path.isfile(candidate):
            return candidate
    # Nothing found -- return the primary candidate path so the caller's
    # report shows where it looked (and found:false), not None.
    return os.path.join(candidate_roots[0], "databases", f"{repo_name}.pkl")


def _read_fingerprint(pkl_path: str) -> Optional[str]:
    """Read the companion ``.pkl.fingerprint`` written by data_pipeline when
    a fresh index is built (embedder config hash). Stored in meta_json so a
    future load-from-DB path can reject rows built with a different embedder,
    the same way the .pkl load path already does."""
    fp_path = pkl_path + ".fingerprint"
    try:
        with open(fp_path, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _doc_vector(doc) -> Optional[list]:
    v = getattr(doc, "vector", None)
    if v is None:
        return None
    if isinstance(v, list):
        return [float(x) for x in v]
    # numpy / torch arrays -- tolist() is the common denom, fall back to list()
    if hasattr(v, "tolist"):
        return [float(x) for x in v.tolist()]
    try:
        return [float(x) for x in v]
    except TypeError:
        return None


def backfill_from_pkl(owner: Optional[str], repo: Optional[str], repo_type: Optional[str],
                      pkl_path: Optional[str] = None) -> dict:
    """Populate the ``embeddings`` table for this repo from its legacy ``.pkl``.

    If ``pkl_path`` is None it's derived from owner/repo/repo_type. Returns a
    small report dict ``{pkl, found, inserted, skipped, fingerprint}``. Never
    raises -- callers can wire this behind a button / startup hook safely."""
    pkl = pkl_path or _pkl_path_for(owner, repo, repo_type)
    report = {"pkl": pkl, "found": False, "inserted": 0, "skipped": 0, "fingerprint": None}
    if not pkl or not os.path.isfile(pkl):
        logger.info("embeddings backfill: no .pkl at %s (nothing to migrate)", pkl)
        return report
    report["found"] = True

    try:
        from adalflow.core.db import LocalDB
        db = LocalDB.load_state(pkl)
    except Exception as e:  # noqa: BLE001 -- never break the caller
        logger.warning("embeddings backfill: could not load %s: %s", pkl, e)
        return report

    docs = []
    try:
        docs = db.get_transformed_data(key="split_and_embed") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("embeddings backfill: get_transformed_data failed: %s", e)
    if not docs:
        logger.info("embeddings backfill: %s has no split_and_embed items", pkl)
        return report

    fingerprint = _read_fingerprint(pkl)
    report["fingerprint"] = fingerprint

    # Replace this repo's rows atomically so re-running after a re-embed
    # doesn't accumulate stale duplicates.
    embeddings_store.wipe(owner, repo, repo_type)

    inserted = skipped = 0
    for idx, doc in enumerate(docs):
        meta = getattr(doc, "meta_data", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        file_path = meta.get("file_path") or f"chunk_{idx}"
        chunk_order = meta.get("order", idx)
        text = getattr(doc, "text", None) or getattr(doc, "non_chunkable_text", "") or ""
        vector = _doc_vector(doc)
        # carry the embedder fingerprint + whatever split metadata exists so a
        # future load-from-DB rebuild has the same info the .pkl load path uses
        full_meta = dict(meta)
        if fingerprint:
            full_meta.setdefault("embedder_fingerprint", fingerprint)
        try:
            embeddings_store.upsert_chunk(
                owner, repo, repo_type, file_path, chunk_order, text,
                vector=vector, meta=full_meta,
            )
            inserted += 1
        except Exception as e:  # noqa: BLE001
            skipped += 1
            logger.warning("embeddings backfill: row %s failed: %s", idx, e)

    report["inserted"] = inserted
    report["skipped"] = skipped
    logger.info(
        "embeddings backfill: %s -> %s inserted, %s skipped (fingerprint=%s)",
        pkl, inserted, skipped, fingerprint,
    )
    return report