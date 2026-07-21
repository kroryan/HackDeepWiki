"""Vulnerability scanning subpackage for HackDeepWiki.

Scans a locally-cloned repository's dependency manifests/lockfiles for known
CVEs via the free OSV.dev API (no API key required), optionally enriches with
NVD data (user-supplied key), and uses the configured LLM to produce a
per-finding impact / exploitability / remediation analysis.

Public entry points (see ``orchestrator`` / the ``/ws/vuln_scan`` handler in
``api.api``):
    - ``dep_parser.parse_dependencies(repo_dir, ...)`` -> list[Dependency]
    - ``osv_client.query_vulnerabilities(deps, ...)`` -> list[CVEFinding]
    - ``llm_analyzer.analyze_findings(...)`` -> enriches findings with AI fields

Storage: results are persisted as JSON in the shared wikicache directory
alongside the wiki itself (100% portable, no extra dependencies).
"""

from api.vuln_scanner.models import (
    Dependency,
    CVEFinding,
    VulnReport,
    GraphData,
    GraphNode,
    GraphLink,
    SEVERITY_ORDER,
    severity_from_score,
)

__all__ = [
    "Dependency",
    "CVEFinding",
    "VulnReport",
    "GraphData",
    "GraphNode",
    "GraphLink",
    "SEVERITY_ORDER",
    "severity_from_score",
]