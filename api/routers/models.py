"""Language and provider-model discovery routes."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field

from api.config import configs
from api.models import Model, ModelConfig, Provider
from api.network_policy import validate_outbound_url
from api.security import sanitize_error_message

logger = logging.getLogger(__name__)
router = APIRouter(tags=["models"])


class ModelProbeRequest(BaseModel):
    endpoint: str = Field(..., description="Base URL of the provider endpoint")
    api_key: str | None = Field(None, description="API key (optional for Ollama)")
    provider_type: str = Field("openai", pattern="^(openai|ollama)$")


@router.get("/lang/config")
async def language_config() -> dict:
    return configs["lang_config"]


@router.get("/models/config", response_model=ModelConfig)
async def model_config() -> ModelConfig:
    """Return configured providers plus well-known fallback providers."""
    try:
        providers: list[Provider] = []
        default_provider = configs.get("default_provider", "google")
        for provider_id, provider_config in configs["providers"].items():
            providers.append(
                Provider(
                    id=provider_id,
                    name=provider_id.capitalize(),
                    supportsCustomModel=provider_config.get(
                        "supportsCustomModel", False
                    ),
                    models=[
                        Model(id=model_id, name=model_id)
                        for model_id in provider_config["models"]
                    ],
                )
            )

        existing_ids = {provider.id for provider in providers}
        standard_providers = (
            (
                "google",
                "Google Gemini",
                (
                    "gemini-2.5-flash",
                    "gemini-2.5-pro",
                    "gemini-1.5-pro",
                    "gemini-1.5-flash",
                ),
            ),
            (
                "openai",
                "OpenAI / Codex",
                ("gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"),
            ),
            (
                "claude",
                "Anthropic Claude",
                (
                    "claude-3-7-sonnet-latest",
                    "claude-3-5-sonnet-latest",
                    "claude-3-opus-latest",
                    "claude-3-haiku-20240307",
                ),
            ),
            ("openai_custom", "Custom OpenAI-compatible", ()),
        )
        for provider_id, name, models in standard_providers:
            if provider_id not in existing_ids:
                providers.append(
                    Provider(
                        id=provider_id,
                        name=name,
                        supportsCustomModel=True,
                        models=[Model(id=item, name=item) for item in models],
                    )
                )
        return ModelConfig(
            providers=providers,
            defaultProvider=default_provider,
        )
    except Exception:
        logger.exception("Could not build provider-model configuration")
        return ModelConfig(
            providers=[
                Provider(
                    id="google",
                    name="Google",
                    supportsCustomModel=True,
                    models=[
                        Model(
                            id="gemini-2.5-flash",
                            name="Gemini 2.5 Flash",
                        )
                    ],
                )
            ],
        )


def _is_local_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return (
        host in {"localhost", "0.0.0.0", "::1"}
        or host.startswith("127.")
        or host.startswith("192.168.")
        or host.startswith("10.")
    )


def _endpoint_candidates(value: str) -> list[str]:
    endpoint = value.rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        probe = f"http://{endpoint}"
        endpoint = f"{'http' if _is_local_host(probe) else 'https'}://{endpoint}"
    result = [endpoint]
    if endpoint.startswith("https://") and _is_local_host(endpoint):
        result.append(f"http://{endpoint[len('https://'):]}")
    return result


@router.post("/models/probe")
async def probe_models(request: ModelProbeRequest) -> dict:
    """Probe a custom endpoint with bounded I/O and deployment egress policy."""
    candidates = _endpoint_candidates(request.endpoint)
    headers = {"Accept": "application/json"}
    if request.api_key:
        headers["Authorization"] = f"Bearer {request.api_key}"

    models: list[dict[str, str]] = []
    last_error: Exception | None = None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0)
        ) as client:
            if request.provider_type == "ollama":
                urls = [
                    f"{base[:-3] if base.endswith('/v1') else base}/api/tags"
                    for base in candidates
                ]
            else:
                urls = [
                    url
                    for base in candidates
                    for url in (f"{base}/models", f"{base}/v1/models")
                ]

            for url in urls:
                try:
                    validate_outbound_url(url)
                    response = await client.get(url, headers=headers)
                    response.raise_for_status()
                    data = response.json()
                    if request.provider_type == "ollama":
                        models = [
                            {"id": item["name"], "name": item["name"]}
                            for item in data.get("models", [])
                        ]
                    else:
                        models = [
                            {
                                "id": item["id"],
                                "name": item.get("name", item["id"]),
                            }
                            for item in data.get("data", [])
                        ]
                    if models:
                        break
                except Exception as exc:
                    last_error = exc

        if models:
            return {"models": models}
        detail = (
            sanitize_error_message(str(last_error))
            if last_error is not None
            else "Endpoint returned no models"
        )
        return {"models": [], "error": detail}
    except Exception as exc:
        logger.warning("Model probe failed: %s", exc)
        return {"models": [], "error": sanitize_error_message(str(exc))}
