"""Engraphis integration -- durable, scoped, explainable memory for the chat
and the code-editing agent, plus the Engraphis web dashboard embedded inside
HackDeepWiki's own UI.

Engraphis (https://github.com/Coding-Dev-Tools/engraphis, Apache-2.0) is a
local-first memory engine: one SQLite file, offline hybrid recall (vector +
lexical + graph), native workspace -> repo -> session scoping. It is an
OPTIONAL dependency: everything in this module degrades to a clean
"unavailable" state when the package is not importable, and nothing here may
ever raise into a chat or code session.

Portability contract (the same rules the rest of the app follows):

* ALL Engraphis state lives under ``DATABASE/engraphis/`` -- the DB file via
  ``ENGRAPHIS_DB_PATH`` and its private state dir via ``ENGRAPHIS_STATE_DIR``.
  Nothing is ever written to the user's home directory.
* ``ENGRAPHIS_UPDATE_CHECK=0`` -- zero network activity at runtime.
* ``ENGRAPHIS_EMBED_MODEL=""`` -- the explicit offline opt-out: Engraphis never
  downloads a sentence-transformers model. Semantic search is instead provided
  by HackDeepWiki's OWN configured embedder (the one RAG indexes repos with),
  installed over Engraphis's factory by ``_install_embedder`` -- see
  ``api/engraphis_embedder.py``. When that embedder isn't reachable (no API
  key, Ollama down, offline) Engraphis keeps its deterministic embedder and
  recall stays functional (lexical + graph + hashed vectors).
* The env vars are written BEFORE the first ``import engraphis`` anywhere in
  the process (engraphis.config.settings caches them at import time), which is
  why every import in this module is lazy and goes through ``_bootstrap_env``.

Memory scoping (per product decision): memory is NEVER global. Each wiki
release gets its own hard-isolated Engraphis workspace,
``{owner}_{repo}_v{N}``, shared by the chat and the code editor for that
release only. A second per-repo workspace, ``{owner}_{repo}_evolution``,
accumulates what changed BETWEEN releases (commit ranges, messages, files)
and is browsed from the wiki header's Engraphis evolution button.

The embedded web UI: Engraphis's own dashboard (a vendored, air-gapped SPA
served by ``engraphis.dashboard_app``) runs on its own loopback port inside
THIS process (a second uvicorn in a daemon thread -- same pattern as the
launcher running the backend itself). Their app sends ``X-Frame-Options:
DENY``; we must not fork their repo, so ``_EmbeddableDashboard`` wraps their
ASGI app and (a) strips that header, (b) serves a tiny same-origin shim
(CSP-compliant with their ``script-src 'self'``) that deep-links a workspace
via ``/?ws=<name>``, and (c) injects the shim <script> tag into index.html.
The CSP itself is relaxed only for frame-ancestors, via the ENGRAPHIS_CSP
override their http_security module explicitly supports.
"""
from __future__ import annotations

import logging
import math
import os
import re
import socket
import subprocess
import threading
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Serialize access to the shared MemoryService from our own call sites (the
# dashboard's own request handling runs in uvicorn's threadpool and is the
# package's problem; ours funnel through here).
_LOCK = threading.RLock()

_state_lock = threading.Lock()
_bootstrapped = False
_service = None            # engraphis.service.MemoryService (shared instance)
_dashboard_thread: Optional[threading.Thread] = None
_dashboard_server = None   # uvicorn.Server, so shutdown() can stop it
_dashboard_port: Optional[int] = None
_start_error: Optional[str] = None

# What the semantic-search layer actually ended up being, surfaced to the
# dashboard banner (see _embedder_payload) and to status(). Populated by
# _install_embedder before the engine is ever built.
_embedder_info: dict[str, Any] = {
    "semantic": False, "model": "", "dim": 0, "reason": "not started",
}

# Bounds for the one-time re-embed that runs when the vector width changes
# (switching embedder provider, or going deterministic -> semantic). Recall
# filters candidates by vector dim, so stale-width vectors are invisible to
# vector search until they're rewritten; this walks them in the background.
_REEMBED_BATCH = 32
_MAX_REEMBED = 5000

# Keep proactive/recall injections bounded so memory can never crowd out the
# actual question/context in a small-context model.
_MAX_INJECTED_MEMORIES = 6
_MAX_INJECTED_CHARS = 2400
_MAX_EVOLUTION_COMMITS = 40

# Full-history backfill (first time we ever see a repo in the evolution
# workspace). Commits are grouped into one memory per batch so the graph gets
# real nodes (authors, files, subjects) without writing one row per commit on a
# 10k-commit repo.
_HISTORY_BATCH = 25
_MAX_HISTORY_COMMITS = 2000

# Progress checkpoints: how the project ACTUALLY moved, sampled along its
# history instead of only listed commit by commit. The stride scales with the
# repo so the arc always has roughly _CHECKPOINT_TARGET steps -- a checkpoint
# every 10 commits for a 200-commit project, every 20 at 400, every 30 at 600
# -- which keeps the cost flat (a couple of local git calls per checkpoint, no
# model call at all) whatever the repo's size.
_CHECKPOINT_TARGET = 20
_MIN_CHECKPOINT_STRIDE = 10
_MAX_CHECKPOINT_STRIDE = 100
_MAX_CHECKPOINTS = 24
_MAX_CHECKPOINT_LINES = 14
_MAX_CHURN_LINES = 6
# git's canonical empty tree: the "before" side of the very first checkpoint,
# so the initial window is diffed against nothing instead of being skipped.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# Wiki-content ingest: one memory per documented area, bounded so a huge wiki
# can't balloon the DB or the graph.
_MAX_WIKI_PAGES = 60
_MAX_WIKI_PAGE_CHARS = 1800

# CSP mirroring engraphis.http_security.DEFAULT_CSP, with the single change
# that makes embedding possible: frame-ancestors allows the HackDeepWiki
# frontend (loopback, any port -- both ports are chosen dynamically at
# launch). Everything else stays as strict as upstream ships it.
_EMBED_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "script-src-attr 'none'",
    "worker-src 'self'",
    "style-src 'self'",
    "style-src-attr 'none'",
    "font-src 'self'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "frame-ancestors 'self' http://localhost:* http://127.0.0.1:*",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
])

