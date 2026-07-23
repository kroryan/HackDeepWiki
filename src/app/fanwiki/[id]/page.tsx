'use client';

import ChatWidget from '@/components/ChatWidget';
import FileBrowserModal from '@/components/FileBrowserModal';
import Markdown from '@/components/Markdown';
import ThemeToggle from '@/components/theme-toggle';
import { useLanguage } from '@/contexts/LanguageContext';
import RepoInfo from '@/types/repoinfo';
import Link from 'next/link';
import { useParams, useRouter, useSearchParams } from 'next/navigation';
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  FaArchive,
  FaBookOpen,
  FaExternalLinkAlt,
  FaFolderOpen,
  FaHome,
  FaLink,
  FaMobileAlt,
  FaSearch,
} from 'react-icons/fa';

interface FanwikiMetadata {
  id: string;
  owner: string;
  repo: string;
  name: string;
  repo_type: 'fanwiki';
  submittedAt: number;
  start_url: string;
  page_count: number;
  description: string;
  main_page_path: string | null;
}

interface FanwikiIndexEntry {
  path: string;
  title: string;
  url: string;
  categories: string[];
}

interface FanwikiIndexResponse {
  entries: FanwikiIndexEntry[];
  truncated: boolean;
  totalArticles: number;
}

interface FanwikiPage extends FanwikiIndexEntry {
  content: string;
}

function resolveRelativePath(currentPath: string, value: string): string | null {
  const withoutFragment = value.split('#', 1)[0].split('?', 1)[0].trim();
  if (
    !withoutFragment ||
    withoutFragment.startsWith('#') ||
    /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(withoutFragment)
  ) {
    return null;
  }

  // Keep percent-encoded MediaWiki title characters intact: they are also
  // present in the imported filename (`Category%3AFoo.md`).
  const base = withoutFragment.startsWith('/')
    ? []
    : currentPath.split('/').slice(0, -1);
  const parts = [...base, ...withoutFragment.replace(/^\/+/, '').split('/')];
  const normalized: string[] = [];
  for (const part of parts) {
    if (!part || part === '.') continue;
    if (part === '..') {
      normalized.pop();
    } else {
      normalized.push(part);
    }
  }
  return normalized.join('/');
}

