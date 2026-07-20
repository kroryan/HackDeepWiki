"""Writable data-root resolution for FreeDeepWiki.

All persistent state (cloned repos, embedding databases, wiki cache, and
adalflow's generator cache dbs) lives under one data root. adalflow hardcodes
`~/.adalflow`, but that directory is frequently left root-owned when the app
was once launched with sudo/Docker — after which every provider fails with
"Permission denied" / "attempt to write a readonly database" even though the
app itself is fine. Resolve the root once, verify it is actually writable,
and fall back to a per-user alternative instead of crashing.
"""

import logging
import os
import tempfile

logger = logging.getLogger(__name__)

_cached_root = None


def _is_writable_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_probe")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def get_data_root() -> str:
    """Return the writable directory used for repos/databases/wikicache/caches.

    Resolution order:
      1. FREEDEPWIKI_DATA_DIR env var (explicit override)
      2. ~/.adalflow (upstream default, kept for existing installs)
      3. ~/.freedeepwiki/adalflow (per-user fallback when ~/.adalflow is
         unwritable, e.g. root-owned from a previous sudo run)
      4. a temp directory as a last resort
    """
    global _cached_root
    if _cached_root:
        return _cached_root

    candidates = []
    env_override = os.environ.get("FREEDEPWIKI_DATA_DIR")
    if env_override:
        candidates.append(env_override)

    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        candidates.append(os.path.join(appdata, "adalflow"))
    else:
        candidates.append(os.path.join(os.path.expanduser("~"), ".adalflow"))
    candidates.append(os.path.join(os.path.expanduser("~"), ".freedeepwiki", "adalflow"))

    for candidate in candidates:
        if _is_writable_dir(candidate):
            _cached_root = candidate
            break

    if not _cached_root:
        _cached_root = os.path.join(tempfile.gettempdir(), "freedeepwiki-adalflow")
        os.makedirs(_cached_root, exist_ok=True)

    default_root = candidates[0]
    if _cached_root != default_root:
        logger.warning(
            f"Data directory '{default_root}' is not writable (was the app previously "
            f"run as root/sudo?). Using '{_cached_root}' instead. To reclaim the old "
            f"data run: sudo chown -R $USER {default_root}"
        )
    else:
        logger.info(f"Using data root: {_cached_root}")

    return _cached_root
