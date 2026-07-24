/* eslint-disable @typescript-eslint/no-explicit-any */
// Module-scope helpers, types and constants extracted from
// src/app/[owner]/[repo]/page.tsx -- these are pure / value-only utilities
// with no React state, so they don't belong inline in a 4000-line component
// file. The RepoWikiPage component imports them back. Nothing here touches
// React hooks or component state, which keeps the extraction risk-free:
// the component's behavior is unchanged because these are verbatim moves.

import { getBackendWebSocketUrl } from '@/utils/backendUrl';
import { WEBSOCKET_CONNECT_TIMEOUT_MS } from '@/utils/timeouts';

// Define the WikiSection and WikiStructure types directly in this file
// since the imported types don't have the sections and rootSections properties
export interface WikiSection {
  id: string;
  title: string;
  pages: string[];
  subsections?: string[];
}

export interface WikiPage {
  id: string;
  title: string;
  content: string;
  filePaths: string[];
  importance: 'high' | 'medium' | 'low';
  relatedPages: string[];
  parentId?: string;
  isSection?: boolean;
  children?: string[];
}

export interface WikiStructure {
  id: string;
  title: string;
  description: string;
  pages: WikiPage[];
  sections: WikiSection[];
  rootSections: string[];
}

// One saved release (version) of a repository's wiki, returned by the backend's
// /api/wiki_cache/releases endpoint. Used to populate the "Wiki Release" dropdown.
export interface WikiRelease {
  version: number;
  created_at: number;
  comprehensive: boolean | null;
  page_count: number;
  provider: string | null;
  model: string | null;
  title: string | null;
  id: string;
}

// One saved release (version) of a vulnerability or website-security scan,
// returned by /api/vuln_cache/releases or /api/web_vuln_cache/releases --
// same versioning scheme as WikiRelease above.
export interface ScanRelease {
  version: number;
  created_at: number;
  total_findings: number | null;
  generated_at: string | null;
  id: string;
}

// Optional per-call overrides for runVulnScan -- lets the "Rerun scan" config
// modal pick a provider/model/category selection without waiting on React
// state (which wouldn't be visible yet in the same tick the scan starts).
export interface VulnScanOverrides {
  provider?: string;
  model?: string;
  nvdKey?: string;
  vulnClient?: boolean;
  vulnServer?: boolean;
  vulnDeps?: boolean;
}

export interface WebVulnScanOverrides {
  provider?: string;
  model?: string;
  enableDeepScan?: boolean;
}

// Add CSS styles for wiki with Japanese aesthetic
export const wikiStyles = `
  .prose code {
    @apply bg-[var(--background)]/70 px-1.5 py-0.5 rounded font-mono text-xs border border-[var(--border-color)];
  }

  .prose pre {
    @apply bg-[var(--background)]/80 text-[var(--foreground)] rounded-md p-4 overflow-x-auto border border-[var(--border-color)] shadow-sm;
  }

  .prose h1, .prose h2, .prose h3, .prose h4 {
    @apply font-serif text-[var(--foreground)];
  }

  .prose p {
    @apply text-[var(--foreground)] leading-relaxed;
  }

  .prose a {
    @apply text-[var(--accent-primary)] hover:text-[var(--highlight)] transition-colors no-underline border-b border-[var(--border-color)] hover:border-[var(--accent-primary)];
  }

  .prose blockquote {
    @apply border-l-4 border-[var(--accent-primary)]/30 bg-[var(--background)]/30 pl-4 py-1 italic;
  }

  .prose ul, .prose ol {
    @apply text-[var(--foreground)];
  }

  .prose table {
    @apply border-collapse border border-[var(--border-color)];
  }

  .prose th {
    @apply bg-[var(--background)]/70 text-[var(--foreground)] p-2 border border-[var(--border-color)];
  }

  .prose td {
    @apply p-2 border border-[var(--border-color)];
  }
`;

// Helper function to generate cache key for localStorage
export const getCacheKey = (owner: string, repo: string, repoType: string, language: string, isComprehensive: boolean = true, pageCount: number = 10): string => {
  return `hackdeepwiki_cache_${repoType}_${owner}_${repo}_${language}_${isComprehensive ? 'comprehensive' : 'concise'}_${pageCount}`;
};