# Same-origin shim served at /hackdeepwiki-shim.js and injected into the
# dashboard's index.html. dashboard.js declares setWS/loadOverview/navTo as
# top-level `function`s (window properties), so the shim can drive the SPA's
# own workspace switcher once it has loaded. `?ws=` deep-linking is what lets
# the sidebar button open THIS wiki release's memory and the header button
# open the evolution workspace, instead of whatever workspace has the most
# memories (the SPA's default). `?view=` additionally picks the SPA route, so
# HackDeepWiki can land straight on the knowledge graph.
_SHIM_JS = b"""\
(function () {
  'use strict';
  var ws = null, view = null;
  try {
    var q = new URLSearchParams(window.location.search);
    ws = q.get('ws');
    view = q.get('view');
  } catch (e) {}

  // --- Honest semantic-search banner ------------------------------------
  // Upstream's banner says "close the dashboard window and re-launch
  // scripts/launch_dashboard.ps1 ... it installs the model automatically",
  // and prints the self-contradictory "loaded at N-dim but your memories are
  // 384-dim". None of that applies here: HackDeepWiki has no such script and
  // never downloads models at runtime -- semantic search comes from the
  // embedder configured for the app itself. Replace the message (built as
  // DOM, no innerHTML, so it stays inside the strict dashboard CSP) with
  // what is actually actionable, and only show it when semantic search is
  // genuinely off.
  function hdwSemBanner(sb) {
    var info = window.__hdwEmbedder || {};
    var notice = document.createElement('div');
    notice.className = 'system-notice';
    var details = document.createElement('details');
    var summary = document.createElement('summary');
    var title = document.createElement('strong');
    title.textContent = 'Semantic search is off';
    var brief = document.createElement('span');
    brief.className = 'system-notice-brief';
    brief.textContent = 'Keyword + graph recall is active for Recall, Why and Timeline.';
    summary.appendChild(title);
    summary.appendChild(brief);
    var detail = document.createElement('div');
    detail.className = 'system-notice-detail';
    detail.textContent =
      'Engraphis uses the same embedder HackDeepWiki indexes repositories ' +
      'with, so meaning-based recall turns on as soon as that embedder is ' +
      'reachable. Configure it in HackDeepWiki (embedder provider + API key, ' +
      'or a running Ollama), then restart the app -- nothing is downloaded ' +
      'and no separate script is needed.';
    details.appendChild(summary);
    details.appendChild(detail);
    if (info.reason) {
      var reason = document.createElement('div');
      reason.className = 'system-notice-reason';
      reason.textContent = 'Why it is off: ' + info.reason;
      detail.appendChild(reason);
    }
    notice.appendChild(details);
    sb.replaceChildren(notice);
  }
  window.renderSemBanner = function (eb) {
    var sb = document.getElementById('sem-banner');
    if (!sb) return;
    window.__emb = eb;
    var info = window.__hdwEmbedder || {};
    var semantic = !!((eb && eb.semantic) || info.semantic);
    if (typeof window.showAs === 'function') window.showAs(sb, !semantic, 'block');
    else sb.hidden = semantic;
    if (semantic) { sb.replaceChildren(); return; }
    hdwSemBanner(sb);
  };
  // dashboard.js runs before this shim and its boot() may already have
  // rendered the upstream banner; re-render ours over it when it did.
  if (typeof window.__emb !== 'undefined') {
    try { window.renderSemBanner(window.__emb); } catch (e) {}
  }

  if (!ws) return;
  var tries = 0;
  var timer = setInterval(function () {
    tries += 1;
    if (tries > 200) { clearInterval(timer); return; }
    if (typeof window.setWS !== 'function' || typeof window.loadOverview !== 'function') return;
    var label = document.getElementById('ws-name');
    if (!label) return;
    // Wait until the SPA's boot() has done its own default pick, then override
    // it exactly once so the two never fight.
    if (label.textContent === '\\u2014' || label.textContent === '') return;
    clearInterval(timer);
    var sameWs = label.textContent === ws;
    try {
      if (!sameWs) window.setWS(ws);
      if (typeof window.navTo === 'function' && view) {
        window.navTo(view);
      } else if (!sameWs) {
        if (typeof window.navTo === 'function') window.navTo('overview');
        window.loadOverview();
      }
    } catch (e) {}
  }, 150);
})();
"""


# ---------------------------------------------------------------------------
# Environment bootstrap (must precede any `import engraphis`)
# ---------------------------------------------------------------------------

def _engraphis_root() -> str:
    from api.data_root import get_data_root
    root = os.path.join(get_data_root(), "engraphis")
    os.makedirs(os.path.join(root, "state"), exist_ok=True)
    return root


def _bootstrap_env() -> None:
    """Point every Engraphis state/network knob at the portable DATABASE dir.

    setdefault throughout: a power user exporting their own ENGRAPHIS_* env
    (e.g. a real sentence-transformers model on a dev install) wins.
    """
    global _bootstrapped
    if _bootstrapped:
        return
    root = _engraphis_root()
    os.environ.setdefault("ENGRAPHIS_DB_PATH", os.path.join(root, "engraphis.db"))
    os.environ.setdefault("ENGRAPHIS_STATE_DIR", os.path.join(root, "state"))
    # No runtime network, ever: no update pings, no model downloads. The empty
    # embed model is Engraphis's documented explicit offline opt-out -- the
    # deterministic numpy embedder is used instead.
    os.environ.setdefault("ENGRAPHIS_UPDATE_CHECK", "0")
    os.environ.setdefault("ENGRAPHIS_EMBED_MODEL", "")
    # Their http_security honors a wholesale CSP override; this is the
    # supported, no-fork way to allow the dashboard inside our iframe.
    os.environ.setdefault("ENGRAPHIS_CSP", _EMBED_CSP)
    _bootstrapped = True


def is_available() -> bool:
    """True when the engraphis package is importable (memory tools usable)."""
    import importlib.util
    try:
        return importlib.util.find_spec("engraphis") is not None
    except Exception:  # noqa: BLE001 - a broken install must read as "absent"
        return False


def _dashboard_possible() -> bool:
    """The embedded web additionally needs fastapi+uvicorn (always bundled)."""
    import importlib.util
    try:
        return all(
            importlib.util.find_spec(m) is not None
            for m in ("engraphis", "fastapi", "uvicorn")
        )
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Workspace naming (the isolation boundary)
# ---------------------------------------------------------------------------

def _clean_component(value: str) -> str:
    """Engraphis names allow letters, digits, space and . _ - / ; we keep it
    stricter (no spaces/slashes) so a workspace name is also URL-safe."""
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", str(value or "").strip())
    return cleaned.strip("._-") or "unknown"


def workspace_for_version(owner: str, repo: str,
                          wiki_version: Optional[int]) -> str:
    """Per-wiki-release memory scope, shared by chat + code editor."""
    base = f"{_clean_component(owner)}_{_clean_component(repo)}"[:80]
    version = int(wiki_version) if wiki_version is not None else 0
    return f"{base}_v{version}"


def workspace_for_evolution(owner: str, repo: str) -> str:
    """Cross-release scope: what changed between wiki versions/commits."""
    base = f"{_clean_component(owner)}_{_clean_component(repo)}"[:80]
    return f"{base}_evolution"


# ---------------------------------------------------------------------------
# Shared MemoryService + embedded dashboard server
# ---------------------------------------------------------------------------

class _EmbeddableDashboard:
    """ASGI wrapper around engraphis.dashboard_app that makes it embeddable
    without touching the upstream repo: strips X-Frame-Options (their
    middleware hard-sets DENY), serves the deep-link shim, and injects the
    shim <script> into index.html."""

    def __init__(self, inner, index_path: str):
        self._inner = inner
        self._index_path = index_path

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self._inner(scope, receive, send)
            return

        path = scope.get("path") or "/"
        method = (scope.get("method") or "GET").upper()

        if method == "GET" and path == "/hackdeepwiki-shim.js":
            await self._send_asset(send, _shim_body(),
                                   b"application/javascript; charset=utf-8")
            return

        if method == "GET" and path == "/":
            try:
                with open(self._index_path, "rb") as f:
                    html = f.read()
                marker = b"</body>"
                tag = b'<script src="/hackdeepwiki-shim.js" defer></script>'
                if marker in html:
                    html = html.replace(marker, tag + marker, 1)
                else:  # defensive: still serve the dashboard unmodified
                    html = html + tag
                await self._send_asset(send, html, b"text/html; charset=utf-8")
                return
            except OSError:
                pass  # fall through to the real app

        async def send_wrapper(message):
            if message.get("type") == "http.response.start":
                headers = [
                    (k, v) for (k, v) in message.get("headers") or []
                    if k.lower() != b"x-frame-options"
                ]
                message = dict(message, headers=headers)
            await send(message)

        await self._inner(scope, receive, send_wrapper)

    @staticmethod
    async def _send_asset(send, body: bytes, content_type: bytes) -> None:
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", content_type),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store, no-cache, must-revalidate, max-age=0"),
                (b"x-content-type-options", b"nosniff"),
                (b"content-security-policy", _EMBED_CSP.encode("ascii")),
                (b"referrer-policy", b"strict-origin-when-cross-origin"),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def _shim_body() -> bytes:
    """The shim, prefixed with the real embedder status it renders.

    Served fresh on every request (the dashboard asset is no-store), so a
    reload after fixing an API key shows the new state without a rebuild.
    """
    import json
    info = dict(_embedder_info)
    payload = json.dumps(info, ensure_ascii=True).encode("ascii", "replace")
    return b"window.__hdwEmbedder = " + payload + b";\n" + _SHIM_JS


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _install_embedder() -> None:
    """Give Engraphis real semantic embeddings, without forking it.

    Engraphis picks its embedder through ``engraphis.backends.embedder_st.
    get_embedder``, which can only return a sentence-transformers model (a
    torch download this build deliberately never ships) or the deterministic
    hashing embedder -- lexical-only, which is why the dashboard reports
    "semantic search is off". HackDeepWiki already runs a configured text
    embedder for RAG, so we install an adapter over it (see
    ``api/engraphis_embedder``) as the factory's result. Same no-fork posture
    as ``_EmbeddableDashboard``: upstream code is untouched, only the factory
    it calls is replaced -- and only after ``_bootstrap_env``, before any
    engine is created.

    ``engraphis.core.engine`` binds ``get_embedder`` at module import, so the
    engine module's own reference is patched too. When the app's embedder
    isn't reachable, nothing is patched and Engraphis behaves exactly as it
    does today.
    """
    global _embedder_info
    try:
        from api.engraphis_embedder import build_embedder, is_enabled
    except Exception as e:  # noqa: BLE001
        _embedder_info = {"semantic": False, "model": "", "dim": 0,
                          "reason": f"{type(e).__name__}: {e}"}
        return

    adapter = None
    try:
        adapter = build_embedder()
    except Exception as e:  # noqa: BLE001 - build_embedder shouldn't raise
        logger.warning("Engraphis embedder adapter failed to build: %s", e)

    if adapter is None:
        _embedder_info = {
            "semantic": False, "model": "", "dim": 0,
            "reason": ("disabled by HACKDEEPWIKI_ENGRAPHIS_EMBEDDER"
                       if not is_enabled() else
                       "HackDeepWiki's configured embedder is not reachable "
                       "(missing API key, Ollama not running, or offline)"),
        }
        return

    try:
        from engraphis.backends import embedder_st
        import engraphis.core.engine as _engine

        def _hackdeepwiki_get_embedder(model_name=None, dim: int = 256,
                                       _adapter=adapter):
            return _adapter

        embedder_st.get_embedder = _hackdeepwiki_get_embedder
        embedder_st.LAST_EMBEDDER_ERROR = ""
        _engine.get_embedder = _hackdeepwiki_get_embedder
    except Exception as e:  # noqa: BLE001
        _embedder_info = {"semantic": False, "model": "", "dim": 0,
                          "reason": f"{type(e).__name__}: {e}"}
        logger.warning("Could not install the HackDeepWiki embedder into "
                       "Engraphis: %s", e)
        return

    _embedder_info = {"semantic": True, "model": adapter.model,
                      "dim": int(adapter.dim), "reason": ""}


