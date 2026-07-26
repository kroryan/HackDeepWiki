"""Enforce pip-audit with narrow, expiring exceptions and approved Git sources."""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any


def validate_report(report: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    today = date.today()
    errors: list[str] = []
    allowed_vulnerabilities: set[tuple[str, str, str]] = set()
    allowed_skips: set[tuple[str, str, str]] = set()

    for exception in policy.get("exceptions", []):
        expiry = date.fromisoformat(exception["expires"])
        identity = (
            str(exception["package"]).lower(),
            str(exception["version"]),
            str(exception["advisory"]),
        )
        if expiry < today:
            errors.append(
                f"Expired pip audit exception: {identity[2]} ({exception['expires']})"
            )
        allowed_vulnerabilities.add(identity)

    for skip in policy.get("allowed_skips", []):
        allowed_skips.add(
            (
                str(skip["package"]).lower(),
                str(skip["version"]),
                str(skip["reason_contains"]),
            )
        )

    observed_vulnerabilities: set[tuple[str, str, str]] = set()
    observed_skips: set[tuple[str, str, str]] = set()
    for dependency in report.get("dependencies", []):
        package = str(dependency.get("name", "")).lower()
        version = str(dependency.get("version", ""))
        skip_reason = dependency.get("skip_reason")
        if skip_reason:
            if not version:
                embedded_version = re.search(
                    rf"\b{re.escape(package)} \(([^)]+)\)", str(skip_reason), re.IGNORECASE
                )
                if embedded_version:
                    version = embedded_version.group(1)
            matches = {
                item
                for item in allowed_skips
                if item[0] == package
                and item[1] == version
                and item[2] in str(skip_reason)
            }
            if not matches:
                errors.append(
                    f"Unapproved unaudited dependency: {package} {version}: {skip_reason}"
                )
            observed_skips.update(matches)
            continue

        for vulnerability in dependency.get("vulns", []):
            identifiers = {
                str(vulnerability.get("id", "")),
                *(str(alias) for alias in vulnerability.get("aliases", [])),
            }
            matches = {
                item
                for item in allowed_vulnerabilities
                if item[0] == package and item[1] == version and item[2] in identifiers
            }
            if not matches:
                errors.append(
                    f"Unapproved vulnerability: {package} {version} "
                    f"{vulnerability.get('id')}"
                )
            observed_vulnerabilities.update(matches)

    stale_vulnerabilities = allowed_vulnerabilities - observed_vulnerabilities
    for package, version, advisory in sorted(stale_vulnerabilities):
        errors.append(
            f"Stale pip audit exception (remove it): {package} {version} {advisory}"
        )
    stale_skips = allowed_skips - observed_skips
    for package, version, _reason in sorted(stale_skips):
        errors.append(f"Expected unaudited dependency was not observed: {package} {version}")
    return errors


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_pip_audit.py AUDIT_JSON", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[1]
    report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    policy = json.loads(
        (root / ".github/audit/pip-audit-exceptions.json").read_text(encoding="utf-8")
    )
    errors = validate_report(report, policy)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("pip audit: no unapproved vulnerabilities or unaudited dependencies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
