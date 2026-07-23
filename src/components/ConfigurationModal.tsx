'use client';

import React, { useState } from 'react';
import { useLanguage } from '@/contexts/LanguageContext';
import UserSelector from './UserSelector';
import TokenInput from './TokenInput';
import WikiTypeSelector from './WikiTypeSelector';

interface ConfigurationModalProps {
  isOpen: boolean;
  onClose: () => void;

  // Repository input
  repositoryInput: string;

  // Language selection
  selectedLanguage: string;
  setSelectedLanguage: (value: string) => void;
  supportedLanguages: Record<string, string>;

  // Wiki type options
  isComprehensiveView: boolean;
  setIsComprehensiveView: (value: boolean) => void;
  isUserFocusedView: boolean;
  setIsUserFocusedView: (value: boolean) => void;
  pageCount: number;
  setPageCount: (value: number) => void;

  // Model selection
  provider: string;
  setProvider: (value: string) => void;
  model: string;
  setModel: (value: string) => void;
  isCustomModel: boolean;
  setIsCustomModel: (value: boolean) => void;
  customModel: string;
  setCustomModel: (value: string) => void;

  // Platform selection
  selectedPlatform: 'github' | 'gitlab' | 'bitbucket';
  setSelectedPlatform: (value: 'github' | 'gitlab' | 'bitbucket') => void;

  // Access token
  accessToken: string;
  setAccessToken: (value: string) => void;

  // File filter options
  excludedDirs: string;
  setExcludedDirs: (value: string) => void;
  excludedFiles: string;
  setExcludedFiles: (value: string) => void;
  includedDirs: string;
  setIncludedDirs: (value: string) => void;
  includedFiles: string;
  setIncludedFiles: (value: string) => void;

  // Form submission
  onSubmit: () => void;
  isSubmitting: boolean;

  // Vulnerability scan options (🔐 Security Analysis)
  enableVulnScan: boolean;
  setEnableVulnScan: (value: boolean) => void;
  vulnClient: boolean;
  setVulnClient: (value: boolean) => void;
  vulnServer: boolean;
  setVulnServer: (value: boolean) => void;
  vulnDeps: boolean;
  setVulnDeps: (value: boolean) => void;
  nvdKey: string;
  setNvdKey: (value: string) => void;

  // 🌐 Website wikis: crawl scope + analysis-mode options. Only rendered
  // when isWebsite is true (i.e. parseRepositoryInput classified the input
  // as a website, not a code repo) -- these must never appear for a repo.
  isWebsite?: boolean;
  crawlScopeMode?: 'count' | 'subdomains' | 'all';
  setCrawlScopeMode?: (value: 'count' | 'subdomains' | 'all') => void;
  crawlMaxPages?: number;
  setCrawlMaxPages?: (value: number) => void;
  crawlSubdomains?: string;
  setCrawlSubdomains?: (value: string) => void;
  crawlRespectRobots?: boolean;
  setCrawlRespectRobots?: (value: boolean) => void;
  enableTechnicalAnalysis?: boolean;
  setEnableTechnicalAnalysis?: (value: boolean) => void;
  enableDeepScan?: boolean;
  setEnableDeepScan?: (value: boolean) => void;

  // Authentication
  authRequired?: boolean;
  authCode?: string;
  setAuthCode?: (code: string) => void;
  isAuthLoading?: boolean;
}

