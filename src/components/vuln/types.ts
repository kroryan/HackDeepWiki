// TypeScript mirrors of api/vuln_scanner/models.py — the shape of the
// vulnerability report stored in wikicache and streamed back from /ws/vuln_scan.

export type Severity = 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW' | 'UNKNOWN';
export type VulnCategory = 'client' | 'server' | 'dependency';

export interface CVEFinding {
  id: string;
  aliases: string[];
  package_name: string;
  package_ecosystem: string;
  installed_version: string;
  fixed_version: string | null;
  severity: Severity;
  cvss_score: number | null;
  summary: string;
  details: string;
  references: string[];
  published: string;
  cwe_ids: string[];
  category: VulnCategory;
  dev: boolean;
  source_files: string[];
  usage_files: string[];
  // LLM-generated
  ai_impact_analysis: string;
  ai_exploitability: string;
  ai_remediation: string;
  ai_priority: number; // 1-5
}

export interface ScannedDependency {
  name: string;
  version: string;
  ecosystem: string;
  category: VulnCategory;
  dev: boolean;
  source_files: string[];
  usage_files: string[];
}

// 'package' | 'file' | 'cwe' | 'fix' come from the dependency scan graph;
// 'site' | 'technology' | 'category' | 'finding' come from the website scan
// graph (api.web_vuln_scanner.models.build_web_graph) -- both share this
// type so VulnGraph3D/VulnGraph2D work unmodified for either report.
export type GraphNodeType =
  | 'package' | 'cve' | 'file' | 'cwe' | 'fix'
  | 'site' | 'technology' | 'category' | 'finding';

export interface GraphNode {
  id: string;
  type: GraphNodeType;
  label: string;
  severity?: Severity | null;
  cvss_score?: number | null;
  cve_count?: number | null;
  group?: VulnCategory | null;
}

export interface GraphLink {
  source: string;
  target: string;
  label: string; // AFFECTED_BY | CATEGORIZED_AS | USES | FIXED_IN
}

export interface GraphData {
  nodes: GraphNode[];
  links: GraphLink[];
}

// Mirrors api/vuln_common/remediation.py -- one consolidated, prioritized
// "Suggested Solutions" page shared by every scan type (dependency/web/code).
export interface RemediationStep {
  action: string;
  severity: Severity | 'INFO';
  finding_ids: string[];
  finding_titles: string[];
  category: string;
  affected_count: number;
}

export interface RemediationPlan {
  steps: RemediationStep[];
  summary: string;
  total_findings_covered: number;
}

export interface VulnReport {
  repo_url: string;
  repo_type: string;
  owner: string;
  repo: string;
  language: string;
  generated_at: string;
  provider: string;
  model: string;
  counts: Record<Severity, number>;
  total_findings: number;
  total_dependencies_scanned: number;
  client_findings: CVEFinding[];
  server_findings: CVEFinding[];
  dependency_findings: CVEFinding[];
  all_findings: CVEFinding[];
  scanned_dependencies: ScannedDependency[];
  graph: GraphData;
  ai_analyzed: boolean;
  code_scan_findings?: CVEFinding[] | Record<string, unknown>[];
  code_scan_ran?: boolean;
  remediation_plan?: RemediationPlan;
}

export type VulnScanStatus = 'idle' | 'running' | 'done' | 'error';

export const SEVERITY_ORDER: Severity[] = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'UNKNOWN'];

// One saved release (version) of a dependency or website security scan,
// returned by /api/vuln_cache/releases or /api/web_vuln_cache/releases --
// same versioning scheme as the wiki's release history.
export interface ScanRelease {
  version: number;
  created_at: number;
  total_findings: number | null;
  generated_at: string | null;
  id: string;
}