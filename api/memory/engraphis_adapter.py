"""Vendor adapter implementing the product-owned :class:`MemoryPort`."""

from __future__ import annotations

from typing import Any

from .port import MemoryStatus, RememberedMemory


class EngraphisMemoryAdapter:
    """Keep Engraphis imports and response-shape translation at one boundary."""

    @staticmethod
    def _backend():
        from api import engraphis_integration

        return engraphis_integration

    def status(self) -> MemoryStatus:
        raw = self._backend().status()
        available = bool(raw.get("available"))
        detail = str(raw.get("error") or raw.get("reason") or "")
        embedder = raw.get("embedder") or {}
        return MemoryStatus(
            state=(
                "healthy"
                if available and not detail
                else ("degraded" if available else "failed")
            ),
            available=available,
            dashboard_url=raw.get("dashboard_url"),
            semantic=bool(embedder.get("semantic")),
            detail=detail,
        )

    def ensure_workspace(self, workspace: str, description: str = "") -> bool:
        return bool(self._backend().ensure_workspace(workspace, description))

    def remember(
        self,
        workspace: str,
        content: str,
        *,
        mtype: str = "semantic",
        source: str = "hackdeepwiki",
        title: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> RememberedMemory:
        result = self._backend().remember_detailed(
            workspace,
            content,
            mtype=mtype,
            source=source,
            title=title,
            metadata=metadata,
        )
        return RememberedMemory(
            id=str(result.get("id") or ""),
            workspace=workspace,
            stored=not bool(result.get("error")) and bool(result.get("id")),
        )

    def recall(self, workspace: str, query: str, *, k: int = 6) -> str:
        return self._backend().recall(workspace, query, k=k)

    def link(
        self,
        workspace: str,
        a: str,
        b: str,
        *,
        relation: str = "related",
        reason: str = "",
    ) -> bool:
        return bool(
            self._backend().link(
                workspace,
                a,
                b,
                relation=relation,
                reason=reason,
            )
        )

    def close(self) -> None:
        self._backend().shutdown()
