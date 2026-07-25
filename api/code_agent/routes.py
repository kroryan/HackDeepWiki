"""FastAPI surface for Code Editing mode.

The frontend NEVER talks to an opencode port directly: everything is proxied
here, so the dynamic per-repo ports, basic-auth passwords and event schema
stay a backend concern.

Transports:
  POST /api/code/session      ensure server+session for a repo (idempotent;
                              the client calls it before every send, so a
                              crashed/idle-reaped instance transparently
                              respawns)
  WS   /ws/code/chat          one prompt in -> streamed reply out, using the
                              EXACT wire format of /ws/chat (plain answer
                              text + api/stream_events.py process frames), so
                              Ask.tsx's StreamParser/history code is reused
                              unchanged
  WS   /ws/code/events        normalized activity envelopes for the
                              right-hand panel
  POST /api/code/abort, GET /api/code/diff, GET /api/code/messages
  GET  /api/code/agent/status, POST /api/code/agent/update
"""

import asyncio
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import urlparse

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)

from api.code_agent import events as oc_events
from api.code_agent.binary import (
    OPENCODE_VERSION,
    download_opencode,
    installed_opencode_version,
    resolve_opencode_binary,
)
from api.code_agent.context import build_code_session_context
from api.code_agent.manager import (
    CodeAgentError,
    manager,
    repo_key_for,
    repo_worktree_fingerprint,
)
from api.code_agent.models import (
    CodeAbortRequest,
    CodeAgentUpdateRequest,
    CodeChatRequest,
    CodeSessionRequest,
    CodeSessionResponse,
)
from api.code_agent.config import describe_target, map_provider
from api.chat_common import capture_chat_exchange
from api.security import authorization_is_valid, sanitize_error_message
from api.stream_events import encode_process

logger = logging.getLogger(__name__)

router = APIRouter()

_ERROR_STATUS = {
    "unsupported_repo_type": 400,
    "repo_not_cloned": 409,
    "local_path_forbidden": 403,
    "opencode_unavailable": 503,
    "opencode_error": 502,
}

# Inactivity cutoff for a chat stream: no bus event at all for this long
# means something is wedged (agent turns legitimately go minutes between
# events while a build runs, but the tool part still updates).
_CHAT_EVENT_TIMEOUT = 900

_MUTATION_REQUEST = re.compile(
    r"\b("
    r"create|write|edit|change|modify|implement|fix|refactor|remove|delete|"
    r"add|apply|rename|generate|build|"
    r"crea|crear|escribe|editar|edita|cambia|cambiar|modifica|implementar|"
    r"implementa|arregla|corrige|refactoriza|elimina|añade|agrega|aplica|"
    r"renombra|genera|compila"
    r")\b",
    re.IGNORECASE,
)


def _expects_repository_change(prompt: str) -> bool:
    """Conservative mutation-intent detector for post-turn verification."""
    return bool(_MUTATION_REQUEST.search(prompt or ""))


def _http_error(e: CodeAgentError) -> HTTPException:
    logger.warning("Code agent request failed (%s): %s", e.code, e)
    return HTTPException(
        status_code=_ERROR_STATUS.get(e.code, 500),
        detail={"code": e.code, "message": sanitize_error_message(str(e))},
    )


async def _require_code_authorization(
    authorization: Optional[str] = Header(
        None, alias="X-HackDeepWiki-Authorization"
    ),
) -> bool:
    if not _code_authorization_is_valid(authorization):
        raise HTTPException(status_code=401, detail="Authorization code is invalid")
    return True


_AUTH_DEPENDENCY = [Depends(_require_code_authorization)]


