"""Product-owned lifecycle/transport contract for OpenCode."""

from __future__ import annotations

from typing import Any, Protocol

CodeAgentInstance = Any


class CodeAgentPort(Protocol):
    async def ensure_instance(
        self,
        repo_key: str,
        repo_dir: str,
        provider: str,
        model: str,
        api_key: str | None,
        api_endpoint: str | None,
    ) -> CodeAgentInstance: ...

    def get(self, repo_key: str) -> CodeAgentInstance | None: ...

    def instances(self) -> list[CodeAgentInstance]: ...

    async def create_session(
        self,
        instance: CodeAgentInstance,
        title: str,
    ) -> str: ...

    async def session_exists(
        self, instance: CodeAgentInstance, session_id: str
    ) -> bool: ...

    async def abort(
        self,
        instance: CodeAgentInstance,
        session_id: str,
    ) -> None: ...

    async def get_diff(
        self, instance: CodeAgentInstance, session_id: str
    ) -> list[Any]: ...

    async def list_messages(
        self, instance: CodeAgentInstance, session_id: str
    ) -> list[Any]: ...

    def status(self) -> dict[str, Any]: ...

    def shutdown_all_sync(self) -> None: ...
