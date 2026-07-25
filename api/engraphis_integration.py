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
* ``ENGRAPHIS_EMBED_MODEL=""`` -- the explicit offline opt-out: Engraphis uses
  its deterministic embedder (numpy-only) instead of trying to download
  sentence-transformers models. Recall stays fully functional offline
  (lexical + graph + deterministic vectors).
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

# Keep proactive/recall injections bounded so memory can never crowd out the
# actual question/context in a small-context model.
_MAX_INJECTED_MEMORIES = 6
_MAX_INJECTED_CHARS = 2400
_MAX_EVOLUTION_COMMITS = 40

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
# memories (the SPA's default).
_SHIM_JS = b"""\
(function () {
  'use strict';
  var ws = null;
  try { ws = new URLSearchParams(window.location.search).get('ws'); } catch (e) {}
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
    if (label.textContent === ws) return;
    try {
      window.setWS(ws);
      if (typeof window.navTo === 'function') window.navTo('overview');
      window.loadOverview();
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
            await self._send_asset(send, _SHIM_JS, b"application/javascript; charset=utf-8")
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


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
    }


def dashboard_url_for(workspace: str) -> Optional[str]:
    """Deep-linked dashboard URL for one workspace (see _SHIM_JS)."""
    _ensure_started()
    if _dashboard_port is None:
        return None
    from urllib.parse import quote
    return f"http://127.0.0.1:{_dashboard_port}/?ws={quote(workspace)}"


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


def remember(workspace: str, content: str, *, mtype: str = "semantic",
             session_id: Optional[str] = None, source: str = "agent",
             metadata: Optional[dict] = None) -> str:
    """Store one memory in a workspace. Returns a short outcome string."""
    svc = get_service()
    if svc is None:
        return "Memory is unavailable in this build (Engraphis not installed)."
    content = (content or "").strip()
    if not content:
        return "Nothing to remember: empty content."
    with _LOCK:
        try:
            result = svc.remember(
                content, workspace=workspace, mtype=mtype,
                session_id=session_id, source=source, metadata=metadata,
            )
            op = (result or {}).get("op") or "add"
            mem_id = (result or {}).get("id") or ""
            return f"Remembered ({op}) in workspace '{workspace}' [{mem_id}]."
        except Exception as e:  # noqa: BLE001
            logger.warning("engraphis remember failed: %s", e)
            return f"Could not store the memory: {e}"


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

        summary = (
            f"Wiki release v{version} of {owner}/{repo} ({repo_type}, "
            f"language '{language}') was generated"
        )
        summary += f" from commit {repo_commit[:12]}." if repo_commit else \
            " (no commit anchor -- non-git source or missing clone)."
        if previous_version is not None:
            summary += f" Previous release: v{previous_version}"
            summary += f" at commit {previous_commit[:12]}." if previous_commit else "."
        remember(workspace, summary, mtype="episodic", source="hackdeepwiki",
                 metadata={"kind": "wiki_release", "version": version,
                           "commit": repo_commit or ""})

        commits = _git_commit_range(clone_dir, previous_commit, repo_commit)
        if commits:
            listing = "\n".join(commits[:_MAX_EVOLUTION_COMMITS])
            extra = len(commits) - _MAX_EVOLUTION_COMMITS
            if extra > 0:
                listing += f"\n… and {extra} more commits"
            remember(
                workspace,
                f"Changes between wiki v{previous_version} and v{version} of "
                f"{owner}/{repo} (git log {previous_commit[:12]}..{repo_commit[:12]}):\n"
                f"{listing}",
                mtype="episodic", source="hackdeepwiki",
                metadata={"kind": "wiki_diff", "from_version": previous_version,
                          "to_version": version},
            )
        stats = _git_diff_stat(clone_dir, previous_commit, repo_commit)
        if stats:
            remember(
                workspace,
                f"Files changed between wiki v{previous_version} and v{version} "
                f"of {owner}/{repo}:\n{stats}",
                mtype="episodic", source="hackdeepwiki",
                metadata={"kind": "wiki_diffstat", "from_version": previous_version,
                          "to_version": version},
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("engraphis evolution record skipped: %s", e)


def _git_commit_range(clone_dir: Optional[str], old: Optional[str],
                      new: Optional[str]) -> list[str]:
    if not clone_dir or not old or not new or old == new:
        return []
    try:
        out = subprocess.run(
            ["git", "-C", clone_dir, "log", "--oneline", "--no-decorate",
             f"{old}..{new}"],
            capture_output=True, text=True, timeout=20,
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
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode != 0:
            return ""
        lines = out.stdout.splitlines()
        return "\n".join(lines[-60:]).strip()
    except Exception:  # noqa: BLE001
        return ""