def _code_authorization_is_valid(value: Optional[str]) -> bool:
    """Keep loopback zero-config, but never expose an auto-approved shell.

    A Docker/LAN bind must enable the normal shared auth mode. An operator
    with a separately isolated trusted network can explicitly opt back into
    the old behavior via HACKDEEPWIKI_ALLOW_UNAUTHENTICATED_CODE_AGENT=true.
    """
    from api.config import WIKI_AUTH_MODE

    bind_host = os.environ.get("HACKDEEPWIKI_HOST", "127.0.0.1").strip().lower()
    loopback = bind_host in {"127.0.0.1", "localhost", "::1"}
    explicit_unsafe_opt_in = os.environ.get(
        "HACKDEEPWIKI_ALLOW_UNAUTHENTICATED_CODE_AGENT", ""
    ).lower() in {"1", "true", "yes"}
    if not loopback and not WIKI_AUTH_MODE and not explicit_unsafe_opt_in:
        return False
    return authorization_is_valid(value)


def _websocket_origin_allowed(websocket: WebSocket) -> bool:
    """Allow non-browser clients and browser connections from the serving
    origin (plus explicitly configured origins).

    WebSockets are not covered by the application's CORS middleware. Without
    this check, a malicious web page could drive a localhost CodeAgent when
    auth is disabled in the normal desktop configuration.
    """
    origin = websocket.headers.get("origin")
    if not origin:
        return True

    configured = {
        item.rstrip("/")
        for item in os.environ.get("HACKDEEPWIKI_ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    }
    if origin.rstrip("/") in configured:
        return True

    origin_host = (urlparse(origin).hostname or "").lower()
    request_host = (
        urlparse(f"//{websocket.headers.get('host', '')}").hostname or ""
    ).lower()
    loopback = {"localhost", "127.0.0.1", "::1"}
    return bool(
        origin_host
        and request_host
        and (origin_host == request_host or {origin_host, request_host} <= loopback)
    )


async def _authorize_websocket(websocket: WebSocket) -> bool:
    code = (
        websocket.query_params.get("authorization_code")
        or websocket.headers.get("x-hackdeepwiki-authorization")
    )
    if not _code_authorization_is_valid(code) or not _websocket_origin_allowed(websocket):
        await websocket.close(code=1008)
        return False
    return True


@router.post(
    "/api/code/session",
    response_model=CodeSessionResponse,
    dependencies=_AUTH_DEPENDENCY,
)
async def ensure_code_session(request: CodeSessionRequest) -> CodeSessionResponse:
    try:
        repo_key, repo_dir = repo_key_for(request.repo_url, request.type)
        inst = await manager.ensure_instance(
            repo_key, repo_dir, request.provider, request.model or "",
            request.api_key, request.api_endpoint,
        )

        session_id: Optional[str] = None
        if request.existing_session_id and await manager.session_exists(inst, request.existing_session_id):
            session_id = request.existing_session_id

        version_warning: Optional[str] = None
        if session_id is None:
            system_context, version_warning = await build_code_session_context(
                request.owner, request.repo, request.type, request.language,
                repo_dir, request.wiki_version, bool(request.include_security_context),
            )
            session_id = await manager.create_session(
                inst, request.title or f"HackDeepWiki: {request.owner}/{request.repo}"
            )
            inst.sessions[session_id] = {"context": system_context, "context_sent": False}
        elif session_id not in inst.sessions:
            # Resumed after an instance restart: the context was already
            # injected into the (persisted) opencode session last time.
            inst.sessions[session_id] = {"context": None, "context_sent": True}

        # The chat frame (CodeChatRequest) only carries repo_key/session_id,
        # so the memory scope has to ride on the session itself.
        inst.sessions[session_id].update({
            "owner": request.owner, "repo": request.repo,
            "wiki_version": request.wiki_version,
        })

        binary = resolve_opencode_binary()
        workspace_label = (
            os.path.basename(os.path.normpath(repo_dir)) or "local repository"
            if request.type == "local"
            else f"{request.owner}/{request.repo}".strip("/")
        )
        return CodeSessionResponse(
            session_id=session_id,
            repo_key=repo_key,
            repo_dir=workspace_label,
            is_local_type=request.type == "local",
            opencode_version=installed_opencode_version(binary) if binary else None,
            version_warning=version_warning,
            active_sessions=max(1, len(inst.sessions)),
            model_target=describe_target(
                request.provider, request.model or "", request.api_key, request.api_endpoint),
        )
    except CodeAgentError as e:
        raise _http_error(e)


@router.post("/api/code/abort", dependencies=_AUTH_DEPENDENCY)
async def abort_code_session(request: CodeAbortRequest) -> dict:
    inst = manager.get(request.repo_key)
    if not inst:
        return {"status": "no_instance"}
    try:
        await manager.abort(inst, request.session_id)
        return {"status": "aborted"}
    except CodeAgentError as e:
        raise _http_error(e)


@router.get("/api/code/diff", dependencies=_AUTH_DEPENDENCY)
async def code_session_diff(repo_key: str, session_id: str) -> list:
    inst = manager.get(repo_key)
    if not inst:
        return []
    try:
        return await manager.get_diff(inst, session_id)
    except CodeAgentError as e:
        raise _http_error(e)


@router.get("/api/code/messages", dependencies=_AUTH_DEPENDENCY)
async def code_session_messages(repo_key: str, session_id: str) -> list:
    """Backfill: the session's message list straight from opencode, so a
    dropped chat WebSocket can recover assistant text it missed."""
    inst = manager.get(repo_key)
    if not inst:
        return []
    try:
        return await manager.list_messages(inst, session_id)
    except CodeAgentError as e:
        raise _http_error(e)


@router.get("/api/code/agent/status", dependencies=_AUTH_DEPENDENCY)
async def code_agent_status() -> dict:
    return manager.status()


@router.post("/api/code/agent/update", dependencies=_AUTH_DEPENDENCY)
async def code_agent_update(request: CodeAgentUpdateRequest) -> dict:
    """Download a release into DATABASE/opencode/bin (the writable override
    that beats the read-only bundled copy). Synchronous: the archives are
    ~30 MB.

    Deliberately does NOT stop running instances -- the first version of this
    endpoint did, and clicking Update while the agent was mid-answer killed
    the session ("the opencode process exited"). The new binary lands in the
    override dir; running agents keep the old version until they restart
    naturally (idle reap / app restart / provider change), and every NEW
    instance picks up the update immediately. ``pending_restart`` tells the
    UI how many running agents are still on the old version."""
    if request.version not in ("", "pinned", OPENCODE_VERSION):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "unsupported_version",
                "message": f"Only the verified pinned release {OPENCODE_VERSION} is supported.",
            },
        )
    version = OPENCODE_VERSION
    try:
        path = await asyncio.to_thread(download_opencode, version)
        return {
            "status": "ok",
            "version": installed_opencode_version(path),
            "pending_restart": len(manager.instances()),
        }
    except RuntimeError as e:
        logger.error("OpenCode update failed: %s", e, exc_info=True)
        raise HTTPException(
            status_code=502,
            detail={
                "code": "download_failed",
                "message": sanitize_error_message(str(e)),
            },
        )


# ---------------------------------------------------------------------------
# WS /ws/code/chat -- the left-chat transport
# ---------------------------------------------------------------------------

async def handle_code_chat_websocket(websocket: WebSocket) -> None:
    if not await _authorize_websocket(websocket):
        return
    await websocket.accept()
    queue = None
    fanout = None
    inst = None
    request: Optional[CodeChatRequest] = None
    try:
        raw = await websocket.receive_json()
        request = CodeChatRequest(**raw)

        inst = manager.get(request.repo_key)
        if not inst:
            await websocket.send_text(
                "\n\n**Code agent error:** the agent for this repository is not "
                "running. Send the message again to restart it."
            )
            return
        session_meta = inst.sessions.setdefault(
            request.session_id, {"context": None, "context_sent": True}
        )

        _, _, model_ref = map_provider(
            request.provider, request.model or "", request.api_key, request.api_endpoint
        )

        # Subscribe BEFORE prompting so no early event is lost.
        fanout = oc_events.get_fanout(inst, manager)
        queue = fanout.subscribe(request.session_id)

        # Process frames use the {label, query} payload shape the chat UI's
        # process panel already renders (Ask.tsx).
        if session_meta.get("context") and not session_meta.get("context_sent"):
            await manager.inject_context(inst, request.session_id, session_meta["context"])
            session_meta["context_sent"] = True
            await websocket.send_text(encode_process("tool", {
                "label": "Code agent", "query": "wiki context injected into the session",
            }))

        expects_change = _expects_repository_change(request.content)
        before_fingerprint = (
            await asyncio.to_thread(repo_worktree_fingerprint, inst.repo_dir)
            if expects_change else None
        )
        await manager.prompt_async(inst, request.session_id, request.content, model_ref)

        # -- stream loop ----------------------------------------------------
        message_roles: dict[str, str] = {}
        part_offsets: dict[str, int] = {}
        part_types: dict[str, str] = {}
        tool_states: dict[str, str] = {}
        saw_activity = False
        sent_any_text = False
        last_assistant_message = ""
        answer_parts: list[str] = []
        saw_mutation_tool = False
        completed_normally = False

        while True:
            try:
                evt = await asyncio.wait_for(queue.get(), timeout=_CHAT_EVENT_TIMEOUT)
            except asyncio.TimeoutError:
                await websocket.send_text(
                    "\n\n**Code agent error:** no activity from the agent for "
                    f"{_CHAT_EVENT_TIMEOUT // 60} minutes; giving up on this reply."
                )
                break

            evt_type = evt.get("type", "")
            props = evt.get("properties") or {}

            if evt_type in ("instance.exited", "_fanout.stopped"):
                tail = "\n".join(props.get("stderr_tail") or [])
                await websocket.send_text(
                    "\n\n**Code agent error:** the opencode process exited."
                    + (
                        f"\n```\n{sanitize_error_message(tail)}\n```"
                        if tail else ""
                    )
                )
                break

            if evt_type.startswith("session.error"):
                err = props.get("error") or props
                msg = err.get("data", {}).get("message") or err.get("message") or str(err)
                await websocket.send_text(
                    f"\n\n**Code agent error:** {sanitize_error_message(str(msg))}"
                )
                break

            if evt_type == "message.updated":
                info = props.get("info") or {}
                if info.get("id"):
                    message_roles[info["id"]] = info.get("role", "")
                saw_activity = True
                # Deliberately do NOT close when an assistant message
                # completes: an agentic turn produces SEVERAL assistant
                # messages ("Let me look at the code..." -> tools -> the
                # real answer). Closing on the first one ate the final
                # answer (real-world bug: the user only ever saw the
                # preamble). The turn ends at session idle, below.
                continue

            if evt_type == "session.status":
                # opencode >=1.18: {status: {type: busy|retry|idle, ...}}.
                # Retries (provider unreachable) previously showed as an
                # eternal "Thinking..." -- forward them so the user sees WHY
                # nothing is streaming. Idle ends the turn, but only after
                # some activity: a stale idle right after subscribing must
                # not close an answer that hasn't started.
                status = props.get("status") or {}
                stype = status.get("type")
                if stype == "busy":
                    saw_activity = True
                elif stype == "retry":
                    saw_activity = True
                    msg = str(status.get("message") or "connection problem")
                    await websocket.send_text(encode_process("tool", {
                        "label": "Code agent",
                        "query": f"retry {status.get('attempt', '?')}: {msg}"[:400],
                    }))
                elif stype == "idle" and saw_activity:
                    completed_normally = True
                    break
                continue

            if "idle" in evt_type and saw_activity:
                completed_normally = True
                break

            # opencode >=1.18.5 streams text token-by-token as
            # message.part.delta ({messageID, partID, field, delta});
            # part.updated then carries the cumulative snapshot. Handle both
            # through the same per-part offset so nothing duplicates
            # whichever mix a given opencode version emits.
            if evt_type == "message.part.delta":
                if props.get("field") != "text":
                    continue
                part_id = props.get("partID") or ""
                delta = props.get("delta") or ""
                message_id = props.get("messageID") or ""
                if not delta or message_roles.get(message_id, "") != "assistant":
                    continue
                saw_activity = True
                if part_types.get(part_id) == "reasoning":
                    await websocket.send_text(encode_process("thinking", {"text": delta}))
                else:
                    # Separate consecutive assistant messages ("I'll check
                    # the code..." / final answer) visually.
                    if sent_any_text and message_id != last_assistant_message and last_assistant_message:
                        await websocket.send_text("\n\n")
                    last_assistant_message = message_id
                    sent_any_text = True
                    await websocket.send_text(delta)
                    answer_parts.append(delta)
                part_offsets[part_id] = part_offsets.get(part_id, 0) + len(delta)
                continue

            if evt_type == "message.part.updated":
                part = props.get("part") or {}
                part_type = part.get("type")
                part_id = part.get("id") or ""
                if part_type:
                    part_types[part_id] = part_type
                role = message_roles.get(part.get("messageID") or "", "")

                if part_type == "text" and role == "assistant":
                    # Cumulative snapshot; emit only the tail the deltas
                    # haven't already delivered.
                    text = part.get("text") or ""
                    sent = part_offsets.get(part_id, 0)
                    if len(text) > sent:
                        message_id = part.get("messageID") or ""
                        if sent_any_text and message_id != last_assistant_message and last_assistant_message:
                            await websocket.send_text("\n\n")
                        last_assistant_message = message_id
                        sent_any_text = True
                        await websocket.send_text(text[sent:])
                        answer_parts.append(text[sent:])
                        part_offsets[part_id] = len(text)
                elif part_type == "reasoning":
                    text = part.get("text") or ""
                    sent = part_offsets.get(part_id, 0)
                    if len(text) > sent:
                        await websocket.send_text(
                            encode_process("thinking", {"text": text[sent:]}))
                        part_offsets[part_id] = len(text)
                elif part_type == "tool":
                    state = part.get("state") or {}
                    status = state.get("status") or "running"
                    # One frame per state transition, not per token of output.
                    if tool_states.get(part_id) != status:
                        tool_states[part_id] = status
                        if (
                            status == "completed"
                            and str(part.get("tool") or "").lower()
                            in {"edit", "write", "patch", "apply_patch", "bash", "shell"}
                        ):
                            saw_mutation_tool = True
                        title = (state.get("title") or "")[:300]
                        await websocket.send_text(encode_process("tool", {
                            "label": part.get("tool") or "tool",
                            "query": f"{title} [{status}]" if title else status,
                        }))

        if completed_normally and expects_change:
            after_fingerprint = await asyncio.to_thread(
                repo_worktree_fingerprint, inst.repo_dir
            )
            if before_fingerprint is not None and after_fingerprint is not None:
                verified_change = before_fingerprint != after_fingerprint
            else:
                try:
                    verified_change = (
                        bool(await manager.get_diff(inst, request.session_id))
                        or saw_mutation_tool
                    )
                except CodeAgentError:
                    verified_change = saw_mutation_tool
            if not verified_change:
                warning = (
                    "\n\n⚠️ **Verification warning:** the request asked for a "
                    "repository change, but OpenCode finished without any verified "
                    "HEAD/working-tree or session diff. The model may not support "
                    "reliable tool calling; no file change should be assumed."
                )
                await websocket.send_text(warning)
                answer_parts.append(warning)

        # Same durable memory the repository chat writes to, so a coding turn
        # and a wiki question accumulate into one story per wiki release.
        await asyncio.to_thread(
            capture_chat_exchange,
            owner=session_meta.get("owner"), repo=session_meta.get("repo"),
            wiki_version=session_meta.get("wiki_version"),
            question=request.content, answer="".join(answer_parts),
            source="code_agent",
        )
    except WebSocketDisconnect:
        # User closed/cancelled mid-answer: stop the agent's current work.
        if inst and request:
            try:
                await manager.abort(inst, request.session_id)
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        logger.error("code chat websocket failed: %s", e, exc_info=True)
        try:
            await websocket.send_text(
                f"\n\n**Code agent error:** {sanitize_error_message(str(e))}"
            )
        except Exception:  # noqa: BLE001
            pass
    finally:
        if fanout and queue:
            fanout.unsubscribe(queue)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# WS /ws/code/events -- the right-panel activity feed
