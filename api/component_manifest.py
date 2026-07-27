"""Validated access to the packaged-component manifest."""

from __future__ import annotations

import json
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def manifest_path() -> Path:
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS) / "build" / "components.json"
    return Path(__file__).resolve().parents[1] / "build" / "components.json"


@lru_cache(maxsize=1)
def component_manifest() -> dict[str, Any]:
    path = manifest_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 1:
        raise RuntimeError("Unsupported build/components.json schema")
    for component in ("node", "opencode", "engraphis", "appimage", "mingit"):
        if component not in data:
            raise RuntimeError(f"Component manifest is missing {component}")
    hashes: list[str] = []
    hashes.append(data["mingit"]["sha256"])
    hashes.extend(data["opencode"]["assets"].values())
    hashes.extend(
        asset["sha256"] for asset in data["node"]["assets"].values()
    )
    hashes.extend(
        data["appimage"][name]["sha256"] for name in ("tool", "runtime")
    )
    if not all(isinstance(value, str) and _SHA256.fullmatch(value) for value in hashes):
        raise RuntimeError("Component manifest contains an invalid SHA-256")
    return data


def validate_component_manifest() -> dict[str, Any]:
    """Validate and return a detached copy of the component inventory."""
    return dict(component_manifest())


def public_component_inventory() -> dict[str, Any]:
    manifest = component_manifest()
    return {
        "node": {"version": manifest["node"]["version"]},
        "opencode": {"version": manifest["opencode"]["version"]},
        "engraphis": {
            "version": manifest["engraphis"]["version"],
            "commit": manifest["engraphis"]["commit"],
        },
        "mingit": {"version": manifest["mingit"]["version"]},
        "appimage": {"verified": True},
    }
