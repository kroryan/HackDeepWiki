import json
from pathlib import Path

import pytest

from scripts.deepwiki_config import classify_models, render


def read_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def test_render_preserves_upstream_and_selects_ollama(tmp_path):
    source = Path(__file__).parents[1] / "api" / "config"
    output = tmp_path / "config"
    models = (
        ["ornith:35b", "qwen3.5:latest"],
        ["nomic-embed-text:latest"],
    )

    render(
        source,
        output,
        "http://ollama.test:11434",
        discovered_models=models,
    )

    generator = read_json(output / "generator.json")
    ollama = generator["providers"]["ollama"]
    embedder = read_json(output / "embedder.json")

    assert generator["default_provider"] == "ollama"
    assert ollama["default_model"] == "ornith:35b"
    assert list(ollama["models"]) == ["ornith:35b", "qwen3.5:latest"]
    assert "google" in generator["providers"]
    assert (
        embedder["embedder_ollama"]["model_kwargs"]["model"]
        == "nomic-embed-text:latest"
    )


def test_render_can_be_repeated_with_a_different_model(tmp_path):
    source = Path(__file__).parents[1] / "api" / "config"
    output = tmp_path / "config"
    models = (
        ["first-model", "second-model"],
        ["nomic-embed-text"],
    )

    render(
        source,
        output,
        "http://ollama.test:11434",
        preferred_model="first-model",
        discovered_models=models,
    )
    render(
        source,
        output,
        "http://ollama.test:11434",
        preferred_model="second-model",
        discovered_models=models,
    )

    generator = read_json(output / "generator.json")
    assert generator["providers"]["ollama"]["default_model"] == "second-model"
    assert list(generator["providers"]["ollama"]["models"])[0] == "second-model"
    assert not output.with_name("config.previous").exists()


def test_nomic_is_the_automatic_embedding_default(tmp_path):
    source = Path(__file__).parents[1] / "api" / "config"
    output = tmp_path / "config"

    render(
        source,
        output,
        "http://ollama.test:11434",
        discovered_models=(
            ["ornith:35b"],
            ["qwen3-embedding:latest", "nomic-embed-text:latest"],
        ),
    )

    embedder = read_json(output / "embedder.json")
    assert (
        embedder["embedder_ollama"]["model_kwargs"]["model"]
        == "nomic-embed-text:latest"
    )


def test_classify_models_uses_capabilities_and_legacy_fallback():
    completion, embedding = classify_models(
        [
            {"name": "ornith:35b", "capabilities": ["completion", "tools"]},
            {"name": "qwen3-embedding", "capabilities": ["embedding"]},
            {"name": "legacy-chat", "details": {"family": "qwen"}},
            {"name": "legacy-embed", "details": {"family": "nomic-bert"}},
        ]
    )

    assert completion == ["ornith:35b", "legacy-chat"]
    assert embedding == ["qwen3-embedding", "legacy-embed"]


def test_requested_model_must_exist_at_endpoint(tmp_path):
    source = Path(__file__).parents[1] / "api" / "config"

    with pytest.raises(SystemExit, match="not available"):
        render(
            source,
            tmp_path / "config",
            "http://ollama.test:11434",
            preferred_model="missing-model",
            discovered_models=(["ornith:35b"], ["nomic-embed-text"]),
        )
