// Node sizing rules for the 3D graph.

export const BASE_NODE_SIZE = 6;

/**
 * Map a CVSS score (0-10) to a node radius. Falls back to a severity-based
 * default when no numeric score is available.
 */
export function cveNodeRadius(cvss: number | null | undefined,
                              severity: string): number {
  if (cvss != null && !Number.isNaN(cvss)) {
    return 4 + (cvss / 10) * 10; // 4 .. 14
  }
  switch (severity) {
    case 'CRITICAL': return 13;
    case 'HIGH': return 11;
    case 'MEDIUM': return 9;
    case 'LOW': return 7;
    default: return 6;
  }
}

/** Package node radius grows with how many CVEs affect it (capped). */
export function packageNodeRadius(cveCount: number | null | undefined): number {
  const n = cveCount ?? 0;
  return 5 + Math.min(n, 12) * 0.6; // 5 .. ~12
}

export const FILE_NODE_SIZE = 3;
export const CWE_NODE_SIZE = 5;
export const FIX_NODE_SIZE = 4;

// Website scan graph node types (build_web_graph).
export const SITE_NODE_SIZE = 15; // the graph's single root node
export const CATEGORY_NODE_SIZE = 6;
export const TECH_NODE_SIZE = 6;