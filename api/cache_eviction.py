"""Bounded wiki-cache eviction (Fase 9.6) -- LRU + TTL caps so the flat
``<data_root>/wikicache/`` directory can't grow without bound across many
repos and many generated releases.

The cache layout (see api.wiki_cache_paths) keeps a *versioned* file per
generation (``..._v3.json``) because the "releases" feature lets the user view
older wiki versions -- so old versions are intentional and must not be
deleted wholesale. Eviction therefore respects two rules:

  1. The NEWEST file per repo/language/type group is ALWAYS protected -- a
     repo's current wiki is never deleted by the evictor, even if every cap
     is exceeded. Only surplus (older releases) can be reclaimed.
  2. Everything is opt-in via environment variables, defaulting to UNLIMITED.
     A destructive feature that's off by default can't surprise a user who
     hasn't asked for it -- this matches the project's local-first, never-
     delete-your-data stance. Set the env vars to enable:

       HACKDEEPWIKI_WIKI_CACHE_MAX_AGE_DAYS  -- TTL: delete surplus releases
                                                older than N days (0 = off)
       HACKDEEPWIKI_WIKI_CACHE_MAX_BYTES      -- size cap: evict oldest surplus
                                                until total bytes <= N (0 = off)
       HACKDEEPWIKI_WIKI_CACHE_MAX_FILES      -- count cap: evict oldest surplus
                                                until total files <= N (0 = off)

Wired into the wiki-save path (after a successful write) and app startup, so
the caps are enforced as the cache grows and on launch. Also exposed via
/api/wiki_cache/prune for a manual run + report.

Stdlib only (os/re/time) -- no new dependency, PyInstaller-safe.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

from api.wiki_cache_paths import (
    WIKI_CACHE_DIR,
    WIKI_CACHE_FILE_PREFIX,
    LEGACY_WIKI_CACHE_FILE_PREFIX,
)

logger = logging.getLogger(__name__)

_VERSION_SUFFIX_RE = re.compile(r"_v\d+$")


def _env_int(name: str, default: int) -> int:
    """Read a non-negative int env var, falling back to ``default`` on any
    parse error / negative value. Centralized so a malformed env var (e.g.
    ``HACKDEEPWIKI_WIKI_CACHE_MAX_FILES=abc``) degrades to the default
    instead of crashing the wiki-save path that calls prune."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        logger.warning("ignoring invalid %s=%r (not an int)", name, raw)
        return default
    if v < 0:
        return default
    return v


def _group_key(filename: str) -> str:
    """Strip the ``_vN`` version suffix + ``.json`` to get the repo/language/type
    group identity, so all releases of one wiki cluster together and the
    newest among them can be protected. e.g.
    ``hackdeepwiki_cache_github_octocat_Hello-World_en_v3.json``
    -> ``hackdeepwiki_cache_github_octocat_Hello-World_en``"""
    base = filename
    if base.endswith(".json"):
        base = base[: -len(".json")]
    return _VERSION_SUFFIX_RE.sub("", base, count=1)


def _list_cache_files() -> list[str]:
    """Every wiki-cache .json in the flat dir (current + legacy prefix), as
    absolute paths. Empty if the dir doesn't exist yet."""
    if not os.path.isdir(WIKI_CACHE_DIR):
        return []
    prefixes = (WIKI_CACHE_FILE_PREFIX, LEGACY_WIKI_CACHE_FILE_PREFIX)
    out = []
    for fn in os.listdir(WIKI_CACHE_DIR):
        if fn.endswith(".json") and fn.startswith(prefixes):
            out.append(os.path.join(WIKI_CACHE_DIR, fn))
    return out


def _protect_newest_per_group(files: list[str]) -> set[str]:
    """Return the set of paths that must NOT be evicted -- the newest (by
    mtime, tie-broken by name) file in each repo/language/type group, so a
    repo's current wiki is always safe even when every cap is blown."""
    newest: dict[str, str] = {}
    newest_mtime: dict[str, float] = {}
    for path in files:
        key = _group_key(os.path.basename(path))
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if key not in newest or mtime > newest_mtime[key] or (
            mtime == newest_mtime[key] and path > newest[key]
        ):
            newest[key] = path
            newest_mtime[key] = mtime
    return set(newest.values())


