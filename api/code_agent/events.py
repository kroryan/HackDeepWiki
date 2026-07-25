"""Consuming opencode's SSE event bus and fanning it out to subscribers.

One ``EventFanout`` per opencode instance: a single task holds the streamed
``GET /event`` connection (reconnecting with backoff while the process lives)
and pushes every parsed event into per-subscriber asyncio queues. Subscribers
filter by opencode session id -- the chat WebSocket wants one session's text
deltas, the right-hand activity panel wants that session's tool/file/shell
events -- while the fan-out stays a dumb pipe.

Safety valve for full-auto mode: the generated config allows edit/bash/
webfetch outright, but a new opencode tool category could still default to
"ask". Any permission request seen on the bus is auto-approved here (and
logged), so a session can never silently wedge waiting for an answer nobody
will see.
"""

import asyncio
import contextlib
import json
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# A subscriber's queue is bounded so one stuck WebSocket can't buffer the
# world; on overflow we drop the OLDEST event (panel views are resyncable via
# GET /api/code/diff and the message backfill endpoint).
_QUEUE_MAX = 2000


def extract_session_id(evt: dict) -> Optional[str]:
    """opencode event payload shapes vary by type; the session id may live at
    several depths. Normalize once, here."""
    props = evt.get("properties") or {}
    for holder in (props, props.get("info") or {}, props.get("part") or {},
                   props.get("permission") or {}):
        sid = holder.get("sessionID") or holder.get("sessionId")
        if sid:
            return sid
    return None


