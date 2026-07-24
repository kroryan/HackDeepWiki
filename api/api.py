import os
import re
import io
import hmac
import html as html_lib
import shutil
import tempfile
import zipfile
import logging
import mimetypes
from fastapi import FastAPI, HTTPException, Query, WebSocket, Depends, Request, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, HTMLResponse, StreamingResponse
from starlette.background import BackgroundTask
from typing import List, Optional, Dict, Any, Literal
import json
from datetime import datetime, timezone
from pydantic import BaseModel, Field
import google.generativeai as genai
import asyncio

# Configure logging
from api.logging_config import setup_logging

setup_logging()
logger = logging.getLogger(__name__)


# Fase 3: lifespan that starts/stops the SQLite-backed job worker. Defined
# here (before app creation) so FastAPI can take it at construction. The
# import is local so a failure in the jobs module never blocks app import.
from contextlib import asynccontextmanager

@asynccontextmanager
async def _build_lifespan(_app):
    """Start the background job worker on startup, stop it on shutdown.
    Wrapped so an import/start error degrades gracefully (app still serves)
    instead of crashing the whole process."""
    started = False
    try:
        from api.jobs.queue import ensure_worker, stop_worker
        await ensure_worker()
        started = True
        logger.info("Job worker started (Fase 3)")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Job worker failed to start (jobs will not run): {e}")
    # Fase 9.6 -- prune the wiki cache against configured caps on launch so a
    # cache that accumulated past a cap (e.g. set after the fact) is reined in
    # without waiting for the next save. Best-effort + no-op unless the
    # operator opted in via env (see api.cache_eviction).
    try:
        from api.cache_eviction import prune_wiki_cache
        prune_wiki_cache()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"wiki cache startup prune skipped: {e}")
    try:
        yield
    finally:
        if started:
            try:
                from api.jobs.queue import stop_worker
                stop_worker()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Job worker stop failed: {e}")


# Initialize FastAPI app
app = FastAPI(
    title="Streaming API",
    description="API for streaming chat completions",
    # Fase 3: start the SQLite-backed job worker on startup, stop it on
    # shutdown. The worker is an in-process asyncio task (no external process
    # to supervise) -- the app's lifetime is the worker's, matching the
    # local-first, no-services constraint.
    lifespan=_build_lifespan,
)

# Configure CORS. HackDeepWiki is a local desktop app: the bundled Next.js
# frontend talks to this backend almost entirely through its own server-side
# route handlers (see src/app/api/**/route.ts) -- genuine cross-origin
# browser fetches never happen, and WebSocket upgrades (the one thing that
# *does* connect directly browser-to-backend, since WS can't go through a
# Next.js route handler) aren't covered by CORSMiddleware at all. So
# `allow_origins=["*"]` bought nothing functionally, while leaving the
# backend port reachable from JavaScript on *any* website the user happens
# to have open in a tab -- a classic "malicious site talks to your local
# server" drive-by. Restrict to actual local origins; the frontend's port is
# chosen dynamically at runtime (see next.config.ts), so this matches any
# localhost/127.0.0.1 port rather than a single hardcoded one.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Helper function to get the (guaranteed-writable) adalflow root path
from api.data_root import get_data_root as get_adalflow_default_root_path
from api import zim_reader
from api import zim_library
from api import fanwiki_library

# --- Pydantic Models ---
# The request/response schemas live in api.models (extracted from this file
# so the route module doesn't also own every schema). A few request models
# that are tightly co-located with their route groups (ModelProbeRequest, the
# Fanwiki* models, ZimImportRequest) are still defined inline below next to
# their routes.
from api.models import (
    AuthorizationConfig,
    FileContentRequest,
    Model,
    ModelConfig,
    PageEditAIRequest,
    PageEditRequest,
    ProcessedProjectEntry,
    Provider,
    RepoInfo,
    RepoStructureRequest,
    WikiCacheData,
    WikiCacheRequest,
    WikiExportRequest,
    WikiPage,
    WikiSection,
    WikiStructureModel,
)

from api.config import configs, WIKI_AUTH_MODE, WIKI_AUTH_CODE, DEFAULT_EXCLUDED_DIRS, DEFAULT_EXCLUDED_FILES
from api.config import normalize_language, language_display_name, is_supported_language
# Aliased: this module already defines its own route handler named
# get_model_config() (GET /models/config, a completely different zero-arg
# "list providers" endpoint) -- importing under the same name would shadow it.
from api.config import get_model_config as get_provider_model_config
from api.provider_streaming import stream_provider_response
from api.prompts import PAGE_EDIT_AI_SYSTEM_PROMPT
from api.security import sanitize_error_message
from api.mcp_server import handle_request as mcp_handle_request, get_runtime_token as mcp_runtime_token
from api.storage.wiki_search import index_wiki_cache, search as wiki_fts_search, drop_repo as wiki_fts_drop_repo
from api.storage.wiki_shares import (
    create_share, resolve_share, list_shares, delete_share,
)
from api.storage import repo_key as _repo_key

@app.get("/lang/config")
async def get_lang_config():
    return configs["lang_config"]

@app.get("/auth/status")
async def get_auth_status():
    """
    Check if authentication is required for the wiki.
    """
    return {"auth_required": WIKI_AUTH_MODE}

@app.post("/auth/validate")
async def validate_auth_code(request: AuthorizationConfig):
    """
    Check authorization code using a constant-time comparison to avoid timing
    side-channels on the secret. An empty configured code can never match
    (matches the `not authorization_code` guard used by the write endpoints).

    A simple in-memory sliding-window lockout rate-limits brute-force attempts
    against the passcode: after _AUTH_MAX_FAILED_ATTEMPTS failed attempts in
    the last _AUTH_LOCKOUT_WINDOW seconds, further attempts are rejected with
    429 until the window clears. (Local single-user app; per-IP tracking isn't
    meaningful here, so this is process-global -- sufficient to stop naive
    brute force while the constant-time compare already closes the timing
    channel.)
    """
    import time

    now = time.time()
    _AUTH_FAILED_ATTEMPTS[:] = [t for t in _AUTH_FAILED_ATTEMPTS if now - t < _AUTH_LOCKOUT_WINDOW]
    if len(_AUTH_FAILED_ATTEMPTS) >= _AUTH_MAX_FAILED_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many failed attempts. Try again later.")

    if not WIKI_AUTH_CODE or not request.code:
        return {"success": False}
    ok = hmac.compare_digest(WIKI_AUTH_CODE, request.code)
    if not ok:
        _AUTH_FAILED_ATTEMPTS.append(now)
    return {"success": ok}


# Sliding-window brute-force lockout for /auth/validate (process-global;
# see validate_auth_code). Configurable via env so an operator can widen it.
import time as _time  # noqa: E402
_AUTH_LOCKOUT_WINDOW = int(os.environ.get("HACKDEEPWIKI_AUTH_LOCKOUT_WINDOW", "300"))
_AUTH_MAX_FAILED_ATTEMPTS = int(os.environ.get("HACKDEEPWIKI_AUTH_MAX_FAILED", "10"))
_AUTH_FAILED_ATTEMPTS: list = []


def verify_authorization(authorization_code: Optional[str] = Query(None)):
    """Shared auth gate. When WIKI_AUTH_MODE is on, require a valid
    authorization_code (constant-time compare). Applied to read endpoints that
    enumerate the filesystem (/api/fs/list, /local_repo/structure) and to
    fanwiki inspect/attach_images -- these walk arbitrary local directories and
    must not be reachable unauthenticated if the operator has enabled auth
    (especially since the app may be exposed on the LAN). When auth is off
    (the local-first default), this is a no-op so behavior is unchanged."""
    if WIKI_AUTH_MODE and (
        not authorization_code or not WIKI_AUTH_CODE
        or not hmac.compare_digest(WIKI_AUTH_CODE, authorization_code)
    ):
        raise HTTPException(status_code=401, detail="Authorization code is invalid")
    return True

@app.get("/models/config", response_model=ModelConfig)
async def get_model_config():
    """
    Get available model providers and their models.

    This endpoint returns the configuration of available model providers and their
    respective models that can be used throughout the application.

    Returns:
        ModelConfig: A configuration object containing providers and their models
    """
    try:
        logger.info("Fetching model configurations")

        # Create providers from the config file
        providers = []
        default_provider = configs.get("default_provider", "google")

        # Add provider configuration based on config.py
        for provider_id, provider_config in configs["providers"].items():
            models = []
            # Add models from config
            for model_id in provider_config["models"].keys():
                # Get a more user-friendly display name if possible
                models.append(Model(id=model_id, name=model_id))

            # Add provider with its models
            providers.append(
                Provider(
                    id=provider_id,
                    name=f"{provider_id.capitalize()}",
                    supportsCustomModel=provider_config.get("supportsCustomModel", False),
                    models=models
                )
            )

        # Ensure other standard providers exist if not in config
        existing_ids = {p.id for p in providers}
        standard_providers = [
            ("google", "Google Gemini", ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-pro", "gemini-1.5-flash"]),
            ("openai", "OpenAI / Codex", ["gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"]),
            ("claude", "Anthropic Claude", ["claude-3-7-sonnet-latest", "claude-3-5-sonnet-latest", "claude-3-opus-latest", "claude-3-haiku-20240307"]),
            ("openai_custom", "Custom OpenAI-compatible", []),
        ]

        for p_id, p_name, p_models in standard_providers:
            if p_id not in existing_ids:
                providers.append(
                    Provider(
                        id=p_id,
                        name=p_name,
                        supportsCustomModel=True,
                        models=[Model(id=m, name=m) for m in p_models]
                    )
                )

        # Create and return the full configuration
        config = ModelConfig(
            providers=providers,
            defaultProvider=default_provider
        )
        return config

    except Exception as e:
        logger.error(f"Error creating model configuration: {str(e)}")
        # Return some default configuration in case of error
        return ModelConfig(
            providers=[
                Provider(
                    id="google",
                    name="Google",
                    supportsCustomModel=True,
                    models=[
                        Model(id="gemini-2.5-flash", name="Gemini 2.5 Flash")
                    ]
                )
            ],
        )

class ModelProbeRequest(BaseModel):
    endpoint: str = Field(..., description="Base URL of the provider endpoint")
    api_key: Optional[str] = Field(None, description="API key (optional for Ollama)")
    provider_type: str = Field("openai", description="'openai' or 'ollama'")

@app.post("/models/probe")
async def probe_models(request: ModelProbeRequest):
    """
    Probe an OpenAI-compatible or Ollama endpoint and return its model list.
    Used by the frontend to populate the model dropdown for custom providers.
    Tries multiple URL patterns to support various OpenAI-compatible APIs
    (Novita, Together, Groq, vLLM, etc.)
    """
    import httpx
    from urllib.parse import urlparse

    endpoint = request.endpoint.rstrip("/")

    def _is_local_host(url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return (
            host in ("localhost", "0.0.0.0")
            or host.startswith("127.")
            or host.startswith("192.168.")
            or host.startswith("10.")
        )

    # Normalize the scheme. Users often paste "localhost:11434" (no scheme) or
    # "https://localhost:11434"; plain-HTTP servers like Ollama then fail with
    # "[SSL: WRONG_VERSION_NUMBER]". Prefer http:// for local hosts and keep an
    # http:// fallback candidate when https was requested against one.
    if not endpoint.startswith(("http://", "https://")):
        scheme = "http" if _is_local_host(f"http://{endpoint}") else "https"
        endpoint = f"{scheme}://{endpoint}"

    endpoint_candidates = [endpoint]
    if endpoint.startswith("https://") and _is_local_host(endpoint):
        endpoint_candidates.append("http://" + endpoint[len("https://"):])

    headers = {"Accept": "application/json"}
    if request.api_key:
        headers["Authorization"] = f"Bearer {request.api_key}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if request.provider_type == "ollama":
                models = []
                last_error = None
                for base in endpoint_candidates:
                    # Ollama's native API lives at /api/tags, not /v1/api/tags. Users
                    # commonly save the OpenAI-compatible endpoint (".../v1", copied
                    # from docs for that mode) in the same field used here for the
                    # native API probe -- blindly appending /api/tags then produces
                    # a 404 against .../v1/api/tags. Strip a trailing /v1 first.
                    ollama_base = base[:-3] if base.endswith("/v1") else base
                    try:
                        resp = await client.get(f"{ollama_base}/api/tags", headers=headers)
                        resp.raise_for_status()
                        data = resp.json()
                        models = [
                            {"id": m["name"], "name": m["name"]}
                            for m in data.get("models", [])
                        ]
                        break
                    except Exception as url_err:
                        last_error = url_err
                        continue
                if not models and last_error:
                    raise last_error
            else:
                # Try multiple URL patterns for OpenAI-compatible endpoints
                # Different providers use different URL structures:
                # - OpenAI: https://api.openai.com/v1/models
                # - Novita: https://api.novita.ai/v3/openai/models
                # - Together: https://api.together.xyz/v1/models
                # - vLLM: http://localhost:8000/v1/models
                models = []
                urls_to_try = []
                for base in endpoint_candidates:
                    urls_to_try.append(f"{base}/models")     # If endpoint already includes /v1 or /v3/openai
                    urls_to_try.append(f"{base}/v1/models")  # Standard OpenAI format
                
                last_error = None
                for url in urls_to_try:
                    try:
                        resp = await client.get(url, headers=headers)
                        if resp.status_code == 200:
                            data = resp.json()
                            models = [
                                {"id": m["id"], "name": m.get("name", m["id"])}
                                for m in data.get("data", [])
                            ]
                            if models:
                                break
                    except Exception as url_err:
                        last_error = url_err
                        continue
                
                if not models and last_error:
                    return {"models": [], "error": f"Could not fetch models from any URL pattern. Last error: {last_error}"}
                    
        return {"models": models}
    except Exception as e:
        logger.warning(f"Model probe failed for {endpoint}: {e}")
        return {"models": [], "error": str(e)}


@app.post("/export/wiki")
async def export_wiki(request: WikiExportRequest):
    """
    Export wiki content as Markdown or JSON.

    Args:
        request: The export request containing wiki pages and format

    Returns:
        A downloadable file in the requested format
    """
    try:
        logger.info(f"Exporting wiki for {request.repo_url} in {request.format} format")

        # Extract repository name from URL for the filename
        repo_parts = request.repo_url.rstrip('/').split('/')
        repo_name = repo_parts[-1] if len(repo_parts) > 0 else "wiki"

        # Get current timestamp for the filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if request.format == "markdown":
            # Generate Markdown content
            content = generate_markdown_export(request.repo_url, request.pages)
            filename = f"{repo_name}_wiki_{timestamp}.md"
            media_type = "text/markdown"
        elif request.format == "obsidian":
            # Generate a full Obsidian vault (one .md per page with [[wikilinks]])
            # packaged as a downloadable .zip
            content = generate_obsidian_vault_export(
                request.repo_url,
                request.pages,
                title=request.title or f"{repo_name} Wiki",
                version=request.version,
                vuln_report=request.vuln_report,
                include_vulns=request.include_vulns,
                include_vuln_graph=request.include_vuln_graph,
            )
            version_suffix = f"_v{request.version}" if request.version else ""
            filename = f"{repo_name}_wiki{version_suffix}_{timestamp}_obsidian.zip"
            media_type = "application/zip"
        elif request.format == "hdwreader":
            # Portable offline bundle for the HackDeepWikiReader companion
            # app (Android/Linux/Windows) -- see generate_hdwreader_export.
            content = generate_hdwreader_export(
                repo_url=request.repo_url,
                repo_type=request.repo_type or "github",
                owner=request.owner or "",
                repo=request.repo or repo_name,
                pages=request.pages,
                sections=request.sections or [],
                root_sections=request.root_sections or [],
                title=request.title or f"{repo_name} Wiki",
                description=request.description or "",
                language=request.language or "en",
                provider=request.provider or "",
                model=request.model or "",
                version=request.version,
                vuln_report=request.vuln_report,
                include_vulns=request.include_vulns,
                web_vuln_report=request.web_vuln_report,
                include_web_vulns=request.include_web_vulns,
            )
            version_suffix = f"_v{request.version}" if request.version else ""
            filename = f"{repo_name}_wiki{version_suffix}_{timestamp}.hdwreader"
            media_type = "application/zip"
        elif request.format == "mediawiki_xml":
            # Standard MediaWiki export-0.11 XML -- the same format
            # api.fanwiki_import reads on the way in, so a generated wiki can
            # round-trip into a real MediaWiki instance (Special:Import) or
            # any other tool that already speaks this format, not just this
            # app's own hdwreader/Obsidian exports.
            from api.fanwiki_import import export_mediawiki_xml
            content = export_mediawiki_xml(
                pages=request.pages,
                sitename=request.title or f"{repo_name} Wiki",
                base_url=request.repo_url,
                language=request.language or "en",
            )
            version_suffix = f"_v{request.version}" if request.version else ""
            filename = f"{repo_name}_wiki{version_suffix}_{timestamp}.xml"
            media_type = "application/xml"
        elif request.format == "zim":
            # Offline archive readable by any Kiwix-family reader (or this
            # app's own HackDeepWikiReader, which already reads .zim files
            # for imported content) -- see api.zim_export for the shared
            # builder used by both this and the imported-fanwiki export.
            content = generate_zim_export(
                repo_url=request.repo_url,
                pages=request.pages,
                title=request.title or f"{repo_name} Wiki",
                description=request.description or "",
                language=request.language or "en",
            )
            version_suffix = f"_v{request.version}" if request.version else ""
            filename = f"{repo_name}_wiki{version_suffix}_{timestamp}.zim"
            media_type = "application/octet-stream"
        else:  # JSON format
            # Generate JSON content
            content = generate_json_export(request.repo_url, request.pages)
            filename = f"{repo_name}_wiki_{timestamp}.json"
            media_type = "application/json"

        # Create response with appropriate headers for file download
        response = Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f"attachment; filename={filename}"
            }
        )

        return response

    except Exception as e:
        error_msg = f"Error exporting wiki: {str(e)}"
        logger.error(error_msg)
        raise HTTPException(status_code=500, detail=error_msg)