def _schedule_reembed() -> None:
    """Rewrite stale-width vectors in the background after an embedder change.

    Recall's vector leg only looks at vectors whose width matches the query
    vector (``Store.iter_vectors(..., dim=...)``), so every memory written
    under the previous embedder (typically the 384-dim deterministic one)
    would be invisible to semantic search forever. This walks them in bounded
    batches on a daemon thread -- the app never blocks on it, and an
    interrupted run simply continues on the next launch.
    """
    if not _embedder_info.get("semantic"):
        return  # nothing to gain: deterministic vectors are already 384-dim
    thread = threading.Thread(target=_reembed_stale_vectors,
                              name="engraphis-reembed", daemon=True)
    thread.start()


def _reembed_stale_vectors() -> None:
    svc = _service
    if svc is None:
        return
    try:
        engine = svc.engine
        store = engine.store
        embedder = engine.embedder
        dim = int(getattr(embedder, "dim", 0))
        model = str(getattr(embedder, "model", "") or "")
        if dim <= 0:
            return
        state = (_ingest_state().get("__embedder__") or {}).get("vectors") or {}
        if state.get("dim") == dim and state.get("model") == model:
            return

        done = 0
        while done < _MAX_REEMBED:
            with _LOCK:
                rows = store.conn.execute(
                    "SELECT m.id, m.title, m.content FROM memories m "
                    "LEFT JOIN mem_vectors v ON v.id = m.id "
                    "WHERE v.dim IS NULL OR v.dim <> ? LIMIT ?",
                    (dim, _REEMBED_BATCH),
                ).fetchall()
            if not rows:
                _mark_ingested("__embedder__", "vectors", dim=dim, model=model)
                if done:
                    logger.info("Engraphis: re-embedded %d memories at %d-dim "
                                "(%s)", done, dim, model or "semantic")
                return
            ids = [str(r["id"]) for r in rows]
            # Same text the engine embeds on write ("title\ncontent"), so a
            # rewritten vector is identical to a freshly stored one.
            texts = [
                (f"{r['title']}\n{r['content']}" if r["title"] else str(r["content"] or ""))
                for r in rows
            ]
            vectors = embedder.embed(texts)
            with _LOCK:
                for mem_id, vector in zip(ids, vectors):
                    store.put_vector(mem_id, vector, model=model)
                store.conn.commit()
                index = getattr(engine, "index", None)
                # NumpyVectorIndex reads mem_vectors directly (already
                # written above); an ANN backend keeps its own table.
                if index is not None and type(index).__name__ != "NumpyVectorIndex":
                    try:
                        index.upsert(ids, vectors)
                    except Exception as e:  # noqa: BLE001
                        logger.debug("vector index upsert skipped: %s", e)
            done += len(ids)
        logger.info("Engraphis: re-embedded the first %d memories at %d-dim; "
                    "the rest continue on the next launch", done, dim)
    except Exception as e:  # noqa: BLE001 - never break startup
        logger.warning("Engraphis vector re-embed skipped: %s: %s",
                       type(e).__name__, e)


def _ensure_started() -> None:
    """Build the shared MemoryService and (when possible) start the embedded
    dashboard server. Idempotent; failures are recorded, never raised."""
    global _service, _dashboard_thread, _dashboard_server, _dashboard_port, _start_error

    with _state_lock:
        if _service is not None or _start_error is not None:
            return
        if not is_available():
            _start_error = "engraphis is not installed in this build"
            return
        _bootstrap_env()
        # Must precede the first MemoryEngine construction below: the engine
        # resolves its embedder once, at create().
        _install_embedder()

        dashboard_error: Optional[str] = None
        try:
            if _dashboard_possible():
                # Importing engraphis.dashboard_app builds the FastAPI app AND
                # its MemoryService at module import (their documented
                # `uvicorn engraphis.dashboard_app:app` entry). Reuse both:
                # one SQLite writer shared by the dashboard, the chat tools
                # and the code agent's MCP tools -- the exact single-writer
                # pattern upstream uses for its own /mcp mount.
                import engraphis.dashboard_app as _dash

                _service = _dash.app.state.service
                wrapped = _EmbeddableDashboard(_dash.app, str(_dash._INDEX))

                import uvicorn

                port = _find_free_port()
                config = uvicorn.Config(
                    wrapped,
                    host="127.0.0.1",
                    port=port,
                    log_level="warning",
                    # Loopback-only; engraphis validates proxies itself and
                    # documents proxy_headers=False as the safe setting.
                    proxy_headers=False,
                )
                server = uvicorn.Server(config)
                thread = threading.Thread(
                    target=server.run, name="engraphis-dashboard", daemon=True
                )
                thread.start()
                _dashboard_server = server
                _dashboard_thread = thread
                _dashboard_port = port
                logger.info("Engraphis dashboard embedded at http://127.0.0.1:%s "
                            "(db: %s)", port, os.environ.get("ENGRAPHIS_DB_PATH"))
        except Exception as e:  # noqa: BLE001 - fall back to memory-only below
            dashboard_error = f"{type(e).__name__}: {e}"
            logger.warning("Engraphis dashboard failed to start (falling back "
                           "to memory-only mode): %s", dashboard_error)

        if _service is None:
            # Memory-only mode: the dashboard couldn't mount (e.g. a missing
            # optional web dep) or fastapi/uvicorn are absent. The memory
            # tools for chat + code editor still work -- only the embedded
            # web UI is unavailable, and status() reports why.
            try:
                from engraphis.service import MemoryService
                from engraphis.config import settings

                _service = MemoryService.create(
                    settings.db_path,
                    embed_model=settings.embed_model,
                    embed_dim=settings.embed_dim or 384,
                    allowed_workspaces=settings.allowed_workspaces,
                )
                _start_error = dashboard_error  # surfaced as dashboard_error only
                logger.info("Engraphis memory service started without the "
                            "embedded dashboard%s",
                            f" ({dashboard_error})" if dashboard_error else "")
            except Exception as e:  # noqa: BLE001 - memory must never break the app
                _start_error = f"{type(e).__name__}: {e}"
                _service = None
                logger.warning("Engraphis integration failed to start: %s", _start_error)

        if _service is not None:
            _schedule_reembed()


def shutdown() -> None:
    """Stop the embedded dashboard server (called from the app lifespan)."""
    global _dashboard_server
    server = _dashboard_server
    if server is not None:
        try:
            server.should_exit = True
        except Exception:  # noqa: BLE001
            pass
        _dashboard_server = None