class EventFanout:
    def __init__(self, instance, manager) -> None:
        self._instance = instance
        self._manager = manager
        self._subscribers: list[tuple[Optional[str], asyncio.Queue]] = []
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._broadcast({"type": "_fanout.stopped"})

    def subscribe(self, session_id: Optional[str]) -> asyncio.Queue:
        """Queue receiving events for ``session_id`` (None = all sessions).
        Events without any session id (server-level) go to every subscriber."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._subscribers.append((session_id, queue))
        self.start()
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers = [(s, q) for s, q in self._subscribers if q is not queue]

    def _broadcast(self, evt: dict) -> None:
        evt_session = extract_session_id(evt)
        for wanted, queue in self._subscribers:
            if wanted is not None and evt_session is not None and evt_session != wanted:
                continue
            try:
                queue.put_nowait(evt)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(evt)

    async def _consume(self) -> None:
        inst = self._instance
        backoff = 0.5
        while inst.alive():
            try:
                async with httpx.AsyncClient(auth=inst.auth, timeout=httpx.Timeout(30, read=None)) as client:
                    async with client.stream("GET", f"{inst.base_url}/event") as resp:
                        backoff = 0.5
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            try:
                                evt = json.loads(line[5:].strip())
                            except json.JSONDecodeError:
                                continue
                            # A streaming generation makes no REST calls, so
                            # without this the instance looks idle mid-work
                            # and the 30-min reaper could kill it mid-answer.
                            inst.touch()
                            await self._handle_permissions(evt)
                            self._broadcast(evt)
            except (httpx.HTTPError, OSError) as e:
                if not inst.alive():
                    break
                logger.warning("opencode event stream for %s dropped (%s); reconnecting in %.1fs",
                               inst.repo_key, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8)
        logger.info("opencode instance %s exited; event fan-out closing", inst.repo_key)
        self._broadcast({
            "type": "instance.exited",
            "properties": {"stderr_tail": list(inst.stderr_tail)[-10:]},
        })

    async def _handle_permissions(self, evt: dict) -> None:
        evt_type = evt.get("type", "")
        if "permission" not in evt_type:
            return
        props = evt.get("properties") or {}
        perm = props.get("permission") or props.get("info") or props
        perm_id = perm.get("id") or perm.get("permissionID")
        session_id = extract_session_id(evt)
        # Only *requests* need answering; replied/updated notifications don't
        # carry an answerable id or will simply 404 -- which we swallow.
        if not perm_id or not session_id:
            return
        logger.info("Auto-approving opencode permission %s (%s) for full-auto mode",
                    perm_id, perm.get("title") or perm.get("type") or "?")
        try:
            await self._manager.respond_permission(self._instance, session_id, perm_id)
        except Exception as e:  # noqa: BLE001 - already-answered permissions 404; never fatal
            logger.debug("Permission auto-approve for %s ignored: %s", perm_id, e)


def get_fanout(instance, manager) -> EventFanout:
    """The fan-out attached to an instance, creating it on first use."""
    if instance.fanout is None:
        instance.fanout = EventFanout(instance, manager)
    return instance.fanout


# ---------------------------------------------------------------------------
# Event normalization for the activity panel (WS /ws/code/events).
# Raw bus events are verbose and shape-shifting across opencode versions;
# the panel gets a small, stable envelope instead. Returns None for events
# the panel doesn't care about.
# ---------------------------------------------------------------------------

def debug_view(evt: dict) -> dict:
    """A compact, bounded rendering of ANY bus event for the panel's Debug
    tab -- the unfiltered firehose (thinking, text deltas, every tool state,
    session bookkeeping) so the user can tell "slow because it's working"
    from "slow because it's stuck". Payloads are truncated hard: text parts
    arrive as cumulative snapshots on every token, so forwarding them whole
    would ship the full answer N times over."""
    evt_type = evt.get("type", "")
    props = evt.get("properties") or {}
    part = props.get("part") or {}
    info = props.get("info") or {}

    summary = ""
    if part:
        part_type = part.get("type") or "?"
        if part_type in ("text", "reasoning"):
            text = part.get("text") or ""
            tail = text[-160:].replace("\n", "⏎ ")
            summary = f"{part_type} ({len(text)} chars) …{tail}" if text else part_type
        elif part_type == "tool":
            state = part.get("state") or {}
            bits = [part.get("tool") or "tool", state.get("status") or ""]
            title = state.get("title") or ""
            if title:
                bits.append(title[:120])
            command = (state.get("input") or {}).get("command")
            if command:
                bits.append(f"$ {str(command)[:160]}")
            summary = " | ".join(b for b in bits if b)
        else:
            summary = json.dumps(part, ensure_ascii=False)[:300]
    elif info:
        bits = [info.get("role") or "", (info.get("time") or {}) and
                ("completed" if (info.get("time") or {}).get("completed") else "in-progress")]
        summary = " | ".join(b for b in bits if b) or json.dumps(info, ensure_ascii=False)[:300]
    elif props:
        summary = json.dumps(props, ensure_ascii=False)[:300]

    return {
        "t": "debug",
        "type": evt_type,
        "session": extract_session_id(evt),
        "summary": summary,
    }


def normalize_for_panel(evt: dict) -> Optional[dict]:
    evt_type = evt.get("type", "")
    props = evt.get("properties") or {}

    if evt_type == "instance.exited":
        return {"t": "status", "state": "crashed",
                "stderr_tail": props.get("stderr_tail") or []}

    if evt_type.startswith("session.error"):
        err = props.get("error") or props
        return {"t": "error", "message": str(err.get("data", {}).get("message")
                                             or err.get("message") or err)[:2000]}

    if evt_type.startswith("session") and ("idle" in evt_type or props.get("status") == "idle"):
        return {"t": "status", "state": "idle"}

    if evt_type.startswith("file.edited") or evt_type.startswith("file.watcher"):
        return {"t": "diff_hint", "file": props.get("file") or props.get("path")}

    if evt_type == "message.part.updated":
        part = props.get("part") or {}
        part_type = part.get("type")
        if part_type == "tool":
            state = part.get("state") or {}
            tool_name = part.get("tool") or part.get("name") or "tool"
            entry = {
                "t": "tool",
                "part_id": part.get("id"),
                "name": tool_name,
                "status": state.get("status") or "running",
                "title": (state.get("title") or "")[:300],
            }
            state_input = state.get("input") or {}
            if tool_name == "bash" or state_input.get("command"):
                entry["t"] = "shell"
                entry["command"] = str(state_input.get("command") or "")[:2000]
                output = state.get("output") or state.get("metadata", {}).get("output") or ""
                entry["output"] = str(output)[-8000:]
            if tool_name in ("edit", "write", "patch") and state_input.get("filePath"):
                entry["t"] = "file_edited"
                entry["file"] = state_input.get("filePath")
            return entry
        if part_type == "reasoning":
            return None  # thinking goes to the left chat, not the panel
        return None

    return None
