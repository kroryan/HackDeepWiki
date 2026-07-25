"""Locating and (lazily) downloading the opencode binary.

Resolution order -- most-user-controlled first, so an update the user installed
into the writable DATABASE dir always beats the read-only copy baked into the
AppImage/exe:

  1. ``HACKDEEPWIKI_OPENCODE_BIN`` env var (dev/testing override)
  2. ``<DATABASE>/opencode/bin/opencode(.exe)`` -- written by the update
     endpoint or by the lazy download; survives replacing the executable
  3. the binary bundled next to the app (``bin/opencode`` in the PyInstaller
     payload, same place the Node runtime lives)
  4. ``opencode`` on PATH
  5. nothing -> ``ensure_opencode`` downloads the pinned release into (2)

The download mirrors the vulnscan-image policy (api/web_vuln_scanner/
docker_tools.py): lazy, progress-reported, and every failure degrades to a
clear error instead of a crash. Downloads are atomic (temp file + rename) so
a killed download never leaves a half-written binary behind.
"""

import logging
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from typing import Awaitable, Callable, Optional

from api.data_root import get_data_root

logger = logging.getLogger(__name__)

# Pinned release of anomalyco/opencode. Single source of truth: also imported
# by scripts/prepare_assets.py so the bundled binary and the lazy download
# agree. NOTE the project moved from sst/opencode to anomalyco/opencode -- the
# old repo's assets 404.
OPENCODE_VERSION = "v1.18.5"
GITHUB_REPO = "anomalyco/opencode"

# Optional callback: awaited with one human-readable progress line at a time.
ProgressCb = Optional[Callable[[str], Awaitable[None]]]


def opencode_binary_name() -> str:
    return "opencode.exe" if sys.platform == "win32" else "opencode"


def release_asset_name() -> str:
    """The CLI archive asset name for this platform in an opencode release.

    v1.18.x asset naming (verified): linux ships .tar.gz, windows/darwin ship
    .zip; arch suffixes are x64/arm64.
    """
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    if sys.platform == "win32":
        return f"opencode-windows-{arch}.zip"
    if sys.platform == "darwin":
        return f"opencode-darwin-{arch}.zip"
    return f"opencode-linux-{arch}.tar.gz"


def override_bin_dir() -> str:
    """Writable, update-surviving location for a user-installed opencode."""
    return os.path.join(get_data_root(), "opencode", "bin")


def _bundled_bin_dir() -> str:
    """Where a bundled binary would live: ``bin/`` next to the frozen app's
    payload (the same directory that carries the bundled Node runtime), or
    ``<project root>/bin`` in development."""
    if getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None):
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "bin")


def resolve_opencode_binary() -> Optional[str]:
    """Return the path of the opencode binary to use, or None if absent
    everywhere (the caller then triggers the lazy download)."""
    env_override = os.environ.get("HACKDEEPWIKI_OPENCODE_BIN")
    if env_override:
        if os.path.isfile(env_override):
            return env_override
        logger.warning("HACKDEEPWIKI_OPENCODE_BIN=%r does not exist; ignoring", env_override)

    name = opencode_binary_name()
    for candidate_dir in (override_bin_dir(), _bundled_bin_dir()):
        candidate = os.path.join(candidate_dir, name)
        if os.path.isfile(candidate):
            return candidate

    return shutil.which(name)


_version_cache: dict[str, str] = {}


def installed_opencode_version(binary_path: str) -> Optional[str]:
    """``opencode --version`` output (e.g. ``1.18.5``), cached per path."""
    cached = _version_cache.get(binary_path)
    if cached:
        return cached
    try:
        out = subprocess.run(
            [binary_path, "--version"],
            capture_output=True, text=True, timeout=15,
        )
        match = re.search(r"\d+\.\d+\.\S*", out.stdout or out.stderr or "")
        if match:
            _version_cache[binary_path] = match.group(0)
            return match.group(0)
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("Could not read opencode version from %s: %s", binary_path, e)
    return None


def _download_url(version: str) -> str:
    if version == "latest":
        return f"https://github.com/{GITHUB_REPO}/releases/latest/download/{release_asset_name()}"
    return f"https://github.com/{GITHUB_REPO}/releases/download/{version}/{release_asset_name()}"


