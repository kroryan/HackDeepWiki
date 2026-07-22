'use client';

import React, { useMemo } from 'react';
import Mermaid from '@/components/Mermaid';
import { GraphData, GraphNode, Severity } from './types';
import { SEVERITY_COLORS } from './config/colors';

interface Props {
  graph: GraphData;
  height?: number | string;
}

const MAX_CVE_NODES = 40; // keep the mermaid diagram readable

/**
 * Renders the vulnerability graph as a Mermaid flowchart (always available,
 * no extra deps). Used both as a standalone "2D" view and as the automatic
 * fallback when the 3D view can't load.
 */
export default function VulnGraph2D({ graph: graphIn, height = 460 }: Props) {
  // Defensive: a malformed/legacy report could theoretically still reach
  // here without nodes/links (the backend now backfills both, but this is
  // the last line of defense against crashing the whole panel over it).
  const graph: GraphData = useMemo(() => ({ nodes: graphIn?.nodes ?? [], links: graphIn?.links ?? [] }), [graphIn]);
  const chart = useMemo(() => buildMermaid(graph), [graph]);

  if (!graph.nodes.length) {
    return (
      <div className="flex items-center justify-center text-[var(--muted)] text-sm"
           style={{ height }}>
        No graph data available.
      </div>
    );
  }

  return (
    <div style={{ height }} className="overflow-auto rounded-md border border-[var(--border-color)] bg-[var(--card-bg)] p-2">
      <Mermaid chart={chart} zoomingEnabled />
    </div>
  );
}

function sanitizeId(id: string): string {
  return 'n' + id.replace(/[^A-Za-z0-9]/g, '_');
}

function buildMermaid(graph: GraphData): string {
  // Rank CVE nodes by severity, keep only the worst MAX_CVE_NODES to avoid
  // exploding mermaid on huge reports. Every other node type (site/category/
  // finding/technology/package/cwe/fix/file) is structural, not per-CVE, so
  // it's always kept -- capping only CVEs is what keeps a website scan's
  // graph (mostly non-CVE header/cookie/TLS findings) from being reduced to
  // near-nothing, since most of it has no 'cve' node to hang off of.
  const sevRank: Record<Severity, number> = {
    CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, UNKNOWN: 4,
  };
  const cveNodesKept = graph.nodes
    .filter((n) => n.type === 'cve')
    .sort((a, b) => (sevRank[a.severity || 'UNKNOWN'] - sevRank[b.severity || 'UNKNOWN']))
    .slice(0, MAX_CVE_NODES);
  const keepIds = new Set<string>(graph.nodes.filter((n) => n.type !== 'cve').map((n) => n.id));
  for (const n of cveNodesKept) keepIds.add(n.id);

  const keepLinks = graph.links.filter((l) => keepIds.has(l.source) && keepIds.has(l.target));
  const keepNodes = graph.nodes.filter((n) => keepIds.has(n.id));

  const lines: string[] = ['graph LR'];

  for (const node of keepNodes) {
    lines.push(`  ${nodeLine(node)}`);
  }
  for (const link of keepLinks) {
    lines.push(
      `  ${sanitizeId(link.source)} -->|${link.label}| ${sanitizeId(link.target)}`,
    );
  }

  // classDef colours
  lines.push(`  classDef crit fill:${SEVERITY_COLORS.CRITICAL},color:#fff,stroke:#7f1d1d;`);
  lines.push(`  classDef high fill:${SEVERITY_COLORS.HIGH},color:#fff,stroke:#7f1d1d;`);
  lines.push(`  classDef med fill:${SEVERITY_COLORS.MEDIUM},color:#000,stroke:#78350f;`);
  lines.push(`  classDef low fill:${SEVERITY_COLORS.LOW},color:#fff,stroke:#14532d;`);
  lines.push(`  classDef unk fill:${SEVERITY_COLORS.UNKNOWN},color:#fff,stroke:#334155;`);
  lines.push(`  classDef pkg fill:#3b82f6,color:#fff,stroke:#1e3a8a;`);
  lines.push(`  classDef cwe fill:#a855f7,color:#fff,stroke:#581c87;`);
  lines.push(`  classDef fix fill:#22c55e,color:#fff,stroke:#14532d;`);
  lines.push(`  classDef file fill:#94a3b8,color:#000,stroke:#334155;`);
  lines.push(`  classDef site fill:#e2e8f0,color:#000,stroke:#334155;`);
  lines.push(`  classDef tech fill:#3b82f6,color:#fff,stroke:#1e3a8a;`);
  lines.push(`  classDef cat fill:#a855f7,color:#fff,stroke:#581c87;`);
  lines.push(`  classDef finding fill:#f59e0b,color:#000,stroke:#78350f;`);

  // assign classes
  const classAssign: string[] = [];
  for (const node of keepNodes) {
    const sid = sanitizeId(node.id);
    if (node.type === 'cve' || node.type === 'finding') {
      const cls = node.severity === 'CRITICAL' ? 'crit'
        : node.severity === 'HIGH' ? 'high'
        : node.severity === 'MEDIUM' ? 'med'
        : node.severity === 'LOW' ? 'low' : 'unk';
      classAssign.push(`class ${sid} ${cls};`);
    } else if (node.type === 'package') classAssign.push(`class ${sid} pkg;`);
    else if (node.type === 'cwe') classAssign.push(`class ${sid} cwe;`);
    else if (node.type === 'fix') classAssign.push(`class ${sid} fix;`);
    else if (node.type === 'file') classAssign.push(`class ${sid} file;`);
    else if (node.type === 'site') classAssign.push(`class ${sid} site;`);
    else if (node.type === 'technology') classAssign.push(`class ${sid} tech;`);
    else if (node.type === 'category') classAssign.push(`class ${sid} cat;`);
  }
  lines.push(...classAssign);

  return lines.join('\n');
}

function nodeLine(node: GraphNode): string {
  const sid = sanitizeId(node.id);
  const label = escapeMermaid(node.label);
  if (node.type === 'cve') {
    const sev = node.severity || 'UNKNOWN';
    const cvss = node.cvss_score != null ? ` ${node.cvss_score.toFixed(1)}` : '';
    return `${sid}["🔴 ${label}<br/>${sev}${cvss}"]`;
  }
  if (node.type === 'finding') {
    const sev = node.severity || 'UNKNOWN';
    return `${sid}["⚠️ ${label}<br/>${sev}"]`;
  }
  if (node.type === 'package') return `${sid}["📦 ${label}"]`;
  if (node.type === 'cwe') return `${sid}["🏷️ ${label}"]`;
  if (node.type === 'fix') return `${sid}["🛡️ ${label}"]`;
  if (node.type === 'site') return `${sid}["🌐 ${label}"]`;
  if (node.type === 'technology') return `${sid}["🧩 ${label}"]`;
  if (node.type === 'category') return `${sid}["🗂️ ${label}"]`;
  return `${sid}["📁 ${label}"]`;
}

function escapeMermaid(s: string): string {
  // mermaid node text with htmlLabels: escape quotes and brackets
  return s.replace(/"/g, "'").replace(/[<>]/g, (c) => (c === '<' ? '&lt;' : '&gt;'));
}