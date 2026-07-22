"""Data models for the vulnerability scanner.

Plain ``@dataclass`` objects with ``to_dict``/``from_dict`` helpers so the
whole report can be (de)serialised to JSON for wikicache storage, Obsidian
export, and the frontend 3D graph -- no pydantic dependency needed here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Severity helpers
# ---------------------------------------------------------------------------

SEVERITY_ORDER = {
    "CRITICAL": 4,
    "HIGH": 3,
    "MEDIUM": 2,
    "LOW": 1,
    "UNKNOWN": 0,
}

# Stable ordering for display / counts.
SEVERITY_RANKS = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]


def severity_from_score(score: Optional[float]) -> str:
    """Map a numeric CVSS base score (0.0-10.0) onto a severity label using
    the standard CVSS v3 thresholds."""
    if score is None:
        return "UNKNOWN"
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if s >= 9.0:
        return "CRITICAL"
    if s >= 7.0:
        return "HIGH"
    if s >= 4.0:
        return "MEDIUM"
    if s > 0.0:
        return "LOW"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Core records
# ---------------------------------------------------------------------------


@dataclass
class Dependency:
    """A single resolved dependency found in the repo's manifests/lockfiles."""

    name: str
    version: str
    ecosystem: str  # OSV ecosystem: npm, PyPI, Go, crates.io, Maven, RubyGems, Packagist, NuGet
    category: str = "dependency"  # client | server | dependency
    dev: bool = False
    source_files: List[str] = field(default_factory=list)  # manifests where it was declared
    usage_files: List[str] = field(default_factory=list)  # code files that import/use it

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CVEFinding:
    """A known vulnerability affecting an installed dependency.

    The ``ai_*`` fields are filled in later by ``llm_analyzer``; they default
    to empty so a report can be persisted before/without LLM enrichment.
    """

    id: str  # "CVE-2024-XXXXX" or "GHSA-xxxx"
    aliases: List[str] = field(default_factory=list)
    package_name: str = ""
    package_ecosystem: str = ""
    installed_version: str = ""
    fixed_version: Optional[str] = None
    severity: str = "UNKNOWN"  # CRITICAL | HIGH | MEDIUM | LOW | UNKNOWN
    cvss_score: Optional[float] = None
    summary: str = ""
    details: str = ""
    references: List[str] = field(default_factory=list)
    published: str = ""
    cwe_ids: List[str] = field(default_factory=list)
    category: str = "dependency"  # inherited from the dependency
    dev: bool = False
    source_files: List[str] = field(default_factory=list)
    usage_files: List[str] = field(default_factory=list)
    # LLM-generated fields
    ai_impact_analysis: str = ""
    ai_exploitability: str = ""
    ai_remediation: str = ""
    ai_priority: int = 0  # 1-5

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 3D graph model (consumed directly by the frontend)
# ---------------------------------------------------------------------------


@dataclass
class GraphNode:
    id: str
    type: str  # package | cve | file | cwe | fix
    label: str
    severity: Optional[str] = None  # for cve nodes
    cvss_score: Optional[float] = None
    cve_count: Optional[int] = None  # for package nodes
    group: Optional[str] = None  # category for package nodes

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class GraphLink:
    source: str
    target: str
    label: str  # AFFECTED_BY | CATEGORIZED_AS | USES | FIXED_IN

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class GraphData:
    nodes: List[GraphNode] = field(default_factory=list)
    links: List[GraphLink] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "links": [l.to_dict() for l in self.links],
        }


# ---------------------------------------------------------------------------
# Aggregate report
# ---------------------------------------------------------------------------