// Helper function to add tokens and other parameters to request body
export const addTokensToRequestBody = (
  requestBody: Record<string, any>,
  token: string,
  repoType: string,
  provider: string = '',
  model: string = '',
  isCustomModel: boolean = false,
  customModel: string = '',
  language: string = 'en',
  excludedDirs?: string,
  excludedFiles?: string,
  includedDirs?: string,
  includedFiles?: string
): void => {
  if (token !== '') {
    requestBody.token = token;
  }

  // Add provider-based model selection parameters
  requestBody.provider = provider;
  // When a custom model name is provided it takes precedence over the dropdown selection.
  // The backend chat-completion request schema only honors `model` (it does not parse a
  // separate custom_model field), so we must send the custom model as `model` for it to
  // actually be used against the provider endpoint.
  requestBody.model = (isCustomModel && customModel) ? customModel : model;
  if (isCustomModel && customModel) {
    requestBody.custom_model = customModel;
  }

  requestBody.language = language;

  // Add file filter parameters if provided
  if (excludedDirs) {
    requestBody.excluded_dirs = excludedDirs;
  }
  if (excludedFiles) {
    requestBody.excluded_files = excludedFiles;
  }
  if (includedDirs) {
    requestBody.included_dirs = includedDirs;
  }
  if (includedFiles) {
    requestBody.included_files = includedFiles;
  }

  // Inject API Keys and Endpoints from localStorage if available
  try {
    if (typeof window !== 'undefined') {
      const savedKeys = localStorage.getItem('deepwiki_api_keys');
      if (savedKeys) {
        const parsedKeys = JSON.parse(savedKeys);
        if (parsedKeys[provider]) {
          requestBody.api_key = parsedKeys[provider];
        }
      }
      const savedEndpoints = localStorage.getItem('deepwiki_api_endpoints');
      if (savedEndpoints) {
        const parsedEndpoints = JSON.parse(savedEndpoints);
        if (parsedEndpoints[provider]) {
          requestBody.api_endpoint = parsedEndpoints[provider];
        }
      }
    }
  } catch (e) {
    console.error('Failed to parse saved api settings in addTokensToRequestBody', e);
  }
};

export interface CloneProgress {
  phase: string;
  percent: number;
}

interface BackendRepoStructure {
  fileTreeData: string;
  readmeContent: string;
  defaultBranch: string;
}

// Hard cap on how many relevant_files a single wiki page's generation prompt
// will include. The wiki-structure planning prompt (determineWikiStructure)
// only asks the LLM for "actual files" per page -- it never enforced a
// maximum -- so for a large repository the planner can assign hundreds or
// thousands of paths to one page. Since each path becomes a line in the
// page-content prompt, an uncapped list can balloon the prompt to hundreds
// of thousands of tokens, well past any model's context window (verified:
// a ~14,600-file C# project produced a single-page prompt of 400k+ tokens).
export const MAX_RELEVANT_FILES_PER_PAGE = 30;

export function treeToFileList(tree: any[]): string {
  return tree
    .filter((item: { type: string; path: string }) => item.type === 'blob')
    .map((item: { type: string; path: string }) => item.path)
    .join('\n');
}

// Structure determination (file tree + README) for a github/gitlab/bitbucket
// repo, sourced from the backend's own local clone instead of the
// provider's REST API -- the backend already has (or is about to make)
// this exact clone for wiki generation itself, so asking the provider API
// separately just for the tree/README is a redundant network round trip
// that's also subject to that provider's rate limit. Reports live
// `git clone --progress` phases via onProgress while it clones (skipped
// entirely, near-instant, if the repo was already cloned by a previous
// generation). Falls back to a plain blocking HTTP call if the WebSocket
// can't be used, and returns null (never throws) if both fail, so the
// caller can fall back to the original per-provider API logic unchanged.
export async function fetchRepoStructureViaBackendClone(
  repoUrl: string,
  repoType: string,
  token: string,
  onProgress: (progress: CloneProgress | null) => void,
  force: boolean = false
): Promise<BackendRepoStructure | null> {
  try {
    const cloneWebSocketUrl = await getBackendWebSocketUrl('/ws/repo/clone');
    const result = await new Promise<BackendRepoStructure>((resolve, reject) => {
      const ws = new WebSocket(cloneWebSocketUrl);
      let settled = false;

      const timeout = setTimeout(() => {
        if (!settled) {
          settled = true;
          ws.close();
          reject(new Error('WebSocket connection timeout'));
        }
      }, WEBSOCKET_CONNECT_TIMEOUT_MS);

      ws.onopen = () => {
        clearTimeout(timeout);
        ws.send(JSON.stringify({ repo_url: repoUrl, repo_type: repoType, token: token || undefined, force }));
      };

      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === 'progress') {
            onProgress({ phase: msg.phase, percent: msg.percent });
          } else if (msg.type === 'done') {
            settled = true;
            resolve({
              fileTreeData: treeToFileList(msg.tree || []),
              readmeContent: msg.readme || '',
              defaultBranch: msg.default_branch || 'main',
            });
            ws.close();
          } else if (msg.type === 'error') {
            settled = true;
            reject(new Error(msg.message || 'Backend repo clone failed'));
            ws.close();
          }
        } catch {
          // Ignore unparsable frames rather than aborting an otherwise-working clone.
        }
      };

      ws.onerror = () => {
        if (!settled) {
          settled = true;
          reject(new Error('WebSocket error during repo clone'));
        }
      };

      ws.onclose = () => {
        if (!settled) {
          settled = true;
          reject(new Error('WebSocket closed before repo clone finished'));
        }
      };
    });
    return result;
  } catch (wsError) {
    console.warn('WS repo-clone failed, falling back to HTTP:', wsError);
    onProgress({ phase: 'Cloning', percent: 0 });
    try {
      const response = await fetch(`/api/repo/structure`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo_url: repoUrl, repo_type: repoType, token: token || undefined, force }),
      });
      if (!response.ok) return null;
      const data = await response.json();
      return {
        fileTreeData: treeToFileList(data.tree || []),
        readmeContent: data.readme || '',
        defaultBranch: data.default_branch || 'main',
      };
    } catch (httpError) {
      console.warn('HTTP repo-structure fallback also failed:', httpError);
      return null;
    }
  }
}

