'use client';

import React, { useMemo, useState } from 'react';
import { WebVulnReport, WebFinding } from './webTypes';
import { VulnScanStatus, ScanRelease } from './types';
import WebVulnOverview from './WebVulnOverview';
import WebFindingCard from './WebFindingCard';
import WebFindingDetailDrawer from './WebFindingDetailDrawer';
import VulnRemediationPlan from './VulnRemediationPlan';
import VulnGraph3D from './VulnGraph3D';
import ScanReleaseSelector from './ScanReleaseSelector';

interface Props {
  report: WebVulnReport | null;
  status: VulnScanStatus;
  progressMessage?: string;
  progressPercent?: number | null;
  errorMessage?: string;
  onRetry?: () => void;
  releases?: ScanRelease[];
  selectedVersion?: number | null;
  onSelectVersion?: (version: number) => void;
  onDeleteVersion?: (version: number) => void;
}

type Tab = 'headers' | 'cookies' | 'tls' | 'exposure' | 'cve' | 'graph' | 'solutions';

export default function WebVulnSection({
  report, status, progressMessage, progressPercent, errorMessage, onRetry,
  releases = [], selectedVersion = null, onSelectVersion, onDeleteVersion,
}: Props) {
  const [tab, setTab] = useState<Tab>('exposure');
  const [selected, setSelected] = useState<WebFinding | null>(null);

  const enabledTabs = useMemo<Tab[]>(() => {
    if (!report) return ['headers', 'cookies', 'tls', 'exposure', 'cve', 'graph'];
    const t: Tab[] = [];
    if (report.header_findings.length) t.push('headers');
    if (report.cookie_findings.length) t.push('cookies');
    if (report.tls_findings.length) t.push('tls');
    if (report.exposure_findings.length) t.push('exposure');
    if (report.cve_findings.length) t.push('cve');
    t.push('graph');
    if (report.remediation_plan?.steps?.length) t.push('solutions');
    return t.length ? t : ['exposure'];
  }, [report]);

  const activeTab: Tab = enabledTabs.includes(tab) ? tab : enabledTabs[0];

  if (status === 'running' && !report) {
    return <ScanProgressView message={progressMessage} percent={progressPercent} />;
  }

  if (status === 'error' && !report) {
    return (
      <div className="p-6 rounded-md border border-[var(--border-color)] bg-[var(--card-bg)]">
        <h3 className="text-base font-semibold text-[var(--highlight)] mb-2">
          🌐 Website security scan failed
        </h3>
        <p className="text-sm text-[var(--foreground)]/80 mb-4 break-words">
          {errorMessage || 'Unknown error.'}
        </p>
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="px-4 py-2 text-sm rounded-md bg-[var(--accent-primary)] text-white hover:opacity-90"
          >
            Retry scan
          </button>
        )}
      </div>
    );
  }

  if (!report) {
    return (
      <div className="p-6 rounded-md border border-[var(--border-color)] bg-[var(--card-bg)]">
        <h3 className="text-base font-semibold text-[var(--foreground)] mb-2">
          🌐 No website scan yet
        </h3>
        <p className="text-sm text-[var(--foreground)]/80 mb-4">
          Run a scan to check this site for header/cookie/TLS misconfigurations, exposed paths, and known CVEs.
        </p>
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="px-4 py-2 text-sm rounded-md bg-[var(--accent-primary)] text-white hover:opacity-90"
          >
            Run scan
          </button>
        )}
      </div>
    );
  }

  const findings: WebFinding[] =
    activeTab === 'headers' ? report.header_findings
    : activeTab === 'cookies' ? report.cookie_findings
    : activeTab === 'tls' ? report.tls_findings
    : activeTab === 'exposure' ? report.exposure_findings
    : activeTab === 'cve' ? report.cve_findings
    : [];

  return (
    <div className="space-y-4">
      {onSelectVersion && onDeleteVersion && (
        <ScanReleaseSelector
          releases={releases}
          selectedVersion={selectedVersion}
          onSelectVersion={onSelectVersion}
          onDeleteVersion={onDeleteVersion}
          disabled={status === 'running'}
        />
      )}
      <WebVulnOverview report={report} />

      <div className="flex flex-wrap gap-1 border-b border-[var(--border-color)]">
        {enabledTabs.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={`px-3 py-1.5 text-sm rounded-t-md border-b-2 transition-colors ${
              activeTab === t
                ? 'border-[var(--accent-primary)] text-[var(--accent-primary)]'
                : 'border-transparent text-[var(--muted)] hover:text-[var(--foreground)]'
            }`}
          >
            {tabLabel(t, report)}
          </button>
        ))}
      </div>

      {activeTab === 'graph' ? (
        <VulnGraph3D
          graph={report.graph}
          onNodeClick={(node) => {
            if (node.type !== 'cve' && node.type !== 'finding') return;
            const rawId = node.id.replace(/^(cve|finding):/, '');
            const f = report.all_findings.find((x) => x.id === rawId) || null;
            setSelected(f);
          }}
        />
      ) : activeTab === 'solutions' ? (
        <VulnRemediationPlan plan={report.remediation_plan} />
      ) : findings.length === 0 ? (
        <div className="p-6 rounded-md border border-[var(--border-color)] bg-[var(--card-bg)] text-sm text-[var(--muted)]">
          No findings in this category. 🎉
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {findings.map((f) => (
            <WebFindingCard key={f.id} finding={f} onClick={setSelected} />
          ))}
        </div>
      )}

      <WebFindingDetailDrawer finding={selected} onClose={() => setSelected(null)} />
    </div>
  );
}

function tabLabel(t: Tab, report: WebVulnReport): string {
  switch (t) {
    case 'headers': return `📋 Headers (${report.header_findings.length})`;
    case 'cookies': return `🍪 Cookies (${report.cookie_findings.length})`;
    case 'tls': return `🔒 TLS (${report.tls_findings.length})`;
    case 'exposure': return `🔓 Exposure (${report.exposure_findings.length})`;
    case 'cve': return `🐛 CVEs (${report.cve_findings.length})`;
    case 'graph': return `🕸️ Graph`;
    case 'solutions': return `🛠️ Solutions`;
  }
}

function ScanProgressView({ message, percent }: { message?: string; percent?: number | null }) {
  return (
    <div className="p-6 rounded-md border border-[var(--border-color)] bg-[var(--card-bg)]">
      <h3 className="text-base font-semibold text-[var(--accent-primary)] mb-3">
        🌐 Scanning website for vulnerabilities…
      </h3>
      <div className="w-full h-2 rounded-full bg-[var(--background)] overflow-hidden mb-3">
        <div
          className="h-full bg-[var(--accent-primary)] transition-all"
          style={{ width: `${Math.max(0, Math.min(100, percent ?? 0))}%` }}
        />
      </div>
      <p className="text-sm text-[var(--foreground)]/80">{message || 'Working…'}</p>
      {percent != null && (
        <p className="text-xs text-[var(--muted)] mt-1">{percent}%</p>
      )}
    </div>
  );
}
