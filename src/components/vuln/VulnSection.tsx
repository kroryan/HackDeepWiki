'use client';

import React, { useMemo, useState } from 'react';
import {
  VulnReport,
  CVEFinding,
  VulnScanStatus,
} from './types';
import VulnOverview from './VulnOverview';
import VulnFindingCard from './VulnFindingCard';
import VulnDetailDrawer from './VulnDetailDrawer';
import VulnGraph3D from './VulnGraph3D';
import VulnRemediationPlan from './VulnRemediationPlan';

interface Props {
  report: VulnReport | null;
  status: VulnScanStatus;
  progressMessage?: string;
  progressPercent?: number | null;
  errorMessage?: string;
  onRetry?: () => void;
}

type Tab = 'client' | 'server' | 'dependencies' | 'graph' | 'solutions';

export default function VulnSection({
  report, status, progressMessage, progressPercent, errorMessage, onRetry,
}: Props) {
  const [tab, setTab] = useState<Tab>('client');
  const [selected, setSelected] = useState<CVEFinding | null>(null);

  const enabledTabs = useMemo<Tab[]>(() => {
    if (!report) return ['client', 'server', 'dependencies', 'graph'];
    const t: Tab[] = [];
    if (report.client_findings.length) t.push('client');
    if (report.server_findings.length) t.push('server');
    if (report.dependency_findings.length) t.push('dependencies');
    t.push('graph');
    if (report.remediation_plan?.steps?.length) t.push('solutions');
    return t;
  }, [report]);

  // keep tab valid
  const activeTab: Tab = enabledTabs.includes(tab) ? tab : enabledTabs[0];

  if (status === 'running' && !report) {
    return (
      <ScanProgressView message={progressMessage} percent={progressPercent} />
    );
  }

  if (status === 'error' && !report) {
    return (
      <div className="p-6 rounded-md border border-[var(--border-color)] bg-[var(--card-bg)]">
        <h3 className="text-base font-semibold text-[var(--highlight)] mb-2">
          🔐 Security scan failed
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
          🔐 No vulnerability scan yet
        </h3>
        <p className="text-sm text-[var(--foreground)]/80 mb-4">
          Run a scan to check this repo&apos;s dependencies against known CVEs (OSV.dev).
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

  const findings: CVEFinding[] =
    activeTab === 'client' ? report.client_findings
    : activeTab === 'server' ? report.server_findings
    : activeTab === 'dependencies' ? report.dependency_findings
    : [];

  return (
    <div className="space-y-4">
      <VulnOverview report={report} />

      {/* tabs */}
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
            // open the drawer with the matching finding when a CVE node is clicked
            if (node.type !== 'cve') return;
            const f = report.all_findings.find((x) => x.id === node.label) || null;
            setSelected(f);
          }}
        />
      ) : activeTab === 'solutions' ? (
        <VulnRemediationPlan plan={report.remediation_plan} />
      ) : findings.length === 0 ? (
        <div className="p-6 rounded-md border border-[var(--border-color)] bg-[var(--card-bg)] text-sm text-[var(--muted)]">
          No vulnerabilities in this category. 🎉
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {findings.map((f) => (
            <VulnFindingCard key={`${f.id}-${f.package_name}`} finding={f} onClick={setSelected} />
          ))}
        </div>
      )}

      <VulnDetailDrawer finding={selected} onClose={() => setSelected(null)} />
    </div>
  );
}

function tabLabel(t: Tab, report: VulnReport): string {
  switch (t) {
    case 'client': return `🖥️ Client (${report.client_findings.length})`;
    case 'server': return `🔒 Server (${report.server_findings.length})`;
    case 'dependencies': return `📦 Dependencies (${report.dependency_findings.length})`;
    case 'graph': return `🕸️ Graph`;
    case 'solutions': return `🛠️ Solutions`;
  }
}

function ScanProgressView({ message, percent }: { message?: string; percent?: number | null }) {
  return (
    <div className="p-6 rounded-md border border-[var(--border-color)] bg-[var(--card-bg)]">
      <h3 className="text-base font-semibold text-[var(--accent-primary)] mb-3">
        🔐 Scanning for vulnerabilities…
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