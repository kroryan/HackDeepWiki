"""Atomic persisted state for idempotent Engraphis ingestion."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any


class IngestionState:
    def __init__(self, path_provider: Callable[[], str]) -> None:
        self._path_provider = path_provider
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return Path(self._path_provider())

    def read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def mark(
        self,
        workspace: str,
        kind: str,
        **fields: Any,
    ) -> None:
        with self._lock:
            state = self.read()
            state.setdefault(workspace, {})[kind] = fields
            path = self.path
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f"{path.suffix}.tmp")
            payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
