'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import ForceGraph3D, { ForceGraphMethods, NodeObject, LinkObject } from 'react-force-graph-3d';
import { forceCollide } from 'd3-force-3d';
import SpriteText from 'three-spritetext';
import { GraphData, GraphNode, Severity } from './types';
import { SEVERITY_COLORS, NODE_COLORS } from './config/colors';
import {
  cveNodeRadius, packageNodeRadius, FILE_NODE_SIZE, CWE_NODE_SIZE, FIX_NODE_SIZE,
  SITE_NODE_SIZE, CATEGORY_NODE_SIZE, TECH_NODE_SIZE,
} from './config/sizes';
import { GRAPH_CONFIG, CAMERA_DISTANCE } from './config/graph';

interface Props {
  graph: GraphData;
  onNodeClick?: (node: GraphNode) => void;
  height?: number | string;
}

// Minimal local typings for the force-graph runtime objects (the library's
// own types are loose; we only use a handful of fields).
interface FGNode {
  id: string;
  type?: string;
  severity?: Severity | null;
  cvss_score?: number | null;
  cve_count?: number | null;
  label?: string;
  __raw?: GraphNode;
  x?: number;
  y?: number;
  z?: number;
}
interface FGLink {
  source: string | FGNode;
  target: string | FGNode;
  label: string;
}

const MAX_NODES_3D = 250;

// Links were previously a flat 50%-opacity gray at 0.6px width -- barely
// visible against the graph background, and with no distance/charge/collision
// force actually applied (GRAPH_CONFIG declared them but nothing wired them
// up), the layout had no consistent link length or node spacing, so it read
// as a random tangle rather than a legible structure. Fully opaque, wider
// default links plus a bright highlight+particle treatment for whatever's
// connected to the selected node fixes both the "can't see the edges" and
// the "looks disorganized" complaints together.
const LINK_COLOR_DEFAULT = '#94a3b8'; // slate-400, fully opaque
const LINK_COLOR_DIM = '#334155'; // slate-700 -- unrelated links recede once something is selected
const LINK_COLOR_HIGHLIGHT = '#60a5fa'; // blue-400
const LINK_WIDTH_DEFAULT = 1.1;
const LINK_WIDTH_HIGHLIGHT = 3.2;

/**
 * The actual 3D force graph. Only ever loaded client-side (the parent wraps it
 * in next/dynamic with ssr:false) so Three.js never runs during SSR.
 */
