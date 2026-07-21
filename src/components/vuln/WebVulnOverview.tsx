'use client';

import React from 'react';
import { WebVulnReport, WEB_SEVERITY_ORDER } from './webTypes';
import { SEVERITY_COLORS } from './config/colors';

const INFO_COLOR = '#64748b';

function webSeverityColor(sev: string): string {
  return (SEVERITY_COLORS as Record<string, string>)[sev] ?? INFO_COLOR;
}

interface Props {
  report: WebVulnReport;
}

export default function WebVulnOverview({ report }: Props) {
  return (
    <div className="mb-4">
      <h3 className="text-base font-semibold text-[var(--foreground)] mb-3">
        🌐 Website Security Overview
      </h3>
      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-2">
        {WEB_SEVERITY_ORDER.map((sev) => (
          <div
            key={sev}
            className="rounded-md border bg-[var(--card-bg)] p-3 text-center"
            style={{ borderColor: 'var(--border-color)' }}
          >
            <div className="text-2xl font-bold" style={{ color: webSeverityColor(sev) }}>
              {report.counts[sev] ?? 0}
            </div>
            <div className="text-[10px] uppercase tracking-wide text-[var(--muted)] mt-0.5">
              {sev}
            </div>
          </div>
        ))}
      </div>
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-[var(--muted)]">
        <span>Total findings: <strong className="text-[var(--foreground)]">{report.total_findings}</strong></span>
        <span>Pages scanned: <strong className="text-[var(--foreground)]">{report.pages_scanned}</strong></span>
        <span>AI cross-check: <strong className="text-[var(--foreground)]">{report.ai_analyzed ? 'yes' : 'no'}</strong></span>
        <span>
          Deep scan (Docker): <strong className="text-[var(--foreground)]">{report.deep_scan_ran ? 'yes' : 'no'}</strong>
        </span>
        {report.detected_technologies.length > 0 && (
          <span>Technologies: <strong className="text-[var(--foreground)]">{report.detected_technologies.map((t) => t.name).join(', ')}</strong></span>
        )}
        {report.generated_at && (
          <span>Generated: <strong className="text-[var(--foreground)]">{new Date(report.generated_at).toLocaleString()}</strong></span>
        )}
      </div>
      {!report.deep_scan_ran && (
        <div className="mt-2 text-xs text-[var(--muted)] bg-[var(--background)]/50 border border-[var(--border-color)] rounded-md px-3 py-2">
          This report only includes the always-on header/cookie/TLS/exposed-path checks.
          For a full professional-tool pass (nmap, nikto, whatweb, testssl, nuclei, subfinder,
          ffuf, dalfox, wpscan), use &quot;Rerun scan&quot; and enable the Docker deep scan.
        </div>
      )}
    </div>
  );
}
