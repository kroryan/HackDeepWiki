'use client';

import React, {useEffect, useState} from 'react';
import {useLanguage} from '@/contexts/LanguageContext';
import UserSelector from './UserSelector';
import WikiTypeSelector from './WikiTypeSelector';
import TokenInput from './TokenInput';

export interface AppliedModelSelection {
  provider: string;
  model: string;
  isCustomModel: boolean;
  customModel: string;
  isComprehensiveView: boolean;
  pageCount?: number;
  excludedDirs: string;
  excludedFiles: string;
  includedDirs: string;
  includedFiles: string;
  // 🔐 Security Analysis (optional — only present when the modal is used for
  // a wiki refresh/generation that offers the vulnerability scan toggle).
  enableVulnScan?: boolean;
  vulnClient?: boolean;
  vulnServer?: boolean;
  vulnDeps?: boolean;
  nvdKey?: string;
  includeVulnsInObsidian?: boolean;
}

interface ModelSelectionModalProps {
  isOpen: boolean;
  onClose: () => void;
  provider: string;
  setProvider: (value: string) => void;
  model: string;
  setModel: (value: string) => void;
  isCustomModel: boolean;
  setIsCustomModel: (value: boolean) => void;
  customModel: string;
  setCustomModel: (value: string) => void;
  onApply: (token?: string, selection?: AppliedModelSelection) => void;

  // Wiki type options
  isComprehensiveView: boolean;
  setIsComprehensiveView: (value: boolean) => void;
  pageCount?: number;
  setPageCount?: (value: number) => void;

  // File filter options - optional
  excludedDirs?: string;
  setExcludedDirs?: (value: string) => void;
  excludedFiles?: string;
  setExcludedFiles?: (value: string) => void;
  includedDirs?: string;
  setIncludedDirs?: (value: string) => void;
  includedFiles?: string;
  setIncludedFiles?: (value: string) => void;
  showFileFilters?: boolean;
  showWikiType: boolean;

  // Token input for refresh
  showTokenInput?: boolean;
  repositoryType?: 'github' | 'gitlab' | 'bitbucket';
  // Authentication
  authRequired?: boolean;
  authCode?: string;
  setAuthCode?: (code: string) => void;
  isAuthLoading?: boolean;

  // 🔐 Security Analysis (vulnerability scan) — shown when showVulnScan is true
  // (e.g. the Refresh Wiki modal). Mirrors the block in ConfigurationModal so the
  // user can opt into a scan when updating the wiki, just like when generating it.
  showVulnScan?: boolean;
  enableVulnScan?: boolean;
  vulnClient?: boolean;
  vulnServer?: boolean;
  vulnDeps?: boolean;
  nvdKey?: string;
  includeVulnsInObsidian?: boolean;
}

