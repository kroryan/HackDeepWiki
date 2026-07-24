"""Generating the per-repo opencode configuration.

The whole point of Code Editing mode is "zero extra setup": whatever
provider/model/key the user already selected in HackDeepWiki's chat is what
opencode runs with. This module maps HackDeepWiki's provider names onto
opencode's config schema and writes one JSON config per repo into
``DATABASE/opencode/config/`` -- NEVER an ``opencode.json`` inside the repo
clone, which would pollute the working tree and show up in the agent's own
diffs. The config is handed to the child via the ``OPENCODE_CONFIG`` env var.

The config also:
  * sets full-auto permissions (edit/bash/webfetch all "allow") -- the
    Devin-like mode the user chose; the events layer additionally
    auto-approves any residual permission request as a safety valve,
  * registers HackDeepWiki's inbound MCP server so the coding agent can
    consult the generated wiki (search_wiki / read_doc / ask_repo / ...),
  * disables sharing and self-update (the app manages the binary).
"""

import json
import logging
import os
import secrets
import sys
from typing import Optional, Tuple

from api.data_root import get_data_root

logger = logging.getLogger(__name__)


def backend_port() -> int:
    """The FastAPI port of THIS process. ``HACKDEEPWIKI_BACKEND_PORT`` is
    exported by api/main.py and scripts/launcher.py before uvicorn starts
    (the plain ``PORT`` env is overloaded: the launcher reuses it for the
    frontend in the Node child's env)."""
    for var in ("HACKDEEPWIKI_BACKEND_PORT", "PORT"):
        raw = os.environ.get(var)
        if raw:
            try:
                return int(raw)
            except ValueError:
                pass
    return 8001


def opencode_config_dir() -> str:
    return os.path.join(get_data_root(), "opencode", "config")


def opencode_home_dir() -> str:
    """Where opencode keeps its own state (sessions, caches, auth). Redirected
    into DATABASE so the AppImage stays stateless and sessions survive both
    app updates and opencode binary updates."""
    return os.path.join(get_data_root(), "opencode", "home")


def _openai_compatible_provider(provider_id: str, display_name: str, base_url: str,
                                model: str, api_key: Optional[str]) -> dict:
    """opencode provider block for any OpenAI-compatible endpoint (Ollama,
    LiteLLM, Dashscope, custom gateways)."""
    options: dict = {"baseURL": base_url}
    if api_key:
        options["apiKey"] = api_key
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": display_name,
        "options": options,
        "models": {model: {"name": model}},
    }