export default function ConfigurationModal({
  isOpen,
  onClose,
  repositoryInput,
  selectedLanguage,
  setSelectedLanguage,
  supportedLanguages,
  isComprehensiveView,
  setIsComprehensiveView,
  isUserFocusedView,
  setIsUserFocusedView,
  pageCount,
  setPageCount,
  provider,
  setProvider,
  model,
  setModel,
  isCustomModel,
  setIsCustomModel,
  customModel,
  setCustomModel,
  selectedPlatform,
  setSelectedPlatform,
  accessToken,
  setAccessToken,
  excludedDirs,
  setExcludedDirs,
  excludedFiles,
  setExcludedFiles,
  includedDirs,
  setIncludedDirs,
  includedFiles,
  setIncludedFiles,
  onSubmit,
  isSubmitting,
  enableVulnScan,
  setEnableVulnScan,
  vulnClient,
  setVulnClient,
  vulnServer,
  setVulnServer,
  vulnDeps,
  setVulnDeps,
  nvdKey,
  setNvdKey,
  isWebsite = false,
  crawlScopeMode = 'count',
  setCrawlScopeMode,
  crawlMaxPages = 60,
  setCrawlMaxPages,
  crawlSubdomains = '',
  setCrawlSubdomains,
  crawlRespectRobots = true,
  setCrawlRespectRobots,
  enableTechnicalAnalysis = false,
  setEnableTechnicalAnalysis,
  enableDeepScan = false,
  setEnableDeepScan,
  authRequired,
  authCode,
  setAuthCode,
  isAuthLoading
}: ConfigurationModalProps) {
  const { messages: t } = useLanguage();

  // Show token section state
  const [showTokenSection, setShowTokenSection] = useState(false);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 overflow-y-auto">
      <div className="flex min-h-screen items-center justify-center p-4 text-center bg-black/50">
        <div className="relative transform overflow-hidden rounded-lg bg-[var(--card-bg)] text-left shadow-xl transition-all sm:my-8 sm:max-w-2xl sm:w-full">
          {/* Modal header with close button */}
          <div className="flex items-center justify-between px-6 py-4 border-b border-[var(--border-color)]">
            <h3 className="text-lg font-medium text-[var(--accent-primary)]">
              <span className="text-[var(--accent-primary)]">{t.form?.configureWiki || 'Configure Wiki'}</span>
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
          <div className="p-6 max-h-[70vh] overflow-y-auto">
            {/* Repository info */}
            <div className="mb-4">
              <label className="block text-sm font-medium text-[var(--foreground)] mb-2">
                {t.form?.repository || 'Repository'}
              </label>
              <div className="bg-[var(--background)]/70 p-3 rounded-md border border-[var(--border-color)] text-sm text-[var(--foreground)]">
                {repositoryInput}
              </div>
            </div>

            {/* Language selection */}
            <div className="mb-4">
              <label htmlFor="language-select" className="block text-sm font-medium text-[var(--foreground)] mb-2">
                {t.form?.wikiLanguage || 'Wiki Language'}
              </label>
              <select
                id="language-select"
                value={selectedLanguage}
                onChange={(e) => setSelectedLanguage(e.target.value)}
                className="input-japanese block w-full px-3 py-2 text-sm rounded-md bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
              >
                {
                  Object.entries(supportedLanguages).map(([key, value])=> <option key={key} value={key}>{value}</option>)
                }
              </select>
            </div>

            <WikiTypeSelector
              isComprehensiveView={isComprehensiveView}
              setIsComprehensiveView={setIsComprehensiveView}
              pageCount={pageCount}
              setPageCount={setPageCount}
              isUserFocusedView={isUserFocusedView}
              setIsUserFocusedView={setIsUserFocusedView}
            />

            {/* Model Selector */}
            <div className="mb-4">
              <UserSelector
                provider={provider}
                setProvider={setProvider}
                model={model}
                setModel={setModel}
                isCustomModel={isCustomModel}
                setIsCustomModel={setIsCustomModel}
                customModel={customModel}
                setCustomModel={setCustomModel}
                showFileFilters={true}
                excludedDirs={excludedDirs}
                setExcludedDirs={setExcludedDirs}
                excludedFiles={excludedFiles}
                setExcludedFiles={setExcludedFiles}
                includedDirs={includedDirs}
                setIncludedDirs={setIncludedDirs}
                includedFiles={includedFiles}
                setIncludedFiles={setIncludedFiles}
              />
            </div>

            {/* Access token section using TokenInput component -- git hosting
                auth doesn't apply to a website crawl. */}
            {!isWebsite && (
              <TokenInput
                selectedPlatform={selectedPlatform}
                setSelectedPlatform={setSelectedPlatform}
                accessToken={accessToken}
                setAccessToken={setAccessToken}
                showTokenSection={showTokenSection}
                onToggleTokenSection={() => setShowTokenSection(!showTokenSection)}
                allowPlatformChange={true}
              />
            )}

            {/* 🌐 Website wiki options -- crawl scope + analysis modes. Only
                shown when the input was detected as a website, never for an
                actual code repo. */}
            {isWebsite && (
              <div className="mb-4 p-4 rounded-md border border-[var(--border-color)] bg-[var(--background)]/40">
                <label className="block text-sm font-medium text-[var(--foreground)] mb-2">
                  <span className="text-[var(--accent-primary)]">🌐 Crawl Scope</span>
                </label>
                <div className="flex flex-col gap-2 mb-3">
                  <label className="flex items-center gap-2 text-sm text-[var(--foreground)] cursor-pointer">
                    <input
                      type="radio"
                      name="crawl-scope-mode"
                      checked={crawlScopeMode === 'count'}
                      onChange={() => setCrawlScopeMode?.('count')}
                      className="h-4 w-4 accent-[var(--accent-primary)]"
                    />
                    Limit to a number of pages
                  </label>
                  {crawlScopeMode === 'count' && (
                    <input
                      type="number"
                      min={1}
                      max={2000}
                      value={crawlMaxPages}
                      onChange={(e) => setCrawlMaxPages?.(Math.max(1, Math.min(2000, Number(e.target.value) || 1)))}
                      className="input-japanese ml-6 w-32 px-3 py-1.5 text-sm rounded-md bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
                    />
                  )}

                  <label className="flex items-center gap-2 text-sm text-[var(--foreground)] cursor-pointer">
                    <input
                      type="radio"
                      name="crawl-scope-mode"
                      checked={crawlScopeMode === 'subdomains'}
                      onChange={() => setCrawlScopeMode?.('subdomains')}
                      className="h-4 w-4 accent-[var(--accent-primary)]"
                    />
                    Specific subdomains / sections
                  </label>
                  {crawlScopeMode === 'subdomains' && (
                    <div className="ml-6">
                      <textarea
                        value={crawlSubdomains}
                        onChange={(e) => setCrawlSubdomains?.(e.target.value)}
                        rows={3}
                        placeholder={"One per line, e.g.:\nblog.example.com\nexample.com/docs\nshop.example.com"}
                        className="input-japanese block w-full px-3 py-2 text-sm rounded-md bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
                      />
                      <p className="text-xs text-[var(--muted)] mt-1">
                        One subdomain or path per line. Each is crawled as its own starting point (still limited to pages on the same site).
                      </p>
                    </div>
                  )}

                  <label className="flex items-center gap-2 text-sm text-[var(--foreground)] cursor-pointer">
                    <input
                      type="radio"
                      name="crawl-scope-mode"
                      checked={crawlScopeMode === 'all'}
                      onChange={() => setCrawlScopeMode?.('all')}
                      className="h-4 w-4 accent-[var(--accent-primary)]"
                    />
                    Entire site (capped at 2000 pages for safety)
                  </label>
                </div>

                <label className="flex items-center gap-2 text-sm text-[var(--foreground)] cursor-pointer pt-1 border-t border-[var(--border-color)]/30">
                  <input
                    type="checkbox"
                    checked={crawlRespectRobots}
                    onChange={(e) => setCrawlRespectRobots?.(e.target.checked)}
                    className="h-4 w-4 rounded border-[var(--border-color)] accent-[var(--accent-primary)] mt-2"
                  />
                  <span className="mt-2">Respect robots.txt (recommended)</span>
                </label>

                <div className="mt-4 pt-3 border-t border-[var(--border-color)]/30 space-y-2">
                  <label className="flex items-start gap-2 text-sm text-[var(--foreground)] cursor-pointer">
                    <input
                      type="checkbox"
                      checked={enableTechnicalAnalysis}
                      onChange={(e) => setEnableTechnicalAnalysis?.(e.target.checked)}
                      className="h-4 w-4 rounded border-[var(--border-color)] accent-[var(--accent-primary)] mt-0.5"
                    />
                    <span>
                      <span className="text-[var(--accent-primary)]">🛠️ Technical Analysis mode</span>
                      <span className="block text-xs text-[var(--muted)] font-normal">
                        Generate a wiki ABOUT the site itself (architecture, tech stack, page structure) instead of a wiki about the site&apos;s subject matter. Off by default: a fan wiki becomes a fan wiki, not a report on its HTML.
                      </span>
                    </span>
                  </label>
                  <p className="text-xs text-[var(--muted)] pl-6">
                    User-generated pages (profiles, comments, forum posts) are always excluded from the wiki -- the AI is instructed to skip them entirely, so no community/user content ever appears.
                  </p>

                  <label className="flex items-start gap-2 text-sm text-[var(--foreground)] cursor-pointer pt-2 border-t border-[var(--border-color)]/30 mt-2">
                    <input
                      type="checkbox"
                      checked={enableDeepScan}
                      onChange={(e) => setEnableDeepScan?.(e.target.checked)}
                      className="h-4 w-4 rounded border-[var(--border-color)] accent-[var(--accent-primary)] mt-0.5"
                    />
                    <span>
                      <span className="text-[var(--accent-primary)]">🐳 Deep security scan (Docker)</span>
                      <span className="block text-xs text-[var(--muted)] font-normal">
                        Adds nmap/nikto/httpx/testssl/nuclei/subfinder/ffuf/dalfox/wpscan on top of the always-on header/cookie/TLS/exposed-path checks. Requires Docker; downloads a multi-GB scan toolkit image the first time it runs.
                      </span>
                    </span>
                  </label>
                </div>
              </div>
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

            {/* 🔐 Security Analysis (CVE dependency vulnerability scan) --
                only meaningful for a cloned code repo (it scans dependency
                manifests on disk). Websites get their own always-on
                "Website Security" scan (headers/cookies/TLS/exposed-paths,
                plus the optional Docker deep scan above) instead. */}
            {!isWebsite && (
            <div className="mb-4 p-4 rounded-md border border-[var(--border-color)] bg-[var(--background)]/40">
              <label className="flex items-center gap-2 text-sm font-medium text-[var(--foreground)] cursor-pointer">
                <input
                  type="checkbox"
                  checked={enableVulnScan}
                  onChange={(e) => setEnableVulnScan(e.target.checked)}
                  className="h-4 w-4 rounded border-[var(--border-color)] accent-[var(--accent-primary)]"
                />
                <span className="text-[var(--accent-primary)]">🔐 Security Analysis</span>
                <span className="text-xs text-[var(--muted)] font-normal">
                  (scan dependencies for known CVEs via OSV.dev)
                </span>
              </label>

              {enableVulnScan && (
                <div className="mt-3 ml-6 space-y-2">
                  <p className="text-xs text-[var(--muted)] mb-1">
                    Categories to include:
                  </p>
                  <label className="flex items-center gap-2 text-sm text-[var(--foreground)] cursor-pointer">
                    <input
                      type="checkbox"
                      checked={vulnClient}
                      onChange={(e) => setVulnClient(e.target.checked)}
                      className="h-4 w-4 rounded border-[var(--border-color)] accent-[var(--accent-primary)]"
                    />
                    Client-side vulnerabilities
                  </label>
                  <label className="flex items-center gap-2 text-sm text-[var(--foreground)] cursor-pointer">
                    <input
                      type="checkbox"
                      checked={vulnServer}
                      onChange={(e) => setVulnServer(e.target.checked)}
                      className="h-4 w-4 rounded border-[var(--border-color)] accent-[var(--accent-primary)]"
                    />
                    Server-side vulnerabilities
                  </label>
                  <label className="flex items-center gap-2 text-sm text-[var(--foreground)] cursor-pointer">
                    <input
                      type="checkbox"
                      checked={vulnDeps}
                      onChange={(e) => setVulnDeps(e.target.checked)}
                      className="h-4 w-4 rounded border-[var(--border-color)] accent-[var(--accent-primary)]"
                    />
                    Dependency vulnerabilities
                  </label>

                  <div className="pt-2">
                    <label htmlFor="nvd-key" className="block text-xs font-medium text-[var(--muted)] mb-1">
                      🔑 Optional NVD API key (free at nvd.nist.gov — adds CVSS scores)
                    </label>
                    <input
                      type="password"
                      id="nvd-key"
                      value={nvdKey}
                      onChange={(e) => setNvdKey(e.target.value)}
                      className="input-japanese block w-full px-3 py-2 text-sm rounded-md bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
                      placeholder="leave empty to use OSV.dev only"
                      autoComplete="off"
                    />
                  </div>
                </div>
              )}
            </div>
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
              onClick={onSubmit}
              disabled={isSubmitting}
              className="px-4 py-2 text-sm font-medium rounded-md border border-transparent bg-[var(--accent-primary)]/90 text-white hover:bg-[var(--accent-primary)] transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {isSubmitting ? (t.common?.processing || 'Processing...') : (t.common?.generateWiki || 'Generate Wiki')}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
