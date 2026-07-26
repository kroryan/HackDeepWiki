from __future__ import annotations

import inspect
import re
import time

import httpx
import pytest
from fastapi.routing import APIWebSocketRoute

from api import config, mcp_client
from api.api import app
from api.network_policy import OutboundTargetRejected, validate_outbound_url
from api.routers.auth import reset_auth_rate_limit_for_tests
from api.security import (
    AUTH_HEADER,
    INTERNAL_PROXY_HEADER,
    PUBLIC_HTTP_PATHS,
    _authorization_session_is_valid,
    authorization_is_valid,
    create_authorization_session_token,
    decrypt_secret,
    encrypt_secret,
    encryption_enabled,
    origin_is_allowed,
    rotate_secret,
    sanitize_error_message,
)
from api.settings import DeploymentProfile, Settings


@pytest.fixture(autouse=True)
def _reset_auth(monkeypatch):
    reset_auth_rate_limit_for_tests()
    monkeypatch.setattr(config, "WIKI_AUTH_MODE", False)
    monkeypatch.setattr(config, "WIKI_AUTH_CODE", "")
    monkeypatch.delenv("HACKDEEPWIKI_INTERNAL_PROXY_TOKEN", raising=False)
    monkeypatch.delenv("HACKDEEPWIKI_ENC_KEY", raising=False)


def _concrete_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "test", path)


@pytest.mark.asyncio
async def test_every_non_public_openapi_operation_is_denied_without_auth(monkeypatch):
    monkeypatch.setattr(config, "WIKI_AUTH_MODE", True)
    monkeypatch.setattr(config, "WIKI_AUTH_CODE", "correct-horse-battery-staple")
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        schema = app.openapi()
        checked = 0
        for path, operations in schema["paths"].items():
            if path in PUBLIC_HTTP_PATHS:
                continue
            for method in operations:
                if method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                response = await client.request(method, _concrete_path(path))
                assert response.status_code == 401, (method, path, response.text)
                checked += 1
        assert checked >= 70


@pytest.mark.asyncio
async def test_public_auth_and_liveness_endpoints_remain_reachable(monkeypatch):
    monkeypatch.setattr(config, "WIKI_AUTH_MODE", True)
    monkeypatch.setattr(config, "WIKI_AUTH_CODE", "correct-horse-battery-staple")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/auth/status")).status_code == 200
        response = await client.post(
            "/auth/validate",
            json={"code": "correct-horse-battery-staple"},
        )
        assert response.status_code == 200
        assert response.json()["success"] is True
        assert response.json()["session_token"].startswith("hdw1.")


@pytest.mark.asyncio
async def test_signed_session_and_internal_proxy_authenticate(monkeypatch):
    monkeypatch.setattr(config, "WIKI_AUTH_MODE", True)
    monkeypatch.setattr(config, "WIKI_AUTH_CODE", "correct-horse-battery-staple")
    monkeypatch.setenv("HACKDEEPWIKI_INTERNAL_PROXY_TOKEN", "internal-only")
    token = create_authorization_session_token(
        now=int(time.time()),
        lifetime_seconds=600,
    )
    assert authorization_is_valid(token)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        by_session = await client.get(
            "/lang/config",
            headers={AUTH_HEADER: token},
        )
        by_proxy = await client.get(
            "/lang/config",
            headers={INTERNAL_PROXY_HEADER: "internal-only"},
        )
        assert by_session.status_code == 200
        assert by_proxy.status_code == 200


def test_expired_or_tampered_session_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "WIKI_AUTH_MODE", True)
    monkeypatch.setattr(config, "WIKI_AUTH_CODE", "correct-horse-battery-staple")
    token = create_authorization_session_token(
        now=1_000,
        lifetime_seconds=600,
    )
    assert _authorization_session_is_valid(
        token,
        "correct-horse-battery-staple",
        now=1_599,
    )
    assert not _authorization_session_is_valid(
        token,
        "correct-horse-battery-staple",
        now=1_600,
    )
    assert not authorization_is_valid(token[:-1] + ("a" if token[-1] != "a" else "b"))
    assert not _authorization_session_is_valid("hdw1.not-a-time.nonce.signature", "secret")
    assert not _authorization_session_is_valid("wrong.2000.nonce.signature", "secret")


def test_security_helpers_cover_local_defaults_and_redaction(monkeypatch):
    assert sanitize_error_message("") == ""
    message = "Bearer abcdefghijklmnopqrstuvwxyz /home/alice/private/file " + ("x" * 400)
    redacted = sanitize_error_message(message)
    assert "abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "/home/alice" not in redacted
    assert redacted.endswith("...")

    assert origin_is_allowed(None, "localhost:8001")
    monkeypatch.setenv("HACKDEEPWIKI_ALLOWED_ORIGINS", "https://wiki.example")
    assert origin_is_allowed("https://wiki.example/", "localhost:8001")
    assert origin_is_allowed("http://localhost:3000", "127.0.0.1:8001")

    assert not encryption_enabled()
    assert encrypt_secret("") == ""
    assert encrypt_secret("plain-local-secret") == "plain-local-secret"
    assert decrypt_secret("legacy-plain-secret") == "legacy-plain-secret"


def test_encrypted_secret_requires_key_and_rejects_malformed(monkeypatch):
    monkeypatch.setenv("HACKDEEPWIKI_ENC_KEY", "encryption-passphrase")
    encrypted = encrypt_secret("secret")
    monkeypatch.delenv("HACKDEEPWIKI_ENC_KEY")
    with pytest.raises(ValueError, match="is not set"):
        decrypt_secret(encrypted)

    monkeypatch.setenv("HACKDEEPWIKI_ENC_KEY", "encryption-passphrase")
    with pytest.raises(ValueError, match="Malformed"):
        decrypt_secret("enc:v1:not-enough-fields")
    assert rotate_secret(
        "legacy-plain-secret",
        old_passphrase="ignored",
        new_passphrase="",
    ) == "legacy-plain-secret"