@app.get("/local_repo/structure", dependencies=[Depends(verify_authorization)])
async def get_local_repo_structure(path: str = Query(None, description="Path to local repository")):
    """Return the file tree and README content for a local repository."""
    if not path:
        return JSONResponse(
            status_code=400,
            content={"error": "No path provided. Please provide a 'path' query parameter."}
        )

    if not os.path.isdir(path):
        return JSONResponse(
            status_code=404,
            content={"error": f"Directory not found: {path}"}
        )

    try:
        logger.info(f"Processing local repository at: {path}")
        file_tree_lines = []
        readme_content = ""

        # Same exclusion lists data_pipeline.py already applies when actually
        # embedding the repo (build/dist/cache/IDE dirs, lockfiles, binaries,
        # ...). This endpoint used to walk the raw filesystem with almost no
        # filtering (just hidden dirs/__pycache__/node_modules/.venv), so for
        # a large non-web project (e.g. a full game/engine source tree with
        # tens of thousands of files) the wiki-structure planning prompt --
        # which embeds this file_tree verbatim -- could balloon to hundreds
        # of thousands of tokens before a single relevant_files list was even
        # considered, blowing past any local model's context window. Applying
        # the same filters here keeps the tree limited to what the embedding
        # pipeline would actually consider anyway.
        excluded_dir_names = {d.strip("./").rstrip("/") for d in DEFAULT_EXCLUDED_DIRS} | {
            '__pycache__', 'node_modules', '.venv'
        }
        excluded_file_names = set(DEFAULT_EXCLUDED_FILES)

        def _is_excluded_file(name: str) -> bool:
            if name in excluded_file_names:
                return True
            for pattern in excluded_file_names:
                if pattern.startswith('*.') and name.endswith(pattern[1:]):
                    return True
            return False

        for root, dirs, files in os.walk(path):
            # Exclude hidden dirs, virtual envs, and the standard build/cache/
            # IDE/VCS noise dirs (see DEFAULT_EXCLUDED_DIRS in api/config.py).
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in excluded_dir_names]
            for file in files:
                if file.startswith('.') or file == '__init__.py' or file == '.DS_Store':
                    continue
                if _is_excluded_file(file):
                    continue
                rel_dir = os.path.relpath(root, path)
                rel_file = os.path.join(rel_dir, file) if rel_dir != '.' else file
                file_tree_lines.append(rel_file)
                # Find README.md (case-insensitive)
                if file.lower() == 'readme.md' and not readme_content:
                    try:
                        with open(os.path.join(root, file), 'r', encoding='utf-8') as f:
                            readme_content = f.read()
                    except Exception as e:
                        logger.warning(f"Could not read README.md: {str(e)}")
                        readme_content = ""

        file_tree_str = '\n'.join(sorted(file_tree_lines))
        return {"file_tree": file_tree_str, "readme": readme_content}
    except Exception as e:
        logger.error(f"Error processing local repository: {str(e)}")
        return JSONResponse(
            status_code=500,
            content={"error": f"Error processing local repository: {str(e)}"}
        )

# Wiki export-format generators live in api.exports (extracted from this
# module). They're pure with respect to app state -- all inputs are params --
# so the move is mechanical. The only consumer here is /export/wiki below.
from api.exports import (
    HDWREADER_FORMAT_VERSION,
    generate_hdwreader_export,
    generate_json_export,
    generate_markdown_export,
    generate_obsidian_vault_export,
    generate_zim_export,
)

# Import the simplified chat implementation
from api.simple_chat import chat_completions_stream
from api.websocket_wiki import handle_websocket_chat

# Add the chat_completions_stream endpoint to the main app
app.add_api_route("/chat/completions/stream", chat_completions_stream, methods=["POST"])

# Add the WebSocket endpoint. Registered via a decorator wrapper rather than
# FastAPI.add_websocket_route(handle_websocket_chat) -- that method (needed
# because the handler lives in a separate module, so it can't be decorated
# at its own definition site) was removed in newer FastAPI/Starlette. This
# thin wrapper is the drop-in replacement: functionally identical route,
# compatible with current and future versions.
@app.websocket("/ws/chat")
async def _ws_chat(websocket: WebSocket):
    await handle_websocket_chat(websocket)

# --- Wiki Cache Helper Functions ---

# Wiki-cache path layout lives in api.wiki_cache_paths (single source of truth,
# shared with mcp_tools + wiki_search so they locate caches the same way
# instead of re-declaring the prefix strings and drifting). Re-exported here
# under the names this module historically used, so the ~15 existing helpers
# below keep working unchanged.
from api.wiki_cache_paths import (
    WIKI_CACHE_DIR,
    WIKI_CACHE_FILE_PREFIX,
    LEGACY_WIKI_CACHE_FILE_PREFIX as _LEGACY_WIKI_CACHE_FILE_PREFIX,
    repo_cache_prefix as _repo_cache_prefix,
    repo_cache_prefixes as _repo_cache_prefixes,
    list_cache_files as _list_cache_files_raw,
)

# --- Vulnerability scan cache helpers ---
# Vulnerability reports live in the same portable wikicache dir as the wiki
# itself (no new storage location). Versioned the same way wiki releases are
# (_vN suffix, never overwritten) -- a plain single-file-per-repo/language
# cache that got silently replaced on every re-scan looked, from the user's
# side, exactly like scans "disappearing": run a scan, come back later after
# a re-scan (or just a server restart racing a fresh save), and the earlier
# result is simply gone with no way to get it back. Every scan is now its
# own release; reads default to the latest but any past one stays retrievable.
VULN_CACHE_PREFIX = "hackdeepwiki_vulns"
_LEGACY_VULN_CACHE_PREFIX = "freedeepwiki_vulns"  # pre-rename filename prefix


def _vuln_cache_prefix(repo_type: str, owner: str, repo: str, language: str,
                        prefix: str = VULN_CACHE_PREFIX) -> str:
    return f"{prefix}_{repo_type}_{owner}_{repo}_{language}"


def _vuln_cache_path(repo_type: str, owner: str, repo: str, language: str,
                      version: Optional[int] = None, prefix: str = VULN_CACHE_PREFIX) -> str:
    suffix = f"_v{version}" if version is not None else ""
    return os.path.join(
        WIKI_CACHE_DIR,
        f"{_vuln_cache_prefix(repo_type, owner, repo, language, prefix)}{suffix}.json",
    )


def _list_cache_files_for_prefix(prefix: str) -> List[str]:
    """Every JSON cache file (any version, plus any pre-versioning legacy
    single-file cache -- see _parse_cache_version, which treats a filename
    with no _vN suffix as version 0) matching an exact filename prefix."""
    try:
        return [
            os.path.join(WIKI_CACHE_DIR, fn)
            for fn in os.listdir(WIKI_CACHE_DIR)
            if fn.startswith(prefix) and fn.endswith(".json")
        ]
    except OSError:
        return []


def _next_version_for_prefix(prefix: str) -> int:
    files = _list_cache_files_for_prefix(prefix)
    max_version = 0
    for path in files:
        max_version = max(max_version, _parse_cache_version(os.path.basename(path)))
    return max_version + 1


def _latest_path_for_prefix(prefix: str) -> Optional[str]:
    files = _list_cache_files_for_prefix(prefix)
    if not files:
        return None
    files.sort(key=lambda p: _parse_cache_version(os.path.basename(p)))
    return files[-1]


def save_vuln_cache(report: dict) -> tuple[str, int]:
    """Persist a vulnerability report dict as a new versioned release (never
    overwrites a previous scan's file). Returns (path, version)."""
    repo_type = report.get("repo_type", "")
    owner = report.get("owner", "")
    repo = report.get("repo", "")
    language = report.get("language", "en")
    prefix = _vuln_cache_prefix(repo_type, owner, repo, language)
    version = _next_version_for_prefix(prefix)
    path = _vuln_cache_path(repo_type, owner, repo, language, version=version)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path, version


def read_vuln_cache(repo_type: str, owner: str, repo: str, language: str,
                     version: Optional[int] = None) -> Optional[dict]:
    if version is not None:
        path = _vuln_cache_path(repo_type, owner, repo, language, version=version)
        if not os.path.isfile(path):
            return None
    else:
        # Latest release under the current prefix, falling back to the
        # pre-rename (FreeDeepWiki) prefix so scans saved before the rename
        # are still found.
        path = _latest_path_for_prefix(_vuln_cache_prefix(repo_type, owner, repo, language))
        if path is None:
            path = _latest_path_for_prefix(
                _vuln_cache_prefix(repo_type, owner, repo, language, prefix=_LEGACY_VULN_CACHE_PREFIX))
            if path is None:
                return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        logger.warning("Failed to read vuln cache %s: %s", path, exc)
        return None

    # Route through the dataclass so a report saved before a field existed
    # (e.g. an older `graph` shape) comes back with defaults instead of
    # missing keys -- the frontend types several of these as required, so an
    # absent key would crash the Security Analysis panel on old reports.
    from api.vuln_scanner.models import VulnReport
    return VulnReport.from_dict(data).to_dict()


def list_vuln_cache_releases(repo_type: str, owner: str, repo: str, language: str) -> List[dict]:
    """Every saved scan release for a repo/language, newest first -- mirrors
    list_wiki_releases below."""
    prefixes = [
        _vuln_cache_prefix(repo_type, owner, repo, language),
        _vuln_cache_prefix(repo_type, owner, repo, language, prefix=_LEGACY_VULN_CACHE_PREFIX),
    ]
    files = [p for prefix in prefixes for p in _list_cache_files_for_prefix(prefix)]
    releases = []
    for path in files:
        filename = os.path.basename(path)
        version = _parse_cache_version(filename)
        try:
            mtime = os.path.getmtime(path)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            releases.append({
                "version": version,
                "created_at": int(mtime * 1000),
                "total_findings": data.get("total_findings"),
                "generated_at": data.get("generated_at"),
                "id": filename,
            })
        except Exception as e:
            logger.warning(f"Could not read vuln release metadata from {filename}: {e}")
            continue
    releases.sort(key=lambda r: (r["version"], r["created_at"]), reverse=True)
    return releases


# --- Website security scan cache helpers ---
# Same wikicache dir, own prefix -- keyed by (owner='website', repo=hostname,
# language), mirroring the dependency vuln cache above (same versioning, same
# reasoning: a single overwritten file per repo/language looked like scans
# disappearing on a re-scan or a restart racing a save).
WEB_VULN_CACHE_PREFIX = "hackdeepwiki_webvulns"
_LEGACY_WEB_VULN_CACHE_PREFIX = "freedeepwiki_webvulns"  # pre-rename filename prefix


def _web_vuln_cache_prefix(owner: str, repo: str, language: str,
                            prefix: str = WEB_VULN_CACHE_PREFIX) -> str:
    return f"{prefix}_{owner}_{repo}_{language}"


def _web_vuln_cache_path(owner: str, repo: str, language: str,
                          version: Optional[int] = None, prefix: str = WEB_VULN_CACHE_PREFIX) -> str:
    suffix = f"_v{version}" if version is not None else ""
    return os.path.join(
        WIKI_CACHE_DIR,
        f"{_web_vuln_cache_prefix(owner, repo, language, prefix)}{suffix}.json",
    )


def save_web_vuln_cache(report: dict) -> tuple[str, int]:
    """Persist a web vuln report dict as a new versioned release. Returns (path, version)."""
    owner = report.get("owner", "")
    repo = report.get("repo", "")
    language = report.get("language", "en")
    prefix = _web_vuln_cache_prefix(owner, repo, language)
    version = _next_version_for_prefix(prefix)
    path = _web_vuln_cache_path(owner, repo, language, version=version)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return path, version


def read_web_vuln_cache(owner: str, repo: str, language: str,
                         version: Optional[int] = None) -> Optional[dict]:
    if version is not None:
        path = _web_vuln_cache_path(owner, repo, language, version=version)
        if not os.path.isfile(path):
            return None
    else:
        path = _latest_path_for_prefix(_web_vuln_cache_prefix(owner, repo, language))
        if path is None:
            path = _latest_path_for_prefix(
                _web_vuln_cache_prefix(owner, repo, language, prefix=_LEGACY_WEB_VULN_CACHE_PREFIX))
            if path is None:
                return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        logger.warning("Failed to read web vuln cache %s: %s", path, exc)
        return None

    # Route through the dataclass so a report saved before a field was added
    # (e.g. `graph`, added after these caches already existed) comes back
    # with that field defaulted instead of missing -- the frontend types it
    # as required, so an absent key would crash the graph tab on old reports.
    from api.web_vuln_scanner.models import WebVulnReport
    return WebVulnReport.from_dict(data).to_dict()


def list_web_vuln_cache_releases(owner: str, repo: str, language: str) -> List[dict]:
    prefixes = [
        _web_vuln_cache_prefix(owner, repo, language),
        _web_vuln_cache_prefix(owner, repo, language, prefix=_LEGACY_WEB_VULN_CACHE_PREFIX),
    ]
    files = [p for prefix in prefixes for p in _list_cache_files_for_prefix(prefix)]
    releases = []
    for path in files:
        filename = os.path.basename(path)
        version = _parse_cache_version(filename)
        try:
            mtime = os.path.getmtime(path)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            releases.append({
                "version": version,
                "created_at": int(mtime * 1000),
                "total_findings": data.get("total_findings"),
                "generated_at": data.get("generated_at"),
                "id": filename,
            })
        except Exception as e:
            logger.warning(f"Could not read web vuln release metadata from {filename}: {e}")
            continue
    releases.sort(key=lambda r: (r["version"], r["created_at"]), reverse=True)
    return releases


def _split_newline_filters(value) -> List[str]:
    """Normalise the newline-separated filter strings the frontend sends
    (same convention as the chat request's excluded_dirs/excluded_files)."""
    if not value:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [line.strip() for line in str(value).splitlines() if line.strip()]


# WIKI_CACHE_FILE_PREFIX / _LEGACY_WIKI_CACHE_FILE_PREFIX / _repo_cache_prefix /
# _repo_cache_prefixes are imported from api.wiki_cache_paths above (single
# source of truth). The old local definitions lived here and were duplicated
# in mcp_tools, which is how the two drifted on the cache layout.


