'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import React, { useEffect, useState } from 'react';
import { FaHome, FaSearch } from 'react-icons/fa';
import ThemeToggle from '@/components/theme-toggle';

interface ZimMetadata {
  id: string;
  path: string;
  title: string;
  description: string;
  articleCount: number;
  mainEntryPath: string | null;
}

interface ZimSearchHit {
  path: string;
  title: string;
}

export default function ZimReaderPage() {
  const params = useParams();
  const zimId = params.id as string;

  const [metadata, setMetadata] = useState<ZimMetadata | null>(null);
  const [metaError, setMetaError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<ZimSearchHit[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [currentPath, setCurrentPath] = useState<string | null>(null);

  useEffect(() => {
    if (!zimId) return;
    fetch(`/api/zim/${zimId}`)
      .then(async (res) => {
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail || body.error || 'Failed to load ZIM metadata');
        }
        return res.json();
      })
      .then((data: ZimMetadata) => {
        setMetadata(data);
        setCurrentPath(data.mainEntryPath);
      })
      .catch((e) => setMetaError(e instanceof Error ? e.message : 'Failed to load ZIM metadata'));
  }, [zimId]);

  const runSearch = async (q: string) => {
    if (!q.trim()) {
      setResults([]);
      return;
    }
    setIsSearching(true);
    try {
      const res = await fetch(`/api/zim/${zimId}/search?q=${encodeURIComponent(q)}&limit=30`);
      const data = await res.json();
      setResults(Array.isArray(data) ? data : []);
    } catch {
      setResults([]);
    } finally {
      setIsSearching(false);
    }
  };

  return (
    <div className="min-h-screen flex flex-col bg-[var(--background)]">
      <header className="border-b border-[var(--border-color)] px-4 py-3 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-3 min-w-0">
          <Link href="/" className="text-[var(--muted)] hover:text-[var(--accent-primary)]" title="Home">
            <FaHome className="h-4 w-4" />
          </Link>
          <div className="min-w-0">
            <h1 className="font-medium text-[var(--foreground)] truncate">
              {metadata?.title || 'Loading…'}
            </h1>
            {metadata && (
              <p className="text-xs text-[var(--muted)]">
                {metadata.articleCount.toLocaleString()} articles
              </p>
            )}
          </div>
        </div>
        <ThemeToggle />
      </header>

      {metaError && (
        <div className="p-4 text-[var(--highlight)] text-sm">{metaError}</div>
      )}

      <div className="flex-1 flex min-h-0">
        {/* Search + results panel */}
        <div className="w-80 shrink-0 border-r border-[var(--border-color)] flex flex-col min-h-0">
          <div className="p-3 border-b border-[var(--border-color)]">
            <div className="relative">
              <FaSearch className="absolute left-3 top-1/2 -translate-y-1/2 text-[var(--muted)] h-3.5 w-3.5" />
              <input
                type="text"
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  runSearch(e.target.value);
                }}
                placeholder="Search this .zim…"
                className="input-japanese w-full pl-9 pr-3 py-2 rounded-lg border-[var(--border-color)] bg-transparent text-sm text-[var(--foreground)] focus:outline-none focus:border-[var(--accent-primary)]"
              />
            </div>
          </div>
          <div className="flex-1 overflow-y-auto">
            {isSearching && (
              <p className="p-3 text-xs text-[var(--muted)]">Searching…</p>
            )}
            {!isSearching && query && results.length === 0 && (
              <p className="p-3 text-xs text-[var(--muted)]">No results.</p>
            )}
            {results.map((hit) => (
              <button
                key={hit.path}
                type="button"
                onClick={() => setCurrentPath(hit.path)}
                className={`block w-full text-left px-3 py-2 text-sm border-b border-[var(--border-color)]/50 hover:bg-[var(--card-bg)] transition-colors ${
                  currentPath === hit.path ? 'bg-[var(--accent-primary)]/10 text-[var(--accent-primary)]' : 'text-[var(--foreground)]'
                }`}
              >
                {hit.title}
              </button>
            ))}
          </div>
        </div>

        {/* Reader panel container follows the app theme; the iframe's own
            document (third-party ZIM content with its own CSS) cannot be
            re-themed from here without risky script injection into
            sandboxed, untrusted HTML -- only the surrounding chrome does. */}
        <div className="flex-1 min-h-0 bg-[var(--background)]">
          {currentPath ? (
            <iframe
              key={currentPath}
              src={`/api/zim/${zimId}/entry?path=${encodeURIComponent(currentPath)}`}
              sandbox="allow-scripts"
              className="w-full h-full border-0"
              title="ZIM entry"
            />
          ) : (
            <div className="flex items-center justify-center h-full text-[var(--muted)] text-sm">
              {metadata ? 'Search or pick an article to start reading.' : 'Loading…'}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
