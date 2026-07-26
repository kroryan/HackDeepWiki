"""Generate a deterministic CycloneDX inventory from both lock files."""

from __future__ import annotations

import argparse
import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path


def _python_components(lock_path: Path) -> list[dict]:
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    return [
        {
            "type": "library",
            "name": package["name"],
            "version": package["version"],
            "purl": f"pkg:pypi/{package['name']}@{package['version']}",
            "properties": [
                {"name": "hackdeepwiki:ecosystem", "value": "python"},
                {
                    "name": "hackdeepwiki:category",
                    "value": str(package.get("category", "main")),
                },
            ],
        }
        for package in lock.get("package", [])
    ]


def _npm_components(lock_path: Path) -> list[dict]:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    components = []
    for location, package in sorted(lock.get("packages", {}).items()):
        if not location or not package.get("version"):
            continue
        name = package.get("name") or location.rsplit("node_modules/", 1)[-1]
        version = package["version"]
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": f"pkg:npm/{name.replace('@', '%40')}@{version}",
                "properties": [
                    {"name": "hackdeepwiki:ecosystem", "value": "npm"},
                    {
                        "name": "hackdeepwiki:development",
                        "value": str(bool(package.get("dev"))).lower(),
                    },
                ],
            }
        )
    return components


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="HackDeepWiki.cdx.json")
    parser.add_argument("--commit", default="unknown")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    components = _python_components(root / "api" / "poetry.lock")
    components.extend(_npm_components(root / "package-lock.json"))
    components.sort(key=lambda item: (item["purl"], item["version"]))
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "component": {
                "type": "application",
                "name": "HackDeepWiki",
                "version": args.commit,
            },
        },
        "components": components,
    }
    Path(args.output).write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