def map_provider(provider: str, model: str, api_key: Optional[str],
                 api_endpoint: Optional[str]) -> Tuple[dict, dict, str]:
    """Map a HackDeepWiki provider selection to opencode.

    Returns ``(provider_config, extra_env, model_ref)`` where
    ``provider_config`` goes under the config's ``"provider"`` key,
    ``extra_env`` is added to the child environment, and ``model_ref`` is the
    ``providerID/modelID`` string used both as the config default and in each
    prompt body.
    """
    provider = (provider or "").lower()
    provider_config: dict = {}
    extra_env: dict = {}

    if provider == "google":
        # opencode's Google provider reads GOOGLE_GENERATIVE_AI_API_KEY --
        # a different env name than HackDeepWiki's GOOGLE_API_KEY.
        key = api_key or os.environ.get("GOOGLE_API_KEY")
        if key:
            extra_env["GOOGLE_GENERATIVE_AI_API_KEY"] = key
        model_ref = f"google/{model}"
    elif provider == "claude":
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if key:
            extra_env["ANTHROPIC_API_KEY"] = key
        model_ref = f"anthropic/{model}"
    elif provider == "openai":
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if key:
            extra_env["OPENAI_API_KEY"] = key
        if api_endpoint:
            provider_config["openai"] = {"options": {"baseURL": api_endpoint}}
        model_ref = f"openai/{model}"
    elif provider == "openrouter":
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if key:
            extra_env["OPENROUTER_API_KEY"] = key
        model_ref = f"openrouter/{model}"
    elif provider == "ollama":
        # Same normalization as api/ollama_patch.py::normalize_ollama_host,
        # inlined because importing that module drags in adalflow just to
        # prepend a scheme.
        host = (api_endpoint or os.environ.get("OLLAMA_HOST") or "http://localhost:11434").strip()
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        base = host.rstrip("/") + "/v1"
        provider_config["ollama"] = _openai_compatible_provider(
            "ollama", "Ollama", base, model, api_key
        )
        model_ref = f"ollama/{model}"
    elif provider == "litellm":
        base = (api_endpoint or os.environ.get("LITELLM_ENDPOINT") or "http://localhost:4000").rstrip("/")
        provider_config["litellm"] = _openai_compatible_provider(
            "litellm", "LiteLLM", base, model,
            api_key or os.environ.get("LITELLM_API_KEY"),
        )
        model_ref = f"litellm/{model}"
    elif provider == "dashscope":
        base = (api_endpoint or "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
        provider_config["dashscope"] = _openai_compatible_provider(
            "dashscope", "Dashscope", base, model,
            api_key or os.environ.get("DASHSCOPE_API_KEY"),
        )
        model_ref = f"dashscope/{model}"
    elif provider == "bedrock":
        # AWS credentials already live in this process's env (api/config.py
        # re-exports them); they're inherited by the child via os.environ.
        model_ref = f"amazon-bedrock/{model}"
    elif provider == "azure":
        key = api_key or os.environ.get("AZURE_API_KEY")
        if key:
            extra_env["AZURE_API_KEY"] = key
        if api_endpoint:
            extra_env["AZURE_RESOURCE_NAME"] = api_endpoint
        model_ref = f"azure/{model}"
    elif api_endpoint:
        # Unknown provider with an explicit endpoint: assume OpenAI-compatible
        # (covers openai_custom and self-hosted gateways).
        provider_config[provider or "custom"] = _openai_compatible_provider(
            provider or "custom", provider or "Custom", api_endpoint.rstrip("/"), model, api_key
        )
        model_ref = f"{provider or 'custom'}/{model}"
    else:
        logger.warning("Unknown provider %r for code agent; passing through as-is", provider)
        model_ref = f"{provider}/{model}"

    return provider_config, extra_env, model_ref


def provider_signature(provider: str, api_key: Optional[str], api_endpoint: Optional[str]) -> str:
    """Cheap fingerprint of everything that requires an opencode RESTART when
    it changes (credentials/endpoints are read at server startup; the model
    itself is per-message and never forces a restart)."""
    return f"{(provider or '').lower()}|{api_endpoint or ''}|{'k' if api_key else ''}"


def write_opencode_config(repo_key: str, provider: str, model: str,
                          api_key: Optional[str], api_endpoint: Optional[str]) -> Tuple[str, dict, str]:
    """Write ``DATABASE/opencode/config/{repo_key}.json`` and return
    ``(config_path, extra_env, model_ref)``."""
    from api.mcp_server import get_runtime_token

    provider_config, extra_env, model_ref = map_provider(provider, model, api_key, api_endpoint)

    config: dict = {
        "$schema": "https://opencode.ai/config.json",
        "model": model_ref,
        "permission": {"edit": "allow", "bash": "allow", "webfetch": "allow"},
        "share": "disabled",
        "autoupdate": False,
        "mcp": {
            "hackdeepwiki": {
                "type": "remote",
                "url": f"http://127.0.0.1:{backend_port()}/mcp",
                "headers": {"Authorization": f"Bearer {get_runtime_token()}"},
                "enabled": True,
            }
        },
    }
    if provider_config:
        config["provider"] = provider_config

    os.makedirs(opencode_config_dir(), exist_ok=True)
    config_path = os.path.join(opencode_config_dir(), f"{repo_key}.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    return config_path, extra_env, model_ref


def build_child_env(config_path: str, extra_env: dict, server_password: str) -> dict:
    """Environment for the ``opencode serve`` child process."""
    env = os.environ.copy()
    env.update(extra_env)
    env["OPENCODE_CONFIG"] = config_path
    env["OPENCODE_SERVER_PASSWORD"] = server_password
    home = opencode_home_dir()
    os.makedirs(home, exist_ok=True)
    if sys.platform != "win32":
        # Keep opencode's own state portable inside DATABASE. On Windows we
        # deliberately do NOT override APPDATA/LOCALAPPDATA (they're
        # globally meaningful to every library the child loads); opencode
        # state then lands in the default per-user location, which is
        # acceptable -- it survives app updates either way.
        env["XDG_DATA_HOME"] = os.path.join(home, "data")
        env["XDG_CONFIG_HOME"] = os.path.join(home, "config")
        env["XDG_CACHE_HOME"] = os.path.join(home, "cache")
        env["XDG_STATE_HOME"] = os.path.join(home, "state")
    return env


def new_server_password() -> str:
    return secrets.token_urlsafe(24)