export default function FanwikiReaderPage() {
  const params = useParams();
  const router = useRouter();
  const searchParams = useSearchParams();
  const fanwikiId = params.id as string;
  const pageParam = searchParams.get('page');
  const { language, messages } = useLanguage();

  const [metadata, setMetadata] = useState<FanwikiMetadata | null>(null);
  const [indexEntries, setIndexEntries] = useState<FanwikiIndexEntry[]>([]);
  const [indexTruncated, setIndexTruncated] = useState(false);
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<FanwikiIndexEntry[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [currentPath, setCurrentPath] = useState<string | null>(pageParam);
  const [currentPage, setCurrentPage] = useState<FanwikiPage | null>(null);
  const [isPageLoading, setIsPageLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pageRevision, setPageRevision] = useState(0);
  const [toolRunning, setToolRunning] = useState<'repair' | 'attach' | null>(null);
  const [toolMessage, setToolMessage] = useState<string | null>(null);
  const [toolError, setToolError] = useState<string | null>(null);
  const [isAttachModalOpen, setIsAttachModalOpen] = useState(false);
  const [isImagesBrowserOpen, setIsImagesBrowserOpen] = useState(false);
  const [imagesDir, setImagesDir] = useState('');

  const repoInfo: RepoInfo | null = useMemo(() => {
    if (!metadata) return null;
    return {
      owner: metadata.owner,
      repo: metadata.repo,
      type: 'fanwiki',
      token: null,
      localPath: null,
      repoUrl: metadata.start_url,
    };
  }, [metadata]);

  useEffect(() => {
    if (!fanwikiId) return;
    setError(null);
    fetch(`/api/fanwiki/${encodeURIComponent(fanwikiId)}`, { cache: 'no-store' })
      .then(async (response) => {
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(body.detail || body.error || 'No se pudo abrir la wiki importada.');
        }
        return body as FanwikiMetadata;
      })
      .then((data) => {
        setMetadata(data);
        setCurrentPath((existing) => existing || data.main_page_path);
      })
      .catch((reason) => {
        setError(reason instanceof Error ? reason.message : 'No se pudo abrir la wiki importada.');
      });
  }, [fanwikiId]);

  useEffect(() => {
    if (!fanwikiId) return;
    fetch(`/api/fanwiki/${encodeURIComponent(fanwikiId)}/index?limit=500`, { cache: 'no-store' })
      .then((response) => response.json())
      .then((data: FanwikiIndexResponse) => {
        setIndexEntries(Array.isArray(data.entries) ? data.entries : []);
        setIndexTruncated(Boolean(data.truncated));
      })
      .catch(() => setIndexEntries([]));
  }, [fanwikiId]);

  useEffect(() => {
    if (!pageParam) return;
    setCurrentPath(pageParam);
  }, [pageParam]);

  useEffect(() => {
    if (!fanwikiId || !currentPath) return;
    setIsPageLoading(true);
    setError(null);
    fetch(
      `/api/fanwiki/${encodeURIComponent(fanwikiId)}/page?path=${encodeURIComponent(currentPath)}`,
      { cache: 'no-store' },
    )
      .then(async (response) => {
        const body = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(body.detail || body.error || messages.fanwiki?.loadError || 'Article load failed.');
        }
        return body as FanwikiPage;
      })
      .then(setCurrentPage)
      .catch((reason) => {
        setCurrentPage(null);
        setError(reason instanceof Error ? reason.message : messages.fanwiki?.loadError || 'Article load failed.');
      })
      .finally(() => setIsPageLoading(false));
  }, [fanwikiId, currentPath, pageRevision]);

  useEffect(() => {
    const normalized = query.trim();
    if (!normalized) {
      setResults([]);
      setIsSearching(false);
      return;
    }
    setIsSearching(true);
    const timer = window.setTimeout(() => {
      fetch(
        `/api/fanwiki/${encodeURIComponent(fanwikiId)}/search?q=${encodeURIComponent(normalized)}&limit=50`,
        { cache: 'no-store' },
      )
        .then((response) => response.json())
        .then((data) => setResults(Array.isArray(data) ? data : []))
        .catch(() => setResults([]))
        .finally(() => setIsSearching(false));
    }, 180);
    return () => window.clearTimeout(timer);
  }, [fanwikiId, query]);

  const openPage = useCallback((path: string) => {
    setCurrentPath(path);
    setCurrentPage(null);
    router.push(`/fanwiki/${encodeURIComponent(fanwikiId)}?page=${encodeURIComponent(path)}`);
  }, [fanwikiId, router]);

  const resolveInternalLink = useCallback((href: string) => {
    if (!currentPath) return null;
    const resolved = resolveRelativePath(currentPath, href);
    return resolved?.toLowerCase().endsWith('.md') ? resolved : null;
  }, [currentPath]);

  const resolveImageUrl = useCallback((src: string) => {
    if (!currentPath || /^(?:data:|https?:|\/\/)/i.test(src)) return src;
    const resolved = resolveRelativePath(currentPath, src);
    return resolved
      ? `/api/fanwiki/${encodeURIComponent(fanwikiId)}/asset?path=${encodeURIComponent(resolved)}`
      : src;
  }, [currentPath, fanwikiId]);

  const repairLinks = useCallback(async () => {
    if (!metadata || toolRunning) return;
    setToolRunning('repair');
    setToolError(null);
    setToolMessage(null);
    try {
      const response = await fetch('/api/fanwiki/repair_links', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start_url: metadata.start_url }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail || body.error || 'No se pudieron reparar los enlaces.');
      }
      setToolMessage(
        `Enlaces revisados: ${Number(body.files_scanned || 0).toLocaleString()} archivos con marcadores, ` +
        `${Number(body.links_resolved || 0).toLocaleString()} resueltos y ` +
        `${Number(body.links_unresolved || 0).toLocaleString()} sin destino importado.`,
      );
      setPageRevision((value) => value + 1);
    } catch (reason) {
      setToolError(reason instanceof Error ? reason.message : 'No se pudieron reparar los enlaces.');
    } finally {
      setToolRunning(null);
    }
  }, [metadata, toolRunning]);

  const attachImages = useCallback(async () => {
    if (!metadata || toolRunning) return;
    const selectedDir = imagesDir.trim();
    if (!selectedDir) {
      setToolError(messages.fanwiki?.selectImageFolderFirst || 'Select an image folder first.');
      return;
    }
    setToolRunning('attach');
    setToolError(null);
    setToolMessage(null);
    try {
      const response = await fetch('/api/fanwiki/attach_images', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          start_url: metadata.start_url,
          images_dir: selectedDir,
        }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(body.detail || body.error || messages.fanwiki?.imagesAttachError || 'Failed to attach images.');
      }
      setToolMessage(
        `Imágenes revisadas: ${Number(body.files_scanned || 0).toLocaleString()} páginas, ` +
        `${Number(body.images_attached || 0).toLocaleString()} añadidas y ` +
        `${Number(body.images_still_missing || 0).toLocaleString()} aún no encontradas.`,
      );
      setIsAttachModalOpen(false);
      setPageRevision((value) => value + 1);
    } catch (reason) {
      setToolError(reason instanceof Error ? reason.message : messages.fanwiki?.imagesAttachError || 'Failed to attach images.');
    } finally {
      setToolRunning(null);
    }
  }, [imagesDir, metadata, toolRunning]);

  const visibleEntries = query ? results : indexEntries;

  return (
    <div
      className="h-screen overflow-hidden flex flex-col bg-[var(--background)]"
      data-testid="fanwiki-reader"
    >
      <header className="border-b border-[var(--border-color)] px-4 py-3 flex items-center justify-between gap-4 shrink-0">
        <div className="flex items-center gap-3 min-w-0">
          <Link href="/" className="text-[var(--muted)] hover:text-[var(--accent-primary)]" title="Inicio">
            <FaHome className="h-4 w-4" />
          </Link>
          <div className="min-w-0">
            <h1 className="font-medium text-[var(--foreground)] truncate" data-testid="fanwiki-title">
              {metadata?.name || 'Cargando…'}
            </h1>
            {metadata && (
              <p className="text-xs text-[var(--muted)]">
                {metadata.page_count.toLocaleString()} {messages.fanwiki?.articles || 'articles'} · {messages.fanwiki?.mediawikiXml || 'MediaWiki XML'}
              </p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <ThemeToggle />
        </div>
      </header>

      {metadata && (
        <div
          className="border-b border-[var(--border-color)] px-3 py-2 flex flex-wrap items-center gap-2 shrink-0 bg-[var(--card-bg)]/40"
          aria-label="Herramientas de la wiki importada"
          data-testid="fanwiki-tools"
        >
          <button
            type="button"
            onClick={() => {
              setToolError(null);
              setIsAttachModalOpen(true);
            }}
            disabled={toolRunning !== null}
            className="flex items-center gap-2 px-3 py-2 text-xs rounded-md border border-[var(--border-color)] text-[var(--foreground)] hover:border-[var(--accent-primary)] hover:text-[var(--accent-primary)] disabled:opacity-50 transition-colors"
            data-testid="fanwiki-attach-images"
          >
            <FaFolderOpen />
            {messages.fanwiki?.addImagesFolder || 'Add image folder'}
          </button>
          <button
            type="button"
            onClick={repairLinks}
            disabled={toolRunning !== null}
            className="flex items-center gap-2 px-3 py-2 text-xs rounded-md border border-[var(--border-color)] text-[var(--foreground)] hover:border-[var(--accent-primary)] hover:text-[var(--accent-primary)] disabled:opacity-50 transition-colors"
            title="La importación hace una primera reparación automáticamente; esta acción vuelve a comprobarla después de cambios o importaciones parciales."
            data-testid="fanwiki-repair-links"
          >
            <FaLink />
            {toolRunning === 'repair' ? (messages.fanwiki?.repairing || 'Repairing...') : (messages.fanwiki?.repairInternalLinks || 'Repair internal links')}
          </button>
          <a
            href={`/api/fanwiki/${encodeURIComponent(fanwikiId)}/export/obsidian`}
            className="flex items-center gap-2 px-3 py-2 text-xs rounded-md border border-[var(--border-color)] text-[var(--foreground)] hover:border-[var(--accent-primary)] hover:text-[var(--accent-primary)] transition-colors"
            title="Descargar todos los artículos e imágenes como una bóveda Obsidian"
            data-testid="fanwiki-export-obsidian"
          >
            <FaBookOpen />
            {messages.fanwiki?.exportObsidian || 'Export Obsidian'}
          </a>
          <a
            href={`/api/fanwiki/${encodeURIComponent(fanwikiId)}/export/hdwreader`}
            className="flex items-center gap-2 px-3 py-2 text-xs rounded-md border border-[var(--border-color)] text-[var(--foreground)] hover:border-[var(--accent-primary)] hover:text-[var(--accent-primary)] transition-colors"
            title="Descargar una wiki portátil para HackDeepWikiReader"
            data-testid="fanwiki-export-hdwreader"
          >
            <FaMobileAlt />
            {messages.fanwiki?.exportHDWReader || 'Export HDWReader'}
          </a>
          <a
            href={`/api/fanwiki/${encodeURIComponent(fanwikiId)}/export/zim`}
            className="flex items-center gap-2 px-3 py-2 text-xs rounded-md border border-[var(--border-color)] text-[var(--foreground)] hover:border-[var(--accent-primary)] hover:text-[var(--accent-primary)] transition-colors"
            title="Descargar como archivo .zim offline -- legible en Kiwix o cualquier lector ZIM sin conexión a internet"
            data-testid="fanwiki-export-zim"
          >
            <FaArchive />
            {messages.fanwiki?.exportZIM || 'Export ZIM'}
          </a>
          <span className="ml-auto text-[11px] text-[var(--muted)]">
            {messages.fanwiki?.initialRepairNote || 'Initial repair runs automatically on import.'}
          </span>
        </div>
      )}

      {error && (
        <div className="px-4 py-2 border-b border-[var(--highlight)]/30 bg-[var(--highlight)]/10 text-[var(--highlight)] text-sm">
          {error}
        </div>
      )}
      {toolError && (
        <div className="px-4 py-2 border-b border-red-500/30 bg-red-500/10 text-red-400 text-sm">
          {toolError}
        </div>
      )}
      {toolMessage && (
        <div className="px-4 py-2 border-b border-emerald-500/30 bg-emerald-500/10 text-emerald-400 text-sm">
          {toolMessage}
        </div>
      )}

      <div className="flex-1 flex min-h-0">
        <aside className="w-72 md:w-80 shrink-0 border-r border-[var(--border-color)] flex flex-col min-h-0">
          <div className="p-3 border-b border-[var(--border-color)]">
            <div className="relative">
              <FaSearch className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--muted)] h-3.5 w-3.5" />
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={messages.fanwiki?.searchPlaceholder || 'Search articles...'}
                aria-label={messages.fanwiki?.searchPlaceholder || 'Search articles'}
                className="input-japanese w-full pl-9 pr-3 py-2 rounded-lg border-[var(--border-color)] bg-transparent text-sm text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
              />
            </div>
          </div>
          <div className="flex-1 overflow-y-auto" data-testid="fanwiki-index">
            {isSearching && (
              <p className="p-3 text-xs text-[var(--muted)]">{messages.fanwiki?.searching || 'Searching...'}</p>
            )}
            {!isSearching && query && visibleEntries.length === 0 && (
              <p className="p-3 text-xs text-[var(--muted)]">{messages.fanwiki?.noResults || 'No results.'}</p>
            )}
            {visibleEntries.map((entry) => (
              <button
                key={entry.path}
                type="button"
                onClick={() => openPage(entry.path)}
                className={`block w-full text-left px-3 py-2 text-sm border-b border-[var(--border-color)]/50 hover:bg-[var(--card-bg)] transition-colors ${
                  currentPath === entry.path
                    ? 'bg-[var(--accent-primary)]/10 text-[var(--accent-primary)]'
                    : 'text-[var(--foreground)]'
                }`}
              >
                {entry.title}
              </button>
            ))}
            {!query && indexTruncated && (
              <p className="p-3 text-xs text-[var(--muted)]">
                Mostrando los primeros {indexEntries.length.toLocaleString()} artículos. Usa la búsqueda para acceder al resto.
              </p>
            )}
          </div>
        </aside>

        <main id="fanwiki-content" className="flex-1 min-w-0 overflow-y-auto">
          {isPageLoading && (
            <div className="h-full flex items-center justify-center text-sm text-[var(--muted)]">
              {messages.fanwiki?.loadingArticle || 'Loading article...'}
            </div>
          )}
          {!isPageLoading && currentPage && (
            <article className="max-w-5xl mx-auto px-6 py-8" data-testid="fanwiki-article">
              <div className="mb-6 border-b border-[var(--border-color)] pb-4 min-w-0">
                <div className="flex items-start justify-between gap-4">
                  <h2 className="text-2xl md:text-3xl font-bold font-serif text-[var(--foreground)]">
                    {currentPage.title}
                  </h2>
                  {currentPage.url && (
                    <a
                      href={currentPage.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="shrink-0 text-xs text-[var(--muted)] hover:text-[var(--accent-primary)] flex items-center gap-1.5"
                    >
                      {messages.fanwiki?.source || 'Source'} <FaExternalLinkAlt className="h-3 w-3" />
                    </a>
                  )}
                </div>
                {currentPage.categories.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {currentPage.categories.slice(0, 12).map((category) => (
                      <span
                        key={category}
                        title={category}
                        className="px-2 py-0.5 rounded-full text-[11px] border border-[var(--border-color)] text-[var(--muted)] max-w-full truncate"
                      >
                        {category}
                      </span>
                    ))}
                  </div>
                )}
              </div>
              <Markdown
                content={currentPage.content}
                repoInfo={repoInfo || undefined}
                resolveInternalLink={resolveInternalLink}
                onInternalLink={openPage}
                resolveImageUrl={resolveImageUrl}
              />
            </article>
          )}
          {!isPageLoading && !currentPage && !error && (
            <div className="h-full flex items-center justify-center text-sm text-[var(--muted)]">
              {messages.fanwiki?.selectArticle || 'Select an article to read.'}
            </div>
          )}
        </main>
      </div>

      <ChatWidget
        repoInfo={repoInfo}
        language={language}
        currentPageId={currentPath || undefined}
        title={messages.ask?.title || 'Wiki chat'}
        fabAriaLabel={messages.ask?.title || 'Ask this wiki'}
      />

      {isAttachModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/55 p-4">
          <div className="w-full max-w-lg rounded-lg border border-[var(--border-color)] bg-[var(--card-bg)] p-5 shadow-custom">
            <h2 className="text-lg font-semibold text-[var(--foreground)]">
              {messages.fanwiki?.addImagesTo || 'Add images to'} {metadata?.name}
            </h2>
            <p className="mt-2 text-sm text-[var(--muted)]">
              {messages.fanwiki?.addImagesDesc || 'Select a local folder. Images referenced by the XML will be copied and articles updated.'}
            </p>
            <div className="mt-4 flex gap-2">
              <input
                type="text"
                value={imagesDir}
                onChange={(event) => setImagesDir(event.target.value)}
                placeholder="/ruta/a/las/imagenes"
                className="input-japanese min-w-0 flex-1 rounded-md border border-[var(--border-color)] bg-transparent px-3 py-2 text-sm text-[var(--foreground)]"
                aria-label="Carpeta de imágenes"
              />
              <button
                type="button"
                onClick={() => setIsImagesBrowserOpen(true)}
                className="px-3 py-2 text-sm rounded-md border border-[var(--border-color)] text-[var(--foreground)] hover:border-[var(--accent-primary)]"
              >
                {messages.fanwiki?.browse || 'Browse'}
              </button>
            </div>
            {toolError && (
              <p className="mt-3 text-sm text-red-400">{toolError}</p>
            )}
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setIsAttachModalOpen(false)}
                disabled={toolRunning === 'attach'}
                className="px-4 py-2 text-sm rounded-md text-[var(--foreground)] hover:bg-[var(--background)] disabled:opacity-50"
              >
                {messages.common?.cancel || 'Cancel'}
              </button>
              <button
                type="button"
                onClick={attachImages}
                disabled={toolRunning === 'attach' || !imagesDir.trim()}
                className="btn-japanese px-4 py-2 text-sm rounded-md disabled:opacity-50"
              >
                {toolRunning === 'attach' ? (messages.fanwiki?.adding || 'Adding...') : (messages.fanwiki?.addImages || 'Add images')}
              </button>
            </div>
          </div>
        </div>
      )}

      <FileBrowserModal
        isOpen={isImagesBrowserOpen}
        onClose={() => setIsImagesBrowserOpen(false)}
        onSelect={setImagesDir}
        mode="directory"
        initialPath={imagesDir || undefined}
        title={messages.fanwiki?.selectImageFolder || 'Select image folder'}
      />
    </div>
  );
}
