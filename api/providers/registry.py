"""Lazy provider registry and user-visible capability matrix."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
from threading import Lock
from typing import Any

from .port import ProviderCapabilities, ProviderKind


@dataclass
class LazyClientClass:
    """Callable class-like proxy that resolves an SDK adapter on first use.

    Configuration files historically store actual Python classes in
    ``model_client``.  Keeping ``__name__`` and call semantics preserves that
    contract while avoiding eager imports of every cloud SDK during startup.
    """

    module: str
    attribute: str
    _resolved: type[Any] | None = None
    _lock: Lock = field(default_factory=Lock)

    @property
    def __name__(self) -> str:
        return self.attribute

    def resolve(self) -> type[Any]:
        if self._resolved is None:
            with self._lock:
                if self._resolved is None:
                    module = import_module(self.module)
                    self._resolved = getattr(module, self.attribute)
        return self._resolved

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.resolve()(*args, **kwargs)

    def __repr__(self) -> str:
        return f"<lazy client {self.module}:{self.attribute}>"


def _lazy(module: str, name: str) -> LazyClientClass:
    return LazyClientClass(module, name)


CLIENT_CLASSES: dict[str, LazyClientClass] = {
    "GoogleGenAIClient": _lazy("api.google_genai_client", "GoogleGenAIClient"),
    "GoogleEmbedderClient": _lazy(
        "api.google_embedder_client", "GoogleEmbedderClient"
    ),
    "OpenAIClient": _lazy("api.openai_client", "OpenAIClient"),
    "LiteLLMClient": _lazy("api.litellm_client", "LiteLLMClient"),
    "OpenRouterClient": _lazy("api.openrouter_client", "OpenRouterClient"),
    "AnthropicClient": _lazy("api.anthropic_client", "AnthropicClient"),
    "OllamaClient": _lazy(
        "adalflow.components.model_client.ollama_client", "OllamaClient"
    ),
    "BedrockClient": _lazy("api.bedrock_client", "BedrockClient"),
    "AzureAIClient": _lazy("api.azureai_client", "AzureAIClient"),
    "DashscopeClient": _lazy("api.dashscope_client", "DashscopeClient"),
}


PROVIDER_CAPABILITIES: dict[str, ProviderCapabilities] = {
    "openai": ProviderCapabilities(
        ProviderKind.OPENAI_COMPATIBLE,
        tools=True,
        images=True,
        reasoning=True,
        embeddings=True,
    ),
    "openai_custom": ProviderCapabilities(
        ProviderKind.OPENAI_COMPATIBLE, tools=True, embeddings=True
    ),
    "openrouter": ProviderCapabilities(
        ProviderKind.OPENAI_COMPATIBLE, tools=True, images=True, reasoning=True
    ),
    "litellm": ProviderCapabilities(
        ProviderKind.OPENAI_COMPATIBLE, tools=True, images=True, reasoning=True
    ),
    "azure": ProviderCapabilities(
        ProviderKind.OPENAI_COMPATIBLE,
        tools=True,
        images=True,
        reasoning=True,
        embeddings=True,
    ),
    "claude": ProviderCapabilities(
        ProviderKind.ANTHROPIC, tools=True, images=True, reasoning=True
    ),
    "google": ProviderCapabilities(
        ProviderKind.GOOGLE,
        tools=True,
        images=True,
        reasoning=True,
        embeddings=True,
    ),
    "bedrock": ProviderCapabilities(
        ProviderKind.BEDROCK, tools=True, images=True, embeddings=True
    ),
    "ollama": ProviderCapabilities(
        ProviderKind.OLLAMA, tools=True, reasoning=True, embeddings=True
    ),
    "dashscope": ProviderCapabilities(
        ProviderKind.OPENAI_COMPATIBLE, tools=True, reasoning=True
    ),
}


def provider_capabilities(provider: str) -> ProviderCapabilities:
    try:
        return PROVIDER_CAPABILITIES[provider.lower()]
    except KeyError as exc:
        raise ValueError(f"Unknown provider: {provider}") from exc