def _repo_has_any_cache(repo_type: str, owner: str, repo: str) -> bool:
    """Whether any wiki release -- in any language or comprehensive/concise
    variant -- still exists for this repo. Used to decide whether it's
    safe to also delete the repo's cloned-to-disk copy and embeddings db:
    those are shared across every release of a repo (keyed only by
    owner_repo, not by language/version), so they must only be removed
    once literally nothing references them anymore.
    """
    prefixes = (
        f"{WIKI_CACHE_FILE_PREFIX}{repo_type}_{owner}_{repo}_",
        f"{_LEGACY_WIKI_CACHE_FILE_PREFIX}{repo_type}_{owner}_{repo}_",
    )
    try:
        return any(
            filename.startswith(prefixes) and filename.endswith(".json")
            for filename in os.listdir(WIKI_CACHE_DIR)
        )
    except OSError:
        return True  # can't tell -- err on the side of not deleting shared data


def _delete_local_repo_clone(repo_type: str, owner: str, repo: str) -> None:
    """Removes the local git clone and embeddings db for an owner/repo,
    the same way DatabaseManager._create_repo names them
    (~/.adalflow/repos/{owner}_{repo}, ~/.adalflow/databases/{owner}_{repo}.pkl,
    or HACKDEEPWIKI_DATA_DIR's equivalent). Only ever called once
    _repo_has_any_cache confirms no wiki release still needs them --
    never for repo_type == 'local', where the "clone" is the user's own
    folder on disk, not something HackDeepWiki created.
    """
    root_path = get_adalflow_default_root_path()
    repo_name = f"{owner}_{repo}"
    clone_dir = os.path.join(root_path, "repos", repo_name)
    db_file = os.path.join(root_path, "databases", f"{repo_name}.pkl")
    if os.path.isdir(clone_dir):
        shutil.rmtree(clone_dir, ignore_errors=True)
        logger.info(f"Deleted local repo clone: {clone_dir}")
    if os.path.isfile(db_file):
        os.remove(db_file)
        logger.info(f"Deleted embeddings db: {db_file}")


def _parse_cache_version(filename: str) -> int:
    """Extract the ``_vN`` release version from a cache filename.

    Returns 0 for legacy caches that predate versioning (no ``_vN`` suffix).
    """
    m = re.search(r"_v(\d+)\.json$", filename)
    return int(m.group(1)) if m else 0


def get_wiki_cache_path(
    owner: str,
    repo: str,
    repo_type: str,
    language: str,
    comprehensive: Optional[bool] = None,
    page_count: Optional[int] = None,
    version: Optional[int] = None,
) -> str:
    """Generates the file path for a given wiki cache.

    When ``version`` is provided the filename carries a ``_v{version}`` suffix so
    each release is stored as its own file and an update never overwrites the
    previous wiki.
    """
    variant = ""
    if comprehensive is not None and page_count is not None:
        mode = "comprehensive" if comprehensive else "concise"
        variant = f"_{mode}_{page_count}"
    version_suffix = f"_v{version}" if version is not None else ""
    filename = (
        f"{WIKI_CACHE_FILE_PREFIX}{repo_type}_{owner}_{repo}_{language}{variant}{version_suffix}.json"
    )
    return os.path.join(WIKI_CACHE_DIR, filename)


def _list_repo_cache_files(repo_type: str, owner: str, repo: str, language: str) -> List[str]:
    """Return absolute paths of every cache file for one repo/language/type
    (both the current and pre-rename filename prefix -- see
    ``_repo_cache_prefixes``). Thin wrapper over the shared
    api.wiki_cache_paths.list_cache_files so this module and mcp_tools/
    wiki_search agree on what's a cache file."""
    try:
        return _list_cache_files_raw(repo_type, owner, repo, language)
    except Exception as e:
        logger.error(f"Error listing cache files: {e}")
        return []


def _next_cache_version(repo_type: str, owner: str, repo: str, language: str) -> int:
    """Next release version number = max existing version + 1 (min 1)."""
    files = _list_repo_cache_files(repo_type, owner, repo, language)
    max_version = 0
    for path in files:
        max_version = max(max_version, _parse_cache_version(os.path.basename(path)))
    return max_version + 1


async def read_wiki_cache(
    owner: str,
    repo: str,
    repo_type: str,
    language: str,
    comprehensive: Optional[bool] = None,
    page_count: Optional[int] = None,
    version: Optional[int] = None,
) -> Optional[WikiCacheData]:
    """Reads wiki cache data from the file system.

    If ``version`` is given, returns that specific release. Otherwise returns the
    latest release (highest ``_vN``; legacy files count as v0, ties broken by
    mtime). An optional ``comprehensive``/``page_count`` preference is honored
    when choosing among same-version variants, but the latest release is always
    preferred over an older exact-variant match so re-entering a wiki restores
    the most recent update instead of regenerating.
    """
    def _load(path: str) -> Optional[WikiCacheData]:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return WikiCacheData(**json.load(f))
        except Exception as e:
            logger.error(f"Error reading wiki cache from {path}: {e}")
            return None

    # Specific release requested -> find the file with that exact version.
    if version is not None:
        files = _list_repo_cache_files(repo_type, owner, repo, language)
        exact = [p for p in files if _parse_cache_version(os.path.basename(p)) == version]
        if exact:
            exact.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for cache_path in exact:
                cached = _load(cache_path)
                if cached:
                    logger.info(f"Loaded wiki release v{version} from {os.path.basename(cache_path)}")
                    return cached
        logger.info(f"Wiki release v{version} not found for {owner}/{repo}")
        return None

    # No version specified -> pick the latest release.
    files = _list_repo_cache_files(repo_type, owner, repo, language)
    if not files:
        return None

    # Sort by version desc, then mtime desc, so the newest release wins.
    files.sort(
        key=lambda p: (_parse_cache_version(os.path.basename(p)), os.path.getmtime(p)),
        reverse=True,
    )

    # Prefer the latest release whose variant matches the request, but fall back
    # to the latest release regardless of variant.
    def _matches_variant(cached: WikiCacheData) -> bool:
        if page_count is not None and len(cached.wiki_structure.pages) != page_count:
            return False
        if comprehensive is not None and cached.comprehensive is not None and cached.comprehensive != comprehensive:
            return False
        return True

    fallback = None
    for cache_path in files:
        cached = _load(cache_path)
        if not cached:
            continue
        if fallback is None:
            fallback = cached
        if _matches_variant(cached):
            logger.info(f"Using wiki release v{cached.version or 0} from {os.path.basename(cache_path)}")
            return cached

    if fallback:
        logger.info(
            "No exact cache variant match; falling back to %s",
            os.path.basename(files[0]),
        )
        return fallback
    return None

async def save_wiki_cache(data: WikiCacheRequest) -> Optional[int]:
    """Saves wiki cache data to the file system as a NEW versioned release.

    Each save creates a fresh ``_v{N}`` file (N = max existing version + 1) so an
    update never overwrites the previous wiki. Returns the assigned version, or
    None on failure.

    Dedupe guard: if the payload is identical to the newest existing release
    (ignoring the version number), no new file is written and the existing
    version is returned. This makes duplicate POSTs (double-fired frontend
    effects, retries, reloads) idempotent instead of minting v1..vN copies of
    the same wiki.
    """
    try:
        files = _list_repo_cache_files(
            data.repo.type, data.repo.owner, data.repo.repo, data.language
        )
        if files:
            files.sort(
                key=lambda p: (_parse_cache_version(os.path.basename(p)), os.path.getmtime(p)),
                reverse=True,
            )
            newest_path = files[0]
            with open(newest_path, "r", encoding="utf-8") as f:
                newest = WikiCacheData(**json.load(f))
            newest_version = _parse_cache_version(os.path.basename(newest_path))
            same_content = (
                newest.wiki_structure == data.wiki_structure
                and newest.generated_pages == data.generated_pages
                and newest.provider == data.provider
                and newest.model == data.model
            )
            if same_content:
                logger.info(
                    f"Wiki cache save skipped: payload identical to existing release "
                    f"v{newest_version} ({os.path.basename(newest_path)})"
                )
                return newest_version if newest_version > 0 else 0
    except Exception as dedupe_error:
        logger.warning(f"Wiki cache dedupe check failed (saving anyway): {dedupe_error}")

    next_version = _next_cache_version(
        data.repo.type, data.repo.owner, data.repo.repo, data.language
    )
    cache_path = get_wiki_cache_path(
        data.repo.owner,
        data.repo.repo,
        data.repo.type,
        data.language,
        data.comprehensive,
        data.page_count,
        version=next_version,
    )
    logger.info(f"Attempting to save wiki cache as release v{next_version}. Path: {cache_path}")
    try:
        payload = WikiCacheData(
            wiki_structure=data.wiki_structure,
            generated_pages=data.generated_pages,
            repo=data.repo,
            provider=data.provider,
            model=data.model,
            comprehensive=data.comprehensive,
            page_count=data.page_count,
            version=next_version,
        )
        # Log size of data to be cached for debugging (avoid logging full content if large)
        try:
            payload_json = payload.model_dump_json()
            payload_size = len(payload_json.encode('utf-8'))
            logger.info(f"Payload prepared for caching. Size: {payload_size} bytes.")
        except Exception as ser_e:
            logger.warning(f"Could not serialize payload for size logging: {ser_e}")


        logger.info(f"Writing cache file to: {cache_path}")
        with open(cache_path, 'w', encoding='utf-8') as f:
            json.dump(payload.model_dump(), f, indent=2)
        logger.info(f"Wiki cache successfully saved to {cache_path} (release v{next_version})")

        # Fase 2: keep the FTS5 wiki-search index in sync with the cache so a
        # /api/wiki/search right after a save finds the new content. Best-effort
        # -- a failure here must never roll back a successful cache write, so
        # the wiki itself is never lost to an indexing error.
        try:
            pages_for_index = []
            ws = payload.wiki_structure
            if ws and ws.pages:
                pages_for_index.extend(p.model_dump() for p in ws.pages)
            if payload.generated_pages:
                pages_for_index.extend(p.model_dump() for p in payload.generated_pages.values())
            if pages_for_index:
                index_wiki_cache(
                    data.repo.owner, data.repo.repo, data.repo.type,
                    data.language, pages_for_index, version=f"v{next_version}",
                )
        except Exception as idx_e:
            logger.warning(f"FTS wiki index update failed (cache saved anyway): {idx_e}")

        # Fase 9.6 -- enforce the configured cache caps now that the cache
        # just grew. Best-effort (a failure here must never roll back a
        # successful save); only ever reclaims OLDER surplus releases, never a
        # repo's newest. No-op unless the operator opts in via env (see
        # api.cache_eviction).
        try:
            from api.cache_eviction import prune_wiki_cache
            prune_wiki_cache()
        except Exception as ev_e:  # noqa: BLE001
            logger.warning(f"wiki cache prune skipped (cache saved anyway): {ev_e}")

        return next_version
    except IOError as e:
        logger.error(f"IOError saving wiki cache to {cache_path}: {e.strerror} (errno: {e.errno})", exc_info=True)
        return None
    except Exception as e:
        logger.error(f"Unexpected error saving wiki cache to {cache_path}: {e}", exc_info=True)
        return None

# --- Wiki Cache API Endpoints ---

@app.get("/api/wiki_cache", response_model=Optional[WikiCacheData])
async def get_cached_wiki(
    owner: str = Query(..., description="Repository owner"),
    repo: str = Query(..., description="Repository name"),
    repo_type: str = Query(..., description="Repository type (e.g., github, gitlab)"),
    language: str = Query(..., description="Language of the wiki content"),
    comprehensive: Optional[bool] = Query(
        None,
        description="Whether the wiki uses comprehensive sections",
    ),
    page_count: Optional[int] = Query(
        None,
        ge=1,
        le=50,
        description="Requested number of wiki pages",
    ),
    version: Optional[int] = Query(
        None,
        ge=0,
        description="Specific wiki release version to load (latest if omitted)",
    ),
):
    """
    Retrieves cached wiki data (structure and generated pages) for a repository.
    """
    # Language validation
    language = normalize_language(language)

    logger.info(f"Attempting to retrieve wiki cache for {owner}/{repo} ({repo_type}), lang: {language}, version: {version}")
    cached_data = await read_wiki_cache(
        owner,
        repo,
        repo_type,
        language,
        comprehensive,
        page_count,
        version,
    )
    if cached_data:
        return cached_data
    else:
        # Return 200 with null body if not found, as frontend expects this behavior
        logger.info(f"Wiki cache not found for {owner}/{repo} ({repo_type}), lang: {language}")
        return None

@app.get("/api/wiki_cache/releases")
async def list_wiki_releases(
    owner: str = Query(..., description="Repository owner"),
    repo: str = Query(..., description="Repository name"),
    repo_type: str = Query(..., description="Repository type (e.g., github, gitlab)"),
    language: str = Query(..., description="Language of the wiki content"),
):
    """
    Lists every saved release (version) of a repository's wiki, newest first.
    Used by the frontend's "Wiki Release" dropdown to let the user read any
    previously generated version instead of an update silently overwriting it.
    """
    language = normalize_language(language)

    files = _list_repo_cache_files(repo_type, owner, repo, language)
    releases = []
    for path in files:
        filename = os.path.basename(path)
        version = _parse_cache_version(filename)
        try:
            mtime = os.path.getmtime(path)
            with open(path, "r", encoding="utf-8") as f:
                cached = WikiCacheData(**json.load(f))
            releases.append({
                "version": version,
                "created_at": int(mtime * 1000),
                "comprehensive": cached.comprehensive,
                "page_count": len(cached.wiki_structure.pages),
                "provider": cached.provider,
                "model": cached.model,
                "title": cached.wiki_structure.title,
                "id": filename,
            })
        except Exception as e:
            logger.warning(f"Could not read release metadata from {filename}: {e}")
            continue

    # Newest release first (version desc, then mtime desc).
    releases.sort(key=lambda r: (r["version"], r["created_at"]), reverse=True)
    return {"releases": releases, "latest": releases[0]["version"] if releases else None}

@app.post("/api/wiki_cache")
async def store_wiki_cache(request_data: WikiCacheRequest):
    """
    Stores generated wiki data (structure and pages) to the server-side cache as
    a new versioned release. Returns the assigned version so the frontend can
    select it in the Wiki Release dropdown.
    """
    # Language validation
    request_data.language = normalize_language(request_data.language)

    logger.info(f"Attempting to save wiki cache for {request_data.repo.owner}/{request_data.repo.repo} ({request_data.repo.type}), lang: {request_data.language}")
    version = await save_wiki_cache(request_data)
    if version is not None:
        return {"message": "Wiki cache saved successfully", "version": version}
    else:
        raise HTTPException(status_code=500, detail="Failed to save wiki cache")

@app.patch("/api/wiki_cache/page")
async def edit_wiki_page(request_data: PageEditRequest):
    """
    Saves manually or AI-edited content for a single wiki page. Reuses
    read_wiki_cache/save_wiki_cache as-is (same dedupe + versioning), so an
    edit is just "load a release, replace one page's content, save as a new
    release" -- it never touches the cache file format directly.
    """
    language = normalize_language(request_data.language)

    cached = await read_wiki_cache(
        request_data.repo.owner,
        request_data.repo.repo,
        request_data.repo.type,
        language,
        version=request_data.version,
    )
    if cached is None:
        raise HTTPException(status_code=404, detail="Wiki cache not found for this repository/language")
    if request_data.page_id not in cached.generated_pages:
        raise HTTPException(status_code=404, detail=f"Page '{request_data.page_id}' not found in this wiki release")

    updated_pages = dict(cached.generated_pages)
    updated_pages[request_data.page_id] = updated_pages[request_data.page_id].model_copy(
        update={"content": request_data.content}
    )

    save_request = WikiCacheRequest(
        repo=request_data.repo,
        language=language,
        wiki_structure=cached.wiki_structure,
        generated_pages=updated_pages,
        provider=cached.provider or "",
        model=cached.model or "",
        comprehensive=cached.comprehensive if cached.comprehensive is not None else True,
        page_count=cached.page_count if cached.page_count is not None else len(cached.wiki_structure.pages),
    )
    new_version = await save_wiki_cache(save_request)
    if new_version is None:
        raise HTTPException(status_code=500, detail="Failed to save edited page")
    return {"message": "Page updated successfully", "version": new_version, "page_id": request_data.page_id}

