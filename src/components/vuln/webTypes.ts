// TypeScript mirrors of api/web_vuln_scanner/models.py -- the shape of the
// website security report streamed back from /ws/web_vuln_scan and stored
// in wikicache (hackdeepwiki_webvulns_*.json).

import { RemediationPlan } from './types';

export type WebSeverity = 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW' | 'INFO';
export type WebFindingCategory = 'headers' | 'cookies' | 'tls' | 'exposure' | 'cve';

export interface WebFinding {
  id: string;
  category: WebFindingCategory;
  severity: WebSeverity;
  title: string;
  description: string;
  url: string;
  evidence: string;
  remediation: string;
  references: string[];
  cve_id: string | null;
  cvss_score: number | null;
  technology: string | null;
  technology_version: string | null;
  ai_proposed: boolean;
  ai_dismissed: boolean;
  ai_dismiss_reason: string;
  ai_notes: string;
}

export interface DetectedTechnology {
  name: string;
}

export interface WebVulnReport {
  site_url: string;
  owner: string;
  repo: string;
  language: string;
  generated_at: string;
  provider: string;
  model: string;
  pages_scanned: number;
  counts: Record<WebSeverity, number>;
  total_findings: number;
  header_findings: WebFinding[];
  cookie_findings: WebFinding[];
  tls_findings: WebFinding[];
  exposure_findings: WebFinding[];
  cve_findings: WebFinding[];
  all_findings: WebFinding[];
  detected_technologies: DetectedTechnology[];
  ai_analyzed: boolean;
  remediation_plan?: RemediationPlan;
}

export const WEB_SEVERITY_ORDER: WebSeverity[] = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW', 'INFO'];
