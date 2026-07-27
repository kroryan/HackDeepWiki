"""Contract tests for api.git_exe -- the single place that locates git.

The regression these lock in: the packaged Windows .exe used to invoke a bare
``"git"`` and die with ``[WinError 2]`` on machines without Git on PATH.
"""

from __future__ import annotations

import hashlib
import os
import zipfile

import pytest

import api.git_exe as git_exe
from api.git_exe import GitNotFoundError


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    monkeypatch.setattr(git_exe, "_resolved", None)
    monkeypatch.delenv("HACKDEEPWIKI_GIT", raising=False)


def test_env_override_wins(monkeypatch, tmp_path):
    fake = tmp_path / "git"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HACKDEEPWIKI_GIT", str(fake))
    monkeypatch.setattr(git_exe, "_works", lambda binary: True)
    assert git_exe.resolve_git() == str(fake)


def test_broken_env_override_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("HACKDEEPWIKI_GIT", str(tmp_path / "missing"))
    monkeypatch.setattr(git_exe, "_works", lambda binary: True)
    monkeypatch.setattr(git_exe.shutil, "which", lambda name: "/usr/bin/git")
    assert git_exe.resolve_git() == "/usr/bin/git"


def test_path_candidates_must_actually_run(monkeypatch):
    monkeypatch.setattr(git_exe.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(git_exe, "_works", lambda binary: False)
    assert git_exe.resolve_git() is None


def test_git_executable_raises_actionable_error_on_linux(monkeypatch):
    monkeypatch.setattr(git_exe.sys, "platform", "linux")
    monkeypatch.setattr(git_exe, "resolve_git", lambda: None)
    with pytest.raises(GitNotFoundError, match="HACKDEEPWIKI_GIT"):
        git_exe.git_executable()


def test_git_executable_provisions_mingit_on_windows(monkeypatch):
    monkeypatch.setattr(git_exe.sys, "platform", "win32")
    monkeypatch.setattr(git_exe, "resolve_git", lambda: None)
    monkeypatch.setattr(git_exe, "_install_mingit", lambda: "C:/data/git/cmd/git.exe")
    assert git_exe.git_executable() == "C:/data/git/cmd/git.exe"
    # And the result is cached for the next caller.
    monkeypatch.setattr(
        git_exe, "_install_mingit",
        lambda: pytest.fail("second call must hit the cache"),
    )
    monkeypatch.setattr(git_exe.os.path, "isfile", lambda path: True)
    assert git_exe.git_executable() == "C:/data/git/cmd/git.exe"


def test_install_mingit_verifies_checksum_and_extracts(monkeypatch, tmp_path):
    """Full provisioning pipeline against a local fake MinGit zip."""
    archive = tmp_path / "mingit.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("cmd/git.exe", b"fake-binary")
        bundle.writestr("mingw64/bin/git.exe", b"fake-binary")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    target = tmp_path / "managed" / "git"
    monkeypatch.setattr(git_exe, "managed_git_dir", lambda: str(target))
    monkeypatch.setattr(
        git_exe, "_mingit_component",
        lambda: {"version": "0.0-test", "url": archive.as_uri(), "sha256": digest},
    )
    monkeypatch.setattr(git_exe, "_works", lambda binary: os.path.isfile(binary))

    installed = git_exe._install_mingit()
    assert installed == str(target / "cmd" / "git.exe")
    assert (target / "cmd" / "git.exe").read_bytes() == b"fake-binary"
    assert not (tmp_path / "managed" / "git.partial").exists()


def test_install_mingit_rejects_bad_checksum(monkeypatch, tmp_path):
    archive = tmp_path / "mingit.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("cmd/git.exe", b"fake-binary")

    target = tmp_path / "managed" / "git"
    monkeypatch.setattr(git_exe, "managed_git_dir", lambda: str(target))
    monkeypatch.setattr(
        git_exe, "_mingit_component",
        lambda: {"version": "0.0-test", "url": archive.as_uri(), "sha256": "0" * 64},
    )
    with pytest.raises(GitNotFoundError, match="SHA-256"):
        git_exe._install_mingit()
    assert not target.exists()


def test_manifest_pins_mingit():
    from api.component_manifest import component_manifest

    mingit = component_manifest()["mingit"]
    assert mingit["url"].startswith(
        "https://github.com/git-for-windows/git/releases/download/"
    )
    assert len(mingit["sha256"]) == 64