@app.post("/api/wiki/page/edit/stream")
async def edit_wiki_page_ai_stream(request_data: PageEditAIRequest):
    """
    Streams an AI-rewritten version of a single wiki page back to the
    caller. Purely a proposal -- nothing is persisted here; the frontend
    saves it (if the user accepts it) via PATCH /api/wiki_cache/page like
    any manual edit.
    """
    language_name = language_display_name(request_data.language)

    prompt = PAGE_EDIT_AI_SYSTEM_PROMPT.format(
        page_title=request_data.page_title,
        current_content=request_data.current_content,
        instruction=request_data.instruction,
        language_name=language_name,
    )
    model_config = get_provider_model_config(request_data.provider, request_data.model)["model_kwargs"]

    async def response_stream():
        try:
            async for text in stream_provider_response(
                provider=request_data.provider,
                requested_model=request_data.model,
                prompt=prompt,
                model_config_kwargs=model_config,
                api_key=request_data.api_key,
                api_endpoint=request_data.api_endpoint,
            ):
                yield text
        except Exception as e:
            logger.error(f"Error in page edit AI stream: {e}")
            yield f"\nError: {str(e)}"

    return StreamingResponse(response_stream(), media_type="text/event-stream")

@app.post("/api/wiki/file_content")
async def get_wiki_file_content(request_data: FileContentRequest):
    """Full, untruncated content of one repo file, for the in-app code
    viewer opened by clicking a "sources consulted" file citation in a repo
    chat (see api/search_tool.py's format_sources_footer, which renders
    those citations as `codefile:` links the frontend intercepts instead of
    navigating). Unlike the chat agent's own READ_FILE tool
    (api/search_tool.py's read_file), this never truncates -- a human
    reading the file in a dedicated viewer should see all of it, not a
    context-budget-limited excerpt."""
    from api.data_pipeline import get_file_content as _get_file_content
    try:
        content = _get_file_content(request_data.repo_url, request_data.file_path, request_data.repo_type, request_data.token)
    except Exception as e:
        logger.error(f"Error reading file {request_data.file_path!r}: {e}")
        raise HTTPException(status_code=404, detail=f"Could not read file: {e}")
    return {"file_path": request_data.file_path, "content": content}

@app.post("/api/repo/structure")
async def get_repo_structure_endpoint(request_data: RepoStructureRequest):
    """File tree + README for a repo, read from a local clone (made fresh
    via a normal blocking `git clone` if one doesn't exist yet) instead of
    the GitHub/GitLab/Bitbucket REST API -- avoids that API's rate limit
    for the "determine wiki structure" step exactly like /api/wiki/file_content
    already does for individual source-file citations. HTTP fallback for
    /ws/repo/clone below when a WebSocket isn't available; blocks for the
    whole clone instead of streaming progress.
    """
    from api.data_pipeline import get_repo_structure as _get_repo_structure
    try:
        structure = await asyncio.to_thread(
            _get_repo_structure, request_data.repo_url, request_data.repo_type, request_data.token, request_data.force
        )
    except Exception as e:
        logger.error(f"Error building repo structure for {request_data.repo_url}: {e}")
        raise HTTPException(status_code=502, detail=f"Could not read repository structure: {e}")
    return structure

@app.websocket("/ws/repo/clone")
async def ws_repo_clone(websocket: WebSocket):
    """Clones (or reuses an existing local clone of) a github/gitlab/
    bitbucket repo and streams git's own --progress phases back as they
    happen, so "open a repo for the first time" shows a real progress bar
    instead of a silent multi-second hang -- then sends the resulting file
    tree + README in one final message. See fetchRepoStructureViaBackendClone
    in src/app/[owner]/[repo]/page.tsx for the client side.
    """
    await websocket.accept()
    try:
        raw = await websocket.receive_text()
        payload = json.loads(raw)
        repo_url = (payload.get("repo_url") or "").strip()
        repo_type = payload.get("repo_type") or "github"
        token = payload.get("token") or None
        force = bool(payload.get("force", False))

        if not repo_url:
            await websocket.send_json({"type": "error", "message": "repo_url is required"})
            return

        from api.data_pipeline import (
            _local_clone_dir as _get_local_clone_dir,
            clone_repo_with_progress,
            _walk_repo_tree,
            _repo_default_branch,
        )

        local_dir = _get_local_clone_dir(repo_url, repo_type)

        async def on_progress(evt):
            await websocket.send_json({"type": "progress", **evt})

        await clone_repo_with_progress(repo_url, local_dir, repo_type, token, on_progress, force=force)

        tree, readme_content = await asyncio.to_thread(_walk_repo_tree, local_dir)
        default_branch = await asyncio.to_thread(_repo_default_branch, local_dir)

        await websocket.send_json({
            "type": "done",
            "default_branch": default_branch,
            "tree": tree,
            "readme": readme_content,
        })
    except Exception as e:
        logger.error(f"Error in /ws/repo/clone: {e}")
        try:
            await websocket.send_json({"type": "error", "message": sanitize_error_message(str(e))})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.websocket("/ws/website/crawl")
async def ws_website_crawl(websocket: WebSocket):
    """Crawls a website with headless Chromium (Playwright) and writes each
    page to disk as a Markdown file mirroring the site's URL structure, then
    streams progress back exactly like /ws/repo/clone does for git repos --
    the resulting local directory is handed to the same wiki generation code
    path afterwards (see repo_type == "website" handling throughout).

    Inbound: one JSON message: {start_url, scope: {mode, max_pages,
    subdomains, respect_robots}, fresh}.
    Outbound: {"type":"progress",...} frames, then a final
    {"type":"done","local_dir","page_count","tree","readme"} (or
    {"type":"error","message"}).
    """
    await websocket.accept()
    try:
        raw = await websocket.receive_text()
        payload = json.loads(raw)

        start_url = (payload.get("start_url") or "").strip()
        if not start_url:
            await websocket.send_json({"type": "error", "message": "start_url is required"})
            return
        if not start_url.startswith(("http://", "https://")):
            start_url = f"https://{start_url}"

        scope_payload = payload.get("scope") or {}
        fresh = bool(payload.get("fresh", False))

        from api.web_crawler.models import CrawlScope
        scope = CrawlScope(
            mode=scope_payload.get("mode") or "count",
            max_pages=int(scope_payload.get("max_pages") or 60),
            subdomains=_split_newline_filters(scope_payload.get("subdomains")) or [],
            respect_robots=bool(scope_payload.get("respect_robots", True)),
        )

        from api.web_crawler.orchestrator import run_site_crawl
        from api.web_crawler.site_store import website_local_dir

        async def on_progress(evt):
            await websocket.send_json({
                "type": "progress", "message": evt.message,
                "pages_done": evt.pages_done, "percent": evt.percent,
            })

        result = await run_site_crawl(start_url, scope, on_progress, fresh=fresh)

        if result["page_count"] == 0:
            # Silently "succeeding" with an empty crawl used to sail straight
            # into wiki-structure planning against a 0-document RAG index,
            # surfacing as a baffling "No valid XML found in response" several
            # steps later with nothing pointing back at the real cause. Fail
            # here instead, with the most likely reason from crawl_site's own
            # diagnostics (see its docstring) -- bot challenge (Cloudflare
            # "Just a moment..." and similar are common on fan wikis/Fandom
            # sites and cannot be solved by a plain headless browser) is
            # called out specifically since it's both the most common cause
            # and the least obvious to a user who just sees "0 pages".
            diag = result.get("diagnostics") or {}
            if diag.get("bot_challenge"):
                reason = ("El sitio parece estar protegido por un desafío anti-bots (p. ej. Cloudflare "
                           "\"Just a moment...\"), que un navegador headless no puede superar automáticamente.")
            elif diag.get("robots_blocked"):
                reason = "El robots.txt del sitio no permite el rastreo (puedes desactivar \"respetar robots.txt\" si tienes permiso para rastrearlo)."
            elif diag.get("http_error"):
                reason = "El sitio devolvió un error HTTP al intentar acceder a él."
            elif diag.get("fetch_failed"):
                reason = "No se pudo conectar con el sitio (¿la URL es correcta y está accesible?)."
            else:
                reason = "No se encontró contenido de página válido en el sitio."
            await websocket.send_json({
                "type": "error",
                "message": f"El rastreo no encontró ninguna página. {reason}",
            })
            return

        from api.data_pipeline import _walk_repo_tree
        tree, _ = await asyncio.to_thread(_walk_repo_tree, result["local_dir"])
        library_entry = await asyncio.to_thread(
            fanwiki_library.get_by_start_url, result["start_url"]
        )

        await websocket.send_json({
            "type": "done",
            "id": library_entry["id"] if library_entry else None,
            "local_dir": result["local_dir"],
            "page_count": result["page_count"],
            "tree": tree,
        })
    except Exception as e:
        logger.error(f"Error in /ws/website/crawl: {e}")
        try:
            await websocket.send_json({"type": "error", "message": sanitize_error_message(str(e))})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.get("/api/fs/list", dependencies=[Depends(verify_authorization)])
async def fs_list(
    path: Optional[str] = Query(None, description="Absolute directory to list; defaults to the user's home directory"),
    extensions: Optional[str] = Query(None, description="Comma-separated file extensions to include (e.g. '.xml'); omit to list every file"),
):
    """Lists one local directory's contents -- a generic, reusable "browse
    the filesystem" endpoint (not specific to fanwiki) so any picker in the
    app (the fanwiki XML file, the images folder, ...) can offer real
    navigation instead of requiring the user to type an absolute path from
    memory, the same way a native "Open File" dialog would. No sandboxing
    beyond what every other local-path field in this app already has (repo
    clone dir, ZIM import, local repo path, ...): this is a local desktop
    app operating on the user's own machine, not a multi-tenant server.
    """
    target = path or os.path.expanduser("~")
    target = os.path.abspath(target)
    if not os.path.isdir(target):
        raise HTTPException(status_code=400, detail=f"Not a directory: {target}")

    ext_filter = None
    if extensions:
        ext_filter = {e.strip().lower() for e in extensions.split(",") if e.strip()}

    entries = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                if entry.name.startswith('.'):
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=True)
                except OSError:
                    continue
                if not is_dir and ext_filter:
                    _, ext = os.path.splitext(entry.name)
                    if ext.lower() not in ext_filter:
                        continue
                size = None
                if not is_dir:
                    try:
                        size = entry.stat().st_size
                    except OSError:
                        pass
                entries.append({"name": entry.name, "is_dir": is_dir, "size": size})
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied: {target}")

    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    root = os.path.abspath(os.sep)
    parent = None if target == root else (os.path.dirname(target) or root)

    return {"path": target, "parent": parent, "entries": entries}


class FanwikiInspectRequest(BaseModel):
    path: str = Field(..., description="Local path to a MediaWiki XML export (Special:Export) file")


@app.post("/api/fanwiki/inspect", dependencies=[Depends(verify_authorization)])
async def fanwiki_inspect(request: FanwikiInspectRequest):
    """Reads just the <siteinfo> header of a MediaWiki XML export -- fast
    even for a multi-GB dump, since it stops as soon as that (always small)
    element closes -- so the import UI can show the wiki's namespaces for
    the user to choose which ones to import. See api.fanwiki_import."""
    path = request.path.strip()
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=400, detail=f"File not found: {path}")
    from api.fanwiki_import import inspect_dump
    try:
        info = await asyncio.to_thread(inspect_dump, path)
    except Exception as e:
        logger.error(f"Error inspecting fanwiki dump {path}: {e}")
        raise HTTPException(status_code=400, detail=f"Could not read MediaWiki XML: {e}")
    return {
        "sitename": info.sitename,
        "base_url": info.base_url,
        "dbname": info.dbname,
        "file_size": info.file_size,
        "namespaces": [{"key": ns.key, "name": ns.name} for ns in info.namespaces],
    }


@app.websocket("/ws/fanwiki/import")
async def ws_fanwiki_import(websocket: WebSocket):
    """Streams a MediaWiki XML export dump into a local Markdown tree (see
    api.fanwiki_import) -- the fanwiki-import counterpart of
    /ws/website/crawl, sharing the exact same on-disk layout so the result
    is generated exactly like a website wiki (see repo_type == "fanwiki"
    handling throughout, and api.fanwiki_import's module docstring).

    Inbound: one JSON message: {path, namespaces: [int]|null, images_dir,
    fresh, max_pages}. `namespaces: null` means "import every namespace" --
    the user's explicit choice, not a default (see
    api.fanwiki_import.import_dump).
    Outbound: {"type":"progress",...} frames, then a final
    {"type":"done","local_dir","page_count","image_count","links_resolved",
    "links_unresolved","tree","start_url"} (or {"type":"error","message"}).
    """
    await websocket.accept()
    try:
        raw = await websocket.receive_text()
        payload = json.loads(raw)

        path = (payload.get("path") or "").strip()
        if not path:
            await websocket.send_json({"type": "error", "message": "path is required"})
            return
        if not os.path.isfile(path):
            await websocket.send_json({"type": "error", "message": f"File not found: {path}"})
            return

        namespaces_payload = payload.get("namespaces")
        allowed_namespaces = set(namespaces_payload) if namespaces_payload is not None else None
        images_dir = (payload.get("images_dir") or "").strip() or None
        fresh = bool(payload.get("fresh", False))
        max_pages = payload.get("max_pages")

        from api.fanwiki_import import inspect_dump, import_dump, ImportProgress

        dump_info = await asyncio.to_thread(inspect_dump, path)

        # import_dump is a long, purely synchronous loop (ElementTree.
        # iterparse has no async form) so it runs in a worker thread; its
        # progress callback is therefore called from that thread and must be
        # bridged back onto this coroutine's own event loop to actually send
        # a WebSocket frame.
        loop = asyncio.get_running_loop()

        def on_progress(p: "ImportProgress") -> None:
            async def _send():
                try:
                    await websocket.send_json({
                        "type": "progress", "message": p.message,
                        "pages_done": p.pages_done, "percent": p.percent,
                    })
                except Exception:
                    pass
            asyncio.run_coroutine_threadsafe(_send(), loop)

        result = await asyncio.to_thread(
            import_dump, path, dump_info, allowed_namespaces,
            on_progress, fresh, 25, max_pages, images_dir,
        )

        from api.data_pipeline import _walk_repo_tree
        tree, _ = await asyncio.to_thread(_walk_repo_tree, result["local_dir"])

        await websocket.send_json({
            "type": "done",
            "local_dir": result["local_dir"],
            "page_count": result["page_count"],
            "image_count": result["image_count"],
            "links_resolved": result["links_resolved"],
            "links_unresolved": result["links_unresolved"],
            "start_url": result["start_url"],
            "tree": tree,
        })
    except Exception as e:
        logger.error(f"Error in /ws/fanwiki/import: {e}")
        try:
            await websocket.send_json({"type": "error", "message": sanitize_error_message(str(e))})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.get("/api/fanwiki/structure")
async def fanwiki_structure(start_url: str = Query(..., description="The fanwiki's synthetic start URL")):
    """Read-only file tree of an already-imported fanwiki -- the fanwiki
    counterpart of /local_repo/structure, and deliberately never crawls or
    imports anything itself. fetchRepositoryStructure calls this on *every*
    visit to a fanwiki wiki page (not just right after importing), so unlike
    /ws/website/crawl it must never attempt any network/import side effect --
    that's the whole reason repo_type == "fanwiki" exists as something
    distinct from "website" in the first place (see _create_repo in
    api/data_pipeline.py)."""
    from api.web_crawler.site_store import website_local_dir
    from api.data_pipeline import _walk_repo_tree
    local_dir = website_local_dir(start_url)
    if not os.path.isdir(local_dir):
        raise HTTPException(status_code=404, detail=f"No imported fanwiki found for {start_url}")
    tree, _ = await asyncio.to_thread(_walk_repo_tree, local_dir)
    return {"tree": tree, "local_dir": local_dir}


