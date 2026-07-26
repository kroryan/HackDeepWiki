"""Product-owned memory contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class RememberedMemory:
    id: str
    workspace: str
    stored: bool


@dataclass(frozen=True)
class MemoryStatus:
    state: str
    available: bool
    dashboard_url: str | None
    semantic: bool
    detail: str = ""


class MemoryPort(Protocol):
    """Operations used by wiki, chat and Code Agent."""

    def status(self) -> MemoryStatus: ...

    def ensure_workspace(self, workspace: str, description: str = "") -> bool: ...

    def remember(
        self,
        workspace: str,
        content: str,
        *,
        mtype: str = "semantic",
        source: str = "hackdeepwiki",
        title: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> RememberedMemory: ...

    def recall(self, workspace: str, query: str, *, k: int = 6) -> str: ...

    def link(
        self,
        workspace: str,
        a: str,
        b: str,
        *,
        relation: str = "related",
        reason: str = "",
    ) -> bool: ...

    def close(self) -> None: ...
