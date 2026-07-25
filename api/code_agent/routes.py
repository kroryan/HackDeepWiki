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
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from api.code_agent import events as oc_events
from api.code_agent.binary import (
    OPENCODE_VERSION,
    download_opencode,
    installed_opencode_version,
    resolve_opencode_binary,
)
from api.code_agent.context import build_code_session_context
from api.code_agent.manager import CodeAgentError, manager, repo_key_for
from api.code_agent.models import (
    CodeAbortRequest,
    CodeAgentUpdateRequest,
    CodeChatRequest,
    CodeSessionRequest,
    CodeSessionResponse,
)
from api.code_agent.config import describe_target, map_provider
from api.stream_events import encode_process

logger = logging.getLogger(__name__)

router = APIRouter()

_ERROR_STATUS = {
    "unsupported_repo_type": 400,
    "repo_not_cloned": 409,
    "opencode_unavailable": 503,
    "opencode_error": 502,
}

# Inactivity cutoff for a chat stream: no bus event at all for this long
# means something is wedged (agent turns legitimately go minutes between
# events while a build runs, but the tool part still updates).
_CHAT_EVENT_TIMEOUT = 900


def _http_error(e: CodeAgentError) -> HTTPException:
    return HTTPException(
        status_code=_ERROR_STATUS.get(e.code, 500),
        detail={"code": e.code, "message": str(e)},
    )


@router.post("/api/code/session", response_model=CodeSessionResponse)
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

        binary = resolve_opencode_binary()
        return CodeSessionResponse(
            session_id=session_id,
            repo_key=repo_key,
            repo_dir=repo_dir,
            is_local_type=request.type == "local",
            opencode_version=installed_opencode_version(binary) if binary else None,
            version_warning=version_warning,
            active_sessions=max(1, len(inst.sessions)),
            model_target=describe_target(
                request.provider, request.model or "", request.api_key, request.api_endpoint),
        )
    except CodeAgentError as e:
        raise _http_error(e)


@router.post("/api/code/abort")
async def abort_code_session(request: CodeAbortRequest) -> dict:
    inst = manager.get(request.repo_key)
    if not inst:
        return {"status": "no_instance"}
    try:
        await manager.abort(inst, request.session_id)
        return {"status": "aborted"}
    except CodeAgentError as e:
        raise _http_error(e)


@router.get("/api/code/diff")
async def code_session_diff(repo_key: str, session_id: str) -> list:
    inst = manager.get(repo_key)
    if not inst:
        return []
    try:
        return await manager.get_diff(inst, session_id)
    except CodeAgentError as e:
        raise _http_error(e)


@router.get("/api/code/messages")
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


@router.get("/api/code/agent/status")
async def code_agent_status() -> dict:
    return manager.status()


@router.post("/api/code/agent/update")
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
    version = OPENCODE_VERSION if request.version in ("", "pinned") else request.version
    try:
        path = await asyncio.to_thread(download_opencode, version)
        return {
            "status": "ok",
            "path": path,
            "version": installed_opencode_version(path),
            "pending_restart": len(manager.instances()),
        }
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail={"code": "download_failed", "message": str(e)})


# ---------------------------------------------------------------------------
# WS /ws/code/chat -- the left-chat transport
# ---------------------------------------------------------------------------

async def handle_code_chat_websocket(websocket: WebSocket) -> None:
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

        await manager.prompt_async(inst, request.session_id, request.content, model_ref)

        # -- stream loop ----------------------------------------------------
        message_roles: dict[str, str] = {}
        part_offsets: dict[str, int] = {}
        tool_states: dict[str, str] = {}
        saw_activity = False

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
                    + (f"\n```\n{tail}\n```" if tail else "")
                )
                break

            if evt_type.startswith("session.error"):
                err = props.get("error") or props
                msg = err.get("data", {}).get("message") or err.get("message") or str(err)
                await websocket.send_text(f"\n\n**Code agent error:** {str(msg)[:2000]}")
                break

            if evt_type == "message.updated":
                info = props.get("info") or {}
                if info.get("id"):
                    message_roles[info["id"]] = info.get("role", "")
                saw_activity = True
                # Assistant turn finished -> close (Ask.tsx commits the
                # accumulated text to history on WS close, exactly like the
                # normal chat).
                if info.get("role") == "assistant" and (info.get("time") or {}).get("completed"):
                    break
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
                    break
                continue

            if "idle" in evt_type and saw_activity:
                break

            if evt_type == "message.part.updated":
                part = props.get("part") or {}
                part_type = part.get("type")
                part_id = part.get("id") or ""
                role = message_roles.get(part.get("messageID") or "", "")

                if part_type == "text" and role == "assistant":
                    # opencode sends cumulative text; emit only the new tail.
                    text = part.get("text") or ""
                    sent = part_offsets.get(part_id, 0)
                    if len(text) > sent:
                        await websocket.send_text(text[sent:])
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
                        title = (state.get("title") or "")[:300]
                        await websocket.send_text(encode_process("tool", {
                            "label": part.get("tool") or "tool",
                            "query": f"{title} [{status}]" if title else status,
                        }))
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
            await websocket.send_text(f"\n\n**Code agent error:** {e}")
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
            "repo_dir": inst.repo_dir,
            "active_sessions": max(1, len(inst.sessions)),
        })

        while True:
            evt = await queue.get()
            envelope = oc_events.normalize_for_panel(evt)
            if envelope is not None:
                await websocket.send_json(envelope)
            # Unfiltered firehose for the Debug tab: EVERY bus event, compact
            # and truncated (see debug_view). Localhost-only traffic, and the
            # frontend keeps a bounded buffer.
            await websocket.send_json(oc_events.debug_view(evt))
            if evt.get("type") in ("instance.exited", "_fanout.stopped"):
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