@app.delete("/api/fanwiki/imported")
async def delete_imported_fanwiki(
    start_url: str = Query(..., description="Exact start URL of the imported fanwiki"),
    authorization_code: Optional[str] = Query(None, description="Authorization code"),
):
    """Delete an imported XML source tree after verifying its manifest.

    This is deliberately separate from wiki-cache deletion: an import can
    exist without a generated release, and deleting a generated release
    should not silently destroy the reusable source material.
    """
    if WIKI_AUTH_MODE and (
        not authorization_code or not WIKI_AUTH_CODE
        or not hmac.compare_digest(WIKI_AUTH_CODE, authorization_code)
    ):
        raise HTTPException(status_code=401, detail="Authorization code is invalid")
    deleted = await asyncio.to_thread(fanwiki_library.delete, start_url)
    if not deleted:
        raise HTTPException(status_code=404, detail="Imported fanwiki source not found")
    return {"message": "Imported fanwiki source deleted successfully"}


class FanwikiRepairLinksRequest(BaseModel):
    start_url: str = Field(..., description="The fanwiki's synthetic start URL, as returned by /ws/fanwiki/import")


@app.post("/api/fanwiki/repair_links")
async def fanwiki_repair_links(request: FanwikiRepairLinksRequest):
    """Re-runs internal-link resolution for an already-imported fanwiki
    without re-importing the whole dump (see
    api.fanwiki_import.repair_internal_links) -- exposed standalone, as
    requested, so the user can re-trigger it later (e.g. after importing a
    second batch of namespaces into the same site) without waiting through
    a full re-import."""
    from api.web_crawler.site_store import website_local_dir
    from api.fanwiki_import import repair_internal_links

    if fanwiki_library.get_by_start_url(request.start_url) is None:
        raise HTTPException(status_code=404, detail="Imported fanwiki source not found")
    local_dir = website_local_dir(request.start_url)
    if not os.path.isdir(local_dir):
        raise HTTPException(status_code=404, detail=f"No imported fanwiki found for {request.start_url}")
    try:
        result = await asyncio.to_thread(repair_internal_links, local_dir)
    except Exception as e:
        logger.error(f"Error repairing fanwiki links for {request.start_url}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "files_scanned": result.files_scanned,
        "links_resolved": result.links_resolved,
        "links_unresolved": result.links_unresolved,
    }


class FanwikiAttachImagesRequest(BaseModel):
    start_url: str = Field(..., description="The fanwiki's synthetic start URL, as returned by /ws/fanwiki/import")
    images_dir: str = Field(..., description="Local folder of images to match against unresolved [[File:...]] references")


@app.post("/api/fanwiki/attach_images", dependencies=[Depends(verify_authorization)])
async def fanwiki_attach_images(request: FanwikiAttachImagesRequest):
    """Attaches images to an already-imported fanwiki after the fact (see
    api.fanwiki_import.attach_images) -- the images folder doesn't need to
    exist, or be complete, at import time; the user can come back and
    supply it, or a better one, whenever they have it, without re-importing
    the XML dump."""
    from api.web_crawler.site_store import website_local_dir
    from api.fanwiki_import import attach_images

    if fanwiki_library.get_by_start_url(request.start_url) is None:
        raise HTTPException(status_code=404, detail="Imported fanwiki source not found")
    local_dir = website_local_dir(request.start_url)
    if not os.path.isdir(local_dir):
        raise HTTPException(status_code=404, detail=f"No imported fanwiki found for {request.start_url}")
    if not os.path.isdir(request.images_dir):
        raise HTTPException(status_code=400, detail=f"Images folder not found: {request.images_dir}")
    try:
        result = await asyncio.to_thread(attach_images, local_dir, request.images_dir)
    except Exception as e:
        logger.error(f"Error attaching images for {request.start_url}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "files_scanned": result.files_scanned,
        "images_attached": result.images_attached,
        "images_still_missing": result.images_still_missing,
    }


