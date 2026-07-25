"""HackDeepWiki's embedder, installed as Engraphis's embedder.

Engraphis's own choices are sentence-transformers (a torch download this build
never ships) or a deterministic hashing embedder that is lexical-only -- which
is what made the dashboard report "semantic search is off" and point at
launch scripts that don't exist here. api/engraphis_embedder.py exposes the
embedder RAG already uses through Engraphis's Embedder protocol instead.

No network: a fake adalflow-style embedder stands in for the provider, so the
adapter, the install and the vector migration are all exercised for real.
"""

import numpy as np
import pytest

from api.engraphis_embedder import HackDeepWikiEmbedder, build_embedder, is_enabled

DIM = 8


class _Embedding:
    def __init__(self, embedding, index):
        self.embedding = embedding
        self.index = index


class _Output:
    def __init__(self, data, error=None):
        self.data = data
        self.error = error


class _FakeProvider:
    """Stands in for an adalflow embedder: `embedder(input=...)` -> output."""

    def __init__(self, *, dim=DIM, fail=False, reverse=False, error=None):
        self.dim = dim
        self.fail = fail
        self.reverse = reverse
        self.error = error
        self.calls = []

    def __call__(self, input):  # noqa: A002 - adalflow's own keyword
        self.calls.append(input)
        if self.fail:
            raise RuntimeError("provider unreachable")
        if self.error:
            return _Output([], error=self.error)
        texts = input if isinstance(input, list) else [input]
        data = [
            _Embedding([float(len(t) + i + 1)] * self.dim, i)
            for i, t in enumerate(texts)
        ]
        if self.reverse:  # a provider that answers out of order
            data = list(reversed(data))
        return _Output(data)


def _adapter(provider, **kwargs):
    kwargs.setdefault("dim", provider.dim)
    kwargs.setdefault("model", "fake:test")
    kwargs.setdefault("single_input", False)
    kwargs.setdefault("batch_size", 64)
    return HackDeepWikiEmbedder(provider, **kwargs)


# -- the adapter ----------------------------------------------------------

def test_returns_one_normalized_row_per_text():
    embedder = _adapter(_FakeProvider())
    out = embedder.embed(["alpha", "beta", "gamma"])
    assert out.shape == (3, DIM)
    assert out.dtype == np.float32
    assert np.allclose(np.linalg.norm(out, axis=1), 1.0)


def test_empty_input_returns_an_empty_matrix():
    out = _adapter(_FakeProvider()).embed([])
    assert out.shape == (0, DIM)


def test_out_of_order_provider_results_are_reordered():
    ordered = _adapter(_FakeProvider()).embed(["a", "bbbb", "cc"])
    shuffled = _adapter(_FakeProvider(reverse=True)).embed(["a", "bbbb", "cc"])
    assert np.allclose(ordered, shuffled)


def test_batching_respects_batch_size():
    provider = _FakeProvider()
    _adapter(provider, batch_size=2).embed(["a", "b", "c", "d", "e"])
    assert [len(c) for c in provider.calls] == [2, 2, 1]


def test_ollama_style_provider_gets_one_string_per_call():
    provider = _FakeProvider()
    _adapter(provider, single_input=True).embed(["a", "b", "c"])
    assert len(provider.calls) == 3
    assert all(isinstance(c, str) for c in provider.calls)


def test_a_dead_provider_still_yields_usable_vectors():
    """A provider hiccup must never lose the memory being written."""
    embedder = _adapter(_FakeProvider(fail=True))
    out = embedder.embed(["alpha", "beta"])
    assert out.shape == (2, DIM)
    assert np.allclose(np.linalg.norm(out, axis=1), 1.0)
    assert not np.allclose(out[0], out[1])  # real (deterministic) vectors


def test_an_error_payload_is_treated_as_a_failure():
    out = _adapter(_FakeProvider(error="rate limited")).embed(["alpha"])
    assert out.shape == (1, DIM)
    assert np.linalg.norm(out[0]) > 0


def test_wrong_width_rows_fall_back_instead_of_poisoning_the_index():
    """Recall filters vectors by width; a row of the wrong width would be
    silently invisible forever."""
    embedder = _adapter(_FakeProvider(dim=DIM + 3), dim=DIM)
    out = embedder.embed(["alpha", "beta"])
    assert out.shape == (2, DIM)
    assert np.allclose(np.linalg.norm(out, axis=1), 1.0)


def test_oversized_text_is_capped_before_the_provider_sees_it():
    from api.engraphis_embedder import _MAX_TEXT_CHARS

    provider = _FakeProvider()
    _adapter(provider).embed(["x" * (_MAX_TEXT_CHARS * 3)])
    assert len(provider.calls[0][0]) == _MAX_TEXT_CHARS


def test_the_opt_out_env_disables_the_semantic_embedder(monkeypatch):
    monkeypatch.setenv("HACKDEEPWIKI_ENGRAPHIS_EMBEDDER", "0")
    assert not is_enabled()
    assert build_embedder() is None


def test_build_embedder_returns_none_when_the_provider_is_unreachable(monkeypatch):
    """Offline/misconfigured is a normal state, not a crash."""
    monkeypatch.delenv("HACKDEEPWIKI_ENGRAPHIS_EMBEDDER", raising=False)
    import api.tools.embedder as app_embedder

    def _boom(embedder_type=None, **kwargs):
        raise RuntimeError("no API key configured")

    monkeypatch.setattr(app_embedder, "get_embedder", _boom)
    assert build_embedder() is None


