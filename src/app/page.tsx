'use client';

import React, { useState, useEffect } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { FaWikipediaW, FaGithub } from 'react-icons/fa';
import ThemeToggle from '@/components/theme-toggle';
import Mermaid from '../components/Mermaid';
import ConfigurationModal from '@/components/ConfigurationModal';
import {
  getDefaultWikiPageCount,
  normalizeWikiPageCount,
} from '@/utils/wikiPageCount';
import ProcessedProjects from '@/components/ProcessedProjects';
import { extractUrlPath, extractUrlDomain } from '@/utils/urlDecoder';
import { useProcessedProjects } from '@/hooks/useProcessedProjects';

import { useLanguage } from '@/contexts/LanguageContext';

// Define the demo mermaid charts outside the component
const DEMO_FLOW_CHART = `graph TD
  A[Code Repository] --> B[HackDeepWiki]
  B --> C[Architecture Diagrams]
  B --> D[Component Relationships]
  B --> E[Data Flow]
  B --> F[Process Workflows]

  style A fill:#f9d3a9,stroke:#d86c1f
  style B fill:#d4a9f9,stroke:#6c1fd8
  style C fill:#a9f9d3,stroke:#1fd86c
  style D fill:#a9d3f9,stroke:#1f6cd8
  style E fill:#f9a9d3,stroke:#d81f6c
  style F fill:#d3f9a9,stroke:#6cd81f`;

const DEMO_SEQUENCE_CHART = `sequenceDiagram
  participant User
  participant HackDeepWiki
  participant GitHub

  User->>HackDeepWiki: Enter repository URL
  HackDeepWiki->>GitHub: Request repository data
  GitHub-->>HackDeepWiki: Return repository data
  HackDeepWiki->>HackDeepWiki: Process and analyze code
  HackDeepWiki-->>User: Display wiki with diagrams

  %% Add a note to make text more visible
  Note over User,GitHub: HackDeepWiki supports sequence diagrams for visualizing interactions`;