export default function VulnGraph3DInner({ graph, onNodeClick, height = 460 }: Props) {
  const fgRef = useRef<ForceGraphMethods<NodeObject<FGNode>, LinkObject<FGNode, FGLink>> | undefined>(undefined);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const data = useMemo(() => prepareData(graph), [graph]);

  useEffect(() => {
    setSelectedId(null);
    const fg = fgRef.current;
    if (fg) {
      // frame the graph
      fg.cameraPosition({ z: CAMERA_DISTANCE });
      const t = setTimeout(() => fg.zoomToFit(200, 60), 600);
      return () => clearTimeout(t);
    }
    return undefined;
  }, [data]);

  // Wire up the link-distance/charge-repulsion/collision forces GRAPH_CONFIG
  // already declares -- react-force-graph only exposes alpha/velocity decay
  // and cooldown as direct props, so the rest of the d3-force simulation has
  // to be reached into via d3Force(). Without this the graph falls back to
  // the library's raw defaults, which pack same-sized nodes far too close
  // together for a graph with 4-14px node radii.
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return undefined;
    const linkForce = fg.d3Force('link') as
      { distance?: (d: number) => void; strength?: (s: number) => void } | undefined;
    linkForce?.distance?.(GRAPH_CONFIG.linkDistance);
    linkForce?.strength?.(GRAPH_CONFIG.linkStrength);
    const chargeForce = fg.d3Force('charge') as { strength?: (s: number) => void } | undefined;
    chargeForce?.strength?.(GRAPH_CONFIG.chargeStrength);
    fg.d3Force('collide', forceCollide(GRAPH_CONFIG.collideRadius) as never);
    const t = setTimeout(() => fg.d3ReheatSimulation(), 0);
    return () => clearTimeout(t);
  }, [data]);

  // Default zoom/rotate speed feels twitchy for a graph you're trying to
  // read rather than a 3D game -- slow both down once OrbitControls mounts
  // (ForceGraph3D loads its Three.js internals asynchronously, so retry
  // until controls() actually returns something).
  useEffect(() => {
    const applyControls = () => {
      const controls = fgRef.current?.controls() as
        { zoomSpeed?: number; rotateSpeed?: number } | undefined;
      if (controls) {
        controls.zoomSpeed = 0.35;
        controls.rotateSpeed = 0.6;
        return true;
      }
      return false;
    };
    if (!applyControls()) {
      const t = setTimeout(applyControls, 500);
      return () => clearTimeout(t);
    }
    return undefined;
  }, [data]);

  if (!data.nodes.length) {
    return (
      <div className="flex items-center justify-center text-[var(--muted)] text-sm"
           style={{ height }}>
        No vulnerable dependencies to graph.
      </div>
    );
  }

  const linkEndpointId = (end: string | FGNode): string =>
    typeof end === 'string' ? end : end.id;
  const isLinkSelected = (l: FGLink): boolean =>
    !!selectedId && (linkEndpointId(l.source) === selectedId || linkEndpointId(l.target) === selectedId);

  return (
    <div style={{ height }} className="rounded-md border border-[var(--border-color)] bg-[var(--background)]/40 overflow-hidden">
      <ForceGraph3D
        ref={fgRef}
        graphData={data}
        nodeRelSize={GRAPH_CONFIG.nodeRelSize}
        cooldownTicks={GRAPH_CONFIG.cooldownTicks}
        d3AlphaDecay={GRAPH_CONFIG.d3AlphaDecay}
        d3VelocityDecay={GRAPH_CONFIG.d3VelocityDecay}
        linkDirectionalArrowLength={4}
        linkDirectionalArrowRelPos={1}
        linkLabel={(l: FGLink) => l.label}
        linkColor={(l: FGLink) => (
          isLinkSelected(l) ? LINK_COLOR_HIGHLIGHT : selectedId ? LINK_COLOR_DIM : LINK_COLOR_DEFAULT
        )}
        linkWidth={(l: FGLink) => (isLinkSelected(l) ? LINK_WIDTH_HIGHLIGHT : LINK_WIDTH_DEFAULT)}
        linkOpacity={0.9}
        linkDirectionalParticles={(l: FGLink) => (isLinkSelected(l) ? 4 : 0)}
        linkDirectionalParticleWidth={2.4}
        linkDirectionalParticleColor={() => LINK_COLOR_HIGHLIGHT}
        linkDirectionalParticleSpeed={0.006}
        nodeColor={nodeColor}
        nodeVal={nodeVal}
        nodeLabel={nodeLabel}
        nodeThreeObject={nodeThreeObject}
        nodeThreeObjectExtend
        onNodeClick={(n: FGNode) => {
          setSelectedId((prev) => (prev === n.id ? null : n.id));
          if (n.__raw) onNodeClick?.(n.__raw);
        }}
        onBackgroundClick={() => setSelectedId(null)}
        showNavInfo={false}
        backgroundColor="rgba(0,0,0,0)"
      />
    </div>
  );
}

// Even after capping CVE nodes, a pathological report (thousands of
// usage-file/package nodes on a huge monorepo, or a giant crawl's worth of
// per-page findings) could still exceed what WebGL/the browser can render
// without freezing the tab -- this is the hard backstop.
const HARD_NODE_CAP = 400;
// Lowest-priority-first: node types dropped to get under HARD_NODE_CAP.
// cve/site/technology/category are never dropped here (cve is already
// capped above; the rest are typically few in number and structurally
// important -- dropping them would gut the graph rather than just declutter it).
const DROP_PRIORITY: string[] = ['file', 'cwe', 'fix', 'finding', 'package'];

