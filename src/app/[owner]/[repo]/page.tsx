/* eslint-disable @typescript-eslint/no-unused-vars */
'use client';

import ChatWidget from '@/components/ChatWidget';
import FileBrowserModal from '@/components/FileBrowserModal';
import Markdown from '@/components/Markdown';
import ModelSelectionModal, { AppliedModelSelection } from '@/components/ModelSelectionModal';
import ThemeToggle from '@/components/theme-toggle';
import WikiTreeView from '@/components/WikiTreeView';
import VulnSection from '@/components/vuln/VulnSection';
import { VulnReport, VulnScanStatus } from '@/components/vuln/types';
import WebVulnSection from '@/components/vuln/WebVulnSection';
import { WebVulnReport } from '@/components/vuln/webTypes';
import RescanConfigModal, { RescanSelection } from '@/components/vuln/RescanConfigModal';
import { useLanguage } from '@/contexts/LanguageContext';
import { RepoInfo } from '@/types/repoinfo';
import { getSavedApiCredentials } from '@/utils/apiCredentials';
import { getBackendWebSocketUrl } from '@/utils/backendUrl';
import getRepoUrl from '@/utils/getRepoUrl';
import { WEBSOCKET_CONNECT_TIMEOUT_MS } from '@/utils/timeouts';
import { extractUrlDomain, extractUrlPath } from '@/utils/urlDecoder';
import { normalizeWikiPageCount } from '@/utils/wikiPageCount';
import { StreamParser } from '@/utils/streamParser';
import {
  addTokensToRequestBody,
  CloneProgress,
  createBitbucketHeaders,
  createGithubHeaders,
  createGitlabHeaders,
  fetchRepoStructureViaBackendClone,
  fetchWebsiteStructureViaCrawl,
  getCacheKey,
  MAX_RELEVANT_FILES_PER_PAGE,
  treeToFileList,
  WebVulnScanOverrides,
  VulnScanOverrides,
  WikiPage,
  WikiRelease,
  WikiSection,
  WikiStructure,
  wikiStyles,
  ScanRelease,
  WebsiteCrawlScope,
  WebsiteCrawlResult,
} from '@/utils/repoWikiHelpers';
import Link from 'next/link';
import { useParams, useSearchParams } from 'next/navigation';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { FaArchive, FaBitbucket, FaBookOpen, FaDownload, FaEdit, FaExclamationTriangle, FaFileCode, FaFileExport, FaFolder, FaGithub, FaGitlab, FaHistory, FaHome, FaMagic, FaMobileAlt, FaSave, FaSync, FaTimes, FaTrash } from 'react-icons/fa';