# -- installed into Engraphis ---------------------------------------------

@pytest.fixture
def engraphis_env(tmp_path, monkeypatch):
    pytest.importorskip("engraphis")
    monkeypatch.setenv("ENGRAPHIS_UPDATE_CHECK", "0")
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "")
    monkeypatch.setenv("HACKDEEPWIKI_DATA_DIR", str(tmp_path))

    from api import data_root
    monkeypatch.setattr(data_root, "_cached_root", None, raising=False)

    from engraphis.backends import embedder_st
    import engraphis.core.engine as engine_mod

    # _install_embedder replaces these module globals; monkeypatch restores
    # them so one test can't leak a fake embedder into the next.
    monkeypatch.setattr(embedder_st, "get_embedder", embedder_st.get_embedder)
    monkeypatch.setattr(engine_mod, "get_embedder", engine_mod.get_embedder)

    from api import engraphis_integration as eng
    monkeypatch.setattr(eng, "_service", None)
    monkeypatch.setattr(eng, "_start_error", None)
    monkeypatch.setattr(eng, "_bootstrapped", True)
    monkeypatch.setattr(eng, "_embedder_info", dict(eng._embedder_info))
    return eng


def _install_fake(eng, monkeypatch, provider=None):
    import api.engraphis_embedder as mod

    adapter = _adapter(provider or _FakeProvider())
    monkeypatch.setattr(mod, "build_embedder", lambda: adapter)
    eng._install_embedder()
    return adapter


def test_install_reports_semantic_and_replaces_the_factory(engraphis_env, monkeypatch):
    eng = engraphis_env
    adapter = _install_fake(eng, monkeypatch)

    from engraphis.backends import embedder_st
    import engraphis.core.engine as engine_mod

    # The engine binds get_embedder at import time -- patching only the
    # backend module would leave the engine on the deterministic embedder.
    assert embedder_st.get_embedder("x", 384) is adapter
    assert engine_mod.get_embedder("x", 384) is adapter
    assert eng._embedder_info["semantic"] is True
    assert eng._embedder_info["dim"] == DIM


def test_an_unreachable_embedder_leaves_engraphis_untouched(engraphis_env, monkeypatch):
    eng = engraphis_env
    import api.engraphis_embedder as mod
    from engraphis.backends import embedder_st

    before = embedder_st.get_embedder
    monkeypatch.setattr(mod, "build_embedder", lambda: None)
    eng._install_embedder()

    assert embedder_st.get_embedder is before
    assert eng._embedder_info["semantic"] is False
    assert eng._embedder_info["reason"]  # the dashboard banner explains why


def test_a_service_built_after_install_is_semantic(engraphis_env, monkeypatch, tmp_path):
    """Upstream decides the dashboard's 'semantic search off' banner purely by
    `isinstance(embedder, DeterministicEmbedder)`; installing ours flips it."""
    eng = engraphis_env
    adapter = _install_fake(eng, monkeypatch)

    from engraphis.backends.embedder_deterministic import DeterministicEmbedder
    from engraphis.service import MemoryService

    service = MemoryService.create(str(tmp_path / "sem.db"), embed_model="",
                                   embed_dim=384)
    try:
        assert service.engine.embedder is adapter
        assert not isinstance(service.engine.embedder, DeterministicEmbedder)
        # ...and the write path really uses it: the stored vector is ours.
        service.remember("the gateway issues JWT tokens",
                         workspace="acme_widgets_v1", mtype="semantic")
        widths = {r["dim"] for r in service.store.conn.execute(
            "SELECT dim FROM mem_vectors").fetchall()}
        assert widths == {DIM}
    finally:
        service.store.conn.close()


def test_stale_width_vectors_are_re_embedded(engraphis_env, monkeypatch, tmp_path):
    """Memories written under the 384-dim deterministic embedder would be
    invisible to semantic recall forever (recall filters by width)."""
    eng = engraphis_env
    from engraphis.service import MemoryService

    db = str(tmp_path / "migrate.db")
    old = MemoryService.create(db, embed_model="", embed_dim=384)
    for text in ("the gateway issues JWT tokens",
                 "the watchdog polls every 500 ms"):
        old.remember(text, workspace="acme_widgets_v1", mtype="semantic")
    widths = {r["dim"] for r in old.store.conn.execute(
        "SELECT dim FROM mem_vectors").fetchall()}
    assert widths == {384}
    old.store.conn.close()

    _install_fake(eng, monkeypatch)
    new = MemoryService.create(db, embed_model="", embed_dim=384)
    monkeypatch.setattr(eng, "_service", new)
    try:
        eng._reembed_stale_vectors()
        rows = new.store.conn.execute(
            "SELECT dim FROM mem_vectors").fetchall()
        assert rows and {r["dim"] for r in rows} == {DIM}
        # Second run is a no-op: the state file records the current width.
        eng._reembed_stale_vectors()
        assert {r["dim"] for r in new.store.conn.execute(
            "SELECT dim FROM mem_vectors").fetchall()} == {DIM}
    finally:
        new.store.conn.close()


def test_status_reports_the_embedder_to_the_dashboard(engraphis_env, monkeypatch):
    eng = engraphis_env
    _install_fake(eng, monkeypatch)
    body = eng._shim_body().decode("utf-8")
    assert "__hdwEmbedder" in body
    assert '"semantic": true' in body.replace("'", '"')
