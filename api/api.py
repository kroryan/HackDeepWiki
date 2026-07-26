import logging

from fastapi import WebSocket

from api.app_factory import create_app
from api.lifespan import application_lifespan
from api.logging_config import setup_logging
from api.routers.auth import router as auth_router
from api.routers.conversations import router as conversations_router
from api.routers.exports import router as exports_router
from api.routers.fanwiki import router as fanwiki_router
from api.routers.health import router as health_router
from api.routers.integrations import router as integrations_router
from api.routers.jobs import router as jobs_router
from api.routers.models import router as models_router
from api.routers.operations import router as operations_router
from api.routers.projects import router as projects_router
from api.routers.repositories import router as repositories_router
from api.routers.scans import router as scans_router
from api.routers.sources import router as sources_router
from api.routers.storage import router as storage_router
from api.routers.wiki import router as wiki_router
from api.routers.wiki_discovery import router as wiki_discovery_router
from api.routers.zim import router as zim_router
from api.security import authorize_websocket
from api.services.security_cache import read_vuln_cache, read_web_vuln_cache
from api.services.wiki_cache import read_wiki_cache
from api.simple_chat import chat_completions_stream
from api.websocket_wiki import handle_websocket_chat

setup_logging()
logger = logging.getLogger(__name__)
app = create_app(lifespan=application_lifespan)
app.include_router(auth_router)
app.include_router(conversations_router)
app.include_router(exports_router)
app.include_router(fanwiki_router)
app.include_router(health_router)
app.include_router(integrations_router)
app.include_router(jobs_router)
app.include_router(models_router)
app.include_router(operations_router)
app.include_router(projects_router)
app.include_router(repositories_router)
app.include_router(scans_router)
app.include_router(sources_router)
app.include_router(storage_router)
app.include_router(wiki_router)
app.include_router(wiki_discovery_router)
app.include_router(zim_router)
app.add_api_route("/chat/completions/stream", chat_completions_stream, methods=["POST"])


@app.websocket("/ws/chat")
async def _ws_chat(websocket: WebSocket):
    if not await authorize_websocket(websocket):
        return
    await handle_websocket_chat(websocket)

try:
    from api.code_agent.routes import router as code_agent_router

    app.include_router(code_agent_router)
except Exception as exc:  # noqa: BLE001 - optional embedded adapter
    logger.warning("Code Editing mode unavailable: %s", exc)

__all__ = [
    "app",
    "read_vuln_cache",
    "read_web_vuln_cache",
    "read_wiki_cache",
]
