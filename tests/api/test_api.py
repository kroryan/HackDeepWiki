"""In-process API smoke tests; no manually running server or fixed port."""

import httpx
import pytest

from api.api import app


pytestmark = pytest.mark.integration


@pytest.fixture
def asgi_transport():
    return httpx.ASGITransport(app=app)


@pytest.mark.asyncio
async def test_health_endpoint(asgi_transport):
    async with httpx.AsyncClient(
        transport=asgi_transport,
        base_url="http://testserver",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_chat_endpoint_rejects_invalid_payload(asgi_transport):
    async with httpx.AsyncClient(
        transport=asgi_transport,
        base_url="http://testserver",
    ) as client:
        response = await client.post("/chat/completions/stream", json={})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(error["loc"][-1] == "repo_url" for error in detail)
    assert any(error["loc"][-1] == "messages" for error in detail)


@pytest.mark.asyncio
async def test_code_agent_route_is_registered(asgi_transport):
    async with httpx.AsyncClient(
        transport=asgi_transport,
        base_url="http://testserver",
    ) as client:
        response = await client.post("/api/code/session", json={})

    assert response.status_code == 422
