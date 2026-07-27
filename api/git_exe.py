"""Locating -- and on Windows, provisioning -- the ``git`` executable.

Every subprocess that shells out to git must resolve the binary through this
module instead of relying on a bare ``"git"`` in PATH. The packaged Windows
.exe runs on machines where Git was never installed (or was installed
GUI-only, off PATH), and a bare invocation there dies with the opaque
``[WinError 2] The system cannot find the file specified``.

Resolution order (first hit wins):

1. ``HACKDEEPWIKI_GIT`` environment override (absolute path to the binary).
2. A previously provisioned portable MinGit under the data root
   (``<data_root>/git/cmd/git.exe`` -- survives app updates, Windows only).
3. ``shutil.which("git")`` -- the normal case on Linux/macOS and on Windows
   machines with Git for Windows on PATH.
4. Well-known Windows install locations that the GUI installer uses but that
   are not always on the PATH of a double-clicked .exe.

If nothing resolves and we are on Windows, :func:`git_executable` downloads
the MinGit build pinned in ``build/components.json`` (checksum-verified, the
same contract node/opencode use) into the data root and uses that. On other
platforms -- where a distro git is one package-manager command away and no
portable build exists upstream -- it raises :class:`GitNotFoundError` with an
actionable message instead of letting WinError/ENOENT leak to the UI.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from typing import Optional

from api.data_root import get_data_root

logger = logging.getLogger(__name__)

_ENV_OVERRIDE = "HACKDEEPWIKI_GIT"
_DOWNLOAD_TIMEOUT = 300  # seconds; MinGit is ~37 MB
_lock = threading.Lock()
_resolved: Optional[str] = None


class GitNotFoundError(RuntimeError):
    """Raised when no usable git executable exists and none can be provisioned."""


def managed_git_dir() -> str:
    """Writable, update-surviving home of the portable MinGit install."""
    return os.path.join(get_data_root(), "git")


def _managed_git_binary() -> str:
    return os.path.join(managed_git_dir(), "cmd", "git.exe")


def _windows_install_candidates() -> list[str]:
    """Paths the Git for Windows installer uses; a double-clicked .exe does
    not always inherit the PATH entries the installer registered."""
    roots = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramW6432"),
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(os.path.join(local, "Programs"))
    return [
        os.path.join(root, "Git", "cmd", "git.exe")
        for root in roots
        if root
    ]


def _works(binary: str) -> bool:
    """A candidate only counts if ``git --version`` actually runs."""
    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            timeout=15,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def resolve_git() -> Optional[str]:
    """Best usable git binary right now, or None (never downloads)."""
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        if os.path.isfile(override) and _works(override):
            return override
        logger.warning("%s=%r is not a working git binary; ignoring",
                       _ENV_OVERRIDE, override)

    if sys.platform == "win32":
        managed = _managed_git_binary()
        if os.path.isfile(managed) and _works(managed):
            return managed

    found = shutil.which("git")
    if found and _works(found):
        return found

    if sys.platform == "win32":
        for candidate in _windows_install_candidates():
            if os.path.isfile(candidate) and _works(candidate):
                return candidate
    return None


def _mingit_component() -> dict:
    from api.component_manifest import component_manifest

    manifest = component_manifest()
    mingit = manifest.get("mingit")
    if not isinstance(mingit, dict) or "url" not in mingit or "sha256" not in mingit:
        raise GitNotFoundError(
            "Git is not installed and this build carries no portable Git "
            "manifest. Install Git from https://git-scm.com/download/win or "
            f"set {_ENV_OVERRIDE} to a git.exe path."
        )
    return mingit


def _install_mingit() -> str:
    """Download the pinned MinGit into the data root (Windows only).

    Checksum-verified against build/components.json, extracted next to the
    other managed runtimes, and atomically promoted: a torn download can never
    masquerade as a working install because extraction happens into a staging
    directory that is renamed into place last.
    """
    mingit = _mingit_component()
    target_dir = managed_git_dir()
    staging_dir = target_dir + ".partial"
    logger.info("Git not found on this machine; downloading portable MinGit "
                "%s (~37 MB, one-time setup)...", mingit.get("version", ""))

    request = urllib.request.Request(
        mingit["url"], headers={"User-Agent": "HackDeepWiki"}
    )
    digest = hashlib.sha256()
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as archive:
        archive_path = archive.name
        try:
            with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT) as response:
                for chunk in iter(lambda: response.read(1024 * 1024), b""):
                    digest.update(chunk)
                    archive.write(chunk)
        except OSError as exc:
            archive.close()
            os.unlink(archive_path)
            raise GitNotFoundError(
                "Git is not installed and the portable Git download failed "
                f"({exc}). Install Git from https://git-scm.com/download/win "
                f"or set {_ENV_OVERRIDE} to a git.exe path."
            ) from exc

    try:
        actual = digest.hexdigest()
        if actual != mingit["sha256"]:
            raise GitNotFoundError(
                "Portable Git download failed its SHA-256 check "
                f"(expected {mingit['sha256']}, got {actual}); refusing to "
                "run it. Install Git from https://git-scm.com/download/win."
            )
        shutil.rmtree(staging_dir, ignore_errors=True)
        os.makedirs(staging_dir, exist_ok=True)
        with zipfile.ZipFile(archive_path) as bundle:
            bundle.extractall(staging_dir)
        shutil.rmtree(target_dir, ignore_errors=True)
        os.replace(staging_dir, target_dir)
    finally:
        os.unlink(archive_path)
        shutil.rmtree(staging_dir, ignore_errors=True)

    binary = _managed_git_binary()
    if not _works(binary):
        raise GitNotFoundError(
            "Portable Git was downloaded but does not run on this machine. "
            "Install Git from https://git-scm.com/download/win or set "
            f"{_ENV_OVERRIDE} to a git.exe path."
        )
    logger.info("Portable MinGit ready at %s", binary)
    return binary


def git_executable() -> str:
    """Path of the git binary to use, provisioning MinGit on Windows if needed.

    Raises :class:`GitNotFoundError` with an actionable message when git is
    unavailable and cannot be provisioned -- callers should let that surface
    to the user instead of a raw WinError/ENOENT.
    """
    global _resolved
    cached = _resolved
    if cached and os.path.isfile(cached):
        return cached
    with _lock:
        if _resolved and os.path.isfile(_resolved):
            return _resolved
        found = resolve_git()
        if found is None:
            if sys.platform == "win32":
                found = _install_mingit()
            else:
                raise GitNotFoundError(
                    "Git is not installed or not on PATH. Install it with "
                    "your package manager (e.g. `sudo apt install git`) or "
                    f"set {_ENV_OVERRIDE} to the git binary path."
                )
        _resolved = found
        return found
