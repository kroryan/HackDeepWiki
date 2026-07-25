"""Offline contract tests for the maintained Google GenAI embedder adapter."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adalflow.core.types import ModelType

from api.google_embedder_client import GoogleEmbedderClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "offline-test-key")
    sdk = MagicMock()
    sdk.aio.models.embed_content = AsyncMock()
    with patch("api.google_embedder_client.genai.Client", return_value=sdk) as factory:
        instance = GoogleEmbedderClient()
    factory.assert_called_once_with(api_key="offline-test-key")
    return instance, sdk


def test_single_embedding_uses_google_genai_request_shape(client):
    embedder, sdk = client
    response = SimpleNamespace(
        embeddings=[SimpleNamespace(values=[0.1, 0.2, 0.3])]
    )
    sdk.models.embed_content.return_value = response
    api_kwargs = embedder.convert_inputs_to_api_kwargs(
        input="Hello world",
        model_kwargs={
            "model": "gemini-embedding-001",
            "task_type": "SEMANTIC_SIMILARITY",
        },
        model_type=ModelType.EMBEDDER,
    )

    raw = embedder.call(api_kwargs, ModelType.EMBEDDER)
    parsed = embedder.parse_embedding_response(raw)

    request = sdk.models.embed_content.call_args.kwargs
    assert request["model"] == "gemini-embedding-001"
    assert request["contents"] == "Hello world"
    assert request["config"].task_type == "SEMANTIC_SIMILARITY"
    assert parsed.error is None
    assert parsed.data[0].embedding == [0.1, 0.2, 0.3]


def test_batch_embedding_preserves_order(client):
    embedder, sdk = client
    sdk.models.embed_content.return_value = SimpleNamespace(
        embeddings=[
            SimpleNamespace(values=[1.0, 2.0]),
            SimpleNamespace(values=[3.0, 4.0]),
        ]
    )
    api_kwargs = embedder.convert_inputs_to_api_kwargs(
        input=["first", "second"],
        model_kwargs={"model": "gemini-embedding-001"},
        model_type=ModelType.EMBEDDER,
    )

    parsed = embedder.parse_embedding_response(
        embedder.call(api_kwargs, ModelType.EMBEDDER)
    )

    assert sdk.models.embed_content.call_args.kwargs["contents"] == [
        "first",
        "second",
    ]
    assert [item.embedding for item in parsed.data] == [
        [1.0, 2.0],
        [3.0, 4.0],
    ]


@pytest.mark.asyncio
async def test_async_embedding_uses_native_async_sdk(client):
    embedder, sdk = client
    expected = SimpleNamespace(
        embeddings=[SimpleNamespace(values=[0.5, 0.6])]
    )
    sdk.aio.models.embed_content.return_value = expected
    api_kwargs = embedder.convert_inputs_to_api_kwargs(
        input="async",
        model_kwargs={"model": "gemini-embedding-001"},
        model_type=ModelType.EMBEDDER,
    )

    actual = await embedder.acall(api_kwargs, ModelType.EMBEDDER)

    assert actual is expected
    sdk.aio.models.embed_content.assert_awaited_once()


def test_rejects_non_embedding_model_type(client):
    embedder, _ = client
    with pytest.raises(ValueError, match="EMBEDDER"):
        embedder.call({}, ModelType.LLM)
