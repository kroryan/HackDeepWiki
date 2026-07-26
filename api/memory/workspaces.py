"""Deterministic Engraphis workspace identities."""

from __future__ import annotations

import re


def clean_component(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", str(value or "").strip())
    return cleaned.strip("._-") or "unknown"


def workspace_for_version(
    owner: str,
    repo: str,
    wiki_version: int | None,
) -> str:
    base = f"{clean_component(owner)}_{clean_component(repo)}"[:80]
    version = int(wiki_version) if wiki_version is not None else 0
    return f"{base}_v{version}"


def workspace_for_evolution(owner: str, repo: str) -> str:
    base = f"{clean_component(owner)}_{clean_component(repo)}"[:80]
    return f"{base}_evolution"
