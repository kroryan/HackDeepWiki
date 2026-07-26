"""Provider-neutral types and network policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncIterator, Mapping, Protocol

DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_READ_TIMEOUT_SECONDS = 120.0
DEFAULT_PROVIDER_RETRIES = 3


class ProviderKind(str, Enum):
    OPENAI_COMPATIBLE = "openai-compatible"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    BEDROCK = "bedrock"
    OLLAMA = "ollama"


@dataclass(frozen=True)
class ProviderCapabilities:
    kind: ProviderKind
    streaming: bool = True
    tools: bool = False
    images: bool = False
    reasoning: bool = False
    embeddings: bool = False
    cancellation: bool = True


class ProviderError(RuntimeError):
    """Sanitized provider failure with a stable error category."""

    def __init__(self, category: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.category = category
        self.retryable = retryable


class ProviderAdapter(Protocol):
    """Minimum async interface expected from future provider adapters."""

    capabilities: ProviderCapabilities

    async def stream(
        self,
        *,
        messages: list[Mapping[str, Any]],
        model: str,
        options: Mapping[str, Any],
    ) -> AsyncIterator[str]: ...

    async def close(self) -> None: ...
