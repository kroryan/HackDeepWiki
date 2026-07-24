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
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from api.code_agent import binary as oc_binary
from api.code_agent import events as oc_events
from api.code_agent.config import map_provider, provider_signature
from api.code_agent.manager import CodeAgentError, repo_head_commit, repo_key_for


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
    # Model changes never force a restart; credential/endpoint changes do.
    assert provider_signature("ollama", None, None) == provider_signature("OLLAMA", None, None)
    assert provider_signature("openai", "k1", None) == provider_signature("openai", "k2", None)
    assert provider_signature("openai", "k", None) != provider_signature("openai", None, None)
    assert provider_signature("openai", None, "http://a") != provider_signature("openai", None, "http://b")


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


def test_repo_key_for_local_uses_dir_in_place(tmp_path):
    repo_key, repo_dir = repo_key_for(str(tmp_path), "local")
    assert repo_dir == str(tmp_path)
    assert repo_key.startswith("local_")


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


def test_normalize_error_and_exit():
    err = oc_events.normalize_for_panel(
        {"type": "session.error", "properties": {"error": {"message": "boom"}}})
    assert err["t"] == "error" and "boom" in err["message"]
    exited = oc_events.normalize_for_panel(
        {"type": "instance.exited", "properties": {"stderr_tail": ["x"]}})
    assert exited["t"] == "status" and exited["state"] == "crashed"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
