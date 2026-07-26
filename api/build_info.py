"""Runtime build identity (commit/run/channel, never a fictitious SemVer)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api.component_manifest import public_component_inventory


def _identity_path() -> Path:
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS) / "build" / "build-info.json"
    return Path(__file__).resolve().parents[1] / "build" / "build-info.json"


def _source_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def build_info() -> dict[str, Any]:
    path = _identity_path()
    if path.is_file():
        identity = json.loads(path.read_text(encoding="utf-8"))
    else:
        identity = {
            "commit": os.environ.get("HACKDEEPWIKI_BUILD_COMMIT") or _source_commit(),
            "run": os.environ.get("HACKDEEPWIKI_BUILD_RUN") or "local",
            "channel": os.environ.get("HACKDEEPWIKI_BUILD_CHANNEL") or "source",
            "built_at": None,
        }
    return {**identity, "components": public_component_inventory()}


def write_build_info(path: Path | None = None) -> Path:
    destination = path or (
        Path(__file__).resolve().parents[1] / "build" / "build-info.json"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "commit": os.environ.get("GITHUB_SHA") or _source_commit(),
        "run": os.environ.get("GITHUB_RUN_NUMBER") or "local",
        "channel": (
            "tag"
            if os.environ.get("GITHUB_REF", "").startswith("refs/tags/")
            else os.environ.get("HACKDEEPWIKI_BUILD_CHANNEL", "development")
        ),
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    destination.write_text(
        json.dumps(data, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination
