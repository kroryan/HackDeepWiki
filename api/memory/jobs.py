"""Durable Engraphis ingestion jobs."""

from __future__ import annotations

import asyncio

from api.jobs.queue import register_handler


async def _record_release(payload: dict) -> dict:
    from api import engraphis_integration

    await asyncio.to_thread(engraphis_integration.record_wiki_release, **payload)
    return {"recorded": True}


async def _record_content(payload: dict) -> dict:
    from api import engraphis_integration
    from api.models import WikiPage, WikiStructureModel

    wiki_structure = WikiStructureModel(**payload.pop("wiki_structure"))
    generated_pages = {
        page_id: WikiPage(**page) for page_id, page in payload.pop("generated_pages").items()
    }
    await asyncio.to_thread(
        engraphis_integration.record_wiki_content,
        wiki_structure=wiki_structure,
        generated_pages=generated_pages,
        **payload,
    )
    return {"recorded": True}


def register_memory_jobs() -> None:
    register_handler("engraphis.release", _record_release)
    register_handler("engraphis.content", _record_content)