def _get_fanwiki_entry_or_404(fanwiki_id: str) -> Dict:
    entry = fanwiki_library.get(fanwiki_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Imported fanwiki not found")
    return entry


@app.get("/api/fanwiki/{fanwiki_id}")
async def get_fanwiki_metadata(fanwiki_id: str):
    """Metadata for the direct XML-wiki reader.

    Importing a MediaWiki dump already produces readable Markdown articles;
    generating an LLM summary wiki is optional and must not be a prerequisite
    for opening that imported source.
    """
    return _get_fanwiki_entry_or_404(fanwiki_id)


@app.get("/api/fanwiki/{fanwiki_id}/index")
async def get_fanwiki_index(
    fanwiki_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
):
    _get_fanwiki_entry_or_404(fanwiki_id)
    return await asyncio.to_thread(fanwiki_library.page_index, fanwiki_id, offset, limit)


@app.get("/api/fanwiki/{fanwiki_id}/search")
async def search_fanwiki(
    fanwiki_id: str,
    q: str = Query(..., min_length=1),
    limit: int = Query(30, ge=1, le=100),
):
    _get_fanwiki_entry_or_404(fanwiki_id)
    return await asyncio.to_thread(fanwiki_library.search, fanwiki_id, q, limit)


@app.get("/api/fanwiki/{fanwiki_id}/page")
async def get_fanwiki_page(fanwiki_id: str, path: str = Query(..., min_length=1)):
    _get_fanwiki_entry_or_404(fanwiki_id)
    try:
        return await asyncio.to_thread(fanwiki_library.read_page, fanwiki_id, path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Fanwiki page not found: {path}")


@app.get("/api/fanwiki/{fanwiki_id}/asset")
async def get_fanwiki_asset(fanwiki_id: str, path: str = Query(..., min_length=1)):
    _get_fanwiki_entry_or_404(fanwiki_id)
    try:
        asset_path = await asyncio.to_thread(fanwiki_library.resolve_asset, fanwiki_id, path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Fanwiki asset not found: {path}")
    media_type = mimetypes.guess_type(asset_path)[0] or "application/octet-stream"
    return FileResponse(
        asset_path,
        media_type=media_type,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@app.get("/api/fanwiki/{fanwiki_id}/export/{export_format}")
async def export_imported_fanwiki(fanwiki_id: str, export_format: str):
    """Download a complete XML import without requiring LLM generation.

    FileResponse streams a temporary archive instead of duplicating a large
    wiki in memory. The archive is removed after the response completes.
    """
    entry = _get_fanwiki_entry_or_404(fanwiki_id)
    if export_format not in {"obsidian", "hdwreader", "zim"}:
        raise HTTPException(
            status_code=400,
            detail="Unsupported export format. Use obsidian, hdwreader, or zim.",
        )

    suffix = {"obsidian": ".zip", "hdwreader": ".hdwreader", "zim": ".zim"}[export_format]
    fd, archive_path = tempfile.mkstemp(prefix="hackdeepwiki-export-", suffix=suffix)
    os.close(fd)
    try:
        exporter = {
            "obsidian": fanwiki_library.export_obsidian,
            "hdwreader": fanwiki_library.export_hdwreader,
            "zim": fanwiki_library.export_zim,
        }[export_format]
        result = await asyncio.to_thread(exporter, fanwiki_id, archive_path)
    except (KeyError, FileNotFoundError):
        try:
            os.unlink(archive_path)
        except FileNotFoundError:
            pass
        raise HTTPException(status_code=404, detail="Imported fanwiki source is incomplete")
    except Exception as exc:
        try:
            os.unlink(archive_path)
        except FileNotFoundError:
            pass
        logger.exception(
            "Failed to export imported fanwiki %s as %s", fanwiki_id, export_format
        )
        raise HTTPException(status_code=500, detail=str(exc))

    safe_name = re.sub(
        r"[^A-Za-z0-9._-]+", "_", str(entry.get("repo") or "fanwiki")
    ).strip("._")
    filename = {
        "obsidian": f"{safe_name or 'fanwiki'}_obsidian.zip",
        "hdwreader": f"{safe_name or 'fanwiki'}.hdwreader",
        "zim": f"{safe_name or 'fanwiki'}.zim",
    }[export_format]
    media_type = "application/zip" if export_format != "zim" else "application/octet-stream"
    return FileResponse(
        archive_path,
        media_type=media_type,
        filename=filename,
        headers={
            "X-HackDeepWiki-Page-Count": str(result["page_count"]),
            "X-HackDeepWiki-Asset-Count": str(result["asset_count"]),
        },
        background=BackgroundTask(os.unlink, archive_path),
    )


@app.websocket("/ws/vuln_scan")
async def ws_vuln_scan(websocket: WebSocket):
    """Runs a CVE vulnerability scan over the locally-cloned repo and streams
    progress back, then returns the full report (and persists it to wikicache).

    Sequential with wiki generation by design: the repo must already be cloned
    locally (wiki generation clones it via DatabaseManager._create_repo / the
    /ws/repo/clone flow), so this runs *after* the wiki is generated and reuses
    the exact same clone dir -- no second download.

    Inbound: one JSON message with the scan parameters.
    Outbound: {"type":"progress","message","percent"} frames, then a final
    {"type":"done","report":{...}} (or {"type":"error","message"}).

    See runVulnScan() in src/app/[owner]/[repo]/page.tsx for the client side.
    """
    await websocket.accept()
    try:
        raw = await websocket.receive_text()
        payload = json.loads(raw)

        repo_url = (payload.get("repo_url") or "").strip()
        repo_type = payload.get("repo_type") or "github"
        language = payload.get("language") or "en"
        provider = payload.get("provider") or "google"
        model = payload.get("model") or None
        api_key = payload.get("api_key") or None
        api_endpoint = payload.get("api_endpoint") or None
        local_path = (payload.get("local_path") or "").strip()
        owner = payload.get("owner") or ""
        repo = payload.get("repo") or ""
        token = payload.get("token") or None
        force = bool(payload.get("force", False))
        nvd_key = payload.get("nvd_key") or None
        enable_client = bool(payload.get("enable_client", True))
        enable_server = bool(payload.get("enable_server", True))
        enable_deps = bool(payload.get("enable_deps", True))
        run_llm = bool(payload.get("run_llm", True))
        excluded_dirs = _split_newline_filters(payload.get("excluded_dirs"))
        excluded_files = _split_newline_filters(payload.get("excluded_files"))

        if not repo_url and not local_path:
            await websocket.send_json(
                {"type": "error", "message": "repo_url or local_path is required"})
            return

        # Resolve the local clone dir (reuse the same clone wiki gen made).
        if repo_type == "local":
            repo_dir = local_path or repo_url
        else:
            from api.data_pipeline import _local_clone_dir as _get_local_clone_dir, clone_repo_with_progress
            repo_dir = _get_local_clone_dir(repo_url, repo_type)
            # "Rescan" (force=True, set by the manual rerun button -- see
            # RescanConfigModal's onSubmit in page.tsx) must reflect the
            # repo's *current* remote state, not whatever happened to be
            # cloned whenever the wiki was last generated. Without this, a
            # manual rescan silently re-scanned a stale clone and always
            # reproduced the exact same findings even after new commits
            # landed upstream -- indistinguishable from "the rescan did
            # nothing." The automatic scan that fires right after wiki
            # generation/refresh doesn't set force: that clone is already
            # as fresh as this request can make it.
            if force and repo_url:
                await websocket.send_json(
                    {"type": "progress", "message": "Refreshing repository clone…", "percent": 0})
                try:
                    await clone_repo_with_progress(repo_url, repo_dir, repo_type, token, None, force=True)
                except Exception as exc:
                    logger.warning("Force re-clone before vuln scan failed (scanning existing clone instead): %s", exc)

        if not repo_dir or not os.path.isdir(repo_dir):
            await websocket.send_json({"type": "error",
                "message": (f"Repository clone not found at {repo_dir}. "
                            "Generate the wiki first so the repo is cloned locally.")})
            return

        from api.vuln_scanner.orchestrator import run_vuln_scan

        async def on_progress(msg: str, pct: Optional[int] = None):
            await websocket.send_json(
                {"type": "progress", "message": msg, "percent": pct})

        report = await run_vuln_scan(
            repo_dir=repo_dir, repo_url=repo_url, repo_type=repo_type,
            owner=owner, repo=repo, language=language,
            provider=provider, model=model, api_key=api_key,
            api_endpoint=api_endpoint,
            excluded_dirs=excluded_dirs, excluded_files=excluded_files,
            nvd_key=nvd_key, enable_client=enable_client,
            enable_server=enable_server, enable_deps=enable_deps,
            run_llm=run_llm, on_progress=on_progress,
        )

        report_dict = report.to_dict()
        saved_version: Optional[int] = None
        try:
            _, saved_version = save_vuln_cache(report_dict)
        except Exception as exc:
            logger.warning("Failed to persist vuln cache: %s", exc)

        await websocket.send_json({"type": "done", "report": report_dict, "version": saved_version})
    except Exception as e:
        logger.error(f"Error in /ws/vuln_scan: {e}")
        try:
            await websocket.send_json({"type": "error", "message": sanitize_error_message(str(e))})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.get("/api/vuln_cache")
async def get_vuln_cache(
    owner: str = Query(..., description="Repository owner"),
    repo: str = Query(..., description="Repository name"),
    repo_type: str = Query(..., description="Repository type (github, gitlab, bitbucket, local)"),
    language: str = Query("en", description="Wiki language the scan was run for"),
    version: Optional[int] = Query(None, description="Specific scan release version; omit for the latest"),
):
    """Return the stored vulnerability report for a repo/language, or 404 if
    none exists (so the frontend can decide whether to offer/run a scan).
    Defaults to the latest scan release; pass `version` for an older one."""
    data = read_vuln_cache(repo_type, owner, repo, language, version=version)
    if data is None:
        raise HTTPException(status_code=404,
                            detail="No vulnerability scan found for this repo.")
    return data


@app.get("/api/vuln_cache/releases")
async def get_vuln_cache_releases(
    owner: str = Query(..., description="Repository owner"),
    repo: str = Query(..., description="Repository name"),
    repo_type: str = Query(..., description="Repository type (github, gitlab, bitbucket, local)"),
    language: str = Query("en", description="Wiki language the scan was run for"),
):
    """Lists every saved dependency-vulnerability scan release for a
    repo/language, newest first -- mirrors /api/wiki_cache/releases."""
    releases = list_vuln_cache_releases(repo_type, owner, repo, language)
    return {"releases": releases, "latest": releases[0]["version"] if releases else None}


@app.websocket("/ws/web_vuln_scan")
async def ws_web_vuln_scan(websocket: WebSocket):
    """Runs a website security scan (headers/cookies/TLS/exposed paths/ports
    via pure Python + the optional Docker toolkit -- nmap/nikto/httpx/
    whatweb/testssl/nuclei/subfinder/ffuf/dalfox/wpscan) and streams progress
    back, then returns the full report (persisted to wikicache).

    Sequential with the site crawl by design: the site must already be
    crawled (via /ws/website/crawl), so this reads the crawl manifest for
    its sample of URLs -- no second crawl.

    Inbound: one JSON message with the scan parameters.
    Outbound: {"type":"progress","message","percent"} frames, then a final
    {"type":"done","report":{...}} (or {"type":"error","message"}).
    """
    await websocket.accept()
    try:
        raw = await websocket.receive_text()
        payload = json.loads(raw)

        site_url = (payload.get("site_url") or "").strip()
        owner = payload.get("owner") or "website"
        repo = payload.get("repo") or ""
        language = payload.get("language") or "en"
        provider = payload.get("provider") or "google"
        model = payload.get("model") or None
        api_key = payload.get("api_key") or None
        api_endpoint = payload.get("api_endpoint") or None
        run_llm = bool(payload.get("run_llm", True))
        enable_deep_scan = bool(payload.get("enable_deep_scan", False))

        if not site_url:
            await websocket.send_json({"type": "error", "message": "site_url is required"})
            return

        from api.web_vuln_scanner.orchestrator import run_web_vuln_scan

        async def on_progress(msg: str, pct: Optional[int] = None):
            await websocket.send_json(
                {"type": "progress", "message": msg, "percent": pct})

        report = await run_web_vuln_scan(
            site_url=site_url, owner=owner, repo=repo, language=language,
            provider=provider, model=model, api_key=api_key,
            api_endpoint=api_endpoint, run_llm=run_llm,
            enable_deep_scan=enable_deep_scan, on_progress=on_progress,
        )

        report_dict = report.to_dict()
        saved_version: Optional[int] = None
        try:
            _, saved_version = save_web_vuln_cache(report_dict)
        except Exception as exc:
            logger.warning("Failed to persist web vuln cache: %s", exc)

        await websocket.send_json({"type": "done", "report": report_dict, "version": saved_version})
    except Exception as e:
        logger.error(f"Error in /ws/web_vuln_scan: {e}")
        try:
            await websocket.send_json({"type": "error", "message": sanitize_error_message(str(e))})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.get("/api/web_vuln_cache")
async def get_web_vuln_cache(
    owner: str = Query(..., description="Repository owner (typically 'website')"),
    repo: str = Query(..., description="Site hostname"),
    language: str = Query("en", description="Wiki language the scan was run for"),
    version: Optional[int] = Query(None, description="Specific scan release version; omit for the latest"),
):
    """Return the stored website vulnerability report, or 404 if none exists.
    Defaults to the latest scan release; pass `version` for an older one."""
    data = read_web_vuln_cache(owner, repo, language, version=version)
    if data is None:
        raise HTTPException(status_code=404,
                            detail="No website vulnerability scan found for this site.")
    return data


@app.get("/api/web_vuln_cache/releases")
async def get_web_vuln_cache_releases(
    owner: str = Query(..., description="Repository owner (typically 'website')"),
    repo: str = Query(..., description="Site hostname"),
    language: str = Query("en", description="Wiki language the scan was run for"),
):
    """Lists every saved website security scan release, newest first."""
    releases = list_web_vuln_cache_releases(owner, repo, language)
    return {"releases": releases, "latest": releases[0]["version"] if releases else None}


@app.delete("/api/wiki_cache")
async def delete_wiki_cache(
    owner: str = Query(..., description="Repository owner"),
    repo: str = Query(..., description="Repository name"),
    repo_type: str = Query(..., description="Repository type (e.g., github, gitlab)"),
    language: str = Query(..., description="Language of the wiki content"),
    authorization_code: Optional[str] = Query(None, description="Authorization code"),
    comprehensive: Optional[bool] = Query(None),
    page_count: Optional[int] = Query(None, ge=1, le=50),
    version: Optional[int] = Query(
        None,
        ge=0,
        description="Delete only this release version. If omitted, deletes all releases.",
    ),
):
    """
    Deletes wiki cache file(s) from the file system.

    With versioning, an update no longer deletes — it creates a new release. An
    explicit delete targets either a single release (``version`` given) or every
    release of the repo/language/type (``version`` omitted).
    """
    # Language validation (strict: reject unsupported instead of coercing)
    if not is_supported_language(language):
        raise HTTPException(status_code=400, detail="Language is not supported")

    if WIKI_AUTH_MODE:
        logger.info("check the authorization code")
        if not authorization_code or not WIKI_AUTH_CODE:
            raise HTTPException(status_code=401, detail="Authorization code is invalid")
        if not hmac.compare_digest(WIKI_AUTH_CODE, authorization_code):
            raise HTTPException(status_code=401, detail="Authorization code is invalid")

    logger.info(f"Attempting to delete wiki cache for {owner}/{repo} ({repo_type}), lang: {language}, version: {version}")

    prefixes = _repo_cache_prefixes(repo_type, owner, repo, language)
    cache_paths = []
    try:
        for filename in os.listdir(WIKI_CACHE_DIR):
            if not filename.endswith(".json"):
                continue
            if not any(filename == f"{p}.json" or filename.startswith(f"{p}_") for p in prefixes):
                continue
            if version is not None and _parse_cache_version(filename) != version:
                continue
            cache_paths.append(os.path.join(WIKI_CACHE_DIR, filename))
    except Exception as e:
        logger.error(f"Error scanning cache dir for deletion: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to scan wiki cache: {str(e)}")

    deleted_paths = []
    try:
        for cache_path in dict.fromkeys(cache_paths):
            if os.path.exists(cache_path):
                os.remove(cache_path)
                deleted_paths.append(cache_path)
                logger.info(f"Successfully deleted wiki cache: {cache_path}")
    except Exception as e:
        logger.error(f"Error deleting wiki cache {cache_path}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete wiki cache: {str(e)}")

    if deleted_paths:
        # The clone and embeddings db are shared by every release of this
        # repo (any language, any comprehensive/concise variant) -- only
        # safe to remove once none of them reference it anymore, and never
        # for a 'local' repo (that "clone" is the user's own folder).
        if repo_type in ("github", "gitlab", "bitbucket") and not _repo_has_any_cache(repo_type, owner, repo):
            try:
                _delete_local_repo_clone(repo_type, owner, repo)
            except Exception as e:
                logger.warning(f"Failed to delete local repo clone for {owner}/{repo}: {e}")
        return {
            "message": (
                f"Wiki cache for {owner}/{repo} ({language}) deleted successfully"
            )
        }
    else:
        logger.warning(
            "Wiki cache not found for %s/%s (%s), lang: %s",
            owner,
            repo,
            repo_type,
            language,
        )
        raise HTTPException(status_code=404, detail="Wiki cache not found")


@app.post("/api/wiki_cache/prune")
async def prune_wiki_cache_endpoint(
    max_age_days: Optional[int] = Query(None, ge=0, description="Override HACKDEEPWIKI_WIKI_CACHE_MAX_AGE_DAYS (0=off)"),
    max_bytes: Optional[int] = Query(None, ge=0, description="Override HACKDEEPWIKI_WIKI_CACHE_MAX_BYTES (0=off)"),
    max_files: Optional[int] = Query(None, ge=0, description="Override HACKDEEPWIKI_WIKI_CACHE_MAX_FILES (0=off)"),
):
    """Manually run the wiki-cache eviction (Fase 9.6) and report what was
    reclaimed. Only ever removes OLDER surplus releases -- a repo's newest
    release is always protected. Query params override the env-configured caps
    for this run (0 = that cap disabled); omitted = use env / default."""
    from api.cache_eviction import prune_wiki_cache
    try:
        return prune_wiki_cache(max_age_days=max_age_days, max_bytes=max_bytes, max_files=max_files)
    except Exception as e:
        logger.error(f"wiki cache prune failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))


@app.delete("/api/vuln_cache")
async def delete_vuln_cache_release(
    owner: str = Query(..., description="Repository owner"),
    repo: str = Query(..., description="Repository name"),
    repo_type: str = Query(..., description="Repository type (github, gitlab, bitbucket, local)"),
    language: str = Query("en", description="Wiki language the scan was run for"),
    version: Optional[int] = Query(
        None, ge=0,
        description="Delete only this release version. If omitted, deletes all releases.",
    ),
):
    """Deletes dependency-vulnerability scan release(s) -- mirrors DELETE /api/wiki_cache."""
    prefixes = [
        _vuln_cache_prefix(repo_type, owner, repo, language),
        _vuln_cache_prefix(repo_type, owner, repo, language, prefix=_LEGACY_VULN_CACHE_PREFIX),
    ]
    deleted_paths = []
    for prefix in prefixes:
        for path in _list_cache_files_for_prefix(prefix):
            if version is not None and _parse_cache_version(os.path.basename(path)) != version:
                continue
            try:
                os.remove(path)
                deleted_paths.append(path)
                logger.info(f"Successfully deleted vuln cache: {path}")
            except Exception as e:
                logger.error(f"Error deleting vuln cache {path}: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to delete vuln cache: {str(e)}")

    if not deleted_paths:
        raise HTTPException(status_code=404, detail="Vulnerability scan cache not found")
    return {"message": f"Vulnerability scan cache for {owner}/{repo} ({language}) deleted successfully"}


@app.delete("/api/web_vuln_cache")
async def delete_web_vuln_cache_release(
    owner: str = Query(..., description="Repository owner (typically 'website')"),
    repo: str = Query(..., description="Site hostname"),
    language: str = Query("en", description="Wiki language the scan was run for"),
    version: Optional[int] = Query(
        None, ge=0,
        description="Delete only this release version. If omitted, deletes all releases.",
    ),
):
    """Deletes website security scan release(s) -- mirrors DELETE /api/wiki_cache."""
    prefixes = [
        _web_vuln_cache_prefix(owner, repo, language),
        _web_vuln_cache_prefix(owner, repo, language, prefix=_LEGACY_WEB_VULN_CACHE_PREFIX),
    ]
    deleted_paths = []
    for prefix in prefixes:
        for path in _list_cache_files_for_prefix(prefix):
            if version is not None and _parse_cache_version(os.path.basename(path)) != version:
                continue
            try:
                os.remove(path)
                deleted_paths.append(path)
                logger.info(f"Successfully deleted web vuln cache: {path}")
            except Exception as e:
                logger.error(f"Error deleting web vuln cache {path}: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to delete web vuln cache: {str(e)}")

    if not deleted_paths:
        raise HTTPException(status_code=404, detail="Website vulnerability scan cache not found")
    return {"message": f"Website vulnerability scan cache for {owner}/{repo} ({language}) deleted successfully"}


@app.get("/health")
async def health_check():
    """Health check endpoint for Docker and monitoring"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "hackdeepwiki-api"
    }

# ---- MCP server (Fase 1) ---------------------------------------------------
# Exposes the wiki of a generated repo as MCP tools (search_wiki/read_doc/
# list_wiki_structure/read_file/ask_repo) over JSON-RPC 2.0. Implemented
# from stdlib -- no `mcp` pip dependency (portable). Gated by a runtime
# token when HACKDEEPWIKI_MCP_TOKEN is set; local-first default is opt-in
# (no token => auth off, like WIKI_AUTH_MODE).
@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """Single-request JSON-RPC 2.0 entrypoint for MCP clients. A client
    POSTs one request, gets one response. Supports initialize / tools/list /
    tools/call. Streaming/SSE is out of scope for the local-first app."""
    try:
        req = await request.json()
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {sanitize_error_message(str(e))}"}},
        )
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    # handle_request does the token check itself; we pass the header through.
    resp = mcp_handle_request(req, auth_header=auth)
    status = 200
    if "error" in resp and resp["error"].get("code") == -32001:
        status = 401
    return JSONResponse(status_code=status, content=resp)


@app.get("/mcp/token")
async def mcp_token():
    """Surface the runtime MCP token so the UI can show it for the user to
    paste into their MCP client config. The token is per-process unless
    HACKDEEPWIKI_MCP_TOKEN is set explicitly (in which case that value is
    returned)."""
    token = mcp_runtime_token()
    # Don't echo the full token over an unauthenticated endpoint when one
    # was explicitly configured -- that would defeat gating. Local-first
    # (auto) mode returns it in full since it's meant to be copied.
    if os.environ.get("HACKDEEPWIKI_MCP_TOKEN"):
        return {"configured": True, "token": token, "hint": "Set via HACKDEEPWIKI_MCP_TOKEN env."}
    return {"configured": False, "token": token, "hint": "Per-process token; rotate on restart. Set HACKDEEPWIKI_MCP_TOKEN to pin it."}

# ---- Fase 2: wiki full-text search + shareable links ----------------------

@app.get("/api/wiki/search")
async def wiki_search_endpoint(
    q: str = Query(..., description="Search query"),
    owner: Optional[str] = Query(None, description="Filter to one owner"),
    repo: Optional[str] = Query(None, description="Filter to one repo"),
    language: Optional[str] = Query(None, description="Filter to one language"),
    limit: int = Query(20, ge=1, le=100, description="Max results"),
):
    """Full-text search across all generated wikis (FTS5). Returns ranked
    hits with a snippet each, enough to deep-link to the page. Spans repos
    by default; scope with owner/repo/language. Only finds wikis that have
    been indexed (indexing happens on save, see save_wiki_cache)."""
    try:
        hits = wiki_fts_search(q, owner=owner, repo=repo, language=language, limit=limit)
        return {"query": q, "count": len(hits), "results": hits}
    except Exception as e:
        logger.error(f"wiki search failed for {q!r}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))


@app.post("/api/share")
async def create_share_endpoint(
    owner: Optional[str] = Query(None),
    repo: str = Query(...),
    repo_type: str = Query("github"),
    language: str = Query(...),
    version: Optional[str] = Query(None),
    title: Optional[str] = Query(None),
):
    """Mint a shareable link ID for one wiki release. The link resolves to
    the repo/language/version pointer; the wiki content is read on demand
    from the wikicache, so sharing doesn't duplicate content. Idempotent:
    re-sharing the same release returns the existing ID."""
    try:
        share_id = create_share(owner, repo, repo_type, language, version=version, title=title)
        return {"share_id": share_id, "url": f"/share/{share_id}"}
    except Exception as e:
        logger.error(f"create share failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))


@app.get("/api/share/{share_id}")
async def get_share_endpoint(share_id: str):
    """Resolve a share ID to its wiki pointer (repo/language/version). The
    frontend /share/[id] page calls this, then loads the wiki via
    /api/wiki_cache. Returns 404 for unknown/expired shares or shares whose
    referenced wiki has been deleted (resolved by checking the cache exists)."""
    try:
        resolved = resolve_share(share_id)
    except Exception as e:
        logger.error(f"resolve share {share_id} failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))
    if not resolved:
        raise HTTPException(status_code=404, detail="Share not found or expired")
    # Confirm the referenced wiki still exists on disk; a deleted wiki
    # invalidates its share cleanly instead of resolving to nothing.
    files = _list_repo_cache_files(
        resolved["repo_type"], resolved.get("owner", ""), resolved["repo"], resolved["language"]
    )
    if not files:
        raise HTTPException(status_code=404, detail="The wiki this share pointed to has been deleted")
    return resolved


@app.get("/api/shares")
async def list_shares_endpoint(
    owner: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
):
    """List the user's shareable links (optionally filtered by repo), for a
    'my shares' management view."""
    try:
        return {"shares": list_shares(owner=owner, repo=repo)}
    except Exception as e:
        logger.error(f"list shares failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))


@app.delete("/api/share/{share_id}")
async def delete_share_endpoint(share_id: str):
    """Revoke a shareable link. The wiki itself is untouched."""
    try:
        deleted = delete_share(share_id)
    except Exception as e:
        logger.error(f"delete share {share_id} failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))
    if not deleted:
        raise HTTPException(status_code=404, detail="Share not found")
    return {"deleted": share_id}


@app.get("/api/mindmap/{owner}/{repo}")
async def mindmap_endpoint(
    owner: str,
    repo: str,
    repo_type: str = Query("github"),
    language: str = Query("en"),
):
    """Return the wiki's section/page tree as a nested structure suitable for
    rendering a dedicated mind-map (OpenDeepWiki has a dedicated /mindmap
    worker+route; HackDeepWiki already renders Mermaid inline, so this route
    just exposes the tree the mind-map view consumes, no worker needed).

    Reads from the latest wiki cache release. Returns 404 if no wiki exists."""
    try:
        cached = await read_wiki_cache(owner, repo, repo_type, language)
    except Exception as e:
        logger.error(f"mindmap load failed for {owner}/{repo}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))
    if not cached:
        raise HTTPException(status_code=404, detail="No wiki generated for this repo/language")
    ws = cached.wiki_structure
    # Build {title, children} tree from rootSections -> sections -> pages.
    sections_by_id = {s.id: s for s in (ws.sections or [])}
    pages_by_id = {p.id: p for p in (ws.pages or [])}

    def node_for_page(pid: str) -> dict:
        p = pages_by_id.get(pid)
        if not p:
            return {"id": pid, "title": pid}
        return {"id": p.id, "title": p.title, "related": p.relatedPages or []}

    def node_for_section(sid: str) -> dict:
        s = sections_by_id.get(sid)
        if not s:
            return {"id": sid, "title": sid, "children": []}
        children = []
        for sub in (s.subsections or []):
            children.append(node_for_section(sub))
        for pid in (s.pages or []):
            children.append(node_for_page(pid))
        return {"id": s.id, "title": s.title, "children": children}

    if ws.rootSections:
        tree = [node_for_section(sid) for sid in ws.rootSections]
    else:
        tree = [node_for_page(p.id) for p in (ws.pages or [])]
    return {
        "title": ws.title or f"{owner}/{repo}",
        "description": ws.description or "",
        "tree": tree,
    }


# ---- Fase 3: jobs queue endpoints -----------------------------------------

@app.post("/api/jobs")
async def enqueue_job_endpoint(
    kind: str = Query(..., description="Job kind (e.g. 'wiki_regenerate', 'translate')"),
    owner: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    repo_type: Optional[str] = Query(None),
    payload: dict = Body(default={}),
):
    """Enqueue a background job. Returns the job id; the frontend polls
    /api/jobs/{id} or /api/wiki_cache for the result. The worker dispatches
    by ``kind`` to whatever handler has been registered (register_handler)."""
    from api.jobs.queue import enqueue, _HANDLERS
    if kind not in _HANDLERS:
        # Don't 500 -- a request for a kind nobody registered is a client
        # error (typo, stale UI), and 400 tells the caller exactly that.
        raise HTTPException(status_code=400, detail=f"No handler registered for job kind '{kind}'")
    rk = _repo_key(owner, repo, repo_type)
    job_id = enqueue(kind, rk, payload)
    return {"job_id": job_id, "status": "queued", "kind": kind}


@app.get("/api/jobs")
async def list_jobs_endpoint(
    owner: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    repo_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None, description="Filter: queued/running/done/dead/cancelled"),
    limit: int = Query(50, ge=1, le=200),
):
    """List jobs, optionally scoped to a repo and/or status, newest first.
    Used by a 'job history' / 'is my regenerate done yet' UI."""
    from api.jobs.queue import list_jobs
    rk = _repo_key(owner, repo, repo_type) if (owner or repo or repo_type) else None
    return {"jobs": list_jobs(repo_key_value=rk, status=status, limit=limit)}


@app.get("/api/jobs/{job_id}")
async def get_job_endpoint(job_id: int):
    """Poll one job's status/result. Returns done+result_json when complete,
    'dead'+error when dead-lettered, or running/queued while in flight."""
    from api.jobs.queue import list_jobs
    jobs = list_jobs(limit=500)
    job = next((j for j in jobs if j["id"] == job_id), None)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.delete("/api/jobs/{job_id}")
async def cancel_job_endpoint(job_id: int):
    """Cancel a QUEUED job. Running jobs can't be safely killed mid-handler,
    so only queued ones are cancellable (returns 409 if running/done)."""
    from api.jobs.queue import cancel
    cancelled = cancel(job_id)
    if not cancelled:
        raise HTTPException(status_code=409, detail="Job is not queued (running/done/dead) and cannot be cancelled")
    return {"cancelled": job_id}


# ---- Fase 4/5: accounting + pricing endpoints -----------------------------

@app.get("/api/accounting")
async def accounting_endpoint(since_days: Optional[int] = Query(None, ge=1, le=365)):
    """Aggregate token usage + estimated cost, optionally within the last
    N days. Breaks down by provider. Ollama (local) records $0 -- Ollama
    Cloud is subscription/GPU-time based, not per-token, so per-token cost
    isn't applicable; usage is still recorded for visibility."""
    from api.storage import accounting
    try:
        return accounting.summary(since_days=since_days)
    except Exception as e:
        logger.error(f"accounting summary failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))


@app.get("/api/pricing")
async def list_pricing_endpoint():
    """List the model_pricing table (editable at runtime via PUT). These are
    USD per 1M tokens (input, output); a model not in the table costs $0
    (usage still recorded). Seeded once; user edits persist across restarts."""
    from api.storage import accounting
    try:
        return {"pricing": accounting.list_pricing()}
    except Exception as e:
        logger.error(f"list pricing failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))


@app.put("/api/pricing")
async def set_pricing_endpoint(
    model_pattern: str = Query(..., description="Substring matched against model name, e.g. 'gpt-4o'"),
    input_per_m: Optional[float] = Query(None, description="USD per 1M input tokens (null=unknown->$0)"),
    output_per_m: Optional[float] = Query(None, description="USD per 1M output tokens (null=unknown->$0)"),
):
    """Add or update a pricing row at runtime -- how prices stay current
    without an AppImage rebuild. ``model_pattern`` is substring-matched
    (lowercased), so 'gpt-4o' covers every gpt-4o-* variant."""
    from api.storage import accounting
    try:
        accounting.set_price(model_pattern, input_per_m, output_per_m)
        return {"set": model_pattern, "input_per_m": input_per_m, "output_per_m": output_per_m}
    except Exception as e:
        logger.error(f"set pricing failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))


@app.delete("/api/pricing")
async def delete_pricing_endpoint(model_pattern: str = Query(...)):
    """Remove a pricing row (the model then costs $0 going forward)."""
    from api.storage import accounting
    try:
        deleted = accounting.delete_price(model_pattern)
    except Exception as e:
        logger.error(f"delete pricing failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))
    if not deleted:
        raise HTTPException(status_code=404, detail="Pricing row not found")
    return {"deleted": model_pattern}


@app.get("/api/profiles")
async def list_profiles_endpoint():
    """List configured provider profiles (names + provider + endpoint only --
    never returns API keys, even decrypted). For a 'manage providers' UI."""
    from api.storage import provider_profiles
    try:
        return {"profiles": provider_profiles.list_all()}
    except Exception as e:
        logger.error(f"list profiles failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))


@app.post("/api/profiles")
async def upsert_profile_endpoint(
    name: str = Query(...),
    provider: str = Query(...),
    api_key: Optional[str] = Query(None, description="Plaintext key; encrypted at rest if HACKDEEPWIKI_ENC_KEY is set"),
    api_endpoint: Optional[str] = Query(None),
):
    """Create or update a provider profile. The api_key is encrypted at rest
    via api.security (AES-256-GCM when HACKDEEPWIKI_ENC_KEY is set; plaintext
    passthrough in the zero-config local-first default)."""
    from api.storage import provider_profiles
    try:
        provider_profiles.upsert(name, provider, api_key=api_key, api_endpoint=api_endpoint)
        return {"saved": name, "provider": provider, "encrypted_at_rest": bool(os.environ.get("HACKDEEPWIKI_ENC_KEY"))}
    except Exception as e:
        logger.error(f"upsert profile failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))


@app.delete("/api/profiles/{name}")
async def delete_profile_endpoint(name: str):
    from api.storage import provider_profiles
    try:
        deleted = provider_profiles.delete(name)
    except Exception as e:
        logger.error(f"delete profile failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))
    if not deleted:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"deleted": name}


# ---------------------------------------------------------------------------
# Fase 6 -- Agent skills. Discovery + opt-in injection. A skill is a
# SKILL.md (frontmatter + workflow body) under the bundled skills dir or a
# user skills dir; the chat request's `skills` field selects which to inject
# into the system prompt (see apply_skills_to_system_prompt in chat_common,
# wired in simple_chat.py + websocket_wiki.py). This route lists them for a
# "pick a skill" UI. No write side -- skills are files, not DB rows, so a
# user adds one by dropping <name>/SKILL.md in the user skills dir.
# ---------------------------------------------------------------------------
@app.get("/api/skills")
async def list_skills_endpoint():
    from api.skills import list_skills
    try:
        skills = list_skills()
    except Exception as e:
        logger.error(f"list skills failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))
    # Surface name + description + allowed-tools only; the full workflow body
    # and the on-disk path are prompt/internal details the UI doesn't need
    # (and path leaks filesystem layout).
    return {"skills": [
        {"name": s["name"], "description": s.get("description", ""),
         "allowed_tools": s.get("allowed_tools", "")}
        for s in skills
    ]}


# ---------------------------------------------------------------------------
# Fase 7 -- external MCP servers. The inverse of Fase 1: let HackDeepWiki's
# chat call tools from OTHER MCP servers the user configures (a GitHub MCP,
# a filesystem MCP, ...). Config lives in profile.db (mcp_servers table) so a
# user adds/removes servers at runtime without rebuilding. Registration of a
# server's tools into a chat's tool set is the chat path's job; these routes
# are the CRUD front.
# ---------------------------------------------------------------------------
@app.get("/api/mcp_servers")
async def list_mcp_servers_endpoint():
    from api import mcp_client
    try:
        return {"servers": mcp_client.list_servers()}
    except Exception as e:
        logger.error(f"list mcp servers failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))


@app.post("/api/mcp_servers")
async def add_mcp_server_endpoint(request: Request):
    """Register an external MCP server. Body: {name, transport ('stdio'|'http'),
    config}. For stdio, config = {command, args?, env?}; for http, config =
    {url, headers?}. Config is stored as JSON in profile.db."""
    from api import mcp_client
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=sanitize_error_message(f"invalid JSON body: {e}"))
    name = body.get("name")
    transport = (body.get("transport") or "stdio").lower()
    config = body.get("config") or {}
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if transport not in ("stdio", "http"):
        raise HTTPException(status_code=400, detail="transport must be 'stdio' or 'http'")
    if transport == "stdio" and not config.get("command"):
        raise HTTPException(status_code=400, detail="stdio config requires 'command'")
    if transport == "http" and not config.get("url"):
        raise HTTPException(status_code=400, detail="http config requires 'url'")
    try:
        mcp_client.add_server(name, transport, config)
    except Exception as e:
        logger.error(f"add mcp server failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))
    return {"saved": name, "transport": transport}


@app.delete("/api/mcp_servers/{name}")
async def remove_mcp_server_endpoint(name: str):
    from api import mcp_client
    try:
        deleted = mcp_client.remove_server(name)
    except Exception as e:
        logger.error(f"remove mcp server failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))
    if not deleted:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return {"deleted": name}


# ---------------------------------------------------------------------------
# Fase 6 -- server-side chat history + sessions. The frontend's chat
# sessions currently live only in localStorage; these routes give them a
# durable home in <repo_key>.db so a conversation survives a browser clear
# or a machine switch. owner/repo/type identify which per-repo DB (mirrors
# the fields the chat request already carries); session_id is the
# frontend's existing session id.
# ---------------------------------------------------------------------------
@app.get("/api/chat_history/sessions")
async def list_chat_sessions_endpoint(
    owner: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    type: Optional[str] = Query("github"),
):
    from api.storage import chat_history
    try:
        return {"sessions": chat_history.list_sessions(owner, repo, type)}
    except Exception as e:
        logger.error(f"list chat sessions failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))


@app.post("/api/chat_history/sessions")
async def persist_chat_session_endpoint(request: Request):
    """Bulk-save (or replace) a whole session: {owner, repo, type, session_id,
    title, messages:[{role,content}]}. Atomically replaces the session's
    history so a re-sync from localStorage can't leave a gapped/duplicated
    transcript."""
    from api.storage import chat_history
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=sanitize_error_message(f"invalid JSON body: {e}"))
    sid = body.get("session_id")
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required")
    messages = body.get("messages") or []
    try:
        chat_history.persist_session_json(
            body.get("owner"), body.get("repo"), body.get("type") or "github",
            sid, body.get("title") or "", messages,
        )
    except Exception as e:
        logger.error(f"persist chat session failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))
    return {"saved": sid, "count": len(messages)}


@app.get("/api/chat_history")
async def get_chat_history_endpoint(
    session_id: str = Query(...),
    owner: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    type: Optional[str] = Query("github"),
    limit: int = Query(200, ge=1, le=2000),
):
    from api.storage import chat_history
    try:
        return {"messages": chat_history.get_history(owner, repo, type, session_id, limit)}
    except Exception as e:
        logger.error(f"get chat history failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))


@app.delete("/api/chat_history/sessions/{session_id}")
async def delete_chat_session_endpoint(
    session_id: str,
    owner: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    type: Optional[str] = Query("github"),
):
    from api.storage import chat_history
    try:
        chat_history.delete_session(owner, repo, type, session_id)
    except Exception as e:
        logger.error(f"delete chat session failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))
    return {"deleted": session_id}


# ---------------------------------------------------------------------------
# Fase 7.2 -- migrate the legacy adalflow .pkl embedding index into the
# durable ``embeddings`` SQLite table (a parallel inspectable copy; the hot
# path still reads the .pkl). Read-only on the .pkl, wipes+reinserts the
# table for this repo so it's safe to re-run after a re-embed. Best-effort:
# a missing .pkl returns found=false, never raises.
# ---------------------------------------------------------------------------
@app.post("/api/embeddings/backfill")
async def backfill_embeddings_endpoint(
    owner: Optional[str] = Query(None),
    repo: Optional[str] = Query(None),
    type: Optional[str] = Query("github"),
):
    from api.storage import embeddings_backfill
    from api.storage import embeddings as embeddings_store
    try:
        report = embeddings_backfill.backfill_from_pkl(owner, repo, type)
        report["rows_in_db"] = embeddings_store.count(owner, repo, type)
    except Exception as e:
        logger.error(f"embeddings backfill failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=sanitize_error_message(str(e)))
    return report


@app.get("/")
async def root():
    """Root endpoint to check if the API is running and list available endpoints dynamically."""
    # Collect routes dynamically from the FastAPI app
    endpoints = {}
    for route in app.routes:
        if hasattr(route, "methods") and hasattr(route, "path"):
            # Skip docs and static routes
            if route.path in ["/openapi.json", "/docs", "/redoc", "/favicon.ico"]:
                continue
            # Group endpoints by first path segment
            path_parts = route.path.strip("/").split("/")
            group = path_parts[0].capitalize() if path_parts[0] else "Root"
            method_list = list(route.methods - {"HEAD", "OPTIONS"})
            for method in method_list:
                endpoints.setdefault(group, []).append(f"{method} {route.path}")

    # Optionally, sort endpoints for readability
    for group in endpoints:
        endpoints[group].sort()

    return {
        "message": "Welcome to Streaming API",
        "version": "1.0.0",
        "endpoints": endpoints
    }

# --- Processed Projects Endpoint --- (New Endpoint)
@app.get("/api/processed_projects", response_model=List[ProcessedProjectEntry])
async def get_processed_projects():
    """
    Lists all processed projects found in the wiki cache directory.
    Projects are identified by files named like: hackdeepwiki_cache_{repo_type}_{owner}_{repo}_{language}.json
    (or the pre-rename freedeepwiki_cache_... prefix, for wikis generated
    before the FreeDeepWiki -> HackDeepWiki rename).
    """
    project_entries: List[ProcessedProjectEntry] = []
    # WIKI_CACHE_DIR is already defined globally in the file

    try:
        if os.path.exists(WIKI_CACHE_DIR):
            logger.info(f"Scanning for project cache files in: {WIKI_CACHE_DIR}")
            filenames = await asyncio.to_thread(os.listdir, WIKI_CACHE_DIR)
        else:
            logger.info(f"Cache directory {WIKI_CACHE_DIR} not found.")
            filenames = []

        newest_projects: Dict[tuple, ProcessedProjectEntry] = {}
        for filename in filenames:
            matched_prefix = next(
                (p for p in (WIKI_CACHE_FILE_PREFIX, _LEGACY_WIKI_CACHE_FILE_PREFIX)
                 if filename.startswith(p)),
                None,
            )
            if matched_prefix and filename.endswith(".json"):
                file_path = os.path.join(WIKI_CACHE_DIR, filename)
                try:
                    stats = await asyncio.to_thread(os.stat, file_path) # Use asyncio.to_thread for os.stat
                    cache_name = filename.replace(matched_prefix, "").replace(".json", "")
                    # Strip the release version suffix (_vN) and the variant suffix
                    # (_comprehensive_N / _concise_N) so the remaining
                    # repo_type_owner_repo_language splits cleanly into parts.
                    cache_name = re.sub(r"_v\d+$", "", cache_name)
                    cache_name = re.sub(
                        r"_(?:comprehensive|concise)_\d+$",
                        "",
                        cache_name,
                    )
                    parts = cache_name.split('_')

                    # Expecting repo_type_owner_repo_language
                    # Example: hackdeepwiki_cache_github_kroryan_HackDeepWiki_en.json
                    # parts = [github, kroryan, HackDeepWiki, en]
                    if len(parts) >= 4:
                        repo_type = parts[0]
                        owner = parts[1]
                        language = parts[-1] # language is the last part
                        repo = "_".join(parts[2:-1]) # repo can contain underscores

                        # The cache payload is the authoritative source for
                        # RepoInfo. Besides avoiding filename-decoding loss, it
                        # restores fanwiki repoUrl/start_url so a list entry can
                        # be reopened after a browser reload.
                        cached_repo = None
                        cached_title = None
                        try:
                            with open(file_path, "r", encoding="utf-8") as cache_file:
                                cache_payload = json.load(cache_file)
                            cached_repo = cache_payload.get("repo")
                            cached_title = (cache_payload.get("wiki_structure") or {}).get("title")
                        except (OSError, json.JSONDecodeError):
                            pass
                        if isinstance(cached_repo, dict):
                            repo_type = str(cached_repo.get("type") or repo_type)
                            owner = str(cached_repo.get("owner") or owner)
                            repo = str(cached_repo.get("repo") or repo)
                        start_url = (
                            str(cached_repo.get("repoUrl") or "")
                            if isinstance(cached_repo, dict)
                            else ""
                        )

                        entry = ProcessedProjectEntry(
                            id=filename,
                            owner=owner,
                            repo=repo,
                            name=cached_title or f"{owner}/{repo}",
                            repo_type=repo_type,
                            submittedAt=int(stats.st_mtime * 1000),
                            language=language,
                            status="generated",
                            start_url=start_url or None,
                        )
                        project_key = (repo_type, owner, repo, language)
                        previous = newest_projects.get(project_key)
                        if previous is None or entry.submittedAt > previous.submittedAt:
                            newest_projects[project_key] = entry
                    else:
                        logger.warning(f"Could not parse project details from filename: {filename}")
                except Exception as e:
                    logger.error(f"Error processing file {file_path}: {e}")
                    continue # Skip this file on error

        project_entries = list(newest_projects.values())

        # Imported XML sources are valid durable projects before an LLM wiki
        # exists. Do not duplicate one once at least one generated release for
        # that fanwiki is already listed.
        imported_fanwikis = await asyncio.to_thread(fanwiki_library.list_all)
        imported_by_route = {
            (entry["owner"], entry["repo"]): entry for entry in imported_fanwikis
        }
        # Old generated caches may predate RepoInfo.repoUrl persistence. The
        # durable import manifest can restore it by the same fanwiki route.
        for entry in project_entries:
            if entry.repo_type == "fanwiki" and not entry.start_url:
                imported_match = imported_by_route.get((entry.owner, entry.repo))
                if imported_match:
                    entry.start_url = imported_match["start_url"]

        generated_fanwiki_urls = {
            entry.start_url
            for entry in project_entries
            if entry.repo_type == "fanwiki" and entry.start_url
        }
        generated_fanwiki_routes = {
            (entry.owner, entry.repo)
            for entry in project_entries
            if entry.repo_type == "fanwiki"
        }
        for imported in imported_fanwikis:
            if (
                imported["start_url"] in generated_fanwiki_urls
                or (imported["owner"], imported["repo"]) in generated_fanwiki_routes
            ):
                continue
            project_entries.append(ProcessedProjectEntry(**imported))

        # Mix in imported .zim archives, using the same list shape so the
        # frontend can render them alongside LLM-generated wikis. A .zim has
        # no owner/repo/language in the git-repo sense, so those fields are
        # synthesized: repo_type='zim' is what the frontend uses to route to
        # /zim/{id} instead of /{owner}/{repo}.
        for zim_entry in zim_library.list_all():
            project_entries.append(
                ProcessedProjectEntry(
                    id=zim_entry["id"],
                    owner="zim",
                    repo=zim_entry["id"],
                    name=zim_entry["title"],
                    repo_type="zim",
                    submittedAt=zim_entry["importedAt"],
                    language="",
                )
            )

        # Sort by most recent first
        project_entries.sort(key=lambda p: p.submittedAt, reverse=True)
        logger.info(f"Found {len(project_entries)} processed project entries.")
        return project_entries

    except Exception as e:
        logger.error(f"Error listing processed projects from {WIKI_CACHE_DIR}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list processed projects from server cache.")


# --- ZIM Archive Import & Reader Endpoints ---

class ZimImportRequest(BaseModel):
    path: str = Field(..., description="Absolute filesystem path to a .zim file")


def _register_zim_path(path: str) -> zim_library.ZimEntry:
    """Validate `path` is a real .zim file and register it. Raises
    HTTPException on failure; used by both /import (one explicit path) and
    /rescan (every new file already sitting in the drop folder)."""
    archive = zim_reader.open_archive(path)
    metadata = zim_reader.get_metadata(archive)
    return zim_library.register(
        path=path,
        title=metadata["title"],
        description=metadata["description"],
        article_count=metadata["articleCount"],
    )


@app.post("/api/zim/import")
async def import_zim(request: ZimImportRequest):
    """Register a local .zim file as a browsable/chattable project.

    The file is never copied or uploaded -- only its absolute path is stored.
    Validates the path exists and is a well-formed .zim archive before
    registering it.
    """
    path = request.path.strip()
    if not path:
        raise HTTPException(status_code=400, detail="No path provided")
    if not os.path.isfile(path):
        raise HTTPException(status_code=400, detail=f"File not found: {path}")

    try:
        return _register_zim_path(path)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to open ZIM file {path}: {e}")
        raise HTTPException(status_code=400, detail=f"Not a valid .zim file: {e}")


@app.get("/api/zim/drop_dir")
async def get_zim_drop_dir():
    """Return the folder the user can drop .zim files into directly (useful
    for multi-gigabyte archives where typing/pasting a path is friction) --
    shown in the UI so the user knows where to put files before hitting
    Rescan."""
    return {"path": zim_library.ZIM_DROP_DIR}


@app.post("/api/zim/rescan")
async def rescan_zim_drop_dir():
    """Register every .zim file sitting in the drop folder that isn't
    already in the library. Lets a user drop a huge file into that folder
    with a file manager (or `cp`/`mv`) and pick it up here without typing an
    absolute path."""
    already_registered = zim_library.registered_paths()
    added: list[zim_library.ZimEntry] = []
    errors: list[dict] = []
    for path in zim_library.list_drop_dir_zim_files():
        if path in already_registered:
            continue
        try:
            added.append(_register_zim_path(path))
        except Exception as e:
            logger.error(f"Failed to auto-register dropped ZIM file {path}: {e}")
            errors.append({"path": path, "error": str(e)})
    return {"added": added, "errors": errors}


def _get_zim_entry_or_404(zim_id: str) -> zim_library.ZimEntry:
    entry = zim_library.get(zim_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="ZIM archive not found in library")
    return entry


# Security headers for ZIM-rendered HTML. ZIM content is user-imported and
# served from this backend's origin, so a malicious archive could ship a
# script that, rendered in the browser, talks to the local /api/* endpoints
# with the user's origin. We can't lock everything down (legit ZIMs need their
# own images/CSS/scripts from the archive's /raw/ namespace) but we CAN close
# the backend-call hole: connect-src 'none' blocks XHR/fetch to /api/*, and
# base-uri 'self' prevents <base> hijacking beyond the injected same-origin
# base. Other resource types stay permissive so archive rendering is intact.
_ZIM_HTML_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'self' data:; img-src 'self' data: blob:; "
    "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
    "connect-src 'none'; base-uri 'self'; frame-ancestors 'self'",
}


def _open_zim_or_500(entry: zim_library.ZimEntry):
    try:
        return zim_reader.open_archive(entry["path"])
    except Exception as e:
        logger.error(f"Failed to open registered ZIM file {entry['path']}: {e}")
        raise HTTPException(status_code=500, detail=f"Could not open ZIM file: {e}")


@app.get("/api/zim/{zim_id}")
async def get_zim_metadata(zim_id: str):
    entry = _get_zim_entry_or_404(zim_id)
    archive = _open_zim_or_500(entry)
    metadata = zim_reader.get_metadata(archive)
    return {**entry, **metadata}


@app.get("/api/zim/{zim_id}/search")
async def search_zim(zim_id: str, q: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=100)):
    entry = _get_zim_entry_or_404(zim_id)
    archive = _open_zim_or_500(entry)
    return zim_reader.search_entries(archive, q, limit=limit)


@app.get("/api/zim/{zim_id}/index")
async def get_zim_index(zim_id: str, limit: int = Query(500, ge=1, le=2000)):
    """Browsable list of the archive's pages, shown below the search box
    when the user isn't actively searching (see zim_reader.build_title_index)."""
    entry = _get_zim_entry_or_404(zim_id)
    archive = _open_zim_or_500(entry)
    return zim_reader.build_title_index(archive, limit=limit)


# ZIM content is arbitrary third-party HTML with its own CSS, rendered in an
# iframe sandboxed WITHOUT allow-same-origin (see the reader page for why --
# combining allow-scripts with allow-same-origin is a documented sandbox
# bypass). Scripts are additionally left OUT of the sandbox entirely: real
# .zim archives that ship a client-side app (seen firsthand: a Vue-based
# DevDocs bundle) rely on browser capabilities an opaque-origin sandboxed
# frame doesn't have (localStorage/sessionStorage throw instead of just
# being empty, no IndexedDB, no same-origin fetch), and when that app's
# startup code isn't defensive about it, it crashes mid-render -- which
# looked like "the good static page flashes, then gets replaced by a
# broken 'Loading...' panel that never resolves." Never executing the
# archive's scripts means whatever HTML/CSS it shipped is what actually
# stays on screen, for every .zim, not just the well-behaved ones. It also
# means re-theming it to match the app's dark/light mode isn't possible
# from here (no script access) -- each .zim keeps its own native look,
# wrapped by our own chrome around it rather than reskinned.
def _zim_visible_text(html: str) -> str:
    """Rough visible-text extraction for the JS-shell heuristic: drop
    <script>/<style> blocks, strip all tags, decode a few common entities,
    collapse whitespace. Not a real HTML parser -- intentionally crude, since
    it's only used to decide whether a page is an empty loading shell (see
    _maybe_inject_zim_js_shell_notice), never to display content."""
    s = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    s = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", s)
    s = re.sub(r"(?is)<[^>]+>", " ", s)            # drop all tags
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    s = s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return " ".join(s.split()).strip()


# A ZIM entry whose visible text is essentially just a loading placeholder
# (and nothing else) is a JavaScript-only archive shell: its real content is
# produced by client-side JS that our sandbox="" deliberately never runs (see
# the long comment above _rewrite_zim_html). That leaves the user staring at a
# stuck "Loading..." forever. This regex matches the common placeholder
# wordings across the languages ZIM archives ship (en/es/zh/ja/fr/...).
_ZIM_LOADING_PLACEHOLDER_RE = re.compile(
    r"(?i)(\bloading\b|cargando|carga|chargement|changement|"
    r"加载|载入|読み込み|ロード中|불러오는|로딩|carregando|laden|laddar|"
    r"betölt|yüklen|иниц|загруз|oczekiwan|spuštan|nalagan|betölt|"
    r"\.\s*\.\s*\.\s*$)"
)


def _is_zim_js_shell(html: str) -> bool:
    """True when an HTML entry is a JS-only shell with no static content:
    visible text is short AND matches a loading placeholder. Static archives
    with real article text have long visible text -> never match -> never get
    the notice injected, so this can't regress a working page."""
    text = _zim_visible_text(html)
    if not text or len(text) > 60:
        return False
    return bool(_ZIM_LOADING_PLACEHOLDER_RE.search(text))


_ZIM_JS_SHELL_NOTICE = (
    '<div style="position:fixed;inset:0;display:flex;align-items:center;'
    'justify-content:center;text-align:center;font:14px/1.5 system-ui,sans-serif;'
    'color:#333;background:#fff;padding:24px;box-sizing:border-box;z-index:2147483647;">'
    '<div style="max-width:34em;">'
    '<b>This page is a JavaScript app.</b><br><br>'
    'Its content is generated by scripts inside the archive, which are disabled '
    'in the reader sandbox for safety. The archive did not ship a static '
    'fallback for this entry. Try another article from the list on the left.'
    '</div></div>'
)


def _maybe_inject_zim_js_shell_notice(html: str) -> str:
    """If the entry is a JS-only loading shell, prepend a visible notice so
    the user sees an honest explanation instead of a stuck "Loading...".
    Only ADDS a div -- never strips or alters existing content -- so a
    false-positive (a legitimately short page that happens to say "loading")
    at worst shows an extra notice, it can't break rendering."""
    if not _is_zim_js_shell(html):
        return html
    # Insert right after <body ...> so the notice overlays the shell; fall back
    # to prepending if there's no body tag.
    notice = _ZIM_JS_SHELL_NOTICE
    if re.search(r"(?i)<body[^>]*>", html):
        return re.sub(r"(?i)(<body[^>]*>)", r"\1" + notice, html, count=1)
    return notice + html


def _rewrite_zim_html(zim_id: str, entry_path: str, html_bytes: bytes) -> str:
    """Inject a <base> tag so an entry's own relative links/assets
    ("../../application.css", "../pagination/index") resolve against our raw
    proxy the same way they would against the ZIM's internal namespace.

    Relative URLs resolve against the *directory* of the current URL, so
    base must include entry_path's directory, not just the raw-proxy root --
    otherwise every entry not at the ZIM root (i.e. almost all of them)
    breaks its own asset paths.

    Also detects a JS-only loading shell (archive content that exists only as
    client-side JS, which our sandbox never runs) and overlays an honest
    notice instead of leaving the user on a stuck "Loading..." -- see
    _maybe_inject_zim_js_shell_notice. Static entries with real text are
    never matched, so this is a strict no-op for working pages.
    """
    html = html_bytes.decode("utf-8", errors="replace")
    entry_dir = entry_path.rsplit("/", 1)[0] + "/" if "/" in entry_path else ""
    base_tag = f'<base href="/api/zim/{zim_id}/raw/{entry_dir}">'
    if re.search(r"(?i)<head[^>]*>", html):
        html = re.sub(r"(?i)(<head[^>]*>)", r"\1" + base_tag, html, count=1)
    else:
        html = base_tag + html
    html = _maybe_inject_zim_js_shell_notice(html)
    return html


@app.get("/api/zim/{zim_id}/entry", response_class=HTMLResponse)
async def get_zim_entry(zim_id: str, path: str = Query(...)):
    entry = _get_zim_entry_or_404(zim_id)
    archive = _open_zim_or_500(entry)
    try:
        content, mimetype = zim_reader.get_entry_content(archive, path)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Entry not found: {path}")

    if not mimetype.startswith("text/html"):
        # Non-HTML main entries (rare) -- send back through the raw proxy path
        # instead of trying to render them as a page.
        return Response(content=content, media_type=mimetype)

    html = _rewrite_zim_html(zim_id, path, content)
    return HTMLResponse(content=html, headers=_ZIM_HTML_HEADERS)


@app.get("/api/zim/{zim_id}/raw/{entry_path:path}")
async def get_zim_raw(zim_id: str, entry_path: str):
    """Proxy raw bytes of an entry inside the ZIM namespace (images, CSS, JS,
    linked articles). `entry_path` is resolved entirely inside libzim's own
    archive namespace -- it is never used to build a filesystem path, so it
    cannot escape to the real filesystem regardless of its contents.

    HTML entries reached this way (the user clicked an in-page link to another
    article, which resolves through <base> to this raw route rather than
    /entry) get the same <base> rewrite as /entry, so their own relative
    links/assets keep resolving correctly -- otherwise browsing would break
    one hop after the first click.
    """
    entry = _get_zim_entry_or_404(zim_id)
    archive = _open_zim_or_500(entry)
    try:
        content, mimetype = zim_reader.get_entry_content(archive, entry_path)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Entry not found: {entry_path}")

    if mimetype.startswith("text/html"):
        html = _rewrite_zim_html(zim_id, entry_path, content)
        return HTMLResponse(content=html, headers=_ZIM_HTML_HEADERS)

    return Response(
        content=content,
        media_type=mimetype,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@app.delete("/api/zim/{zim_id}")
async def delete_zim(zim_id: str):
    """Unregister a .zim from the library. Never deletes the underlying file."""
    entry = _get_zim_entry_or_404(zim_id)
    zim_reader.close_archive(entry["path"])
    zim_library.unregister(zim_id)
    return {"message": "ZIM archive removed from library"}
