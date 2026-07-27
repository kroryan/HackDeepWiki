"""Obsidian export helpers for the vulnerability report.

Produces the contents of a ``🔐 Security/`` folder to drop into the Obsidian
vault zip:
    - ``Security Overview.md``        – stats + a Mermaid dependency graph
    - ``Client-Side Vulnerabilities.md``
    - ``Server-Side Vulnerabilities.md``
    - ``Dependency Vulnerabilities.md``
    - ``Vulnerability Graph.canvas``  – native Obsidian Canvas board

Pure stdlib (``json``); no extra dependencies. The Mermaid diagram and the
Canvas board are both generated (per the project decision: ship both for max
compatibility).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

SEVERITY_COLORS: Dict[str, str] = {
    "CRITICAL": "#ff3333",
    "HIGH": "#ef4444",
    "MEDIUM": "#f59e0b",
    "LOW": "#22c55e",
    "UNKNOWN": "#64748b",
}

# Obsidian Canvas colour ids: 1=red,2=orange,3=yellow,4=green,5=cyan,6=purple.
SEVERITY_CANVAS_COLOR: Dict[str, str] = {
    "CRITICAL": "1", "HIGH": "2", "MEDIUM": "3", "LOW": "4", "UNKNOWN": "5",
}

SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]


def _safe_filename(name: str) -> str:
    name = re.sub(r'[*"\\/<>:|?]', '-', name)
    name = re.sub(r'[\x00-\x1f]', '', name)
    name = re.sub(r'-{2,}', '-', name)
    return name.strip().rstrip('.-').strip() or "Untitled"


def _md_finding(f: Dict[str, Any]) -> str:
    """One finding as a Markdown block for a subsection note."""
    sev = f.get("severity", "UNKNOWN")
    lines: List[str] = []
    lines.append(f"## `{f.get('id', '')}`")
    lines.append("")
    lines.append(f"> [!warning] {sev}"
                 + (f" · CVSS {f.get('cvss_score')}" if f.get("cvss_score") is not None else "")
                 + (f" · AI priority {f.get('ai_priority')}/5" if f.get("ai_priority") else ""))
    lines.append("")
    lines.append(f"- **Package:** `{f.get('package_name', '')}@{f.get('installed_version', '')}`"
                 f" ({f.get('package_ecosystem', '')}"
                 + (", dev" if f.get("dev") else "") + ")")
    fixed = f.get("fixed_version")
    lines.append(f"- **Fixed in:** `{fixed}`" if fixed else "- **Fixed in:** _no fix published yet_")
    if f.get("aliases"):
        lines.append(f"- **Aliases:** {', '.join('`' + a + '`' for a in f['aliases'])}")
    if f.get("cwe_ids"):
        lines.append(f"- **CWE:** {', '.join('`' + c + '`' for c in f['cwe_ids'])}")
    if f.get("summary"):
        lines.append(f"- **Summary:** {f['summary']}")
    lines.append("")
    if f.get("ai_impact_analysis"):
        lines.append("### 📊 Impact analysis")
        lines.append(f["ai_impact_analysis"])
        lines.append("")
    if f.get("ai_exploit_vector"):
        lines.append("### 🎯 Attack vector")
        lines.append(f["ai_exploit_vector"])
        lines.append("")
    if f.get("ai_exploitability"):
        lines.append("### ⚔️ Exploitability")
        lines.append(f["ai_exploitability"])
        lines.append("")
    if f.get("ai_exploit_plan"):
        lines.append("### 🗺️ Exploitation plan")
        lines.append(f["ai_exploit_plan"])
        lines.append("")
    if f.get("ai_remediation"):
        lines.append("### 🛠️ Remediation")
        lines.append(f["ai_remediation"])
        lines.append("")
    if f.get("usage_files"):
        lines.append("### 📁 Used in")
        for fp in f["usage_files"]:
            lines.append(f"- `{fp}`")
        lines.append("")
    if f.get("references"):
        lines.append("### References")
        for r in f["references"]:
            lines.append(f"- {r}")
        lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def build_subsection_note(title: str, findings: List[Dict[str, Any]]) -> str:
    body: List[str] = [f"# {title}", ""]
    if not findings:
        body.append("_No vulnerabilities in this category._")
        return "\n".join(body) + "\n"
    counts: Dict[str, int] = {}
    for f in findings:
        s = f.get("severity", "UNKNOWN")
        counts[s] = counts.get(s, 0) + 1
    body.append("Counts: " + " · ".join(f"{s}: {counts.get(s, 0)}" for s in SEVERITY_ORDER if counts.get(s))
                + "\n")
    for f in findings:
        body.append(_md_finding(f))
    return "\n".join(body)


def build_mermaid(report: Dict[str, Any]) -> str:
    """Mermaid flowchart of packages -> CVEs -> CWEs (capped for readability)."""
    graph = report.get("graph") or {}
    nodes = graph.get("nodes") or []
    links = graph.get("links") or []
    if not nodes:
        return "```mermaid\ngraph LR\n  empty[No vulnerable dependencies]\n```\n"

    # Cap to worst 40 CVEs + neighbours.
    sev_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}
    cve_nodes = sorted(
        [n for n in nodes if n.get("type") == "cve"],
        key=lambda n: sev_rank.get(n.get("severity") or "UNKNOWN", 4),
    )[:40]
    keep = set(n["id"] for n in cve_nodes)
    keep_links = [link for link in links
                  if link.get("source") in keep or link.get("target") in keep]
    for link in keep_links:
        keep.add(link["source"])
        keep.add(link["target"])
    keep_nodes = [n for n in nodes if n.get("id") in keep]

    def sid(i: str) -> str:
        return "n" + re.sub(r'[^A-Za-z0-9]', '_', i or "")

    lines = ["```mermaid", "graph LR"]
    for n in keep_nodes:
        s = sid(n.get("id", ""))
        label = (n.get("label") or "").replace('"', "'")
        t = n.get("type")
        if t == "cve":
            lines.append(f'  {s}["🔴 {label}<br/>{n.get("severity","UNKNOWN")}'
                         + (f' {n.get("cvss_score")}' if n.get("cvss_score") is not None else "") + '"]')
        elif t == "package":
            lines.append(f'  {s}["📦 {label}"]')
        elif t == "cwe":
            lines.append(f'  {s}["🏷️ {label}"]')
        elif t == "fix":
            lines.append(f'  {s}["🛡️ {label}"]')
        else:
            lines.append(f'  {s}["📁 {label}"]')
    for link in keep_links:
        lines.append(f'  {sid(link["source"])} -->|{link.get("label","")}| {sid(link["target"])}')
    lines += [
        "  classDef crit fill:#ff3333,color:#fff;",
        "  classDef high fill:#ef4444,color:#fff;",
        "  classDef med fill:#f59e0b,color:#000;",
        "  classDef low fill:#22c55e,color:#fff;",
        "  classDef pkg fill:#3b82f6,color:#fff;",
        "  classDef cwe fill:#a855f7,color:#fff;",
        "  classDef fix fill:#22c55e,color:#fff;",
    ]
    for n in keep_nodes:
        s = sid(n.get("id", ""))
        t = n.get("type")
        if t == "cve":
            cls = {"CRITICAL": "crit", "HIGH": "high", "MEDIUM": "med", "LOW": "low"}.get(n.get("severity"), "crit")
            lines.append(f"  class {s} {cls};")
        elif t == "package":
            lines.append(f"  class {s} pkg;")
        elif t == "cwe":
            lines.append(f"  class {s} cwe;")
        elif t == "fix":
            lines.append(f"  class {s} fix;")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def build_canvas(report: Dict[str, Any]) -> Dict[str, Any]:
    """Build an Obsidian Canvas (.canvas) JSON board."""
    graph = report.get("graph") or {}
    nodes = graph.get("nodes") or []
    links = graph.get("links") or []
    if not nodes:
        return {"nodes": [], "edges": []}

    sev_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "UNKNOWN": 4}
    cve_nodes = sorted(
        [n for n in nodes if n.get("type") == "cve"],
        key=lambda n: sev_rank.get(n.get("severity") or "UNKNOWN", 4),
    )[:60]
    keep = set(n["id"] for n in cve_nodes)
    keep_links = [link for link in links
                  if link.get("source") in keep or link.get("target") in keep]
    for link in keep_links:
        keep.add(link["source"])
        keep.add(link["target"])
    keep_nodes = [n for n in nodes if n.get("id") in keep]

    canvas_nodes: List[Dict[str, Any]] = []
    pos: Dict[str, Dict[str, int]] = {}
    # Layout: packages column (x=-500), CVEs column (x=0), CWE column (x=500),
    # fixes (x=0, below CVEs), files (x=-1000).
    col_x = {"package": -500, "cve": 0, "cwe": 500, "fix": 0, "file": -1000}
    counters: Dict[str, int] = {"package": 0, "cve": 0, "cwe": 0, "fix": 0, "file": 0}

    for n in keep_nodes:
        nid = n.get("id", "")
        t = n.get("type", "file")
        x = col_x.get(t, -1000)
        y = counters.get(t, 0) * 160 - 400
        counters[t] = counters.get(t, 0) + 1
        if t == "fix":
            # offset fixes to the right of CVEs
            x = 260
        label = n.get("label", "")
        if t == "cve":
            text = f"## 🔴 {label}\n**{n.get('severity','UNKNOWN')}**"
            if n.get("cvss_score") is not None:
                text += f" · CVSS {n.get('cvss_score')}"
            color = SEVERITY_CANVAS_COLOR.get(n.get("severity") or "UNKNOWN", "5")
            w, h = 300, 120
        elif t == "package":
            text = f"## 📦 {label}"
            color = "6"
            w, h = 240, 100
        elif t == "cwe":
            text = f"## 🏷️ {label}"
            color = "6"
            w, h = 200, 80
        elif t == "fix":
            text = f"## 🛡️ fix\n{label}"
            color = "4"
            w, h = 200, 80
        else:
            text = f"## 📁 {label}"
            color = "5"
            w, h = 220, 80
        pos[nid] = {"x": x, "y": y, "w": w, "h": h}
        canvas_nodes.append({
            "id": nid,
            "type": "text",
            "text": text,
            "x": x, "y": y, "width": w, "height": h,
            "color": color,
        })

    edges: List[Dict[str, Any]] = []
    for i, link in enumerate(keep_links):
        edges.append({
            "id": f"e{i}",
            "fromNode": link.get("source"),
            "toNode": link.get("target"),
            "label": link.get("label", ""),
        })
    return {"nodes": canvas_nodes, "edges": edges}


def build_overview_note(report: Dict[str, Any], include_graph: bool) -> str:
    counts = report.get("counts") or {}
    lines: List[str] = ["# 🔐 Security Overview", ""]
    lines.append(f"- **Repository:** {report.get('repo_url','')}")
    lines.append(f"- **Generated:** {report.get('generated_at','')}")
    lines.append(f"- **Dependencies scanned:** {report.get('total_dependencies_scanned',0)}")
    lines.append(f"- **Total findings:** {report.get('total_findings',0)}")
    lines.append(f"- **AI analysis:** {'yes' if report.get('ai_analyzed') else 'defaults only'}")
    lines.append("")
    lines.append("## Severity counts")
    lines.append("")
    for s in SEVERITY_ORDER:
        lines.append(f"- **{s}:** {counts.get(s, 0)}")
    lines.append("")
    lines.append("## Sections")
    lines.append("- [[Client-Side Vulnerabilities]]")
    lines.append("- [[Server-Side Vulnerabilities]]")
    lines.append("- [[Dependency Vulnerabilities]]")
    if include_graph:
        lines.append("- [[Vulnerability Graph]] (Canvas board)")
    lines.append("")
    if include_graph:
        lines.append("## Vulnerability Dependency Graph")
        lines.append("")
        lines.append(build_mermaid(report))
    return "\n".join(lines) + "\n"


def build_security_folder(report: Dict[str, Any],
                          include_graph: bool = True) -> Dict[str, str]:
    """Return {filename: content} for the 🔐 Security/ folder of the vault.

    Filenames are relative (no leading folder); the caller puts them under
    ``<vault>/🔐 Security/``.
    """
    folder = "🔐 Security"
    files: Dict[str, str] = {}

    files[f"{folder}/Security Overview.md"] = build_overview_note(report, include_graph)
    files[f"{folder}/Client-Side Vulnerabilities.md"] = build_subsection_note(
        "🖥️ Client-Side Vulnerabilities", report.get("client_findings") or [])
    files[f"{folder}/Server-Side Vulnerabilities.md"] = build_subsection_note(
        "🔒 Server-Side Vulnerabilities", report.get("server_findings") or [])
    files[f"{folder}/Dependency Vulnerabilities.md"] = build_subsection_note(
        "📦 Dependency Vulnerabilities", report.get("dependency_findings") or [])
    if include_graph:
        files[f"{folder}/Vulnerability Graph.canvas"] = json.dumps(
            build_canvas(report), ensure_ascii=False, indent=2)
    return files