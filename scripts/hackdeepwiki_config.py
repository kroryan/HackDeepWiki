#!/usr/bin/env python3
"""Build an Ollama-first runtime config from the current upstream config."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def model_name(model: dict[str, Any]) -> str:
    return str(model.get("name") or model.get("model") or "").strip()


def classify_models(
    models: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    completion: list[str] = []
    embedding: list[str] = []

    for model in models:
        name = model_name(model)
        if not name:
            continue

        capabilities = set(model.get("capabilities") or [])
        if "completion" in capabilities:
            completion.append(name)
        if "embedding" in capabilities:
            embedding.append(name)

        # Compatibility with Ollama versions that did not expose capabilities.
        if not capabilities:
            family = str(model.get("details", {}).get("family") or "").lower()
            if "embed" in name.lower() or family in {"nomic-bert", "bert"}:
                embedding.append(name)
            else:
                completion.append(name)

    return list(dict.fromkeys(completion)), list(dict.fromkeys(embedding))


def discover_ollama_models(endpoint: str) -> tuple[list[str], list[str]]:
    url = f"{endpoint.rstrip('/')}/api/tags"
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "HackDeepWiki"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Could not read Ollama models from {url}: {exc}") from exc

    models = payload.get("models", [])
    if not isinstance(models, list):
        raise SystemExit(f"Invalid Ollama response from {url}: 'models' is not a list")

    completion, embedding = classify_models(models)
    if not completion:
        raise SystemExit(f"No completion models found at {url}")
    if not embedding:
        raise SystemExit(
            f"No embedding models found at {url}. "
            "Pull an embedding model such as nomic-embed-text."
        )
    return completion, embedding


def select_model(
    models: list[str],
    preferred: str,
    kind: str,
    automatic_priority: tuple[str, ...] = (),
) -> str:
    if preferred:
        if preferred not in models:
            available = ", ".join(models)
            raise SystemExit(
                f"Requested {kind} model '{preferred}' is not available. "
                f"Available models: {available}"
            )
        return preferred
    for candidate in automatic_priority:
        if candidate in models:
            return candidate
        candidate_base = candidate.split(":", 1)[0]
        for model in models:
            if model.split(":", 1)[0] == candidate_base:
                return model
    return models[0]


def configure_generator(
    path: Path,
    models: list[str],
    preferred_model: str,
) -> str:
    config = load_json(path)
    providers = config.setdefault("providers", {})
    ollama = providers.setdefault("ollama", {})
    upstream_models = ollama.get("models", {})
    selected = select_model(models, preferred_model, "completion")

    config["default_provider"] = "ollama"
    ollama["default_model"] = selected
    ollama["supportsCustomModel"] = True
    ordered_models = [selected, *(model for model in models if model != selected)]
    ollama["models"] = {
        model: upstream_models.get(
            model,
            {
                "options": {
                    "temperature": 0.7,
                    "top_p": 0.8,
                    "num_ctx": 32000,
                }
            },
        )
        for model in ordered_models
    }
    write_json(path, config)
    return selected


def configure_embedder(
    path: Path,
    models: list[str],
    preferred_model: str,
) -> str:
    config = load_json(path)
    selected = select_model(
        models,
        preferred_model,
        "embedding",
        automatic_priority=("nomic-embed-text:latest",),
    )
    ollama = config.setdefault("embedder_ollama", {})
    ollama["client_class"] = "OllamaClient"
    ollama.setdefault("model_kwargs", {})["model"] = selected
    write_json(path, config)
    return selected


def render(
    source: Path,
    output: Path,
    ollama_endpoint: str,
    preferred_model: str = "",
    preferred_embed_model: str = "",
    discovered_models: tuple[list[str], list[str]] | None = None,
) -> tuple[str, str]:
    if not source.is_dir():
        raise SystemExit(f"Config source does not exist: {source}")

    completion_models, embedding_models = (
        discovered_models
        if discovered_models is not None
        else discover_ollama_models(ollama_endpoint)
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="config-", dir=output.parent))
    try:
        for item in source.iterdir():
            if item.is_file():
                shutil.copy2(item, temporary / item.name)

        selected_model = configure_generator(
            temporary / "generator.json",
            completion_models,
            preferred_model,
        )
        selected_embed_model = configure_embedder(
            temporary / "embedder.json",
            embedding_models,
            preferred_embed_model,
        )

        previous = output.with_name(f"{output.name}.previous")
        if previous.exists():
            shutil.rmtree(previous)
        if output.exists():
            os.replace(output, previous)
        os.replace(temporary, output)
        if previous.exists():
            shutil.rmtree(previous)
        return selected_model, selected_embed_model
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ollama-endpoint", required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--embed-model", default="")
    args = parser.parse_args()
    model, embed_model = render(
        args.source,
        args.output,
        args.ollama_endpoint,
        args.model,
        args.embed_model,
    )
    print(f"Discovered Ollama completion models; default: {model}")
    print(f"Discovered Ollama embedding models; default: {embed_model}")


if __name__ == "__main__":
    main()
