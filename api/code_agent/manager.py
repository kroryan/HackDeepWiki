"""Spawning and supervising `opencode serve` -- one process per repository.

An opencode server binds to the project directory it was launched from, so the
manager runs one instance per repo with ``cwd`` set to the local clone
(``DATABASE/repos/{owner}_{repo}``, the very clone wiki generation made; for
``type='local'`` repos it's the user's own directory, edited in place).

Process-management idioms follow the codebase's existing supervisors:
scripts/launcher.py's Node child (Popen + stdout/stderr drain threads +
terminate->wait->kill) and api/mcp_client.py's stdio client. Instances idle
out after IDLE_SHUTDOWN_SECONDS and everything is torn down at app exit via
both the FastAPI lifespan and atexit (uvicorn runs in a thread in the frozen
build, so atexit is the reliable path there).
"""

import asyncio
import atexit
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import httpx

from api.code_agent.binary import ensure_opencode, installed_opencode_version
from api.code_agent.config import (
    build_child_env,
    new_server_password,
    provider_signature,
    write_opencode_config,
)

logger = logging.getLogger(__name__)

IDLE_SHUTDOWN_SECONDS = 30 * 60
HEALTH_TIMEOUT_SECONDS = 30
# Kept small: only used for error surfacing when the process dies.
_STDERR_TAIL_LINES = 40

SUPPORTED_REPO_TYPES = ("github", "gitlab", "bitbucket", "local")


