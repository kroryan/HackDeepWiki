"""Stable provider boundary used by HackDeepWiki.

Provider SDKs are intentionally imported only when a provider is selected.
This keeps optional/cloud integrations from breaking local Ollama startup and
makes the capability contract independent from any SDK.
"""

from .port import ProviderCapabilities, ProviderError, ProviderKind
from .registry import (
    CLIENT_CLASSES,
    PROVIDER_CAPABILITIES,
    LazyClientClass,
    provider_capabilities,
)

__all__ = [
    "CLIENT_CLASSES",
    "PROVIDER_CAPABILITIES",
    "LazyClientClass",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderKind",
    "provider_capabilities",
]
