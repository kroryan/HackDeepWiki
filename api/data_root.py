"""Writable data-root resolution for FreeDeepWiki.

All persistent state (cloned repos, embedding databases, wiki cache, and
adalflow's generator cache dbs) lives under one data root. The root is resolved
to a portable ``DATABASE`` folder located **next to the executable** (.AppImage /
.exe) so the entire install — executable + DATABASE — is self-contained and can be
copied or zipped as a single unit. Everything the app produces (wikis, caches,
repos, embeddings, logs, config, adalflow internal dbs) lands inside DATABASE.

adalflow hardcodes ``~/.adalflow`` and reads no env var, so we also monkey-patch
its ``get_adalflow_default_root_path`` to return the same DATABASE root, ensuring
the library's own caches/dbs are portable too.
"""

import logging
import os
import shutil
import sys
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


def get_portable_base_dir() -> str:
    """Return the directory the user launched the app from — the folder that
    contains the .AppImage / .exe — so a portable ``DATABASE`` folder lives next
    to the executable and travels with it.

    Resolution order:
      1. AppImage: the ``APPIMAGE`` env var holds the absolute path of the
         .AppImage file the user ran. ``sys.executable`` inside an AppImage points
         into the read-only squashfs mount (``/tmp/.mount_...``), which we avoid.
      2. Frozen PyInstaller build (Windows .exe / onefile): ``sys.executable`` is
         the launcher executable, so its directory is the install folder.
      3. Development mode: the project root (parent of this api/ dir).
    """
    appimage = os.environ.get("APPIMAGE")
    if appimage and os.path.isfile(appimage):
        return os.path.dirname(os.path.abspath(appimage))
    if getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _legacy_env_override() -> str:
    """Honour an explicit data-dir override. Accepts both the canonical
    ``FREEDEEPWIKI_DATA_DIR`` and the legacy misspelled ``FREEDEPWIKI_DATA_DIR``
    so older docs/scripts still work."""
    return (
        os.environ.get("FREEDEEPWIKI_DATA_DIR")
        or os.environ.get("FREEDEPWIKI_DATA_DIR")
        or ""
    )


def get_data_root() -> str:
    """Return the writable directory used for repos/databases/wikicache/caches.

    Resolution order:
      1. Explicit env override (FREEDEEPWIKI_DATA_DIR / FREEDEPWIKI_DATA_DIR)
      2. <portable_base>/DATABASE  (PRIMARY — portable folder next to the exe)
      3. ~/.adalflow (upstream default, kept for existing installs)
      4. ~/.freedeepwiki/adalflow (per-user fallback when ~/.adalflow is
         unwritable, e.g. root-owned from a previous sudo run)
      5. a temp directory as a last resort
    """
    global _cached_root
    if _cached_root:
        return _cached_root

    database_dir = os.path.join(get_portable_base_dir(), "DATABASE")

    candidates = []
    env_override = _legacy_env_override()
    if env_override:
        candidates.append(env_override)
    candidates.append(database_dir)
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


def _patch_adalflow_root(root: str) -> None:
    """Monkey-patch adalflow's hardcoded ``~/.adalflow`` root to return ``root``.

    adalflow's ``get_adalflow_default_root_path()`` reads no env var, so without
    this the library scatters its own logs/caches/dbs in the user's home instead of
    inside the portable DATABASE. We patch the function in ``adalflow.utils.global_config``
    plus every already-loaded submodule that captured the original reference (e.g.
    ``adalflow.core.db``, ``adalflow.core.generator`` import it at module load).
    """
    try:
        import adalflow.utils.global_config as _agc
        _target = (lambda r=root: r)
        _agc.get_adalflow_default_root_path = _target
        try:
            import adalflow.utils as _au
            _au.get_adalflow_default_root_path = _target
        except Exception:
            pass
        for _mod in list(sys.modules.values()):
            if _mod is None:
                continue
            _name = getattr(_mod, "__name__", "")
            if (
                _name
                and _name.startswith("adalflow")
                and hasattr(_mod, "get_adalflow_default_root_path")
                and getattr(_mod, "get_adalflow_default_root_path", None) is not _target
            ):
                try:
                    setattr(_mod, "get_adalflow_default_root_path", _target)
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Could not redirect adalflow root to {root}: {e}")


def migrate_legacy_wikicache(target_root: str) -> None:
    """Copy wiki cache files from legacy locations (~/.freedeepwiki/adalflow/wikicache,
    ~/.adalflow/wikicache) into ``<target_root>/wikicache`` so previously generated
    wikis are still found after switching to the portable DATABASE layout. Existing
    files in the target are never overwritten.
    """
    home = os.path.expanduser("~")
    legacy_dirs = [
        os.path.join(home, ".freedeepwiki", "adalflow", "wikicache"),
        os.path.join(home, ".adalflow", "wikicache"),
    ]
    target_wikicache = os.path.join(target_root, "wikicache")
    try:
        os.makedirs(target_wikicache, exist_ok=True)
    except OSError:
        return
    moved = 0
    for legacy in legacy_dirs:
        if not os.path.isdir(legacy) or os.path.abspath(legacy) == os.path.abspath(target_wikicache):
            continue
        for fn in os.listdir(legacy):
            if not fn.endswith(".json"):
                continue
            src = os.path.join(legacy, fn)
            dst = os.path.join(target_wikicache, fn)
            if os.path.exists(dst):
                continue
            try:
                shutil.copy2(src, dst)
                moved += 1
                logger.info(f"Migrated legacy wiki cache: {fn} -> {target_wikicache}")
            except OSError as e:
                logger.warning(f"Could not migrate legacy wiki cache {fn}: {e}")
    if moved:
        print(f"Migrated {moved} legacy wiki cache file(s) into {target_wikicache}")


# Resolve the root once at import time and redirect adalflow's hardcoded root to it
# so EVERYTHING (app data + adalflow internal dbs/caches) lives inside DATABASE.
_resolved_root = get_data_root()
_patch_adalflow_root(_resolved_root)