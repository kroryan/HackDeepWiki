"""MCP, Engraphis and local skill integration boundaries."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api import mcp_client
from api.mcp_server import get_runtime_token, handle_request
from api.network_policy import validate_outbound_url
from api.security import sanitize_error_message
from api.settings import DeploymentProfile, get_settings
from api.skills import list_skills

logger = logging.getLogger(__name__)
router = APIRouter(tags=["integrations"])


class McpServerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    transport: Literal["stdio", "http"] = "stdio"
    config: dict[str, Any] = Field(default_factory=dict)


@router.post("/mcp")
async def mcp_endpoint(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception as exc:
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32700,
                    "message": (
                        f"Parse error: {sanitize_error_message(str(exc))}"
                    ),
                },
            },
        )
    authorization = request.headers.get("authorization")
    response = handle_request(payload, auth_header=authorization)
    status = (
        401
        if response.get("error", {}).get("code") == -32001
        else 200
    )
    return JSONResponse(status_code=status, content=response)


@router.get("/mcp/token")
async def mcp_token() -> dict[str, Any]:
    if os.environ.get("HACKDEEPWIKI_MCP_TOKEN"):
        return {
            "configured": True,
            "token": None,
            "hint": (
                "Configured through HACKDEEPWIKI_MCP_TOKEN; "
                "the value is never exposed by the API."
            ),
        }
    return {
        "configured": False,
        "token": get_runtime_token(),
        "hint": (
            "Per-process token; rotate on restart. "
            "Set HACKDEEPWIKI_MCP_TOKEN to pin it."
        ),
    }


@router.get("/api/engraphis/status")
async def engraphis_status(
    owner: str | None = Query(None),
    repo: str | None = Query(None),
    wiki_version: int | None = Query(None),
    view: Literal["version", "evolution"] = Query("version"),
    panel: str = Query("", max_length=80),
) -> dict[str, Any]:
    from api import engraphis_integration

    def resolve() -> dict[str, Any]:
        info = engraphis_integration.status()
        if not info.get("available") or not info.get("dashboard_url"):
            return info
        result = dict(info)
        if not (owner and repo):
            result["url"] = result.get("dashboard_url")
            return result
        if view == "evolution":
            workspace = engraphis_integration.workspace_for_evolution(
                owner, repo
            )
            description = f"Evolution of the {owner}/{repo} wiki across releases."
        else:
            workspace = engraphis_integration.workspace_for_version(
                owner,
                repo,
                wiki_version,
            )
            description = (
                f"Memory of {owner}/{repo} wiki release v{wiki_version or 0} "
                "(shared by chat and code editor)."
            )
        engraphis_integration.ensure_workspace(workspace, description)
        result["workspace"] = workspace
        result["url"] = engraphis_integration.dashboard_url_for(
            workspace,
            panel,
        )
        return result

    return await asyncio.to_thread(resolve)


@router.get("/api/skills")
async def skills() -> dict[str, Any]:
    try:
        discovered = list_skills()
    except Exception as exc:
        logger.exception("Skill discovery failed")
        raise HTTPException(
            status_code=500,
            detail=sanitize_error_message(str(exc)),
        ) from exc
    return {
        "skills": [
            {
                "name": skill["name"],
                "description": skill.get("description", ""),
                "allowed_tools": skill.get("allowed_tools", ""),
            }
            for skill in discovered
        ]
    }


@router.get("/api/mcp_servers")
async def mcp_servers() -> dict[str, Any]:
    try:
        return {"servers": mcp_client.list_servers()}
    except Exception as exc:
        logger.exception("Could not list MCP servers")
        raise HTTPException(
            status_code=500,
            detail=sanitize_error_message(str(exc)),
        ) from exc


@router.post("/api/mcp_servers")
async def add_mcp_server(request: McpServerRequest) -> dict[str, str]:
    config = request.config
    if request.transport == "stdio":
        if get_settings().deployment_profile is not DeploymentProfile.DESKTOP:
            raise HTTPException(
                status_code=403,
                detail="stdio MCP servers are supported only in desktop mode",
            )
        if not isinstance(config.get("command"), str) or not config["command"]:
            raise HTTPException(400, "stdio config requires command")
    else:
        url = config.get("url")
        if not isinstance(url, str) or not url:
            raise HTTPException(400, "http config requires url")
        try:
            validate_outbound_url(url)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    try:
        mcp_client.add_server(
            request.name,
            request.transport,
            config,
        )
    except Exception as exc:
        logger.exception("Could not save MCP server")
        raise HTTPException(
            status_code=500,
            detail=sanitize_error_message(str(exc)),
        ) from exc
    return {"saved": request.name, "transport": request.transport}


@router.delete("/api/mcp_servers/{name}")
async def remove_mcp_server(name: str) -> dict[str, str]:
    try:
        deleted = mcp_client.remove_server(name)
    except Exception as exc:
        logger.exception("Could not remove MCP server")
        raise HTTPException(
            status_code=500,
            detail=sanitize_error_message(str(exc)),
        ) from exc
    if not deleted:
        raise HTTPException(404, "MCP server not found")
    return {"deleted": name}