def _extract_binary(archive_path: str, dest_dir: str) -> str:
    """Extract the single opencode binary out of the release archive into
    dest_dir (atomically: extract to a temp name, chmod, then rename)."""
    name = opencode_binary_name()
    final_path = os.path.join(dest_dir, name)
    tmp_path = os.path.join(dest_dir, name + ".tmp")

    def _write_member(fileobj) -> None:
        with open(tmp_path, "wb") as out:
            shutil.copyfileobj(fileobj, out)

    if archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            members = [m for m in zf.namelist() if os.path.basename(m) == name]
            if not members:
                raise RuntimeError(f"No {name!r} inside {os.path.basename(archive_path)}")
            with zf.open(members[0]) as f:
                _write_member(f)
    else:
        with tarfile.open(archive_path, "r:gz") as tf:
            members = [m for m in tf.getmembers() if os.path.basename(m.name) == name and m.isfile()]
            if not members:
                raise RuntimeError(f"No {name!r} inside {os.path.basename(archive_path)}")
            extracted = tf.extractfile(members[0])
            if extracted is None:
                raise RuntimeError(f"Could not extract {members[0].name}")
            with extracted as f:
                _write_member(f)

    os.chmod(tmp_path, os.stat(tmp_path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(tmp_path, final_path)
    return final_path


def download_opencode(version: str = OPENCODE_VERSION, progress_cb=None) -> str:
    """Blocking download of an opencode release into the DATABASE override dir.

    ``progress_cb`` (if given) is a plain sync callable taking one progress
    line -- the async wrapper in ``ensure_opencode`` adapts it. Returns the
    installed binary path; raises RuntimeError with a user-explainable message
    on any failure.
    """
    dest_dir = override_bin_dir()
    os.makedirs(dest_dir, exist_ok=True)
    url = _download_url(version)
    asset = release_asset_name()

    def report(line: str) -> None:
        logger.info("opencode download: %s", line)
        if progress_cb:
            try:
                progress_cb(line)
            except Exception:  # noqa: BLE001 - progress must never break the download
                pass

    report(f"Downloading {asset} ({version}) from GitHub...")
    tmp_archive = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "HackDeepWiki"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            fd, tmp_archive = tempfile.mkstemp(
                suffix=os.path.splitext(asset)[1], dir=dest_dir
            )
            done = 0
            last_pct = -10
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = int(done * 100 / total)
                        if pct >= last_pct + 10:
                            last_pct = pct
                            report(f"Downloading opencode... {pct}% ({done // (1024*1024)} MB)")
        report("Extracting...")
        final_path = _extract_binary(tmp_archive, dest_dir)
        # The version cache is keyed by path; an update writes a NEW binary
        # to the SAME path, so the stale entry must go before re-probing.
        _version_cache.pop(final_path, None)
        ver = installed_opencode_version(final_path)
        if not ver:
            raise RuntimeError("Downloaded opencode binary did not run (--version failed)")
        report(f"opencode {ver} installed at {final_path}")
        return final_path
    except Exception as e:
        raise RuntimeError(
            f"Could not download opencode from {url}: {e}. "
            "Check your internet connection, or place an opencode binary at "
            f"{os.path.join(dest_dir, opencode_binary_name())} manually."
        ) from e
    finally:
        if tmp_archive and os.path.exists(tmp_archive):
            try:
                os.remove(tmp_archive)
            except OSError:
                pass


async def ensure_opencode(progress_cb: ProgressCb = None) -> str:
    """Async entry point: resolve the binary, lazily downloading the pinned
    release if it's missing everywhere. Runs the blocking download in a
    thread so the event loop stays responsive."""
    import asyncio

    resolved = resolve_opencode_binary()
    if resolved:
        return resolved

    loop = asyncio.get_running_loop()
    lines: "asyncio.Queue[str]" = asyncio.Queue()

    def sync_progress(line: str) -> None:
        loop.call_soon_threadsafe(lines.put_nowait, line)

    async def pump() -> None:
        while True:
            line = await lines.get()
            if progress_cb:
                await progress_cb(line)

    pump_task = asyncio.create_task(pump()) if progress_cb else None
    try:
        return await asyncio.to_thread(download_opencode, OPENCODE_VERSION, sync_progress)
    finally:
        if pump_task:
            pump_task.cancel()
