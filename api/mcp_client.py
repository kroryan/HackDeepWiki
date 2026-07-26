"""MCP client (Fase 7) -- consume tools from EXTERNAL MCP servers.

Fase 1 EXPOSES HackDeepWiki's wiki as MCP tools; this is the inverse: let
HackDeepWiki's chat/agent call tools from OTHER MCP servers the user
configures (a GitHub MCP server, a filesystem MCP, a database MCP, ...). Same
stdlib JSON-RPC as mcp_server -- NO ``mcp`` pip dependency (portable).

A server is configured by command+args (stdio transport, the way local MCP
servers are launched) or by URL (HTTP transport). The client connects, lists
the server's tools, and exposes them to the agent loop as additional
prefix->handler entries so an external tool is callable exactly like the
built-in SEARCH_WIKI/READ_FILE textual tools (one-line convention), OR as
native tool schemas when the provider supports native tool-calling.

Config lives in profile.db (mcp_servers table) so a user adds/removes servers
at runtime without rebuilding. This module is the client; registration of a
server's tools into a chat's tool set is wired by the chat path (Fase 7
finishing step).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from typing import Any, Optional

from api.build_info import build_info
from api.network_policy import validate_outbound_url
from api.security import decrypt_secret, encrypt_secret
from api.storage import connect, profile_db_path

logger = logging.getLogger(__name__)

# Per-request timeout for a single stdio JSON-RPC round-trip (initialize,
# tools/list, tools/call). A server that hangs on readline would otherwise
# block the chat's setup forever; this turns it into a clean failure that
# list_server_tools / call_server_tool already swallow into "no tools / error
# string" so the chat proceeds with just the built-in tools.
STDIO_TIMEOUT = int(os.environ.get("HACKDEEPWIKI_MCP_STDIO_TIMEOUT", "30"))


def _client_build() -> str:
    identity = build_info()
    return (
        f"{identity.get('channel', 'source')}-"
        f"{str(identity.get('commit', 'unknown'))[:12]}"
    )


def _ensure_servers(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mcp_servers (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL UNIQUE,
            transport     TEXT NOT NULL DEFAULT 'stdio',
            -- stdio: JSON {"command":"...","args":[...],"env":{...}}
            -- http:  JSON {"url":"http://...","headers":{...}}
            config_json   TEXT NOT NULL,
            enabled       INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def add_server(name: str, transport: str, config: dict, enabled: bool = True) -> int:
    """Register an external MCP server. ``transport`` is 'stdio' or 'http'.
    ``config`` is the transport-specific connection config (see table comment)."""
    with connect(profile_db_path()) as conn:
        _ensure_servers(conn)
        cur = conn.execute(
            "INSERT INTO mcp_servers (name, transport, config_json, enabled) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET transport=excluded.transport, "
            "config_json=excluded.config_json, enabled=excluded.enabled",
            (
                name,
                transport,
                encrypt_secret(json.dumps(config)),
                1 if enabled else 0,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def _redact_config(value: Any) -> Any:
    """Return connection metadata without retrievable credentials."""
    secret_names = {
        "authorization",
        "api_key",
        "apikey",
        "password",
        "secret",
        "token",
    }
    if isinstance(value, dict):
        return {
            key: (
                "[configured]"
                if key.lower().replace("-", "_") in secret_names
                else _redact_config(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_config(item) for item in value]
    return value


def list_servers(*, include_secrets: bool = False) -> list[dict]:
    with connect(profile_db_path()) as conn:
        _ensure_servers(conn)
        rows = conn.execute(
            "SELECT id, name, transport, config_json, enabled, created_at FROM mcp_servers ORDER BY name"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            stored = d.pop("config_json")
            config = json.loads(decrypt_secret(stored))
            d["config"] = config if include_secrets else _redact_config(config)
        except Exception:  # noqa: BLE001
            d["config"] = {}
        out.append(d)
    return out


def remove_server(name: str) -> bool:
    with connect(profile_db_path()) as conn:
        _ensure_servers(conn)
        cur = conn.execute("DELETE FROM mcp_servers WHERE name = ?", (name,))
        conn.commit()
        return cur.rowcount > 0


# ---- stdio transport -------------------------------------------------------

class StdioMcpClient:
    """A minimal MCP stdio client: launches a server process, speaks
    line-delimited JSON-RPC 2.0, and exposes initialize/tools/list/tools/call.
    One process per server; the process is killed on close()."""

    def __init__(self, command: str, args: list[str], env: Optional[dict] = None):
        self._command = command
        self._args = args or []
        self._env = {**os.environ, **(env or {})}
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._next_id = 1
        self._stderr_drainer: Optional[asyncio.Task] = None

    def _resolve_command(self) -> str:
        """Resolve the server command to an executable path. A bare name
        (e.g. ``npx``, ``python``) is looked up on PATH via ``shutil.which`` --
        important inside the AppImage where the bundled runtime's bin dir may
        not be on PATH for the launched server, and a missing command would
        otherwise surface as an opaque OSError. An absolute path is used as-is
        (it may legitimately point outside PATH)."""
        if not self._command:
            raise RuntimeError("MCP server config has no command set")
        if os.path.isabs(self._command):
            if not os.path.exists(self._command):
                raise RuntimeError(f"MCP command {self._command!r} does not exist")
            return self._command
        resolved = shutil.which(self._command)
        if not resolved:
            raise RuntimeError(
                f"MCP command {self._command!r} not found in PATH "
                f"(PATH has {len(os.environ.get('PATH', '').split(os.pathsep))} entries). "
                "Install it or set an absolute path in the server config."
            )
        return resolved

    async def _drain_stderr(self) -> None:
        """Continuously read and discard the server's stderr so a verbose
        server (many MCP servers log every request on stderr) can't fill the
        OS pipe buffer and block. Logged at DEBUG so it's invisible unless a
        dev is diagnosing a server."""
        proc = self._proc
        if not proc or not proc.stderr:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                logger.debug(
                    "MCP stderr [%s]: %s",
                    self._command,
                    line.decode(errors="replace").rstrip(),
                )
        except Exception as e:  # noqa: BLE001 - drainer must never disrupt the chat
            logger.debug("MCP stderr drainer stopped: %s", e)

    async def connect(self) -> None:
        cmd = self._resolve_command()
        self._proc = await asyncio.create_subprocess_exec(
            cmd, *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
        )
        # Drain stderr in the background so a chatty server can't deadlock on
        # a full stderr pipe. Cancelled in close().
        self._stderr_drainer = asyncio.create_task(self._drain_stderr())
        # initialize handshake
        await self._call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "hackdeepwiki",
                "version": _client_build(),
            },
        })
        # acknowledge (notification, no response expected)
        await self._notify("notifications/initialized", {})

    async def list_tools(self) -> list[dict]:
        resp = await self._call("tools/list", {})
        return resp.get("tools", []) if isinstance(resp, dict) else []

    async def call_tool(self, name: str, arguments: dict) -> str:
        resp = await self._call("tools/call", {"name": name, "arguments": arguments})
        # MCP returns content as a list of {type, text} blocks; flatten to text.
        content = resp.get("content", []) if isinstance(resp, dict) else []
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(t for t in texts if t)

    async def close(self) -> None:
        if self._stderr_drainer and not self._stderr_drainer.done():
            self._stderr_drainer.cancel()
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    self._proc.kill()
                except Exception:
                    pass

    async def _call(self, method: str, params: dict) -> Any:
        if not self._proc or not self._proc.stdin or not self._proc.stdout:
            raise RuntimeError("MCP stdio client not connected")
        req_id = self._next_id
        self._next_id += 1
        req = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        self._proc.stdin.write((json.dumps(req) + "\n").encode())
        await self._proc.stdin.drain()
        # read lines until we get the response with our id (skip notifications)
        while True:
            try:
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=STDIO_TIMEOUT
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"MCP server timed out ({STDIO_TIMEOUT}s) responding to {method}"
                )
            if not line:
                raise RuntimeError(f"MCP server closed stdin before responding to {method}")
            try:
                msg = json.loads(line.decode())
            except json.JSONDecodeError:
                continue
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"MCP error on {method}: {msg['error']}")
                return msg.get("result")

    async def _notify(self, method: str, params: dict) -> None:
        if not self._proc or not self._proc.stdin:
            return
        req = {"jsonrpc": "2.0", "method": method, "params": params}
        self._proc.stdin.write((json.dumps(req) + "\n").encode())
        await self._proc.stdin.drain()


# ---- HTTP transport --------------------------------------------------------

async def _http_call(url: str, method: str, params: dict,
                     headers: Optional[dict] = None) -> Any:
    """Single-request JSON-RPC over HTTP POST. Good enough for tools/list +
    tools/call (the streaming/SSE variant is out of scope for local-first)."""
    import urllib.request

    validate_outbound_url(url)
    req_body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    # run blocking urllib in a thread so the async loop isn't stalled
    loop = asyncio.get_running_loop()

    def _do():
        r = urllib.request.Request(url, data=req_body, headers=hdrs, method="POST")
        with urllib.request.urlopen(r, timeout=30) as resp:
            return json.loads(resp.read().decode())

    msg = await loop.run_in_executor(None, _do)
    if "error" in msg:
        raise RuntimeError(f"MCP HTTP error on {method}: {msg['error']}")
    return msg.get("result")


async def list_server_tools(server: dict) -> list[dict]:
    """Connect to a configured server (stdio or http) and return its tools.
    Used at chat-setup time to register the server's tools into the agent's
    tool set. Returns [] on failure (an unreachable external server must not
    break the chat -- the built-in tools still work)."""
    cfg = server.get("config", {})
    transport = server.get("transport", "stdio")
    try:
        if transport == "http":
            res = await _http_call(cfg["url"], "tools/list", {}, cfg.get("headers"))
            return res.get("tools", []) if isinstance(res, dict) else []
        # stdio
        client = StdioMcpClient(cfg["command"], cfg.get("args", []), cfg.get("env"))
        try:
            await client.connect()
            return await client.list_tools()
        finally:
            await client.close()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"MCP server {server.get('name')} tools/list failed: {e}")
        return []


# How long chat setup will wait for ONE server's tools/list before giving up
# on it and proceeding with just the other tools. Kept modest so a
# misconfigured/unreachable server can't stall the start of every chat.
LIST_TOOLS_TIMEOUT = int(os.environ.get("HACKDEEPWIKI_MCP_LIST_TIMEOUT", "10"))


async def list_server_tools_timed(server: dict) -> list[dict]:
    """list_server_tools with an overall timeout. A server that takes longer
    than LIST_TOOLS_TIMEOUT to enumerate its tools is skipped (returns []),
    so one slow external server can't delay the chat for everyone."""
    try:
        return await asyncio.wait_for(
            list_server_tools(server), timeout=LIST_TOOLS_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.warning(
            "MCP server %s tools/list timed out after %ss; skipping",
            server.get("name"), LIST_TOOLS_TIMEOUT,
        )
        return []
    except Exception as e:  # noqa: BLE001
        logger.warning(f"MCP server {server.get('name')} tools/list failed: {e}")
        return []


def _flatten_tool_content(resp: Any) -> str:
    """MCP tools/call returns content as a list of {type, text} blocks; flatten
    text blocks to a single string. Falls back to a json dump for non-text
    content so the model still sees something usable."""
    content = resp.get("content", []) if isinstance(resp, dict) else []
    texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    joined = "\n".join(t for t in texts if t)
    if joined:
        return joined
    if content:
        return json.dumps(content, ensure_ascii=False)
    return "(empty result)"


async def call_server_tool(server: dict, tool_name: str, arguments: dict) -> str:
    """Call one tool on a configured external server and return its text
    result. NEVER raises -- on any failure it returns a short error string so
    the agent loop can feed the error back to the model instead of crashing
    the chat. A fresh connection is opened per call (matches list_server_tools'
    connect/list/close lifecycle); MCP servers are expected to handle this."""
    cfg = server.get("config", {})
    transport = server.get("transport", "stdio")
    try:
        if transport == "http":
            res = await _http_call(
                cfg["url"], "tools/call",
                {"name": tool_name, "arguments": arguments or {}},
                cfg.get("headers"),
            )
            return _flatten_tool_content(res)
        # stdio
        client = StdioMcpClient(cfg["command"], cfg.get("args", []), cfg.get("env"))
        try:
            await client.connect()
            result = await client.call_tool(tool_name, arguments or {})
            return result if result else "(empty result)"
        finally:
            await client.close()
    except Exception as e:  # noqa: BLE001 - never break the chat on an external tool
        logger.warning(f"MCP call_tool {server.get('name')}/{tool_name} failed: {e}")
        return f"MCP tool {tool_name!r} on server {server.get('name')!r} failed: {e}"
