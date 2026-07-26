"""Security regression tests for the CodeAgent transport boundary."""

from fastapi import HTTPException
import pytest

from api.code_agent.routes import (
    _authorize_websocket,
    _code_authorization_is_valid,
    _require_code_authorization,
    code_agent_update,
    router,
)
from api.code_agent.models import CodeAgentUpdateRequest
from api.security import sanitize_error_message


@pytest.fixture
def protected_config(monkeypatch):
    monkeypatch.setenv("HACKDEEPWIKI_HOST", "127.0.0.1")
    monkeypatch.setattr("api.config.WIKI_AUTH_MODE", True)
    monkeypatch.setattr("api.config.WIKI_AUTH_CODE", "correct-horse")


def test_exposed_code_agent_requires_auth_even_when_global_auth_is_off(monkeypatch):
    monkeypatch.setattr("api.config.WIKI_AUTH_MODE", False)
    monkeypatch.setattr("api.config.WIKI_AUTH_CODE", "")
    monkeypatch.setenv("HACKDEEPWIKI_HOST", "0.0.0.0")
    assert _code_authorization_is_valid(None) is False

    monkeypatch.setenv("HACKDEEPWIKI_HOST", "127.0.0.1")
    assert _code_authorization_is_valid(None) is True


def test_every_code_http_route_has_auth_dependency():
    http_routes = [
        route for route in router.routes
        if getattr(route, "path", "").startswith("/api/code/")
    ]
    assert http_routes
    assert all(getattr(route, "dependencies", []) for route in http_routes)


@pytest.mark.asyncio
async def test_update_response_never_discloses_install_path(monkeypatch):
    async def run_inline(func, *args):
        return func(*args)

    monkeypatch.setattr("api.code_agent.routes.asyncio.to_thread", run_inline)
    monkeypatch.setattr(
        "api.code_agent.routes.download_opencode",
        lambda _version: "/home/private/DATABASE/opencode/bin/opencode",
    )
    monkeypatch.setattr(
        "api.code_agent.routes.installed_opencode_version",
        lambda _path: "1.18.5",
    )
    monkeypatch.setattr("api.code_agent.routes.manager.instances", lambda: [])

    result = await code_agent_update(CodeAgentUpdateRequest(version="pinned"))

    assert result == {
        "status": "ok",
        "version": "1.18.5",
        "pending_restart": 0,
    }


@pytest.mark.asyncio
async def test_code_http_authorization_uses_shared_secret(protected_config):
    with pytest.raises(HTTPException) as exc:
        await _require_code_authorization(None)
    assert exc.value.status_code == 401
    with pytest.raises(HTTPException):
        await _require_code_authorization("wrong")
    assert await _require_code_authorization("correct-horse") is True


class _FakeWebSocket:
    def __init__(self, code=None, origin=None, host="localhost:8001"):
        self.query_params = {"authorization_code": code} if code else {}
        self.headers = {"host": host}
        if origin:
            self.headers["origin"] = origin
        self.closed = None

    async def close(self, code=1000):
        self.closed = code


@pytest.mark.asyncio
async def test_code_websocket_requires_shared_authorization(protected_config):
    missing = _FakeWebSocket()
    assert await _authorize_websocket(missing) is False
    assert missing.closed == 1008

    valid = _FakeWebSocket(code="correct-horse")
    assert await _authorize_websocket(valid) is True
    assert valid.closed is None


@pytest.mark.asyncio
async def test_code_websocket_rejects_cross_site_origin(protected_config):
    cross_site = _FakeWebSocket(
        code="correct-horse",
        origin="https://attacker.example",
    )
    assert await _authorize_websocket(cross_site) is False
    assert cross_site.closed == 1008

    same_host = _FakeWebSocket(
        code="correct-horse",
        origin="http://localhost:3000",
    )
    assert await _authorize_websocket(same_host) is True


def test_code_error_sanitizer_redacts_paths_and_credentials():
    message = "failed /home/alice/private/repo with sk-abcdefghijklmnop1234"
    sanitized = sanitize_error_message(message)
    assert "/home/alice" not in sanitized
    assert "sk-abcdefghijklmnop1234" not in sanitized
