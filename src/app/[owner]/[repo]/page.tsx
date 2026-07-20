/* eslint-disable @typescript-eslint/no-unused-vars */
'use client';

import ChatWidget from '@/components/ChatWidget';
import Markdown from '@/components/Markdown';
import ModelSelectionModal, { AppliedModelSelection } from '@/components/ModelSelectionModal';
import ThemeToggle from '@/components/theme-toggle';
import WikiTreeView from '@/components/WikiTreeView';
import { useLanguage } from '@/contexts/LanguageContext';
import { RepoInfo } from '@/types/repoinfo';
import { getSavedApiCredentials } from '@/utils/apiCredentials';
import getRepoUrl from '@/utils/getRepoUrl';
import { WEBSOCKET_CONNECT_TIMEOUT_MS } from '@/utils/timeouts';
import { extractUrlDomain, extractUrlPath } from '@/utils/urlDecoder';
import { normalizeWikiPageCount } from '@/utils/wikiPageCount';
import Link from 'next/link';
import { useParams, useSearchParams } from 'next/navigation';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { FaBitbucket, FaBookOpen, FaDownload, FaEdit, FaExclamationTriangle, FaFileExport, FaFolder, FaGithub, FaGitlab, FaHistory, FaHome, FaMagic, FaSave, FaSync, FaTimes, FaTrash } from 'react-icons/fa';
// Define the WikiSection and WikiStructure types directly in this file
// since the imported types don't have the sections and rootSections properties
interface WikiSection {
  id: string;
  title: string;
  pages: string[];
  subsections?: string[];
}

