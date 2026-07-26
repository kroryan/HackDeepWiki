"""Enforce npm audit with narrow, expiring, dev-only exceptions."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_npm_audit.py AUDIT_JSON", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[1]
    report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    policy = json.loads(
        (root / ".github/audit/npm-audit-exceptions.json").read_text(
            encoding="utf-8"
        )
    )
    allowed: set[int] = set()
    for exception in policy["exceptions"]:
        expiry = date.fromisoformat(exception["expires"])
        if expiry < date.today():
            print(
                f"Expired npm audit exception: {exception['advisory']} "
                f"({exception['expires']})",
                file=sys.stderr,
            )
            return 1
        allowed.add(int(exception["source"]))

    vulnerabilities = report.get("vulnerabilities", {})
    locked_packages = json.loads(
        (root / "package-lock.json").read_text(encoding="utf-8")
    ).get("packages", {})

    def sources_for(name: str, seen: set[str] | None = None) -> set[int]:
        seen = set() if seen is None else seen
        if name in seen:
            return set()
        seen.add(name)
        result: set[int] = set()
        for item in vulnerabilities.get(name, {}).get("via", []):
            if isinstance(item, dict) and item.get("source") is not None:
                result.add(int(item["source"]))
            elif isinstance(item, str):
                result.update(sources_for(item, seen))
        return result

    rejected: list[str] = []
    for name, vulnerability in vulnerabilities.items():
        if vulnerability.get("severity") not in {"high", "critical"}:
            continue
        sources = sources_for(name)
        nodes = vulnerability.get("nodes", [])
        dev_only = bool(nodes) and all(
            node in locked_packages and bool(locked_packages[node].get("dev"))
            for node in nodes
        )
        if not sources or not sources <= allowed or not dev_only:
            rejected.append(
                f"{name}: sources={sorted(sources)}, dev_only={dev_only}"
            )
    if rejected:
        print("Unapproved high/critical npm advisories:", file=sys.stderr)
        print("\n".join(rejected), file=sys.stderr)
        return 1
    print("npm audit: no unapproved high/critical advisories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
