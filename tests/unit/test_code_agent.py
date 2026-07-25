"""Unit tests for the Code Editing mode backend (api/code_agent).

Covers the pure logic that must not drift: binary resolution order, the
HackDeepWiki->opencode provider mapping (especially Ollama's baseURL and the
MCP block), repo_key derivation, provider restart signatures, and the
opencode-event normalization the chat/panel WebSockets rely on.

Run: pytest tests/unit/test_code_agent.py
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from api.code_agent import binary as oc_binary
from api.code_agent import events as oc_events
from api.code_agent.config import map_provider, provider_signature
from api.code_agent.manager import (
    CodeAgentError,
    repo_head_commit,
    repo_key_for,
    repo_worktree_fingerprint,
)
from api.code_agent.routes import _expects_repository_change


# ---------------------------------------------------------------------------
# binary resolution
# ---------------------------------------------------------------------------

def test_env_override_wins(tmp_path, monkeypatch):
    fake = tmp_path / "opencode"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setenv("HACKDEEPWIKI_OPENCODE_BIN", str(fake))
    assert oc_binary.resolve_opencode_binary() == str(fake)


def test_override_dir_beats_bundled_and_path(tmp_path, monkeypatch):
    monkeypatch.delenv("HACKDEEPWIKI_OPENCODE_BIN", raising=False)
    override_dir = tmp_path / "opencode" / "bin"
    override_dir.mkdir(parents=True)
    override_bin = override_dir / oc_binary.opencode_binary_name()
    override_bin.write_text("")
    monkeypatch.setattr(oc_binary, "override_bin_dir", lambda: str(override_dir))
    assert oc_binary.resolve_opencode_binary() == str(override_bin)


def test_missing_everywhere_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("HACKDEEPWIKI_OPENCODE_BIN", raising=False)
    monkeypatch.setattr(oc_binary, "override_bin_dir", lambda: str(tmp_path / "a"))
    monkeypatch.setattr(oc_binary, "_bundled_bin_dir", lambda: str(tmp_path / "b"))
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert oc_binary.resolve_opencode_binary() is None


def test_release_asset_name_platforms(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    assert oc_binary.release_asset_name() == "opencode-linux-x64.tar.gz"
    monkeypatch.setattr(sys, "platform", "win32")
    assert oc_binary.release_asset_name() == "opencode-windows-x64.zip"


# ---------------------------------------------------------------------------
# provider mapping
# ---------------------------------------------------------------------------

def test_map_provider_ollama_openai_compatible(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    provider_config, extra_env, model_ref = map_provider("ollama", "qwen3:14b", None, None)
    assert model_ref == "ollama/qwen3:14b"
    block = provider_config["ollama"]
    assert block["npm"] == "@ai-sdk/openai-compatible"
    assert block["options"]["baseURL"] == "http://localhost:11434/v1"
    assert "qwen3:14b" in block["models"]


def test_map_provider_ollama_endpoint_without_scheme():
    provider_config, _, _ = map_provider("ollama", "m", None, "192.168.1.5:11434")
    assert provider_config["ollama"]["options"]["baseURL"] == "http://192.168.1.5:11434/v1"


def test_map_provider_ollama_endpoint_hygiene():
    """Real-world bug: the UI-saved endpoint 'https://localhost:11434/v1'
    produced 'https://localhost:11434/v1/v1' -- TLS against plain-HTTP Ollama
    plus a doubled path, surfacing as an opaque 'Cannot connect to API'."""
    provider_config, _, _ = map_provider("ollama", "m", None, "https://localhost:11434/v1")
    assert provider_config["ollama"]["options"]["baseURL"] == "http://localhost:11434/v1"
    # /v1/v1 pasted outright also collapses to one
    provider_config, _, _ = map_provider("ollama", "m", None, "http://localhost:11434/v1/v1")
    assert provider_config["ollama"]["options"]["baseURL"] == "http://localhost:11434/v1"
    # https kept for genuinely remote hosts
    provider_config, _, _ = map_provider("ollama", "m", None, "https://ollama.example.com")
    assert provider_config["ollama"]["options"]["baseURL"] == "https://ollama.example.com/v1"


def test_provider_signature_changes_when_resolution_changes():
    # Equivalent normalized endpoints are equal; a genuinely different
    # resolved endpoint forces a restart. The signature itself stays opaque.
    normalized = provider_signature("ollama", None, "https://localhost:11434/v1", "m")
    canonical = provider_signature("ollama", None, "http://localhost:11434/v1", "m")
    other = provider_signature("ollama", None, "http://localhost:11435/v1", "m")
    assert normalized == canonical
    assert normalized != other
    assert "localhost" not in normalized


def test_map_provider_google_env_name_translation():
    _, extra_env, model_ref = map_provider("google", "gemini-2.5-pro", "KEY", None)
    # opencode reads GOOGLE_GENERATIVE_AI_API_KEY, not HackDeepWiki's GOOGLE_API_KEY
    assert extra_env["GOOGLE_GENERATIVE_AI_API_KEY"] == "KEY"
    assert model_ref == "google/gemini-2.5-pro"


def test_map_provider_claude_is_anthropic():
    _, extra_env, model_ref = map_provider("claude", "claude-sonnet-5", "K", None)
    assert model_ref == "anthropic/claude-sonnet-5"
    assert extra_env["ANTHROPIC_API_KEY"] == "K"


def test_map_provider_unknown_with_endpoint_is_openai_compatible():
    provider_config, _, model_ref = map_provider("openai_custom", "m", "k", "https://gw.example/v1")
    assert model_ref == "openai_custom/m"
    assert provider_config["openai_custom"]["options"]["baseURL"] == "https://gw.example/v1"
    assert provider_config["openai_custom"]["options"]["apiKey"] == "k"


def test_provider_signature_restart_semantics():
    # Credential values (not merely presence), endpoint and compatible-model
    # declarations all participate in restart semantics.
    assert provider_signature("ollama", None, None) == provider_signature("OLLAMA", None, None)
    assert provider_signature("openai", "k1", None) != provider_signature("openai", "k2", None)
    assert provider_signature("openai", "k", None) != provider_signature("openai", None, None)
    assert provider_signature("openai", None, "http://a") != provider_signature("openai", None, "http://b")
    assert provider_signature("openai", "secret-key-value", None) == provider_signature(
        "openai", "secret-key-value", None
    )
    assert "secret-key-value" not in provider_signature(
        "openai", "secret-key-value", None
    )


def test_write_opencode_config_full_auto_and_mcp(tmp_path, monkeypatch):
    from api.code_agent import config as oc_config
    monkeypatch.setattr(oc_config, "opencode_config_dir", lambda: str(tmp_path))
    monkeypatch.setenv("HACKDEEPWIKI_BACKEND_PORT", "8123")
    path, _, model_ref = oc_config.write_opencode_config("o_r", "ollama", "m1", None, None)
    data = json.loads(Path(path).read_text())
    assert data["permission"] == {"edit": "allow", "bash": "allow", "webfetch": "allow"}
    assert data["share"] == "disabled"
    mcp = data["mcp"]["hackdeepwiki"]
    assert mcp["url"] == "http://127.0.0.1:8123/mcp"
    assert mcp["headers"]["Authorization"].startswith("Bearer ")
    assert data["model"] == model_ref == "ollama/m1"


# ---------------------------------------------------------------------------
# repo_key_for
# ---------------------------------------------------------------------------

def test_repo_key_for_rejects_codeless_types():
    for repo_type in ("web", "zim", "fanwiki", "website"):
        with pytest.raises(CodeAgentError) as exc:
            repo_key_for("http://example/x/y", repo_type)
        assert exc.value.code == "unsupported_repo_type"


def test_repo_key_for_local_uses_dir_in_place(tmp_path, monkeypatch):
    monkeypatch.delenv("HACKDEEPWIKI_ALLOWED_LOCAL_ROOTS", raising=False)
    monkeypatch.setenv("HACKDEEPWIKI_HOST", "127.0.0.1")
    repo_key, repo_dir = repo_key_for(str(tmp_path), "local")
    assert repo_dir == os.path.realpath(tmp_path)
    assert repo_key.startswith("local_")


def test_repo_key_for_local_requires_roots_when_exposed(tmp_path, monkeypatch):
    monkeypatch.delenv("HACKDEEPWIKI_ALLOWED_LOCAL_ROOTS", raising=False)
    monkeypatch.setenv("HACKDEEPWIKI_HOST", "0.0.0.0")
    with pytest.raises(CodeAgentError) as exc:
        repo_key_for(str(tmp_path), "local")
    assert exc.value.code == "local_path_forbidden"


def test_repo_key_for_local_enforces_canonical_allowed_root(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setenv("HACKDEEPWIKI_HOST", "0.0.0.0")
    monkeypatch.setenv("HACKDEEPWIKI_ALLOWED_LOCAL_ROOTS", str(allowed))
    assert repo_key_for(str(allowed), "local")[1] == str(allowed)
    with pytest.raises(CodeAgentError) as exc:
        repo_key_for(str(outside), "local")
    assert exc.value.code == "local_path_forbidden"


def test_verify_archive_checksum_rejects_tampering(tmp_path, monkeypatch):
    archive = tmp_path / "asset.zip"
    archive.write_bytes(b"trusted archive")
    digest = __import__("hashlib").sha256(archive.read_bytes()).hexdigest()
    monkeypatch.setitem(oc_binary.OPENCODE_ARCHIVE_SHA256, "asset.zip", digest)
    oc_binary.verify_archive_checksum(
        str(archive), "asset.zip", oc_binary.OPENCODE_VERSION
    )
    archive.write_bytes(b"tampered archive")
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        oc_binary.verify_archive_checksum(
            str(archive), "asset.zip", oc_binary.OPENCODE_VERSION
        )


def test_opencode_install_lock_serializes_concurrent_first_use(tmp_path):
    active = 0
    maximum = 0
    guard = threading.Lock()
    barrier = threading.Barrier(3)

    def worker():
        nonlocal active, maximum
        barrier.wait()
        with oc_binary._installation_lock(str(tmp_path)):
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with guard:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert maximum == 1


def test_repo_key_for_missing_clone_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "api.data_pipeline._local_clone_dir",
        lambda url, t: str(tmp_path / "does-not-exist"),
    )
    with pytest.raises(CodeAgentError) as exc:
        repo_key_for("https://github.com/a/b", "github")
    assert exc.value.code == "repo_not_cloned"


def test_repo_head_commit(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "x"], cwd=tmp_path, check=True)
    head = repo_head_commit(str(tmp_path))
    assert head and len(head) == 40
    assert repo_head_commit(str(tmp_path / "nope")) is None


def test_repo_worktree_fingerprint_detects_real_changes(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git", "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-q", "--allow-empty", "-m", "x",
        ],
        cwd=tmp_path,
        check=True,
    )
    before = repo_worktree_fingerprint(str(tmp_path))
    (tmp_path / "created.txt").write_text("real change")
    after = repo_worktree_fingerprint(str(tmp_path))
    assert before and after and before != after


def test_mutation_intent_detection_is_conservative():
    assert _expects_repository_change("Corrige el bug y crea el archivo")
    assert _expects_repository_change("Please implement this change")
    assert not _expects_repository_change("Explain how this module works")
    assert not _expects_repository_change("Give me a detailed plan")


# ---------------------------------------------------------------------------
# wiki<->code commit backfill (version anchoring for pre-tracking wikis)
# ---------------------------------------------------------------------------

def _make_git_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "x"], cwd=tmp_path, check=True)
    return repo_head_commit(str(tmp_path))


def test_backfill_latest_release_of_git_repo(tmp_path, monkeypatch):
    import types
    from api.code_agent import context as oc_context

    head = _make_git_repo(tmp_path)
    v1 = tmp_path / "cache_github_o_r_en_v1.json"
    v2 = tmp_path / "cache_github_o_r_en_v2.json"
    v1.write_text(json.dumps({"version": 1}))
    v2.write_text(json.dumps({"version": 2}))
    monkeypatch.setattr("api.wiki_cache_paths.list_cache_files",
                        lambda *a: [str(v1), str(v2)])

    # Latest release (v2): anchored to the clone HEAD and persisted.
    cached = types.SimpleNamespace(version=2, repo_commit=None)
    got = oc_context._maybe_backfill_wiki_commit(cached, "o", "r", "github", "en", str(tmp_path))
    assert got == head
    assert json.loads(v2.read_text())["repo_commit"] == head
    assert "repo_commit" not in json.loads(v1.read_text())

    # Older release (v1): unknowable, stays unanchored.
    older = types.SimpleNamespace(version=1, repo_commit=None)
    assert oc_context._maybe_backfill_wiki_commit(older, "o", "r", "github", "en", str(tmp_path)) is None

    # Live local dirs are never inferred.
    local = types.SimpleNamespace(version=2, repo_commit=None)
    assert oc_context._maybe_backfill_wiki_commit(local, "o", "r", "local", "en", str(tmp_path)) is None

    # Already-anchored releases pass straight through.
    anchored = types.SimpleNamespace(version=2, repo_commit="abc123")
    assert oc_context._maybe_backfill_wiki_commit(anchored, "o", "r", "github", "en", str(tmp_path)) == "abc123"


# ---------------------------------------------------------------------------
# event normalization
# ---------------------------------------------------------------------------

def test_extract_session_id_shapes():
    assert oc_events.extract_session_id(
        {"properties": {"sessionID": "s1"}}) == "s1"
    assert oc_events.extract_session_id(
        {"properties": {"info": {"sessionID": "s2"}}}) == "s2"
    assert oc_events.extract_session_id(
        {"properties": {"part": {"sessionID": "s3"}}}) == "s3"
    assert oc_events.extract_session_id({"properties": {}}) is None


def test_normalize_shell_tool_part():
    evt = {
        "type": "message.part.updated",
        "properties": {"part": {
            "id": "p1", "type": "tool", "tool": "bash",
            "state": {"status": "completed", "title": "run tests",
                      "input": {"command": "pytest -q"}, "output": "3 passed"},
        }},
    }
    env = oc_events.normalize_for_panel(evt)
    assert env["t"] == "shell"
    assert env["command"] == "pytest -q"
    assert env["output"] == "3 passed"
    assert env["status"] == "completed"


def test_normalize_edit_tool_part():
    evt = {
        "type": "message.part.updated",
        "properties": {"part": {
            "id": "p2", "type": "tool", "tool": "edit",
            "state": {"status": "completed", "title": "src/x.py",
                      "input": {"filePath": "src/x.py"}},
        }},
    }
    env = oc_events.normalize_for_panel(evt)
    assert env["t"] == "file_edited"
    assert env["file"] == "src/x.py"


def test_normalize_text_and_unknown_events_hidden():
    text_part = {"type": "message.part.updated",
                 "properties": {"part": {"type": "text", "text": "hi"}}}
    assert oc_events.normalize_for_panel(text_part) is None
    assert oc_events.normalize_for_panel({"type": "server.connected"}) is None


def test_normalize_session_status_retry_and_idle():
    retry = oc_events.normalize_for_panel({
        "type": "session.status",
        "properties": {"sessionID": "s", "status": {
            "type": "retry", "attempt": 2,
            "message": "Cannot connect to API: Unable to connect."}},
    })
    assert retry["t"] == "error"
    assert "retry 2" in retry["message"] and "Cannot connect" in retry["message"]

    idle = oc_events.normalize_for_panel({
        "type": "session.status",
        "properties": {"sessionID": "s", "status": {"type": "idle"}},
    })
    assert idle == {"t": "status", "state": "idle"}

    busy = oc_events.normalize_for_panel({
        "type": "session.status",
        "properties": {"sessionID": "s", "status": {"type": "busy"}},
    })
    assert busy == {"t": "status", "state": "busy"}


def test_describe_target():
    from api.code_agent.config import describe_target
    assert describe_target("ollama", "m", None, None) == "ollama/m → http://localhost:11434/v1"
    assert describe_target("claude", "c", "k", None) == "anthropic/c → api.anthropic.com"


def test_normalize_error_and_exit():
    err = oc_events.normalize_for_panel(
        {"type": "session.error", "properties": {"error": {"message": "boom"}}})
    assert err["t"] == "error" and "boom" in err["message"]
    exited = oc_events.normalize_for_panel(
        {"type": "instance.exited", "properties": {"stderr_tail": ["x"]}})
    assert exited["t"] == "status" and exited["state"] == "crashed"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