interface WikiPage {
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

interface WikiStructure {
  id: string;
  title: string;
  description: string;
  pages: WikiPage[];
  sections: WikiSection[];
  rootSections: string[];
}

// One saved release (version) of a repository's wiki, returned by the backend's
// /api/wiki_cache/releases endpoint. Used to populate the "Wiki Release" dropdown.
interface WikiRelease {
  version: number;
  created_at: number;
  comprehensive: boolean | null;
  page_count: number;
  provider: string | null;
  model: string | null;
  title: string | null;
  id: string;
}

// Add CSS styles for wiki with Japanese aesthetic
const wikiStyles = `
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
const getCacheKey = (owner: string, repo: string, repoType: string, language: string, isComprehensive: boolean = true, pageCount: number = 10): string => {
  return `freedeepwiki_cache_${repoType}_${owner}_${repo}_${language}_${isComprehensive ? 'comprehensive' : 'concise'}_${pageCount}`;
};

// Helper function to add tokens and other parameters to request body
const addTokensToRequestBody = (
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
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

const createGithubHeaders = (githubToken: string): HeadersInit => {
  const headers: HeadersInit = {
    'Accept': 'application/vnd.github.v3+json'
  };

  if (githubToken) {
    headers['Authorization'] = `Bearer ${githubToken}`;
  }

  return headers;
};

const createGitlabHeaders = (gitlabToken: string): HeadersInit => {
  const headers: HeadersInit = {
    'Content-Type': 'application/json',
  };

  if (gitlabToken) {
    headers['PRIVATE-TOKEN'] = gitlabToken;
  }

  return headers;
};

const createBitbucketHeaders = (bitbucketToken: string): HeadersInit => {
  const headers: HeadersInit = {
    'Content-Type': 'application/json',
  };

  if (bitbucketToken) {
    headers['Authorization'] = `Bearer ${bitbucketToken}`;
  }

  return headers;
};


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
  const [error, setError] = useState<string | null>(null);
  const [wikiStructure, setWikiStructure] = useState<WikiStructure | undefined>();
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

        const filePaths = page.filePaths;

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

        // Create the prompt content - simplified to avoid message dialogs
 const promptContent =
`You are an expert technical writer and software architect.
Your task is to generate a comprehensive and accurate technical wiki page in Markdown format about a specific feature, system, or module within a given software project.

You will be given:
1. The "[WIKI_PAGE_TOPIC]" for the page you need to create.
2. A list of "[RELEVANT_SOURCE_FILES]" from the project that you MUST use as the sole basis for the content. You have access to the full content of these files. You MUST use AT LEAST 5 relevant source files for comprehensive coverage - if fewer are provided, search for additional related files in the codebase.

CRITICAL STARTING INSTRUCTION:
The very first thing on the page MUST be a \`<details>\` block listing ALL the \`[RELEVANT_SOURCE_FILES]\` you used to generate the content. There MUST be AT LEAST 5 source files listed - if fewer were provided, you MUST find additional related files to include.
Format it exactly like this:
<details>
<summary>Relevant source files</summary>

Remember, do not provide any acknowledgements, disclaimers, apologies, or any other preface before the \`<details>\` block. JUST START with the \`<details>\` block.
The following files were used as context for generating this wiki page:

${filePaths.map(path => `- [${path}](${generateFileUrl(path)})`).join('\n')}
<!-- Add additional relevant files if fewer than 5 were provided -->
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
    *   For EVERY piece of significant information, explanation, diagram, table entry, or code snippet, you MUST cite the specific source file(s) and relevant line numbers from which the information was derived.
    *   Place citations at the end of the paragraph, under the diagram/table, or after the code snippet.
    *   Use the exact format: \`Sources: [filename.ext:start_line-end_line]()\` for a range, or \`Sources: [filename.ext:line_number]()\` for a single line. Multiple files can be cited: \`Sources: [file1.ext:1-10](), [file2.ext:5](), [dir/file3.ext]()\` (if the whole file is relevant and line numbers are not applicable or too broad).
    *   If an entire section is overwhelmingly based on one or two files, you can cite them under the section heading in addition to more specific citations within the section.
    *   IMPORTANT: You MUST cite AT LEAST 5 different source files throughout the wiki page to ensure comprehensive coverage.

7.  **Technical Accuracy:** All information must be derived SOLELY from the \`[RELEVANT_SOURCE_FILES]\`. Do not infer, invent, or use external knowledge about similar systems or common practices unless it's directly supported by the provided code. If information is not present in the provided files, do not include it or explicitly state its absence if crucial to the topic.

8.  **Clarity and Conciseness:** Use clear, professional, and concise technical language suitable for other developers working on or learning about the project. Avoid unnecessary jargon, but use correct technical terms where appropriate.

9.  **Conclusion/Summary:** End with a brief summary paragraph if appropriate for "${page.title}", reiterating the key aspects covered and their significance within the project.

IMPORTANT: Generate the content in ${language === 'en' ? 'English' :
            language === 'ja' ? 'Japanese (日本語)' :
            language === 'zh' ? 'Mandarin Chinese (中文)' :
            language === 'zh-tw' ? 'Traditional Chinese (繁體中文)' :
            language === 'es' ? 'Spanish (Español)' :
            language === 'kr' ? 'Korean (한국어)' :
            language === 'vi' ? 'Vietnamese (Tiếng Việt)' : 
            language === "pt-br" ? "Brazilian Portuguese (Português Brasileiro)" :
            language === "fr" ? "Français (French)" :
            language === "ru" ? "Русский (Russian)" :
            'English'} language.

Remember:
- Ground every claim in the provided source files.
- Prioritize accuracy and direct representation of the code's functionality and structure.
- Structure the document logically for easy understanding by other developers.
`;