# ---------------------------------------------------------------------------

async def handle_code_events_websocket(websocket: WebSocket) -> None:
    if not await _authorize_websocket(websocket):
        return
    await websocket.accept()
    queue = None
    fanout = None
    try:
        raw = await websocket.receive_json()
        repo_key = raw.get("repo_key") or ""
        session_id = raw.get("session_id") or None

        inst = manager.get(repo_key)
        if not inst:
            await websocket.send_json({"t": "status", "state": "no_instance"})
            return

        fanout = oc_events.get_fanout(inst, manager)
        queue = fanout.subscribe(session_id)
        await websocket.send_json({
            "t": "status", "state": "connected",
            "workspace": os.path.basename(os.path.normpath(inst.repo_dir)),
            "active_sessions": max(1, len(inst.sessions)),
        })

        # message.part.delta arrives per TOKEN -- hundreds per second during
        # a streaming answer. Forwarding each one as its own Debug envelope
        # saturated the browser's main thread (the tab couldn't even answer
        # WS pings, so connections died and reconnect-looped). Coalesce
        # deltas per part and flush a compact summary at most ~2x/second.
        delta_acc: dict[str, dict] = {}
        _FLUSH_SECONDS = 0.5

        async def drain_deltas() -> None:
            for pid, acc in list(delta_acc.items()):
                tail = acc["tail"].replace("\n", "⏎ ")
                await websocket.send_json({
                    "t": "debug",
                    "type": "message.part.delta",
                    "session": acc["session"],
                    "summary": f"…{pid[-8:]}: +{acc['count']} deltas ({acc['chars']} chars) …{tail}",
                })
            delta_acc.clear()

        while True:
            evt = await queue.get()
            evt_type = evt.get("type")

            if evt_type == "message.part.delta":
                props = evt.get("properties") or {}
                if props.get("field") == "text":
                    pid = str(props.get("partID") or "?")
                    delta = str(props.get("delta") or "")
                    acc = delta_acc.setdefault(pid, {
                        "count": 0, "chars": 0, "tail": "",
                        "session": oc_events.extract_session_id(evt),
                        "since": time.monotonic(),
                    })
                    acc["count"] += 1
                    acc["chars"] += len(delta)
                    acc["tail"] = (acc["tail"] + delta)[-160:]
                    if time.monotonic() - acc["since"] >= _FLUSH_SECONDS:
                        await drain_deltas()
                continue

            # Any non-delta event flushes pending delta summaries first so
            # the Debug feed keeps its ordering.
            await drain_deltas()
            envelope = oc_events.normalize_for_panel(evt)
            if envelope is not None:
                await websocket.send_json(envelope)
            # Unfiltered firehose for the Debug tab: EVERY bus event, compact
            # and truncated (see debug_view). Localhost-only traffic, and the
            # frontend keeps a bounded buffer.
            await websocket.send_json(oc_events.debug_view(evt))
            if evt_type in ("instance.exited", "_fanout.stopped"):
                break
    except WebSocketDisconnect:
        logger.info("code events ws: client disconnected")
    except Exception as e:  # noqa: BLE001
        logger.error("code events websocket failed: %s", e, exc_info=True)
    finally:
        if fanout and queue:
            fanout.unsubscribe(queue)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


@router.websocket("/ws/code/chat")
async def code_chat_ws(websocket: WebSocket) -> None:
    await handle_code_chat_websocket(websocket)


@router.websocket("/ws/code/events")
async def code_events_ws(websocket: WebSocket) -> None:
    await handle_code_events_websocket(websocket)
