'use client';

import React, { useEffect, useState } from 'react';
import UserSelector from '@/components/UserSelector';

export interface RescanSelection {
  provider: string;
  model: string;
  isCustomModel: boolean;
  customModel: string;
  // dependency scan only
  vulnClient?: boolean;
  vulnServer?: boolean;
  vulnDeps?: boolean;
  nvdKey?: string;
  // website scan only
  enableDeepScan?: boolean;
}

interface Props {
  isOpen: boolean;
  onClose: () => void;
  variant: 'dependency' | 'website';
  provider: string;
  model: string;
  isCustomModel: boolean;
  customModel: string;
  vulnClient?: boolean;
  vulnServer?: boolean;
  vulnDeps?: boolean;
  nvdKey?: string;
  enableDeepScan?: boolean;
  onSubmit: (selection: RescanSelection) => void;
}

/**
 * Floating "pick a provider/model (and scan options) before running" modal
 * for the Security Analysis / Website Security "Rerun scan" buttons --
 * mirrors ModelSelectionModal's role for "Refresh Wiki", but scoped to just
 * what a rescan needs (no wiki-type/file-filter/token fields).
 */
export default function RescanConfigModal({
  isOpen, onClose, variant,
  provider, model, isCustomModel, customModel,
  vulnClient = true, vulnServer = true, vulnDeps = true, nvdKey = '',
  enableDeepScan = false,
  onSubmit,
}: Props) {
  const [localProvider, setLocalProvider] = useState(provider);
  const [localModel, setLocalModel] = useState(model);
  const [localIsCustomModel, setLocalIsCustomModel] = useState(isCustomModel);
  const [localCustomModel, setLocalCustomModel] = useState(customModel);
  const [localVulnClient, setLocalVulnClient] = useState(vulnClient);
  const [localVulnServer, setLocalVulnServer] = useState(vulnServer);
  const [localVulnDeps, setLocalVulnDeps] = useState(vulnDeps);
  const [localNvdKey, setLocalNvdKey] = useState(nvdKey);
  const [localDeepScan, setLocalDeepScan] = useState(enableDeepScan);

  useEffect(() => {
    if (isOpen) {
      setLocalProvider(provider);
      setLocalModel(model);
      setLocalIsCustomModel(isCustomModel);
      setLocalCustomModel(customModel);
      setLocalVulnClient(vulnClient);
      setLocalVulnServer(vulnServer);
      setLocalVulnDeps(vulnDeps);
      setLocalNvdKey(nvdKey);
      setLocalDeepScan(enableDeepScan);
    }
  }, [isOpen, provider, model, isCustomModel, customModel, vulnClient, vulnServer, vulnDeps, nvdKey, enableDeepScan]);

  if (!isOpen) return null;

  const handleSubmit = () => {
    onSubmit({
      provider: localProvider,
      model: localIsCustomModel ? localCustomModel : localModel,
      isCustomModel: localIsCustomModel,
      customModel: localCustomModel,
      ...(variant === 'dependency'
        ? { vulnClient: localVulnClient, vulnServer: localVulnServer, vulnDeps: localVulnDeps, nvdKey: localNvdKey }
        : { enableDeepScan: localDeepScan }),
    });
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto">
      <div className="flex min-h-screen items-center justify-center p-4 text-center bg-black/50">
        <div className="relative transform overflow-hidden rounded-lg bg-[var(--card-bg)] text-left shadow-xl transition-all sm:my-8 sm:max-w-lg sm:w-full">
          <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border-color)]">
            <h3 className="text-lg font-medium text-[var(--accent-primary)]">
              {variant === 'dependency' ? '🔐 Security Analysis scan' : '🌐 Website Security scan'}
            </h3>
            <button
              type="button"
              onClick={onClose}
              className="text-[var(--muted)] hover:text-[var(--foreground)] focus:outline-none transition-colors"
            >
              <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          <div className="p-6">
            <p className="text-xs text-[var(--muted)] mb-3">
              Choose which model runs the AI cross-check for this scan
              {variant === 'website' ? ', and whether to include the Docker-based deep scan.' : '.'}
            </p>
            <UserSelector
              provider={localProvider}
              setProvider={setLocalProvider}
              model={localModel}
              setModel={setLocalModel}
              isCustomModel={localIsCustomModel}
              setIsCustomModel={setLocalIsCustomModel}
              customModel={localCustomModel}
              setCustomModel={setLocalCustomModel}
            />

            <div className="my-4 border-t border-[var(--border-color)]/30" />

            {variant === 'dependency' ? (
              <div className="space-y-2">
                <p className="text-xs text-[var(--muted)] mb-1">Categories to include:</p>
                <label className="flex items-center gap-2 text-sm text-[var(--foreground)] cursor-pointer">
                  <input
                    type="checkbox"
                    checked={localVulnClient}
                    onChange={(e) => setLocalVulnClient(e.target.checked)}
                    className="h-4 w-4 rounded border-[var(--border-color)] accent-[var(--accent-primary)]"
                  />
                  Client-side vulnerabilities
                </label>
                <label className="flex items-center gap-2 text-sm text-[var(--foreground)] cursor-pointer">
                  <input
                    type="checkbox"
                    checked={localVulnServer}
                    onChange={(e) => setLocalVulnServer(e.target.checked)}
                    className="h-4 w-4 rounded border-[var(--border-color)] accent-[var(--accent-primary)]"
                  />
                  Server-side vulnerabilities
                </label>
                <label className="flex items-center gap-2 text-sm text-[var(--foreground)] cursor-pointer">
                  <input
                    type="checkbox"
                    checked={localVulnDeps}
                    onChange={(e) => setLocalVulnDeps(e.target.checked)}
                    className="h-4 w-4 rounded border-[var(--border-color)] accent-[var(--accent-primary)]"
                  />
                  Dependency vulnerabilities
                </label>
                <div className="pt-2">
                  <label htmlFor="rescan-nvd-key" className="block text-xs font-medium text-[var(--muted)] mb-1">
                    🔑 Optional NVD API key (free at nvd.nist.gov — adds CVSS scores)
                  </label>
                  <input
                    type="password"
                    id="rescan-nvd-key"
                    value={localNvdKey}
                    onChange={(e) => setLocalNvdKey(e.target.value)}
                    className="input-japanese block w-full px-3 py-2 text-sm rounded-md bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
                    placeholder="leave empty to use OSV.dev only"
                    autoComplete="off"
                  />
                </div>
              </div>
            ) : (
              <label className="flex items-start gap-2 text-sm text-[var(--foreground)] cursor-pointer">
                <input
                  type="checkbox"
                  checked={localDeepScan}
                  onChange={(e) => setLocalDeepScan(e.target.checked)}
                  className="h-4 w-4 rounded border-[var(--border-color)] accent-[var(--accent-primary)] mt-0.5"
                />
                <span>
                  <span className="text-[var(--accent-primary)]">🐳 Deep security scan (Docker)</span>
                  <span className="block text-xs text-[var(--muted)] font-normal">
                    Adds nmap/nikto/httpx/whatweb/testssl/nuclei/subfinder/ffuf/dalfox/wpscan on top of the
                    always-on header/cookie/TLS/exposed-path checks. Requires Docker; downloads a multi-GB
                    scan toolkit image the first time it runs. Recommended for a thorough report.
                  </span>
                </span>
              </label>
            )}
          </div>

          <div className="flex items-center justify-end gap-2 px-6 py-4 border-t border-[var(--border-color)]">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm font-medium rounded-md border border-[var(--border-color)]/50 text-[var(--muted)] bg-transparent hover:bg-[var(--background)] hover:text-[var(--foreground)] transition-colors"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleSubmit}
              className="px-4 py-2 text-sm font-medium rounded-md border border-transparent bg-[var(--accent-primary)]/90 text-white hover:bg-[var(--accent-primary)] transition-colors"
            >
              Run scan
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