@pytest.mark.asyncio
async def test_disallowed_browser_origin_is_rejected_even_when_auth_is_off():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/lang/config",
            headers={"Origin": "https://malicious.example"},
        )
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_explicit_mcp_token_is_never_echoed(monkeypatch):
    monkeypatch.setenv("HACKDEEPWIKI_MCP_TOKEN", "mcp-super-secret")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/mcp/token")
    assert response.status_code == 200
    assert response.json()["configured"] is True
    assert response.json()["token"] is None
    assert "mcp-super-secret" not in response.text


def test_every_websocket_route_has_an_authorization_guard():
    def flatten(routes):
        for route in routes:
            original = getattr(route, "original_router", None)
            if original is not None:
                yield from flatten(original.routes)
            else:
                yield route

    routes = [
        route for route in flatten(app.routes) if isinstance(route, APIWebSocketRoute)
    ]
    paths = {route.path for route in routes}
    assert paths == {
        "/ws/chat",
        "/ws/repo/clone",
        "/ws/website/crawl",
        "/ws/fanwiki/import",
        "/ws/vuln_scan",
        "/ws/web_vuln_scan",
        "/ws/code/chat",
        "/ws/code/events",
    }
    for route in routes:
        source = inspect.getsource(route.endpoint)
        assert "authorize" in source, route.path


def test_non_loopback_deployment_fails_closed_without_strong_auth():
    base = {
        "HACKDEEPWIKI_HOST": "0.0.0.0",
        "HACKDEEPWIKI_DEPLOYMENT_PROFILE": "trusted-lan",
    }
    with pytest.raises(RuntimeError, match="AUTH_MODE"):
        Settings.from_environ(base).validate_deployment()
    with pytest.raises(RuntimeError, match="at least 16"):
        Settings.from_environ(
            {
                **base,
                "HACKDEEPWIKI_AUTH_MODE": "true",
                "HACKDEEPWIKI_AUTH_CODE": "short",
            }
        ).validate_deployment()
    secure = Settings.from_environ(
        {
            **base,
            "HACKDEEPWIKI_AUTH_MODE": "true",
            "HACKDEEPWIKI_AUTH_CODE": "long-enough-auth-secret",
        }
    )
    secure.validate_deployment()
    assert secure.deployment_profile is DeploymentProfile.TRUSTED_LAN


def test_provider_secret_encryption_round_trip_and_wrong_key(monkeypatch):
    monkeypatch.setenv("HACKDEEPWIKI_ENC_KEY", "first-encryption-passphrase")
    encrypted = encrypt_secret("provider-api-key")
    assert encrypted.startswith("enc:v1:")
    assert "provider-api-key" not in encrypted
    assert decrypt_secret(encrypted) == "provider-api-key"

    monkeypatch.setenv("HACKDEEPWIKI_ENC_KEY", "different-passphrase")
    with pytest.raises(ValueError, match="Could not decrypt"):
        decrypt_secret(encrypted)


def test_secret_rotation_reencrypts_with_new_key(monkeypatch):
    monkeypatch.setenv("HACKDEEPWIKI_ENC_KEY", "first-encryption-passphrase")
    encrypted = encrypt_secret("provider-api-key")
    rotated = rotate_secret(
        encrypted,
        old_passphrase="first-encryption-passphrase",
        new_passphrase="second-encryption-passphrase",
    )
    assert rotated.startswith("enc:v1:")
    assert rotated != encrypted
    monkeypatch.setenv("HACKDEEPWIKI_ENC_KEY", "second-encryption-passphrase")
    assert decrypt_secret(rotated) == "provider-api-key"


def test_mcp_credentials_are_encrypted_and_redacted(monkeypatch, tmp_path):
    database = tmp_path / "profile.db"
    monkeypatch.setattr(mcp_client, "profile_db_path", lambda: str(database))
    monkeypatch.setenv("HACKDEEPWIKI_ENC_KEY", "mcp-encryption-passphrase")
    mcp_client.add_server(
        "private",
        "http",
        {
            "url": "https://mcp.example/rpc",
            "headers": {"Authorization": "Bearer private-token"},
        },
    )
    raw = database.read_bytes()
    assert b"private-token" not in raw
    public = mcp_client.list_servers()
    assert public[0]["config"]["headers"]["Authorization"] == "[configured]"
    internal = mcp_client.list_servers(include_secrets=True)
    assert internal[0]["config"]["headers"]["Authorization"] == (
        "Bearer private-token"
    )


def test_invalid_boolean_setting_is_rejected():
    with pytest.raises(ValueError, match="must be a boolean"):
        Settings.from_environ({"HACKDEEPWIKI_AUTH_MODE": "sometimes"})


def test_trusted_lan_egress_blocks_private_targets(monkeypatch):
    monkeypatch.setenv("HACKDEEPWIKI_DEPLOYMENT_PROFILE", "trusted-lan")
    monkeypatch.delenv("HACKDEEPWIKI_EGRESS_POLICY", raising=False)
    monkeypatch.setattr(
        "api.network_policy.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("127.0.0.1", 80)),
        ],
    )
    with pytest.raises(OutboundTargetRejected, match="Private"):
        validate_outbound_url("http://internal.example")


def test_desktop_egress_keeps_local_scanning_use_case(monkeypatch):
    monkeypatch.setenv("HACKDEEPWIKI_DEPLOYMENT_PROFILE", "desktop")
    monkeypatch.delenv("HACKDEEPWIKI_EGRESS_POLICY", raising=False)
    assert validate_outbound_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
