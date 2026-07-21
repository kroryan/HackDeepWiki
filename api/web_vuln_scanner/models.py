"""Data models for the website security scanner. Mirrors the shape of
``api.vuln_scanner.models`` (dataclasses + to_dict/from_dict, same severity
scale) but findings are keyed by URL + check, not by package."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

SEVERITY_RANKS = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]

# Web-check categories, used to route findings into report sections.
CATEGORY_HEADERS = "headers"
CATEGORY_COOKIES = "cookies"
CATEGORY_TLS = "tls"
CATEGORY_EXPOSURE = "exposure"  # exposed sensitive paths
CATEGORY_CVE = "cve"  # technology fingerprint -> known CVE


@dataclass
class WebFinding:
    id: str  # stable slug, e.g. "missing-hsts", "exposed-.env", or a CVE id
    category: str  # headers | cookies | tls | exposure | cve
    severity: str = "INFO"  # CRITICAL | HIGH | MEDIUM | LOW | INFO
    title: str = ""
    description: str = ""
    url: str = ""  # the specific page/endpoint this finding applies to
    evidence: str = ""  # short raw evidence (header value, response snippet, ...)
    remediation: str = ""
    references: List[str] = field(default_factory=list)
    # Only populated for category == "cve"
    cve_id: Optional[str] = None
    cvss_score: Optional[float] = None
    technology: Optional[str] = None
    technology_version: Optional[str] = None
    # LLM-assisted CVE correlation: the deterministic OSV-based pass may miss
    # CVEs that don't match on exact version fingerprints, or surface ones
    # that don't actually apply -- the LLM can propose additional candidates
    # (ai_proposed=True) or flag a low-confidence dismissal (ai_dismissed=True
    # with ai_dismiss_reason explaining why), same idea as the user's request
    # for the dependency scanner. Human-reviewable, never silently mutates
    # deterministic findings.
    ai_proposed: bool = False
    ai_dismissed: bool = False
    ai_dismiss_reason: str = ""
    ai_notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WebVulnReport:
    site_url: str = ""
    owner: str = ""
    repo: str = ""
    language: str = "en"
    generated_at: str = ""
    provider: str = ""
    model: str = ""
    pages_scanned: int = 0
    counts: Dict[str, int] = field(default_factory=lambda: {s: 0 for s in SEVERITY_RANKS})
    total_findings: int = 0
    header_findings: List[Dict[str, Any]] = field(default_factory=list)
    cookie_findings: List[Dict[str, Any]] = field(default_factory=list)
    tls_findings: List[Dict[str, Any]] = field(default_factory=list)
    exposure_findings: List[Dict[str, Any]] = field(default_factory=list)
    cve_findings: List[Dict[str, Any]] = field(default_factory=list)
    all_findings: List[Dict[str, Any]] = field(default_factory=list)
    detected_technologies: List[Dict[str, Any]] = field(default_factory=list)  # [{name, version}]
    ai_analyzed: bool = False
    # Whether the opt-in Docker toolkit pass (nmap/nikto/httpx/whatweb/
    # testssl/nuclei/subfinder/ffuf/dalfox/wpscan) ran -- surfaced to the
    # frontend so a thin report (just the always-on header/cookie/TLS checks)
    # is visibly explained instead of looking incomplete.
    deep_scan_ran: bool = False
    # Consolidated, prioritized "Suggested Solutions" page -- see
    # api.vuln_common.remediation.build_remediation_plan.
    remediation_plan: Dict[str, Any] = field(default_factory=dict)
    # Interactive graph (site -> technology -> CVE, site -> category -> finding)
    # -- see build_web_graph below. Mirrors api.vuln_scanner's dependency graph
    # so the frontend can reuse the same VulnGraph3D/2D component for both.
    # Defaults to an empty (not missing) nodes/links shape -- the frontend
    # types this as required GraphData, so reading a report saved before this
    # field existed must still produce something it can render (an empty
    # graph), not a crash.
    graph: Dict[str, Any] = field(default_factory=lambda: {"nodes": [], "links": []})

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WebVulnReport":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in (data or {}).items() if k in known}
        return cls(**filtered)


_CATEGORY_LABELS = {
    CATEGORY_HEADERS: "Headers",
    CATEGORY_COOKIES: "Cookies",
    CATEGORY_TLS: "TLS",
    CATEGORY_EXPOSURE: "Exposed Paths",
}


def build_web_graph(findings: List[WebFinding], technologies: List[str], site_url: str):
    """Build the interactive graph (Site / Technology / CVE / Category /
    Finding nodes) from a completed scan's findings. Reuses the generic
    GraphNode/GraphLink/GraphData dataclasses from api.vuln_scanner.models --
    same JSON shape, so the frontend's VulnGraph3D/VulnGraph2D components
    work unmodified for both dependency and website scans."""
    from api.vuln_scanner.models import GraphData, GraphLink, GraphNode

    g = GraphData()
    seen_nodes: set = set()
    seen_links: set = set()

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

    hostname = urlparse(site_url).netloc or site_url
    site_node = f"site:{hostname}"
    add_node(GraphNode(id=site_node, type="site", label=hostname))

    tech_node_by_key: Dict[str, str] = {}
    for tech in technologies:
        tid = f"tech:{tech}"
        tech_node_by_key[tech] = tid
        add_node(GraphNode(id=tid, type="technology", label=tech))
        add_link(site_node, tid, "USES")

    for f in findings:
        if f.category == CATEGORY_CVE:
            cve_node = f"cve:{f.id}"
            add_node(GraphNode(
                id=cve_node, type="cve", label=f.cve_id or f.title[:40],
                severity=f.severity, cvss_score=f.cvss_score,
            ))
            tech_key = None
            if f.technology:
                candidate = f"{f.technology}@{f.technology_version}" if f.technology_version else f.technology
                tech_key = tech_node_by_key.get(candidate) or tech_node_by_key.get(f.technology)
            add_link(tech_key or site_node, cve_node, "AFFECTED_BY")
            continue

        cat_node = f"category:{f.category}"
        add_node(GraphNode(id=cat_node, type="category", label=_CATEGORY_LABELS.get(f.category, f.category.title())))
        add_link(site_node, cat_node, "HAS")

        finding_node = f"finding:{f.id}"
        add_node(GraphNode(id=finding_node, type="finding", label=f.title[:60], severity=f.severity))
        add_link(cat_node, finding_node, "CONTAINS")

    return g
