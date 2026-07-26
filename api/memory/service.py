"""Memory use-case facade backed by a product-owned port."""

from __future__ import annotations

from typing import Any

from .port import MemoryPort, MemoryStatus, RememberedMemory


class MemoryService:
    """Use-case level entry point that depends only on ``MemoryPort``."""

    def __init__(self, port: MemoryPort):
        self._port = port

    def status(self) -> MemoryStatus:
        return self._port.status()

    def recall_context(
        self, workspace: str, question: str, *, limit: int = 6
    ) -> str:
        if not workspace or not question.strip():
            return ""
        return self._port.recall(workspace, question, k=limit)

    def remember_event(
        self,
        workspace: str,
        content: str,
        *,
        title: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> RememberedMemory:
        return self._port.remember(
            workspace,
            content,
            mtype="episodic",
            title=title,
            metadata=metadata,
        )

    def close(self) -> None:
        self._port.close()


_DEFAULT_SERVICE: MemoryService | None = None


def get_memory_service() -> MemoryService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        from .engraphis_adapter import EngraphisMemoryAdapter

        _DEFAULT_SERVICE = MemoryService(EngraphisMemoryAdapter())
    return _DEFAULT_SERVICE