export default function Home() {
  const router = useRouter();
  const { language, setLanguage, messages, supportedLanguages } = useLanguage();
  const { projects, isLoading: projectsLoading } = useProcessedProjects();

  // Create a simple translation function
  const t = (key: string, params: Record<string, string | number> = {}): string => {
    // Split the key by dots to access nested properties
    const keys = key.split('.');
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    let value: any = messages;

    // Navigate through the nested properties
    for (const k of keys) {
      if (value && typeof value === 'object' && k in value) {
        value = value[k];
      } else {
        // Return the key if the translation is not found
        return key;
      }
    }

    // If the value is a string, replace parameters
    if (typeof value === 'string') {
      return Object.entries(params).reduce((acc: string, [paramKey, paramValue]) => {
        return acc.replace(`{${paramKey}}`, String(paramValue));
      }, value);
    }

    // Return the key if the value is not a string
    return key;
  };

  const [repositoryInput, setRepositoryInput] = useState('https://github.com/kroryan/HackDeepWiki');

  const REPO_CONFIG_CACHE_KEY = 'hackdeepwikiRepoConfigCache';

  const loadConfigFromCache = (repoUrl: string) => {
    if (!repoUrl) return;
    try {
      const cachedConfigs = localStorage.getItem(REPO_CONFIG_CACHE_KEY);
      if (cachedConfigs) {
        const configs = JSON.parse(cachedConfigs);
        const config = configs[repoUrl.trim()];
        if (config) {
          const cachedComprehensive =
            config.isComprehensiveView === undefined
              ? true
              : config.isComprehensiveView;
          setSelectedLanguage(config.selectedLanguage || language);
          setIsComprehensiveView(cachedComprehensive);
          setPageCount(
            normalizeWikiPageCount(config.pageCount, cachedComprehensive),
          );
          setProvider(config.provider || '');
          setModel(config.model || '');
          setIsCustomModel(config.isCustomModel || false);
          setCustomModel(config.customModel || '');
          setSelectedPlatform(config.selectedPlatform || 'github');
          setExcludedDirs(config.excludedDirs || '');
          setExcludedFiles(config.excludedFiles || '');
          setIncludedDirs(config.includedDirs || '');
          setIncludedFiles(config.includedFiles || '');
          if (config.enableVulnScan !== undefined) setEnableVulnScan(config.enableVulnScan);
          if (config.vulnClient !== undefined) setVulnClient(config.vulnClient);
          if (config.vulnServer !== undefined) setVulnServer(config.vulnServer);
          if (config.vulnDeps !== undefined) setVulnDeps(config.vulnDeps);
          if (config.nvdKey !== undefined) setNvdKey(config.nvdKey || '');
        }
      }
    } catch (error) {
      console.error('Error loading config from localStorage:', error);
    }
  };

  const handleRepositoryInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const newRepoUrl = e.target.value;
    setRepositoryInput(newRepoUrl);
    if (newRepoUrl.trim() === "") {
      // Optionally reset fields if input is cleared
    } else {
        loadConfigFromCache(newRepoUrl);
    }
  };

  useEffect(() => {
    if (repositoryInput) {
      loadConfigFromCache(repositoryInput);
    }
  }, []);

  // Provider-based model selection state
  const [provider, setProvider] = useState<string>('');
  const [model, setModel] = useState<string>('');
  const [isCustomModel, setIsCustomModel] = useState<boolean>(false);
  const [customModel, setCustomModel] = useState<string>('');

  // Wiki type state - default to comprehensive view
  const [isComprehensiveView, setIsComprehensiveView] = useState<boolean>(true);
  const [pageCount, setPageCount] = useState<number>(
    getDefaultWikiPageCount(true),
  );

  const [excludedDirs, setExcludedDirs] = useState('');
  const [excludedFiles, setExcludedFiles] = useState('');
  const [includedDirs, setIncludedDirs] = useState('');
  const [includedFiles, setIncludedFiles] = useState('');
  const [selectedPlatform, setSelectedPlatform] = useState<'github' | 'gitlab' | 'bitbucket'>('github');
  const [accessToken, setAccessToken] = useState('');

  // 🔐 Vulnerability scan (Security Analysis) config
  const [enableVulnScan, setEnableVulnScan] = useState<boolean>(false);
  const [vulnClient, setVulnClient] = useState<boolean>(true);
  const [vulnServer, setVulnServer] = useState<boolean>(true);
  const [vulnDeps, setVulnDeps] = useState<boolean>(true);
  const [nvdKey, setNvdKey] = useState<string>('');

  // 🌐 Website wikis: what parseRepositoryInput classified the current input
  // as -- ConfigurationModal only shows the website-only options (crawl
  // scope, Community Analysis, Technical Analysis mode) when this is
  // 'website', never for an actual code repo.
  const [detectedInputType, setDetectedInputType] = useState<string>('github');
  // Crawl scope: how much of the site to crawl -- a page-count cap, an
  // explicit subdomain/path list (documented in the UI as one entry per
  // line), or the whole site (still bounded by a hard server-side cap).
  const [crawlScopeMode, setCrawlScopeMode] = useState<'count' | 'subdomains' | 'all'>('count');
  const [crawlMaxPages, setCrawlMaxPages] = useState<number>(60);
  const [crawlSubdomains, setCrawlSubdomains] = useState<string>('');
  const [crawlRespectRobots, setCrawlRespectRobots] = useState<boolean>(true);
  // Default OFF: per the intended behaviour, a website wiki is a wiki
  // ABOUT the site's subject matter (e.g. a fan wiki gets rebuilt as a fan
  // wiki) -- a technical/architecture analysis of the site itself is an
  // explicit opt-in, entirely different generation mode.
  const [enableTechnicalAnalysis, setEnableTechnicalAnalysis] = useState<boolean>(false);
  // User-generated content (profiles, comments, forum posts) is always
  // excluded from the wiki -- the AI is instructed to skip it entirely,
  // not an opt-in toggle.
  // Deep website security scan (Docker toolkit: nmap/nikto/httpx/testssl/
  // nuclei/subfinder/ffuf/dalfox/wpscan) -- opt-in since it requires Docker
  // and downloads a multi-GB image on first use. The always-on pure-Python
  // checks (headers/cookies/TLS/exposed-paths) run regardless.
  const [enableDeepScan, setEnableDeepScan] = useState<boolean>(false);

  const [error, setError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [selectedLanguage, setSelectedLanguage] = useState<string>(language);

  // Authentication state
  const [authRequired, setAuthRequired] = useState<boolean>(false);
  const [authCode, setAuthCode] = useState<string>('');
  const [isAuthLoading, setIsAuthLoading] = useState<boolean>(true);

  // .zim import state
  const [isZimModalOpen, setIsZimModalOpen] = useState(false);
  const [zimPath, setZimPath] = useState('');
  const [zimError, setZimError] = useState<string | null>(null);
  const [isZimImporting, setIsZimImporting] = useState(false);
  const [zimDropDir, setZimDropDir] = useState<string | null>(null);
  const [isRescanningZim, setIsRescanningZim] = useState(false);
  const [rescanMessage, setRescanMessage] = useState<string | null>(null);
  const [projectsListKey, setProjectsListKey] = useState(0);

  useEffect(() => {
    fetch('/api/zim/drop_dir')
      .then((res) => res.json())
      .then((data) => setZimDropDir(data.path || null))
      .catch(() => setZimDropDir(null));
  }, []);

  const handleImportZim = async () => {
    const path = zimPath.trim();
    if (!path) {
      setZimError('Please enter the absolute path to a .zim file.');
      return;
    }
    setIsZimImporting(true);
    setZimError(null);
    try {
      const response = await fetch('/api/zim/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || data.error || 'Failed to import .zim file');
      }
      setIsZimModalOpen(false);
      setZimPath('');
      router.push(`/zim/${data.id}`);
    } catch (e: unknown) {
      setZimError(e instanceof Error ? e.message : 'Failed to import .zim file');
    } finally {
      setIsZimImporting(false);
    }
  };

  // Picks up any .zim file the user dropped directly into the drop folder
  // (via file manager / cp / mv) -- avoids typing a path for huge archives.
  const handleRescanZim = async () => {
    setIsRescanningZim(true);
    setRescanMessage(null);
    try {
      const response = await fetch('/api/zim/rescan', { method: 'POST' });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || data.error || 'Failed to rescan');
      }
      const addedCount = Array.isArray(data.added) ? data.added.length : 0;
      const errorCount = Array.isArray(data.errors) ? data.errors.length : 0;
      if (addedCount === 0 && errorCount === 0) {
        setRescanMessage('No new .zim files found.');
      } else {
        setRescanMessage(
          `${addedCount} archive(s) added${errorCount ? `, ${errorCount} failed` : ''}.`
        );
      }
      // ProcessedProjects fetches once on mount with no external refetch
      // hook -- bump a key to remount it so newly-registered archives show
      // up immediately without a manual page reload.
      setProjectsListKey((k) => k + 1);
    } catch (e: unknown) {
      setRescanMessage(e instanceof Error ? e.message : 'Failed to rescan');
    } finally {
      setIsRescanningZim(false);
    }
  };

  // Sync the language context with the selectedLanguage state
  useEffect(() => {
    setLanguage(selectedLanguage);
  }, [selectedLanguage, setLanguage]);

  // Fetch authentication status on component mount
  useEffect(() => {
    const fetchAuthStatus = async () => {
      try {
        setIsAuthLoading(true);
        const response = await fetch('/api/auth/status');
        if (!response.ok) {
          throw new Error(`HTTP error! status: ${response.status}`);
        }
        const data = await response.json();
        setAuthRequired(data.auth_required);
      } catch (err) {
        console.error("Failed to fetch auth status:", err);
        // Assuming auth is required if fetch fails to avoid blocking UI for safety
        setAuthRequired(true);
      } finally {
        setIsAuthLoading(false);
      }
    };

    fetchAuthStatus();
  }, []);

  // Parse repository URL/input and extract owner and repo
  const parseRepositoryInput = (input: string): {
    owner: string,
    repo: string,
    type: string,
    fullPath?: string,
    localPath?: string
  } | null => {
    input = input.trim();

    let owner = '', repo = '', type = 'github', fullPath;
    let localPath: string | undefined;

    // Handle Windows absolute paths (e.g., C:\path\to\folder)
    const windowsPathRegex = /^[a-zA-Z]:\\(?:[^\\/:*?"<>|\r\n]+\\)*[^\\/:*?"<>|\r\n]*$/;

    // A hostname that actually looks like a real domain/host, not a bare
    // shorthand segment like "kroryan" (from "kroryan/HackDeepWiki").
    const looksLikeRealHost = (h: string): boolean =>
      h.includes('.') || h === 'localhost' || /^\d{1,3}(\.\d{1,3}){3}$/.test(h);

    if (windowsPathRegex.test(input)) {
      type = 'local';
      localPath = input;
      repo = input.split('\\').pop() || 'local-repo';
      owner = 'local';
    }
    // Handle Unix/Linux absolute paths (e.g., /path/to/folder)
    else if (input.startsWith('/')) {
      type = 'local';
      localPath = input;
      repo = input.split('/').filter(Boolean).pop() || 'local-repo';
      owner = 'local';
    }
    // Anything that parses as a URL (or a bare "host[/path...]" once a
    // scheme is assumed -- extractUrlDomain/extractUrlPath both do that) is
    // classified by how many path segments follow the host, using the
    // browser's own URL parser rather than a hand-rolled regex (a prior
    // regex here mis-split single-path-segment URLs like
    // "https://example.com/blog" into a bogus 2-segment owner/repo pair).
    //
    //   0 or 1 path segments, real-looking host -> website (crawl target):
    //     "kroryandev.com", "https://example.com/", "https://example.com/blog"
    //   >=2 path segments -> git host/owner/repo:
    //     "https://github.com/kroryan/HackDeepWiki", self-hosted forges, etc.
    //   host has no dot and no path (e.g. "kroryan/HackDeepWiki" parsed as
    //     host "kroryan") -> falls through to Unsupported, same as before
    //     this feature existed, so a mistyped GitHub shorthand doesn't
    //     silently turn into a crawl of a nonexistent domain.
    else if (extractUrlDomain(input)) {
      const domain = extractUrlDomain(input)!;
      const hostname = domain.replace(/^https?:\/\//, '');
      const path = (extractUrlPath(input) || '').replace(/\.git$/, '');
      const parts = path ? path.split('/').filter(Boolean) : [];

      if (parts.length >= 2) {
        // host/owner/repo -- a git host of some kind.
        if (domain.includes('github.com')) {
          type = 'github';
        } else if (domain.includes('gitlab.com') || domain.includes('gitlab.')) {
          type = 'gitlab';
        } else if (domain.includes('bitbucket.org') || domain.includes('bitbucket.')) {
          type = 'bitbucket';
        } else {
          type = 'web'; // fallback for other git hosting services (Gitea, Codeberg, self-hosted, ...)
        }
        fullPath = path;
        repo = parts[parts.length - 1] || '';
        owner = parts[parts.length - 2] || '';
      } else if (looksLikeRealHost(hostname)) {
        // 0-1 path segments on a real domain -- a website to crawl, not a
        // git shorthand (which requires an explicit owner/repo pair).
        type = 'website';
        owner = 'website';
        repo = hostname;
        fullPath = path;
      }
      // else: single bare segment with no dot (e.g. "kroryan/HackDeepWiki"
      // parsed to host "kroryan", path "HackDeepWiki") -- falls through,
      // owner/repo stay empty, caught by the check below.
    }

    // Nothing matched (extractUrlDomain failed entirely, the host/path shape
    // matched neither a git nor a website pattern, or a git match had an
    // empty owner/repo segment) -- unsupported input.
    if (!owner || !repo) {
      console.error('Unsupported URL format:', input);
      return null;
    }

    // Clean values
    owner = owner.trim();
    repo = repo.trim();

    // Remove .git suffix if present
    if (repo.endsWith('.git')) {
      repo = repo.slice(0, -4);
    }

    return { owner, repo, type, fullPath, localPath };
  };

  // State for configuration modal
  const [isConfigModalOpen, setIsConfigModalOpen] = useState(false);

  const handleFormSubmit = (e: React.FormEvent) => {
    e.preventDefault();

    // Parse repository input to validate
    const parsedRepo = parseRepositoryInput(repositoryInput);

    if (!parsedRepo) {
      setError('Invalid repository format. Use "owner/repo", GitHub/GitLab/BitBucket URL, or a local folder path like "/path/to/folder" or "C:\\path\\to\\folder".');
      return;
    }

    // Drives whether ConfigurationModal shows the website-only options
    // (crawl scope, Community Analysis, Technical Analysis) -- those must
    // never appear for an actual code repo.
    setDetectedInputType(parsedRepo.type);

    // If valid, open the configuration modal
    setError(null);
    setIsConfigModalOpen(true);
  };

  const validateAuthCode = async () => {
    try {
      if(authRequired) {
        if(!authCode) {
          return false;
        }
        const response = await fetch('/api/auth/validate', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({'code': authCode})
        });
        if (!response.ok) {
          return false;
        }
        const data = await response.json();
        return data.success || false;
      }
    } catch {
      return false;
    }
    return true;
  };

  const handleGenerateWiki = async () => {

    // Check authorization code
    const validation = await validateAuthCode();
    if(!validation) {
      setError(`Failed to validate the authorization code`);
      console.error(`Failed to validate the authorization code`);
      setIsConfigModalOpen(false);
      return;
    }

    // Prevent multiple submissions
    if (isSubmitting) {
      console.log('Form submission already in progress, ignoring duplicate click');
      return;
    }

    try {
      const currentRepoUrl = repositoryInput.trim();
      if (currentRepoUrl) {
        const existingConfigs = JSON.parse(localStorage.getItem(REPO_CONFIG_CACHE_KEY) || '{}');
        const configToSave = {
          selectedLanguage,
          isComprehensiveView,
          pageCount,
          provider,
          model,
          isCustomModel,
          customModel,
          selectedPlatform,
          excludedDirs,
          excludedFiles,
          includedDirs,
          includedFiles,
          enableVulnScan,
          vulnClient,
          vulnServer,
          vulnDeps,
          nvdKey,
        };
        existingConfigs[currentRepoUrl] = configToSave;
        localStorage.setItem(REPO_CONFIG_CACHE_KEY, JSON.stringify(existingConfigs));
      }
    } catch (error) {
      console.error('Error saving config to localStorage:', error);
    }

    setIsSubmitting(true);

    // Parse repository input
    const parsedRepo = parseRepositoryInput(repositoryInput);

    if (!parsedRepo) {
      setError('Invalid repository format. Use "owner/repo", GitHub/GitLab/BitBucket URL, or a local folder path like "/path/to/folder" or "C:\\path\\to\\folder".');
      setIsSubmitting(false);
      return;
    }

    const { owner, repo, type, localPath, fullPath } = parsedRepo;

    // Store tokens in query params if they exist
    const params = new URLSearchParams();
    if (accessToken) {
      params.append('token', accessToken);
    }
    // Always include the type parameter
    params.append('type', (type === 'local' || type === 'website') ? type : (selectedPlatform || 'github'));
    // Add local path if it exists
    if (localPath) {
      params.append('local_path', encodeURIComponent(localPath));
    } else if (type === 'website') {
      // repositoryInput may be a bare host ("kroryandev.com", no scheme) --
      // the crawler and backend both need a full URL, so rebuild one from
      // the parsed hostname (repo) + path instead of sending the raw input.
      const websiteUrl = `https://${repo}${fullPath ? `/${fullPath}` : ''}`;
      params.append('repo_url', encodeURIComponent(websiteUrl));
    } else {
      params.append('repo_url', encodeURIComponent(repositoryInput));
    }
    // Add model parameters
    params.append('provider', provider);
    // Send the custom model as `model` when one is provided, so the repo page picks it up
    // directly as its selected model (the backend only honors `model`, not custom_model).
    params.append('model', (isCustomModel && customModel) ? customModel : model);
    if (isCustomModel && customModel) {
      params.append('custom_model', customModel);
    }
    // Add file filters configuration
    if (excludedDirs) {
      params.append('excluded_dirs', excludedDirs);
    }
    if (excludedFiles) {
      params.append('excluded_files', excludedFiles);
    }
    if (includedDirs) {
      params.append('included_dirs', includedDirs);
    }
    if (includedFiles) {
      params.append('included_files', includedFiles);
    }

    // Add language parameter
    params.append('language', selectedLanguage);

    // Add comprehensive parameter
    params.append('comprehensive', isComprehensiveView.toString());
    params.append('pages', pageCount.toString());

    // 🔐 Security Analysis (vulnerability scan) parameters
    if (enableVulnScan) {
      params.append('vuln_scan', '1');
      params.append('vuln_client', vulnClient ? '1' : '0');
      params.append('vuln_server', vulnServer ? '1' : '0');
      params.append('vuln_deps', vulnDeps ? '1' : '0');
      if (nvdKey) {
        params.append('nvd_key', encodeURIComponent(nvdKey));
      }
    }

    // 🌐 Website wiki crawl + analysis-mode parameters (only meaningful for
    // type === 'website'; harmless no-ops otherwise since the repo page only
    // reads them when repo_type is 'website').
    if (type === 'website') {
      params.append('crawl_scope_mode', crawlScopeMode);
      params.append('crawl_max_pages', crawlMaxPages.toString());
      if (crawlScopeMode === 'subdomains' && crawlSubdomains) {
        params.append('crawl_subdomains', encodeURIComponent(crawlSubdomains));
      }
      params.append('crawl_respect_robots', crawlRespectRobots ? '1' : '0');
      params.append('technical_analysis', enableTechnicalAnalysis ? '1' : '0');
      params.append('deep_scan', enableDeepScan ? '1' : '0');
    }

    const queryString = params.toString() ? `?${params.toString()}` : '';

    // Navigate to the dynamic route
    router.push(`/${owner}/${repo}${queryString}`);

    // The isSubmitting state will be reset when the component unmounts during navigation
  };

  return (
    <div className="h-screen paper-texture p-4 md:p-8 flex flex-col">
      <header className="max-w-6xl mx-auto mb-6 h-fit w-full">
        <div
          className="flex flex-col md:flex-row md:items-center md:justify-between gap-4 bg-[var(--card-bg)] rounded-lg shadow-custom border border-[var(--border-color)] p-4">
          <div className="flex items-center">
            <div className="bg-[var(--accent-primary)] p-2 rounded-lg mr-3">
              <FaWikipediaW className="text-2xl text-white" />
            </div>
            <div className="mr-6">
              <h1 className="text-xl md:text-2xl font-bold text-[var(--accent-primary)]">{t('common.appName')}</h1>
              <div className="flex flex-wrap items-baseline gap-x-2 md:gap-x-3 mt-0.5">
                <p className="text-xs text-[var(--muted)] whitespace-nowrap">{t('common.tagline')}</p>
                <div className="hidden md:inline-block">
                  <Link href="/wiki/projects"
                    className="text-xs font-medium text-[var(--accent-primary)] hover:text-[var(--highlight)] hover:underline whitespace-nowrap">
                    {t('nav.wikiProjects')}
                  </Link>
                </div>
              </div>
            </div>
          </div>

          <form onSubmit={handleFormSubmit} className="flex flex-col gap-3 w-full max-w-3xl">
            {/* Repository URL input and submit button */}
            <div className="flex flex-col sm:flex-row gap-2">
              <div className="relative flex-1">
                <input
                  type="text"
                  value={repositoryInput}
                  onChange={handleRepositoryInputChange}
                  placeholder={t('form.repoPlaceholder') || "owner/repo, GitHub/GitLab/BitBucket URL, or local folder path"}
                  className="input-japanese block w-full pl-10 pr-3 py-2.5 border-[var(--border-color)] rounded-lg bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
                />
                {error && (
                  <div className="text-[var(--highlight)] text-xs mt-1">
                    {error}
                  </div>
                )}
              </div>
              <button
                type="submit"
                className="btn-japanese px-6 py-2.5 rounded-lg disabled:opacity-50 disabled:cursor-not-allowed"
                disabled={isSubmitting}
              >
                {isSubmitting ? t('common.processing') : t('common.generateWiki')}
              </button>
            </div>
          </form>

          <div className="flex justify-center items-center gap-3 w-full max-w-3xl mt-3">
            <button
              type="button"
              onClick={() => setIsZimModalOpen(true)}
              className="text-sm text-[var(--muted)] hover:text-[var(--accent-primary)] underline underline-offset-2 transition-colors"
            >
              {t('common.importZim') !== 'common.importZim' ? t('common.importZim') : 'Import .zim archive'}
            </button>
            <span className="text-[var(--muted)] text-xs">•</span>
            <button
              type="button"
              onClick={handleRescanZim}
              disabled={isRescanningZim}
              className="text-sm text-[var(--muted)] hover:text-[var(--accent-primary)] underline underline-offset-2 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              title="Scan the .zim drop folder for new files"
            >
              {isRescanningZim ? 'Rescanning…' : 'Rescan .zim folder'}
            </button>
          </div>
          {rescanMessage && (
            <p className="text-center text-xs text-[var(--muted)] mt-1">{rescanMessage}</p>
          )}

          {isZimModalOpen && (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
              <div className="bg-[var(--card-bg)] border border-[var(--border-color)] rounded-lg shadow-custom w-full max-w-md p-6 card-japanese">
                <h3 className="text-lg font-semibold text-[var(--foreground)] mb-2">
                  Import .zim archive
                </h3>
                <p className="text-xs text-[var(--muted)] mb-4">
                  Enter the absolute path to a .zim file already on this machine (Kiwix / OpenZIM
                  offline wiki dump). The file is read in place, never uploaded or copied.
                </p>
                <input
                  type="text"
                  value={zimPath}
                  onChange={(e) => setZimPath(e.target.value)}
                  placeholder="/home/user/wikipedia_en_all.zim"
                  className="input-japanese block w-full px-3 py-2.5 border-[var(--border-color)] rounded-lg bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)] mb-2"
                  autoFocus
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') handleImportZim();
                  }}
                />
                {zimError && (
                  <div className="text-[var(--highlight)] text-xs mb-2">{zimError}</div>
                )}
                {zimDropDir && (
                  <div className="mt-2 mb-2 p-2 rounded-lg bg-[var(--background)] border border-[var(--border-color)]">
                    <p className="text-xs text-[var(--muted)]">
                      For large files, drop the .zim directly into this folder instead, then use
                      &quot;Rescan .zim folder&quot; below the form:
                    </p>
                    <code className="text-xs text-[var(--accent-primary)] break-all">{zimDropDir}</code>
                  </div>
                )}
                <div className="flex justify-end gap-2 mt-4">
                  <button
                    type="button"
                    onClick={() => {
                      setIsZimModalOpen(false);
                      setZimError(null);
                    }}
                    className="px-4 py-2 rounded-lg text-sm text-[var(--foreground)] hover:bg-[var(--background)] transition-colors"
                    disabled={isZimImporting}
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={handleImportZim}
                    className="btn-japanese px-4 py-2 rounded-lg text-sm disabled:opacity-50 disabled:cursor-not-allowed"
                    disabled={isZimImporting}
                  >
                    {isZimImporting ? 'Importing...' : 'Import'}
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Configuration Modal */}
          <ConfigurationModal
            isOpen={isConfigModalOpen}
            onClose={() => setIsConfigModalOpen(false)}
            repositoryInput={repositoryInput}
            selectedLanguage={selectedLanguage}
            setSelectedLanguage={setSelectedLanguage}
            supportedLanguages={supportedLanguages}
            isComprehensiveView={isComprehensiveView}
            setIsComprehensiveView={setIsComprehensiveView}
            pageCount={pageCount}
            setPageCount={setPageCount}
            provider={provider}
            setProvider={setProvider}
            model={model}
            setModel={setModel}
            isCustomModel={isCustomModel}
            setIsCustomModel={setIsCustomModel}
            customModel={customModel}
            setCustomModel={setCustomModel}
            selectedPlatform={selectedPlatform}
            setSelectedPlatform={setSelectedPlatform}
            accessToken={accessToken}
            setAccessToken={setAccessToken}
            excludedDirs={excludedDirs}
            setExcludedDirs={setExcludedDirs}
            excludedFiles={excludedFiles}
            setExcludedFiles={setExcludedFiles}
            includedDirs={includedDirs}
            setIncludedDirs={setIncludedDirs}
            includedFiles={includedFiles}
            setIncludedFiles={setIncludedFiles}
            onSubmit={handleGenerateWiki}
            isSubmitting={isSubmitting}
            enableVulnScan={enableVulnScan}
            setEnableVulnScan={setEnableVulnScan}
            vulnClient={vulnClient}
            setVulnClient={setVulnClient}
            vulnServer={vulnServer}
            setVulnServer={setVulnServer}
            vulnDeps={vulnDeps}
            setVulnDeps={setVulnDeps}
            nvdKey={nvdKey}
            setNvdKey={setNvdKey}
            isWebsite={detectedInputType === 'website'}
            crawlScopeMode={crawlScopeMode}
            setCrawlScopeMode={setCrawlScopeMode}
            crawlMaxPages={crawlMaxPages}
            setCrawlMaxPages={setCrawlMaxPages}
            crawlSubdomains={crawlSubdomains}
            setCrawlSubdomains={setCrawlSubdomains}
            crawlRespectRobots={crawlRespectRobots}
            setCrawlRespectRobots={setCrawlRespectRobots}
            enableTechnicalAnalysis={enableTechnicalAnalysis}
            setEnableTechnicalAnalysis={setEnableTechnicalAnalysis}
            enableDeepScan={enableDeepScan}
            setEnableDeepScan={setEnableDeepScan}
            authRequired={authRequired}
            authCode={authCode}
            setAuthCode={setAuthCode}
            isAuthLoading={isAuthLoading}
          />

        </div>
      </header>

      <main className="flex-1 max-w-6xl mx-auto w-full overflow-y-auto">
        <div
          className="min-h-full flex flex-col items-center p-8 pt-10 bg-[var(--card-bg)] rounded-lg shadow-custom card-japanese">

          {/* Conditionally show processed projects or welcome content */}
          {!projectsLoading && projects.length > 0 ? (
            <div className="w-full">
              {/* Header section for existing projects */}
              <div className="flex flex-col items-center w-full max-w-2xl mb-8 mx-auto">
                <div className="flex flex-col sm:flex-row items-center mb-6 gap-4">
                  <div className="relative">
                    <div className="absolute -inset-1 bg-[var(--accent-primary)]/20 rounded-full blur-md"></div>
                    <FaWikipediaW className="text-5xl text-[var(--accent-primary)] relative z-10" />
                  </div>
                  <div className="text-center sm:text-left">
                    <h2 className="text-2xl font-bold text-[var(--foreground)] font-serif mb-1">{t('projects.existingProjects')}</h2>
                    <p className="text-[var(--accent-primary)] text-sm max-w-md">{t('projects.browseExisting')}</p>
                  </div>
                </div>
              </div>

              {/* Show processed projects */}
              <ProcessedProjects
                key={projectsListKey}
                showHeader={false}
                maxItems={6}
                messages={messages}
                className="w-full"
              />
            </div>
          ) : (
            <>
              {/* Header section */}
              <div className="flex flex-col items-center w-full max-w-2xl mb-8">
                <div className="flex flex-col sm:flex-row items-center mb-6 gap-4">
                  <div className="relative">
                    <div className="absolute -inset-1 bg-[var(--accent-primary)]/20 rounded-full blur-md"></div>
                    <FaWikipediaW className="text-5xl text-[var(--accent-primary)] relative z-10" />
                  </div>
                  <div className="text-center sm:text-left">
                    <h2 className="text-2xl font-bold text-[var(--foreground)] font-serif mb-1">{t('home.welcome')}</h2>
                    <p className="text-[var(--accent-primary)] text-sm max-w-md">{t('home.welcomeTagline')}</p>
                  </div>
                </div>

                <p className="text-[var(--foreground)] text-center mb-8 text-lg leading-relaxed">
                  {t('home.description')}
                </p>
              </div>

          {/* Quick Start section - redesigned for better spacing */}
          <div
            className="w-full max-w-2xl mb-10 bg-[var(--accent-primary)]/5 border border-[var(--accent-primary)]/20 rounded-lg p-5">
            <h3 className="text-sm font-semibold text-[var(--accent-primary)] mb-3 flex items-center">
              <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4 mr-2" fill="none" viewBox="0 0 24 24"
                stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              {t('home.quickStart')}
            </h3>
            <p className="text-sm text-[var(--foreground)] mb-3">{t('home.enterRepoUrl')}</p>
            <div className="grid grid-cols-1 gap-3 text-xs text-[var(--muted)]">
              <div
                className="bg-[var(--background)]/70 p-3 rounded border border-[var(--border-color)] font-mono overflow-x-hidden whitespace-nowrap"
              >https://github.com/kroryan/HackDeepWiki
              </div>
              <div
                className="bg-[var(--background)]/70 p-3 rounded border border-[var(--border-color)] font-mono overflow-x-hidden whitespace-nowrap"
              >https://gitlab.com/gitlab-org/gitlab
              </div>
              <div
                className="bg-[var(--background)]/70 p-3 rounded border border-[var(--border-color)] font-mono overflow-x-hidden whitespace-nowrap"
              >kroryan/HackDeepWiki
              </div>
              <div
                className="bg-[var(--background)]/70 p-3 rounded border border-[var(--border-color)] font-mono overflow-x-hidden whitespace-nowrap"
              >https://bitbucket.org/atlassian/atlaskit
              </div>
              <div
                className="bg-[var(--background)]/70 p-3 rounded border border-[var(--border-color)] font-mono overflow-x-hidden whitespace-nowrap"
              >https://example.com <span className="text-[var(--accent-primary)]">(website)</span>
              </div>
            </div>
          </div>

          {/* Visualization section - improved for better visibility */}
          <div
            className="w-full max-w-2xl mb-8 bg-[var(--background)]/70 rounded-lg p-6 border border-[var(--border-color)]">
            <div className="flex flex-col sm:flex-row items-start sm:items-center gap-2 mb-4">
              <svg xmlns="http://www.w3.org/2000/svg"
                className="h-5 w-5 text-[var(--accent-primary)] flex-shrink-0 mt-0.5 sm:mt-0" fill="none"
                viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
              </svg>
              <h3 className="text-base font-semibold text-[var(--foreground)] font-serif">{t('home.advancedVisualization')}</h3>
            </div>
            <p className="text-sm text-[var(--foreground)] mb-5 leading-relaxed">
              {t('home.diagramDescription')}
            </p>

            {/* Diagrams with improved layout */}
            <div className="grid grid-cols-1 gap-6">
              <div className="bg-[var(--card-bg)] p-4 rounded-lg border border-[var(--border-color)] shadow-custom">
                <h4 className="text-sm font-medium text-[var(--foreground)] mb-3 font-serif">{t('home.flowDiagram')}</h4>
                <Mermaid chart={DEMO_FLOW_CHART} />
              </div>

              <div className="bg-[var(--card-bg)] p-4 rounded-lg border border-[var(--border-color)] shadow-custom">
                <h4 className="text-sm font-medium text-[var(--foreground)] mb-3 font-serif">{t('home.sequenceDiagram')}</h4>
                <Mermaid chart={DEMO_SEQUENCE_CHART} />
              </div>
            </div>
          </div>
            </>
          )}
        </div>
      </main>

      <footer className="max-w-6xl mx-auto mt-8 flex flex-col gap-4 w-full">
        <div
          className="flex flex-col sm:flex-row justify-between items-center gap-4 bg-[var(--card-bg)] rounded-lg p-4 border border-[var(--border-color)] shadow-custom">
          <p className="text-[var(--muted)] text-sm font-serif">{t('footer.copyright')}</p>

          <div className="flex items-center gap-6">
            <div className="flex items-center space-x-5">
              <a href="https://github.com/kroryan/HackDeepWiki" target="_blank" rel="noopener noreferrer"
                className="text-[var(--muted)] hover:text-[var(--accent-primary)] transition-colors">
                <FaGithub className="text-xl" />
              </a>
            </div>
            <ThemeToggle />
          </div>
        </div>
      </footer>
    </div>
  );
}