function prepareData(graphIn: GraphData) {
  // Defensive: a malformed/legacy report could theoretically still reach
  // here without nodes/links (the backend now backfills both, but this is
  // the last line of defense against crashing the whole panel over it).
  const graph: GraphData = { nodes: graphIn?.nodes ?? [], links: graphIn?.links ?? [] };
  // Cap CVE nodes to the worst MAX_NODES_3D for perf; every other node type
  // (site/category/finding/technology/package/cwe/fix/file) is structural,
  // not per-CVE, so it's always kept -- capping only CVEs is what keeps a
  // website scan's graph (mostly non-CVE header/cookie/TLS findings) from
  // collapsing to near-nothing, since most of it has no 'cve' node to hang
  // off of.
  const cveNodes = graph.nodes.filter((n) => n.type === 'cve');
  cveNodes.sort(sevCompare);
  const keep = new Set<string>(graph.nodes.filter((n) => n.type !== 'cve').map((n) => n.id));
  for (const n of cveNodes.slice(0, MAX_NODES_3D)) keep.add(n.id);

  if (keep.size > HARD_NODE_CAP) {
    const nodeById = new Map(graph.nodes.map((n) => [n.id, n]));
    for (const type of DROP_PRIORITY) {
      if (keep.size <= HARD_NODE_CAP) break;
      for (const id of keep) {
        if (keep.size <= HARD_NODE_CAP) break;
        if (nodeById.get(id)?.type === type) keep.delete(id);
      }
    }
  }

  const keepLinks = graph.links.filter((l) => keep.has(l.source) && keep.has(l.target));
  const keepNodes = graph.nodes.filter((n) => keep.has(n.id));

  return {
    nodes: keepNodes.map((n) => ({ ...n, __raw: n })) as FGNode[],
    links: keepLinks.map((l) => ({ source: l.source, target: l.target, label: l.label })) as FGLink[],
  };
}

function sevCompare(a: GraphNode, b: GraphNode) {
  const r: Record<Severity, number> = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3, UNKNOWN: 4 };
  return r[a.severity || 'UNKNOWN'] - r[b.severity || 'UNKNOWN'];
}

function nodeColor(node: FGNode): string {
  const n = node.__raw ?? (node as unknown as GraphNode);
  if (n.type === 'cve' || n.type === 'finding') {
    return SEVERITY_COLORS[(n.severity as Severity) || 'UNKNOWN'] || SEVERITY_COLORS.UNKNOWN;
  }
  return NODE_COLORS[n.type] || '#94a3b8';
}

function nodeVal(node: FGNode): number {
  const n = node.__raw ?? (node as unknown as GraphNode);
  switch (n.type) {
    case 'cve': return cveNodeRadius(n.cvss_score, n.severity ?? 'UNKNOWN');
    case 'finding': return cveNodeRadius(null, n.severity ?? 'UNKNOWN');
    case 'package': return packageNodeRadius(n.cve_count);
    case 'cwe': return CWE_NODE_SIZE;
    case 'fix': return FIX_NODE_SIZE;
    case 'file': return FILE_NODE_SIZE;
    case 'site': return SITE_NODE_SIZE;
    case 'category': return CATEGORY_NODE_SIZE;
    case 'technology': return TECH_NODE_SIZE;
    default: return FILE_NODE_SIZE;
  }
}

function nodeLabel(node: FGNode): string {
  const n = node.__raw ?? (node as unknown as GraphNode);
  if (n.type === 'cve') {
    return `${n.label} — ${n.severity || 'UNKNOWN'}` +
      (n.cvss_score != null ? ` (CVSS ${n.cvss_score.toFixed(1)})` : '');
  }
  if (n.type === 'finding') {
    return `${n.label} — ${n.severity || 'UNKNOWN'}`;
  }
  return n.label || '';
}

function nodeThreeObject(node: FGNode): SpriteText | null {
  const n = node.__raw ?? (node as unknown as GraphNode);
  // Only label the meaningful nodes; files are too numerous and clutter 3D.
  if (n.type === 'file') return null;
  const sprite = new SpriteText(n.label || '');
  sprite.color = '#e2e8f0';
  sprite.backgroundColor = (n.type === 'cwe' || n.type === 'finding')
    ? SEVERITY_COLORS[(n.severity as Severity) || 'UNKNOWN']
    : NODE_COLORS[n.type] || '#334155';
  sprite.padding = 2;
  sprite.textHeight = 4;
  // three ships untyped in this build, so the inherited Object3D.position isn't
  // visible on the SpriteText type — cast through unknown to nudge the label
  // above the node sphere. (Runtime: SpriteText is a THREE.Sprite, has position.)
  (sprite as unknown as { position: { y: number } }).position.y = 8;
  return sprite;
}
