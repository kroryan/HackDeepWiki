'use client';

import React from 'react';
import { CVEFinding } from './types';
import { severityColor } from './config/colors';

interface Props {
  finding: CVEFinding | null;
  onClose: () => void;
}

export default function VulnDetailDrawer({ finding, onClose }: Props) {
  if (!finding) return null;
  const color = severityColor(finding.severity);

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
        {/* header */}
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
              {finding.dev && (
                <span className="text-[10px] text-[var(--muted)] border border-[var(--border-color)] rounded px-1">
                  dev dependency
                </span>
              )}
            </div>
            <h3 className="mt-2 font-mono text-base text-[var(--foreground)] break-all">
              {finding.id}
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
          <Field label="Package">
            <span className="font-mono">
              {finding.package_name}@{finding.installed_version}
            </span>{' '}
            <span className="text-[var(--muted)]">({finding.package_ecosystem}, {finding.category})</span>
          </Field>

          <Field label="Fixed in">
            {finding.fixed_version ? (
              <span className="font-mono text-[var(--highlight)]">{finding.fixed_version}</span>
            ) : (
              <span className="text-[var(--muted)]">No fixed version published yet</span>
            )}
          </Field>

          {finding.aliases.length > 0 && (
            <Field label="Aliases">
              <span className="font-mono">{finding.aliases.join(', ')}</span>
            </Field>
          )}

          {finding.cwe_ids.length > 0 && (
            <Field label="CWE">
              <div className="flex flex-wrap gap-1">
                {finding.cwe_ids.map((cwe) => (
                  <span key={cwe} className="text-[11px] px-1.5 py-0.5 rounded bg-[var(--background)]/60 text-[var(--foreground)]/80 font-mono">
                    {cwe}
                  </span>
                ))}
              </div>
            </Field>
          )}

          {finding.summary && (
            <Field label="Summary">{finding.summary}</Field>
          )}

          <AiField label="📊 Impact analysis" text={finding.ai_impact_analysis} />
          <AiField label="⚔️ Exploitability" text={finding.ai_exploitability} />
          <AiField label="🛠️ Remediation" text={finding.ai_remediation} />

          {finding.ai_priority > 0 && (
            <Field label="🎯 AI priority">
              <span className="font-semibold">{finding.ai_priority}/5</span>
            </Field>
          )}

          {finding.usage_files.length > 0 && (
            <Field label="📁 Used in">
              <div className="flex flex-col gap-0.5 font-mono text-[11px] text-[var(--foreground)]/70">
                {finding.usage_files.map((f) => (
                  <span key={f}>{f}</span>
                ))}
              </div>
            </Field>
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

          {finding.published && (
            <Field label="Published">
              <span className="text-[var(--muted)] text-xs">{finding.published}</span>
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

function AiField({ label, text }: { label: string; text: string }) {
  if (!text) return null;
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wide text-[var(--muted)] mb-1">{label}</div>
      <div className="text-[var(--foreground)]/90 whitespace-pre-wrap">{text}</div>
    </div>
  );
}