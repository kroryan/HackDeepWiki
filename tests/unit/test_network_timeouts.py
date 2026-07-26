"""Static guard: synchronous HTTP calls must always have a timeout."""

from __future__ import annotations

import ast
from pathlib import Path

REQUEST_METHODS = {"get", "post", "put", "patch", "delete", "request", "head"}


def test_every_requests_call_has_an_explicit_timeout():
    root = Path(__file__).parents[2] / "api"
    missing: list[str] = []
    for path in root.rglob("*.py"):
        # Poetry deliberately creates the locked environment below ``api/``.
        # It is third-party code, not part of the product boundary this guard
        # audits (and a few dependencies still ship Python-2 training tools).
        if {"dist", "build", "AppDir", ".venv", "site-packages"} & set(path.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "requests"
                and function.attr in REQUEST_METHODS
            ):
                continue
            if not any(keyword.arg == "timeout" for keyword in node.keywords):
                missing.append(f"{path.relative_to(root.parent)}:{node.lineno}")
    assert not missing, "requests calls without timeout: " + ", ".join(missing)