def get_service():
    """The shared MemoryService, or None when unavailable."""
    _ensure_started()
    return _service


def status() -> dict[str, Any]:
    _ensure_started()
    return {
        "available": _service is not None,
        "dashboard_url": (
            f"http://127.0.0.1:{_dashboard_port}" if _dashboard_port else None
        ),
        "error": _start_error,
        "embedder": dict(_embedder_info),
    }


def dashboard_url_for(workspace: str, view: str = "") -> Optional[str]:
    """Deep-linked dashboard URL for one workspace (see _SHIM_JS). ``view``
    optionally picks the SPA route, e.g. 'graph' for the knowledge graph."""
    _ensure_started()
    if _dashboard_port is None:
        return None
    from urllib.parse import quote
    url = f"http://127.0.0.1:{_dashboard_port}/?ws={quote(workspace)}"
    if view:
        url += f"&view={quote(view)}"
    return url


# ---------------------------------------------------------------------------
# Memory operations (never raise -- they return user/model-facing strings)
# ---------------------------------------------------------------------------

def ensure_workspace(workspace: str, description: str = "") -> bool:
    svc = get_service()
    if svc is None:
        return False
    with _LOCK:
        try:
            svc.create_workspace(workspace, description)
            return True
        except Exception:  # noqa: BLE001 - "already exists" is the normal case
            return True


def remember_detailed(workspace: str, content: str, *, mtype: str = "semantic",
                      session_id: Optional[str] = None, source: str = "agent",
                      metadata: Optional[dict] = None,
                      title: str = "") -> dict[str, Any]:
    """Store one memory and return ``{"id", "op", "error"}``.

    The id is what makes a *connected* memory possible: callers that write a
    memory can immediately ``link`` it to the memories it follows from or
    refers to, instead of leaving isolated nodes in the graph.
    """
    svc = get_service()
    if svc is None:
        return {"id": "", "op": "", "error": "engraphis unavailable"}
    content = (content or "").strip()
    if not content:
        return {"id": "", "op": "", "error": "empty content"}
    with _LOCK:
        try:
            result = svc.remember(
                content, workspace=workspace, mtype=mtype, title=title,
                session_id=session_id, source=source, metadata=metadata,
            ) or {}
            return {"id": str(result.get("id") or ""),
                    "op": str(result.get("op") or "add"), "error": ""}
        except Exception as e:  # noqa: BLE001
            logger.warning("engraphis remember failed: %s", e)
            return {"id": "", "op": "", "error": f"{e}"}


def remember(workspace: str, content: str, *, mtype: str = "semantic",
             session_id: Optional[str] = None, source: str = "agent",
             metadata: Optional[dict] = None, title: str = "") -> str:
    """Store one memory in a workspace. Returns a short outcome string."""
    if get_service() is None:
        return "Memory is unavailable in this build (Engraphis not installed)."
    if not (content or "").strip():
        return "Nothing to remember: empty content."
    result = remember_detailed(
        workspace, content, mtype=mtype, session_id=session_id, source=source,
        metadata=metadata, title=title,
    )
    if result.get("error"):
        return f"Could not store the memory: {result['error']}"
    return (f"Remembered ({result['op']}) in workspace '{workspace}' "
            f"[{result['id']}].")


def link(workspace: str, a: str, b: str, *, relation: str = "related",
         reason: str = "") -> bool:
    """Draw one edge between two memories. False when it couldn't be drawn.

    Engraphis infers the graph layer from the relation vocabulary
    (``follows`` -> temporal, ``part_of``/``references`` -> entity,
    ``depends_on`` -> causal), so callers only pick the relation.
    """
    svc = get_service()
    if svc is None or not a or not b or a == b:
        return False
    with _LOCK:
        try:
            svc.link(a, b, workspace=workspace, relation=relation,
                     reason=reason[:180])
            return True
        except Exception as e:  # noqa: BLE001 - a missing edge must never break a chat
            logger.debug("engraphis link %s -> %s (%s) skipped: %s",
                         a, b, relation, e)
            return False


def recall_ids(workspace: str, query: str, *, k: int = 3) -> list[str]:
    """Ids of the memories most related to ``query``, best first.

    Passive (no reinforcement, no receipt): this is used to decide what a new
    memory should be *linked* to, which shouldn't distort the usage signal
    that an explicit recall carries.
    """
    svc = get_service()
    if svc is None or not (query or "").strip():
        return []
    with _LOCK:
        try:
            result = svc.recall(query, workspace=workspace, k=k,
                                reinforce=False, record_receipt=False,
                                intent="recall")
        except Exception as e:  # noqa: BLE001
            logger.debug("engraphis recall_ids skipped: %s", e)
            return []
    memories = (result or {}).get("memories") if isinstance(result, dict) else None
    if not isinstance(memories, list):
        return []
    ids = []
    for mem in memories:
        if isinstance(mem, dict) and mem.get("id"):
            ids.append(str(mem["id"]))
    return ids


def recall(workspace: str, query: str, *, k: int = _MAX_INJECTED_MEMORIES) -> str:
    """Recall memories as a compact, model-readable block."""
    svc = get_service()
    if svc is None:
        return "Memory is unavailable in this build (Engraphis not installed)."
    query = (query or "").strip()
    if not query:
        return "Empty memory query."
    with _LOCK:
        try:
            result = svc.recall(query, workspace=workspace, k=k)
        except Exception as e:  # noqa: BLE001
            logger.warning("engraphis recall failed: %s", e)
            return f"Memory recall failed: {e}"
    return _format_recall(result, workspace)


def remember_linked(workspace: str, content: str, *, mtype: str = "episodic",
                    session_id: Optional[str] = None, source: str = "agent",
                    metadata: Optional[dict] = None, title: str = "",
                    chain_key: str = "", relate_query: str = "",
                    max_related: int = 2) -> str:
    """Store a memory AND wire it into the graph. Returns the memory id.

    A store of unconnected memories is a list, not a graph: recall can find a
    single row but nothing explains what came before it or what it builds on,
    and the dashboard's graph view shows a field of isolated dots. Every
    memory written through here gets two kinds of edge:

    * ``follows`` -- the previous memory of the same ``chain_key`` in this
      workspace (e.g. the previous chat turn), giving the workspace a real
      timeline. The chain head is kept in our ingest-state file for the same
      reason ``_has_memory`` is: Engraphis offers semantic recall, not an
      exact "the last memory of kind X" lookup.
    * ``references`` -- the memories most related to ``relate_query``,
      computed BEFORE the write so the new memory can't match itself. This is
      what connects a follow-up answer to the wiki page or earlier decision it
      actually builds on.
    """
    related: list[str] = []
    if relate_query and max_related > 0:
        related = recall_ids(workspace, relate_query, k=max_related)

    mem_id = remember_detailed(
        workspace, content, mtype=mtype, session_id=session_id, source=source,
        metadata=metadata, title=title,
    )["id"]
    if not mem_id:
        return ""

    if chain_key:
        state_key = f"chain_{_clean_component(chain_key)}"
        previous = (_ingest_state().get(workspace) or {}).get(state_key) or {}
        previous_id = str(previous.get("id") or "")
        if previous_id and previous_id != mem_id:
            link(workspace, mem_id, previous_id, relation="follows",
                 reason=f"next {chain_key} in this workspace")
        _mark_ingested(workspace, state_key, id=mem_id)

    for related_id in related:
        if related_id != mem_id:
            link(workspace, mem_id, related_id, relation="references",
                 reason="answers build on this earlier memory")
    return mem_id


def why(workspace: str, query: str) -> str:
    svc = get_service()
    if svc is None:
        return "Memory is unavailable in this build (Engraphis not installed)."
    with _LOCK:
        try:
            result = svc.why(query or "", workspace=workspace)
            return _compact_json(result)
        except Exception as e:  # noqa: BLE001
            return f"Memory 'why' failed: {e}"


def timeline(workspace: str, query: str) -> str:
    svc = get_service()
    if svc is None:
        return "Memory is unavailable in this build (Engraphis not installed)."
    with _LOCK:
        try:
            result = svc.timeline(query or "", workspace=workspace)
            return _compact_json(result)
        except Exception as e:  # noqa: BLE001
            return f"Memory timeline failed: {e}"


def proactive_block(workspace: str, query: str) -> str:
    """Bounded '## Project memory' block for system-prompt injection, or ''.

    Uses recall WITHOUT reinforcement/receipts so passive injection doesn't
    distort the reinforcement signal the way an explicit recall should.
    """
    svc = get_service()
    if svc is None or not (query or "").strip():
        return ""
    with _LOCK:
        try:
            result = svc.recall(
                query, workspace=workspace, k=_MAX_INJECTED_MEMORIES,
                reinforce=False, record_receipt=False, intent="recall",
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("engraphis proactive recall skipped: %s", e)
            return ""
    lines = _memory_lines(result)
    if not lines:
        return ""
    block = "\n".join(lines)[:_MAX_INJECTED_CHARS]
    return (
        "## Project memory (recalled automatically for this wiki release)\n"
        "Facts previously stored for this exact wiki version -- treat them as "
        "context from earlier sessions, and verify anything critical against "
        "the current code/wiki:\n" + block
    )


def _memory_lines(result: Any) -> list[str]:
    lines: list[str] = []
    memories = (result or {}).get("memories") if isinstance(result, dict) else None
    if not isinstance(memories, list):
        return lines
    for mem in memories[:_MAX_INJECTED_MEMORIES]:
        if not isinstance(mem, dict):
            continue
        text = str(mem.get("content") or mem.get("text") or "").strip()
        if not text:
            continue
        mem_id = str(mem.get("id") or "")
        text = text if len(text) <= 400 else text[:400] + "…"
        lines.append(f"- {text}" + (f" [{mem_id}]" if mem_id else ""))
    return lines


def _format_recall(result: Any, workspace: str) -> str:
    if isinstance(result, dict):
        context = str(result.get("context") or "").strip()
        if context:
            return context[:_MAX_INJECTED_CHARS * 2]
        lines = _memory_lines(result)
        if lines:
            return "\n".join(lines)
    return f"No memories found in workspace '{workspace}' for that query."


def _compact_json(value: Any, limit: int = 4000) -> str:
    import json
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


# ---------------------------------------------------------------------------
# Wiki evolution memory (what changed between releases)
# ---------------------------------------------------------------------------

def record_wiki_release(*, owner: str, repo: str, repo_type: str,
                        language: str, version: int,
                        repo_commit: Optional[str],
                        previous_version: Optional[int],
                        previous_commit: Optional[str],
                        clone_dir: Optional[str]) -> None:
    """Best-effort: when a new wiki release is saved, write what changed since
    the previous release into the repo's evolution workspace. Called from
    save_wiki_cache; must never raise (a memory failure must never roll back
    a successful wiki save)."""
    try:
        svc = get_service()
        if svc is None:
            return
        workspace = workspace_for_evolution(owner, repo)
        ensure_workspace(
            workspace,
            f"Evolution of the {owner}/{repo} wiki across releases "
            "(commit ranges, changes between versions).",
        )

        # Wikis are built from a --depth=1 clone, where every question about
        # the past answers "1 commit". Deepen it first so this workspace
        # records the repo's real history (and so the range against the
        # previous release resolves at all).
        ensure_deep_clone(clone_dir)

        summary = (
            f"Wiki release v{version} of {owner}/{repo} ({repo_type}, "
            f"language '{language}') was generated"
        )
        summary += f" from commit {repo_commit[:12]}." if repo_commit else \
            " (no commit anchor -- non-git source or missing clone)."
        if previous_version is not None:
            summary += f" Previous release: v{previous_version}"
            summary += f" at commit {previous_commit[:12]}." if previous_commit else "."
        release_id = remember_detailed(
            workspace, summary, mtype="episodic", source="hackdeepwiki",
            title=f"Wiki release v{version} of {owner}/{repo}",
            metadata={"kind": "wiki_release", "version": version,
                      "commit": repo_commit or ""},
        )["id"]
        # Releases form a chain: v3 follows v2 follows v1. The pointer to the
        # previous release lives in our ingest-state file because Engraphis
        # exposes semantic recall, not an exact "find the memory whose
        # metadata says version=2" lookup (same reasoning as _has_memory).
        previous_release = (_ingest_state().get(workspace) or {}).get("last_release") or {}
        if release_id and previous_release.get("id"):
            link(workspace, release_id, str(previous_release["id"]),
                 relation="follows",
                 reason=f"wiki v{version} follows v{previous_release.get('version')}")
        if release_id:
            _mark_ingested(workspace, "last_release", id=release_id,
                           version=version)

        commits = _git_commit_range(clone_dir, previous_commit, repo_commit)
        if commits:
            listing = "\n".join(commits[:_MAX_EVOLUTION_COMMITS])
            extra = len(commits) - _MAX_EVOLUTION_COMMITS
            if extra > 0:
                listing += f"\n… and {extra} more commits"
            diff_id = remember_detailed(
                workspace,
                f"Changes between wiki v{previous_version} and v{version} of "
                f"{owner}/{repo} (git log {previous_commit[:12]}..{repo_commit[:12]}):\n"
                f"{listing}",
                mtype="episodic", source="hackdeepwiki",
                title=f"Commits between wiki v{previous_version} and v{version}",
                metadata={"kind": "wiki_diff", "from_version": previous_version,
                          "to_version": version},
            )["id"]
            # These commits are what produced the release: an explicit edge
            # makes "why did v3 change?" answerable from the graph.
            link(workspace, diff_id, release_id, relation="part_of",
                 reason=f"commits that produced wiki v{version}")
        stats = _git_diff_stat(clone_dir, previous_commit, repo_commit)
        if stats:
            stats_id = remember_detailed(
                workspace,
                f"Files changed between wiki v{previous_version} and v{version} "
                f"of {owner}/{repo}:\n{stats}",
                mtype="episodic", source="hackdeepwiki",
                title=f"Files changed between wiki v{previous_version} and v{version}",
                metadata={"kind": "wiki_diffstat", "from_version": previous_version,
                          "to_version": version},
            )["id"]
            link(workspace, stats_id, release_id, relation="part_of",
                 reason=f"files changed for wiki v{version}")

        # First time we see this repo: ingest the WHOLE commit history, not
        # just the (empty) range since a previous release. Without this the
        # evolution workspace of a brand-new wiki holds a single "release"
        # memory and the graph stays empty.
        _backfill_history(workspace, owner, repo, clone_dir, repo_commit)
    except Exception as e:  # noqa: BLE001
        logger.warning("engraphis evolution record skipped: %s", e)


def _backfill_history(workspace: str, owner: str, repo: str,
                      clone_dir: Optional[str], head: Optional[str]) -> None:
    """Ingest every commit reachable from HEAD into the evolution workspace,
    once per repo. Subsequent releases only add their own range (handled by
    record_wiki_release), so this stays cheap after the first run.

    Deliberately deterministic (git only, no LLM): it runs on every wiki save
    and must not cost tokens or time proportional to the model.
    """
    if not clone_dir:
        return
    marker = f"__history_backfilled__ {owner}/{repo}"
    shallow = is_shallow_clone(clone_dir)
    previous = (_ingest_state().get(workspace) or {}).get("history_backfill") or {}
    redo = False
    if _has_memory(workspace, "history_backfill"):
        # A first pass over a --depth=1 clone sees exactly ONE commit, whatever
        # the repo's real length. Once the clone has been deepened, redo the
        # full ingest instead of stepping incrementally from that stump.
        if previous.get("shallow") and not shallow:
            redo = True
            logger.info("Engraphis: re-ingesting %s/%s -- the first pass only "
                        "saw a shallow clone", owner, repo)
        else:
            _backfill_incremental(workspace, owner, repo, clone_dir, head)
            return

    records = _git_history_records(clone_dir)
    commits = [_record_line(r) for r in records]
    if not commits:
        return

    stats = _git_history_stats(clone_dir)
    overview_id = ""
    if stats:
        overview_id = remember_detailed(
            workspace, stats, mtype="semantic", source="hackdeepwiki",
            title=f"Repository history overview -- {owner}/{repo}",
            metadata={"kind": "repo_history_overview"},
        )["id"]
    if overview_id:
        _mark_ingested(workspace, "history_overview", id=overview_id)

    written = 0
    previous_batch_id = ""
    newest_batch_id = ""
    for start in range(0, len(commits), _HISTORY_BATCH):
        batch = commits[start:start + _HISTORY_BATCH]
        if not batch:
            continue
        body = "\n".join(batch)
        batch_id = remember_detailed(
            workspace,
            f"Commit history of {owner}/{repo} "
            f"({start + 1}-{start + len(batch)} of {len(commits)}, newest first):\n{body}",
            mtype="episodic", source="hackdeepwiki",
            title=f"Commits {start + 1}-{start + len(batch)} of {len(commits)} -- {owner}/{repo}",
            metadata={"kind": "commit_history_batch", "offset": start,
                      "count": len(batch)},
        )["id"]
        # History is a chain, and every batch is part of the same repository
        # history -- batches are written newest-first, so batch N follows the
        # one written before it in time terms ("comes after it in the log").
        if batch_id and previous_batch_id:
            link(workspace, previous_batch_id, batch_id, relation="follows",
                 reason="later commits in the same history")
        if batch_id and overview_id:
            link(workspace, batch_id, overview_id, relation="part_of",
                 reason=f"commit history of {owner}/{repo}")
        previous_batch_id = batch_id or previous_batch_id
        newest_batch_id = newest_batch_id or batch_id
        written += len(batch)
    # Batches are written newest-first, so the FIRST one is the head of the
    # chain -- that's what the next (incremental) ingest links onto.
    if newest_batch_id:
        _mark_ingested(workspace, "last_commit_batch", id=newest_batch_id)

    # How the project moved, sampled along the history (see
    # _write_history_checkpoints) -- git only, so it costs no tokens.
    _write_history_checkpoints(workspace, owner, repo, clone_dir,
                               overview_id=overview_id, records=records)

    # Wording matters on a redo: Engraphis reinforces near-duplicates instead
    # of inserting them, so "45 commits" phrased like the earlier "1 commits"
    # would silently keep the stump's text as the memory a human reads.
    body = (f"{marker}: the full history of {owner}/{repo} was re-ingested "
            f"after the shallow clone was deepened -- {written} commits, up "
            f"to {(head or 'HEAD')[:12]}." if redo else
            f"{marker}: {written} commits of {owner}/{repo} ingested up to "
            f"{(head or 'HEAD')[:12]}.")
    remember(
        workspace, body,
        mtype="semantic", source="hackdeepwiki",
        title=f"History ingested -- {owner}/{repo}",
        metadata={"kind": "history_backfill", "head": head or "",
                  "count": written, "redo": redo},
    )
    _mark_ingested(workspace, "history_backfill", head=head or "",
                   count=written, shallow=shallow)
    logger.info("Engraphis: backfilled %s commits of %s/%s into %s%s",
                written, owner, repo, workspace,
                " (shallow clone -- only the tip is available)" if shallow else "")


def _backfill_incremental(workspace: str, owner: str, repo: str,
                          clone_dir: Optional[str], head: Optional[str]) -> None:
    """After the initial backfill, ingest only commits newer than the last one
    we recorded -- so the evolution graph keeps growing with each update."""
    last = _last_backfilled_head(workspace)
    if not last or not head or last == head:
        return
    commits = _git_commit_range_detailed(clone_dir, last, head)
    if not commits:
        return
    overview_id = str(
        ((_ingest_state().get(workspace) or {}).get("history_overview") or {}).get("id") or ""
    )
    # New commits may have closed one or more windows since the last save.
    _write_history_checkpoints(workspace, owner, repo, clone_dir,
                               overview_id=overview_id)
    batch_ids: list[str] = []
    for start in range(0, len(commits), _HISTORY_BATCH):
        batch = commits[start:start + _HISTORY_BATCH]
        batch_ids.append(remember_detailed(
            workspace,
            f"New commits in {owner}/{repo} since {last[:12]} "
            f"(up to {head[:12]}):\n" + "\n".join(batch),
            mtype="episodic", source="hackdeepwiki",
            title=f"New commits in {owner}/{repo} since {last[:12]}",
            metadata={"kind": "commit_history_batch", "since": last,
                      "head": head, "count": len(batch)},
        )["id"])

    # Keep extending the ONE chain the initial backfill built instead of
    # starting a new island at every update. Batches are newest-first, so the
    # chain is walked backwards: the oldest new batch follows the previous
    # head, and each newer batch follows the one before it.
    head_id = str(
        ((_ingest_state().get(workspace) or {}).get("last_commit_batch") or {}).get("id") or ""
    )
    for batch_id in reversed(batch_ids):
        if batch_id and head_id:
            link(workspace, batch_id, head_id, relation="follows",
                 reason=f"commits after {last[:12]}")
        head_id = batch_id or head_id
    if head_id:
        _mark_ingested(workspace, "last_commit_batch", id=head_id)
    remember(
        workspace,
        f"__history_backfilled__ {owner}/{repo}: extended to {head[:12]} "
        f"(+{len(commits)} commits).",
        mtype="semantic", source="hackdeepwiki",
        title=f"History extended to {head[:12]} -- {owner}/{repo}",
        metadata={"kind": "history_backfill", "head": head,
                  "count": len(commits)},
    )
    _mark_ingested(workspace, "history_backfill", head=head,
                   count=len(commits), shallow=is_shallow_clone(clone_dir))
    logger.info("Engraphis: +%s new commits of %s/%s into %s",
                len(commits), owner, repo, workspace)


def _has_memory(workspace: str, kind: str) -> bool:
    """True when the initial history backfill already ran for this workspace.

    Tracked in our own small JSON state file rather than by querying Engraphis:
    MemoryService exposes semantic recall, not an exact metadata lookup, and a
    fuzzy match is the wrong tool for an idempotency check.
    """
    return bool((_ingest_state().get(workspace) or {}).get(kind))


def _last_backfilled_head(workspace: str) -> Optional[str]:
    entry = (_ingest_state().get(workspace) or {}).get("history_backfill")
    return (entry or {}).get("head") if isinstance(entry, dict) else None


def _ingest_state_path() -> str:
    return os.path.join(_engraphis_root(), "hackdeepwiki_ingest.json")


def _ingest_state() -> dict:
    import json as _json
    try:
        with open(_ingest_state_path(), "r", encoding="utf-8") as f:
            data = _json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - absent/corrupt state just means "not done"
        return {}


def _mark_ingested(workspace: str, kind: str, **fields: Any) -> None:
    import json as _json
    with _LOCK:
        state = _ingest_state()
        state.setdefault(workspace, {})[kind] = fields
        try:
            path = _ingest_state_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(state, f, indent=2)
            os.replace(tmp, path)
        except Exception as e:  # noqa: BLE001
            logger.debug("could not persist engraphis ingest state: %s", e)


def is_shallow_clone(clone_dir: Optional[str]) -> bool:
    """True for a ``--depth=1`` clone, whose ``git log`` shows ONE commit."""
    if not clone_dir:
        return False
    try:
        out = subprocess.run(
            ["git", "-C", clone_dir, "rev-parse", "--is-shallow-repository"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20,
        )
        return out.returncode == 0 and out.stdout.strip() == "true"
    except Exception:  # noqa: BLE001
        return False


def ensure_deep_clone(clone_dir: Optional[str]) -> bool:
    """Fetch the full history of a shallow wiki clone. True when the clone has
    real history afterwards.

    Wiki clones are ``--depth=1 --single-branch`` (api/data_pipeline.py), so
    every git question about the past answers "1 commit" -- which is exactly
    what the evolution workspace used to record for a 45-commit repository.
    Deepening is best-effort: offline or on a slow network the clone simply
    stays shallow, the backfill records that it did, and the next wiki save
    tries again (see _backfill_history).
    """
    if not clone_dir or not is_shallow_clone(clone_dir):
        return bool(clone_dir)
    logger.info("Engraphis: fetching full history for %s (shallow clone)",
                clone_dir)
    try:
        subprocess.run(
            ["git", "-C", clone_dir, "fetch", "--unshallow", "--tags"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=300,
        )
    except Exception as e:  # noqa: BLE001 - offline is a normal outcome
        logger.info("Engraphis: could not deepen %s (%s); history memory will "
                    "be limited to the commits present", clone_dir, e)
    return not is_shallow_clone(clone_dir)


def _git_history_records(clone_dir: str) -> list[dict]:
    """Every commit reachable from HEAD, newest first, as structured records.

    One `git log` call; the unit separator keeps subjects containing spaces or
    colons parseable, which a plain "%h %s" line does not.
    """
    try:
        out = subprocess.run(
            ["git", "-C", clone_dir, "log", "--no-decorate",
             f"--max-count={_MAX_HISTORY_COMMITS}", "--date=short",
             "--pretty=format:%H\x1f%h\x1f%ad\x1f%an\x1f%s"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120,
        )
        if out.returncode != 0:
            return []
    except Exception:  # noqa: BLE001
        return []
    records = []
    for line in out.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        records.append({"sha": parts[0], "short": parts[1], "date": parts[2],
                        "author": parts[3], "subject": parts[4]})
    return records


def _record_line(record: dict) -> str:
    """The one-line form stored in commit batches -- rich enough for the regex
    graph extractor to mint author and concept nodes."""
    return (f"{record['short']} {record['date']} {record['author']}: "
            f"{record['subject']}")


def _git_full_history(clone_dir: str) -> list[str]:
    """`git log` over the whole repo, one rich line per commit."""
    return [_record_line(r) for r in _git_history_records(clone_dir)]


# --- progress checkpoints -------------------------------------------------

def _history_stride(total: int) -> int:
    """How many commits one progress checkpoint covers, or 0 for a history too
    short to sample.

    The stride scales with the repo so the story always has about
    ``_CHECKPOINT_TARGET`` steps: every 10 commits at 200, every 20 at 400,
    every 30 at 600, and so on up to the cap.
    """
    if total <= _MIN_CHECKPOINT_STRIDE:
        return 0
    stride = math.ceil(total / _CHECKPOINT_TARGET / 10) * 10
    stride = max(_MIN_CHECKPOINT_STRIDE, min(_MAX_CHECKPOINT_STRIDE, stride))
    # Past the stride cap it is the WINDOW that widens, never the number of
    # checkpoints: the cost of describing a history stays flat.
    while total // stride > _MAX_CHECKPOINTS:
        stride *= 2
    return stride


def _git_progress_stat(clone_dir: str, old: str, new: str) -> str:
    """Churn plus where it landed, in ONE local git call.

    ``--shortstat`` says how much moved, ``--dirstat`` says which parts of the
    tree it moved in -- together that is the "quick evaluation" of a window of
    commits, with no model involved.
    """
    try:
        out = subprocess.run(
            ["git", "-C", clone_dir, "diff", "--shortstat",
             "--dirstat=files,0,3", old, new],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60,
        )
        if out.returncode != 0:
            return ""
    except Exception:  # noqa: BLE001
        return ""
    return "\n".join(line.strip() for line in out.stdout.splitlines()
                     if line.strip())


_MILESTONE_RE = re.compile(
    r"^(feat|feature|release|breaking|perf|refactor)\b|!\s*:|BREAKING",
    re.IGNORECASE,
)


def _pick_milestones(window: list[dict], limit: int) -> list[str]:
    """The few commits that best describe a window: features and releases
    first, then evenly spaced ones so the sample still spans the window."""
    if limit <= 0 or not window:
        return []
    chosen: list[int] = [i for i, r in enumerate(window)
                         if _MILESTONE_RE.search(r["subject"])][:limit]
    if len(chosen) < limit:
        step = max(1, len(window) // (limit - len(chosen) + 1))
        for i in range(0, len(window), step):
            if i not in chosen:
                chosen.append(i)
            if len(chosen) >= limit:
                break
    return [window[i]["subject"][:110] for i in sorted(chosen)[:limit]]


def _checkpoint_body(owner: str, repo: str, number: int, start: int,
                     total: int, window: list[dict], churn: str) -> str:
    """One checkpoint, as plain text bounded by _MAX_CHECKPOINT_LINES."""
    authors: dict[str, int] = {}
    for record in window:
        authors[record["author"]] = authors.get(record["author"], 0) + 1
    top = sorted(authors.items(), key=lambda kv: (-kv[1], kv[0]))[:4]

    lines = [
        f"Progress checkpoint #{number} of {owner}/{repo} -- commits "
        f"{start + 1}-{start + len(window)} of {total}, "
        f"{window[0]['date']} to {window[-1]['date']} "
        f"({window[0]['short']}..{window[-1]['short']}).",
    ]
    if churn:
        # Bounded so the milestones always fit too: a wide repo can produce a
        # dirstat line per top-level directory, and which commits landed says
        # more than the tail of that list.
        lines.append("Churn since the previous checkpoint:")
        lines.extend("  " + line for line in churn.splitlines()[:_MAX_CHURN_LINES])
    lines.append("Authors: " + ", ".join(f"{name} ({n})" for name, n in top)
                 + (", …" if len(authors) > len(top) else "") + ".")

    room = _MAX_CHECKPOINT_LINES - len(lines) - 1
    milestones = _pick_milestones(window, room)
    if milestones:
        lines.append("Milestones in this window:")
        lines.extend(f"  - {subject}" for subject in milestones)
    return "\n".join(lines[:_MAX_CHECKPOINT_LINES])


def _write_history_checkpoints(workspace: str, owner: str, repo: str,
                               clone_dir: Optional[str],
                               overview_id: str = "",
                               records: Optional[list[dict]] = None) -> int:
    """Write one memory per window of the history, chained oldest to newest.

    Commit batches answer "what happened"; checkpoints answer "how the project
    moved" -- each one summarising a stride of commits with the churn, the
    directories that changed and the milestone subjects in it. Only COMPLETE
    windows are written and the count of written windows is remembered, so a
    later wiki save only pays for the windows that closed since.
    """
    if not clone_dir:
        return 0
    if records is None:
        records = _git_history_records(clone_dir)
    chronological = list(reversed(records or []))
    total = len(chronological)
    state = (_ingest_state().get(workspace) or {}).get("checkpoints") or {}
    stride = int(state.get("stride") or 0) or _history_stride(total)
    if not stride:
        return 0
    done = int(state.get("done") or 0)
    # The repo outgrew the stride we picked: widen the window instead of
    # writing an unbounded number of checkpoints. Two old windows collapse
    # into one new one, so the count halves with the stride.
    while total // stride > _MAX_CHECKPOINTS:
        stride *= 2
        done //= 2
    windows = total // stride
    if windows <= done:
        return 0

    previous_id = str(state.get("last_id") or "")
    for index in range(done, windows):
        start = index * stride
        window = chronological[start:start + stride]
        # The very first window is diffed against git's empty tree, so the
        # initial burst of work is measured instead of skipped.
        before = chronological[start - 1]["sha"] if start else _EMPTY_TREE
        body = _checkpoint_body(
            owner, repo, index + 1, start, total, window,
            _git_progress_stat(clone_dir, before, window[-1]["sha"]),
        )
        mem_id = remember_detailed(
            workspace, body, mtype="episodic", source="hackdeepwiki",
            title=(f"Progress checkpoint #{index + 1} of {owner}/{repo} "
                   f"({window[0]['date']} to {window[-1]['date']})"),
            metadata={"kind": "history_checkpoint", "number": index + 1,
                      "offset": start, "count": len(window),
                      "stride": stride, "head": window[-1]["sha"]},
        )["id"]
        if mem_id and previous_id:
            link(workspace, mem_id, previous_id, relation="follows",
                 reason="the next stretch of the project's history")
        if mem_id and overview_id:
            link(workspace, mem_id, overview_id, relation="part_of",
                 reason=f"progress of {owner}/{repo}")
        previous_id = mem_id or previous_id

    _mark_ingested(workspace, "checkpoints", stride=stride, done=windows,
                   last_id=previous_id,
                   head=chronological[windows * stride - 1]["sha"])
    logger.info("Engraphis: %s progress checkpoint(s) (every %s commits) for "
                "%s/%s", windows - done, stride, owner, repo)
    return windows - done


def _git_commit_range_detailed(clone_dir: Optional[str], old: str,
                               new: str) -> list[str]:
    if not clone_dir:
        return []
    try:
        out = subprocess.run(
            ["git", "-C", clone_dir, "log", "--no-decorate", "--date=short",
             "--pretty=format:%h %ad %an: %s", f"{old}..{new}"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60,
        )
        if out.returncode != 0:
            return []
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]
    except Exception:  # noqa: BLE001
        return []


def _git_history_stats(clone_dir: str) -> str:
    """A compact, factual overview of the repo's history: size, span, and the
    people who wrote it. Cheap (a few git calls) and highly recallable."""
    try:
        total = subprocess.run(
            ["git", "-C", clone_dir, "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
        authors = subprocess.run(
            ["git", "-C", clone_dir, "shortlog", "-sne", "--all"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60,
        )
        first = subprocess.run(
            ["git", "-C", clone_dir, "log", "--reverse", "--date=short",
             "--pretty=format:%ad", "--max-count=1"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
        last = subprocess.run(
            ["git", "-C", clone_dir, "log", "-1", "--date=short",
             "--pretty=format:%ad"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30,
        )
    except Exception:  # noqa: BLE001
        return ""
    if total.returncode != 0:
        return ""
    parts = [f"Repository history overview: {total.stdout.strip()} commits"]
    if first.returncode == 0 and last.returncode == 0 and first.stdout.strip():
        parts.append(f"spanning {first.stdout.strip()} to {last.stdout.strip()}")
    text = ", ".join(parts) + "."
    if authors.returncode == 0 and authors.stdout.strip():
        top = "\n".join(authors.stdout.strip().splitlines()[:15])
        text += f"\nContributors (commits, name, email):\n{top}"
    return text


def _git_commit_range(clone_dir: Optional[str], old: Optional[str],
                      new: Optional[str]) -> list[str]:
    if not clone_dir or not old or not new or old == new:
        return []
    try:
        out = subprocess.run(
            ["git", "-C", clone_dir, "log", "--oneline", "--no-decorate",
             f"{old}..{new}"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20,
        )
        if out.returncode != 0:
            return []
        return [line for line in out.stdout.splitlines() if line.strip()]
    except Exception:  # noqa: BLE001
        return []


def _git_diff_stat(clone_dir: Optional[str], old: Optional[str],
                   new: Optional[str]) -> str:
    if not clone_dir or not old or not new or old == new:
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", clone_dir, "diff", "--stat=120", f"{old}..{new}"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=20,
        )
        if out.returncode != 0:
            return ""
        lines = out.stdout.splitlines()
        return "\n".join(lines[-60:]).strip()
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Wiki-content memory (what the project IS -- ingested at generation time)
# ---------------------------------------------------------------------------

def record_wiki_content(*, owner: str, repo: str, version: int,
                        wiki_structure: Any, generated_pages: Any) -> None:
    """Ingest the wiki HackDeepWiki just generated into this release's own
    workspace, so chat and the code editor start out knowing what the project
    is and how it works instead of an empty memory.

    Deterministic: the pages were already written by the model during wiki
    generation, so summarizing them costs no extra tokens -- we store the
    structure plus a bounded excerpt of each page. Idempotent per release via
    the ingest-state file; never raises.
    """
    try:
        if get_service() is None:
            return
        workspace = workspace_for_version(owner, repo, version)
        ensure_workspace(
            workspace,
            f"Knowledge about {owner}/{repo} as documented by wiki release "
            f"v{version} (shared by chat and the code editor).",
        )
        if _has_memory(workspace, "wiki_content"):
            return

        pages = _pages_from_structure(wiki_structure, generated_pages)
        overview = _structure_overview(owner, repo, version, wiki_structure, pages)
        overview_id = ""
        if overview:
            overview_id = remember_detailed(
                workspace, overview, mtype="semantic", source="hackdeepwiki",
                title=f"What {owner}/{repo} is -- wiki v{version} overview",
                metadata={"kind": "wiki_overview", "version": version},
            )["id"]

        written = 0
        previous_page_id = ""
        for page in pages[:_MAX_WIKI_PAGES]:
            body = _page_memory(owner, repo, version, page)
            if not body:
                continue
            page_id = remember_detailed(
                workspace, body, mtype="semantic", source="hackdeepwiki",
                title=(page.get("title") or "Wiki page")[:120],
                metadata={"kind": "wiki_page", "version": version,
                          "page_id": page.get("id") or "",
                          "title": page.get("title") or ""},
            )["id"]
            # The wiki is a structure, not a bag of pages: every page belongs
            # to the release overview, and consecutive pages keep the reading
            # order the wiki itself has. Without these edges the graph view
            # shows one isolated node per page.
            if page_id and overview_id:
                link(workspace, page_id, overview_id, relation="part_of",
                     reason=f"documented by wiki v{version}")
            if page_id and previous_page_id:
                link(workspace, page_id, previous_page_id, relation="follows",
                     reason="next page in the wiki structure")
            previous_page_id = page_id or previous_page_id
            written += 1

        _mark_ingested(workspace, "wiki_content", version=version, pages=written)
        if overview_id:
            _mark_ingested(workspace, "wiki_overview", id=overview_id,
                           version=version)
        logger.info("Engraphis: ingested %s wiki pages of %s/%s v%s into %s",
                    written, owner, repo, version, workspace)
    except Exception as e:  # noqa: BLE001
        logger.warning("engraphis wiki-content ingest skipped: %s", e)


def _pages_from_structure(wiki_structure: Any, generated_pages: Any) -> list[dict]:
    """Merge the structure's page list with the generated content, keyed by id.

    Both come straight off the save_wiki_cache payload, which may hold pydantic
    models or plain dicts depending on the caller -- normalize to dicts.
    """
    def _as_dict(value: Any) -> dict:
        if isinstance(value, dict):
            return value
        dump = getattr(value, "model_dump", None)
        if callable(dump):
            try:
                return dump()
            except Exception:  # noqa: BLE001
                return {}
        return {}

    structure = _as_dict(wiki_structure)
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for raw in structure.get("pages") or []:
        page = _as_dict(raw)
        pid = str(page.get("id") or page.get("title") or len(order))
        by_id[pid] = dict(page)
        order.append(pid)

    generated = generated_pages if isinstance(generated_pages, dict) else {}
    for pid, raw in generated.items():
        page = _as_dict(raw)
        key = str(pid)
        if key in by_id:
            by_id[key].update({k: v for k, v in page.items() if v})
        else:
            by_id[key] = dict(page)
            order.append(key)
    return [by_id[pid] for pid in order if by_id.get(pid)]


def _structure_overview(owner: str, repo: str, version: int,
                        wiki_structure: Any, pages: list[dict]) -> str:
    structure = wiki_structure if isinstance(wiki_structure, dict) else (
        getattr(wiki_structure, "model_dump", lambda: {})() or {}
    )
    title = str(structure.get("title") or f"{owner}/{repo}").strip()
    description = str(structure.get("description") or "").strip()
    lines = [
        f"Project overview for {owner}/{repo} (wiki release v{version}): {title}."
    ]
    if description:
        lines.append(description[:800])
    if pages:
        titles = [str(p.get("title") or p.get("id") or "").strip()
                  for p in pages[:_MAX_WIKI_PAGES]]
        titles = [t for t in titles if t]
        if titles:
            lines.append("The wiki documents these areas: " + ", ".join(titles) + ".")
    return "\n".join(lines).strip()


def _page_memory(owner: str, repo: str, version: int, page: dict) -> str:
    title = str(page.get("title") or page.get("id") or "").strip()
    content = str(page.get("content") or "").strip()
    if not title and not content:
        return ""
    excerpt = _strip_markdown(content)[:_MAX_WIKI_PAGE_CHARS]
    files = page.get("filePaths") or page.get("file_paths") or []
    parts = [f"{owner}/{repo} -- {title} (from wiki v{version})."]
    if isinstance(files, list) and files:
        shown = [str(f) for f in files[:12] if f]
        if shown:
            parts.append("Relevant files: " + ", ".join(shown) + ".")
    if excerpt:
        parts.append(excerpt)
    return "\n".join(parts).strip()


def _strip_markdown(text: str) -> str:
    """Flatten markdown to prose so the excerpt carries meaning, not syntax.

    Fenced code blocks and mermaid diagrams are dropped entirely: they blow the
    character budget and the regex entity extractor turns their identifiers
    into junk graph nodes.
    """
    if not text:
        return ""
    out = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    out = re.sub(r"^\s*[|:-]{3,}.*$", " ", out, flags=re.MULTILINE)
    out = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", out)
    out = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", out)
    out = re.sub(r"[`*_>#]+", " ", out)
    out = re.sub(r"\s+", " ", out)
    return out.strip()