class CodeAgentError(Exception):
    """Raised for every user-facing failure; ``code`` maps to an HTTP status
    in routes.py and to an i18n string client-side."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def repo_key_for(repo_url: str, repo_type: str) -> tuple[str, str]:
    """Resolve ``(repo_key, repo_dir)`` for a chat request.

    * git-host types -> the clone wiki generation already made
      (api/data_pipeline._local_clone_dir naming: ``{owner}_{repo}``)
    * ``local``      -> the user's own directory, used in place
    """
    if repo_type not in SUPPORTED_REPO_TYPES:
        raise CodeAgentError(
            "unsupported_repo_type",
            f"Code editing is not available for '{repo_type}' sources (no code to edit).",
        )
    if repo_type == "local":
        repo_dir = os.path.abspath(repo_url)
        if not os.path.isdir(repo_dir):
            raise CodeAgentError("repo_not_cloned", f"Local path does not exist: {repo_dir}")
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", repo_dir.strip(os.sep))
        return f"local_{safe}", repo_dir

    from api.data_pipeline import _local_clone_dir
    repo_dir = _local_clone_dir(repo_url, repo_type)
    if not os.path.isdir(repo_dir):
        raise CodeAgentError(
            "repo_not_cloned",
            "The repository has no local clone yet. Generate the wiki first so the repo is cloned locally.",
        )
    return os.path.basename(repo_dir), repo_dir


def repo_head_commit(repo_dir: str) -> Optional[str]:
    """HEAD commit of a clone, or None for non-git dirs / errors."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_dir, capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _maybe_unshallow(repo_dir: str) -> None:
    """Wiki clones are ``--depth=1 --single-branch``; give the agent real git
    history when we can. Best-effort and non-fatal: offline or slow networks
    just leave the clone shallow (editing still works)."""
    try:
        probe = subprocess.run(
            ["git", "rev-parse", "--is-shallow-repository"],
            cwd=repo_dir, capture_output=True, text=True, timeout=10,
        )
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            return
        logger.info("Unshallowing clone at %s for code editing...", repo_dir)
        subprocess.run(
            ["git", "fetch", "--unshallow", "--tags"],
            cwd=repo_dir, capture_output=True, text=True, timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("Unshallow of %s failed (non-fatal): %s", repo_dir, e)


def _find_free_port(start_port: int = 4096) -> int:
    port = start_port
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1


@dataclass
class OpencodeInstance:
    repo_key: str
    repo_dir: str
    port: int
    password: str
    process: subprocess.Popen
    provider_sig: str
    started_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    stderr_tail: deque = field(default_factory=lambda: deque(maxlen=_STDERR_TAIL_LINES))
    # chat-session-id -> opencode session id (several chat sessions may share
    # one server; opencode supports N sessions per project natively)
    sessions: dict = field(default_factory=dict)
    # Set by the events layer (EventFanout) when it attaches. Typed loosely to
    # avoid an import cycle with events.py.
    fanout: object = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def auth(self) -> tuple[str, str]:
        return ("opencode", self.password)

    def alive(self) -> bool:
        return self.process.poll() is None

    def touch(self) -> None:
        self.last_activity = time.time()


class OpencodeManager:
    def __init__(self) -> None:
        self._instances: dict[str, OpencodeInstance] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = threading.Lock()
        self._reaper_task: Optional[asyncio.Task] = None

    def _lock_for(self, repo_key: str) -> asyncio.Lock:
        with self._global_lock:
            if repo_key not in self._locks:
                self._locks[repo_key] = asyncio.Lock()
            return self._locks[repo_key]

    def get(self, repo_key: str) -> Optional[OpencodeInstance]:
        inst = self._instances.get(repo_key)
        return inst if inst and inst.alive() else None

    def instances(self) -> list[OpencodeInstance]:
        return [i for i in self._instances.values() if i.alive()]

    # -- spawn / health ------------------------------------------------------

    async def ensure_instance(self, repo_key: str, repo_dir: str, provider: str,
                              model: str, api_key: Optional[str],
                              api_endpoint: Optional[str],
                              progress_cb=None) -> OpencodeInstance:
        async with self._lock_for(repo_key):
            sig = provider_signature(provider, api_key, api_endpoint, model)
            existing = self._instances.get(repo_key)
            if existing and existing.alive():
                if existing.provider_sig == sig:
                    existing.touch()
                    return existing
                # Credentials/endpoint changed: opencode reads those at
                # startup, so restart the server (sessions persist on disk in
                # DATABASE/opencode/home and survive the restart).
                logger.info("Provider config changed for %s; restarting opencode", repo_key)
                await self._terminate(existing)

            binary = await ensure_opencode(progress_cb)
            config_path, extra_env, _ = write_opencode_config(
                repo_key, provider, model, api_key, api_endpoint
            )
            await asyncio.to_thread(_maybe_unshallow, repo_dir)

            last_error: Optional[str] = None
            for attempt in range(2):  # one retry on a fresh port
                port = _find_free_port()
                password = new_server_password()
                env = build_child_env(config_path, extra_env, password)
                creationflags = 0
                if sys.platform == "win32":
                    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                logger.info("Starting opencode serve for %s on port %d (attempt %d)",
                            repo_key, port, attempt + 1)
                try:
                    proc = subprocess.Popen(
                        [binary, "serve", "--port", str(port), "--hostname", "127.0.0.1"],
                        cwd=repo_dir, env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, creationflags=creationflags,
                    )
                except OSError as e:
                    raise CodeAgentError("opencode_unavailable", f"Could not start opencode: {e}")

                inst = OpencodeInstance(
                    repo_key=repo_key, repo_dir=repo_dir, port=port,
                    password=password, process=proc, provider_sig=sig,
                )
                self._start_drains(inst)
                if await self._wait_healthy(inst):
                    self._instances[repo_key] = inst
                    self._ensure_reaper()
                    return inst
                last_error = "\n".join(inst.stderr_tail) or "no output"
                await self._terminate(inst)

            raise CodeAgentError(
                "opencode_unavailable",
                f"opencode failed to start: {last_error[-1500:] if last_error else 'unknown error'}",
            )

    def _start_drains(self, inst: OpencodeInstance) -> None:
        """Drain the child's pipes so it can't block on a full buffer; keep a
        stderr tail for error surfacing (launcher.py / mcp_client.py idiom)."""
        def drain(stream, is_err: bool) -> None:
            try:
                for line in stream:
                    line = line.rstrip()
                    if is_err:
                        inst.stderr_tail.append(line)
                    logger.debug("[opencode %s%s] %s", inst.repo_key,
                                 " err" if is_err else "", line)
            except Exception:  # noqa: BLE001 - drainer must never crash
                pass
        threading.Thread(target=drain, args=(inst.process.stdout, False), daemon=True).start()
        threading.Thread(target=drain, args=(inst.process.stderr, True), daemon=True).start()

    async def _wait_healthy(self, inst: OpencodeInstance) -> bool:
        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
        async with httpx.AsyncClient(auth=inst.auth, timeout=5) as client:
            while time.monotonic() < deadline:
                if not inst.alive():
                    return False
                try:
                    resp = await client.get(f"{inst.base_url}/global/health")
                    if resp.status_code < 500:
                        return True
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.25)
        return False

    # -- opencode REST wrappers ---------------------------------------------

    async def _request(self, inst: OpencodeInstance, method: str, path: str,
                       json_body: Optional[dict] = None,
                       timeout: float = 30) -> httpx.Response:
        inst.touch()
        async with httpx.AsyncClient(auth=inst.auth, timeout=timeout) as client:
            resp = await client.request(method, f"{inst.base_url}{path}", json=json_body)
            if resp.status_code >= 400:
                raise CodeAgentError(
                    "opencode_error",
                    f"opencode {method} {path} -> {resp.status_code}: {resp.text[:500]}",
                )
            return resp

    async def create_session(self, inst: OpencodeInstance, title: str) -> str:
        resp = await self._request(inst, "POST", "/session", {"title": title})
        session_id = resp.json().get("id")
        if not session_id:
            raise CodeAgentError("opencode_error", "opencode returned no session id")
        return session_id

    async def session_exists(self, inst: OpencodeInstance, session_id: str) -> bool:
        try:
            await self._request(inst, "GET", f"/session/{session_id}")
            return True
        except CodeAgentError:
            return False

    async def prompt_async(self, inst: OpencodeInstance, session_id: str, text: str,
                           model_ref: str) -> None:
        provider_id, _, model_id = model_ref.partition("/")
        body: dict = {
            "model": {"providerID": provider_id, "modelID": model_id},
            "parts": [{"type": "text", "text": text}],
        }
        await self._request(inst, "POST", f"/session/{session_id}/prompt_async", body)

    async def inject_context(self, inst: OpencodeInstance, session_id: str, text: str) -> None:
        """Add context to a session without triggering a reply and WITHOUT
        replacing opencode's own agent system prompt (which the ``system``
        message field would do) -- the documented plugin pattern
        (``noReply: true``)."""
        await self._request(
            inst, "POST", f"/session/{session_id}/message",
            {"noReply": True, "parts": [{"type": "text", "text": text}]},
            timeout=60,
        )

    async def abort(self, inst: OpencodeInstance, session_id: str) -> None:
        await self._request(inst, "POST", f"/session/{session_id}/abort")

    async def get_diff(self, inst: OpencodeInstance, session_id: str) -> list:
        resp = await self._request(inst, "GET", f"/session/{session_id}/diff", timeout=60)
        data = resp.json()
        return data if isinstance(data, list) else data.get("diffs", [])

    async def list_messages(self, inst: OpencodeInstance, session_id: str) -> list:
        resp = await self._request(inst, "GET", f"/session/{session_id}/message", timeout=60)
        data = resp.json()
        return data if isinstance(data, list) else []

    async def respond_permission(self, inst: OpencodeInstance, session_id: str,
                                 permission_id: str, response: str = "always") -> None:
        await self._request(
            inst, "POST", f"/session/{session_id}/permissions/{permission_id}",
            {"response": response},
        )

    # -- lifecycle -----------------------------------------------------------

    async def _terminate(self, inst: OpencodeInstance) -> None:
        fanout = inst.fanout
        if fanout is not None:
            try:
                await fanout.stop()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        await asyncio.to_thread(self._kill_process, inst.process)
        if self._instances.get(inst.repo_key) is inst:
            del self._instances[inst.repo_key]

    @staticmethod
    def _kill_process(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _ensure_reaper(self) -> None:
        if self._reaper_task and not self._reaper_task.done():
            return
        self._reaper_task = asyncio.create_task(self._reap_idle())

    async def _reap_idle(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for inst in list(self._instances.values()):
                if not inst.alive():
                    del self._instances[inst.repo_key]
                elif now - inst.last_activity > IDLE_SHUTDOWN_SECONDS:
                    logger.info("Stopping idle opencode instance for %s", inst.repo_key)
                    await self._terminate(inst)
            if not self._instances:
                self._reaper_task = None
                return

    def shutdown_all_sync(self) -> None:
        """Synchronous teardown for atexit / lifespan shutdown."""
        for inst in list(self._instances.values()):
            self._kill_process(inst.process)
        self._instances.clear()

    def status(self) -> dict:
        from api.code_agent.binary import (
            OPENCODE_VERSION,
            _bundled_bin_dir,
            override_bin_dir,
            resolve_opencode_binary,
        )
        resolved = resolve_opencode_binary()
        source = "missing"
        if resolved:
            if resolved == os.environ.get("HACKDEEPWIKI_OPENCODE_BIN"):
                source = "env"
            elif resolved.startswith(override_bin_dir()):
                source = "override"
            elif resolved.startswith(_bundled_bin_dir()):
                source = "bundled"
            else:
                source = "path"
        return {
            "resolved_path": resolved,
            "source": source,
            "installed_version": installed_opencode_version(resolved) if resolved else None,
            "pinned_version": OPENCODE_VERSION,
            "running_instances": [
                {
                    "repo_key": i.repo_key,
                    "repo_dir": i.repo_dir,
                    "sessions": len(i.sessions),
                    "idle_seconds": int(time.time() - i.last_activity),
                }
                for i in self.instances()
            ],
        }


manager = OpencodeManager()
atexit.register(manager.shutdown_all_sync)
