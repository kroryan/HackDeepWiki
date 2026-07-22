'use client';

import React from 'react';
import { WebFinding } from './webTypes';
import { SEVERITY_COLORS } from './config/colors';

const INFO_COLOR = '#64748b';

function webSeverityColor(sev: string): string {
  return (SEVERITY_COLORS as Record<string, string>)[sev] ?? INFO_COLOR;
}

interface Props {
  finding: WebFinding | null;
  onClose: () => void;
}

export default function WebFindingDetailDrawer({ finding, onClose }: Props) {
  if (!finding) return null;
  const color = webSeverityColor(finding.severity);

  return (
    <div
      // z-[110]: above the graph's fullscreen overlay (z-[100] in VulnGraph3D)
      // -- clicking a node while the graph is maximized must still open the
      // drawer on top of it, not behind it.
      className="fixed inset-0 z-[110] flex justify-end bg-black/40"
      onClick={onClose}
    >
      <div
        className="h-full w-full max-w-md overflow-y-auto bg-[var(--card-bg)] border-l border-[var(--border-color)] shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div
          className="sticky top-0 z-10 flex items-start justify-between p-4 border-b border-[var(--border-color)] bg-[var(--card-bg)]"
          style={{ borderBottomColor: color, borderBottomWidth: 3 }}
        >
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span
                className="inline-flex items-center text-[10px] font-bold uppercase px-1.5 py-0.5 rounded text-white"
                style={{ backgroundColor: color }}
              >
                {finding.severity}
              </span>
              {finding.cvss_score != null && (
                <span className="text-xs text-[var(--muted)]">
                  CVSS {finding.cvss_score.toFixed(1)}
                </span>
              )}
              {finding.ai_proposed && (
                <span className="text-[10px] text-[var(--accent-primary)] border border-[var(--accent-primary)]/40 rounded px-1">
                  AI-proposed
                </span>
              )}
              {finding.ai_dismissed && (
                <span className="text-[10px] text-[var(--muted)] border border-[var(--border-color)] rounded px-1">
                  likely false positive
                </span>
              )}
            </div>
            <h3 className="mt-2 font-mono text-base text-[var(--foreground)] break-all">
              {finding.title}
            </h3>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="text-[var(--muted)] hover:text-[var(--foreground)] ml-2 shrink-0"
            aria-label="Close"
          >
            <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="p-4 space-y-4 text-sm">
          <Field label="Category">
            <span className="font-mono">{finding.category}</span>
          </Field>

          {finding.url && (
            <Field label="URL">
              <span className="break-all text-xs">{finding.url}</span>
            </Field>
          )}

          {finding.description && (
            <Field label="Description">{finding.description}</Field>
          )}

          {finding.evidence && (
            <Field label="Evidence">
              <pre className="whitespace-pre-wrap break-all text-xs font-mono text-[var(--foreground)]/80 bg-[var(--background)]/50 p-2 rounded">
                {finding.evidence}
              </pre>
            </Field>
          )}

          {finding.cve_id && (
            <Field label="CVE">
              <span className="font-mono text-[var(--highlight)]">{finding.cve_id}</span>
            </Field>
          )}

          {finding.technology && (
            <Field label="Technology">
              <span className="font-mono">
                {finding.technology}{finding.technology_version ? `@${finding.technology_version}` : ''}
              </span>
            </Field>
          )}

          {finding.remediation && (
            <Field label="🛠️ Remediation">{finding.remediation}</Field>
          )}

          {finding.ai_dismissed && finding.ai_dismiss_reason && (
            <Field label="🤖 AI dismissal reason">
              <span className="text-[var(--muted)]">{finding.ai_dismiss_reason}</span>
            </Field>
          )}

          {finding.ai_notes && (
            <Field label="🤖 AI notes">{finding.ai_notes}</Field>
          )}

          {finding.references.length > 0 && (
            <Field label="References">
              <div className="flex flex-col gap-1">
                {finding.references.map((r) => (
                  <a
                    key={r}
                    href={r}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-[var(--link-color)] hover:underline break-all text-xs"
                  >
                    {r}
                  </a>
                ))}
              </div>
            </Field>
          )}
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wide text-[var(--muted)] mb-1">{label}</div>
      <div className="text-[var(--foreground)]/90">{children}</div>
    </div>
  );
}
