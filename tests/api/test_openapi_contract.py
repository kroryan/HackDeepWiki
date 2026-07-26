"""Deliberate review gate for HTTP API additions/removals."""

from __future__ import annotations

import json
from pathlib import Path

from api.api import app


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


def test_openapi_paths_match_reviewed_contract():
    schema = app.openapi()
    actual = {
        path: sorted(
            method.upper()
            for method in operations
            if method.lower() in HTTP_METHODS
        )
        for path, operations in sorted(schema["paths"].items())
    }
    contract_path = Path(__file__).parents[1] / "contracts" / "openapi_paths.json"
    expected = json.loads(contract_path.read_text(encoding="utf-8"))
    assert actual == expected, (
        "The HTTP contract changed. Review auth, frontend compatibility and "
        "payloads, then update tests/contracts/openapi_paths.json deliberately."
    )