        // Prepare request body
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const requestBody: Record<string, any> = {
          repo_url: repoUrl,
          type: effectiveRepoInfo.type,
          retrieval_query: [
            page.title,
            page.content,
            `Relevant files: ${filePaths.join(', ')}`,
          ].filter(Boolean).join('\n'),
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
          // Create WebSocket URL from the server base URL
          const serverBaseUrl = process.env.SERVER_BASE_URL || 'http://localhost:8001';
          const wsBaseUrl = serverBaseUrl.replace(/^http/, 'ws')? serverBaseUrl.replace(/^https/, 'wss'): serverBaseUrl.replace(/^http/, 'ws');
          const wsUrl = `${wsBaseUrl}/ws/chat`;

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
          await new Promise<void>((resolve, reject) => {
            // Handle incoming messages
            ws.onmessage = (event) => {
              content += event.data;
            };

            // Handle WebSocket close
            ws.onclose = () => {
              console.log(`WebSocket connection closed for page: ${page.title}`);
              resolve();
            };

            // Handle WebSocket errors
            ws.onerror = (error) => {
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

          if (!reader) {
            throw new Error('Failed to get response reader');
          }

          try {
            while (true) {
              const { done, value } = await reader.read();
              if (done) break;
              content += decoder.decode(value, { stream: true });
            }
            // Ensure final decoding
            content += decoder.decode();
          } catch (readError) {
            console.error('Error reading stream:', readError);
            throw new Error('Error processing response stream');
          }
        }

        // Clean up markdown delimiters
        content = content.replace(/^```markdown\s*/i, '').replace(/```\s*$/i, '');

        console.log(`Received content for ${page.title}, length: ${content.length} characters`);

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
  }, [generatedPages, currentToken, effectiveRepoInfo, selectedProviderState, selectedModelState, isCustomSelectedModelState, customSelectedModelState, modelExcludedDirs, modelExcludedFiles, language, activeContentRequests, generateFileUrl]);

  // Determine the wiki structure from repository data
  const determineWikiStructure = useCallback(async (fileTree: string, readme: string, owner: string, repo: string) => {
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

      // Get repository URL
      const repoUrl = getRepoUrl(effectiveRepoInfo);

      // Prepare request body
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const requestBody: Record<string, any> = {
        repo_url: repoUrl,
        type: effectiveRepoInfo.type,
        retrieval_query: `Plan a ${isComprehensiveView ? 'comprehensive' : 'concise'} ${pageCount}-page technical wiki for ${owner}/${repo}. Focus on architecture, features, data flow, deployment, and the files named in the repository tree.`,
        messages: [{
          role: 'user',
content: `Analyze this GitHub repository ${owner}/${repo} and create a wiki structure for it.

1. The complete file tree of the project:
<file_tree>
${fileTree}
</file_tree>

2. The README file of the project:
<readme>
${readme}
</readme>

I want to create a wiki for this repository. Determine the most logical structure for a wiki based on the repository's content.

IMPORTANT: The wiki content will be generated in ${language === 'en' ? 'English' :
            language === 'ja' ? 'Japanese (日本語)' :
            language === 'zh' ? 'Mandarin Chinese (中文)' :
            language === 'zh-tw' ? 'Traditional Chinese (繁體中文)' :
            language === 'es' ? 'Spanish (Español)' :
            language === 'kr' ? 'Korean (한国語)' :
            language === 'vi' ? 'Vietnamese (Tiếng Việt)' :
            language === "pt-br" ? "Brazilian Portuguese (Português Brasileiro)" :
            language === "fr" ? "Français (French)" :
            language === "ru" ? "Русский (Russian)" :
            'English'} language.

When designing the wiki structure, include pages that would benefit from visual diagrams, such as:
- Architecture overviews
- Data flow descriptions
- Component relationships
- Process workflows
- State machines
- Class hierarchies

${isComprehensiveView ? `
Create a structured wiki with the following main sections:
- Overview (general information about the project)
- System Architecture (how the system is designed)
- Core Features (key functionality)
- Data Management/Flow: If applicable, how data is stored, processed, accessed, and managed (e.g., database schema, data pipelines, state management).
- Frontend Components (UI elements, if applicable.)
- Backend Systems (server-side components)
- Model Integration (AI model connections)
- Deployment/Infrastructure (how to deploy, what's the infrastructure like)
- Extensibility and Customization: If the project architecture supports it, explain how to extend or customize its functionality (e.g., plugins, theming, custom modules, hooks).

Each section should contain relevant pages. For example, the "Frontend Components" section might include pages for "Home Page", "Repository Wiki Page", "Ask Component", etc.

Return your analysis in the following XML format:

<wiki_structure>
  <title>[Overall title for the wiki]</title>
  <description>[Brief description of the repository]</description>
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
    <!-- More sections as needed -->
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
  <description>[Brief description of the repository]</description>
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
1. Create exactly ${pageCount} pages that make a ${isComprehensiveView ? 'comprehensive' : 'concise'} wiki for this repository. Do not return more or fewer than ${pageCount} <page> elements.
2. Each page should focus on a specific aspect of the codebase (e.g., architecture, key features, setup)
3. The relevant_files should be actual files from the repository that would be used to generate that page
4. Return ONLY valid XML with the structure specified above, with no markdown code block delimiters`
        }]
      };

      // Add tokens if available
      addTokensToRequestBody(requestBody, currentToken, effectiveRepoInfo.type, selectedProviderState, selectedModelState, isCustomSelectedModelState, customSelectedModelState, language, modelExcludedDirs, modelExcludedFiles, modelIncludedDirs, modelIncludedFiles);

      // Use WebSocket for communication
      let responseText = '';

      try {
        // Create WebSocket URL from the server base URL
        const serverBaseUrl = process.env.SERVER_BASE_URL || 'http://localhost:8001';
        const wsBaseUrl = serverBaseUrl.replace(/^http/, 'ws')? serverBaseUrl.replace(/^https/, 'wss'): serverBaseUrl.replace(/^http/, 'ws');
        const wsUrl = `${wsBaseUrl}/ws/chat`;

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
        await new Promise<void>((resolve, reject) => {
          // Handle incoming messages
          ws.onmessage = (event) => {
            responseText += event.data;
          };

          // Handle WebSocket close
          ws.onclose = () => {
            console.log('WebSocket connection closed for wiki structure');
            resolve();
          };

          // Handle WebSocket errors
          ws.onerror = (error) => {
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

        if (!reader) {
          throw new Error('Failed to get response reader');
        }

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          responseText += decoder.decode(value, { stream: true });
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

      if (parseError && (!pagesEls || pagesEls.length === 0)) {
        console.warn('DOM parsing failed, trying regex fallback');
      }

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
      setCurrentPageId(pages.length > 0 ? pages[0].id : undefined);

      // Start generating content for all pages with controlled concurrency
      if (pages.length > 0) {
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
          if (queue.length === 0 && activeRequests === 0 && pages.length > 0 && pagesInProgress.size === 0) {
            // This handles the case where the queue might finish before the finally blocks fully update activeRequests
            // or if the initial queue was processed very quickly
            console.log("Queue empty and no active requests after loop, ensuring loading is false.");
            setIsLoading(false);
            setLoadingMessage(undefined);
          } else if (pages.length === 0) {
            // Handle case where there were no pages to begin with
            setIsLoading(false);
            setLoadingMessage(undefined);
          }
        };

        // Start processing the queue
        processQueue();
      } else {
        // Set loading to false if there were no pages found
        setIsLoading(false);
        setLoadingMessage(undefined);
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
            ? 'Se interrumpió la conexión con el backend de FreeDeepWiki. El repositorio es válido; vuelve a intentar la generación.'
            : 'The connection to the FreeDeepWiki backend was interrupted. The repository is valid; retry the generation.'
          : message
      );
      setLoadingMessage(undefined);
    } finally {
      setStructureRequestInProgress(false);
    }
  }, [generatePageContent, currentToken, effectiveRepoInfo, pagesInProgress.size, structureRequestInProgress, selectedProviderState, selectedModelState, isCustomSelectedModelState, customSelectedModelState, modelExcludedDirs, modelExcludedFiles, language, messages.loading, isComprehensiveView, pageCount]);

  // Fetch repository structure using GitHub or GitLab API
  const fetchRepositoryStructure = useCallback(async () => {
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

    try {
      // Set the request in progress flag
      setRequestInProgress(true);

      // Update loading state
      setIsLoading(true);
      setLoadingMessage(messages.loading?.fetchingStructure || 'Fetching repository structure...');

      let fileTreeData = '';
      let readmeContent = '';

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
            throw new TypeError('NetworkError while contacting the FreeDeepWiki GitHub proxy');
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

      // Now determine the wiki structure
      await determineWikiStructure(fileTreeData, readmeContent, owner, repo);

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
            ? 'No se pudo contactar con el backend de FreeDeepWiki. El repositorio no es el problema; comprueba que el servicio siga activo y vuelve a intentarlo.'
            : 'Could not contact the FreeDeepWiki backend. The repository is not the problem; check that the service is running and retry.'
          : message
      );
      setLoadingMessage(undefined);
    } finally {
      // Reset the request in progress flag
      setRequestInProgress(false);
    }
  }, [owner, repo, determineWikiStructure, currentToken, effectiveRepoInfo, requestInProgress, messages.loading]);

  // Function to export wiki content
  const exportWiki = useCallback(async (format: 'markdown' | 'json' | 'obsidian') => {
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

      // Make API call to export wiki
      const response = await fetch(`/export/wiki`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          repo_url: repoUrl,
          type: effectiveRepoInfo.type,
          pages: pagesToExport,
          format,
          title: wikiStructure.title,
          version: selectedWikiVersion ?? undefined,
        })
      });

      if (!response.ok) {
        const errorText = await response.text().catch(() => 'No error details available');
        throw new Error(`Error exporting wiki: ${response.status} - ${errorText}`);
      }

      // Get the filename from the Content-Disposition header if available
      const contentDisposition = response.headers.get('Content-Disposition');
      const defaultExt = format === 'markdown' ? 'md' : format === 'obsidian' ? 'zip' : 'json';
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
  }, [wikiStructure, generatedPages, effectiveRepoInfo, language, selectedWikiVersion]);

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
  }, [effectiveRepoInfo, language, messages.loading, activeContentRequests, selectedProviderState, selectedModelState, isCustomSelectedModelState, customSelectedModelState, modelExcludedDirs, modelExcludedFiles, isComprehensiveView, pageCount, authCode, authRequired]);

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
          forceFreshGeneration.current = false;
          console.log('Refresh requested: skipping server cache, regenerating wiki.');
          fetchRepositoryStructure();
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
  }, [effectiveRepoInfo, effectiveRepoInfo.owner, effectiveRepoInfo.repo, effectiveRepoInfo.type, language, fetchRepositoryStructure, messages.loading?.fetchingCache, isComprehensiveView, pageCount, refreshTrigger]);

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
  }, [isLoading, error, wikiStructure, generatedPages, effectiveRepoInfo.owner, effectiveRepoInfo.repo, effectiveRepoInfo.type, effectiveRepoInfo.repoUrl, repoUrl, language, isComprehensiveView, pageCount, selectedProviderState, selectedModelState, loadWikiReleases]);

  const handlePageSelect = (pageId: string) => {
    if (currentPageId != pageId) {
      setCurrentPageId(pageId)
      // Unsaved edits are scoped to the page being edited -- navigating
      // away discards them rather than silently carrying them to whatever
      // page is selected next.
      setIsEditingPage(false);
      setEditError(null);
    }
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
    try {
      const response = await fetch('/api/wiki/page/edit/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
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
        rewritten += decoder.decode(value, { stream: true });
        setEditedContent(rewritten);
      }
      rewritten += decoder.decode();
      // Same cleanup as the initial page-generation flow, in case the model
      // wraps its answer in a fence despite being told not to.
      setEditedContent(rewritten.replace(/^```markdown\s*/i, '').replace(/```\s*$/i, ''));
    } catch (err) {
      console.error('AI page edit failed:', err);
      setEditError(err instanceof Error ? err.message : 'AI edit failed');
    } finally {
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
                  ? 'La conexión con el servicio se interrumpió durante el análisis. Puedes volver a intentarlo sin cambiar la URL del repositorio.'
                  : 'The service connection was interrupted during analysis. You can retry without changing the repository URL.'
              ) : embeddingError ? (
                messages.repoPage?.embeddingErrorDefault || 'This error is related to the document embedding system used for analyzing your repository. Please verify your embedding model configuration, API keys, and try again. If the issue persists, consider switching to a different embedding provider in the model settings.'
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
              {currentPageId && generatedPages[currentPageId] ? (
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
      />
    </div>
  );
}
