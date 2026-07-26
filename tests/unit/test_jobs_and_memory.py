from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from api.jobs import queue
from api.memory.engraphis_adapter import EngraphisMemoryAdapter
from api.memory.jobs import _record_content, _record_release, register_memory_jobs
from api.memory.workspaces import (
    clean_component,
    workspace_for_evolution,
    workspace_for_version,
)
from api.models import RepoInfo, WikiCacheData, WikiCacheRequest, WikiPage, WikiStructureModel
from api.services import wiki_cache
from api.storage import connect


def _page() -> WikiPage:
    return WikiPage(
        id="intro",
        title="Introduction",
        content="Generated content",
        filePaths=["README.md"],
        importance="high",
        relatedPages=[],
    )


def _request() -> WikiCacheRequest:
    page = _page()
    return WikiCacheRequest(
        repo=RepoInfo(owner="acme", repo="widgets", type="github"),
        language="en",
        wiki_structure=WikiStructureModel(
            id="wiki",
            title="Widgets",
            description="Widget docs",
            pages=[page],
        ),
        generated_pages={page.id: page},
        provider="openai",
        model="test-model",
        page_count=1,
    )


def test_job_retry_is_not_immediately_claimable(monkeypatch, tmp_path):
    database = tmp_path / "profile.db"
    monkeypatch.setattr(queue, "profile_db_path", lambda: str(database))
    monkeypatch.setattr(queue, "BACKOFF_BASE", 60.0)

    job_id = queue.enqueue("test", "repo")
    job = queue.claim()
    assert job and job["id"] == job_id
    queue._fail(job_id, "temporary", attempts=1)
    assert queue.claim() is None

    with connect(str(database)) as conn:
        row = conn.execute(
            "SELECT status, available_at > datetime('now') FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    assert tuple(row) == ("queued", 1)


@pytest.mark.asyncio
async def test_memory_job_handlers_rebuild_models(monkeypatch):
    captured: list[tuple[str, dict]] = []

    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    # Some constrained CI sandboxes cannot shut down Python's default thread
    # executor. The handler contract is tested synchronously here; production
    # still delegates the blocking Engraphis call with asyncio.to_thread.
    monkeypatch.setattr("api.memory.jobs.asyncio.to_thread", run_inline)

    def record_release(**payload):
        captured.append(("release", payload))

    def record_content(**payload):
        captured.append(("content", payload))

    monkeypatch.setattr(
        "api.engraphis_integration.record_wiki_release",
        record_release,
    )
    monkeypatch.setattr(
        "api.engraphis_integration.record_wiki_content",
        record_content,
    )
    assert await _record_release({"owner": "acme", "repo": "widgets"}) == {
        "recorded": True
    }
    page = _page()
    payload = {
        "owner": "acme",
        "repo": "widgets",
        "version": 2,
        "wiki_structure": WikiStructureModel(
            id="wiki",
            title="Widgets",
            description="Docs",
            pages=[page],
        ).model_dump(),
        "generated_pages": {"intro": page.model_dump()},
    }
    assert await _record_content(payload) == {"recorded": True}
    assert captured[0][0] == "release"
    assert isinstance(captured[1][1]["wiki_structure"], WikiStructureModel)
    assert isinstance(captured[1][1]["generated_pages"]["intro"], WikiPage)


def test_memory_job_registration(monkeypatch):
    registered: dict[str, object] = {}
    monkeypatch.setattr(
        "api.memory.jobs.register_handler",
        lambda kind, handler: registered.__setitem__(kind, handler),
    )
    register_memory_jobs()
    assert set(registered) == {"engraphis.release", "engraphis.content"}


def test_wiki_ingestion_is_enqueued_durably(monkeypatch):
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        queue,
        "enqueue",
        lambda kind, key, payload: calls.append((kind, key, payload)) or len(calls),
    )
    request = _request()
    payload = WikiCacheData(
        wiki_structure=request.wiki_structure,
        generated_pages=request.generated_pages,
        repo=request.repo,
        provider=request.provider,
        model=request.model,
        version=3,
        repo_commit="abc123",
    )
    wiki_cache._enqueue_memory(request, payload, "/clone", 2, "old123")
    assert [call[0] for call in calls] == [
        "engraphis.release",
        "engraphis.content",
    ]
    assert calls[0][2]["previous_commit"] == "old123"
    assert calls[1][2]["generated_pages"]["intro"]["content"] == "Generated content"


def test_atomic_wiki_write_leaves_no_temporary_file(tmp_path):
    target = tmp_path / "nested" / "wiki.json"
    wiki_cache._atomic_write_json(str(target), {"value": "complete"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"value": "complete"}
    assert list(target.parent.glob(".wiki-cache-*.tmp")) == []


def test_workspace_names_are_scoped_and_safe():
    assert clean_component("../../owner name") == "owner_name"
    assert workspace_for_version("owner", "repo", 4) == "owner_repo_v4"
    assert workspace_for_evolution("owner", "repo") == "owner_repo_evolution"


def test_engraphis_adapter_translates_vendor_shapes(monkeypatch):
    backend = SimpleNamespace(
        status=lambda: {
            "available": True,
            "dashboard_url": "http://127.0.0.1:1234",
            "embedder": {"semantic": True},
        },
        ensure_workspace=lambda workspace, description: True,
        remember_detailed=lambda *args, **kwargs: {
            "id": "memory-1",
            "op": "add",
            "error": "",
        },
        recall=lambda workspace, query, k: "context",
        link=lambda *args, **kwargs: True,
        shutdown=lambda: None,
    )
    adapter = EngraphisMemoryAdapter()
    monkeypatch.setattr(adapter, "_backend", lambda: backend)
    assert adapter.status().state == "healthy"
    remembered = adapter.remember("workspace", "fact")
    assert remembered.id == "memory-1"
    assert remembered.stored is True
    assert adapter.recall("workspace", "question") == "context"
