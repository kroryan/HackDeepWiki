// Severity -> colour palette for the vulnerability UI + 3D graph.
// Colours are raw hex so they can be fed straight to three.js materials as
// well as used in CSS.

import { Severity } from '../types';

export const SEVERITY_COLORS: Record<Severity, string> = {
  CRITICAL: '#ff3333',
  HIGH: '#ef4444',
  MEDIUM: '#f59e0b',
  LOW: '#22c55e',
  UNKNOWN: '#64748b',
};

// Obsidian Canvas colour ids (1=red,2=orange,3=yellow,4=green,5=cyan,6=purple).
// https://help.obsidian.md/canvas
export const SEVERITY_CANVAS_COLOR: Record<Severity, string> = {
  CRITICAL: '1',
  HIGH: '2',
  MEDIUM: '3',
  LOW: '4',
  UNKNOWN: '5',
};

export const NODE_COLORS: Record<string, string> = {
  package: '#3b82f6',    // blue
  cve: '#ef4444',        // red (overridden by severity)
  file: '#94a3b8',       // grey
  cwe: '#a855f7',        // purple
  fix: '#22c55e',        // green
  // Website scan graph node types (build_web_graph)
  site: '#e2e8f0',       // near-white, the graph's root node
  technology: '#3b82f6', // blue, same family as 'package'
  category: '#a855f7',   // purple, same family as 'cwe'
  finding: '#f59e0b',    // amber
};

export function severityColor(sev: Severity): string {
  return SEVERITY_COLORS[sev] ?? SEVERITY_COLORS.UNKNOWN;
}