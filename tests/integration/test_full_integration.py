"""Offline integration checks for provider/embedder configuration."""

import pytest


pytestmark = pytest.mark.integration


def test_config_loading_exposes_google_clients():
    from api.config import CLIENT_CLASSES, configs

    assert "embedder_google" in configs
    assert configs["embedder_google"]
    assert "GoogleEmbedderClient" in CLIENT_CLASSES
    assert "GoogleGenAIClient" in CLIENT_CLASSES


def test_google_embedder_selection_builds_without_network(monkeypatch):
    from api.tools.embedder import get_embedder

    monkeypatch.setenv("GOOGLE_API_KEY", "offline-test-key")
    embedder = get_embedder(embedder_type="google")

    assert embedder is not None
    assert embedder.model_client.__class__.__name__ == "GoogleEmbedderClient"


def test_embedder_environment_selection_is_asserted(monkeypatch):
    import api.config as config
    from api.tools.embedder import get_embedder

    monkeypatch.setenv("GOOGLE_API_KEY", "offline-test-key")
    monkeypatch.setattr(config, "EMBEDDER_TYPE", "google")

    assert config.get_embedder_type() == "google"
    assert config.is_google_embedder() is True
    selected = config.get_embedder_config()
    assert selected["client_class"] == "GoogleEmbedderClient"
    assert get_embedder() is not None