export interface WebsiteCrawlScope {
  mode: 'count' | 'subdomains' | 'all';
  maxPages: number;
  subdomains: string;
  respectRobots: boolean;
}

export interface WebsiteCrawlResult {
  fileTreeData: string;
  localDir: string;
  pageCount: number;
}

/**
 * Crawls a website with headless Chromium (see /ws/website/crawl in api.py)
 * and writes each page to disk as Markdown mirroring the site's URL
 * structure, mirroring fetchRepoStructureViaBackendClone's WS-then-HTTP
 * shape -- except there is no HTTP fallback here: a crawl is a genuinely
 * long-running streamed operation (unlike a repo clone, which the HTTP path
 * can do synchronously), so a WS failure is a hard failure for websites.
 */
export async function fetchWebsiteStructureViaCrawl(
  startUrl: string,
  scope: WebsiteCrawlScope,
  fresh: boolean,
  onProgress: (progress: CloneProgress | null) => void
): Promise<WebsiteCrawlResult | null> {
  const crawlWebSocketUrl = await getBackendWebSocketUrl('/ws/website/crawl');

  return new Promise<WebsiteCrawlResult | null>((resolve, reject) => {
    const ws = new WebSocket(crawlWebSocketUrl);
    let settled = false;
    // Crawls can legitimately take minutes for large scopes -- much longer
    // than a git clone -- so this uses its own generous timeout rather than
    // WEBSOCKET_CONNECT_TIMEOUT_MS (which is sized for connection setup).
    const timeout = setTimeout(() => {
      if (!settled) {
        settled = true;
        try { ws.close(); } catch {}
        console.warn('Website crawl timed out.');
        resolve(null);
      }
    }, 20 * 60 * 1000);

    ws.onopen = () => {
      ws.send(JSON.stringify({
        start_url: startUrl,
        fresh,
        scope: {
          mode: scope.mode,
          max_pages: scope.maxPages,
          subdomains: scope.subdomains,
          respect_robots: scope.respectRobots,
        },
      }));
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === 'progress') {
          onProgress({ phase: msg.message || 'Crawling…', percent: msg.percent ?? 0 });
        } else if (msg.type === 'done') {
          settled = true;
          clearTimeout(timeout);
          resolve({
            fileTreeData: treeToFileList(msg.tree || []),
            localDir: msg.local_dir || '',
            pageCount: msg.page_count || 0,
          });
          ws.close();
        } else if (msg.type === 'error') {
          settled = true;
          clearTimeout(timeout);
          console.warn('Website crawl failed:', msg.message);
          // Reject (not resolve(null)) so the specific backend reason (bot
          // challenge / robots.txt / HTTP error / unreachable -- see
          // ws_website_crawl in api.py) reaches the user instead of being
          // discarded in favor of a generic "crawl failed or returned no
          // pages" at the call site.
          reject(new Error(msg.message || 'Website crawl failed.'));
          ws.close();
        }
      } catch {
        // Ignore unparsable frames.
      }
    };

    ws.onerror = () => {
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        console.warn('WebSocket error during website crawl.');
        resolve(null);
      }
    };

    ws.onclose = () => {
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        resolve(null);
      }
    };
  });
}

export const createGithubHeaders = (githubToken: string): HeadersInit => {
  const headers: HeadersInit = {
    'Accept': 'application/vnd.github.v3+json'
  };

  if (githubToken) {
    headers['Authorization'] = `Bearer ${githubToken}`;
  }

  return headers;
};

export const createGitlabHeaders = (gitlabToken: string): HeadersInit => {
  const headers: HeadersInit = {
    'Content-Type': 'application/json',
  };

  if (gitlabToken) {
    headers['PRIVATE-TOKEN'] = gitlabToken;
  }

  return headers;
};

export const createBitbucketHeaders = (bitbucketToken: string): HeadersInit => {
  const headers: HeadersInit = {
    'Content-Type': 'application/json',
  };

  if (bitbucketToken) {
    headers['Authorization'] = `Bearer ${bitbucketToken}`;
  }

  return headers;
};