export default function ModelSelectionModal({
  isOpen,
  onClose,
  provider,
  setProvider,
  model,
  setModel,
  isCustomModel,
  setIsCustomModel,
  customModel,
  setCustomModel,
  onApply,
  isComprehensiveView,
  setIsComprehensiveView,
  pageCount,
  setPageCount,
  excludedDirs = '',
  setExcludedDirs,
  excludedFiles = '',
  setExcludedFiles,
  includedDirs = '',
  setIncludedDirs,
  includedFiles = '',
  setIncludedFiles,
  showFileFilters = false,
  authRequired = false,
  authCode = '',
  setAuthCode,
  isAuthLoading,
  showWikiType = true,
  showTokenInput = false,
  repositoryType = 'github',
  showVulnScan = false,
  enableVulnScan = false,
  vulnClient = true,
  vulnServer = true,
  vulnDeps = true,
  nvdKey = '',
  includeVulnsInObsidian = true,
}: ModelSelectionModalProps) {
  const { messages: t } = useLanguage();

  // Local state for form values (to only apply changes when the user clicks "Submit")
  const [localProvider, setLocalProvider] = useState(provider);
  const [localModel, setLocalModel] = useState(model);
  const [localIsCustomModel, setLocalIsCustomModel] = useState(isCustomModel);
  const [localCustomModel, setLocalCustomModel] = useState(customModel);
  const [localIsComprehensiveView, setLocalIsComprehensiveView] = useState(isComprehensiveView);
  const [localPageCount, setLocalPageCount] = useState(pageCount);
  const [localExcludedDirs, setLocalExcludedDirs] = useState(excludedDirs);
  const [localExcludedFiles, setLocalExcludedFiles] = useState(excludedFiles);
  const [localIncludedDirs, setLocalIncludedDirs] = useState(includedDirs);
  const [localIncludedFiles, setLocalIncludedFiles] = useState(includedFiles);

  // 🔐 Security Analysis local state
  const [localEnableVulnScan, setLocalEnableVulnScan] = useState(enableVulnScan);
  const [localVulnClient, setLocalVulnClient] = useState(vulnClient);
  const [localVulnServer, setLocalVulnServer] = useState(vulnServer);
  const [localVulnDeps, setLocalVulnDeps] = useState(vulnDeps);
  const [localNvdKey, setLocalNvdKey] = useState(nvdKey);
  const [localIncludeVulnsInObsidian, setLocalIncludeVulnsInObsidian] = useState(includeVulnsInObsidian);
  
  // Token input state
  const [localAccessToken, setLocalAccessToken] = useState('');
  const [localSelectedPlatform, setLocalSelectedPlatform] = useState<'github' | 'gitlab' | 'bitbucket'>(repositoryType);
  const [showTokenSection, setShowTokenSection] = useState(showTokenInput);

  // Reset local state when modal is opened
  useEffect(() => {
    if (isOpen) {
      setLocalProvider(provider);
      setLocalModel(model);
      setLocalIsCustomModel(isCustomModel);
      setLocalCustomModel(customModel);
      setLocalIsComprehensiveView(isComprehensiveView);
      setLocalPageCount(pageCount);
      setLocalExcludedDirs(excludedDirs);
      setLocalExcludedFiles(excludedFiles);
      setLocalIncludedDirs(includedDirs);
      setLocalIncludedFiles(includedFiles);
      setLocalSelectedPlatform(repositoryType);
      setLocalAccessToken('');
      setShowTokenSection(showTokenInput);
      setLocalEnableVulnScan(enableVulnScan);
      setLocalVulnClient(vulnClient);
      setLocalVulnServer(vulnServer);
      setLocalVulnDeps(vulnDeps);
      setLocalNvdKey(nvdKey);
      setLocalIncludeVulnsInObsidian(includeVulnsInObsidian);
    }
  }, [isOpen, provider, model, isCustomModel, customModel, isComprehensiveView, pageCount, excludedDirs, excludedFiles, includedDirs, includedFiles, repositoryType, showTokenInput, enableVulnScan, vulnClient, vulnServer, vulnDeps, nvdKey, includeVulnsInObsidian]);

  // Handler for applying changes
  const handleApply = () => {
    setProvider(localProvider);
    setModel(localModel);
    setIsCustomModel(localIsCustomModel);
    setCustomModel(localCustomModel);
    setIsComprehensiveView(localIsComprehensiveView);
    if (localPageCount !== undefined) setPageCount?.(localPageCount);
    if (setExcludedDirs) setExcludedDirs(localExcludedDirs);
    if (setExcludedFiles) setExcludedFiles(localExcludedFiles);
    if (setIncludedDirs) setIncludedDirs(localIncludedDirs);
    if (setIncludedFiles) setIncludedFiles(localIncludedFiles);
    
    // React state updates are asynchronous. Pass the exact submitted values so
    // refresh operations never use the values from the previous render.
    onApply(showTokenInput ? localAccessToken : undefined, {
      provider: localProvider,
      model: localModel,
      isCustomModel: localIsCustomModel,
      customModel: localCustomModel,
      isComprehensiveView: localIsComprehensiveView,
      pageCount: localPageCount,
      excludedDirs: localExcludedDirs,
      excludedFiles: localExcludedFiles,
      includedDirs: localIncludedDirs,
      includedFiles: localIncludedFiles,
      // 🔐 Security Analysis — only forwarded when the modal shows the toggle,
      // so the chat model-selection usage (which doesn't pass showVulnScan)
      // never receives stale vuln fields.
      ...(showVulnScan ? {
        enableVulnScan: localEnableVulnScan,
        vulnClient: localVulnClient,
        vulnServer: localVulnServer,
        vulnDeps: localVulnDeps,
        nvdKey: localNvdKey,
        includeVulnsInObsidian: localIncludeVulnsInObsidian,
      } : {}),
    });
    onClose();
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto">
      <div className="flex min-h-screen items-center justify-center p-4 text-center bg-black/50">
        <div className="relative transform overflow-hidden rounded-lg bg-[var(--card-bg)] text-left shadow-xl transition-all sm:my-8 sm:max-w-lg sm:w-full">
          {/* Modal header with close button */}
          <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border-color)]">
            <h3 className="text-lg font-medium text-[var(--accent-primary)]">
              <span className="text-[var(--accent-primary)]">{t.form?.modelSelection || 'Model Selection'}</span>
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

          {/* Modal body */}
          <div className="p-6">
            {/* Wiki Type Selector */}
            {
              showWikiType && <WikiTypeSelector
                    isComprehensiveView={localIsComprehensiveView}
                    setIsComprehensiveView={setLocalIsComprehensiveView}
                    pageCount={localPageCount}
                    setPageCount={setLocalPageCount}
                />
            }

            {/* Divider */}
            <div className="my-4 border-t border-[var(--border-color)]/30"></div>

            {/* Model Selector */}
            <UserSelector
              provider={localProvider}
              setProvider={setLocalProvider}
              model={localModel}
              setModel={setLocalModel}
              isCustomModel={localIsCustomModel}
              setIsCustomModel={setLocalIsCustomModel}
              customModel={localCustomModel}
              setCustomModel={setLocalCustomModel}
              showFileFilters={showFileFilters}
              excludedDirs={localExcludedDirs}
              setExcludedDirs={showFileFilters ? (value: string) => setLocalExcludedDirs(value) : undefined}
              excludedFiles={localExcludedFiles}
              setExcludedFiles={showFileFilters ? (value: string) => setLocalExcludedFiles(value) : undefined}
              includedDirs={localIncludedDirs}
              setIncludedDirs={showFileFilters ? (value: string) => setLocalIncludedDirs(value) : undefined}
              includedFiles={localIncludedFiles}
              setIncludedFiles={showFileFilters ? (value: string) => setLocalIncludedFiles(value) : undefined}
            />

            {/* Token Input Section for refresh */}
            {showTokenInput && (
              <>
                <div className="my-4 border-t border-[var(--border-color)]/30"></div>
                <TokenInput
                  selectedPlatform={localSelectedPlatform}
                  setSelectedPlatform={setLocalSelectedPlatform}
                  accessToken={localAccessToken}
                  setAccessToken={setLocalAccessToken}
                  showTokenSection={showTokenSection}
                  onToggleTokenSection={() => setShowTokenSection(!showTokenSection)}
                  allowPlatformChange={false} // Don't allow platform change during refresh
                />
              </>
            )}
            {/* Authorization Code Input */}
            {isAuthLoading && (
                <div className="mb-4 p-3 bg-[var(--background)]/50 rounded-md border border-[var(--border-color)] text-sm text-[var(--muted)]">
                  Loading authentication status...
                </div>
            )}
            {!isAuthLoading && authRequired && (
                <div className="mb-4 p-4 bg-[var(--background)]/50 rounded-md border border-[var(--border-color)]">
                  <label htmlFor="authCode" className="block text-sm font-medium text-[var(--foreground)] mb-2">
                    {t.form?.authorizationCode || 'Authorization Code'}
                  </label>
                  <input
                      type="password"
                      id="authCode"
                      value={authCode || ''}
                      onChange={(e) => setAuthCode?.(e.target.value)}
                      className="input-japanese block w-full px-3 py-2 text-sm rounded-md bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
                      placeholder="Enter your authorization code"
                  />
                  <div className="flex items-center mt-2 text-xs text-[var(--muted)]">
                    <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4 mr-1 text-[var(--muted)]"
                         fill="none" viewBox="0 0 24 24" stroke="currentColor">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                            d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    {t.form?.authorizationRequired || 'Authentication is required to generate the wiki.'}
                  </div>
                </div>
            )}

            {/* 🔐 Security Analysis (CVE vulnerability scan) — same block as in
                ConfigurationModal, so a wiki refresh can also opt into a scan. */}
            {showVulnScan && (
              <>
                <div className="my-4 border-t border-[var(--border-color)]/30"></div>
                <div className="mb-4 p-4 rounded-md border border-[var(--border-color)] bg-[var(--background)]/40">
                  <label className="flex items-center gap-2 text-sm font-medium text-[var(--foreground)] cursor-pointer">
                    <input
                      type="checkbox"
                      checked={localEnableVulnScan}
                      onChange={(e) => setLocalEnableVulnScan(e.target.checked)}
                      className="h-4 w-4 rounded border-[var(--border-color)] accent-[var(--accent-primary)]"
                    />
                    <span className="text-[var(--accent-primary)]">🔐 Security Analysis</span>
                    <span className="text-xs text-[var(--muted)] font-normal">
                      (scan dependencies for known CVEs via OSV.dev)
                    </span>
                  </label>

                  {localEnableVulnScan && (
                    <div className="mt-3 ml-6 space-y-2">
                      <p className="text-xs text-[var(--muted)] mb-1">
                        Categories to include:
                      </p>
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
                        <label htmlFor="refresh-nvd-key" className="block text-xs font-medium text-[var(--muted)] mb-1">
                          🔑 Optional NVD API key (free at nvd.nist.gov — adds CVSS scores)
                        </label>
                        <input
                          type="password"
                          id="refresh-nvd-key"
                          value={localNvdKey}
                          onChange={(e) => setLocalNvdKey(e.target.value)}
                          className="input-japanese block w-full px-3 py-2 text-sm rounded-md bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
                          placeholder="leave empty to use OSV.dev only"
                          autoComplete="off"
                        />
                      </div>

                      <label className="flex items-center gap-2 text-sm text-[var(--foreground)] cursor-pointer pt-1">
                        <input
                          type="checkbox"
                          checked={localIncludeVulnsInObsidian}
                          onChange={(e) => setLocalIncludeVulnsInObsidian(e.target.checked)}
                          className="h-4 w-4 rounded border-[var(--border-color)] accent-[var(--accent-primary)]"
                        />
                        Include vulnerability report in Obsidian export
                      </label>
                    </div>
                  )}
                </div>
              </>
            )}
          </div>

          {/* Modal footer */}
          <div className="flex items-center justify-end gap-2 px-6 py-4 border-t border-[var(--border-color)]">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm font-medium rounded-md border border-[var(--border-color)]/50 text-[var(--muted)] bg-transparent hover:bg-[var(--background)] hover:text-[var(--foreground)] transition-colors"
            >
              {t.common?.cancel || 'Cancel'}
            </button>
            <button
              type="button"
              onClick={handleApply}
              className="px-4 py-2 text-sm font-medium rounded-md border border-transparent bg-[var(--accent-primary)]/90 text-white hover:bg-[var(--accent-primary)] transition-colors"
            >
              {t.common?.submit || 'Submit'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
