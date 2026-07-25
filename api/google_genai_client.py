"""AdalFlow-compatible Gemini client built on the maintained ``google.genai`` SDK."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from adalflow.core.model_client import ModelClient
from adalflow.core.types import CompletionUsage, GeneratorOutput, ModelType
from google import genai
from google.genai import types


class GoogleGenAIClient(ModelClient):
    """Minimal ModelClient adapter used by HackDeepWiki's provider config.

    Streaming chat uses ``api.provider_streaming`` directly; this adapter
    preserves compatibility for any AdalFlow Generator consumer without
    importing the retired legacy Google SDK.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        env_api_key_name: str = "GOOGLE_API_KEY",
    ) -> None:
        super().__init__()
        key = api_key or os.environ.get(env_api_key_name)
        if not key:
            raise ValueError(f"Environment variable {env_api_key_name} must be set")
        self.sync_client = genai.Client(api_key=key)
        self.async_client = self.sync_client.aio

    def convert_inputs_to_api_kwargs(
        self,
        input: Optional[Any] = None,
        model_kwargs: Optional[Dict] = None,
        model_type: ModelType = ModelType.UNDEFINED,
    ) -> Dict:
        if model_type != ModelType.LLM:
            raise ValueError("GoogleGenAIClient only supports LLM model type")
        kwargs = dict(model_kwargs or {})
        model = kwargs.pop("model", "gemini-2.5-flash")
        config = types.GenerateContentConfig(**kwargs)
        return {"model": model, "contents": input or "", "config": config}

    def call(
        self,
        api_kwargs: Optional[Dict] = None,
        model_type: ModelType = ModelType.UNDEFINED,
    ):
        if model_type != ModelType.LLM:
            raise ValueError("GoogleGenAIClient only supports LLM model type")
        return self.sync_client.models.generate_content(**dict(api_kwargs or {}))

    async def acall(
        self,
        api_kwargs: Optional[Dict] = None,
        model_type: ModelType = ModelType.UNDEFINED,
    ):
        if model_type != ModelType.LLM:
            raise ValueError("GoogleGenAIClient only supports LLM model type")
        return await self.async_client.models.generate_content(**dict(api_kwargs or {}))

    def track_completion_usage(self, completion: Any) -> CompletionUsage:
        usage = getattr(completion, "usage_metadata", None)
        return CompletionUsage(
            prompt_tokens=getattr(usage, "prompt_token_count", None),
            completion_tokens=getattr(usage, "candidates_token_count", None),
            total_tokens=getattr(usage, "total_token_count", None),
        )

    def parse_chat_completion(self, completion: Any) -> GeneratorOutput:
        try:
            text = completion.text or ""
            return GeneratorOutput(
                data=text,
                usage=self.track_completion_usage(completion),
                raw_response=text,
                api_response=completion,
            )
        except Exception as exc:  # noqa: BLE001
            return GeneratorOutput(
                data=None,
                error=str(exc),
                api_response=completion,
            )
