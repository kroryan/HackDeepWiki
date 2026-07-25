"""Real semantic embeddings for Engraphis, reusing HackDeepWiki's own embedder.

Engraphis ships two embedding backends: sentence-transformers (a torch/model
download we deliberately never bundle -- the AppImage/PyInstaller build stays
lean and offline) and ``DeterministicEmbedder``, a dependency-free hashing
embedder. The deterministic one is NOT semantic: it only captures literal
token/trigram overlap, so recalling "how do we authenticate users?" never
matches a memory phrased "login flow uses JWT". That is exactly why the
dashboard shows its "semantic search is off -- re-launch scripts/..." banner,
whose instructions don't even apply to HackDeepWiki (there is no
``launch_dashboard.ps1`` here, and we never download models at runtime).

HackDeepWiki already *has* a configured, working text embedder: the one RAG
uses to index every repository (``api/config/embedder.json`` -- OpenAI,
Ollama, Google or Bedrock). This module exposes it through Engraphis's
``Embedder`` protocol (``.dim`` + ``.embed(texts, kind=...)``) so memory gets
the same real semantic space the wiki search already runs on -- no extra
dependency, no model download, no second configuration surface.

``build_embedder()`` probes the configured embedder once. If it can't be
reached (no API key, Ollama not running, offline) it returns None and
Engraphis keeps its deterministic embedder: memory still works (lexical +
graph recall) and the dashboard says so honestly instead of pointing at
scripts that don't exist. Set ``HACKDEEPWIKI_ENGRAPHIS_EMBEDDER=0`` to force
that offline behaviour.

Stdlib + numpy + the app's existing embedder stack. No new dependency.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# One probe string, embedded once at startup to learn the real vector width
# (providers disagree: 256 for text-embedding-3-small as configured here, 768
# for nomic-embed-text, 1024 for titan-v2...). Cheaper and more honest than
# trusting a configured number that may not match what the endpoint returns.
_PROBE_TEXT = "hackdeepwiki engraphis embedder probe"

# Per-text cap. Memories are short by construction (chat exchanges, wiki page
# summaries, commit batches), but a runaway one must never blow a provider's
# per-input token limit and take the whole write down with it.
_MAX_TEXT_CHARS = 8000

# Upper bound on how many texts go in one provider request. The configured
# RAG batch (500 for OpenAI) is sized for indexing a whole repo; memory embeds
# a handful of texts at a time, and a smaller batch keeps a failed request
# cheap to retry.
_MAX_BATCH = 64


class HackDeepWikiEmbedder:
    """Engraphis ``Embedder`` backed by HackDeepWiki's configured embedder.

    Contract expected by ``engraphis.core.engine``:
      * ``.dim`` -> int
      * ``.embed(texts: list[str], *, kind="text") -> np.ndarray[float32]``,
        one L2-normalized row per input text, in input order.

    A provider hiccup must never lose a memory: when a batch fails, the rows
    for that batch fall back to Engraphis's deterministic vectors (same dim),
    so the write still succeeds and lexical/graph recall is unaffected.
    """

    def __init__(self, embedder: Any, *, dim: int, model: str,
                 single_input: bool, batch_size: int) -> None:
        self._embedder = embedder
        self._dim = int(dim)
        self.model = model
        self._single_input = bool(single_input)
        self._batch_size = max(1, int(batch_size))
        self._lock = threading.Lock()
        self._failures = 0
        self._fallback = None

    # -- Engraphis Embedder protocol ---------------------------------------

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str], *, kind: str = "text") -> Any:
        import numpy as np

        items = [str(t or "")[:_MAX_TEXT_CHARS] for t in (texts or [])]
        if not items:
            return np.zeros((0, self._dim), dtype=np.float32)

        out = np.zeros((len(items), self._dim), dtype=np.float32)
        step = 1 if self._single_input else self._batch_size
        for start in range(0, len(items), step):
            chunk = items[start:start + step]
            try:
                vectors = self._embed_chunk(chunk)
            except Exception as e:  # noqa: BLE001 - degrade, never lose the write
                self._note_failure(e)
                vectors = self._fallback_vectors(chunk, kind)
            for i, vec in enumerate(vectors):
                row = np.asarray(vec, dtype=np.float32).ravel()
                if row.shape[0] != self._dim:
                    # A provider that changes width mid-flight would silently
                    # poison the index (recall filters by dim); use the
                    # deterministic vector for this row instead.
                    row = self._fallback_vectors([chunk[i]], kind)[0]
                norm = float(np.linalg.norm(row))
                if norm > 0:
                    row = row / norm
                out[start + i] = row
        return out

    # -- internals ----------------------------------------------------------

    def _embed_chunk(self, chunk: list[str]) -> list[Any]:
        """One provider call for up to ``batch_size`` texts (one for Ollama,
        whose embeddings endpoint only accepts a single string)."""
        if self._single_input:
            from api.ollama_patch import prepare_ollama_embedding_query
            payload: Any = prepare_ollama_embedding_query(chunk[0])
        else:
            payload = chunk
        with self._lock:
            output = self._embedder(input=payload)
        return _vectors_from_output(output, expected=len(chunk))

    def _fallback_vectors(self, chunk: list[str], kind: str) -> Any:
        if self._fallback is None:
            from engraphis.backends.embedder_deterministic import DeterministicEmbedder
            self._fallback = DeterministicEmbedder(self._dim)
        safe_kind = kind if kind in ("text", "code") else "text"
        return self._fallback.embed(list(chunk), kind=safe_kind)

    def _note_failure(self, error: Exception) -> None:
        self._failures += 1
        # First failure and then every 20th: a provider that is down would
        # otherwise fill the log with one line per memory written.
        if self._failures == 1 or self._failures % 20 == 0:
            logger.warning(
                "Engraphis embedding via the HackDeepWiki embedder failed "
                "(%d so far); falling back to deterministic vectors for this "
                "batch: %s: %s", self._failures, type(error).__name__, error,
            )


def _vectors_from_output(output: Any, *, expected: int) -> list[Any]:
    """Pull the raw vectors out of an adalflow ``EmbedderOutput``.

    ``output.data`` is a list of ``Embedding(embedding=[...], index=i)``; the
    index is honored because a provider may return them out of order.
    """
    error = getattr(output, "error", None)
    if error:
        raise RuntimeError(str(error))
    data = getattr(output, "data", None)
    if data is None and isinstance(output, (list, tuple)):
        data = output
    if not data:
        raise RuntimeError("embedder returned no data")
    ordered: list[Any] = [None] * len(data)
    for position, item in enumerate(data):
        vector = getattr(item, "embedding", None)
        if vector is None and isinstance(item, dict):
            vector = item.get("embedding")
        if vector is None:
            vector = item  # already a bare vector
        index = getattr(item, "index", None)
        if not isinstance(index, int) or not (0 <= index < len(data)):
            index = position
        ordered[index] = vector
    ordered = [v for v in ordered if v is not None]
    if len(ordered) != expected:
        raise RuntimeError(
            f"embedder returned {len(ordered)} vectors for {expected} inputs"
        )
    return ordered


def is_enabled() -> bool:
    """False when the operator opted out via HACKDEEPWIKI_ENGRAPHIS_EMBEDDER."""
    value = (os.environ.get("HACKDEEPWIKI_ENGRAPHIS_EMBEDDER") or "").strip().lower()
    return value not in {"0", "off", "false", "no", "none"}


def build_embedder() -> Optional[HackDeepWikiEmbedder]:
    """Build + probe the adapter, or return None to keep Engraphis offline.

    Never raises: every failure path (missing config, no API key, provider
    unreachable) returns None so ``_install_embedder`` simply leaves the
    deterministic embedder in place.
    """
    if not is_enabled():
        logger.info("Engraphis semantic embedder disabled by "
                    "HACKDEEPWIKI_ENGRAPHIS_EMBEDDER")
        return None
    try:
        from api.config import get_embedder_config, get_embedder_type
        from api.tools.embedder import get_embedder as build_app_embedder
    except Exception as e:  # noqa: BLE001
        logger.info("Engraphis semantic embedder unavailable (config import "
                    "failed): %s: %s", type(e).__name__, e)
        return None

    try:
        embedder_type = (get_embedder_type() or "openai").lower()
        config = get_embedder_config() or {}
        model_kwargs = config.get("model_kwargs") or {}
        model = str(model_kwargs.get("model") or embedder_type)
        batch_size = min(int(config.get("batch_size") or _MAX_BATCH), _MAX_BATCH)
        app_embedder = build_app_embedder(embedder_type=embedder_type)
        adapter = HackDeepWikiEmbedder(
            app_embedder, dim=1, model=f"{embedder_type}:{model}",
            # Ollama's /api/embeddings takes exactly one string per call --
            # the same restriction api/rag.py works around for its queries.
            single_input=(embedder_type == "ollama"),
            batch_size=batch_size,
        )
        vectors = adapter._embed_chunk([_PROBE_TEXT])
        dim = len(list(vectors[0]))
        if dim <= 0:
            raise RuntimeError("embedder returned an empty vector")
        adapter._dim = dim
    except Exception as e:  # noqa: BLE001 - offline/misconfigured is normal
        logger.info(
            "Engraphis will use offline deterministic embeddings: the "
            "configured HackDeepWiki embedder is not reachable (%s: %s)",
            type(e).__name__, e,
        )
        return None

    logger.info("Engraphis semantic embeddings enabled via the HackDeepWiki "
                "embedder (%s, dim=%d)", adapter.model, adapter.dim)
    return adapter