@dataclass
class VulnReport:
    """The full vulnerability report persisted to wikicache and shipped to the
    frontend. ``to_dict`` is the canonical JSON shape stored on disk."""

    repo_url: str = ""
    repo_type: str = ""
    owner: str = ""
    repo: str = ""
    language: str = "en"
    generated_at: str = ""
    provider: str = ""
    model: str = ""
    # Counts by severity
    counts: Dict[str, int] = field(default_factory=lambda: {s: 0 for s in SEVERITY_RANKS})
    total_findings: int = 0
    total_dependencies_scanned: int = 0
    # Findings split into the three wiki subsections
    client_findings: List[Dict[str, Any]] = field(default_factory=list)
    server_findings: List[Dict[str, Any]] = field(default_factory=list)
    dependency_findings: List[Dict[str, Any]] = field(default_factory=list)
    all_findings: List[Dict[str, Any]] = field(default_factory=list)
    # The deps that were scanned (name@version, for transparency)
    scanned_dependencies: List[Dict[str, Any]] = field(default_factory=list)
    # Interactive graph. Defaults to an empty (not missing) nodes/links shape
    # -- the frontend types this as required GraphData and reads graph.nodes
    # unconditionally, so a report saved before this field existed (or any
    # other malformed/legacy record) must still produce something it can
    # render instead of crashing the whole Security Analysis panel.
    graph: Dict[str, Any] = field(default_factory=lambda: {"nodes": [], "links": []})
    # Whether LLM analysis was run
    ai_analyzed: bool = False
    # Optional Docker-toolkit pass over the repo (gitleaks secret detection +
    # semgrep SAST) -- see api.web_vuln_scanner.docker_tools.run_code_scan_toolkit.
    # Uses the same finding shape as the web scanner (WebFinding.to_dict())
    # since these are file/line findings, not package/CVE findings.
    code_scan_findings: List[Dict[str, Any]] = field(default_factory=list)
    code_scan_ran: bool = False
    # Consolidated, prioritized "Suggested Solutions" page -- see
    # api.vuln_common.remediation.build_remediation_plan. Every scan type
    # (dependency/web/code) produces one of these.
    remediation_plan: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VulnReport":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**filtered)


def build_graph(findings: List[CVEFinding], deps: List[Dependency]) -> GraphData:
    """Build the interactive graph (Package / CVE / File / CWE / Fix nodes)
    from the findings + dependencies. Node/link ids are stable strings so the
    frontend can key on them."""
    g = GraphData()
    seen_nodes: set[str] = set()
    seen_links: set[tuple] = set()

    def add_node(node: GraphNode) -> None:
        if node.id in seen_nodes:
            return
        seen_nodes.add(node.id)
        g.nodes.append(node)

    def add_link(source: str, target: str, label: str) -> None:
        key = (source, target, label)
        if key in seen_links:
            return
        seen_links.add(key)
        g.links.append(GraphLink(source=source, target=target, label=label))

    # CVE count per package (sizes package nodes)
    pkg_cve_count: Dict[str, int] = {}
    for f in findings:
        key = f"{f.package_ecosystem}:{f.package_name}"
        pkg_cve_count[key] = pkg_cve_count.get(key, 0) + 1

    # Package nodes (from deps that have findings, plus all scanned deps lightly)
    finding_pkg_keys = {f"{f.package_ecosystem}:{f.package_name}" for f in findings}
    for dep in deps:
        key = f"{dep.ecosystem}:{dep.name}"
        if key not in finding_pkg_keys:
            continue  # only render packages that actually have CVEs (keeps graph readable)
        add_node(GraphNode(
            id=f"pkg:{key}",
            type="package",
            label=f"{dep.name}@{dep.version}",
            cve_count=pkg_cve_count.get(key, 0),
            group=dep.category,
        ))

    for f in findings:
        pkg_key = f"{f.package_ecosystem}:{f.package_name}"
        pkg_node = f"pkg:{pkg_key}"
        add_node(GraphNode(
            id=f"pkg:{pkg_key}",
            type="package",
            label=f"{f.package_name}@{f.installed_version}",
            cve_count=pkg_cve_count.get(pkg_key, 0),
            group=f.category,
        ))
        cve_node = f"cve:{f.id}"
        add_node(GraphNode(
            id=cve_node,
            type="cve",
            label=f.id,
            severity=f.severity,
            cvss_score=f.cvss_score,
        ))
        add_link(pkg_node, cve_node, "AFFECTED_BY")

        # CWE nodes
        for cwe in f.cwe_ids:
            cwe_node = f"cwe:{cwe}"
            add_node(GraphNode(id=cwe_node, type="cwe", label=cwe))
            add_link(cve_node, cwe_node, "CATEGORIZED_AS")

        # Fix node
        if f.fixed_version:
            fix_node = f"fix:{f.id}"
            add_node(GraphNode(
                id=fix_node,
                type="fix",
                label=f"fix {f.fixed_version}",
            ))
            add_link(cve_node, fix_node, "FIXED_IN")

        # File nodes (usage)
        for fp in (f.usage_files or [])[:8]:  # cap per CVE for readability
            file_node = f"file:{fp}"
            add_node(GraphNode(id=file_node, type="file", label=fp))
            add_link(file_node, pkg_node, "USES")

    return g