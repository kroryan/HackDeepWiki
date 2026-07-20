'use client';

import React, { useEffect, useState } from 'react';
import { FaTimes } from 'react-icons/fa';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { tomorrow } from 'react-syntax-highlighter/dist/cjs/styles/prism';
import RepoInfo from '@/types/repoinfo';
import getRepoUrl from '@/utils/getRepoUrl';
import { getLanguageFromPath } from '@/utils/codeLanguage';

interface CodeViewerProps {
  filePath: string;
  repoInfo: RepoInfo;
  onClose: () => void;
}

// In-app viewer for a `codefile:<path>` citation clicked in a repo chat
// answer (see api/search_tool.py's format_sources_footer and
// Markdown.tsx's `a` component, which intercepts those links instead of
// letting the browser try to navigate to a fake URL). Fetches the file's
// full, untruncated content via POST /api/wiki/file_content -- POST (not a
// query string) so a private repo's access token never ends up in a URL.
//
// Same tab, same page -- a fixed panel filling most of the viewport
// (mirrors ChatWidget's maximized state, `inset-4 sm:inset-8`), not a
// separate window/tab and not a small centered dialog. A code file is
// usually read at length, so it defaults to that large size instead of
// needing a maximize toggle first.
export default function CodeViewer({ filePath, repoInfo, onClose }: CodeViewerProps) {
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setError(null);
    setContent(null);

    fetch('/api/wiki/file_content', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        repo_url: getRepoUrl(repoInfo),
        file_path: filePath,
        repo_type: repoInfo.type,
        token: repoInfo.token || undefined,
      }),
    })
      .then(async (res) => {
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body.detail || `Failed to load file (${res.status})`);
        }
        return res.json();
      })
      .then((data: { file_path: string; content: string }) => {
        if (!cancelled) setContent(data.content);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load file');
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [filePath, repoInfo]);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [onClose]);

  return (
    <div
      className="fixed inset-4 sm:inset-8 z-[100] flex flex-col rounded-xl border border-[var(--border-color)] bg-[var(--card-bg)] shadow-[0_8px_40px_rgba(0,0,0,0.35),0_0_0_1px_var(--border-color)] backdrop-blur-xl overflow-hidden"
    >
      <div className="h-[2px] w-full shrink-0 bg-gradient-to-r from-[var(--accent-primary)] via-[var(--accent-secondary)] to-transparent" />
      <div className="flex items-center justify-between px-4 py-3 border-b border-[var(--border-color)] shrink-0">
        <span className="font-mono text-sm text-[var(--foreground)] truncate">{filePath}</span>
        <div className="flex items-center gap-1.5 shrink-0">
          {content !== null && (
            <button
              onClick={() => navigator.clipboard.writeText(content)}
              className="text-xs text-[var(--muted)] hover:text-[var(--accent-primary)] px-2.5 py-1.5 rounded hover:bg-[var(--accent-primary)]/10"
              title="Copy file content"
            >
              Copy
            </button>
          )}
          <button
            onClick={onClose}
            className="text-[var(--muted)] hover:text-[var(--accent-primary)] transition-colors rounded-full p-1.5 hover:bg-[var(--accent-primary)]/10"
            aria-label="Close"
          >
            <FaTimes className="text-base" />
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-auto min-h-0">
        {isLoading && (
          <p className="p-4 text-sm text-[var(--muted)]">Loading…</p>
        )}
        {error && (
          <p className="p-4 text-sm text-[var(--highlight)]">{error}</p>
        )}
        {content !== null && (
          <SyntaxHighlighter
            language={getLanguageFromPath(filePath)}
            style={tomorrow}
            className="!text-sm !m-0"
            customStyle={{ margin: 0, padding: '1rem', height: '100%' }}
            showLineNumbers={true}
            wrapLongLines={false}
          >
            {content}
          </SyntaxHighlighter>
        )}
      </div>
    </div>
  );
}
