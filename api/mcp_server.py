"""MCP server for HackDeepWiki (Fase 1).

Exposes the wiki of a generated repository as MCP tools so an external agent
(Claude Desktop, Cursor, any MCP client) can search/read the generated docs
and source. Speaks MCP's JSON-RPC 2.0 protocol directly from stdlib -- NO
dependency on the ``mcp`` pip package, whose pydantic/httpx/SSE stack is a
PyInstaller packaging risk the portability principle forbids. The wire
protocol is small (initialize / tools/list / tools/call) and we implement
exactly the subset we need.

Two transports, both gated by a runtime token (HACKDEEPWIKI_MCP_TOKEN, or
auto-generated on first start and surfaced via get_runtime_token()):

  - stdio:  ``python -m api.mcp_server`` -- line-delimited JSON-RPC on stdin/stdout,
            the transport MCP clients use for local servers.
  - HTTP:   POST /mcp (registered in api.api) -- Content-Type: application/json,
            a single JSON-RPC request -> single JSON-RPC response. Good enough
            for the tools/list + tools/call round-trips a client makes; the
            streaming/SSE variant is out of scope for the local-first app.

A leaked token only exposes READ-ONLY wiki/source access (every tool in
api.mcp_tools is read-only), so the blast radius is the same as the existing
unauthenticated wiki-cache endpoints the app already serves.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import sys
from typing import Any, Optional

from api import mcp_tools
from api.build_info import build_info

logger = logging.getLogger(__name__)

# Protocol version we advertise. MCP clients negotiate via "initialize";
# we accept any version and echo this one back.
_PROTOCOL_VERSION = "2024-11-05"
_SERVER_NAME = "hackdeepwiki"


def _server_build() -> str:
    identity = build_info()
    commit = str(identity.get("commit") or "unknown")[:12]
    return f"{identity.get('channel', 'source')}-{commit}"

# Runtime token: explicit env wins; otherwise a random token is generated
# once per process and can be read by the UI (get_runtime_token) so the user
# can copy it into their MCP client config. Persisting it across restarts is
# intentionally NOT done here -- a per-process token means a restart silently
# rotates it, so a leaked token from a previous session can't be reused.
_RUNTIME_TOKEN: Optional[str] = None


def get_runtime_token() -> str:
    """The token an MCP client must present to call tools. Explicit
    HACKDEEPWIKI_MCP_TOKEN env var wins; otherwise a random per-process token
    is minted on first call and the UI is expected to surface it."""
    global _RUNTIME_TOKEN
    if _RUNTIME_TOKEN is None:
        _RUNTIME_TOKEN = os.environ.get("HACKDEEPWIKI_MCP_TOKEN") or secrets.token_urlsafe(24)
    return _RUNTIME_TOKEN


def _ok(req_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _check_token(auth_header: Optional[str]) -> bool:
    """Constant-time compare of a Bearer token against the runtime token.
    Empty configured token => MCP auth disabled (local-first default for a
    developer's own machine), mirroring how WIKI_AUTH_MODE defaults off."""
    expected = get_runtime_token()
    if not os.environ.get("HACKDEEPWIKI_MCP_TOKEN"):
        # No explicit token configured: auth is opt-in for local use.
        return True
    if not auth_header:
        return False
    presented = auth_header.removeprefix("Bearer ").strip() if auth_header else ""
    return hmac.compare_digest(presented, expected)


def handle_request(req: dict[str, Any], auth_header: Optional[str] = None) -> dict[str, Any]:
    """Process one JSON-RPC request -> one JSON-RPC response. This is the
    shared core both the stdio loop and the HTTP route call."""
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    # initialize is always allowed (it's how a client discovers auth/capabilities).
    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": _SERVER_NAME, "version": _server_build()},
        })

    # Every other method requires a valid token.
    if not _check_token(auth_header):
        return _err(req_id, -32001, "Unauthorized: invalid or missing MCP token")

    if method == "notifications/initialized":
        # Client ack of initialize; no response expected for notifications,
        # but we return a (non-standard) empty result the HTTP caller can ignore.
        return _ok(req_id, {})

    if method == "tools/list":
        return _ok(req_id, {"tools": mcp_tools.list_tools()})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            text = mcp_tools.call_tool(name, arguments)
            return _ok(req_id, {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            })
        except ValueError as e:
            return _err(req_id, -32602, str(e))  # invalid params (unknown tool)
        except Exception as e:  # noqa: BLE001 - tool handler failure must not crash the server
            logger.error(f"MCP tool {name} failed: {e}", exc_info=True)
            return _ok(req_id, {
                "content": [{"type": "text", "text": f"Tool error: {e}"}],
                "isError": True,
            })

    return _err(req_id, -32601, f"Method not found: {method}")


# ---- stdio transport -------------------------------------------------------

def serve_stdio() -> None:
    """Line-delimited JSON-RPC over stdin/stdout. This is the transport MCP
    clients (Claude Desktop, etc.) launch a local server with via the
    ``command``/``args`` config. Each line is one request; each response is
    one line. Notifications (no ``id``) get no response per JSON-RPC."""
    logger.info("HackDeepWiki MCP server (stdio) starting")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            resp = _err(None, -32700, f"Parse error: {e}")
            print(json.dumps(resp), flush=True)
            continue
        resp = handle_request(req, auth_header=None)
        # Notifications (requests without "id") get no response.
        if "id" in req or "id" in resp:
            print(json.dumps(resp), flush=True)
    logger.info("HackDeepWiki MCP server (stdio) stdin closed, exiting")


if __name__ == "__main__":  # pragma: no cover - manual launch path
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    serve_stdio()