export default function RepoWikiPage() {
  // Get route parameters and search params
  const params = useParams();
  const searchParams = useSearchParams();

  // Extract owner and repo from route params
  const owner = params.owner as string;
  const repo = params.repo as string;

  // Extract tokens from search params
  const token = searchParams.get('token') || '';
  const localPath = searchParams.get('local_path') ? decodeURIComponent(searchParams.get('local_path') || '') : undefined;
  const repoUrl = searchParams.get('repo_url') ? decodeURIComponent(searchParams.get('repo_url') || '') : undefined;
  const providerParam = searchParams.get('provider') || '';
  const modelParam = searchParams.get('model') || '';
  const isCustomModelParam = searchParams.get('is_custom_model') === 'true';
  const customModelParam = searchParams.get('custom_model') || '';
  const language = searchParams.get('language') || 'en';
  const isComprehensiveParam = searchParams.get('comprehensive') !== 'false';
  const pageCountParam = normalizeWikiPageCount(
    searchParams.get('pages'),
    isComprehensiveParam,
  );
  // Audience mode: 'developer' (default, current behavior -- architecture/
  // implementation-focused) vs 'user' (an end-user guide: installation,
  // configuration, features, workflows, troubleshooting -- no source-code
  // analysis). Orthogonal to comprehensive/concise (which is about depth,
  // not audience), so it's its own param/state, mirroring how
  // isComprehensiveView is wired (editable via WikiTypeSelector, persisted
  // to the URL) rather than technicalAnalysisEnabled's read-only pattern.
  const isUserFocusedParam = searchParams.get('audience') === 'user';
  // Free-text guidance on which topics deserve more/less depth, so the
  // structure-planning LLM doesn't split attention evenly across topics of
  // wildly different real complexity (e.g. a game's core simulation systems
  // vs. a one-line install step) just for lack of any other signal.
  const focusInstructionsParam = searchParams.get('focus')
    ? decodeURIComponent(searchParams.get('focus') || '')
    : '';
  const repoHost = (() => {
    if (!repoUrl) return '';
    try {
      return new URL(repoUrl).hostname.toLowerCase();
    } catch (e) {
      console.warn(`Invalid repoUrl provided: ${repoUrl}`);
      return '';
    }
  })();
  const repoType = repoHost?.includes('bitbucket')
    ? 'bitbucket'
    : repoHost?.includes('gitlab')
      ? 'gitlab'
      : repoHost?.includes('github')
        ? 'github'
        : searchParams.get('type') || 'github';

  // 🔐 Security Analysis (vulnerability scan) params
  const vulnScanRequested = searchParams.get('vuln_scan') === '1';
  const vulnClientEnabled = searchParams.get('vuln_client') !== '0';
  const vulnServerEnabled = searchParams.get('vuln_server') !== '0';
  const vulnDepsEnabled = searchParams.get('vuln_deps') !== '0';
  const nvdKeyParam = searchParams.get('nvd_key')
    ? decodeURIComponent(searchParams.get('nvd_key') || '')
    : '';

  // 🌐 Website wiki (crawl) params -- only meaningful when repoType === 'website'.
  const crawlScopeModeParam = (searchParams.get('crawl_scope_mode') as 'count' | 'subdomains' | 'all') || 'count';
  const crawlMaxPagesParam = Number(searchParams.get('crawl_max_pages')) || 60;
  const crawlSubdomainsParam = searchParams.get('crawl_subdomains')
    ? decodeURIComponent(searchParams.get('crawl_subdomains') || '')
    : '';
  const crawlRespectRobotsParam = searchParams.get('crawl_respect_robots') !== '0';
  const technicalAnalysisEnabled = searchParams.get('technical_analysis') === '1';
  const deepScanEnabled = searchParams.get('deep_scan') === '1';

  // Import language context for translations
  const { messages } = useLanguage();

  // Initialize repo info
  const repoInfo = useMemo<RepoInfo>(() => ({
    owner,
    repo,
    type: repoType,
    token: token || null,
    localPath: localPath || null,
    repoUrl: repoUrl || null
  }), [owner, repo, repoType, localPath, repoUrl, token]);

  // State variables
  const [isLoading, setIsLoading] = useState(true);
  const [loadingMessage, setLoadingMessage] = useState<string | undefined>(
    messages.loading?.initializing || 'Initializing wiki generation...'
  );
  // Live progress while the backend clones the repo to disk for the first
  // time (see fetchRepoStructureViaBackendClone) -- null once cloning
  // finishes or wasn't needed (repo already cloned by a previous generation).
  const [cloneProgress, setCloneProgress] = useState<CloneProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [wikiStructure, setWikiStructure] = useState<WikiStructure | undefined>();
  // The full file/page tree used to plan the wiki structure, kept around so
  // the introduction page's content-generation prompt can embed it verbatim
  // (see generatePageContent's isIntroPage branch) -- guarantees every wiki
  // has one authoritative, complete rendering of the tree in one place,
  // instead of leaving it to chance whether/where the LLM mentions paths.
  const [repoFileTree, setRepoFileTree] = useState<string>('');
  const [currentPageId, setCurrentPageId] = useState<string | undefined>();
  const [generatedPages, setGeneratedPages] = useState<Record<string, WikiPage>>({});
  const [pagesInProgress, setPagesInProgress] = useState(new Set<string>());
  const [isExporting, setIsExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [originalMarkdown, setOriginalMarkdown] = useState<Record<string, string>>({});
  const [requestInProgress, setRequestInProgress] = useState(false);
  const [currentToken, setCurrentToken] = useState(token); // Track current effective token
  const [effectiveRepoInfo, setEffectiveRepoInfo] = useState(repoInfo); // Track effective repo info with cached data
  const [embeddingError, setEmbeddingError] = useState(false);
  const [connectionError, setConnectionError] = useState(false);
  // Set when structure generation fails for a content-quality reason (no
  // pages returned, idle timeout) rather than the repo/URL being invalid --
  // the generic "check your repo exists" hint below is actively misleading
  // for these (the repo/site was already found and read successfully).
  const [contentGenerationError, setContentGenerationError] = useState(false);

  // 🔐 Vulnerability scan state (Security Analysis)
  const [vulnReport, setVulnReport] = useState<VulnReport | null>(null);
  const [vulnStatus, setVulnStatus] = useState<VulnScanStatus>('idle');
  const [vulnProgressMessage, setVulnProgressMessage] = useState<string | undefined>();
  const [vulnProgressPercent, setVulnProgressPercent] = useState<number | null>(null);
  const [vulnError, setVulnError] = useState<string | null>(null);
  // 'wiki' shows the normal page content; 'security' swaps the content panel
  // for the vulnerability section without mutating the LLM-generated wiki tree.
  const [viewMode, setViewMode] = useState<'wiki' | 'security'>('wiki');
  const vulnScanStartedRef = useRef(false);
  // Obsidian export options (only relevant when a vuln report exists).
  const [exportIncludeVulns, setExportIncludeVulns] = useState<boolean>(true);
  const [exportIncludeVulnGraph, setExportIncludeVulnGraph] = useState<boolean>(true);
  // Lets the wiki-save effect kick off the vuln scan without adding
  // runVulnScan to its dependency list (which would retrigger saves).
  const runVulnScanRef = useRef<(overrides?: VulnScanOverrides) => void>(() => {});
  // Release history (versioned like wiki releases -- see VulnRelease below)
  // + the "rerun with a chosen provider/model" floating config modal.
  const [vulnReleases, setVulnReleases] = useState<ScanRelease[]>([]);
  const [selectedVulnVersion, setSelectedVulnVersion] = useState<number | null>(null);
  const [isVulnRescanModalOpen, setIsVulnRescanModalOpen] = useState(false);

  // Attaching an images folder to this fanwiki after the fact (see
  // api/fanwiki_import.py:attach_images) -- the wiki being viewed IS the
  // target, so start_url comes straight from effectiveRepoInfo, never typed
  // by the user (a prior version of this asked for it as free text, which
  // made no sense: the button already lives on the one wiki it applies to).
  const [isAttachImagesModalOpen, setIsAttachImagesModalOpen] = useState(false);
  const [attachImagesDir, setAttachImagesDir] = useState('');
  const [isAttachImagesBrowserOpen, setIsAttachImagesBrowserOpen] = useState(false);
  const [attachImagesRunning, setAttachImagesRunning] = useState(false);
  const [attachImagesError, setAttachImagesError] = useState<string | null>(null);
  const [attachImagesResult, setAttachImagesResult] = useState<{
    files_scanned: number; images_attached: number; images_still_missing: number;
  } | null>(null);

  const handleAttachImages = useCallback(async () => {
    const imagesDir = attachImagesDir.trim();
    if (!imagesDir) {
      setAttachImagesError(messages.repoPage?.attachImagesPathError || 'Please enter an image folder path.');
      return;
    }
    setAttachImagesRunning(true);
    setAttachImagesError(null);
    setAttachImagesResult(null);
    try {
      const response = await fetch('/api/fanwiki/attach_images', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start_url: getRepoUrl(effectiveRepoInfo), images_dir: imagesDir }),
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.detail || data.error || messages.repoPage?.attachImagesFailed || 'Failed to attach images');
      }
      setAttachImagesResult(data);
    } catch (e: unknown) {
      setAttachImagesError(e instanceof Error ? e.message : (messages.repoPage?.attachImagesFailed || 'Failed to attach images'));
    } finally {
      setAttachImagesRunning(false);
    }
  }, [attachImagesDir, effectiveRepoInfo]);

  // 🌐 Website vulnerability scan state -- separate report shape/endpoint
  // from the dependency scan above (WebVulnReport vs VulnReport), used only
  // when effectiveRepoInfo.type === 'website'.
  const [webVulnReport, setWebVulnReport] = useState<WebVulnReport | null>(null);
  const [webVulnStatus, setWebVulnStatus] = useState<VulnScanStatus>('idle');
  const [webVulnProgressMessage, setWebVulnProgressMessage] = useState<string | undefined>();
  const [webVulnProgressPercent, setWebVulnProgressPercent] = useState<number | null>(null);
  const [webVulnError, setWebVulnError] = useState<string | null>(null);
  const webVulnScanStartedRef = useRef(false);
  const runWebVulnScanRef = useRef<(overrides?: WebVulnScanOverrides) => void>(() => {});
  const [webVulnReleases, setWebVulnReleases] = useState<ScanRelease[]>([]);
  const [selectedWebVulnVersion, setSelectedWebVulnVersion] = useState<number | null>(null);
  const [isWebVulnRescanModalOpen, setIsWebVulnRescanModalOpen] = useState(false);

  // Page edit mode (manual textarea + AI-assisted rewrite). Never
  // autosaves -- editedContent only replaces generatedPages[pageId] on an
  // explicit Save, and is discarded on Cancel or navigating away.
  const [isEditingPage, setIsEditingPage] = useState(false);
  const [editedContent, setEditedContent] = useState('');
  const [editInstruction, setEditInstruction] = useState('');
  const [isAiEditing, setIsAiEditing] = useState(false);
  const [isSavingEdit, setIsSavingEdit] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);

  // Model selection state variables
  const [selectedProviderState, setSelectedProviderState] = useState(providerParam);
  const [selectedModelState, setSelectedModelState] = useState(modelParam);
  const [isCustomSelectedModelState, setIsCustomSelectedModelState] = useState(isCustomModelParam);
  const [customSelectedModelState, setCustomSelectedModelState] = useState(customModelParam);
  const [showModelOptions, setShowModelOptions] = useState(false); // Controls whether to show model options
  const excludedDirs = searchParams.get('excluded_dirs') || '';
  const excludedFiles = searchParams.get('excluded_files') || '';
  const [modelExcludedDirs, setModelExcludedDirs] = useState(excludedDirs);
  const [modelExcludedFiles, setModelExcludedFiles] = useState(excludedFiles);
  const includedDirs = searchParams.get('included_dirs') || '';
  const includedFiles = searchParams.get('included_files') || '';
  const [modelIncludedDirs, setModelIncludedDirs] = useState(includedDirs);
  const [modelIncludedFiles, setModelIncludedFiles] = useState(includedFiles);


  // Wiki type state - default to comprehensive view
  const [isComprehensiveView, setIsComprehensiveView] = useState(isComprehensiveParam);
  const [isUserFocusedView, setIsUserFocusedView] = useState(isUserFocusedParam);
  const [focusInstructions, setFocusInstructions] = useState(focusInstructionsParam);
  const [pageCount, setPageCount] = useState(pageCountParam);
  // Using useRef for activeContentRequests to maintain a single instance across renders
  // This map tracks which pages are currently being processed to prevent duplicate requests
  // Note: In a multi-threaded environment, additional synchronization would be needed,
  // but in React's single-threaded model, this is safe as long as we set the flag before any async operations
  const activeContentRequests = useRef(new Map<string, boolean>()).current;
  const [structureRequestInProgress, setStructureRequestInProgress] = useState(false);
  // Create a flag to track if data was loaded from cache to prevent immediate re-save
  const cacheLoadedSuccessfully = useRef(false);

  // Create a flag to ensure the effect only runs once
  const effectRan = React.useRef(false);

  // When the user clicks "Refresh Wiki", loadData must NOT restore the wiki
  // from the server cache (with versioning the old release is no longer deleted,
  // so the cache always hits). This flag makes the next loadData skip the cache
  // and go straight to regeneration; the counter guarantees the effect re-runs
  // even when no other dependency changed.
  const forceFreshGeneration = useRef(false);
  const [refreshTrigger, setRefreshTrigger] = useState(0);

  // Wiki Release versioning state. Each wiki generation/update is saved as a new
  // numbered release on the backend; the dropdown above the Refresh button lets
  // the user open any previous release instead of an update silently overwriting it.
  const [wikiReleases, setWikiReleases] = useState<WikiRelease[]>([]);
  const [selectedWikiVersion, setSelectedWikiVersion] = useState<number | null>(null);

  // Authentication state
  const [authRequired, setAuthRequired] = useState<boolean>(false);
  const [authCode, setAuthCode] = useState<string>('');
  const [isAuthLoading, setIsAuthLoading] = useState<boolean>(true);

  // Default branch state
  const [defaultBranch, setDefaultBranch] = useState<string>('main');

  // Helper function to generate proper repository file URLs
  const generateFileUrl = useCallback((filePath: string): string => {
    if (effectiveRepoInfo.type === 'local') {
      // For local repositories, we can't generate web URLs
      return filePath;
    }

    const repoUrl = effectiveRepoInfo.repoUrl;
    if (!repoUrl) {
      return filePath;
    }

    try {
      const url = new URL(repoUrl);
      const hostname = url.hostname;
      
      if (hostname === 'github.com' || hostname.includes('github')) {
        // GitHub URL format: https://github.com/owner/repo/blob/branch/path
        return `${repoUrl}/blob/${defaultBranch}/${filePath}`;
      } else if (hostname === 'gitlab.com' || hostname.includes('gitlab')) {
        // GitLab URL format: https://gitlab.com/owner/repo/-/blob/branch/path
        return `${repoUrl}/-/blob/${defaultBranch}/${filePath}`;
      } else if (hostname === 'bitbucket.org' || hostname.includes('bitbucket')) {
        // Bitbucket URL format: https://bitbucket.org/owner/repo/src/branch/path
        return `${repoUrl}/src/${defaultBranch}/${filePath}`;
      }
    } catch (error) {
      console.warn('Error generating file URL:', error);
    }

    // Fallback to just the file path
    return filePath;
  }, [effectiveRepoInfo, defaultBranch]);

  // Memoize repo info to avoid triggering updates in callbacks

  // Add useEffect to handle scroll reset
  useEffect(() => {
    // Scroll to top when currentPageId changes
    const wikiContent = document.getElementById('wiki-content');
    if (wikiContent) {
      wikiContent.scrollTo({ top: 0, behavior: 'smooth' });
    }
  }, [currentPageId]);


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

  // Generate content for a wiki page
  const generatePageContent = useCallback(async (page: WikiPage, owner: string, repo: string) => {
    return new Promise<void>(async (resolve) => {
      try {
        // Skip if content already exists
        if (generatedPages[page.id]?.content) {
          resolve();
          return;
        }

        // Skip if this page is already being processed
        // Use a synchronized pattern to avoid race conditions
        if (activeContentRequests.get(page.id)) {
          console.log(`Page ${page.id} (${page.title}) is already being processed, skipping duplicate call`);
          resolve();
          return;
        }

        // Mark this page as being processed immediately to prevent race conditions
        // This ensures that if multiple calls happen nearly simultaneously, only one proceeds
        activeContentRequests.set(page.id, true);

        // Validate repo info
        if (!owner || !repo) {
          throw new Error('Invalid repository information. Owner and repo name are required.');
        }

        // Mark page as in progress
        setPagesInProgress(prev => new Set(prev).add(page.id));
        // Don't set loading message for individual pages during queue processing

        // The wiki-structure planning step has no hard limit on how many
        // <file_path> entries it can put in a page's relevant_files (see
        // determineWikiStructure's prompt below), and for a large repo (e.g.
        // a full game/engine source tree with 10k+ code files) the planning
        // LLM can assign hundreds/thousands of paths to a single page. That
        // list gets embedded verbatim into this page's generation prompt, so
        // without a cap here the prompt itself can balloon past any model's
        // context window regardless of how well file_tree was filtered
        // upstream. Cap defensively at the point of use.
        const filePaths = page.filePaths.slice(0, MAX_RELEVANT_FILES_PER_PAGE);

        // The full file/page tree is only ever fed to the structure-planning
        // call (determineWikiStructure), never to individual page prompts --
        // so whether the tree ends up visible anywhere in the finished wiki
        // was previously left entirely to LLM whim (it might quote a few
        // paths on some page, all of them on none, or nowhere at all).
        // Force it into exactly one place, deterministically: the first page
        // in document order (pages[0], which every structure prompt is
        // instructed to make "Overview"/the equivalent introduction section
        // in the target language -- checking by position rather than an
        // English title match keeps this correct for every generation
        // language). No other page's prompt mentions the tree at all.
        // A raw source-code file tree is a developer artifact -- an
        // end-user guide has no use for it, so this stays off entirely for
        // user-focused repo wikis (websites/fanwikis never had
        // isUserFocusedView branching in the first place, see
        // determineWikiStructure).
        const isRepoPage = effectiveRepoInfo.type !== 'website' && effectiveRepoInfo.type !== 'fanwiki';
        const isIntroPage = !!repoFileTree && wikiStructure?.pages?.[0]?.id === page.id
          && !(isRepoPage && isUserFocusedView);

        // Cross-page linking (B6): the structure step emits <related_pages>
        // ids per page; surface them as titled links so the model weaves
        // connections between pages instead of leaving each page in a
        // silo. Only pages that actually exist in the structure are listed.
        const allPagesById = new Map((wikiStructure?.pages ?? []).map(p => [p.id, p]));
        const relatedPagesResolved = (page.relatedPages ?? [])
          .map(rid => allPagesById.get(rid))
          .filter((p): p is WikiPage => !!p);
        const relatedPagesBlock = relatedPagesResolved.length > 0
          ? `\n\nRELATED PAGES (link to these where the narrative naturally calls for it, using the format \`[Link Text](#${relatedPagesResolved[0].id})\` -- replace the id with the actual page id shown):\n${relatedPagesResolved.map(p => `- id="${p.id}" title="${p.title}"`).join('\n')}\n`
          : '';
        // The tree is wrapped in <file_tree> tags so the server-side
        // context_budget.summarize_file_tree_in_query (run on every /ws/chat
        // request) can cap it to the model's context budget for huge repos.
        // It only summarizes when the tree exceeds the budget; small repos
        // pass through untouched and the model reproduces them verbatim.
        const introStructureBlock = isIntroPage
          ? `\n\nCRITICAL STRUCTURE REQUIREMENT: This is the introduction/overview page, so it MUST include a "## Project Structure" section presenting the COMPLETE file/page tree below, verbatim, inside a single fenced code block (do not summarize, reorder, or omit any entries unless the tree itself was truncated below by the server for context limits -- in that case reproduce what you were given, including the "... and N more" ellipsis lines, exactly) -- this is the one authoritative place in the whole wiki where the full tree is shown:\n<file_tree>\n${repoFileTree}\n</file_tree>\n`
          : '';

        // Same signal as determineWikiStructure's userPriorityNote, but
        // reinforced at the individual-page level: the structure step
        // decides how many PAGES a topic gets, this decides how much DEPTH
        // this one page itself goes into, so a page covering something
        // flagged (or judged) as especially complex/central doesn't end up
        // just as long as a page covering something minor.
        const pagePriorityNoun = isRepoPage ? 'repository' : 'website';
        const pagePriorityNote = focusInstructions.trim()
          ? `\n\nDEPTH GUIDANCE: The user said the following matters most for this wiki -- if "${page.title}" relates to it, go deeper and more thorough than a typical page; if it's clearly minor/trivial by comparison, keep it appropriately concise instead of padding it out.\n<user_priorities>\n${focusInstructions.trim()}\n</user_priorities>`
          : `\n\nDEPTH GUIDANCE: Judge for yourself how central/complex "${page.title}" actually is within the ${pagePriorityNoun}. If it's a core subsystem or major feature, go deeper and more thorough than a typical page; if it's a minor, boilerplate, or one-off topic, keep it appropriately concise instead of padding it out to match other pages.`;

        // Store the initially generated content BEFORE rendering/potential modification
        setGeneratedPages(prev => ({
          ...prev,
          [page.id]: { ...page, content: 'Loading...' } // Placeholder
        }));
        setOriginalMarkdown(prev => ({ ...prev, [page.id]: '' })); // Clear previous original

        // Make API call to generate page content
        console.log(`Starting content generation for page: ${page.title}`);

        // Get repository URL
        const repoUrl = getRepoUrl(effectiveRepoInfo);

        // Create the prompt content - simplified to avoid message dialogs.
        //
        // 🌐 Websites have their own branch here (not just in
        // determineWikiStructure): this prompt used to be 100% hardcoded for
        // code repositories -- "software architect", "[RELEVANT_SOURCE_FILES]"
        // with a hard "AT LEAST 5 source files" requirement, Mermaid diagrams
        // for "architectures/data flow/schemas", code snippets in
        // "Python, Java, JavaScript, SQL", citations demanding "AT LEAST 5
        // different source files" -- none of which fits a crawled website
        // page (no source code, and often only 1-3 genuinely relevant
        // crawled pages, not 5+). That mismatch was confusing enough to
        // contribute to a real wiki-structure failure for websites (see the
        // system-prompt fix in api/prompts.py/websocket_wiki.py); this
        // prompt has the same category of problem for the per-page content
        // step, so it gets the same treatment.
        // 'fanwiki' (an imported MediaWiki XML dump) is page-based content
        // with no source code, exactly like a crawled website -- see
        // api.fanwiki_import's module docstring, and the matching is_website
        // reuse in api/websocket_wiki.py -- so it gets the same prompt
        // template rather than the "software architect" one meant for git repos.
        const isWebsitePage = effectiveRepoInfo.type === 'website' || effectiveRepoInfo.type === 'fanwiki';
        const pageLanguageLine = language === 'en' ? 'English' :
            language === 'ja' ? 'Japanese (日本語)' :
            language === 'zh' ? 'Mandarin Chinese (中文)' :
            language === 'zh-tw' ? 'Traditional Chinese (繁體中文)' :
            language === 'es' ? 'Spanish (Español)' :
            language === 'kr' ? 'Korean (한국어)' :
            language === 'vi' ? 'Vietnamese (Tiếng Việt)' :
            language === "pt-br" ? "Brazilian Portuguese (Português Brasileiro)" :
            language === "fr" ? "Français (French)" :
            language === "ru" ? "Русский (Russian)" :
            'English';

        // B7: localized scaffolding strings. The <details><summary> label
        // is the one piece of the page that's pure UI chrome, not content --
        // so it should follow the configured wiki language, not stay
        // English regardless. "Relevant source files" / "Relevant pages".
        const summaryLabel = isWebsitePage
          ? (language === 'es' ? 'Páginas relevantes'
            : language === 'ja' ? '関連ページ'
            : language === 'zh' ? '相关页面'
            : language === 'zh-tw' ? '相關頁面'
            : language === 'kr' ? '관련 페이지'
            : language === 'vi' ? 'Trang liên quan'
            : language === 'pt-br' ? 'Páginas relevantes'
            : language === 'fr' ? 'Pages pertinentes'
            : language === 'ru' ? 'Связанные страницы'
            : 'Relevant pages')
          : (language === 'es' ? 'Archivos fuente relevantes'
            : language === 'ja' ? '関連ソースファイル'
            : language === 'zh' ? '相关源文件'
            : language === 'zh-tw' ? '相關原始檔'
            : language === 'kr' ? '관련 소스 파일'
            : language === 'vi' ? 'Tệp nguồn liên quan'
            : language === 'pt-br' ? 'Arquivos fonte relevantes'
            : language === 'fr' ? 'Fichiers source pertinents'
            : language === 'ru' ? 'Связанные исходные файлы'
            : 'Relevant source files');
        // Placed at the very top of each template so the model adopts the
        // target language from the first token instead of starting in
        // English and then switching mid-sentence after the trailing
        // "IMPORTANT: Generate the content in X language" line (which
        // stays as a reinforcement).
        const langInstructionTop = `IMPORTANT: Write the ENTIRE page (including all headings, prose, table content, and the <summary> label) in ${pageLanguageLine}. Do NOT write any English unless ${pageLanguageLine} genuinely uses an English term.`;

        const promptContent = isWebsitePage ? (technicalAnalysisEnabled ? `${langInstructionTop}
You are an expert technical writer analyzing a crawled website.
Your task is to generate a comprehensive and accurate wiki page in Markdown format about a specific technical aspect (page template, subsystem, navigation pattern, detected technology) of the website's own implementation.

You will be given:
1. The "[WIKI_PAGE_TOPIC]" for the page you need to create.
2. A list of "[RELEVANT_PAGES]" -- crawled pages from the site (converted to Markdown) that you should use as the basis for the content. There may be as few as one; use what's provided rather than demanding more.

CRITICAL STARTING INSTRUCTION:
The very first thing on the page MUST be a \`<details>\` block listing the \`[RELEVANT_PAGES]\` you used. Format it exactly like this:
<details>
<summary>${summaryLabel}</summary>

Remember, do not provide any acknowledgements, disclaimers, apologies, or any other preface before the \`<details>\` block. JUST START with the \`<details>\` block.
The following pages were used as context for generating this wiki page:

${filePaths.map(path => `- [${path}](${generateFileUrl(path)})`).join('\n')}
</details>

Immediately after the \`<details>\` block, the main title of the page should be a H1 Markdown heading: \`# ${page.title}\`.

Based ONLY on the content of the \`[RELEVANT_PAGES]\` and what can be observed in them (headers, meta tags, script/link references, page structure):

1.  **Introduction:** Start with a concise introduction (1-2 paragraphs) explaining the purpose and scope of "${page.title}" within this technical analysis of the site.
2.  **Detailed Sections:** Break down "${page.title}" into logical sections using H2 (\`##\`) and H3 (\`###\`) headings, covering what's actually observable (technology signals, structural patterns, navigation) rather than speculating about server-side implementation you cannot see.
3.  **Diagrams (optional):** Use a Mermaid \`graph TD\` (top-down, never \`graph LR\`) diagram if it genuinely clarifies a structural or navigation relationship. Quote any label containing punctuation, e.g. A["Blog (WordPress)"].
4.  **Tables:** Use Markdown tables where they help summarize (e.g. detected technologies, page templates).
5.  **Citations:** Cite the specific crawled page path(s) each claim is drawn from, e.g. \`Sources: [path/to/page.md]()\`. Cite as many of the provided pages as are actually relevant -- do not pad citations to hit an arbitrary count.
6.  **Accuracy:** Only state what's actually evidenced in the provided pages. If something can't be determined from crawled HTML/Markdown alone (e.g. backend logic), say so rather than inventing it.
7.  **Conclusion:** End with a brief summary if appropriate for "${page.title}".
${introStructureBlock}${relatedPagesBlock}${pagePriorityNote}
IMPORTANT: Generate the content in ${pageLanguageLine} language.` : `${langInstructionTop}
You are an expert wiki writer creating a content wiki about a website's subject matter (not its technical implementation).
Your task is to generate a comprehensive and accurate wiki page in Markdown format about a specific topic within the site's content, the way the site itself organizes that topic.

You will be given:
1. The "[WIKI_PAGE_TOPIC]" for the page you need to create.
2. A list of "[RELEVANT_PAGES]" -- crawled pages from the site (converted to Markdown) that are your sole source material. There may be as few as one; use what's provided rather than demanding more.

CRITICAL STARTING INSTRUCTION:
The very first thing on the page MUST be a \`<details>\` block listing the \`[RELEVANT_PAGES]\` you used. Format it exactly like this:
<details>
<summary>${summaryLabel}</summary>

Remember, do not provide any acknowledgements, disclaimers, apologies, or any other preface before the \`<details>\` block. JUST START with the \`<details>\` block.
The following pages were used as context for generating this wiki page:

${filePaths.map(path => `- [${path}](${generateFileUrl(path)})`).join('\n')}
</details>

Immediately after the \`<details>\` block, the main title of the page should be a H1 Markdown heading: \`# ${page.title}\`.

Based ONLY on the content of the \`[RELEVANT_PAGES]\`:

1.  **Introduction:** Start with a concise introduction (1-2 paragraphs) covering "${page.title}" as the site itself presents it.
2.  **Detailed Sections:** Break down "${page.title}" into logical sections using H2 (\`##\`) and H3 (\`###\`) headings, organized the way the source pages themselves organize this subject matter.
3.  **Tables:** Use Markdown tables where they help summarize lists of facts, comparisons, or attributes covered by the source pages.
4.  **Diagrams (optional):** If the subject matter has a natural hierarchy, taxonomy, or set of relationships that a diagram would clarify, use a Mermaid \`graph TD\` (top-down, never \`graph LR\`) diagram. Quote any label containing punctuation, e.g. A["Genus (Family)"]. Only add a diagram when it genuinely helps -- never force one where a list would do.
5.  **Citations:** Cite the specific crawled page path(s) each claim is drawn from, e.g. \`Sources: [path/to/page.md]()\`. Cite as many of the provided pages as are actually relevant -- do not pad citations to hit an arbitrary count.
6.  **Accuracy:** Do not invent facts beyond what the source pages state. Do NOT analyze the site's own technical implementation (no HTML/CSS/framework talk) -- write about the subject matter itself.
7.  **Conclusion:** End with a brief summary if appropriate for "${page.title}".
${introStructureBlock}${relatedPagesBlock}${pagePriorityNote}
IMPORTANT: Generate the content in ${pageLanguageLine} language.`) : (isUserFocusedView ? `${langInstructionTop}
You are an expert technical writer creating END-USER documentation for a software product.
Your audience has NO interest in source code -- they just want to install, configure, and use this software effectively. Never discuss architecture, internal implementation, source code organization, or anything a user would never need to know to use the product.

You will be given:
1. The "[WIKI_PAGE_TOPIC]" for the page you need to create.
2. A list of "[RELEVANT_SOURCE_FILES]" -- mostly documentation (README, docs/, examples, config samples) but occasionally a source file where a user-facing option is only documented in code (e.g. a CLI flags definition or a config schema). Use these as your sole basis for the content; you have access to their full text.

CRITICAL STARTING INSTRUCTION:
The very first thing on the page MUST be a \`<details>\` block listing the \`[RELEVANT_SOURCE_FILES]\` you used. Format it exactly like this:
<details>
<summary>${summaryLabel}</summary>

Remember, do not provide any acknowledgements, disclaimers, apologies, or any other preface before the \`<details>\` block. JUST START with the \`<details>\` block.
The following files were used as context for generating this wiki page:

${filePaths.map(path => `- [${path}](${generateFileUrl(path)})`).join('\n')}
</details>

Immediately after the \`<details>\` block, the main title of the page should be a H1 Markdown heading: \`# ${page.title}\`.

Based ONLY on the content of the \`[RELEVANT_SOURCE_FILES]\`:

1.  **Introduction:** Start with a concise introduction (1-2 paragraphs) explaining what "${page.title}" lets the user do and why they'd want it, in plain language -- no jargon that assumes a developer background.

2.  **Step-by-step instructions:** Break "${page.title}" down into clear, numbered steps or logical H2 (\`##\`)/H3 (\`###\`) sections a user can follow in order (e.g. "1. Install the CLI", "2. Set your API key"). Prefer concrete, actionable instructions ("Run \`x\` to do \`y\`", "Open Settings and toggle \`z\`") over abstract descriptions.

3.  **Configuration/options:** If this topic has settings, flags, environment variables, or config file options, list them in a Markdown table: name, what it does, default value, and an example value -- described in terms of their EFFECT on the user's experience, not their internal handling.

4.  **Diagrams (OPTIONAL, sparingly):** Only if it genuinely clarifies a multi-step USER workflow (e.g. "install -> configure -> run"), a simple Mermaid \`graph TD\` (top-down, never \`graph LR\`) with plain-language node labels is fine. Do NOT diagram code architecture, class hierarchies, or internal data flow -- that's not what this page is for. Quote any label containing punctuation, e.g. A["Settings (Advanced)"].

5.  **Common issues / troubleshooting:** If the source files mention error messages, common mistakes, or requirements/prerequisites, call them out explicitly (e.g. "If you see X, it usually means Y -- fix it by Z").

6.  **Source Citations:** Cite the specific file(s) each claim is drawn from, e.g. \`Sources: [README.md](), [docs/config.md]()\`. Whole-file citations are fine here (no line-number ranges needed) -- this is user-facing documentation, not a code walkthrough.

7.  **Accuracy:** Only state what's actually evidenced in the provided files. Do not invent features, flags, or behavior that isn't documented. If something a user would want to know isn't covered by the provided files, say so rather than guessing.

8.  **Tone:** Clear, friendly, and precise -- written for someone who downloaded/installed this software and wants to get value out of it quickly, not someone evaluating its codebase.

9.  **Conclusion:** End with a brief summary or "next steps" pointer if appropriate for "${page.title}".
${introStructureBlock}${relatedPagesBlock}${pagePriorityNote}
IMPORTANT: Generate the content in ${pageLanguageLine} language.

Remember:
- Ground every instruction in the provided files -- never invent a flag, setting, or behavior.
- Write for someone using the software, not someone reading or modifying its source code.
- Structure the document as a practical, actionable guide.
` : `${langInstructionTop}
You are an expert technical writer and software architect.
Your task is to generate a comprehensive and accurate technical wiki page in Markdown format about a specific feature, system, or module within a given software project.

You will be given:
1. The "[WIKI_PAGE_TOPIC]" for the page you need to create.
2. A list of "[RELEVANT_SOURCE_FILES]" from the project that you MUST use as the sole basis for the content. You have access to the full content of these files. Use ALL of the provided files -- they were chosen for this page. If only one or two were provided, that is the complete set; do NOT invent or cite any file path that was not provided, and do NOT claim to have used a file you were not given.

CRITICAL STARTING INSTRUCTION:
The very first thing on the page MUST be a \`<details>\` block listing exactly the \`[RELEVANT_SOURCE_FILES]\` you were given (no more, no less).
Format it exactly like this:
<details>
<summary>${summaryLabel}</summary>

Remember, do not provide any acknowledgements, disclaimers, apologies, or any other preface before the \`<details>\` block. JUST START with the \`<details>\` block.
The following files were used as context for generating this wiki page:

${filePaths.map(path => `- [${path}](${generateFileUrl(path)})`).join('\n')}
</details>

Immediately after the \`<details>\` block, the main title of the page should be a H1 Markdown heading: \`# ${page.title}\`.

Based ONLY on the content of the \`[RELEVANT_SOURCE_FILES]\`:

1.  **Introduction:** Start with a concise introduction (1-2 paragraphs) explaining the purpose, scope, and high-level overview of "${page.title}" within the context of the overall project. If relevant, and if information is available in the provided files, link to other potential wiki pages using the format \`[Link Text](#page-anchor-or-id)\`.

2.  **Detailed Sections:** Break down "${page.title}" into logical sections using H2 (\`##\`) and H3 (\`###\`) Markdown headings. For each section:
    *   Explain the architecture, components, data flow, or logic relevant to the section's focus, as evidenced in the source files.
    *   Identify key functions, classes, data structures, API endpoints, or configuration elements pertinent to that section.

3.  **Mermaid Diagrams:**
    *   EXTENSIVELY use Mermaid diagrams (e.g., \`flowchart TD\`, \`sequenceDiagram\`, \`classDiagram\`, \`erDiagram\`, \`graph TD\`) to visually represent architectures, flows, relationships, and schemas found in the source files.
    *   Ensure diagrams are accurate and directly derived from information in the \`[RELEVANT_SOURCE_FILES]\`.
    *   Provide a brief explanation before or after each diagram to give context.
    *   CRITICAL: All diagrams MUST follow strict vertical orientation:
       - Use "graph TD" (top-down) directive for flow diagrams
       - NEVER use "graph LR" (left-right)
       - Maximum node width should be 3-4 words
       - Quote every flowchart label that contains parentheses, brackets, colons, URLs, or other punctuation. Example: A["Flask (app.py)"].
       - For sequence diagrams:
         - Start with "sequenceDiagram" directive on its own line
         - Define ALL participants at the beginning using "participant" keyword
         - Optionally specify participant types: actor, boundary, control, entity, database, collections, queue
         - Use descriptive but concise participant names, or use aliases: "participant A as Alice"
         - Use the correct Mermaid arrow syntax (8 types available):
           - -> solid line without arrow (rarely used)
           - --> dotted line without arrow (rarely used)
           - ->> solid line with arrowhead (most common for requests/calls)
           - -->> dotted line with arrowhead (most common for responses/returns)
           - ->x solid line with X at end (failed/error message)
           - -->x dotted line with X at end (failed/error response)
           - -) solid line with open arrow (async message, fire-and-forget)
           - --) dotted line with open arrow (async response)
           - Examples: A->>B: Request, B-->>A: Response, A->xB: Error, A-)B: Async event
         - Use +/- suffix for activation boxes: A->>+B: Start (activates B), B-->>-A: End (deactivates B)
         - Group related participants using "box": box GroupName ... end
         - Use structural elements for complex flows:
           - loop LoopText ... end (for iterations)
           - alt ConditionText ... else ... end (for conditionals)
           - opt OptionalText ... end (for optional flows)
           - par ParallelText ... and ... end (for parallel actions)
           - critical CriticalText ... option ... end (for critical regions)
           - break BreakText ... end (for breaking flows/exceptions)
         - Add notes for clarification: "Note over A,B: Description", "Note right of A: Detail"
         - Use autonumber directive to add sequence numbers to messages
         - NEVER use flowchart-style labels like A--|label|-->B. Always use a colon for labels: A->>B: My Label
    *   Few-shot examples (match this style exactly):
        \`\`\`mermaid
        graph TD
          A["Client (UI)"] --> B["API Gateway"]
          B --> C["Auth Service"]
          B --> D["Item Service"]
          C --> E[("User DB")]
          D --> F[("Item DB")]
        \`\`\`
        \`\`\`mermaid
        sequenceDiagram
          participant U as User
          participant API as API Gateway
          participant S as Item Service
          participant DB as Item DB
          U->>API: GET /items/:id
          API->>S: getItem(id)
          S->>DB: SELECT * FROM items
          DB-->>S: row
          S-->>API: item DTO
          API-->>U: 200 OK + JSON
        \`\`\`

4.  **Tables:**
    *   Use Markdown tables to summarize information such as:
        *   Key features or components and their descriptions.
        *   API endpoint parameters, types, and descriptions.
        *   Configuration options, their types, and default values.
        *   Data model fields, types, constraints, and descriptions.

5.  **Code Snippets (ENTIRELY OPTIONAL):**
    *   Include short, relevant code snippets (e.g., Python, Java, JavaScript, SQL, JSON, YAML) directly from the \`[RELEVANT_SOURCE_FILES]\` to illustrate key implementation details, data structures, or configurations.
    *   Ensure snippets are well-formatted within Markdown code blocks with appropriate language identifiers.

6.  **Source Citations (EXTREMELY IMPORTANT):**
    *   For EVERY piece of significant information, explanation, diagram, table entry, or code snippet, you MUST cite the specific source file(s) from which the information was derived. Only cite files that were actually provided in [RELEVANT_SOURCE_FILES] -- never invent a file path or cite a file you were not given.
    *   Place citations at the end of the paragraph, under the diagram/table, or after the code snippet.
    *   Prefer the format \`Sources: [filename.ext]()\` for a whole file. If you can confidently identify the exact line range in the provided file, use \`Sources: [filename.ext:start_line-end_line]()\` -- but only when you are certain of the lines; do NOT fabricate line numbers, and if you are unsure, cite the whole file instead.
    *   Multiple files can be cited: \`Sources: [file1.ext](), [file2.ext](), [dir/file3.ext]()\`.
    *   If an entire section is overwhelmingly based on one or two files, you can cite them under the section heading in addition to more specific citations within the section.
    *   Cite as many of the provided files as are actually relevant -- do not pad citations to hit an arbitrary count, and do not cite a file you did not use.

7.  **Technical Accuracy:** All information must be derived SOLELY from the \`[RELEVANT_SOURCE_FILES]\`. Do not infer, invent, or use external knowledge about similar systems or common practices unless it's directly supported by the provided code. If information is not present in the provided files, do not include it or explicitly state its absence if crucial to the topic.

8.  **Clarity and Conciseness:** Use clear, professional, and concise technical language suitable for other developers working on or learning about the project. Avoid unnecessary jargon, but use correct technical terms where appropriate.

9.  **Conclusion/Summary:** End with a brief summary paragraph if appropriate for "${page.title}", reiterating the key aspects covered and their significance within the project.
${introStructureBlock}${relatedPagesBlock}${pagePriorityNote}
IMPORTANT: Generate the content in ${pageLanguageLine} language.

Remember:
- Ground every claim in the provided source files.
- Prioritize accuracy and direct representation of the code's functionality and structure.
- Structure the document logically for easy understanding by other developers.
`);

        // Prepare request body
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const requestBody: Record<string, any> = {
          repo_url: repoUrl,
          type: effectiveRepoInfo.type,
          // One-shot page generation, not a chat -- the agent tool-calling
          // loop (and its out-of-band process-event frames) has no business
          // here and previously leaked raw FDW control frames straight into
          // saved wiki content when this wasn't set.
          enable_tool_calling: false,
          // Deliberately NOT force_refresh here, even on a refresh: the
          // structure-planning request in determineWikiStructure (which
          // always runs immediately before this, once per refresh) already
          // sends force_refresh and rebuilds the embeddings index fresh on
          // disk. Every page's /ws/chat request creates its own RAG instance
          // and calls prepare_retriever regardless, but that just *loads*
          // the (already fresh) .pkl -- forcing a rebuild here too would
          // re-embed the entire repo from scratch again for every single
          // page (N pages = N full re-embeds instead of 1).
          retrieval_query: [
            page.title,
            page.content,
            `Relevant files: ${filePaths.join(', ')}`,
          ].filter(Boolean).join('\n'),
          // B10: keep RAG context focused on the page's relevant_files so
          // the model doesn't get pulled toward semantically similar but
          // off-topic chunks from unrelated files. Falls back to unfiltered
          // server-side if too few chunks match.
          filter_file_paths: filePaths,
          messages: [{
            role: 'user',
            content: promptContent
          }]
        };

        // Add tokens if available
        addTokensToRequestBody(requestBody, currentToken, effectiveRepoInfo.type, selectedProviderState, selectedModelState, isCustomSelectedModelState, customSelectedModelState, language, modelExcludedDirs, modelExcludedFiles, modelIncludedDirs, modelIncludedFiles);

        // Use WebSocket for communication
        let content = '';

        try {
          const wsUrl = await getBackendWebSocketUrl('/ws/chat');

          // Create a new WebSocket connection
          const ws = new WebSocket(wsUrl);

          // Create a promise that resolves when the WebSocket connection is complete
          await new Promise<void>((resolve, reject) => {
            let connectionAborted = false;

            ws.onerror = (error) => {
              connectionAborted = true;
              console.error('WebSocket error:', error);
              reject(new Error('WebSocket connection failed'));
            };

            // Limit only the connection handshake, never model inference.
            const timeout = setTimeout(() => {
              connectionAborted = true;
              reject(new Error('WebSocket connection timeout'));
            }, WEBSOCKET_CONNECT_TIMEOUT_MS);

            // Clear the timeout if the connection opens successfully
            ws.onopen = () => {
              clearTimeout(timeout);
              if (connectionAborted) {
                ws.close();
                return;
              }
              console.log(`WebSocket connection established for page: ${page.title}`);
              // Send the request as JSON
              ws.send(JSON.stringify(requestBody));
              resolve();
            };
          });

          // Create a promise that resolves when the WebSocket response is complete
          const pageStreamParser = new StreamParser();
          await new Promise<void>((resolve, reject) => {
            // Idle (not total) timeout, reset on every chunk actually
            // received -- see the matching comment on the wiki-structure
            // request above for why this exists (a stream can genuinely
            // stall mid-response with no further data and no close frame,
            // and pages generate one at a time, so a single stalled page
            // would otherwise wedge the entire remaining queue forever).
            const IDLE_TIMEOUT_MS = 5 * 60 * 1000;
            let idleTimer: ReturnType<typeof setTimeout>;
            const resetIdleTimer = () => {
              clearTimeout(idleTimer);
              idleTimer = setTimeout(() => {
                console.warn(`Page "${page.title}" stream went idle -- no data received for 5 minutes.`);
                try { ws.close(); } catch {}
                reject(new Error(
                  language === 'es'
                    ? (messages.repoPage?.modelTimeoutError || 'Model stopped responding (no new data for 5 minutes).')
                    : 'The model stopped responding (no new data for 5 minutes).'
                ));
              }, IDLE_TIMEOUT_MS);
            };
            resetIdleTimer();

            // Handle incoming messages. Even with enable_tool_calling: false
            // above, a reasoning model's thinking tokens can still arrive
            // wrapped as out-of-band process frames -- strip them here too
            // so they never end up saved as page content.
            ws.onmessage = (event) => {
              resetIdleTimer();
              content += pageStreamParser.feed(event.data).text;
            };

            // Handle WebSocket close
            ws.onclose = () => {
              clearTimeout(idleTimer);
              console.log(`WebSocket connection closed for page: ${page.title}`);
              resolve();
            };

            // Handle WebSocket errors
            ws.onerror = (error) => {
              clearTimeout(idleTimer);
              console.error('WebSocket error during message reception:', error);
              reject(new Error('WebSocket error during message reception'));
            };
          });
        } catch (wsError) {
          console.error('WebSocket error, falling back to HTTP:', wsError);

          // Fall back to HTTP if WebSocket fails
          const response = await fetch(`/api/chat/stream`, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
            },
            body: JSON.stringify(requestBody)
          });

          if (!response.ok) {
            const errorText = await response.text().catch(() => 'No error details available');
            console.error(`API error (${response.status}): ${errorText}`);
            throw new Error(`Error generating page content: ${response.status} - ${response.statusText}`);
          }

          // Process the response
          content = '';
          const reader = response.body?.getReader();
          const decoder = new TextDecoder();
          const pageStreamParserHttp = new StreamParser();

          if (!reader) {
            throw new Error('Failed to get response reader');
          }

          try {
            while (true) {
              const { done, value } = await reader.read();
              if (done) break;
              content += pageStreamParserHttp.feed(decoder.decode(value, { stream: true })).text;
            }
            // Ensure final decoding
            content += pageStreamParserHttp.feed(decoder.decode()).text;
          } catch (readError) {
            console.error('Error reading stream:', readError);
            throw new Error('Error processing response stream');
          }
        }

        // Clean up markdown delimiters
        content = content.replace(/^```markdown\s*/i, '').replace(/```\s*$/i, '');

        console.log(`Received content for ${page.title}, length: ${content.length} characters`);

        // A16: empty-body guard. A provider hiccup (rate-limit blip, model
        // emitting only a stop token) can return an empty stream. Saving
        // that would silently wipe the page's placeholder/error state and
        // the user would export an empty wiki with no clue why. Mark the
        // page with an explicit inline error instead, so the user can
        // retry that one page without losing the rest of the wiki.
        if (!content.trim()) {
          console.warn(`Page "${page.title}" came back empty -- treating as a generation failure.`);
          setGeneratedPages(prev => ({
            ...prev,
            [page.id]: { ...page, content: language === 'es'
              ? 'Error: el modelo no devolvió contenido para esta página. Usa "Reintentar" o regenera la página.'
              : 'Error: the model returned no content for this page. Use "Retry" or regenerate the page.' }
          }));
          resolve();
          return;
        }

        // Store the FINAL generated content
        const updatedPage = { ...page, content };
        setGeneratedPages(prev => ({ ...prev, [page.id]: updatedPage }));
        // Store this as the original for potential mermaid retries
        setOriginalMarkdown(prev => ({ ...prev, [page.id]: content }));

        resolve();
      } catch (err) {
        console.error(`Error generating content for page ${page.id}:`, err);
        const errorMessage = err instanceof Error ? err.message : 'Unknown error';
        // Update page state to show error inline on the specific page.
        // IMPORTANT: do NOT call setError() here. setError() triggers the full-screen
        // error UI (which shows the misleading "repository does not exist" fallback) and
        // hides the entire wiki — including the pages that generated successfully — which
        // prevents the user from saving/exporting the rest of the wiki. A single failed
        // page is shown inline via its content below; the wiki view stays usable.
        setGeneratedPages(prev => ({
          ...prev,
          [page.id]: { ...page, content: `Error generating content: ${errorMessage}` }
        }));
        resolve(); // Resolve even on error to unblock queue
      } finally {
        // Clear the processing flag for this page
        // This must happen in the finally block to ensure the flag is cleared
        // even if an error occurs during processing
        activeContentRequests.delete(page.id);

        // Mark page as done
        setPagesInProgress(prev => {
          const next = new Set(prev);
          next.delete(page.id);
          return next;
        });
        setLoadingMessage(undefined); // Clear specific loading message
      }
    });
  }, [generatedPages, currentToken, effectiveRepoInfo, selectedProviderState, selectedModelState, isCustomSelectedModelState, customSelectedModelState, modelExcludedDirs, modelExcludedFiles, modelIncludedDirs, modelIncludedFiles, technicalAnalysisEnabled, isUserFocusedView, focusInstructions, language, activeContentRequests, generateFileUrl, wikiStructure, repoFileTree]);

  // Determine the wiki structure from repository data
  const determineWikiStructure = useCallback(async (fileTree: string, readme: string, owner: string, repo: string, force: boolean = false) => {
    if (!owner || !repo) {
      setError('Invalid repository information. Owner and repo name are required.');
      setIsLoading(false);
      setEmbeddingError(false); // Reset embedding error state
      return;
    }

    // Skip if structure request is already in progress
    if (structureRequestInProgress) {
      console.log('Wiki structure determination already in progress, skipping duplicate call');
      return;
    }

    try {
      setStructureRequestInProgress(true);
      setLoadingMessage(messages.loading?.determiningStructure || 'Determining wiki structure...');
      setRepoFileTree(fileTree);

      // Get repository URL
      const repoUrl = getRepoUrl(effectiveRepoInfo);

      // 🌐 Website wikis have two entirely different generation modes (not a
      // section toggle): by default the wiki is ABOUT the site's subject
      // matter (a fan wiki crawl produces a fan wiki, not a report on the
      // fan wiki's HTML); Technical Analysis mode instead produces a wiki
      // ABOUT the site itself (architecture, page structure, tech stack) the
      // same way a code repo wiki documents a codebase. User-generated pages
      // (profiles/comments/forum posts -- flagged in the crawl manifest) are
      // always excluded entirely, never generated as wiki pages and never
      // mixed into the subject/technical wiki's sections.
      // 'fanwiki' (imported MediaWiki XML dump) gets the same treatment as
      // 'website' here for the same reason as isWebsitePage above -- it's
      // page-based fan/content wiki material already, not a codebase.
      const isWebsite = effectiveRepoInfo.type === 'website' || effectiveRepoInfo.type === 'fanwiki';

      const languageLine = `${language === 'en' ? 'English' :
            language === 'ja' ? 'Japanese (日本語)' :
            language === 'zh' ? 'Mandarin Chinese (中文)' :
            language === 'zh-tw' ? 'Traditional Chinese (繁體中文)' :
            language === 'es' ? 'Spanish (Español)' :
            language === 'kr' ? 'Korean (한국어)' :
            language === 'vi' ? 'Vietnamese (Tiếng Việt)' :
            language === "pt-br" ? "Brazilian Portuguese (Português Brasileiro)" :
            language === "fr" ? "Français (French)" :
            language === "ru" ? "Русский (Russian)" :
            'English'}`;

      // Mode-specific opening: what the source material is and what the LLM
      // should (and should not) focus on. Everything after this (language,
      // diagram guidance, suggested sections, XML schema, closing rules) is
      // shared between repo and website prompts.
      const subjectIntro = isWebsite
        ? `Analyze this crawled website (${repo}) and create a wiki structure for it.

1. The crawled page tree (each entry is one page's local Markdown file, its path mirrors the site's own URL structure; front matter on each file has the original URL and a "likely_user_content" hint -- pages flagged likely_user_content are profile/comment/forum pages and MUST be excluded from this structure entirely; do not create wiki pages for them and do not summarize or reference their content anywhere):
<file_tree>
${fileTree}
</file_tree>

${technicalAnalysisEnabled
  ? `I want a TECHNICAL wiki analyzing this website's own structure, architecture, and technology -- e.g. how its pages are organized, what stack/CMS/framework signals are visible, navigation structure, page templates and layout patterns. Do NOT write about the website's subject-matter content itself; focus entirely on the site as a technical artifact.`
  : `I want a CONTENT wiki about this website's subject matter -- e.g. if this is a fan wiki about a game, produce a fan wiki about that game's content, organized the way the site itself organizes its topics. Do NOT analyze the website's own technical implementation (no HTML/CSS/framework talk); write as if documenting the subject the site is about, using the crawled pages as your source material. Mirror the site's own page/section structure where sensible.`}

Determine the most logical structure for this wiki based on the crawled content.`
        : `Analyze this ${effectiveRepoInfo.type === 'gitlab' ? 'GitLab' : effectiveRepoInfo.type === 'bitbucket' ? 'Bitbucket' : 'GitHub'} repository ${owner}/${repo} and create a wiki structure for it.

1. The complete file tree of the project:
<file_tree>
${fileTree}
</file_tree>

2. The README file of the project:
<readme>
${readme}
</readme>

${isUserFocusedView
  ? `I want to create an END-USER GUIDE for this software -- documentation for people who will INSTALL, CONFIGURE, and USE it, not people who will read or modify its source code. Use the file tree and README only to understand what the software does, how it's installed/configured (look for config files, CLI entry points, env vars, setup scripts), and what user-facing features exist. Determine the most logical structure for a user guide: installation/setup, configuration, features and how to use them, common workflows, and troubleshooting. Do NOT plan any pages about internal architecture, source code organization, or implementation details.`
  : `I want to create a wiki for this repository. Determine the most logical structure for a wiki based on the repository's content.`}`;

      const comprehensiveSections = isWebsite
        ? (technicalAnalysisEnabled
            ? `Create a structured wiki with sections such as:
- Overview (what the site is, and this analysis' scope)
- Site Architecture (page structure, navigation, routing patterns)
- Technology Stack (frameworks/CMS/libraries detected)
- Page Templates & Layout Patterns
- Notable Technical Features

Each section should contain relevant pages.`
            : `Create a structured wiki with sections that mirror how the site itself organizes its subject matter (do not force the generic repo-documentation sections below onto site content -- infer the sections from what the site is actually about).

Each section should contain relevant pages.`)
        : (isUserFocusedView
            ? `Create a structured user guide with sections such as:
- Getting Started (what the software does, requirements, installation/setup)
- Configuration (config files, environment variables, CLI flags/options, settings)
- Features (each major user-facing feature and what it's for)
- Common Workflows (step-by-step guides for the most common tasks a user would want to do)
- Troubleshooting & FAQ (common problems, error messages, and how to resolve them)

Each section should contain relevant pages. Do NOT include sections about system architecture, source code structure, internal APIs, or how the software is implemented -- this wiki is for people USING the software, not developing it.`
            : `Create a structured wiki with the following main sections:
- Overview (general information about the project)
- System Architecture (how the system is designed)
- Core Features (key functionality)
- Data Management/Flow: If applicable, how data is stored, processed, accessed, and managed (e.g., database schema, data pipelines, state management).
- Frontend Components (UI elements, if applicable.)
- Backend Systems (server-side components)
- Model Integration (AI model connections)
- Deployment/Infrastructure (how to deploy, what's the infrastructure like)
- Extensibility and Customization: If the project architecture supports it, explain how to extend or customize its functionality (e.g., plugins, theming, custom modules, hooks).

Each section should contain relevant pages. For example, the "Frontend Components" section might include pages for "Home Page", "Repository Wiki Page", "Ask Component", etc.`);

      const relevantFilesNote = isWebsite
        ? `The relevant_files should be actual crawled page file paths (from the file tree above) that would be used to generate that page. List at most ${MAX_RELEVANT_FILES_PER_PAGE} pages -- pick the most representative ones rather than every match`
        : isUserFocusedView
          ? `The relevant_files should be documentation/config files (README, docs/, config examples, CLI help, .env.example, etc.) that would be used to generate that page -- avoid pure source-code implementation files unless they're the only place a user-facing option is documented. List at most ${MAX_RELEVANT_FILES_PER_PAGE} files -- pick the most representative ones rather than every match`
          : `The relevant_files should be actual files from the repository that would be used to generate that page. List at most ${MAX_RELEVANT_FILES_PER_PAGE} files -- pick the most representative ones rather than every match`;
      const pageFocusNote = isWebsite
        ? (technicalAnalysisEnabled
            ? 'Each page should focus on a specific technical aspect of the website (e.g., a page template, a subsystem, navigation)'
            : 'Each page should focus on a specific topic within the site\'s subject matter, the way the site itself would organize it')
        : isUserFocusedView
          ? 'Each page should focus on a specific end-user concern (e.g., a feature, a setup step, a workflow, a troubleshooting topic) -- never an internal implementation detail'
          : 'Each page should focus on a specific aspect of the codebase (e.g., architecture, key features, setup)';
      const wikiForNoun = isWebsite ? 'website' : 'repository';

      // Real projects/sites are rarely uniform in complexity -- a game's
      // core simulation systems, a library's central API, or a site's main
      // feature area usually deserve far more depth than a one-line install
      // step or a boilerplate config file, but the model previously had no
      // signal to weigh that by and could easily give both equal space.
      // Prefer the user's own stated priorities when given; otherwise tell
      // the model explicitly to judge relative complexity/importance itself
      // rather than defaulting to even coverage.
      const userPriorityNote = focusInstructions.trim()
        ? `\n\nUSER-SPECIFIED PRIORITIES -- the user has given the following guidance on what deserves the most attention in this wiki. Weight page count, section depth, and content length accordingly: give substantially more space to topics they flag as important/complex, and keep topics they flag as minor/trivial brief (a short section or a single concise page, not a full page each). If something isn't mentioned, use your own judgment.\n<user_priorities>\n${focusInstructions.trim()}\n</user_priorities>`
        : `\n\nNo specific priorities were given. Analyze the ${wikiForNoun} yourself: identify which subsystems/topics are the most complex, central, or heavily used, and allocate noticeably more pages/depth to those. Keep simple, boilerplate, or one-off topics (e.g. basic installation, a trivial config file) brief -- a short section or a single concise page, not the same depth as a core subsystem. Not every topic deserves equal space.`;

      // Prepare request body
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const requestBody: Record<string, any> = {
        repo_url: repoUrl,
        type: effectiveRepoInfo.type,
        // One-shot structure determination, not a chat -- same reasoning as
        // the page-generation request body above.
        enable_tool_calling: false,
        force_refresh: force,
        retrieval_query: isWebsite
          ? (technicalAnalysisEnabled
              ? `Plan a ${isComprehensiveView ? 'comprehensive' : 'concise'} ${pageCount}-page technical wiki analyzing the ${repo} website's own architecture, page structure, and technology -- not its subject content.`
              : `Plan a ${isComprehensiveView ? 'comprehensive' : 'concise'} ${pageCount}-page wiki documenting the subject matter of the ${repo} website (its content, topics, and information), structured to mirror how the site itself is organized. Do not analyze the site's technical implementation.`)
          : isUserFocusedView
            ? `Plan a ${isComprehensiveView ? 'comprehensive' : 'concise'} ${pageCount}-page end-user guide for ${owner}/${repo}. Focus on installation, configuration, CLI usage, environment variables, and features a user needs to run the software -- not its internal architecture or source code. Retrieve README, docs/, config samples, .env.example, and CLI help text.`
            : `Plan a ${isComprehensiveView ? 'comprehensive' : 'concise'} ${pageCount}-page technical wiki for ${owner}/${repo}. Focus on architecture, features, data flow, deployment, and the files named in the repository tree.`,
        messages: [{
          role: 'user',
content: `${subjectIntro}

IMPORTANT: The wiki content will be generated in ${languageLine} language.

When designing the wiki structure, include pages that would benefit from visual diagrams, such as:
- Architecture overviews
- Data flow descriptions
- Component relationships
- Process workflows
- State machines
- Class hierarchies
${userPriorityNote}

${isComprehensiveView ? `
${comprehensiveSections}

IMPORTANT -- section nesting: keep the section hierarchy SHALLOW. Aim for at most 3 levels (a top-level section, its subsections, and at most one level of subsections beneath those) -- only go one level deeper than that (4 total) if the wiki is genuinely large/comprehensive and truly needs it. Never chain deeper than 4 levels under any circumstances. Do NOT chain sections into a long line (section -> subsection -> subsection -> subsection -> ...); group related sections as siblings under the same shallow parent instead. Most sections should simply be top-level with no subsections at all.

Return your analysis in the following XML format:

<wiki_structure>
  <title>[Overall title for the wiki]</title>
  <description>[Brief description of the ${wikiForNoun}]</description>
  <sections>
    <section id="section-1">
      <title>[Section title]</title>
      <pages>
        <page_ref>page-1</page_ref>
        <page_ref>page-2</page_ref>
      </pages>
      <subsections>
        <section_ref>section-2</section_ref>
      </subsections>
    </section>
    <!-- More sections as needed. Remember: section-2 above is a leaf here --
         it must not itself declare a <subsections> block. -->
  </sections>
  <pages>
    <page id="page-1">
      <title>[Page title]</title>
      <description>[Brief description of what this page will cover]</description>
      <importance>high|medium|low</importance>
      <relevant_files>
        <file_path>[Path to a relevant file]</file_path>
        <!-- More file paths as needed -->
      </relevant_files>
      <related_pages>
        <related>page-2</related>
        <!-- More related page IDs as needed -->
      </related_pages>
      <parent_section>section-1</parent_section>
    </page>
    <!-- More pages as needed -->
  </pages>
</wiki_structure>
` : `
Return your analysis in the following XML format:

<wiki_structure>
  <title>[Overall title for the wiki]</title>
  <description>[Brief description of the ${wikiForNoun}]</description>
  <pages>
    <page id="page-1">
      <title>[Page title]</title>
      <description>[Brief description of what this page will cover]</description>
      <importance>high|medium|low</importance>
      <relevant_files>
        <file_path>[Path to a relevant file]</file_path>
        <!-- More file paths as needed -->
      </relevant_files>
      <related_pages>
        <related>page-2</related>
        <!-- More related page IDs as needed -->
      </related_pages>
    </page>
    <!-- More pages as needed -->
  </pages>
</wiki_structure>
`}

IMPORTANT FORMATTING INSTRUCTIONS:
- Return ONLY the valid XML structure specified above
- DO NOT wrap the XML in markdown code blocks (no \`\`\` or \`\`\`xml)
- DO NOT include any explanation text before or after the XML
- Ensure the XML is properly formatted and valid
- Start directly with <wiki_structure> and end with </wiki_structure>

IMPORTANT:
1. Create exactly ${pageCount} pages that make a ${isComprehensiveView ? 'comprehensive' : 'concise'} wiki for this ${wikiForNoun}. Do not return more or fewer than ${pageCount} <page> elements.
2. ${pageFocusNote}
3. ${relevantFilesNote}
4. Return ONLY valid XML with the structure specified above, with no markdown code block delimiters`
        }]
      };

      // Add tokens if available
      addTokensToRequestBody(requestBody, currentToken, effectiveRepoInfo.type, selectedProviderState, selectedModelState, isCustomSelectedModelState, customSelectedModelState, language, modelExcludedDirs, modelExcludedFiles, modelIncludedDirs, modelIncludedFiles);

      // Use WebSocket for communication
      let responseText = '';

      try {
        const wsUrl = await getBackendWebSocketUrl('/ws/chat');

        // Create a new WebSocket connection
        const ws = new WebSocket(wsUrl);

        // Create a promise that resolves when the WebSocket connection is complete
        await new Promise<void>((resolve, reject) => {
          let connectionAborted = false;

          ws.onerror = (error) => {
            connectionAborted = true;
            console.error('WebSocket error:', error);
            reject(new Error('WebSocket connection failed'));
          };

          // Limit only the connection handshake, never model inference.
          const timeout = setTimeout(() => {
            connectionAborted = true;
            reject(new Error('WebSocket connection timeout'));
          }, WEBSOCKET_CONNECT_TIMEOUT_MS);

          // Clear the timeout if the connection opens successfully
          ws.onopen = () => {
            clearTimeout(timeout);
            if (connectionAborted) {
              ws.close();
              return;
            }
            console.log('WebSocket connection established for wiki structure');
            // Send the request as JSON
            ws.send(JSON.stringify(requestBody));
            resolve();
          };
        });

        // Create a promise that resolves when the WebSocket response is complete
        const structureStreamParser = new StreamParser();
        await new Promise<void>((resolve, reject) => {
          // Model inference has no *total* timeout by design (see
          // src/utils/timeouts.ts -- a slow local model must be allowed to
          // take as long as it needs). But with nothing bounding this at
          // all, a stream that genuinely stalls mid-response (seen in
          // practice: a cloud-hosted Ollama model's connection going quiet
          // partway through a long structure response, never sending
          // another chunk and never closing) left the user staring at the
          // loading spinner forever with no error and no way to know it had
          // died rather than just being slow. An *idle* timeout -- reset on
          // every chunk actually received -- catches that stalled case
          // without cutting off a model that's still actively, if slowly,
          // producing output.
          const IDLE_TIMEOUT_MS = 5 * 60 * 1000;
          let idleTimer: ReturnType<typeof setTimeout>;
          const resetIdleTimer = () => {
            clearTimeout(idleTimer);
            idleTimer = setTimeout(() => {
              console.warn('Wiki structure stream went idle -- no data received for 5 minutes.');
              try { ws.close(); } catch {}
              setContentGenerationError(true);
              reject(new Error(
                language === 'es'
                  ? (messages.repoPage?.modelTimeoutRetryError || 'Model stopped responding (no new data for 5 minutes). Try generating again.')
                  : 'The model stopped responding (no new data for 5 minutes). Retry the generation.'
              ));
            }, IDLE_TIMEOUT_MS);
          };
          resetIdleTimer();

          // Handle incoming messages (see the page-generation WS handler
          // above for why this still needs frame-stripping even with
          // enable_tool_calling: false).
          ws.onmessage = (event) => {
            resetIdleTimer();
            responseText += structureStreamParser.feed(event.data).text;
          };

          // Handle WebSocket close
          ws.onclose = () => {
            clearTimeout(idleTimer);
            console.log('WebSocket connection closed for wiki structure');
            resolve();
          };

          // Handle WebSocket errors
          ws.onerror = (error) => {
            clearTimeout(idleTimer);
            console.error('WebSocket error during message reception:', error);
            reject(new Error('WebSocket error during message reception'));
          };
        });
      } catch (wsError) {
        console.error('WebSocket error, falling back to HTTP:', wsError);

        // Fall back to HTTP if WebSocket fails
        const response = await fetch(`/api/chat/stream`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify(requestBody)
        });

        if (!response.ok) {
          throw new Error(`Error determining wiki structure: ${response.status}`);
        }

        // Process the response
        responseText = '';
        const reader = response.body?.getReader();
        const decoder = new TextDecoder();
        const structureStreamParserHttp = new StreamParser();

        if (!reader) {
          throw new Error('Failed to get response reader');
        }

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          responseText += structureStreamParserHttp.feed(decoder.decode(value, { stream: true })).text;
        }
      }

      if(responseText.includes('Error preparing retriever: Environment variable OPENAI_API_KEY must be set')) {
         setEmbeddingError(true);
         throw new Error('OPENAI_API_KEY environment variable is not set. Please configure your OpenAI API key.');
       }

       if(responseText.includes('Ollama model') && responseText.includes('not found')) {
         setEmbeddingError(true);
         throw new Error('The specified Ollama embedding model was not found. Please ensure the model is installed locally or select a different embedding model in the configuration.');
       }

       // Custom OpenAI-compatible provider (Novita, Together, Groq, vLLM, ...) returned an
       // error. Surface the backend's detailed message (which includes the endpoint + model +
       // cause) so a misconfigured provider can be diagnosed at a glance — e.g. it reveals when
       // the endpoint fell back to https://api.openai.com/v1 because no API endpoint URL was
       // sent, or which model was not found. This avoids falling through to "No valid XML".
       if (responseText.includes('Error with Openai API') || responseText.includes('MODEL_NOT_FOUND') || (responseText.includes('model') && responseText.includes('not found') && responseText.includes('Error'))) {
         const endpointMatch = responseText.match(/\[endpoint=([^\]]*?)\s+model=([^\]]*?)\]/);
         const endpoint = endpointMatch ? endpointMatch[1].trim() : '';
         const modelStr = endpointMatch ? endpointMatch[2].trim() : '';
         const causeMatch = responseText.match(/Error with Openai API:\s*([\s\S]*?)(?:\n\s*\[|\nPlease|$)/);
         let cause = causeMatch ? causeMatch[1].trim() : '';
         if (!cause) {
           const notFoundMatch = responseText.match(/model[:\s]+([^\s,'}]+)\s+not found/i);
           cause = notFoundMatch ? `model "${notFoundMatch[1]}" not found` : 'provider endpoint error';
         }
         const detail = [
           cause,
           endpoint ? `endpoint=${endpoint}` : '',
           modelStr ? `model=${modelStr}` : '',
         ].filter(Boolean).join(' | ');
         throw new Error(
           `The configured provider endpoint returned an error for the selected model.${detail ? ` (${detail})` : ''} Open Settings, verify the API Endpoint URL, API key, and selected model, click Reload to fetch the available models, and select a valid model for this provider.`
         );
       }

        // Clean up markdown delimiters
      responseText = responseText.replace(/^```(?:xml)?\s*/i, '').replace(/```\s*$/i, '');

      // Extract wiki structure from response
      const xmlMatch = responseText.match(/<wiki_structure>[\s\S]*?<\/wiki_structure>/m);
      if (!xmlMatch) {
        throw new Error('No valid XML found in response');
      }

      let xmlText = xmlMatch[0];
      xmlText = xmlText.replace(/[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]/g, '');
      // Try parsing with DOMParser
      const parser = new DOMParser();
      const xmlDoc = parser.parseFromString(xmlText, "text/xml");

      // Check for parsing errors
      const parseError = xmlDoc.querySelector('parsererror');
      if (parseError) {
        // Log the first few elements to see what was parsed
        const elements = xmlDoc.querySelectorAll('*');
        if (elements.length > 0) {
          console.log('First 5 element names:',
            Array.from(elements).slice(0, 5).map(el => el.nodeName).join(', '));
        }

        // We'll continue anyway since the XML might still be usable
      }

      // Extract wiki structure
      let title = '';
      let description = '';
      let pages: WikiPage[] = [];

      // Try using DOM parsing first
      const titleEl = xmlDoc.querySelector('title');
      const descriptionEl = xmlDoc.querySelector('description');
      const pagesEls = xmlDoc.querySelectorAll('page');

      title = titleEl ? titleEl.textContent || '' : '';
      description = descriptionEl ? descriptionEl.textContent || '' : '';

      // Parse pages using DOM
      pages = [];

      pagesEls.forEach(pageEl => {
        const id = pageEl.getAttribute('id') || `page-${pages.length + 1}`;
        const titleEl = pageEl.querySelector('title');
        const importanceEl = pageEl.querySelector('importance');
        const filePathEls = pageEl.querySelectorAll('file_path');
        const relatedEls = pageEl.querySelectorAll('related');

        const title = titleEl ? titleEl.textContent || '' : '';
        const importance = importanceEl ?
          (importanceEl.textContent === 'high' ? 'high' :
            importanceEl.textContent === 'medium' ? 'medium' : 'low') : 'medium';

        const filePaths: string[] = [];
        filePathEls.forEach(el => {
          if (el.textContent) filePaths.push(el.textContent);
        });

        const relatedPages: string[] = [];
        relatedEls.forEach(el => {
          if (el.textContent) relatedPages.push(el.textContent);
        });

        pages.push({
          id,
          title,
          content: '', // Will be generated later
          filePaths,
          importance,
          relatedPages
        });
      });

      // Regex fallback: strict browser XML parsing (DOMParser in "text/xml"
      // mode) treats even ONE malformed tag ANYWHERE in the document as
      // reason to give up on the rest of the tree -- verified directly
      // against the real model with real crawled data: it produced a fully
      // correct <wiki_structure> with all 10 requested <page> elements, but
      // one <section> mid-document had a mismatched tag (opened <li>, closed
      // </title> -- a normal, occasional LLM slip on deeply nested XML, nothing
      // model-quality-specific about it), and that alone made
      // querySelectorAll('page') return zero results despite every <page>
      // element being intact later in the same string. A previous version of
      // this code already recognized this failure mode (there was a
      // console.warn('...trying regex fallback') right here) but never
      // actually implemented the fallback -- it just logged and then ran the
      // same (now confirmed empty) DOM query anyway. This extracts <page>
      // blocks directly from the raw text instead, tolerant of malformed XML
      // elsewhere in the document.
      if (pages.length === 0) {
        console.warn('DOM parsing found no <page> elements (likely a malformed tag elsewhere in the model\'s XML) -- falling back to regex extraction.');
        const pageBlockRe = /<page\s+id="([^"]*)"[^>]*>([\s\S]*?)<\/page>/g;
        let pageMatch: RegExpExecArray | null;
        while ((pageMatch = pageBlockRe.exec(xmlText)) !== null) {
          const id = pageMatch[1] || `page-${pages.length + 1}`;
          const block = pageMatch[2];
          const titleMatch = block.match(/<title>([\s\S]*?)<\/title>/);
          const importanceMatch = block.match(/<importance>([\s\S]*?)<\/importance>/);
          const filePaths: string[] = [];
          const filePathRe = /<file_path>([\s\S]*?)<\/file_path>/g;
          let fpMatch: RegExpExecArray | null;
          while ((fpMatch = filePathRe.exec(block)) !== null) {
            if (fpMatch[1].trim()) filePaths.push(fpMatch[1].trim());
          }
          const relatedPages: string[] = [];
          const relatedRe = /<related>([\s\S]*?)<\/related>/g;
          let relMatch: RegExpExecArray | null;
          while ((relMatch = relatedRe.exec(block)) !== null) {
            if (relMatch[1].trim()) relatedPages.push(relMatch[1].trim());
          }
          const importanceText = importanceMatch ? importanceMatch[1].trim() : '';
          pages.push({
            id,
            title: titleMatch ? titleMatch[1].trim() : '',
            content: '',
            filePaths,
            importance: importanceText === 'high' ? 'high' : importanceText === 'low' ? 'low' : 'medium',
            relatedPages,
          });
        }
        if (pages.length > 0) {
          console.log(`Regex fallback recovered ${pages.length} page(s) DOM parsing missed.`);
        }
      }

      // Same reasoning for title/description: if DOMParser gave up before
      // reaching them (or, less likely, the malformed tag was early enough
      // to break even these), recover them from the raw text too.
      if (!title) {
        const titleMatch = xmlText.match(/<wiki_structure>\s*<title>([\s\S]*?)<\/title>/);
        if (titleMatch) title = titleMatch[1].trim();
      }
      if (!description) {
        const descMatch = xmlText.match(/<description>([\s\S]*?)<\/description>/);
        if (descMatch) description = descMatch[1].trim();
      }

      // Extract sections if they exist in the XML
      const sections: WikiSection[] = [];
      const rootSections: string[] = [];

      // Try to parse sections if we're in comprehensive view
      if (isComprehensiveView) {
        const sectionsEls = xmlDoc.querySelectorAll('section');

        if (sectionsEls && sectionsEls.length > 0) {
          // Process sections
          sectionsEls.forEach(sectionEl => {
            const id = sectionEl.getAttribute('id') || `section-${sections.length + 1}`;
            const titleEl = sectionEl.querySelector('title');
            const pageRefEls = sectionEl.querySelectorAll('page_ref');
            const sectionRefEls = sectionEl.querySelectorAll('section_ref');

            const title = titleEl ? titleEl.textContent || '' : '';
            const pages: string[] = [];
            const subsections: string[] = [];

            pageRefEls.forEach(el => {
              if (el.textContent) pages.push(el.textContent);
            });

            sectionRefEls.forEach(el => {
              if (el.textContent) subsections.push(el.textContent);
            });

            sections.push({
              id,
              title,
              pages,
              subsections: subsections.length > 0 ? subsections : undefined
            });

            // Check if this is a root section (not referenced by any other section)
            let isReferenced = false;
            sectionsEls.forEach(otherSection => {
              const otherSectionRefs = otherSection.querySelectorAll('section_ref');
              otherSectionRefs.forEach(ref => {
                if (ref.textContent === id) {
                  isReferenced = true;
                }
              });
            });

            if (!isReferenced) {
              rootSections.push(id);
            }
          });
        }

        // Same regex fallback as the <page> extraction above, and for the
        // same reason: a single malformed tag anywhere in the document
        // breaks DOMParser's whole-document tree, and sections happen to be
        // the elements most likely to contain the model's occasional slip
        // (they're deeply nested with several sibling tag types). Without
        // this, a successful page-regex-recovery still left the wiki with
        // no section/TOC grouping.
        if (sections.length === 0) {
          const sectionBlockRe = /<section\s+id="([^"]*)"[^>]*>([\s\S]*?)<\/section>/g;
          let sectionMatch: RegExpExecArray | null;
          while ((sectionMatch = sectionBlockRe.exec(xmlText)) !== null) {
            const id = sectionMatch[1] || `section-${sections.length + 1}`;
            const block = sectionMatch[2];
            const titleMatch = block.match(/<title>([\s\S]*?)<\/title>/);
            const pageRefs: string[] = [];
            const pageRefRe = /<page_ref>([\s\S]*?)<\/page_ref>/g;
            let prMatch: RegExpExecArray | null;
            while ((prMatch = pageRefRe.exec(block)) !== null) {
              if (prMatch[1].trim()) pageRefs.push(prMatch[1].trim());
            }
            const subsectionRefs: string[] = [];
            const sectionRefRe = /<section_ref>([\s\S]*?)<\/section_ref>/g;
            let srMatch: RegExpExecArray | null;
            while ((srMatch = sectionRefRe.exec(block)) !== null) {
              if (srMatch[1].trim()) subsectionRefs.push(srMatch[1].trim());
            }
            sections.push({
              id,
              title: titleMatch ? titleMatch[1].trim() : '',
              pages: pageRefs,
              subsections: subsectionRefs.length > 0 ? subsectionRefs : undefined,
            });
          }
          if (sections.length > 0) {
            const referenced = new Set(sections.flatMap(s => s.subsections || []));
            rootSections.push(...sections.filter(s => !referenced.has(s.id)).map(s => s.id));
            console.log(`Regex fallback recovered ${sections.length} section(s) DOM parsing missed.`);
          }
        }
      }

      // The LLM's XML parsed without throwing but produced zero <page>
      // elements -- previously this fell through to setting an empty-pages
      // wikiStructure and silently stopping the loading spinner with no
      // pages and no error, which reads as "generation finished" when
      // nothing was actually generated. Root cause (websites specifically):
      // the system prompt told the model it was a "coding assistant"
      // embedded in a "website repository" with access to "source code" --
      // a direct contradiction with the user-turn prompt correctly
      // describing a crawled website with no source code at all. Fixed at
      // the source in api/prompts.py (SIMPLE_CHAT_SYSTEM_PROMPT_WEBSITE)
      // and api/websocket_wiki.py. This still fails loudly instead of
      // silently in case it happens for any other reason.
      if (pages.length === 0) {
        setContentGenerationError(true);
        throw new Error(
          language === 'es'
            ? (messages.repoPage?.modelNoPagesError || 'The model did not return any pages for this wiki. Try generating again.')
            : 'The model did not return any pages for this wiki. Retry the generation.'
        );
      }

      // Create wiki structure
      const wikiStructure: WikiStructure = {
        id: 'wiki',
        title,
        description,
        pages,
        sections,
        rootSections
      };

      setWikiStructure(wikiStructure);
      setCurrentPageId(pages[0].id);

      // Start generating content for all pages with controlled concurrency
      // (pages.length > 0 guaranteed by the guard clause above).
      {
        // Mark all pages as in progress
        const initialInProgress = new Set(pages.map(p => p.id));
        setPagesInProgress(initialInProgress);

        console.log(`Starting generation for ${pages.length} pages with controlled concurrency`);

        // Maximum concurrent requests
        const MAX_CONCURRENT = 1;

        // Create a queue of pages
        const queue = [...pages];
        let activeRequests = 0;

        // Function to process next items in queue
        const processQueue = () => {
          // Process as many items as we can up to our concurrency limit
          while (queue.length > 0 && activeRequests < MAX_CONCURRENT) {
            const page = queue.shift();
            if (page) {
              activeRequests++;
              console.log(`Starting page ${page.title} (${activeRequests} active, ${queue.length} remaining)`);

              // Start generating content for this page
              generatePageContent(page, owner, repo)
                .finally(() => {
                  // When done (success or error), decrement active count and process more
                  activeRequests--;
                  console.log(`Finished page ${page.title} (${activeRequests} active, ${queue.length} remaining)`);

                  // Check if all work is done (queue empty and no active requests)
                  if (queue.length === 0 && activeRequests === 0) {
                    console.log("All page generation tasks completed.");
                    setIsLoading(false);
                    setLoadingMessage(undefined);
                  } else {
                    // Only process more if there are items remaining and we're under capacity
                    if (queue.length > 0 && activeRequests < MAX_CONCURRENT) {
                      processQueue();
                    }
                  }
                });
            }
          }

          // Additional check: If the queue started empty or becomes empty and no requests were started/active
          if (queue.length === 0 && activeRequests === 0 && pagesInProgress.size === 0) {
            // This handles the case where the queue might finish before the finally blocks fully update activeRequests
            // or if the initial queue was processed very quickly
            console.log("Queue empty and no active requests after loop, ensuring loading is false.");
            setIsLoading(false);
            setLoadingMessage(undefined);
          }
        };

        // Start processing the queue
        processQueue();
      }

    } catch (error) {
      console.error('Error determining wiki structure:', error);
      const message = error instanceof Error ? error.message : 'An unknown error occurred';
      const disconnected =
        error instanceof TypeError ||
        /NetworkError|Failed to fetch|fetch resource|WebSocket|connection (?:closed|refused|reset)/i.test(message);
      setIsLoading(false);
      setConnectionError(disconnected);
      setError(
        disconnected
          ? language === 'es'
            ? (messages.repoPage?.backendConnectionError || 'Connection to the HackDeepWiki backend was interrupted. The repository is valid; try generating again.')
            : 'The connection to the HackDeepWiki backend was interrupted. The repository is valid; retry the generation.'
          : message
      );
      setLoadingMessage(undefined);
    } finally {
      setStructureRequestInProgress(false);
    }
  }, [generatePageContent, currentToken, effectiveRepoInfo, pagesInProgress.size, structureRequestInProgress, selectedProviderState, selectedModelState, isCustomSelectedModelState, customSelectedModelState, modelExcludedDirs, modelExcludedFiles, modelIncludedDirs, modelIncludedFiles, technicalAnalysisEnabled, language, messages.loading, isComprehensiveView, isUserFocusedView, focusInstructions, pageCount]);

  // Fetch repository structure using GitHub or GitLab API
  const fetchRepositoryStructure = useCallback(async (force: boolean = false) => {
    // If a request is already in progress, don't start another one
    if (requestInProgress) {
      console.log('Repository fetch already in progress, skipping duplicate call');
      return;
    }

    // Reset previous state
    setWikiStructure(undefined);
    setCurrentPageId(undefined);
    setGeneratedPages({});
    setPagesInProgress(new Set());
    setError(null);
    setEmbeddingError(false); // Reset embedding error state
    setConnectionError(false);
    setContentGenerationError(false);

    try {
      // Set the request in progress flag
      setRequestInProgress(true);

      // Update loading state
      setIsLoading(true);
      setLoadingMessage(messages.loading?.fetchingStructure || 'Fetching repository structure...');

      let fileTreeData = '';
      let readmeContent = '';
      let structureFetchedViaBackendClone = false;

      // 🌐 Website wiki: crawl the site with headless Chromium instead of
      // cloning a git repo. This is its own branch (not folded into the
      // provider-clone block below) because it needs the crawl-scope
      // parameters, not a token, and there is no per-provider REST fallback
      // for "crawl a website" the way there is for git hosts.
      if (effectiveRepoInfo.type === 'website') {
        setLoadingMessage('Crawling website…');
        try {
          const startUrl = getRepoUrl(effectiveRepoInfo);
          const crawlResult = await fetchWebsiteStructureViaCrawl(
            startUrl,
            {
              mode: crawlScopeModeParam,
              maxPages: crawlMaxPagesParam,
              subdomains: crawlSubdomainsParam,
              respectRobots: crawlRespectRobotsParam,
            },
            force,
            (progress) => {
              setCloneProgress(progress);
              if (progress) {
                setLoadingMessage(`${progress.phase}${progress.percent ? ` ${progress.percent}%` : ''}`);
              }
            }
          );
          if (crawlResult) {
            fileTreeData = crawlResult.fileTreeData;
            readmeContent = '';
            setDefaultBranch('main');
            structureFetchedViaBackendClone = true;
          } else {
            throw new Error('Website crawl failed or returned no pages.');
          }
        } catch (err) {
          console.error('Website crawl failed:', err);
          throw err;
        } finally {
          setCloneProgress(null);
          setLoadingMessage(messages.loading?.fetchingStructure || 'Fetching repository structure...');
        }
      }

      // 📖 Imported fanwiki (MediaWiki XML export, see the "Import Fanwiki
      // XML" flow on the home page): unlike 'website', this must NEVER try
      // to crawl -- the pages are already on disk from the import step,
      // under the exact same website_local_dir(start_url) layout a live
      // crawl would use (see api.fanwiki_import's module docstring). Calling
      // the website crawl path here would re-fetch the site live (likely
      // failing against whatever originally made XML import worth doing --
      // Cloudflare, a dead/moved site, ...) and, worse, could overwrite the
      // carefully wikitext-converted import with much lower-quality raw
      // HTML-to-Markdown from the crawler. This just reads the already-
      // imported tree, the same way 'local' reads a live filesystem path.
      if (effectiveRepoInfo.type === 'fanwiki') {
        setLoadingMessage('Cargando wiki importada…');
        try {
          const startUrl = getRepoUrl(effectiveRepoInfo);
          const response = await fetch(`/api/fanwiki/structure?start_url=${encodeURIComponent(startUrl)}`);
          if (!response.ok) {
            const errorData = await response.text();
            throw new Error(`Fanwiki structure fetch failed (${response.status}): ${errorData}`);
          }
          const data = await response.json();
          fileTreeData = treeToFileList(data.tree || []);
          readmeContent = '';
          setDefaultBranch('main');
          structureFetchedViaBackendClone = true;
        } catch (err) {
          console.error('Failed to load imported fanwiki structure:', err);
          throw err;
        } finally {
          setLoadingMessage(messages.loading?.fetchingStructure || 'Fetching repository structure...');
        }
      }

      // For a hosted repo, prefer the backend's own local clone (it's
      // about to make -- or already has -- this exact clone for wiki
      // generation itself) over the provider's REST API, which is both a
      // redundant network round trip and subject to a rate limit that
      // repeatedly broke this exact step. Any failure here (WS unusable,
      // HTTP fallback also down) falls through unchanged to the original
      // per-provider logic below.
      if (effectiveRepoInfo.type === 'github' || effectiveRepoInfo.type === 'gitlab' || effectiveRepoInfo.type === 'bitbucket') {
        setLoadingMessage(messages.loading?.fetchingStructure || 'Fetching repository structure...');
        try {
          const cloneResult = await fetchRepoStructureViaBackendClone(
            getRepoUrl(effectiveRepoInfo),
            effectiveRepoInfo.type,
            currentToken,
            (progress) => {
              setCloneProgress(progress);
              if (progress) {
                setLoadingMessage(`${progress.phase}${progress.percent ? ` ${progress.percent}%` : ''}`);
              }
            },
            force
          );
          if (cloneResult) {
            fileTreeData = cloneResult.fileTreeData;
            readmeContent = cloneResult.readmeContent;
            setDefaultBranch(cloneResult.defaultBranch);
            structureFetchedViaBackendClone = true;
          }
        } catch (err) {
          console.warn('Backend repo-clone structure fetch failed, falling back to provider APIs:', err);
        } finally {
          setCloneProgress(null);
          setLoadingMessage(messages.loading?.fetchingStructure || 'Fetching repository structure...');
        }
      }

      if (!structureFetchedViaBackendClone) {
      if (effectiveRepoInfo.type === 'local' && effectiveRepoInfo.localPath) {
        try {
          const response = await fetch(`/local_repo/structure?path=${encodeURIComponent(effectiveRepoInfo.localPath)}`);

          if (!response.ok) {
            const errorData = await response.text();
            throw new Error(`Local repository API error (${response.status}): ${errorData}`);
          }

          const data = await response.json();
          fileTreeData = data.file_tree;
          readmeContent = data.readme;
          // For local repos, we can't determine the actual branch, so use 'main' as default
          setDefaultBranch('main');
        } catch (err) {
          throw err;
        }
      } else if (effectiveRepoInfo.type === 'github') {
        // GitHub API approach
        // Try to get the tree data for common branch names
        let treeData = null;
        let apiErrorDetails = '';
        let gitFallbackReadme = '';
        let gitFallbackAttempted = false;
        let githubNetworkError = false;

        // Determine the GitHub API base URL based on the repository URL
        const getGithubApiUrl = (repoUrl: string | null): string => {
          if (!repoUrl) {
            return '/api/github'; // Server-side proxy can authenticate safely
          }
          
          try {
            const url = new URL(repoUrl);
            const hostname = url.hostname;
            
            // If it's the public GitHub, use the standard API URL
            if (hostname === 'github.com') {
              return '/api/github';
            }
            
            // For GitHub Enterprise, use the enterprise API URL format
            // GitHub Enterprise API URL format: https://github.company.com/api/v3
            return `${url.protocol}//${hostname}/api/v3`;
          } catch {
            return '/api/github'; // Fallback to public GitHub proxy
          }
        };

        const githubApiBaseUrl = getGithubApiUrl(effectiveRepoInfo.repoUrl);
        // First, try to get the default branch from the repository info
        let defaultBranchLocal = null;
        try {
          const repoInfoResponse = await fetch(`${githubApiBaseUrl}/repos/${owner}/${repo}`, {
            headers: createGithubHeaders(currentToken)
          });
          
          if (repoInfoResponse.ok) {
            const repoData = await repoInfoResponse.json();
            defaultBranchLocal = repoData.default_branch;
            console.log(`Found default branch: ${defaultBranchLocal}`);
            // Store the default branch in state
            setDefaultBranch(defaultBranchLocal || 'main');
          }
        } catch (err) {
          console.warn('Could not fetch repository info for default branch:', err);
        }

        // Create list of branches to try, prioritizing the actual default branch
        const branchesToTry = defaultBranchLocal 
          ? [defaultBranchLocal, 'main', 'master'].filter((branch, index, arr) => arr.indexOf(branch) === index)
          : ['main', 'master'];

        for (const branch of branchesToTry) {
          const apiUrl = `${githubApiBaseUrl}/repos/${owner}/${repo}/git/trees/${branch}?recursive=1`;
          const headers = createGithubHeaders(currentToken);

          console.log(`Fetching repository structure from branch: ${branch}`);
          try {
            const response = await fetch(apiUrl, {
              headers
            });

            if (response.ok) {
              treeData = await response.json();
              console.log('Successfully fetched repository structure');
              break;
            } else {
              const errorData = await response.text();
              apiErrorDetails = `Status: ${response.status}, Response: ${errorData}`;

              if (response.status === 403 && !gitFallbackAttempted) {
                gitFallbackAttempted = true;
                console.log('GitHub REST limit reached; trying public Git fallback');
                const fallbackResponse = await fetch(
                  `/api/github/repository-structure?owner=${encodeURIComponent(owner)}&repo=${encodeURIComponent(repo)}`,
                  { cache: 'no-store' }
                );
                if (fallbackResponse.ok) {
                  const fallbackData = await fallbackResponse.json();
                  treeData = { tree: fallbackData.tree };
                  gitFallbackReadme = fallbackData.readme || '';
                  defaultBranchLocal = fallbackData.default_branch || 'main';
                  setDefaultBranch(defaultBranchLocal || 'main');
                  console.log('Successfully fetched repository structure through Git fallback');
                  break;
                }
                const fallbackError = await fallbackResponse.text();
                apiErrorDetails += `; Git fallback: ${fallbackError}`;
              }
              console.error(`Error fetching repository structure: ${apiErrorDetails}`);
            }
          } catch (err) {
            githubNetworkError = true;
            console.error(`Network error fetching branch ${branch}:`, err);
          }
        }

        if (!treeData || !treeData.tree) {
          if (apiErrorDetails) {
            throw new Error(`Could not fetch repository structure. API Error: ${apiErrorDetails}`);
          } else if (githubNetworkError) {
            throw new TypeError('NetworkError while contacting the HackDeepWiki GitHub proxy');
          } else {
            throw new Error('Could not fetch repository structure. Repository might not exist, be empty or private.');
          }
        }

        // Convert tree data to a string representation
        fileTreeData = treeData.tree
          .filter((item: { type: string; path: string }) => item.type === 'blob')
          .map((item: { type: string; path: string }) => item.path)
          .join('\n');

        readmeContent = gitFallbackReadme;

        // Try to fetch README.md content when the Git fallback did not.
        try {
          if (readmeContent) {
            console.log('Using README from Git fallback');
          } else {
            const headers = createGithubHeaders(currentToken);

            const readmeResponse = await fetch(`${githubApiBaseUrl}/repos/${owner}/${repo}/readme`, {
              headers
            });

            if (readmeResponse.ok) {
              const readmeData = await readmeResponse.json();
              readmeContent = atob(readmeData.content);
            } else {
              console.warn(`Could not fetch README.md, status: ${readmeResponse.status}`);
            }
          }
        } catch (err) {
          console.warn('Could not fetch README.md, continuing with empty README', err);
        }
      }
      else if (effectiveRepoInfo.type === 'gitlab') {
        // GitLab API approach
        const projectPath = extractUrlPath(effectiveRepoInfo.repoUrl ?? '')?.replace(/\.git$/, '') || `${owner}/${repo}`;
        const projectDomain = extractUrlDomain(effectiveRepoInfo.repoUrl ?? "https://gitlab.com");
        const encodedProjectPath = encodeURIComponent(projectPath);

        const headers = createGitlabHeaders(currentToken);

        /* eslint-disable-next-line @typescript-eslint/no-explicit-any */
        const filesData: any[] = [];

        try {
          // Step 1: Get project info to determine default branch
          let projectInfoUrl: string;
          let defaultBranchLocal = 'main'; // fallback
          try {
            const validatedUrl = new URL(projectDomain ?? ''); // Validate domain
            projectInfoUrl = `${validatedUrl.origin}/api/v4/projects/${encodedProjectPath}`;
          } catch (err) {
            throw new Error(`Invalid project domain URL: ${projectDomain}`);
          }
          const projectInfoRes = await fetch(projectInfoUrl, { headers });

          if (!projectInfoRes.ok) {
            const errorData = await projectInfoRes.text();
            throw new Error(`GitLab project info error: Status ${projectInfoRes.status}, Response: ${errorData}`);
          }

          const projectInfo = await projectInfoRes.json();
          defaultBranchLocal = projectInfo.default_branch || 'main';
          console.log(`Found GitLab default branch: ${defaultBranchLocal}`);
          // Store the default branch in state
          setDefaultBranch(defaultBranchLocal);

          // Step 2: Paginate to fetch full file tree
          let page = 1;
          let morePages = true;
          
          while (morePages) {
            const apiUrl = `${projectInfoUrl}/repository/tree?recursive=true&per_page=100&page=${page}`;
            const response = await fetch(apiUrl, { headers });

            if (!response.ok) {
                const errorData = await response.text();
              throw new Error(`Error fetching GitLab repository structure (page ${page}): ${errorData}`);
            }

            const pageData = await response.json();
            filesData.push(...pageData);

            const nextPage = response.headers.get('x-next-page');
            morePages = !!nextPage;
            page = nextPage ? parseInt(nextPage, 10) : page + 1;
        }

          if (!Array.isArray(filesData) || filesData.length === 0) {
            throw new Error('Could not fetch repository structure. Repository might be empty or inaccessible.');
        }

          // Step 3: Format file paths
        fileTreeData = filesData
          .filter((item: { type: string; path: string }) => item.type === 'blob')
          .map((item: { type: string; path: string }) => item.path)
          .join('\n');

          // Step 4: Try to fetch README.md content
          const readmeUrl = `${projectInfoUrl}/repository/files/README.md/raw`;
            try {
            const readmeResponse = await fetch(readmeUrl, { headers });
              if (readmeResponse.ok) {
                readmeContent = await readmeResponse.text();
                console.log('Successfully fetched GitLab README.md');
              } else {
              console.warn(`Could not fetch GitLab README.md status: ${readmeResponse.status}`);
              }
            } catch (err) {
            console.warn(`Error fetching GitLab README.md:`, err);
            }
        } catch (err) {
          console.error("Error during GitLab repository tree retrieval:", err);
          throw err;
        }
      }
      else if (effectiveRepoInfo.type === 'bitbucket') {
        // Bitbucket API approach
        const repoPath = extractUrlPath(effectiveRepoInfo.repoUrl ?? '') ?? `${owner}/${repo}`;
        const encodedRepoPath = encodeURIComponent(repoPath);

        // Try to get the file tree for common branch names
        let filesData = null;
        let apiErrorDetails = '';
        let defaultBranchLocal = '';
        const headers = createBitbucketHeaders(currentToken);

        // First get project info to determine default branch
        const projectInfoUrl = `https://api.bitbucket.org/2.0/repositories/${encodedRepoPath}`;
        try {
          const response = await fetch(projectInfoUrl, { headers });

          const responseText = await response.text();

          if (response.ok) {
            const projectData = JSON.parse(responseText);
            defaultBranchLocal = projectData.mainbranch.name;
            // Store the default branch in state
            setDefaultBranch(defaultBranchLocal);

            const apiUrl = `https://api.bitbucket.org/2.0/repositories/${encodedRepoPath}/src/${defaultBranchLocal}/?recursive=true&per_page=100`;
            try {
              const response = await fetch(apiUrl, {
                headers
              });

              const structureResponseText = await response.text();

              if (response.ok) {
                filesData = JSON.parse(structureResponseText);
              } else {
                const errorData = structureResponseText;
                apiErrorDetails = `Status: ${response.status}, Response: ${errorData}`;
              }
            } catch (err) {
              console.error(`Network error fetching Bitbucket branch ${defaultBranchLocal}:`, err);
            }
          } else {
            const errorData = responseText;
            apiErrorDetails = `Status: ${response.status}, Response: ${errorData}`;
          }
        } catch (err) {
          console.error("Network error fetching Bitbucket project info:", err);
        }

        if (!filesData || !Array.isArray(filesData.values) || filesData.values.length === 0) {
          if (apiErrorDetails) {
            throw new Error(`Could not fetch repository structure. Bitbucket API Error: ${apiErrorDetails}`);
          } else {
            throw new Error('Could not fetch repository structure. Repository might not exist, be empty or private.');
          }
        }

        // Convert files data to a string representation
        fileTreeData = filesData.values
          .filter((item: { type: string; path: string }) => item.type === 'commit_file')
          .map((item: { type: string; path: string }) => item.path)
          .join('\n');

        // Try to fetch README.md content
        try {
          const headers = createBitbucketHeaders(currentToken);

          const readmeResponse = await fetch(`https://api.bitbucket.org/2.0/repositories/${encodedRepoPath}/src/${defaultBranchLocal}/README.md`, {
            headers
          });

          if (readmeResponse.ok) {
            readmeContent = await readmeResponse.text();
          } else {
            console.warn(`Could not fetch Bitbucket README.md, status: ${readmeResponse.status}`);
          }
        } catch (err) {
          console.warn('Could not fetch Bitbucket README.md, continuing with empty README', err);
        }
      }
      } // end !structureFetchedViaBackendClone

      // Now determine the wiki structure
      await determineWikiStructure(fileTreeData, readmeContent, owner, repo, force);

    } catch (error) {
      console.error('Error fetching repository structure:', error);
      const message = error instanceof Error ? error.message : 'An unknown error occurred';
      const disconnected =
        error instanceof TypeError ||
        /NetworkError|Failed to fetch|fetch resource|connection (?:closed|refused|reset)/i.test(message);
      setIsLoading(false);
      setConnectionError(disconnected);
      setError(
        disconnected
          ? language === 'es'
            ? 'No se pudo contactar con el backend de HackDeepWiki. El repositorio no es el problema; comprueba que el servicio siga activo y vuelve a intentarlo.'
            : 'Could not contact the HackDeepWiki backend. The repository is not the problem; check that the service is running and retry.'
          : message
      );
      setLoadingMessage(undefined);
    } finally {
      // Reset the request in progress flag
      setRequestInProgress(false);
    }
  }, [owner, repo, determineWikiStructure, currentToken, effectiveRepoInfo, requestInProgress, messages.loading, language, crawlScopeModeParam, crawlMaxPagesParam, crawlSubdomainsParam, crawlRespectRobotsParam]);

  // Release history (versioned like wiki releases) for the dependency vuln
  // scan -- lists every saved scan, loads a specific one, or deletes one.
  // Mirrors loadWikiReleases/loadWikiRelease/deleteWikiRelease above.
  const loadVulnReleases = useCallback(async (autoSelectVersion?: number) => {
    try {
      const params = new URLSearchParams({
        owner: effectiveRepoInfo.owner, repo: effectiveRepoInfo.repo,
        repo_type: repoType, language,
      });
      const response = await fetch(`/api/vuln_cache/releases?${params.toString()}`);
      if (!response.ok) return;
      const data = await response.json();
      const releases: ScanRelease[] = Array.isArray(data?.releases) ? data.releases : [];
      setVulnReleases(releases);
      if (autoSelectVersion != null) {
        setSelectedVulnVersion(autoSelectVersion);
      } else if (releases.length > 0) {
        setSelectedVulnVersion(prev => (prev == null ? releases[0].version : prev));
      }
    } catch (err) {
      console.warn('Error loading vuln releases:', err);
    }
  }, [effectiveRepoInfo.owner, effectiveRepoInfo.repo, repoType, language]);

  const loadVulnRelease = useCallback(async (version: number) => {
    if (!version) return;
    try {
      const params = new URLSearchParams({
        owner: effectiveRepoInfo.owner, repo: effectiveRepoInfo.repo,
        repo_type: repoType, language, version: version.toString(),
      });
      const response = await fetch(`/api/vuln_cache?${params.toString()}`);
      if (!response.ok) throw new Error(`Failed to load release v${version}: ${response.status}`);
      const data = (await response.json()) as VulnReport;
      setVulnReport(data);
      setVulnStatus('done');
      setSelectedVulnVersion(version);
    } catch (err) {
      console.warn('Error loading vuln release:', err);
    }
  }, [effectiveRepoInfo.owner, effectiveRepoInfo.repo, repoType, language]);

  const deleteVulnRelease = useCallback(async (version: number) => {
    if (!version) return;
    if (!window.confirm(`Delete security scan release v${version}? This cannot be undone.`)) return;
    try {
      const params = new URLSearchParams({
        owner: effectiveRepoInfo.owner, repo: effectiveRepoInfo.repo,
        repo_type: repoType, language, version: version.toString(),
      });
      const response = await fetch(`/api/vuln_cache?${params.toString()}`, { method: 'DELETE' });
      if (!response.ok) throw new Error(`Failed to delete release v${version}: ${response.status}`);
      const relRes = await fetch(`/api/vuln_cache/releases?${new URLSearchParams({
        owner: effectiveRepoInfo.owner, repo: effectiveRepoInfo.repo, repo_type: repoType, language,
      }).toString()}`);
      const relData = relRes.ok ? await relRes.json() : { releases: [] };
      const remaining: ScanRelease[] = Array.isArray(relData?.releases) ? relData.releases : [];
      setVulnReleases(remaining);
      if (remaining.length > 0) {
        await loadVulnRelease(remaining[0].version);
      } else {
        setSelectedVulnVersion(null);
        setVulnReport(null);
        setVulnStatus('idle');
      }
    } catch (err) {
      console.warn('Error deleting vuln release:', err);
    }
  }, [effectiveRepoInfo.owner, effectiveRepoInfo.repo, repoType, language, loadVulnRelease]);

  // Function to export wiki content
  // 🔐 Vulnerability scan: open /ws/vuln_scan, stream progress, store report.
  // Sequential with wiki generation by design -- runs after the wiki is done
  // and the repo is already cloned locally, reusing that clone.
  const runVulnScan = useCallback(async (overrides?: VulnScanOverrides) => {
    if (vulnScanStartedRef.current) return;
    vulnScanStartedRef.current = true;
    setVulnStatus('running');
    setVulnError(null);
    setVulnReport(null);
    setVulnProgressMessage('Starting scan…');
    setVulnProgressPercent(0);

    const vulnScanWebSocketUrl = await getBackendWebSocketUrl('/ws/vuln_scan');
    const provider = overrides?.provider ?? selectedProviderState;
    const model = overrides?.model ?? selectedModelState;
    const creds = getSavedApiCredentials(provider);

    const payload = {
      repo_url: repoUrl || getRepoUrl(effectiveRepoInfo),
      repo_type: repoType,
      owner: effectiveRepoInfo.owner,
      repo: effectiveRepoInfo.repo,
      language,
      provider,
      model,
      api_key: creds.api_key || undefined,
      api_endpoint: creds.api_endpoint || undefined,
      local_path: effectiveRepoInfo.localPath || undefined,
      token: currentToken || undefined,
      // A rescan must reflect the repo's current remote state, not whatever
      // was cloned whenever the wiki was last generated -- without this,
      // re-scanning a repo that got new commits upstream silently kept
      // re-scanning the old clone and reproduced identical findings every
      // time. `overrides` is set both for a manual rerun (RescanConfigModal)
      // and for the auto-trigger right after wiki generation/refresh (which
      // now passes `{}` explicitly instead of calling with no args) -- see
      // both call sites of runVulnScan for why forcing here is safe even
      // when it's technically redundant (a repo that was just cloned/
      // re-cloned moments ago).
      force: overrides !== undefined,
      nvd_key: (overrides?.nvdKey ?? nvdKeyParam) || undefined,
      enable_client: overrides?.vulnClient ?? vulnClientEnabled,
      enable_server: overrides?.vulnServer ?? vulnServerEnabled,
      enable_deps: overrides?.vulnDeps ?? vulnDepsEnabled,
      run_llm: true,
      excluded_dirs: modelExcludedDirs,
      excluded_files: modelExcludedFiles,
    };

    try {
      await new Promise<void>((resolve, reject) => {
        const ws = new WebSocket(vulnScanWebSocketUrl);
        let settled = false;
        const timeout = setTimeout(() => {
          if (!settled) {
            settled = true;
            try { ws.close(); } catch {}
            reject(new Error('Vuln scan timed out.'));
          }
        }, 10 * 60 * 1000);

        ws.onopen = () => {
          ws.send(JSON.stringify(payload));
        };
        ws.onmessage = (ev) => {
          try {
            const msg = JSON.parse(ev.data);
            if (msg.type === 'progress') {
              setVulnProgressMessage(msg.message || 'Working…');
              setVulnProgressPercent(typeof msg.percent === 'number' ? msg.percent : null);
            } else if (msg.type === 'done') {
              settled = true;
              clearTimeout(timeout);
              setVulnReport(msg.report as VulnReport);
              setVulnStatus('done');
              setVulnProgressPercent(100);
              const newVersion = typeof msg.version === 'number' ? msg.version : undefined;
              loadVulnReleases(newVersion);
              try { ws.close(); } catch {}
              resolve();
            } else if (msg.type === 'error') {
              settled = true;
              clearTimeout(timeout);
              setVulnError(msg.message || 'Scan failed.');
              setVulnStatus('error');
              try { ws.close(); } catch {}
              reject(new Error(msg.message || 'Scan failed.'));
            }
          } catch {
            /* ignore non-JSON frames */
          }
        };
        // The browser's WebSocket `error` event carries no diagnostic
        // information whatsoever (by spec) -- the actual close code/reason
        // (e.g. 1006 abnormal closure, typically connection refused/reset;
        // or a server-sent reason string) only ever arrives via the `close`
        // event that always follows it. Settling here on `onerror` alone
        // discarded that detail and left every failure indistinguishable as
        // the same generic "WebSocket error during scan.", with nothing to
        // go on to diagnose it. Just flag that an error happened and let
        // `onclose` (below) produce the final message with real detail.
        let hadError = false;
        ws.onerror = () => {
          hadError = true;
        };
        ws.onclose = (event) => {
          if (!settled) {
            settled = true;
            clearTimeout(timeout);
            // Code 1000 (normal) / 1005 (no status, but not flagged as an
            // error) with no prior onerror -> a clean close before a
            // done/error frame arrived; treat as error only if still
            // "running" (matches the previous behavior for that case).
            const isAbnormalClose = hadError || (event.code !== 1000 && event.code !== 1005);
            if (isAbnormalClose) {
              const detail = event.reason ? `: ${event.reason}` : ` (code ${event.code})`;
              const message = `WebSocket error during scan${detail}.`;
              setVulnError(message);
              setVulnStatus('error');
              reject(new Error(message));
            } else {
              setVulnStatus((prev) => prev === 'running' ? 'error' : prev);
              resolve();
            }
          }
        };
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Scan failed.';
      setVulnStatus('error');
      setVulnError(msg);
    } finally {
      vulnScanStartedRef.current = false;
    }
  }, [repoUrl, repoType, effectiveRepoInfo, language, selectedProviderState, selectedModelState,
      nvdKeyParam, vulnClientEnabled, vulnServerEnabled, vulnDepsEnabled,
      modelExcludedDirs, modelExcludedFiles, loadVulnReleases, currentToken]);

  // Load a previously-saved vuln report (if any) so the Security tab is
  // populated when opening an already-scanned repo, without re-scanning.
  const loadVulnCache = useCallback(async () => {
    try {
      const params = new URLSearchParams({
        owner: effectiveRepoInfo.owner,
        repo: effectiveRepoInfo.repo,
        repo_type: repoType,
        language,
      });
      const res = await fetch(`/api/vuln_cache?${params.toString()}`);
      if (res.ok) {
        const data = (await res.json()) as VulnReport;
        setVulnReport(data);
        setVulnStatus('done');
      }
    } catch {
      /* no cache yet -- fine */
    }
  }, [effectiveRepoInfo.owner, effectiveRepoInfo.repo, repoType, language]);

  // On mount: always try to restore a saved vuln report from cache, so the
  // Security tab works on re-open. This used to be skipped whenever
  // vulnScanRequested was true ("a fresh scan is about to run anyway, don't
  // bother") -- but vulnScanRequested reads the `vuln_scan=1` URL param,
  // which the wiki-save effect writes into the browser's address bar via
  // history.replaceState the first time a scan ever runs. That makes it
  // permanently "true" for this repo's URL from then on, which permanently
  // skipped this restore on every future visit -- the report looked like it
  // never persisted, when it was actually saved correctly on disk the whole
  // time. If a fresh scan really is about to run (saveCache's
  // vulnScanRequested && !vulnReport trigger), it overwrites whatever this
  // loads within moments -- a harmless brief flash, not a bug.
  useEffect(() => {
    loadVulnCache();
  }, [loadVulnCache]);

  // Keep the ref pointing at the latest runVulnScan closure.
  useEffect(() => { runVulnScanRef.current = runVulnScan; }, [runVulnScan]);

  // Release history for the website security scan -- same versioned pattern
  // as loadVulnReleases/loadVulnRelease/deleteVulnRelease above.
  const loadWebVulnReleases = useCallback(async (autoSelectVersion?: number) => {
    try {
      const params = new URLSearchParams({
        owner: effectiveRepoInfo.owner, repo: effectiveRepoInfo.repo, language,
      });
      const response = await fetch(`/api/web_vuln_cache/releases?${params.toString()}`);
      if (!response.ok) return;
      const data = await response.json();
      const releases: ScanRelease[] = Array.isArray(data?.releases) ? data.releases : [];
      setWebVulnReleases(releases);
      if (autoSelectVersion != null) {
        setSelectedWebVulnVersion(autoSelectVersion);
      } else if (releases.length > 0) {
        setSelectedWebVulnVersion(prev => (prev == null ? releases[0].version : prev));
      }
    } catch (err) {
      console.warn('Error loading web vuln releases:', err);
    }
  }, [effectiveRepoInfo.owner, effectiveRepoInfo.repo, language]);

  const loadWebVulnRelease = useCallback(async (version: number) => {
    if (!version) return;
    try {
      const params = new URLSearchParams({
        owner: effectiveRepoInfo.owner, repo: effectiveRepoInfo.repo, language, version: version.toString(),
      });
      const response = await fetch(`/api/web_vuln_cache?${params.toString()}`);
      if (!response.ok) throw new Error(`Failed to load release v${version}: ${response.status}`);
      const data = (await response.json()) as WebVulnReport;
      setWebVulnReport(data);
      setWebVulnStatus('done');
      setSelectedWebVulnVersion(version);
    } catch (err) {
      console.warn('Error loading web vuln release:', err);
    }
  }, [effectiveRepoInfo.owner, effectiveRepoInfo.repo, language]);

  const deleteWebVulnRelease = useCallback(async (version: number) => {
    if (!version) return;
    if (!window.confirm(`Delete website security scan release v${version}? This cannot be undone.`)) return;
    try {
      const params = new URLSearchParams({
        owner: effectiveRepoInfo.owner, repo: effectiveRepoInfo.repo, language, version: version.toString(),
      });
      const response = await fetch(`/api/web_vuln_cache?${params.toString()}`, { method: 'DELETE' });
      if (!response.ok) throw new Error(`Failed to delete release v${version}: ${response.status}`);
      const relRes = await fetch(`/api/web_vuln_cache/releases?${new URLSearchParams({
        owner: effectiveRepoInfo.owner, repo: effectiveRepoInfo.repo, language,
      }).toString()}`);
      const relData = relRes.ok ? await relRes.json() : { releases: [] };
      const remaining: ScanRelease[] = Array.isArray(relData?.releases) ? relData.releases : [];
      setWebVulnReleases(remaining);
      if (remaining.length > 0) {
        await loadWebVulnRelease(remaining[0].version);
      } else {
        setSelectedWebVulnVersion(null);
        setWebVulnReport(null);
        setWebVulnStatus('idle');
      }
    } catch (err) {
      console.warn('Error deleting web vuln release:', err);
    }
  }, [effectiveRepoInfo.owner, effectiveRepoInfo.repo, language, loadWebVulnRelease]);

  // 🌐 Website vulnerability scan -- mirrors runVulnScan above but talks to
  // /ws/web_vuln_scan and stores a WebVulnReport. Only meaningful when
  // effectiveRepoInfo.type === 'website'; the site must already be crawled
  // (the wiki-save effect triggers this after the crawl+wiki generation
  // finishes, same sequencing runVulnScan uses for repos).
  const runWebVulnScan = useCallback(async (overrides?: WebVulnScanOverrides) => {
    if (webVulnScanStartedRef.current) return;
    webVulnScanStartedRef.current = true;
    setWebVulnStatus('running');
    setWebVulnError(null);
    setWebVulnReport(null);
    setWebVulnProgressMessage('Starting scan…');
    setWebVulnProgressPercent(0);

    const webVulnScanWebSocketUrl = await getBackendWebSocketUrl('/ws/web_vuln_scan');
    const provider = overrides?.provider ?? selectedProviderState;
    const model = overrides?.model ?? selectedModelState;
    const creds = getSavedApiCredentials(provider);

    const payload = {
      site_url: repoUrl || getRepoUrl(effectiveRepoInfo),
      owner: effectiveRepoInfo.owner,
      repo: effectiveRepoInfo.repo,
      language,
      provider,
      model,
      api_key: creds.api_key || undefined,
      api_endpoint: creds.api_endpoint || undefined,
      run_llm: true,
      enable_deep_scan: overrides?.enableDeepScan ?? deepScanEnabled,
    };

    try {
      await new Promise<void>((resolve, reject) => {
        const ws = new WebSocket(webVulnScanWebSocketUrl);
        let settled = false;
        // The Docker toolkit runs its tools in parallel, but the slowest of
        // them (nikto, or dalfox against a page with many query-param URLs)
        // can still take several minutes on its own, plus wpscan
        // afterward if WordPress is detected -- 10 minutes cut this off
        // mid-scan in practice. 20 minutes gives real headroom.
        const timeout = setTimeout(() => {
          if (!settled) {
            settled = true;
            try { ws.close(); } catch {}
            reject(new Error('Website vuln scan timed out.'));
          }
        }, 20 * 60 * 1000);

        ws.onopen = () => {
          ws.send(JSON.stringify(payload));
        };
        ws.onmessage = (ev) => {
          try {
            const msg = JSON.parse(ev.data);
            if (msg.type === 'progress') {
              setWebVulnProgressMessage(msg.message || 'Working…');
              setWebVulnProgressPercent(typeof msg.percent === 'number' ? msg.percent : null);
            } else if (msg.type === 'done') {
              settled = true;
              clearTimeout(timeout);
              setWebVulnReport(msg.report as WebVulnReport);
              setWebVulnStatus('done');
              setWebVulnProgressPercent(100);
              { const newVersion = typeof msg.version === 'number' ? msg.version : undefined;
                loadWebVulnReleases(newVersion); }
              try { ws.close(); } catch {}
              resolve();
            } else if (msg.type === 'error') {
              settled = true;
              clearTimeout(timeout);
              setWebVulnError(msg.message || 'Scan failed.');
              setWebVulnStatus('error');
              try { ws.close(); } catch {}
              reject(new Error(msg.message || 'Scan failed.'));
            }
          } catch {
            /* ignore non-JSON frames */
          }
        };
        // See the equivalent comment in runVulnScan above: `onerror` carries
        // no diagnostic info, the real close code/reason only arrives via
        // the `close` event that always follows it.
        let hadError = false;
        ws.onerror = () => {
          hadError = true;
        };
        ws.onclose = (event) => {
          if (!settled) {
            settled = true;
            clearTimeout(timeout);
            const isAbnormalClose = hadError || (event.code !== 1000 && event.code !== 1005);
            if (isAbnormalClose) {
              const detail = event.reason ? `: ${event.reason}` : ` (code ${event.code})`;
              const message = `WebSocket error during scan${detail}.`;
              setWebVulnError(message);
              setWebVulnStatus('error');
              reject(new Error(message));
            } else {
              setWebVulnStatus((prev) => prev === 'running' ? 'error' : prev);
              resolve();
            }
          }
        };
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Scan failed.';
      setWebVulnStatus('error');
      setWebVulnError(msg);
    } finally {
      webVulnScanStartedRef.current = false;
    }
  }, [repoUrl, effectiveRepoInfo, language, selectedProviderState, selectedModelState, deepScanEnabled, loadWebVulnReleases]);

  const loadWebVulnCache = useCallback(async () => {
    try {
      const params = new URLSearchParams({
        owner: effectiveRepoInfo.owner,
        repo: effectiveRepoInfo.repo,
        language,
      });
      const res = await fetch(`/api/web_vuln_cache?${params.toString()}`);
      if (res.ok) {
        const data = (await res.json()) as WebVulnReport;
        setWebVulnReport(data);
        setWebVulnStatus('done');
      }
    } catch {
      /* no cache yet -- fine */
    }
  }, [effectiveRepoInfo.owner, effectiveRepoInfo.repo, language]);

  useEffect(() => {
    if (effectiveRepoInfo.type === 'website') {
      loadWebVulnCache();
    }
  }, [effectiveRepoInfo.type, loadWebVulnCache]);

  useEffect(() => { runWebVulnScanRef.current = runWebVulnScan; }, [runWebVulnScan]);

  // Populate the release-history dropdowns for both scan types on mount, so
  // a returning visit shows every past scan even before (or without ever)
  // triggering a fresh one.
  useEffect(() => {
    if (effectiveRepoInfo.type !== 'website' && effectiveRepoInfo.type !== 'fanwiki') {
      loadVulnReleases();
    }
  }, [effectiveRepoInfo.type, loadVulnReleases]);

  useEffect(() => {
    if (effectiveRepoInfo.type === 'website') {
      loadWebVulnReleases();
    }
  }, [effectiveRepoInfo.type, loadWebVulnReleases]);

  const exportWiki = useCallback(async (format: 'markdown' | 'json' | 'obsidian' | 'hdwreader' | 'mediawiki_xml' | 'zim') => {
    if (!wikiStructure || Object.keys(generatedPages).length === 0) {
      setExportError('No wiki content to export');
      return;
    }

    try {
      setIsExporting(true);
      setExportError(null);
      setLoadingMessage(`${language === 'ja' ? 'Wikiを' : 'Exporting wiki as '} ${format} ${language === 'ja' ? 'としてエクスポート中...' : '...'}`);

      // Prepare the pages for export
      const pagesToExport = wikiStructure.pages.map(page => {
        // Use the generated content if available, otherwise use an empty string
        const content = generatedPages[page.id]?.content || 'Content not generated';
        return {
          ...page,
          content
        };
      });

      // Get repository URL
      const repoUrl = getRepoUrl(effectiveRepoInfo);

      // Build the export request body. For Obsidian and hdwreader, optionally
      // embed the vulnerability report(s) when the user opted in and a
      // report is available. hdwreader also carries the section hierarchy
      // and generation metadata the other formats don't need.
      const exportBody: Record<string, unknown> = {
        repo_url: repoUrl,
        type: effectiveRepoInfo.type,
        pages: pagesToExport,
        format,
        title: wikiStructure.title,
        version: selectedWikiVersion ?? undefined,
      };
      if ((format === 'obsidian' || format === 'hdwreader') && vulnReport && exportIncludeVulns) {
        exportBody.vuln_report = vulnReport;
        exportBody.include_vulns = true;
        exportBody.include_vuln_graph = exportIncludeVulnGraph;
      }
      if ((format === 'obsidian' || format === 'hdwreader') && webVulnReport && exportIncludeVulns) {
        exportBody.web_vuln_report = webVulnReport;
        exportBody.include_web_vulns = true;
      }
      if (format === 'hdwreader') {
        exportBody.sections = wikiStructure.sections || [];
        exportBody.root_sections = wikiStructure.rootSections || [];
        exportBody.description = wikiStructure.description;
        exportBody.language = language;
        exportBody.provider = selectedProviderState;
        exportBody.model = selectedModelState;
        exportBody.repo_type = effectiveRepoInfo.type;
        exportBody.owner = effectiveRepoInfo.owner;
        exportBody.repo = effectiveRepoInfo.repo;
      }
      if (format === 'zim') {
        exportBody.description = wikiStructure.description;
        exportBody.language = language;
      }

      // Make API call to export wiki
      const response = await fetch(`/export/wiki`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(exportBody),
      });

      if (!response.ok) {
        const errorText = await response.text().catch(() => 'No error details available');
        throw new Error(`Error exporting wiki: ${response.status} - ${errorText}`);
      }

      // Get the filename from the Content-Disposition header if available
      const contentDisposition = response.headers.get('Content-Disposition');
      const defaultExt = format === 'markdown' ? 'md' : format === 'obsidian' ? 'zip' : format === 'hdwreader' ? 'hdwreader' : format === 'mediawiki_xml' ? 'xml' : format === 'zim' ? 'zim' : 'json';
      let filename = `${effectiveRepoInfo.repo}_wiki.${defaultExt}`;

      if (contentDisposition) {
        const filenameMatch = contentDisposition.match(/filename=(.+)/);
        if (filenameMatch && filenameMatch[1]) {
          filename = filenameMatch[1].replace(/"/g, '');
        }
      }

      // Convert the response to a blob and download it
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);

    } catch (err) {
      console.error('Error exporting wiki:', err);
      const errorMessage = err instanceof Error ? err.message : 'Unknown error during export';
      setExportError(errorMessage);
    } finally {
      setIsExporting(false);
      setLoadingMessage(undefined);
    }
  }, [wikiStructure, generatedPages, effectiveRepoInfo, language, selectedWikiVersion, vulnReport, webVulnReport, exportIncludeVulns, exportIncludeVulnGraph, selectedProviderState, selectedModelState]);

  // No longer needed as we use the modal directly

  const confirmRefresh = useCallback(async (
    newToken?: string,
    selection?: AppliedModelSelection,
  ) => {
    const refreshProvider = selection?.provider ?? selectedProviderState;
    const refreshModel = selection?.model ?? selectedModelState;
    const refreshIsCustomModel = selection?.isCustomModel ?? isCustomSelectedModelState;
    const refreshCustomModel = selection?.customModel ?? customSelectedModelState;
    const refreshComprehensive = selection?.isComprehensiveView ?? isComprehensiveView;
    const refreshUserFocused = selection?.isUserFocusedView ?? isUserFocusedView;
    const refreshFocusInstructions = selection?.focusInstructions ?? focusInstructions;
    const refreshPageCount = normalizeWikiPageCount(
      selection?.pageCount ?? pageCount,
      refreshComprehensive,
    );
    const refreshExcludedDirs = selection?.excludedDirs ?? modelExcludedDirs;
    const refreshExcludedFiles = selection?.excludedFiles ?? modelExcludedFiles;

    setShowModelOptions(false);
    // With wiki versioning, an update no longer deletes the previous wiki — it
    // generates a new release (a new _vN file) so the old version stays available
    // in the Wiki Release dropdown. We only regenerate here; the save step assigns
    // the next version number on the backend.
    setLoadingMessage(messages.loading?.initializing || 'Initializing wiki generation...');
    setIsLoading(true); // Show loading indicator immediately

    if(authRequired && !authCode) {
      setIsLoading(false);
      console.error("Authorization code is required");
      setError('Authorization code is required');
      return;
    }

    // Update token if provided
    if (newToken) {
      // Update current token state
      setCurrentToken(newToken);
      // Update the URL parameters to include the new token
      const currentUrl = new URL(window.location.href);
      currentUrl.searchParams.set('token', newToken);
      window.history.replaceState({}, '', currentUrl.toString());
    }

    const currentUrl = new URL(window.location.href);
    currentUrl.searchParams.set('comprehensive', refreshComprehensive.toString());
    currentUrl.searchParams.set('audience', refreshUserFocused ? 'user' : 'developer');
    if (refreshFocusInstructions.trim()) {
      currentUrl.searchParams.set('focus', encodeURIComponent(refreshFocusInstructions));
    } else {
      currentUrl.searchParams.delete('focus');
    }
    currentUrl.searchParams.set('pages', refreshPageCount.toString());
    // Keep provider/model in the URL in sync with the user's selection so a full page
    // reload uses the same model they chose in the modal (and the server-cache match check
    // in loadData compares against the right values).
    currentUrl.searchParams.set('provider', refreshProvider);
    currentUrl.searchParams.set('model', refreshModel);
    if (refreshIsCustomModel && refreshCustomModel) {
      currentUrl.searchParams.set('is_custom_model', 'true');
      currentUrl.searchParams.set('custom_model', refreshCustomModel);
    } else {
      currentUrl.searchParams.delete('is_custom_model');
      currentUrl.searchParams.delete('custom_model');
    }

    // 🔐 Security Analysis — mirror the vuln scan selection into the URL so the
    // wiki-save effect kicks off a scan after the refreshed wiki is generated
    // (it triggers runVulnScan when vuln_scan=1, and runVulnScan reads the
    // category/nvd params straight from the URL). Reset the scan guard + report
    // so a refresh always re-scans fresh instead of keeping the previous run.
    if (selection?.enableVulnScan) {
      currentUrl.searchParams.set('vuln_scan', '1');
      currentUrl.searchParams.set('vuln_client', (selection.vulnClient ?? true) ? '1' : '0');
      currentUrl.searchParams.set('vuln_server', (selection.vulnServer ?? true) ? '1' : '0');
      currentUrl.searchParams.set('vuln_deps', (selection.vulnDeps ?? true) ? '1' : '0');
      if (selection.nvdKey) {
        currentUrl.searchParams.set('nvd_key', encodeURIComponent(selection.nvdKey));
      } else {
        currentUrl.searchParams.delete('nvd_key');
      }
      vulnScanStartedRef.current = false;
      setVulnReport(null);
      setVulnStatus('idle');
      setVulnError(null);
      setViewMode('wiki');
    } else {
      currentUrl.searchParams.delete('vuln_scan');
    }
    window.history.replaceState({}, '', currentUrl.toString());

    // Proceed with the rest of the refresh logic. The new generation is saved as
    // a NEW release version on the backend (never overwriting the previous wiki),
    // so the old release remains selectable in the Wiki Release dropdown.
    console.log('Refreshing wiki — a new release version will be created on save.');

    // Clear the localStorage cache (if any remnants or if it was used before this change)
    const localStorageCacheKey = getCacheKey(
      effectiveRepoInfo.owner,
      effectiveRepoInfo.repo,
      effectiveRepoInfo.type,
      language,
      refreshComprehensive,
      refreshPageCount,
    );
    localStorage.removeItem(localStorageCacheKey);

    // Reset cache loaded flag
    cacheLoadedSuccessfully.current = false;
    effectRan.current = false; // Allow the main data loading useEffect to run again
    // Make the next loadData bypass the server cache (the old release still
    // exists — versioned updates don't delete it) and bump the trigger so the
    // effect re-runs even if no other dependency changed.
    forceFreshGeneration.current = true;
    setRefreshTrigger((t) => t + 1);

    // Reset all state
    setWikiStructure(undefined);
    setCurrentPageId(undefined);
    setGeneratedPages({});
    setPagesInProgress(new Set());
    setError(null);
    setEmbeddingError(false); // Reset embedding error state
    setContentGenerationError(false);
    setIsLoading(true); // Set loading state for refresh
    setLoadingMessage(messages.loading?.initializing || 'Initializing wiki generation...');

    // Clear any in-progress requests for page content
    activeContentRequests.clear();
    // Reset flags related to request processing if they are component-wide
    setStructureRequestInProgress(false); // Assuming this flag should be reset
    setRequestInProgress(false); // Assuming this flag should be reset

    // Explicitly trigger the data loading process again by re-invoking what the main useEffect does.
    // This will first attempt to load from (now hopefully non-existent or soon-to-be-overwritten) server cache,
    // then proceed to fetchRepositoryStructure if needed.
    // To ensure fetchRepositoryStructure is called if cache is somehow still there or to force a full refresh:
    // One option is to directly call fetchRepositoryStructure() if force refresh means bypassing cache check.
    // For now, we rely on the standard loadData flow initiated by resetting effectRan and dependencies.
    // This will re-trigger the main data loading useEffect.
    // No direct call to fetchRepositoryStructure here, let the useEffect handle it based on effectRan.current = false.
  }, [effectiveRepoInfo, language, messages.loading, activeContentRequests, selectedProviderState, selectedModelState, isCustomSelectedModelState, customSelectedModelState, modelExcludedDirs, modelExcludedFiles, isComprehensiveView, isUserFocusedView, focusInstructions, pageCount, authCode, authRequired]);

  // Start wiki generation when component mounts
  useEffect(() => {
    if (effectRan.current === false) {
      effectRan.current = true; // Set to true immediately to prevent re-entry due to StrictMode

      const loadData = async () => {
        // A "Refresh Wiki" must regenerate, not restore. With versioning the
        // previous release is never deleted, so the server cache always hits —
        // without this skip, refresh would just reload the old wiki and bounce
        // the user straight back.
        if (forceFreshGeneration.current) {
          // Capture before resetting: fetchRepositoryStructure (and everything
          // it calls -- the clone/crawl step, structure planning, per-page
          // generation) now takes `force` as an explicit argument instead of
          // reading this ref internally, specifically so this reset can't run
          // before the value is actually consumed (it used to -- reading
          // forceFreshGeneration.current from inside fetchRepositoryStructure
          // always saw `false` here, since this line already flipped it
          // before fetchRepositoryStructure() below even started).
          const isForceRefresh = true;
          forceFreshGeneration.current = false;
          console.log('Refresh requested: skipping server cache, regenerating wiki with fresh data.');
          fetchRepositoryStructure(isForceRefresh);
          return;
        }

        // Try loading from server-side cache first
        setLoadingMessage(messages.loading?.fetchingCache || 'Checking for cached wiki...');
        try {
          const params = new URLSearchParams({
            owner: effectiveRepoInfo.owner,
            repo: effectiveRepoInfo.repo,
            repo_type: effectiveRepoInfo.type,
            language: language,
            comprehensive: isComprehensiveView.toString(),
            page_count: pageCount.toString(),
          });
          const response = await fetch(`/api/wiki_cache?${params.toString()}`);

          if (response.ok) {
            const cachedData = await response.json(); // Returns null if no cache
            if (cachedData && cachedData.wiki_structure && cachedData.generated_pages && Object.keys(cachedData.generated_pages).length > 0) {
              // The server wiki cache is keyed only by owner/repo/language/page-count, NOT by
              // provider/model. A previous generation with a different model (e.g. an Ollama
              // model like "gpt-oss:120b-cloud") would otherwise be restored here and OVERWRITE
              // the user's explicitly selected provider/model — causing the stale model to be
              // sent to the newly selected provider (e.g. Novita) and fail with MODEL_NOT_FOUND.
              // Only use the cache when it matches the user's explicit selection; otherwise drop
              // it and regenerate with the model the user actually chose.
              const explicitProvider = providerParam;
              const explicitModel = modelParam;
              const cacheMatchesSelection =
                (!cachedData.provider || !explicitProvider || cachedData.provider === explicitProvider) &&
                (!cachedData.model || !explicitModel || cachedData.model === explicitModel);

              if (!cacheMatchesSelection) {
                console.log('Ignoring server-cached wiki: cached model/provider does not match the user\'s selection. Regenerating.', {
                  cachedProvider: cachedData.provider, explicitProvider,
                  cachedModel: cachedData.model, explicitModel,
                });
              } else {
                console.log('Using server-cached wiki data');
                // Only restore model/provider from cache when the user did not specify them
                // explicitly (e.g. navigated directly to the repo URL without query params).
                if (cachedData.model && !explicitModel) {
                  setSelectedModelState(cachedData.model);
                }
                if (cachedData.provider && !explicitProvider) {
                  setSelectedProviderState(cachedData.provider);
                }

              // Update repoInfo
              if(cachedData.repo) {
                setEffectiveRepoInfo(cachedData.repo);
              } else if (cachedData.repo_url && !effectiveRepoInfo.repoUrl) {
                const updatedRepoInfo = { ...effectiveRepoInfo, repoUrl: cachedData.repo_url };
                setEffectiveRepoInfo(updatedRepoInfo); // Update effective repo info state
                console.log('Using cached repo_url:', cachedData.repo_url);
              }

              // Ensure the cached structure has sections and rootSections
              const cachedStructure = {
                ...cachedData.wiki_structure,
                sections: cachedData.wiki_structure.sections || [],
                rootSections: cachedData.wiki_structure.rootSections || []
              };

              // If sections or rootSections are missing, create intelligent ones based on page titles
              if (!cachedStructure.sections.length || !cachedStructure.rootSections.length) {
                const pages = cachedStructure.pages;
                const sections: WikiSection[] = [];
                const rootSections: string[] = [];

                // Group pages by common prefixes or categories
                const pageClusters = new Map<string, WikiPage[]>();

                // Define common categories that might appear in page titles
                const categories = [
                  { id: 'overview', title: 'Overview', keywords: ['overview', 'introduction', 'about'] },
                  { id: 'architecture', title: 'Architecture', keywords: ['architecture', 'structure', 'design', 'system'] },
                  { id: 'features', title: 'Core Features', keywords: ['feature', 'functionality', 'core'] },
                  { id: 'components', title: 'Components', keywords: ['component', 'module', 'widget'] },
                  { id: 'api', title: 'API', keywords: ['api', 'endpoint', 'service', 'server'] },
                  { id: 'data', title: 'Data Flow', keywords: ['data', 'flow', 'pipeline', 'storage'] },
                  { id: 'models', title: 'Models', keywords: ['model', 'ai', 'ml', 'integration'] },
                  { id: 'ui', title: 'User Interface', keywords: ['ui', 'interface', 'frontend', 'page'] },
                  { id: 'setup', title: 'Setup & Configuration', keywords: ['setup', 'config', 'installation', 'deploy'] }
                ];

                // Initialize clusters with empty arrays
                categories.forEach(category => {
                  pageClusters.set(category.id, []);
                });

                // Add an "Other" category for pages that don't match any category
                pageClusters.set('other', []);

                // Assign pages to categories based on title keywords
                pages.forEach((page: WikiPage) => {
                  const title = page.title.toLowerCase();
                  let assigned = false;

                  // Try to find a matching category
                  for (const category of categories) {
                    if (category.keywords.some(keyword => title.includes(keyword))) {
                      pageClusters.get(category.id)?.push(page);
                      assigned = true;
                      break;
                    }
                  }

                  // If no category matched, put in "Other"
                  if (!assigned) {
                    pageClusters.get('other')?.push(page);
                  }
                });

                // Create sections for non-empty categories
                for (const [categoryId, categoryPages] of pageClusters.entries()) {
                  if (categoryPages.length > 0) {
                    const category = categories.find(c => c.id === categoryId) ||
                                    { id: categoryId, title: categoryId === 'other' ? 'Other' : categoryId.charAt(0).toUpperCase() + categoryId.slice(1) };

                    const sectionId = `section-${categoryId}`;
                    sections.push({
                      id: sectionId,
                      title: category.title,
                      pages: categoryPages.map((p: WikiPage) => p.id)
                    });
                    rootSections.push(sectionId);

                    // Update page parentId
                    categoryPages.forEach((page: WikiPage) => {
                      page.parentId = sectionId;
                    });
                  }
                }

                // If we still have no sections (unlikely), fall back to importance-based grouping
                if (sections.length === 0) {
                  const highImportancePages = pages.filter((p: WikiPage) => p.importance === 'high').map((p: WikiPage) => p.id);
                  const mediumImportancePages = pages.filter((p: WikiPage) => p.importance === 'medium').map((p: WikiPage) => p.id);
                  const lowImportancePages = pages.filter((p: WikiPage) => p.importance === 'low').map((p: WikiPage) => p.id);

                  if (highImportancePages.length > 0) {
                    sections.push({
                      id: 'section-high',
                      title: 'Core Components',
                      pages: highImportancePages
                    });
                    rootSections.push('section-high');
                  }

                  if (mediumImportancePages.length > 0) {
                    sections.push({
                      id: 'section-medium',
                      title: 'Key Features',
                      pages: mediumImportancePages
                    });
                    rootSections.push('section-medium');
                  }

                  if (lowImportancePages.length > 0) {
                    sections.push({
                      id: 'section-low',
                      title: 'Additional Information',
                      pages: lowImportancePages
                    });
                    rootSections.push('section-low');
                  }
                }

                cachedStructure.sections = sections;
                cachedStructure.rootSections = rootSections;
              }

              setWikiStructure(cachedStructure);
              setGeneratedPages(cachedData.generated_pages);
              setCurrentPageId(cachedStructure.pages.length > 0 ? cachedStructure.pages[0].id : undefined);
              setIsLoading(false);
              setEmbeddingError(false);
              setContentGenerationError(false);
              setLoadingMessage(undefined);
              cacheLoadedSuccessfully.current = true;
              return; // Exit if cache is successfully loaded
              } // end of use-cache branch (cacheMatchesSelection)
            } else {
              console.log('No valid wiki data in server cache or cache is empty.');
            }
          } else {
            // Log error but proceed to fetch structure, as cache is optional
            console.error('Error fetching wiki cache from server:', response.status, await response.text());
          }
        } catch (error) {
          console.error('Error loading from server cache:', error);
          // Proceed to fetch structure if cache loading fails
        }

        // If we reached here, either there was no cache, it was invalid, or an error occurred
        // Proceed to fetch repository structure
        fetchRepositoryStructure();
      };

      loadData();

    } else {
      console.log('Skipping duplicate repository fetch/cache check');
    }

    // Clean up function for this effect is not strictly necessary for loadData,
    // but keeping the main unmount cleanup in the other useEffect
  }, [effectiveRepoInfo, effectiveRepoInfo.owner, effectiveRepoInfo.repo, effectiveRepoInfo.type, language, fetchRepositoryStructure, messages.loading?.fetchingCache, isComprehensiveView, pageCount, refreshTrigger, providerParam, modelParam]);

  // Fetch the list of saved wiki releases for this repo/language so the Wiki
  // Release dropdown can show every version. Called on mount and after each
  // generation/update. Optionally selects a specific version (e.g. the one just
  // created) once the list is loaded.
  const loadWikiReleases = useCallback(async (autoSelectVersion?: number) => {
    try {
      const params = new URLSearchParams({
        owner: effectiveRepoInfo.owner,
        repo: effectiveRepoInfo.repo,
        repo_type: effectiveRepoInfo.type,
        language: language,
      });
      const response = await fetch(`/api/wiki_cache/releases?${params.toString()}`);
      if (!response.ok) {
        console.warn('Failed to load wiki releases:', response.status);
        return;
      }
      const data = await response.json();
      const releases: WikiRelease[] = Array.isArray(data?.releases) ? data.releases : [];
      setWikiReleases(releases);
      if (autoSelectVersion != null) {
        setSelectedWikiVersion(autoSelectVersion);
      } else if (releases.length > 0) {
        // On first load, point the dropdown at the newest release (the one
        // currently displayed). Functional update keeps this callback's identity
        // stable (no selectedWikiVersion dependency) — a changing identity here
        // previously re-triggered the save effect in an infinite save loop.
        setSelectedWikiVersion(prev => (prev == null ? releases[0].version : prev));
      }
    } catch (err) {
      console.warn('Error loading wiki releases:', err);
    }
  }, [effectiveRepoInfo.owner, effectiveRepoInfo.repo, effectiveRepoInfo.type, language]);

  // Load a specific wiki release version into the view (replaces the currently
  // displayed wiki with the chosen release without regenerating).
  const loadWikiRelease = useCallback(async (version: number) => {
    if (!version) return;
    setLoadingMessage(messages.loading?.fetchingCache || 'Loading wiki release...');
    setIsLoading(true);
    try {
      const params = new URLSearchParams({
        owner: effectiveRepoInfo.owner,
        repo: effectiveRepoInfo.repo,
        repo_type: effectiveRepoInfo.type,
        language: language,
        version: version.toString(),
      });
      const response = await fetch(`/api/wiki_cache?${params.toString()}`);
      if (!response.ok) {
        throw new Error(`Failed to load release v${version}: ${response.status}`);
      }
      const cachedData = await response.json();
      if (!cachedData || !cachedData.wiki_structure) {
        throw new Error(`Release v${version} not found`);
      }
      const cachedStructure = {
        ...cachedData.wiki_structure,
        sections: cachedData.wiki_structure.sections || [],
        rootSections: cachedData.wiki_structure.rootSections || [],
      };
      setWikiStructure(cachedStructure);
      setGeneratedPages(cachedData.generated_pages || {});
      setCurrentPageId(cachedStructure.pages.length > 0 ? cachedStructure.pages[0].id : undefined);
      setSelectedWikiVersion(version);
      cacheLoadedSuccessfully.current = true;
      setError(null);
      setEmbeddingError(false);
      setContentGenerationError(false);
      setIsLoading(false);
      setLoadingMessage(undefined);
    } catch (err) {
      console.error('Error loading wiki release:', err);
      setError(err instanceof Error ? err.message : String(err));
      setIsLoading(false);
      setLoadingMessage(undefined);
    }
  }, [effectiveRepoInfo.owner, effectiveRepoInfo.repo, effectiveRepoInfo.type, language, messages.loading]);

  // Delete the currently selected wiki release from the server, then refresh the
  // dropdown. If the deleted release was the one on screen, load the next newest
  // release (or, if none remain, clear the view so the user can regenerate).
  const deleteWikiRelease = useCallback(async (version: number) => {
    if (!version) return;
    if (!window.confirm(
      (messages.repoPage?.confirmDeleteRelease || 'Delete this wiki release? This cannot be undone.')
        .replace('{version}', String(version))
    )) {
      return;
    }
    setIsLoading(true);
    setLoadingMessage(messages.loading?.clearingCache || 'Deleting wiki release...');
    try {
      const params = new URLSearchParams({
        owner: effectiveRepoInfo.owner,
        repo: effectiveRepoInfo.repo,
        repo_type: effectiveRepoInfo.type,
        language: language,
        version: version.toString(),
      });
      const response = await fetch(`/api/wiki_cache?${params.toString()}`, {
        method: 'DELETE',
        headers: { 'Accept': 'application/json' },
      });
      if (!response.ok) {
        const text = await response.text();
        throw new Error(`Failed to delete release v${version}: ${response.status} ${text}`);
      }
      // Refresh the releases list without auto-selecting the deleted version.
      const releasesParams = new URLSearchParams({
        owner: effectiveRepoInfo.owner,
        repo: effectiveRepoInfo.repo,
        repo_type: effectiveRepoInfo.type,
        language: language,
      });
      const relRes = await fetch(`/api/wiki_cache/releases?${releasesParams.toString()}`);
      const relData = relRes.ok ? await relRes.json() : { releases: [] };
      const remaining: WikiRelease[] = Array.isArray(relData?.releases) ? relData.releases : [];
      setWikiReleases(remaining);

      if (remaining.length > 0) {
        // Load the newest remaining release into the view.
        await loadWikiRelease(remaining[0].version);
      } else {
        // No releases left — clear the view.
        setSelectedWikiVersion(null);
        setWikiStructure(undefined);
        setGeneratedPages({});
        setCurrentPageId(undefined);
        cacheLoadedSuccessfully.current = false;
        setIsLoading(false);
        setLoadingMessage(undefined);
      }
    } catch (err) {
      console.error('Error deleting wiki release:', err);
      setError(err instanceof Error ? err.message : String(err));
      setIsLoading(false);
      setLoadingMessage(undefined);
    }
  }, [effectiveRepoInfo.owner, effectiveRepoInfo.repo, effectiveRepoInfo.type, language, messages.loading, messages.repoPage, loadWikiRelease]);

  // Load the releases list once on mount so the Wiki Release dropdown is populated
  // for an already-generated wiki.
  useEffect(() => {
    loadWikiReleases();
  }, [loadWikiReleases]);

  // Save wiki to server-side cache when generation is complete
  useEffect(() => {
    const saveCache = async () => {
      if (!isLoading &&
          !error &&
          wikiStructure &&
          Object.keys(generatedPages).length > 0 &&
          Object.keys(generatedPages).length >= wikiStructure.pages.length &&
          !cacheLoadedSuccessfully.current) {

        const allPagesHaveContent = wikiStructure.pages.every(page =>
          generatedPages[page.id] && generatedPages[page.id].content && generatedPages[page.id].content !== 'Loading...');

        if (allPagesHaveContent) {
          console.log('Attempting to save wiki data to server cache via Next.js proxy');

          try {
            // Make sure wikiStructure has sections and rootSections
            const structureToCache = {
              ...wikiStructure,
              sections: wikiStructure.sections || [],
              rootSections: wikiStructure.rootSections || []
            };
            const dataToCache = {
              repo: effectiveRepoInfo,
              language: language,
              comprehensive: isComprehensiveView,
              page_count: pageCount,
              wiki_structure: structureToCache,
              generated_pages: generatedPages,
              provider: selectedProviderState,
              model: selectedModelState
            };
            const response = await fetch(`/api/wiki_cache`, {
              method: 'POST',
              headers: {
                'Content-Type': 'application/json',
              },
              body: JSON.stringify(dataToCache),
            });

            if (response.ok) {
              // Mark the on-screen wiki as persisted BEFORE any state updates so
              // this effect can never re-fire and save the same wiki again as
              // another release (this exact loop once produced hundreds of
              // duplicate versions from a single generation).
              cacheLoadedSuccessfully.current = true;
              // 🔐 Kick off the vulnerability scan now that the wiki is
              // generated and the repo clone is on disk -- only if the user
              // opted in, AND only if a report isn't already loaded (from a
              // prior scan or the persisted cache). Without the !vulnReport
              // guard, this fires on every wiki-save cycle (including a plain
              // page reload that re-saves the same cached wiki), and
              // runVulnScan() nulls the report the instant it starts --
              // wiping out a perfectly good cached scan the user had just
              // gotten back, which read as "scans aren't persistent."
              if (vulnScanRequested && !vulnReport) {
                // Passing {} (not calling with no args) makes runVulnScan see
                // `overrides !== undefined` and force a fresh re-clone before
                // scanning. It's provably redundant right here -- the clone
                // this reads was just made (first generation) or just forced
                // fresh by handleRefresh (see fetchRepositoryStructure's
                // `force` chain) -- but making it explicit here removes the
                // dependency on that ordering holding forever as this file
                // keeps changing, for the cost of one extra cheap shallow
                // clone. This is exactly the path that silently re-scanned a
                // stale clone and reproduced identical findings after a
                // "Refresh Wiki" before that chain was force-aware.
                try { runVulnScanRef.current?.({}); } catch (e) { console.warn('vuln scan trigger failed', e); }
              }
              // 🌐 For website wikis, run the (separate) website security
              // scan once the site is crawled and the wiki is generated --
              // headers/cookies/TLS/exposure checks are basic hygiene, not an
              // opt-in the way the deep dependency CVE scan is. But only for
              // the first scan: same !webVulnReport guard as above, so a
              // reload that hits the wiki cache (and loadWebVulnCache already
              // restored the persisted report) doesn't blow it away by
              // starting a redundant fresh scan.
              if (effectiveRepoInfo.type === 'website' && !webVulnReport) {
                try { runWebVulnScanRef.current?.(); } catch (e) { console.warn('web vuln scan trigger failed', e); }
              }
              // The backend assigns and returns the new release version number.
              // Refresh the Wiki Release dropdown and select the version just
              // created so the dropdown reflects the wiki now on screen.
              try {
                const result = await response.json();
                const newVersion = typeof result?.version === 'number' ? result.version : undefined;
                console.log(`Wiki data successfully saved to server cache as release v${newVersion ?? '?'}`);
                if (newVersion != null) {
                  loadWikiReleases(newVersion);
                } else {
                  loadWikiReleases();
                }
              } catch {
                console.log('Wiki data successfully saved to server cache');
                loadWikiReleases();
              }
            } else {
              console.error('Error saving wiki data to server cache:', response.status, await response.text());
            }
          } catch (error) {
            console.error('Error saving to server cache:', error);
          }
        }
      }
    };

    saveCache();
  }, [isLoading, error, wikiStructure, generatedPages, effectiveRepoInfo, effectiveRepoInfo.owner, effectiveRepoInfo.repo, effectiveRepoInfo.type, effectiveRepoInfo.repoUrl, repoUrl, language, isComprehensiveView, pageCount, selectedProviderState, selectedModelState, loadWikiReleases, vulnReport, webVulnReport, vulnScanRequested]);

  const handlePageSelect = (pageId: string) => {
    if (currentPageId != pageId) {
      setCurrentPageId(pageId)
      // Unsaved edits are scoped to the page being edited -- navigating
      // away discards them rather than silently carrying them to whatever
      // page is selected next.
      setIsEditingPage(false);
      setEditError(null);
    }
    // Selecting a wiki page leaves the Security view.
    setViewMode('wiki');
  };

  const startEditingPage = () => {
    if (!currentPageId || !generatedPages[currentPageId]) return;
    setEditedContent(generatedPages[currentPageId].content);
    setEditInstruction('');
    setEditError(null);
    setIsEditingPage(true);
  };

  const cancelEditingPage = () => {
    setIsEditingPage(false);
    setEditedContent('');
    setEditInstruction('');
    setEditError(null);
  };

  const handleAiEditPage = async () => {
    if (!currentPageId || !generatedPages[currentPageId] || !editInstruction.trim() || isAiEditing) return;
    setIsAiEditing(true);
    setEditError(null);

    // A15: the previous version's `while (true) { reader.read() }` had no
    // timeout -- if the upstream model stalled mid-stream (no data, no
    // close frame), the edit spinner spun forever and the only way out
    // was reloading the page. Use the same idle-timeout pattern as the
    // page-generation WebSocket path: reset on every chunk, abort the
    // fetch + bail if 5 minutes pass with no new bytes.
    const IDLE_TIMEOUT_MS = 5 * 60 * 1000;
    const abortCtrl = new AbortController();
    let idleTimer: ReturnType<typeof setTimeout> | null = null;
    const resetIdleTimer = () => {
      if (idleTimer) clearTimeout(idleTimer);
      idleTimer = setTimeout(() => {
        console.warn('AI page edit stream went idle -- no data for 5 minutes.');
        abortCtrl.abort();
      }, IDLE_TIMEOUT_MS);
    };
    resetIdleTimer();

    // A16: some providers occasionally return an empty body (rate limit,
    // transient blip, or a model that only emits a stop token). The old
    // code silently set the page content to '' in that case, destroying
    // the user's existing content on save. Retry once before giving up.
    const attemptEdit = async (): Promise<string> => {
      const response = await fetch('/api/wiki/page/edit/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        signal: abortCtrl.signal,
        body: JSON.stringify({
          page_title: generatedPages[currentPageId].title,
          current_content: editedContent,
          instruction: editInstruction,
          provider: selectedProviderState,
          model: isCustomSelectedModelState ? customSelectedModelState : selectedModelState,
          language,
          ...getSavedApiCredentials(selectedProviderState),
        }),
      });
      if (!response.ok) {
        const errorText = await response.text().catch(() => '');
        throw new Error(errorText || `AI edit failed: ${response.status}`);
      }
      const reader = response.body?.getReader();
      if (!reader) throw new Error('Failed to get response reader');
      const decoder = new TextDecoder();
      let rewritten = '';
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        resetIdleTimer();
        rewritten += decoder.decode(value, { stream: true });
        setEditedContent(rewritten);
      }
      rewritten += decoder.decode();
      if (idleTimer) clearTimeout(idleTimer);
      return rewritten.replace(/^```markdown\s*/i, '').replace(/```\s*$/i, '');
    };

    try {
      let rewritten = await attemptEdit();
      if (!rewritten.trim()) {
        // A16: one retry -- empty body on the first attempt is usually a
        // transient provider hiccup, not a genuine "nothing to change".
        console.warn('AI edit returned empty body; retrying once.');
        rewritten = await attemptEdit();
      }
      if (!rewritten.trim()) {
        setEditError(
          language === 'es'
            ? 'El modelo no devolvió contenido para la edición. Intenta reformular la instrucción.'
            : 'The model returned no content for this edit. Try rephrasing the instruction.'
        );
      } else {
        setEditedContent(rewritten);
      }
    } catch (err) {
      if (abortCtrl.signal.aborted) {
        setEditError(
          language === 'es'
            ? (messages.repoPage?.modelTimeoutError || 'La edición se detuvo: sin datos durante 5 minutos.')
            : 'The edit stalled (no data for 5 minutes).'
        );
      } else {
        console.error('AI page edit failed:', err);
        setEditError(err instanceof Error ? err.message : 'AI edit failed');
      }
    } finally {
      if (idleTimer) clearTimeout(idleTimer);
      setIsAiEditing(false);
    }
  };

  const handleSaveEditedPage = async () => {
    if (!currentPageId || isSavingEdit) return;
    setIsSavingEdit(true);
    setEditError(null);
    try {
      const response = await fetch('/api/wiki_cache/page', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          repo: effectiveRepoInfo,
          language,
          page_id: currentPageId,
          content: editedContent,
        }),
      });
      if (!response.ok) {
        const errBody = await response.json().catch(() => ({}));
        throw new Error(errBody.detail || `Save failed: ${response.status}`);
      }
      const savedPageId = currentPageId;
      setGeneratedPages(prev => ({
        ...prev,
        [savedPageId]: { ...prev[savedPageId], content: editedContent },
      }));
      setOriginalMarkdown(prev => ({ ...prev, [savedPageId]: editedContent }));
      setIsEditingPage(false);
      loadWikiReleases();
    } catch (err) {
      console.error('Saving edited page failed:', err);
      setEditError(err instanceof Error ? err.message : 'Save failed');
    } finally {
      setIsSavingEdit(false);
    }
  };

  const [isModelSelectionModalOpen, setIsModelSelectionModalOpen] = useState(false);

  return (
    <div className="wiki-root">
      <style>{wikiStyles}</style>

      <header className="wiki-header">
        <div className="flex items-center gap-3">
          <Link href="/" className="text-[var(--accent-primary)] hover:text-[var(--highlight)] flex items-center gap-1.5 transition-colors font-mono text-sm">
            <FaHome /> {messages.repoPage?.home || 'Home'}
          </Link>
          {effectiveRepoInfo.owner && (
            <span className="text-[var(--muted)] font-mono text-xs opacity-60">
              / {effectiveRepoInfo.owner}/{effectiveRepoInfo.repo}
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          <ThemeToggle />
        </div>
      </header>

      <main className="wiki-body">
        {isLoading ? (
          <div className="flex flex-col items-center justify-center w-full h-full wiki-content">
            <div className="relative mb-6">
              <div className="absolute -inset-4 bg-[var(--accent-primary)]/10 rounded-full blur-md animate-pulse"></div>
              <div className="relative flex items-center justify-center">
                <div className="w-3 h-3 bg-[var(--accent-primary)]/70 rounded-full animate-pulse"></div>
                <div className="w-3 h-3 bg-[var(--accent-primary)]/70 rounded-full animate-pulse delay-75 mx-2"></div>
                <div className="w-3 h-3 bg-[var(--accent-primary)]/70 rounded-full animate-pulse delay-150"></div>
              </div>
            </div>
            <p className="text-[var(--foreground)] text-center mb-3 font-serif">
              {loadingMessage || messages.common?.loading || 'Loading...'}
              {isExporting && (messages.loading?.preparingDownload || ' Please wait while we prepare your download...')}
            </p>

            {/* Progress bar for the initial repo clone/download (only shown
                for a repo the backend hasn't cloned before -- a refresh of
                an already-generated repo skips straight past this). */}
            {cloneProgress && (
              <div className="w-full max-w-md mt-1 mb-3">
                <div className="bg-[var(--background)]/50 rounded-full h-2 mb-2 overflow-hidden border border-[var(--border-color)]">
                  <div
                    className="bg-[var(--accent-primary)] h-2 rounded-full transition-all duration-300 ease-in-out"
                    style={{ width: `${Math.max(3, cloneProgress.percent)}%` }}
                  />
                </div>
                <p className="text-xs text-[var(--muted)] text-center">
                  {cloneProgress.phase} {cloneProgress.percent}%
                </p>
              </div>
            )}

            {/* Progress bar for page generation */}
            {wikiStructure && (
              <div className="w-full max-w-md mt-3">
                <div className="bg-[var(--background)]/50 rounded-full h-2 mb-3 overflow-hidden border border-[var(--border-color)]">
                  <div
                    className="bg-[var(--accent-primary)] h-2 rounded-full transition-all duration-300 ease-in-out"
                    style={{
                      width: `${Math.max(5, 100 * (wikiStructure.pages.length - pagesInProgress.size) / wikiStructure.pages.length)}%`
                    }}
                  />
                </div>
                <p className="text-xs text-[var(--muted)] text-center">
                  {language === 'ja'
                    ? `${wikiStructure.pages.length}ページ中${wikiStructure.pages.length - pagesInProgress.size}ページ完了`
                    : messages.repoPage?.pagesCompleted
                        ? messages.repoPage.pagesCompleted
                            .replace('{completed}', (wikiStructure.pages.length - pagesInProgress.size).toString())
                            .replace('{total}', wikiStructure.pages.length.toString())
                        : `${wikiStructure.pages.length - pagesInProgress.size} of ${wikiStructure.pages.length} pages completed`}
                </p>

                {/* Show list of in-progress pages */}
                {pagesInProgress.size > 0 && (
                  <div className="mt-4 text-xs">
                    <p className="text-[var(--muted)] mb-2">
                      {messages.repoPage?.currentlyProcessing || 'Currently processing:'}
                    </p>
                    <ul className="text-[var(--foreground)] space-y-1">
                      {Array.from(pagesInProgress).slice(0, 3).map(pageId => {
                        const page = wikiStructure.pages.find(p => p.id === pageId);
                        return page ? <li key={pageId} className="truncate border-l-2 border-[var(--accent-primary)]/30 pl-2">{page.title}</li> : null;
                      })}
                      {pagesInProgress.size > 3 && (
                        <li className="text-[var(--muted)]">
                          {language === 'ja'
                            ? `...他に${pagesInProgress.size - 3}ページ`
                            : messages.repoPage?.andMorePages
                                ? messages.repoPage.andMorePages.replace('{count}', (pagesInProgress.size - 3).toString())
                                : `...and ${pagesInProgress.size - 3} more`}
                        </li>
                      )}
                    </ul>
                  </div>
                )}
              </div>
            )}
          </div>
        ) : error ? (
          <div className="flex flex-col items-center justify-center w-full h-full wiki-content">
            <div className="max-w-lg w-full bg-[var(--highlight)]/5 border border-[var(--highlight)]/30 p-5 shadow-sm">
            <div className="flex items-center text-[var(--highlight)] mb-3">
              <FaExclamationTriangle className="mr-2" />
              <span className="font-bold font-serif">{messages.repoPage?.errorTitle || messages.common?.error || 'Error'}</span>
            </div>
            <p className="text-[var(--foreground)] text-sm mb-3">{error}</p>
            <p className="text-[var(--muted)] text-xs">
              {connectionError ? (
                language === 'es'
                  ? (messages.repoPage?.analysisConnectionError || 'The connection to the service was interrupted during analysis. You can try again without changing the repository URL.')
                  : 'The service connection was interrupted during analysis. You can retry without changing the repository URL.'
              ) : embeddingError ? (
                messages.repoPage?.embeddingErrorDefault || 'This error is related to the document embedding system used for analyzing your repository. Please verify your embedding model configuration, API keys, and try again. If the issue persists, consider switching to a different embedding provider in the model settings.'
              ) : contentGenerationError ? (
                // The repo/site itself was already found and read successfully --
                // this failed at the content-generation step (the model returned
                // no pages, or stopped responding mid-stream), so the generic
                // "check your repo exists" hint below would be actively wrong here.
                language === 'es'
                  ? (messages.repoPage?.generationErrorRetry || 'The repository or site was read successfully; the problem occurred generating content with the AI model. Try again -- changing the model or reducing the number of pages may also help.')
                  : 'The repository or site was read successfully; the problem happened while the AI model was generating content. Retry the generation -- switching models or reducing the page count can also help.'
              ) : (
                messages.repoPage?.errorMessageDefault || 'Please check that your repository exists and is public. Valid formats are "owner/repo", "https://github.com/owner/repo", "https://gitlab.com/owner/repo", "https://bitbucket.org/owner/repo", or local folder paths like "C:\\path\\to\\folder" or "/path/to/folder".'
              )}
            </p>
            <div className="mt-5">
              <Link
                href="/"
                className="btn-japanese px-5 py-2 inline-flex items-center gap-1.5"
              >
                <FaHome className="text-sm" />
                {messages.repoPage?.backToHome || 'Back to Home'}
              </Link>
            </div>
          </div>
          </div>
        ) : wikiStructure ? (
          <React.Fragment>
            {/* Wiki Navigation */}
            <div className="wiki-sidebar">
              <h3 className="text-lg font-bold text-[var(--foreground)] mb-3 font-serif">{wikiStructure.title}</h3>
              <p className="text-[var(--muted)] text-sm mb-5 leading-relaxed">{wikiStructure.description}</p>

              {/* Display repository info */}
              <div className="text-xs text-[var(--muted)] mb-5 flex items-center">
                {effectiveRepoInfo.type === 'local' ? (
                  <div className="flex items-center">
                    <FaFolder className="mr-2" />
                    <span className="break-all">{effectiveRepoInfo.localPath}</span>
                  </div>
                ) : (
                  <>
                    {effectiveRepoInfo.type === 'github' ? (
                      <FaGithub className="mr-2" />
                    ) : effectiveRepoInfo.type === 'gitlab' ? (
                      <FaGitlab className="mr-2" />
                    ) : (
                      <FaBitbucket className="mr-2" />
                    )}
                    <a
                      href={effectiveRepoInfo.repoUrl ?? ''}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="hover:text-[var(--accent-primary)] transition-colors border-b border-[var(--border-color)] hover:border-[var(--accent-primary)]"
                    >
                      {effectiveRepoInfo.owner}/{effectiveRepoInfo.repo}
                    </a>
                  </>
                )}
              </div>

              {/* Wiki Type Indicator */}
              <div className="mb-3 flex items-center text-xs text-[var(--muted)]">
                <span className="mr-2">Wiki Type:</span>
                <span className={`px-2 py-0.5 rounded-full ${isComprehensiveView
                  ? 'bg-[var(--accent-primary)]/10 text-[var(--accent-primary)] border border-[var(--accent-primary)]/30'
                  : 'bg-[var(--background)] text-[var(--foreground)] border border-[var(--border-color)]'}`}>
                  {isComprehensiveView
                    ? (messages.form?.comprehensive || 'Comprehensive')
                    : (messages.form?.concise || 'Concise')}
                </span>
              </div>

              {/* Wiki Release dropdown — pick any saved version to read it.
                  Updates create new versions instead of overwriting, so every
                  release stays available here. */}
              {wikiReleases.length > 0 && (
                <div className="mb-3">
                  <label
                    htmlFor="wiki-release-select"
                    className="flex items-center text-xs text-[var(--muted)] mb-1.5 font-mono"
                  >
                    <FaHistory className="mr-1.5" />
                    {messages.repoPage?.wikiRelease || 'Wiki Release'}
                  </label>
                  <div className="flex items-stretch gap-2">
                    <select
                      id="wiki-release-select"
                      value={selectedWikiVersion ?? ''}
                      onChange={(e) => {
                        const v = Number(e.target.value);
                        if (!Number.isNaN(v) && v > 0) {
                          loadWikiRelease(v);
                        }
                      }}
                      disabled={isLoading}
                      className="flex-1 min-w-0 text-xs px-3 py-2 bg-[var(--background)] text-[var(--foreground)] rounded-md border border-[var(--border-color)] disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:border-[var(--accent-primary)] transition-colors hover:cursor-pointer"
                    >
                      {wikiReleases.map((release) => {
                        const date = new Date(release.created_at);
                        const dateStr = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
                        const mode = release.comprehensive
                          ? (messages.form?.comprehensive || 'Comprehensive')
                          : (messages.form?.concise || 'Concise');
                        return (
                          <option key={release.id} value={release.version}>
                            v{release.version} — {dateStr} ({mode}, {release.page_count}p)
                          </option>
                        );
                      })}
                    </select>
                    <button
                      type="button"
                      onClick={() => {
                        if (selectedWikiVersion != null) {
                          deleteWikiRelease(selectedWikiVersion);
                        }
                      }}
                      disabled={isLoading || selectedWikiVersion == null}
                      title={messages.repoPage?.deleteRelease || 'Delete selected release'}
                      aria-label={messages.repoPage?.deleteRelease || 'Delete selected release'}
                      className="flex items-center justify-center px-3 text-xs bg-[var(--background)] text-[var(--highlight)] rounded-md border border-[var(--border-color)] hover:bg-[var(--highlight)]/10 disabled:opacity-50 disabled:cursor-not-allowed transition-colors hover:cursor-pointer"
                    >
                      <FaTrash />
                    </button>
                  </div>
                </div>
              )}

              {/* Refresh Wiki button — creates a new release version on save */}
              <div className="mb-5">
                <button
                  onClick={() => setIsModelSelectionModalOpen(true)}
                  disabled={isLoading}
                  className="flex items-center w-full text-xs px-3 py-2 bg-[var(--background)] text-[var(--foreground)] rounded-md hover:bg-[var(--background)]/80 disabled:opacity-50 disabled:cursor-not-allowed border border-[var(--border-color)] transition-colors hover:cursor-pointer"
                >
                  <FaSync className={`mr-2 ${isLoading ? 'animate-spin' : ''}`} />
                  {messages.repoPage?.refreshWiki || 'Refresh Wiki'}
                </button>
              </div>

              {/* 🔐 Security Analysis entry — swap the content panel for the
                  vulnerability section. Always visible (for non-website repos)
                  so the user can trigger a scan on demand even if none has run
                  yet or a previous run never produced a cached report. Not
                  meaningful for 'fanwiki' either -- there's no dependency
                  manifest on disk to scan (imported wiki pages, not code),
                  and it isn't live-crawled like 'website' either. */}
              {effectiveRepoInfo.type !== 'website' && effectiveRepoInfo.type !== 'fanwiki' && (
                <div className="mb-5">
                  <button
                    onClick={() => setViewMode('security')}
                    className={`flex items-center w-full text-xs px-3 py-2 rounded-md border transition-colors hover:cursor-pointer ${
                      viewMode === 'security'
                        ? 'bg-[var(--accent-primary)]/15 text-[var(--accent-primary)] border-[var(--accent-primary)]/40'
                        : 'bg-[var(--background)] text-[var(--foreground)] border-[var(--border-color)] hover:bg-[var(--background)]/80'
                    }`}
                  >
                    <span className="mr-2">🔐</span>
                    Security Analysis
                    {vulnReport && vulnReport.total_findings > 0 && (
                      <span className="ml-auto text-[var(--highlight)] font-mono">
                        {vulnReport.total_findings}
                      </span>
                    )}
                    {vulnStatus === 'running' && (
                      <span className="ml-auto text-[var(--muted)] animate-pulse">scanning…</span>
                    )}
                  </button>
                  {viewMode === 'security' && vulnStatus !== 'running' && (
                    <button
                      onClick={() => setIsVulnRescanModalOpen(true)}
                      className="mt-2 flex items-center w-full text-[11px] px-3 py-1.5 bg-[var(--background)] text-[var(--muted)] rounded-md border border-[var(--border-color)] hover:text-[var(--foreground)] transition-colors"
                    >
                      <FaSync className="mr-2" />
                      {vulnReport ? 'Re-run scan' : 'Run scan'}
                    </button>
                  )}
                </div>
              )}

              {/* 🌐 Website Security entry -- same swap-the-panel pattern as
                  the dependency Security Analysis button above, but for
                  website wikis (always runs, not opt-in -- see the
                  wiki-save effect's website branch). */}
              {effectiveRepoInfo.type === 'website' && (
                <div className="mb-5">
                  <button
                    onClick={() => setViewMode('security')}
                    className={`flex items-center w-full text-xs px-3 py-2 rounded-md border transition-colors hover:cursor-pointer ${
                      viewMode === 'security'
                        ? 'bg-[var(--accent-primary)]/15 text-[var(--accent-primary)] border-[var(--accent-primary)]/40'
                        : 'bg-[var(--background)] text-[var(--foreground)] border-[var(--border-color)] hover:bg-[var(--background)]/80'
                    }`}
                  >
                    <span className="mr-2">🌐</span>
                    Website Security
                    {webVulnReport && webVulnReport.total_findings > 0 && (
                      <span className="ml-auto text-[var(--highlight)] font-mono">
                        {webVulnReport.total_findings}
                      </span>
                    )}
                    {webVulnStatus === 'running' && (
                      <span className="ml-auto text-[var(--muted)] animate-pulse">scanning…</span>
                    )}
                  </button>
                  {viewMode === 'security' && webVulnStatus !== 'running' && (
                    <button
                      onClick={() => setIsWebVulnRescanModalOpen(true)}
                      className="mt-2 flex items-center w-full text-[11px] px-3 py-1.5 bg-[var(--background)] text-[var(--muted)] rounded-md border border-[var(--border-color)] hover:text-[var(--foreground)] transition-colors"
                    >
                      <FaSync className="mr-2" />
                      Re-run scan
                    </button>
                  )}
                </div>
              )}

              {/* Export buttons */}
              {Object.keys(generatedPages).length > 0 && (
                <div className="mb-5">
                  <h4 className="text-sm font-semibold text-[var(--foreground)] mb-3 font-serif">
                    {messages.repoPage?.exportWiki || 'Export Wiki'}
                  </h4>
                  <div className="flex flex-col gap-2">
                    <button
                      onClick={() => exportWiki('markdown')}
                      disabled={isExporting}
                      className="btn-japanese flex items-center text-xs px-3 py-2 rounded-md disabled:opacity-50 disabled:cursor-not-allowed"
                    >
                      <FaDownload className="mr-2" />
                      {messages.repoPage?.exportAsMarkdown || 'Export as Markdown'}
                    </button>
                    <button
                      onClick={() => exportWiki('json')}
                      disabled={isExporting}
                      className="flex items-center text-xs px-3 py-2 bg-[var(--background)] text-[var(--foreground)] rounded-md hover:bg-[var(--background)]/80 disabled:opacity-50 disabled:cursor-not-allowed border border-[var(--border-color)] transition-colors"
                    >
                      <FaFileExport className="mr-2" />
                      {messages.repoPage?.exportAsJson || 'Export as JSON'}
                    </button>
                    <button
                      onClick={() => exportWiki('obsidian')}
                      disabled={isExporting}
                      title={messages.repoPage?.exportAsObsidianHint || 'Download the whole selected wiki release as an Obsidian vault (.zip)'}
                      className="flex items-center text-xs px-3 py-2 bg-[var(--background)] text-[var(--foreground)] rounded-md hover:bg-[var(--background)]/80 disabled:opacity-50 disabled:cursor-not-allowed border border-[var(--border-color)] transition-colors"
                    >
                      <FaBookOpen className="mr-2" />
                      {messages.repoPage?.exportAsObsidian || 'Export as Obsidian Vault (.zip)'}
                    </button>
                    <button
                      onClick={() => exportWiki('hdwreader')}
                      disabled={isExporting}
                      title="Download a portable offline bundle (.hdwreader) to read in the HackDeepWikiReader companion app (Android/Linux/Windows)"
                      className="flex items-center text-xs px-3 py-2 bg-[var(--background)] text-[var(--foreground)] rounded-md hover:bg-[var(--background)]/80 disabled:opacity-50 disabled:cursor-not-allowed border border-[var(--border-color)] transition-colors"
                    >
                      <FaMobileAlt className="mr-2" />
                      Export for HackDeepWikiReader
                    </button>
                    <button
                      onClick={() => exportWiki('mediawiki_xml')}
                      disabled={isExporting}
                      title="Download as a MediaWiki export-0.11 XML file -- importable into a real MediaWiki instance (Special:Import) or any tool that speaks the standard format, same as the fanwiki XML this app can import"
                      className="flex items-center text-xs px-3 py-2 bg-[var(--background)] text-[var(--foreground)] rounded-md hover:bg-[var(--background)]/80 disabled:opacity-50 disabled:cursor-not-allowed border border-[var(--border-color)] transition-colors"
                    >
                      <FaFileCode className="mr-2" />
                      Export as MediaWiki XML
                    </button>
                    <button
                      onClick={() => exportWiki('zim')}
                      disabled={isExporting}
                      title="Download as an offline .zim archive -- browsable in Kiwix or any other ZIM reader with no internet connection"
                      className="flex items-center text-xs px-3 py-2 bg-[var(--background)] text-[var(--foreground)] rounded-md hover:bg-[var(--background)]/80 disabled:opacity-50 disabled:cursor-not-allowed border border-[var(--border-color)] transition-colors"
                    >
                      <FaArchive className="mr-2" />
                      Export as ZIM (offline archive)
                    </button>
                    {effectiveRepoInfo.type === 'fanwiki' && (
                      <button
                        onClick={() => {
                          setIsAttachImagesModalOpen(true);
                          setAttachImagesError(null);
                          setAttachImagesResult(null);
                        }}
                        title="Match a local folder of images against this wiki's pages by filename and embed the ones that match -- for images that weren't available (or weren't provided) when this fanwiki was first imported"
                        className="flex items-center text-xs px-3 py-2 bg-[var(--background)] text-[var(--foreground)] rounded-md hover:bg-[var(--background)]/80 disabled:opacity-50 disabled:cursor-not-allowed border border-[var(--border-color)] transition-colors"
                      >
                        <FaFolder className="mr-2" />
                        Attach images
                      </button>
                    )}
                    {(vulnReport || webVulnReport) && (
                      <div className="mt-1 p-2 rounded-md border border-[var(--border-color)] bg-[var(--background)]/40 text-xs space-y-1.5">
                        <label className="flex items-center gap-2 text-[var(--foreground)] cursor-pointer">
                          <input
                            type="checkbox"
                            checked={exportIncludeVulns}
                            onChange={(e) => setExportIncludeVulns(e.target.checked)}
                            className="h-3.5 w-3.5 accent-[var(--accent-primary)]"
                          />
                          Include vulnerability report (🔐 Security folder)
                        </label>
                        <label className={`flex items-center gap-2 text-[var(--muted)] cursor-pointer ${exportIncludeVulns ? '' : 'opacity-50 pointer-events-none'}`}>
                          <input
                            type="checkbox"
                            checked={exportIncludeVulnGraph}
                            onChange={(e) => setExportIncludeVulnGraph(e.target.checked)}
                            className="h-3.5 w-3.5 accent-[var(--accent-primary)]"
                          />
                          Include vulnerability graph (Canvas + Mermaid)
                        </label>
                      </div>
                    )}
                  </div>
                  {exportError && (
                    <div className="mt-2 text-xs text-[var(--highlight)]">
                      {exportError}
                    </div>
                  )}
                </div>
              )}

              <h4 className="text-md font-semibold text-[var(--foreground)] mb-3 font-serif">
                {messages.repoPage?.pages || 'Pages'}
              </h4>
              <WikiTreeView
                wikiStructure={wikiStructure}
                currentPageId={currentPageId}
                onPageSelect={handlePageSelect}
                messages={messages.repoPage}
              />
            </div>

            {/* Wiki Content */}
            <div id="wiki-content" className="wiki-content">
              {viewMode === 'security' && effectiveRepoInfo.type === 'website' ? (
                <div className="w-full p-2">
                  <div className="flex items-center justify-between mb-3">
                    <h3 className="text-xl font-bold text-[var(--foreground)] font-serif">
                      🌐 Website Security
                    </h3>
                    <button
                      onClick={() => setViewMode('wiki')}
                      className="flex items-center gap-1.5 text-xs font-mono px-2.5 py-1.5 rounded-md border border-[var(--border-color)] text-[var(--muted)] hover:text-[var(--accent-primary)] hover:border-[var(--accent-primary)] transition-colors"
                    >
                      <FaBookOpen className="text-xs" />
                      Back to wiki
                    </button>
                  </div>
                  <WebVulnSection
                    report={webVulnReport}
                    status={webVulnStatus}
                    progressMessage={webVulnProgressMessage}
                    progressPercent={webVulnProgressPercent}
                    errorMessage={webVulnError || undefined}
                    onRetry={() => setIsWebVulnRescanModalOpen(true)}
                    releases={webVulnReleases}
                    selectedVersion={selectedWebVulnVersion}
                    onSelectVersion={loadWebVulnRelease}
                    onDeleteVersion={deleteWebVulnRelease}
                  />
                </div>
              ) : viewMode === 'security' ? (
                <div className="w-full p-2">
                  <div className="flex items-center justify-between mb-3">
                    <h3 className="text-xl font-bold text-[var(--foreground)] font-serif">
                      🔐 Security Analysis
                    </h3>
                    <button
                      onClick={() => setViewMode('wiki')}
                      className="flex items-center gap-1.5 text-xs font-mono px-2.5 py-1.5 rounded-md border border-[var(--border-color)] text-[var(--muted)] hover:text-[var(--accent-primary)] hover:border-[var(--accent-primary)] transition-colors"
                    >
                      <FaBookOpen className="text-xs" />
                      Back to wiki
                    </button>
                  </div>
                  <VulnSection
                    report={vulnReport}
                    status={vulnStatus}
                    progressMessage={vulnProgressMessage}
                    progressPercent={vulnProgressPercent}
                    errorMessage={vulnError || undefined}
                    onRetry={() => setIsVulnRescanModalOpen(true)}
                    releases={vulnReleases}
                    selectedVersion={selectedVulnVersion}
                    onSelectVersion={loadVulnRelease}
                    onDeleteVersion={deleteVulnRelease}
                  />
                </div>
              ) : currentPageId && generatedPages[currentPageId] ? (
                <div className="w-full">
                  <div className="flex items-start justify-between gap-3 mb-4">
                    <h3 className="text-xl font-bold text-[var(--foreground)] break-words font-serif">
                      {generatedPages[currentPageId].title}
                    </h3>
                    {!isEditingPage && (
                      <button
                        onClick={startEditingPage}
                        className="shrink-0 flex items-center gap-1.5 text-xs font-mono px-2.5 py-1.5 rounded-md border border-[var(--border-color)] text-[var(--muted)] hover:text-[var(--accent-primary)] hover:border-[var(--accent-primary)] transition-colors"
                        title={messages.repoPage?.editPage || 'Edit this page'}
                      >
                        <FaEdit className="text-xs" />
                        {messages.repoPage?.editPage || 'Edit'}
                      </button>
                    )}
                  </div>

                  {isEditingPage ? (
                    <div className="mb-6 flex flex-col gap-3">
                      <div className="flex flex-col sm:flex-row gap-2">
                        <input
                          type="text"
                          value={editInstruction}
                          onChange={(e) => setEditInstruction(e.target.value)}
                          placeholder={messages.repoPage?.editInstructionPlaceholder || 'Tell the AI what to change (optional)...'}
                          className="input-japanese flex-1 px-3 py-2 rounded-lg border-[var(--border-color)] bg-transparent text-sm text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
                          disabled={isAiEditing || isSavingEdit}
                        />
                        <button
                          onClick={handleAiEditPage}
                          disabled={!editInstruction.trim() || isAiEditing || isSavingEdit}
                          className="shrink-0 flex items-center justify-center gap-1.5 text-xs font-mono px-3 py-2 rounded-md bg-[var(--accent-primary)]/10 text-[var(--accent-primary)] border border-[var(--accent-primary)]/30 hover:bg-[var(--accent-primary)]/20 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          <FaMagic className={isAiEditing ? 'animate-spin' : ''} />
                          {messages.repoPage?.askAi || 'Ask AI'}
                        </button>
                      </div>

                      <textarea
                        value={editedContent}
                        onChange={(e) => setEditedContent(e.target.value)}
                        rows={20}
                        disabled={isSavingEdit}
                        className="input-japanese w-full px-3 py-2 rounded-lg border-[var(--border-color)] bg-transparent text-sm text-[var(--foreground)] font-mono focus:outline-none focus:border-[var(--accent-primary)] resize-y"
                      />

                      {editError && (
                        <p className="text-xs text-[var(--highlight)]">{editError}</p>
                      )}

                      <div className="flex items-center gap-2">
                        <button
                          onClick={handleSaveEditedPage}
                          disabled={isSavingEdit || isAiEditing}
                          className="flex items-center gap-1.5 text-xs font-mono px-3 py-2 rounded-md bg-[var(--accent-primary)] text-black hover:opacity-90 transition-opacity disabled:opacity-50 disabled:cursor-not-allowed"
                        >
                          <FaSave className={isSavingEdit ? 'animate-spin' : ''} />
                          {messages.repoPage?.save || 'Save'}
                        </button>
                        <button
                          onClick={cancelEditingPage}
                          disabled={isSavingEdit}
                          className="flex items-center gap-1.5 text-xs font-mono px-3 py-2 rounded-md border border-[var(--border-color)] text-[var(--muted)] hover:text-[var(--foreground)] transition-colors disabled:opacity-50"
                        >
                          <FaTimes />
                          {messages.repoPage?.cancel || 'Cancel'}
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="prose prose-sm md:prose-base lg:prose-lg max-w-none">
                      <Markdown
                        content={generatedPages[currentPageId].content}
                        repoInfo={effectiveRepoInfo}
                      />
                    </div>
                  )}

                  {!isEditingPage && generatedPages[currentPageId].relatedPages.length > 0 && (
                    <div className="mt-8 pt-4 border-t border-[var(--border-color)]">
                      <h4 className="text-sm font-semibold text-[var(--muted)] mb-3">
                        {messages.repoPage?.relatedPages || 'Related Pages:'}
                      </h4>
                      <div className="flex flex-wrap gap-2">
                        {generatedPages[currentPageId].relatedPages.map(relatedId => {
                          const relatedPage = wikiStructure.pages.find(p => p.id === relatedId);
                          return relatedPage ? (
                            <button
                              key={relatedId}
                              className="bg-[var(--accent-primary)]/10 hover:bg-[var(--accent-primary)]/20 text-xs text-[var(--accent-primary)] px-3 py-1.5 rounded-md transition-colors truncate max-w-full border border-[var(--accent-primary)]/20"
                              onClick={() => handlePageSelect(relatedId)}
                            >
                              {relatedPage.title}
                            </button>
                          ) : null;
                        })}
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <div className="flex flex-col items-center justify-center p-8 text-[var(--muted)] h-full">
                  <div className="relative mb-4">
                    <div className="absolute -inset-2 bg-[var(--accent-primary)]/5 rounded-full blur-md"></div>
                    <FaBookOpen className="text-4xl relative z-10" />
                  </div>
                  <p className="font-serif">
                    {messages.repoPage?.selectPagePrompt || 'Select a page from the navigation to view its content'}
                  </p>
                </div>
              )}
            </div>
          </React.Fragment>
        ) : null}
      </main>

      {!isLoading && wikiStructure && (
        <ChatWidget
          repoInfo={effectiveRepoInfo}
          provider={selectedProviderState}
          model={selectedModelState}
          isCustomModel={isCustomSelectedModelState}
          customModel={customSelectedModelState}
          language={language}
          currentPageId={currentPageId}
          title={messages.ask?.title || 'Repository chat'}
          fabAriaLabel={messages.ask?.title || 'Ask about this repository'}
        />
      )}

      <ModelSelectionModal
        isOpen={isModelSelectionModalOpen}
        onClose={() => setIsModelSelectionModalOpen(false)}
        provider={selectedProviderState}
        setProvider={setSelectedProviderState}
        model={selectedModelState}
        setModel={setSelectedModelState}
        isCustomModel={isCustomSelectedModelState}
        setIsCustomModel={setIsCustomSelectedModelState}
        customModel={customSelectedModelState}
        setCustomModel={setCustomSelectedModelState}
        isComprehensiveView={isComprehensiveView}
        setIsComprehensiveView={setIsComprehensiveView}
        isUserFocusedView={isUserFocusedView}
        setIsUserFocusedView={setIsUserFocusedView}
        focusInstructions={focusInstructions}
        setFocusInstructions={setFocusInstructions}
        pageCount={pageCount}
        setPageCount={setPageCount}
        showFileFilters={true}
        excludedDirs={modelExcludedDirs}
        setExcludedDirs={setModelExcludedDirs}
        excludedFiles={modelExcludedFiles}
        setExcludedFiles={setModelExcludedFiles}
        includedDirs={modelIncludedDirs}
        setIncludedDirs={setModelIncludedDirs}
        includedFiles={modelIncludedFiles}
        setIncludedFiles={setModelIncludedFiles}
        onApply={confirmRefresh}
        showWikiType={true}
        showTokenInput={effectiveRepoInfo.type !== 'local' && !currentToken} // Show token input if not local and no current token
        repositoryType={effectiveRepoInfo.type as 'github' | 'gitlab' | 'bitbucket'}
        authRequired={authRequired}
        authCode={authCode}
        setAuthCode={setAuthCode}
        isAuthLoading={isAuthLoading}
        showVulnScan={effectiveRepoInfo.type !== 'website' && effectiveRepoInfo.type !== 'fanwiki'}
        enableVulnScan={vulnScanRequested}
        vulnClient={vulnClientEnabled}
        vulnServer={vulnServerEnabled}
        vulnDeps={vulnDepsEnabled}
        nvdKey={nvdKeyParam}
      />

      {/* 🔐/🌐 "Rerun scan" floating config modals -- pick a provider/model
          (and scan-specific options) before kicking off a rescan, same idea
          as ModelSelectionModal above but scoped to just a scan instead of a
          full wiki refresh. */}
      <RescanConfigModal
        isOpen={isVulnRescanModalOpen}
        onClose={() => setIsVulnRescanModalOpen(false)}
        variant="dependency"
        provider={selectedProviderState}
        model={selectedModelState}
        isCustomModel={isCustomSelectedModelState}
        customModel={customSelectedModelState}
        vulnClient={vulnClientEnabled}
        vulnServer={vulnServerEnabled}
        vulnDeps={vulnDepsEnabled}
        nvdKey={nvdKeyParam}
        onSubmit={(selection: RescanSelection) => {
          runVulnScan({
            provider: selection.provider,
            model: selection.model,
            vulnClient: selection.vulnClient,
            vulnServer: selection.vulnServer,
            vulnDeps: selection.vulnDeps,
            nvdKey: selection.nvdKey,
          });
        }}
      />
      <RescanConfigModal
        isOpen={isWebVulnRescanModalOpen}
        onClose={() => setIsWebVulnRescanModalOpen(false)}
        variant="website"
        provider={selectedProviderState}
        model={selectedModelState}
        isCustomModel={isCustomSelectedModelState}
        customModel={customSelectedModelState}
        enableDeepScan={deepScanEnabled}
        onSubmit={(selection: RescanSelection) => {
          runWebVulnScan({
            provider: selection.provider,
            model: selection.model,
            enableDeepScan: selection.enableDeepScan,
          });
        }}
      />

      {isAttachImagesModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div className="bg-[var(--card-bg)] border border-[var(--border-color)] rounded-lg shadow-custom w-full max-w-lg p-6 card-japanese max-h-[90vh] overflow-y-auto">
            <h3 className="text-lg font-semibold text-[var(--foreground)] mb-2">
              Attach images to this fanwiki
            </h3>
            <p className="text-xs text-[var(--muted)] mb-4">
              Match a local folder of images against <span className="font-mono">{repo}</span>&apos;s
              pages by filename and embed the ones that match. Safe to run more than once --
              already-embedded images are left alone, only remaining placeholders are resolved.
            </p>

            <label className="block text-xs font-medium text-[var(--foreground)] mb-1">
              Images folder
            </label>
            <div className="flex gap-2 mb-3">
              <input
                type="text"
                value={attachImagesDir}
                onChange={(e) => setAttachImagesDir(e.target.value)}
                placeholder="/home/user/forgottenrealms_images/"
                className="input-japanese block flex-1 px-3 py-2.5 border-[var(--border-color)] rounded-lg bg-transparent text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
                autoFocus
                disabled={attachImagesRunning}
              />
              <button
                type="button"
                onClick={() => setIsAttachImagesBrowserOpen(true)}
                className="px-3 py-2 rounded-lg text-sm text-[var(--foreground)] border border-[var(--border-color)] hover:bg-[var(--background)] transition-colors disabled:opacity-50"
                disabled={attachImagesRunning}
              >
                Browse…
              </button>
            </div>

            {attachImagesError && (
              <div className="text-[var(--highlight)] text-xs mb-2">{attachImagesError}</div>
            )}
            {attachImagesResult && (
              <div className="text-xs text-[var(--accent-primary)] mb-2">
                Scanned {attachImagesResult.files_scanned} page(s): {attachImagesResult.images_attached}{' '}
                image(s) attached, {attachImagesResult.images_still_missing} still missing.
              </div>
            )}

            <div className="flex justify-end gap-2 mt-2">
              <button
                type="button"
                onClick={() => setIsAttachImagesModalOpen(false)}
                className="px-4 py-2 rounded-lg text-sm text-[var(--foreground)] hover:bg-[var(--background)] transition-colors"
                disabled={attachImagesRunning}
              >
                Close
              </button>
              <button
                type="button"
                onClick={handleAttachImages}
                className="btn-japanese px-4 py-2 rounded-lg text-sm disabled:opacity-50 disabled:cursor-not-allowed"
                disabled={attachImagesRunning}
              >
                {attachImagesRunning ? 'Attaching…' : 'Attach images'}
              </button>
            </div>
          </div>
        </div>
      )}

      <FileBrowserModal
        isOpen={isAttachImagesBrowserOpen}
        onClose={() => setIsAttachImagesBrowserOpen(false)}
        mode="directory"
        title="Select an images folder"
        onSelect={(path) => setAttachImagesDir(path)}
      />
    </div>
  );
}