def prune_wiki_cache(
    max_age_days: Optional[int] = None,
    max_bytes: Optional[int] = None,
    max_files: Optional[int] = None,
) -> dict:
    """Enforce the configured caps, evicting only OLDER, NON-PROTECTED
    releases. Returns a report ``{checked, evicted, bytes_freed, caps}``.
    Never raises -- wired into the save path, it must not break a save.

    When a cap arg is None it's read from its env var (default 0 = that cap
    disabled). All three defaulting to 0 means the feature is present but
    inactive -- no deletion happens unless the operator opts in via env."""
    if max_age_days is None:
        max_age_days = _env_int("HACKDEEPWIKI_WIKI_CACHE_MAX_AGE_DAYS", 0)
    if max_bytes is None:
        max_bytes = _env_int("HACKDEEPWIKI_WIKI_CACHE_MAX_BYTES", 0)
    if max_files is None:
        max_files = _env_int("HACKDEEPWIKI_WIKI_CACHE_MAX_FILES", 0)

    report = {
        "checked": 0, "evicted": 0, "bytes_freed": 0,
        "caps": {"max_age_days": max_age_days, "max_bytes": max_bytes, "max_files": max_files},
    }

    files = _list_cache_files()
    report["checked"] = len(files)
    if not files:
        return report

    protected = _protect_newest_per_group(files)
    # Evictable = everything that isn't a repo's newest release, oldest first.
    # This is the LRU order; TTL and size/count caps all consume from this end.
    evictable = []
    for path in files:
        if path in protected:
            continue
        try:
            evictable.append((os.path.getmtime(path), path))
        except OSError:
            continue
    evictable.sort(key=lambda t: t[0])  # oldest mtime first

    now = time.time()
    to_delete: list[str] = []

    # TTL: drop surplus releases older than max_age_days.
    if max_age_days and max_age_days > 0:
        cutoff = now - max_age_days * 86400
        for mtime, path in evictable:
            if mtime < cutoff:
                to_delete.append(path)

    # Size/count caps: from the oldest surplus release still on disk, evict
    # until we're under both caps. Recompute the remaining total after TTL
    # removals so the caps account for what TTL already reclaimed.
    def _remaining(paths: list[str]) -> tuple[int, int]:
        total_files = 0
        total_bytes = 0
        for path in paths:
            try:
                total_bytes += os.path.getsize(path)
                total_files += 1
            except OSError:
                pass
        return total_files, total_bytes

    delete_set = set(to_delete)
    # Candidates for cap-driven eviction: surplus not already TTL-marked,
    # oldest first.
    cap_candidates = [path for _mt, path in evictable if path not in delete_set]
    survivors = [p for p in files if p not in delete_set]
    total_files, total_bytes = _remaining(survivors)

    if (max_bytes and max_bytes > 0 and total_bytes > max_bytes) or \
       (max_files and max_files > 0 and total_files > max_files):
        for path in cap_candidates:
            over_bytes = max_bytes and max_bytes > 0 and total_bytes > max_bytes
            over_files = max_files and max_files > 0 and total_files > max_files
            if not over_bytes and not over_files:
                break
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            to_delete.append(path)
            total_bytes -= size
            total_files -= 1

    # Perform the deletions (dedup, best-effort). A missing file between plan
    # and act is fine -- the goal is "under cap", and a racy removal helps.
    freed = 0
    seen = set()
    for path in to_delete:
        if path in seen:
            continue
        seen.add(path)
        try:
            freed += os.path.getsize(path)
            os.remove(path)
        except OSError as e:
            logger.warning("cache eviction could not remove %s: %s", path, e)

    report["evicted"] = len(seen)
    report["bytes_freed"] = freed
    if report["evicted"]:
        logger.info(
            "wiki cache prune: evicted %s surplus release(s), freed %s bytes "
            "(caps: age_days=%s max_bytes=%s max_files=%s)",
            report["evicted"], freed, max_age_days, max_bytes, max_files,
        )
    return report